"""메인 트레이딩 루프.

한 주기의 흐름:

    잔고 조회 → 킬스위치 갱신 → 심볼별로 (캔들·티커·포지션 조회 → 전략 →
    리스크 → 주문) → 대기

한 심볼에서 난 오류가 다른 심볼이나 루프 전체를 죽이지 않도록 심볼 단위로
격리한다.
"""

from __future__ import annotations

import logging
import signal as signal_module
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

from bot.config import Config
from bot.exchanges.base import ExchangeError, FuturesExchange
from bot.execution import ExecutionResult, Executor
from bot.models import Candle, FundingRate, Position, Signal, SignalAction
from bot.risk import RiskManager
from bot.paper import PaperArena
from bot.store import Fill as StoredFill
from bot.store import Store
from bot.strategies import Strategy, StrategyContext, get_strategy

log = logging.getLogger(__name__)


@dataclass
class CycleReport:
    """한 주기의 요약 — 테스트, 로그, 웹 대시보드가 함께 읽는다."""

    equity: float
    open_positions: int
    results: list[ExecutionResult]
    halted: bool = False
    halt_reason: str = ""
    positions: dict[str, Position] = field(default_factory=dict)
    # 차트는 봇 루프가 이미 받아 온 캔들을 그대로 쓴다 — 요청 스레드가 거래소를
    # 다시 부르면 ccxt 세션이 경쟁한다.
    candles: dict[str, list[Candle]] = field(default_factory=dict)
    contract_sizes: dict[str, float] = field(default_factory=dict)
    at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


class TradingEngine:
    def __init__(
        self,
        config: Config,
        exchange: FuturesExchange,
        *,
        dry_run: bool = True,
        strategy: Strategy | None = None,
        store: Store | None = None,
        history_interval_sec: float = 60.0,
        funding_interval_sec: float = 300.0,
        arena: PaperArena | None = None,
    ) -> None:
        self.config = config
        self.exchange = exchange
        self.dry_run = dry_run
        # 기록은 매 주기마다 할 필요가 없다. 폴링이 15초인데 체결 조회까지
        # 매번 하면 레이트리밋만 태운다.
        self.store = store
        self._history_interval = history_interval_sec
        # 등록된 모든 전략을 동시에 모의매매로 굴린다. 실거래 전략이 무엇이든
        # 나머지도 같은 시세를 보며 성적을 남겨, 지금 쓰는 게 제일 나은지
        # 실시간 데이터로 판단할 수 있게 한다.
        self.arena = arena
        # 펀딩비율 캐시. 8시간마다 정산되는 값이라 매 주기(15초) 조회할 이유가
        # 없다 — 심볼마다 몇 분에 한 번만 갱신한다.
        self._funding_interval = funding_interval_sec
        self._funding: dict[str, FundingRate | None] = {}
        self._funding_at: dict[str, float] = {}
        # None = 아직 한 번도 동기화하지 않음. 0.0 으로 두면 부팅 직후처럼
        # monotonic() 이 작을 때 첫 동기화가 통째로 건너뛰어진다.
        self._last_history_sync: float | None = None
        self.strategy = strategy or get_strategy(config.strategy.name, config.strategy.params)
        self.risk = RiskManager(config.risk, leverage=config.exchange.leverage)
        self.executor = Executor(
            exchange,
            self.risk,
            dry_run=dry_run,
            allow_reverse=config.trading.allow_reverse,
            order_type=config.trading.order_type,
            limit_offset_pct=config.trading.limit_offset_pct,
            limit_timeout_sec=config.trading.limit_timeout_sec,
            limit_fallback_market=config.trading.limit_fallback_market,
        )
        self._stop = threading.Event()
        # 웹 대시보드가 폴링으로 읽어 가는 최신 상태. 루프 스레드가 쓰고 다른
        # 스레드가 읽으므로, 매번 새 객체로 통째로 갈아 끼워 찢긴 값을 막는다.
        self.last_report: CycleReport | None = None
        # 계약 크기는 prepare() 에서 한 번 읽어 둔다 — 손익 계산에 필요한데
        # 요청 스레드가 거래소를 다시 부를 수는 없다.
        self._contract_sizes: dict[str, float] = {}
        self.last_error: str | None = None
        self.started_at: datetime | None = None
        # 상위 시간대 캔들 캐시 (symbol, timeframe) → (받은 시각, 캔들).
        # 1시간봉은 1시간에 한 번 바뀌는데 매 주기 받아 오면 레이트리밋만 태운다.
        self._mtf_cache: dict[tuple[str, str], tuple[float, list[Candle]]] = {}

    # ------------------------------------------------------------------
    def prepare(self) -> None:
        """심볼 검증과 레버리지·마진 모드 설정. 루프 시작 전에 한 번 호출한다."""
        self.exchange.load_markets()
        for symbol in self.config.trading.symbols:
            market = self.exchange.market(symbol)  # 없는 심볼이면 여기서 걸린다
            self._contract_sizes[symbol] = market.contract_size
            self.exchange.set_leverage(
                symbol, self.config.exchange.leverage, self.config.exchange.margin_mode
            )
            log.info(
                "%s 준비 완료 (1계약=%s %s, 최소수량=%s)",
                symbol, market.contract_size, market.base, market.min_amount,
            )
            # 재시작하면 미체결 주문 추적 상태가 사라진다. 남아 있는 주문을
            # 알려 주어 사람이 판단할 수 있게 한다 — 손절 주문일 수도 있으므로
            # 자동으로 취소하지는 않는다.
            if not self.dry_run:
                try:
                    open_orders = self.exchange.fetch_open_orders(symbol)
                    if open_orders:
                        log.warning(
                            "%s 에 미체결 주문 %d건이 남아 있습니다 — 손절 주문일 수 있어 "
                            "자동으로 취소하지 않습니다. 대시보드에서 확인하세요.",
                            symbol, len(open_orders),
                        )
                except Exception:
                    log.debug("%s 미체결 주문 조회 실패", symbol, exc_info=True)
        mode = "DRY-RUN (주문 전송 안 함)" if self.dry_run else "*** 실거래 ***"
        log.info(
            "엔진 준비 완료 — %s | 거래소=%s 전략=%s 심볼=%s 주기=%ss",
            mode, self.exchange.id, self.strategy.name,
            ", ".join(self.config.trading.symbols), self.config.trading.poll_interval_sec,
        )

    # ------------------------------------------------------------------
    def run_cycle(self) -> CycleReport:
        """한 주기를 실행한다. 테스트에서 단독 호출하기 좋다."""
        balance = self.exchange.fetch_balance(self.config.trading.quote_currency)
        equity = balance.total
        self.risk.update_equity(equity)

        positions: dict[str, Position] = {}
        for symbol in self.config.trading.symbols:
            try:
                positions[symbol] = self.exchange.fetch_position(symbol)
            except ExchangeError:
                log.exception("%s 포지션 조회 실패 — 이번 주기 건너뜀", symbol)
                positions[symbol] = Position.flat(symbol)
        open_count = sum(1 for p in positions.values() if p.is_open)

        if self.risk.halted:
            log.warning("킬스위치 작동 중 — 신규 진입 차단, 청산 신호만 처리합니다")

        results: list[ExecutionResult] = []
        candles: dict[str, list[Candle]] = {}
        for symbol in self.config.trading.symbols:
            try:
                result = self._process_symbol(
                    symbol, positions[symbol], equity, open_count, candles
                )
            except ExchangeError as exc:
                log.exception("%s 처리 중 거래소 오류", symbol)
                result = ExecutionResult(symbol, "rejected", f"거래소 오류: {exc}")
            except Exception as exc:  # 전략 버그가 루프를 죽이지 않게 한다
                log.exception("%s 처리 중 예기치 못한 오류", symbol)
                result = ExecutionResult(symbol, "rejected", f"내부 오류: {exc}")
            results.append(result)
            if result.action == "entered":
                open_count += 1
            elif result.action == "exited":
                open_count = max(0, open_count - 1)

        self._sync_history(equity)

        report = CycleReport(
            equity=equity,
            open_positions=open_count,
            results=results,
            halted=self.risk.halted,
            halt_reason=self.risk.halt_reason,
            positions=positions,
            candles=candles,
            contract_sizes=self._contract_sizes,
        )
        self.last_report = report
        return report

    def _funding_rate(self, symbol: str) -> FundingRate | None:
        """모의매매 비용 계산용 펀딩비율. 몇 분간 캐시한다.

        8시간마다 정산되는 값이라 매 주기(15초) 조회할 이유가 없다. 조회에
        실패하면 None 을 캐시한다 — 거래소가 이 값을 안 주는 상황에서 매 주기
        재시도해 봐야 매매 주기만 느려진다. 받는 쪽이 보수적인 기본값으로
        대체한다.
        """
        now = time.monotonic()
        last = self._funding_at.get(symbol)
        if last is not None and now - last < self._funding_interval:
            return self._funding.get(symbol)
        try:
            rate = self.exchange.fetch_funding_rate(symbol)
        except Exception as exc:
            log.debug("%s 펀딩비 조회 실패 — 기본값으로 계산합니다: %s", symbol, exc)
            rate = None
        self._funding[symbol] = rate
        self._funding_at[symbol] = now
        return rate

    def _sync_history(self, equity: float) -> None:
        """체결 내역과 자기자본을 저장소에 남긴다.

        기록이 실패해도 매매는 계속되어야 한다 — 기록은 보조 기능이다.
        """
        if self.store is None:
            return
        now = time.monotonic()
        if (
            self._last_history_sync is not None
            and now - self._last_history_sync < self._history_interval
        ):
            return
        self._last_history_sync = now

        try:
            self.store.record_equity(int(datetime.now(timezone.utc).timestamp() * 1000), equity)
        except Exception:
            log.exception("자기자본 기록 실패 — 매매는 계속합니다")

        for symbol in self.config.trading.symbols:
            try:
                last = self.store.last_fill_timestamp(symbol)
                # 겹치게 조회한다. 경계에서 체결이 빠지는 것보다 중복이 낫고,
                # 중복은 저장소가 id 로 걸러 낸다.
                since = (last - 60_000) if last else None
                fills = self.exchange.fetch_my_trades(symbol, since)
                added = self.store.record_fills(
                    StoredFill(
                        id=f.id, symbol=f.symbol, timestamp=f.timestamp,
                        side=f.side.value, price=f.price, amount=f.amount,
                        cost=f.cost, fee=f.fee,
                    )
                    for f in fills
                )
                if added:
                    log.info("%s 체결 %d건 기록", symbol, added)
            except Exception:
                log.exception("%s 체결 내역 동기화 실패 — 매매는 계속합니다", symbol)

    # 다중 시간대 전략용 상위 캔들. 일목 구름(52+26봉)이 계산되고도 남는 양이다.
    MTF_CANDLE_LIMIT = 160
    MTF_REFRESH_SEC = 60.0

    def _mtf_candles(self, symbol: str) -> dict[str, list[Candle]]:
        """실거래·모의매매 전략들이 선언한 상위 시간대 캔들을 모아 온다.

        60초 캐시를 둔다 — 상위 봉은 느리게 바뀌므로 매 주기 받을 이유가 없고,
        조회가 실패하면 이전 값을 그대로 쓴다(오래된 구름이 없는 구름보다 낫다).
        """
        needed = set(self.strategy.extra_timeframes)
        if self.arena is not None:
            needed |= self.arena.extra_timeframes
        needed.discard(self.config.trading.timeframe)
        if not needed:
            return {}

        now = time.monotonic()
        out: dict[str, list[Candle]] = {}
        for timeframe in sorted(needed):
            key = (symbol, timeframe)
            cached = self._mtf_cache.get(key)
            if cached is not None and now - cached[0] < self.MTF_REFRESH_SEC:
                out[timeframe] = cached[1]
                continue
            try:
                candles = self.exchange.fetch_candles(
                    symbol, timeframe, self.MTF_CANDLE_LIMIT
                )
                self._mtf_cache[key] = (now, candles)
                out[timeframe] = candles
            except Exception:
                log.exception("%s %s 캔들 조회 실패 — 이전 값으로 계속합니다", symbol, timeframe)
                if cached is not None:
                    out[timeframe] = cached[1]
        return out

    def _process_symbol(
        self,
        symbol: str,
        position: Position,
        equity: float,
        open_count: int,
        candle_sink: dict[str, list[Candle]] | None = None,
    ) -> ExecutionResult:
        # 미체결 지정가 주문이 있으면 그 결과부터 확인한다. 체결됐으면 여기서
        # 손절 주문이 걸리고, 아직이면 이번 주기는 기다린다.
        if not self.dry_run:
            pending_result = self.executor.reconcile(symbol, position)
            if pending_result is not None:
                return pending_result

        candles = self.exchange.fetch_candles(
            symbol, self.config.trading.timeframe, self.config.trading.candle_limit
        )
        if candle_sink is not None:
            candle_sink[symbol] = candles
        warmup = self.strategy.warmup_candles
        if warmup and len(candles) < warmup + 1:  # +1: 마지막 미완성 캔들 몫
            return ExecutionResult(
                symbol, "none", f"워밍업 대기 ({len(candles)}/{warmup + 1} 캔들)"
            )

        ticker = self.exchange.fetch_ticker(symbol)
        mtf = self._mtf_candles(symbol)

        # 모의매매는 실거래 전략과 무관하게 항상 돈다. 여기서 터져도 실제 매매는
        # 계속되어야 한다 — 비교용 기능이 본업을 막으면 안 된다.
        if self.arena is not None:
            try:
                self.arena.step(
                    symbol, candles, ticker, self._funding_rate(symbol), mtf_candles=mtf
                )
            except Exception:
                log.exception("%s 모의매매 처리 실패 — 실거래는 계속합니다", symbol)

        ctx = StrategyContext(
            symbol=symbol,
            timeframe=self.config.trading.timeframe,
            candles=candles,
            ticker=ticker,
            position=position,
            equity=equity,
            mtf_candles=mtf,
        )
        sig = self.strategy.generate(ctx)
        if not isinstance(sig, Signal):
            raise TypeError(
                f"전략 '{self.strategy.name}' 이 Signal 이 아닌 {type(sig).__name__} 을 반환했습니다"
            )

        # 킬스위치가 걸린 동안에는 진입만 막고 청산은 통과시킨다.
        if self.risk.halted and sig.is_entry:
            return ExecutionResult(
                symbol, "rejected", f"킬스위치 작동 중 — {self.risk.halt_reason}"
            )

        if sig.action is not SignalAction.HOLD:
            log.info("%s 신호: %s (%s)", symbol, sig.action.value, sig.reason or "사유 없음")

        return self.executor.handle(
            symbol=symbol,
            signal=sig,
            position=position,
            price=ticker.last,
            equity=equity,
            open_positions=open_count,
        )

    # ------------------------------------------------------------------
    def run(self, *, install_signal_handlers: bool = True) -> None:
        """중지 요청을 받을 때까지 주기를 반복한다.

        웹 서버가 이 엔진을 백그라운드 스레드로 돌릴 때는 시그널 핸들러를
        설치하면 안 된다 — 프로세스 종료는 웹 서버가 관리한다.
        """
        if install_signal_handlers:
            self._install_signal_handlers()
        self.started_at = datetime.now(timezone.utc)
        self.prepare()
        try:
            while not self._stop.is_set():
                try:
                    report = self.run_cycle()
                    self.last_error = None
                    self._log_cycle(report)
                except ExchangeError as exc:
                    self.last_error = f"거래소 오류: {exc}"
                    log.exception("주기 실행 중 거래소 오류 — 다음 주기에 재시도합니다")
                except Exception as exc:
                    self.last_error = f"내부 오류: {exc}"
                    log.exception("주기 실행 중 예기치 못한 오류 — 다음 주기에 재시도합니다")
                self._stop.wait(self.config.trading.poll_interval_sec)
        finally:
            self.shutdown()

    def _log_cycle(self, report: CycleReport) -> None:
        traded = [r for r in report.results if r.traded]
        log.info(
            "주기 완료 — 자기자본 %.2f %s, 보유 %d%s",
            report.equity, self.config.trading.quote_currency, report.open_positions,
            f", 체결 {len(traded)}건" if traded else "",
        )
        for r in report.results:
            level = logging.INFO if r.traded or r.action == "rejected" else logging.DEBUG
            log.log(level, "  %s: [%s] %s", r.symbol, r.action, r.detail)

    def stop(self) -> None:
        self._stop.set()

    def _install_signal_handlers(self) -> None:
        def handler(signum, _frame):
            log.info("시그널 %s 수신 — 현재 주기를 마치고 종료합니다", signum)
            self.stop()

        for sig in (signal_module.SIGINT, signal_module.SIGTERM):
            try:
                signal_module.signal(sig, handler)
            except ValueError:  # pragma: no cover - 메인 스레드가 아닐 때
                log.debug("시그널 %s 핸들러를 설치할 수 없습니다", sig)

    def shutdown(self) -> None:
        if self.config.trading.close_positions_on_exit and not self.dry_run:
            log.info("close_positions_on_exit=true — 보유 포지션을 정리합니다")
            for symbol in self.config.trading.symbols:
                try:
                    position = self.exchange.fetch_position(symbol)
                    if position.is_open:
                        self.executor.handle(
                            symbol=symbol,
                            signal=Signal(action=SignalAction.EXIT, reason="봇 종료"),
                            position=position,
                            price=0.0,
                            equity=0.0,
                            open_positions=1,
                        )
                except Exception:
                    log.exception("%s 종료 청산 실패 — 거래소에서 직접 확인하세요", symbol)
        self.exchange.close()
        log.info("엔진 종료")
