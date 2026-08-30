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
from dataclasses import dataclass

from bot.config import Config
from bot.exchanges.base import ExchangeError, FuturesExchange
from bot.execution import ExecutionResult, Executor
from bot.models import Position, Signal, SignalAction
from bot.risk import RiskManager
from bot.strategies import Strategy, StrategyContext, get_strategy

log = logging.getLogger(__name__)


@dataclass
class CycleReport:
    """한 주기의 요약 — 테스트와 로그에서 쓴다."""

    equity: float
    open_positions: int
    results: list[ExecutionResult]
    halted: bool = False


class TradingEngine:
    def __init__(
        self,
        config: Config,
        exchange: FuturesExchange,
        *,
        dry_run: bool = True,
        strategy: Strategy | None = None,
    ) -> None:
        self.config = config
        self.exchange = exchange
        self.dry_run = dry_run
        self.strategy = strategy or get_strategy(config.strategy.name, config.strategy.params)
        self.risk = RiskManager(config.risk, leverage=config.exchange.leverage)
        self.executor = Executor(
            exchange,
            self.risk,
            dry_run=dry_run,
            allow_reverse=config.trading.allow_reverse,
        )
        self._stop = threading.Event()

    # ------------------------------------------------------------------
    def prepare(self) -> None:
        """심볼 검증과 레버리지·마진 모드 설정. 루프 시작 전에 한 번 호출한다."""
        self.exchange.load_markets()
        for symbol in self.config.trading.symbols:
            market = self.exchange.market(symbol)  # 없는 심볼이면 여기서 걸린다
            self.exchange.set_leverage(
                symbol, self.config.exchange.leverage, self.config.exchange.margin_mode
            )
            log.info(
                "%s 준비 완료 (1계약=%s %s, 최소수량=%s)",
                symbol, market.contract_size, market.base, market.min_amount,
            )
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
        for symbol in self.config.trading.symbols:
            try:
                result = self._process_symbol(symbol, positions[symbol], equity, open_count)
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

        return CycleReport(
            equity=equity, open_positions=open_count, results=results, halted=self.risk.halted
        )

    def _process_symbol(
        self, symbol: str, position: Position, equity: float, open_count: int
    ) -> ExecutionResult:
        candles = self.exchange.fetch_candles(
            symbol, self.config.trading.timeframe, self.config.trading.candle_limit
        )
        warmup = self.strategy.warmup_candles
        if warmup and len(candles) < warmup + 1:  # +1: 마지막 미완성 캔들 몫
            return ExecutionResult(
                symbol, "none", f"워밍업 대기 ({len(candles)}/{warmup + 1} 캔들)"
            )

        ticker = self.exchange.fetch_ticker(symbol)
        ctx = StrategyContext(
            symbol=symbol,
            timeframe=self.config.trading.timeframe,
            candles=candles,
            ticker=ticker,
            position=position,
            equity=equity,
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
    def run(self) -> None:
        """SIGINT/SIGTERM 을 받을 때까지 주기를 반복한다."""
        self._install_signal_handlers()
        self.prepare()
        try:
            while not self._stop.is_set():
                try:
                    report = self.run_cycle()
                    self._log_cycle(report)
                except ExchangeError:
                    log.exception("주기 실행 중 거래소 오류 — 다음 주기에 재시도합니다")
                except Exception:
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
