"""봇 스레드의 생명주기 관리.

**동시성 원칙: 거래소 객체는 한 번에 한 스레드만 만진다.**

ccxt 의 동기 클라이언트는 내부적으로 `requests.Session` 을 재사용하는데 이는
스레드 안전하지 않다. 그래서 웹 요청 스레드는 거래소를 직접 호출하지 않는다:

* 봇이 도는 동안 — 대시보드는 봇 루프가 이미 받아 둔 `engine.last_report` 만
  읽는다. 네트워크 호출이 없으니 경쟁도 없고 응답도 즉시 나간다.
* 봇이 멈춰 있을 때 — 요청 스레드가 잠깐 쓰고 닫는 임시 거래소를 만든다.
* 긴급 청산 — 봇을 먼저 완전히 멈춘 뒤 청산한다. 청산 직후 봇이 다시 진입해
  버리는 사고를 막는 의미도 있다.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable

from bot.config import Config
from bot.engine import TradingEngine
from bot.exchanges import create_exchange
from bot.exchanges.base import FuturesExchange
from bot.execution import Executor
from bot.models import Position, Signal, SignalAction
from bot.paper import PaperArena, StrategyStats
from bot.performance import Performance, summarize
from bot.risk import RiskManager
from bot.store import Store

log = logging.getLogger(__name__)

ExchangeFactory = Callable[[], FuturesExchange]


class SupervisorError(Exception):
    """봇을 시작/정지할 수 없는 상태."""


@dataclass
class PositionView:
    symbol: str
    side: str
    contracts: float = 0.0
    entry_price: float | None = None
    notional: float = 0.0
    unrealized_pnl: float = 0.0
    liquidation_price: float | None = None

    @classmethod
    def from_position(cls, position: Position) -> "PositionView":
        return cls(
            symbol=position.symbol,
            side=position.side.value,
            contracts=position.contracts,
            entry_price=position.entry_price,
            notional=position.notional,
            unrealized_pnl=position.unrealized_pnl,
            liquidation_price=position.liquidation_price,
        )


@dataclass
class StatusSnapshot:
    """대시보드가 폴링으로 가져가는 상태. 시크릿은 절대 담지 않는다."""

    running: bool
    live: bool
    exchange: str
    strategy: str
    symbols: list[str]
    timeframe: str
    leverage: float
    quote_currency: str
    started_at: str | None = None
    last_cycle_at: str | None = None
    equity: float | None = None
    day_start_equity: float | None = None
    open_positions: int = 0
    halted: bool = False
    halt_reason: str = ""
    last_error: str | None = None
    positions: list[PositionView] = field(default_factory=list)


class BotSupervisor:
    """엔진을 백그라운드 스레드로 돌리고 상태를 노출한다."""

    def __init__(
        self,
        config: Config,
        *,
        exchange_factory: ExchangeFactory | None = None,
        join_timeout: float = 60.0,
        positions_cache_ttl: float = 5.0,
        positions_error_cache_ttl: float = 15.0,
        store: Store | None = None,
    ) -> None:
        self.config = config
        self.store = store
        # 전략 경쟁 모의매매. 봇이 꺼져 있어도 순위표는 볼 수 있어야 하므로
        # 여기서 만들어 들고 있는다.
        self.arena = PaperArena(config, store) if store is not None else None
        self._exchange_factory = exchange_factory or (lambda: create_exchange(config.exchange))
        self._join_timeout = join_timeout
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._engine: TradingEngine | None = None
        self._live = False
        self._start_error: str | None = None
        # 봇이 멈춰 있을 때의 포지션 조회 결과 캐시. 대시보드는 몇 초마다
        # 폴링하고 탭이 여러 개일 수도 있으므로, 캐시가 없으면 거래소
        # 레이트리밋을 그대로 태운다.
        #
        # 실패는 더 길게 캐시한다. 거래소가 죽으면 한 번의 조회가 재시도
        # 백오프 때문에 수 초씩 걸리는데, 성공과 같은 TTL 을 쓰면 응답이
        # 돌아올 때쯤 캐시가 이미 만료돼 재시도 폭주를 전혀 막지 못한다.
        self._positions_cache_ttl = positions_cache_ttl
        self._positions_error_cache_ttl = positions_error_cache_ttl
        self._positions_cache: tuple[float, list[PositionView] | Exception] | None = None
        self._positions_lock = threading.Lock()
        # 봇이 멈춰 있을 때의 차트용 캔들 캐시. 포지션과 같은 이유로 캐시한다.
        self._candles_cache: dict[str, tuple[float, list[dict[str, float]]]] = {}
        self._candles_lock = threading.Lock()

    # ------------------------------------------------------------------
    @property
    def running(self) -> bool:
        thread = self._thread
        return thread is not None and thread.is_alive()

    # ------------------------------------------------------------------
    def start(self, *, live: bool) -> None:
        with self._lock:
            if self.running:
                raise SupervisorError("봇이 이미 실행 중입니다")
            exchange = self._exchange_factory()
            engine = TradingEngine(
                self.config, exchange, dry_run=not live, store=self.store, arena=self.arena
            )
            self._engine = engine
            self._live = live
            self._start_error = None

            def target() -> None:
                try:
                    # 시그널 핸들러는 웹 서버 몫이다 — 여기서 가로채면 안 된다.
                    engine.run(install_signal_handlers=False)
                except Exception as exc:
                    self._start_error = str(exc)
                    log.exception("봇 스레드가 오류로 종료되었습니다")

            thread = threading.Thread(target=target, name="trading-engine", daemon=True)
            self._thread = thread
            thread.start()
            log.warning(
                "봇 시작 — %s 모드", "실거래" if live else "DRY-RUN"
            )
        self.invalidate_positions_cache()

    def stop(self, *, timeout: float | None = None) -> bool:
        """정지를 요청하고 스레드가 끝날 때까지 기다린다.

        엔진은 현재 주기를 마치고 멈추므로, 폴링 주기만큼은 걸릴 수 있다.
        """
        with self._lock:
            engine, thread = self._engine, self._thread
            if engine is None or thread is None or not thread.is_alive():
                self._thread = None
                return True
            engine.stop()
        thread.join(timeout if timeout is not None else self._join_timeout)
        stopped = not thread.is_alive()
        if stopped:
            self._thread = None
            log.warning("봇 정지 완료")
        else:
            log.error("봇 스레드가 제한 시간 안에 멈추지 않았습니다")
        return stopped

    # ------------------------------------------------------------------
    def snapshot(self) -> StatusSnapshot:
        """현재 상태. 네트워크 호출을 하지 않으므로 언제 불러도 즉시 돌아온다."""
        engine = self._engine
        cfg = self.config
        snapshot = StatusSnapshot(
            running=self.running,
            live=self._live and self.running,
            exchange=cfg.exchange.id,
            strategy=cfg.strategy.name,
            symbols=list(cfg.trading.symbols),
            timeframe=cfg.trading.timeframe,
            leverage=cfg.exchange.leverage,
            quote_currency=cfg.trading.quote_currency,
            last_error=self._start_error,
        )
        if engine is None:
            return snapshot

        snapshot.started_at = _iso(engine.started_at)
        snapshot.last_error = engine.last_error or self._start_error
        snapshot.day_start_equity = engine.risk.day_start_equity
        report = engine.last_report
        if report is not None:
            snapshot.last_cycle_at = _iso(report.at)
            snapshot.equity = report.equity
            snapshot.open_positions = report.open_positions
            snapshot.halted = report.halted
            snapshot.halt_reason = report.halt_reason
            snapshot.positions = [
                PositionView.from_position(p)
                for p in report.positions.values()
                if p.is_open
            ]
        return snapshot

    # ------------------------------------------------------------------
    def candles(self, symbol: str, *, now: float | None = None) -> list[dict[str, float]]:
        """차트용 캔들.

        봇이 도는 동안에는 봇 루프가 이미 받아 둔 것을 쓴다 — 요청 스레드가
        거래소를 다시 부르면 ccxt 세션이 경쟁하기 때문이다. 봇이 멈춰 있을
        때는 그 제약이 없으므로 직접 받아 온다. 차트를 보려고 봇을 켜야 할
        이유는 없다.
        """
        if self.running:
            report = self._engine.last_report if self._engine else None
            return _to_chart(report.candles.get(symbol, [])) if report else []

        clock = (lambda: now) if now is not None else time.monotonic
        with self._candles_lock:
            cached = self._candles_cache.get(symbol)
            if cached is not None and clock() - cached[0] < self._positions_cache_ttl:
                return cached[1]
            try:
                exchange = self._exchange_factory()
                try:
                    candles = _to_chart(
                        exchange.fetch_candles(
                            symbol, self.config.trading.timeframe,
                            self.config.trading.candle_limit,
                        )
                    )
                finally:
                    exchange.close()
            except Exception:
                log.warning("%s 차트 캔들 조회 실패", symbol, exc_info=True)
                # 실패도 캐시해 장애 중 재시도가 폭주하지 않게 한다.
                self._candles_cache[symbol] = (clock(), [])
                return []
            self._candles_cache[symbol] = (clock(), candles)
            return candles

    def contract_size(self, symbol: str) -> float:
        """손익 계산에 쓰는 계약 크기.

        봇이 한 번이라도 돌았으면 그때 읽어 둔 값을 쓴다. 그 전이라면 거래소에
        물어본다 — Gate 는 1계약이 0.0001 BTC 라, 이 값을 1 로 두면 수익률이
        1만 배로 어긋난다.
        """
        report = self._engine.last_report if self._engine else None
        if report is not None and report.contract_sizes.get(symbol):
            return report.contract_sizes[symbol]
        if self.running:
            return 1.0
        try:
            exchange = self._exchange_factory()
            try:
                return exchange.market(symbol).contract_size or 1.0
            finally:
                exchange.close()
        except Exception:
            log.debug("%s 계약 크기 조회 실패", symbol, exc_info=True)
            return 1.0

    def leaderboard(self) -> list[StrategyStats]:
        """전략 경쟁 순위표. 봇이 꺼져 있어도 지금까지의 성적을 보여 준다."""
        if self.arena is None:
            return []
        report = self._engine.last_report if self._engine else None
        prices = {}
        if report is not None:
            for symbol, candles in report.candles.items():
                if candles:
                    prices[symbol] = candles[-1].close
        return self.arena.leaderboard(prices)

    def reset_paper(self) -> None:
        if self.arena is not None:
            self.arena.reset()

    def performance(self, symbol: str | None = None) -> Performance:
        """기록해 둔 체결과 자기자본으로 성과를 계산한다."""
        if self.store is None:
            return Performance()
        target = symbol or (self.config.trading.symbols[0] if self.config.trading.symbols else None)
        return summarize(
            self.store.fills(target),
            self.store.equity_curve(),
            contract_size=self.contract_size(target) if target else 1.0,
        )

    def fetch_positions_live(self, *, now: float | None = None) -> list[PositionView]:
        """거래소에 직접 물어본다. 봇이 멈춰 있을 때만 쓴다.

        결과는 짧게 캐시된다 — 대시보드 폴링 주기나 열린 탭 수와 무관하게
        거래소 호출을 TTL 당 한 번으로 묶기 위해서다.
        """
        if self.running:
            raise SupervisorError(
                "봇이 실행 중입니다 — 실행 중에는 최신 주기 결과를 사용하세요"
            )
        clock = (lambda: now) if now is not None else time.monotonic
        with self._positions_lock:
            cached = self._positions_cache
            if cached is not None:
                age = clock() - cached[0]
                ttl = (
                    self._positions_error_cache_ttl
                    if isinstance(cached[1], Exception)
                    else self._positions_cache_ttl
                )
                if age < ttl:
                    if isinstance(cached[1], Exception):
                        raise cached[1]
                    return cached[1]

            # 캐시 시각은 조회가 *끝난* 시점으로 잡는다. 시작 시점으로 잡으면
            # 조회 자체가 오래 걸릴 때(거래소 장애 시의 재시도 백오프) 결과를
            # 받자마자 캐시가 만료돼 버린다.
            try:
                views = self._fetch_positions_uncached()
            except Exception as exc:
                self._positions_cache = (clock(), exc)
                raise
            self._positions_cache = (clock(), views)
            return views

    def _fetch_positions_uncached(self) -> list[PositionView]:
        exchange = self._exchange_factory()
        try:
            views = []
            for symbol in self.config.trading.symbols:
                position = exchange.fetch_position(symbol)
                if position.is_open:
                    views.append(PositionView.from_position(position))
            return views
        finally:
            exchange.close()

    def invalidate_positions_cache(self) -> None:
        """포지션이 바뀐 직후(청산 등) 캐시를 버린다."""
        with self._positions_lock:
            self._positions_cache = None

    # ------------------------------------------------------------------
    def close_all_positions(self) -> list[str]:
        """긴급 정지: 봇을 멈추고 보유 포지션을 전부 시장가 청산한다.

        먼저 멈추는 이유는 두 가지다 — 거래소 객체를 두 스레드가 동시에 만지지
        않게 하고, 청산 직후 봇이 곧바로 재진입하는 것을 막기 위해서다.
        """
        was_running = self.running
        if was_running and not self.stop():
            raise SupervisorError(
                "봇을 멈추지 못해 청산을 중단했습니다. 거래소에서 직접 확인하세요."
            )

        exchange = self._exchange_factory()
        try:
            executor = Executor(
                exchange,
                RiskManager(self.config.risk, leverage=self.config.exchange.leverage),
                dry_run=False,
            )
            messages: list[str] = []
            for symbol in self.config.trading.symbols:
                position = exchange.fetch_position(symbol)
                if not position.is_open:
                    continue
                result = executor.handle(
                    symbol=symbol,
                    signal=Signal(action=SignalAction.EXIT, reason="대시보드 긴급 청산"),
                    position=position,
                    price=0.0,
                    equity=0.0,
                    open_positions=1,
                )
                messages.append(f"{symbol}: {result.detail}")
            if not messages:
                messages.append("청산할 포지션이 없습니다")
            log.warning("긴급 청산 실행 — %s", "; ".join(messages))
            self.invalidate_positions_cache()
            return messages
        finally:
            exchange.close()


def _to_chart(candles) -> list[dict[str, float]]:
    """차트 라이브러리가 읽는 형태로 바꾼다. 시각은 초 단위를 쓴다."""
    return [
        {
            "time": candle.timestamp // 1000,
            "open": candle.open,
            "high": candle.high,
            "low": candle.low,
            "close": candle.close,
        }
        for candle in candles
    ]


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.isoformat(timespec="seconds")
