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

import hmac
import json
import logging
import secrets
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable

from bot.ai_traders import AITrader, build_traders
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

# 이 시간보다 짧게 살다 죽은 실행은 "정상 기동" 으로 치지 않는다. 설정이
# 잘못돼 매 주기 죽는 상황에서 감시견이 무한 재시작 루프를 도는 것을 막는다.
MIN_HEALTHY_RUN_SEC = 120.0
MAX_RESTART_BACKOFF_SEC = 300.0

# AI 대회: 스펙 수정 쿨다운과 대회 기간. 공정성은 서버가 강제해야 의미가 있다.
AI_SPEC_COOLDOWN_SEC = 6 * 3600
AI_COMPETITION_DAYS = 28


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
    # 자동 재시작(감시견) 상태. 재배포·크래시 뒤에도 계속 돌고 있는지 화면에서
    # 확인할 수 있어야 한다.
    auto_restart: bool = False
    auto_restart_live: bool = False
    auto_restart_count: int = 0
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
        autorestart_interval: float = 15.0,
    ) -> None:
        self.config = config
        self.store = store
        # 대시보드에서 바꾼 회당 주문 금액을 재기동 후에도 유지한다.
        # config.risk 객체는 엔진·실행기·모의매매가 전부 공유하므로, 리스트를
        # 제자리에서 바꾸면 즉시 모든 곳에 반영된다.
        if store is not None:
            saved = store.get_setting("order_notional")
            if saved:
                try:
                    self._apply_order_notional(float(saved))
                except (ValueError, TypeError):
                    pass
        # 전략 경쟁 모의매매. 봇이 꺼져 있어도 순위표는 볼 수 있어야 하므로
        # 여기서 만들어 들고 있는다. AI 수동 매매(클로드/GPT/그록)도 같은
        # 아레나에 합류해 알고리즘 전략들과 같은 규칙으로 경쟁한다.
        self.ai_traders: dict[str, AITrader] = build_traders()
        if store is not None:
            # 저장해 둔 봇 스펙을 복원한다. 즉시 주문(orders)은 다시 큐에 넣지
            # 않는다 — 재배포할 때마다 같은 주문이 또 나가면 안 된다.
            for name, trader in self.ai_traders.items():
                saved = store.get_setting(f"ai_spec:{name}")
                if not saved:
                    continue
                try:
                    trader.set_spec(json.loads(saved))
                    trader.clear_pending()
                except (ValueError, TypeError):
                    log.warning("'%s' 의 저장된 봇 스펙을 읽을 수 없습니다", name)
        self.arena = (
            # 슬리피지 0.01%: 시장가 즉시 체결을 가정하되 호가+슬리피지로
            # 체결가를 계산한다 (대회 규칙).
            PaperArena(config, store, extra_strategies=self.ai_traders,
                       slippage_pct=0.01)
            if store is not None
            else None
        )
        # AI 대회 기간: 첫 봇 스펙이 적용된 순간부터 4주. 종료 시점의
        # 수익률이 최종 순위다.
        if self.arena is not None and store is not None:
            saved_start = store.get_setting("ai_competition_start_ms")
            if saved_start:
                try:
                    self.arena.ai_end_ms = (
                        int(saved_start) + AI_COMPETITION_DAYS * 86_400_000
                    )
                except (ValueError, TypeError):
                    pass
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
        # 자동 재시작. "돌고 있어야 한다" 는 의사(_want_running)를 따로 두고,
        # 감시견 스레드가 실제 상태를 거기에 맞춘다. 재배포로 프로세스가 새로
        # 뜨거나 봇 스레드가 예외로 죽어도 모의매매가 계속 기록되게 하는 장치다.
        #
        # 대시보드의 정지 버튼과 긴급 청산은 이 의사를 끈다 — 끄자마자 15초 뒤
        # 되살아나면 정지 버튼이 아무 의미가 없다.
        self._autorestart_interval = autorestart_interval
        self._want_running = False
        self._want_live = False
        self._watchdog: threading.Thread | None = None
        self._watchdog_stop = threading.Event()
        self._autorestart_count = 0
        self._start_failures = 0
        self._next_attempt_at = 0.0
        self._last_start_at: float | None = None

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
            # 여기서 먼저 기록해 둔다. 거래소 접속이 실패해도 감시견이 계속
            # 재시도해야 하기 때문이다 — 부팅 직후 네트워크가 늦게 뜨는 경우.
            self._want_running = True
            self._want_live = live
            self._last_start_at = time.monotonic()
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
            # 사람이 멈춘 것은 "이제 돌지 말라" 는 뜻이다. 감시견이 되살리지 않는다.
            self._want_running = False
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

    # --- 자동 재시작 ---------------------------------------------------
    def enable_autorestart(self, *, live: bool = False) -> None:
        """프로세스가 사는 동안 봇이 계속 돌아가게 한다.

        곧바로 start() 를 부르지 않고 감시견에게 맡긴다. 거래소 접속이 느리거나
        실패해도 웹 서버가 뜨는 것을 막지 않기 위해서다 — 화면조차 안 뜨면
        무엇이 잘못됐는지 볼 방법이 없다.
        """
        self._want_running = True
        self._want_live = live
        self._next_attempt_at = 0.0
        self._start_failures = 0
        if self._watchdog is not None and self._watchdog.is_alive():
            return
        self._watchdog_stop.clear()
        thread = threading.Thread(
            target=self._watchdog_loop, name="bot-watchdog", daemon=True
        )
        self._watchdog = thread
        thread.start()
        log.warning(
            "자동 재시작 켜짐 — %s 모드로 계속 돌립니다 (%.0f초마다 확인)",
            "실거래" if live else "DRY-RUN",
            self._autorestart_interval,
        )

    def disable_autorestart(self) -> None:
        """감시견을 끈다. 봇 자체는 건드리지 않는다."""
        self._want_running = False
        self._watchdog_stop.set()
        self._watchdog = None

    @property
    def autorestart(self) -> bool:
        return self._want_running

    def _watchdog_loop(self) -> None:
        # 첫 확인은 기다리지 않고 바로 한다.
        while True:
            try:
                self.autorestart_tick()
            except Exception:  # 감시견은 어떤 이유로도 죽으면 안 된다
                log.exception("자동 재시작 확인 중 오류")
            if self._watchdog_stop.wait(self._autorestart_interval):
                return

    def autorestart_tick(self, *, now: float | None = None) -> bool:
        """필요하면 봇을 다시 띄운다. 실제로 시작했으면 True.

        테스트에서 시간을 넣어 부를 수 있도록 공개 메서드로 둔다.
        """
        if not self._want_running or self.running:
            return False
        clock = time.monotonic() if now is None else now
        if clock < self._next_attempt_at:
            return False

        # 방금 띄웠는데 벌써 죽어 있다면 설정이나 거래소 쪽 문제다. 실패로 세어
        # 간격을 벌린다 — 15초마다 재시작을 반복하면 로그만 지저분해지고
        # 거래소 레이트리밋만 태운다.
        if (
            self._last_start_at is not None
            and clock - self._last_start_at < MIN_HEALTHY_RUN_SEC
        ):
            self._note_start_failure(clock, self._start_error or "봇 스레드가 곧바로 종료됨")
            return False

        want_live = self._want_live
        try:
            self.start(live=want_live)
        except Exception as exc:
            self._note_start_failure(clock, str(exc))
            return False

        self._start_failures = 0
        self._autorestart_count += 1
        log.warning(
            "봇 자동 시작 (%d회째) — %s 모드",
            self._autorestart_count, "실거래" if want_live else "DRY-RUN",
        )
        return True

    def _note_start_failure(self, clock: float, reason: str) -> None:
        self._start_failures += 1
        delay = min(
            MAX_RESTART_BACKOFF_SEC,
            self._autorestart_interval * (2 ** min(self._start_failures, 5)),
        )
        self._next_attempt_at = clock + delay
        self._last_start_at = None
        log.error(
            "봇 자동 시작 실패 (%d회) — %.0f초 뒤 다시 시도합니다: %s",
            self._start_failures, delay, reason,
        )

    def shutdown(self) -> None:
        """프로세스 종료용. 감시견을 끄고 봇을 멈춘다."""
        self.disable_autorestart()
        self.stop()

    # ------------------------------------------------------------------
    def order_notional(self) -> float:
        """현재 회당 주문 금액 (USDT)."""
        tiers = self.config.risk.notional_tiers
        return tiers[0] if tiers else 0.0

    def set_order_notional(self, value: float) -> None:
        """회당 주문 금액을 바꾼다. 실행 중인 봇과 모의매매에 즉시 반영된다."""
        self._apply_order_notional(value)
        if self.store is not None:
            self.store.set_setting("order_notional", str(value))

    def _apply_order_notional(self, value: float) -> None:
        self.config.risk.notional_tiers[:] = [value] * max(
            4, len(self.config.risk.notional_tiers)
        )

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
            auto_restart=self._want_running,
            auto_restart_live=self._want_live,
            auto_restart_count=self._autorestart_count,
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
        """모의매매 기록 초기화. AI 대회(스펙·쿨다운·기간)도 함께 리셋된다."""
        if self.arena is not None:
            self.arena.reset()
            self.arena.ai_end_ms = 0
        for name, trader in self.ai_traders.items():
            trader.clear_pending()
            trader.set_spec({})   # 기본값(아무것도 안 함)으로 되돌린다
            if self.store is not None:
                self.store.set_setting(f"ai_spec:{name}", "")
                self.store.set_setting(f"ai_spec_at:{name}", "")
        if self.store is not None:
            self.store.set_setting("ai_competition_start_ms", "")

    # ------------------------------------------------------------------
    def submit_ai_order(self, trader: str, action: str, notional: float = 0.0) -> dict:
        """AI 수동 매매 주문을 큐에 넣는다. 다음 봇 주기에 체결된다."""
        if trader not in self.ai_traders:
            raise SupervisorError(f"알 수 없는 AI 트레이더 '{trader}'")
        if self.arena is None:
            raise SupervisorError("모의매매 저장소가 없어 AI 매매를 쓸 수 없습니다")
        return self.ai_traders[trader].submit(action, notional)

    def ai_state(self) -> list[dict]:
        """AI 트레이더별 현재 봇 스펙과 대기 주문."""
        return [
            {
                "name": name,
                "label": t.label,
                "pending": t.pending(),
                "spec": t.spec,
                "leverage": t.leverage,
                "spec_next_allowed_ms": self.ai_spec_next_allowed_ms(name),
            }
            for name, t in self.ai_traders.items()
        ]

    def ai_token(self, trader: str) -> str:
        """AI 전용 접근 토큰. 처음 요청될 때 만들어 저장한다."""
        if trader not in self.ai_traders:
            raise SupervisorError(f"알 수 없는 AI 트레이더 '{trader}'")
        if self.store is None:
            raise SupervisorError("저장소가 없어 토큰을 만들 수 없습니다")
        key = f"ai_token:{trader}"
        existing = self.store.get_setting(key)
        if existing:
            return existing
        token = "aibot_" + secrets.token_urlsafe(18)
        self.store.set_setting(key, token)
        log.warning("AI 접근 토큰 발급 — %s", trader)
        return token

    def ai_trader_by_token(self, token: str | None) -> AITrader | None:
        """토큰으로 트레이더를 찾는다. 토큰이 곧 권한이다 — 자기 봇만 조작한다."""
        if not token or self.store is None:
            return None
        for name, trader in self.ai_traders.items():
            stored = self.store.get_setting(f"ai_token:{name}")
            if stored and hmac.compare_digest(stored, token):
                return trader
        return None

    def ai_spec_next_allowed_ms(self, trader: str) -> int:
        """이 트레이더가 다음으로 스펙을 바꿀 수 있는 시각(ms). 0 = 지금 가능."""
        if self.store is None:
            return 0
        last = self.store.get_setting(f"ai_spec_at:{trader}")
        if not last:
            return 0
        try:
            allowed = int(last) + AI_SPEC_COOLDOWN_SEC * 1000
        except (ValueError, TypeError):
            return 0
        return allowed if allowed > int(time.time() * 1000) else 0

    def ai_competition_end_ms(self) -> int:
        """대회 종료 시각(ms). 0 = 아직 시작 전 (첫 스펙 적용 시 시작)."""
        return self.arena.ai_end_ms if self.arena is not None else 0

    def ai_set_spec(self, trader: str, raw) -> list[str]:
        """봇 스펙 교체. 오류 목록이 비면 성공이고, 스펙은 저장소에 남는다.

        대회 규칙: 성공한 교체는 6시간에 1회만 — API 를 직접 부르는 AI 가
        15초마다 고치면 사람이 전달해 주는 AI 와 공정하지 않다. 검증에 실패한
        시도는 횟수에 세지 않는다 (고쳐서 다시 낼 수 있어야 한다).
        """
        if trader not in self.ai_traders:
            raise SupervisorError(f"알 수 없는 AI 트레이더 '{trader}'")
        # 검증을 먼저 한다 — 형식이 틀린 시도는 쿨다운을 소모하지 않아야
        # AI 가 오류를 고쳐서 바로 다시 낼 수 있다.
        from bot.ai_traders import validate_spec
        _, validation_errors = validate_spec(raw)
        if validation_errors:
            return validation_errors
        now_ms = int(time.time() * 1000)
        end = self.ai_competition_end_ms()
        if end and now_ms > end:
            return ["대회가 종료되었습니다 — 스펙을 더는 바꿀 수 없습니다"]
        next_allowed = self.ai_spec_next_allowed_ms(trader)
        if next_allowed:
            wait_min = max(1, (next_allowed - now_ms) // 60_000)
            return [f"스펙 수정은 6시간에 1회입니다 — 약 {wait_min}분 후에 다시 시도하세요"]
        errors = self.ai_traders[trader].set_spec(raw)
        if not errors and self.store is not None:
            self.store.set_setting(
                f"ai_spec:{trader}", json.dumps(self.ai_traders[trader].spec)
            )
            self.store.set_setting(f"ai_spec_at:{trader}", str(now_ms))
            # 첫 스펙 적용이 대회의 출발선이다.
            if not self.store.get_setting("ai_competition_start_ms"):
                self.store.set_setting("ai_competition_start_ms", str(now_ms))
                if self.arena is not None:
                    self.arena.ai_end_ms = now_ms + AI_COMPETITION_DAYS * 86_400_000
                log.warning(
                    "AI 대회 시작 — %d일 뒤 수익률로 최종 순위", AI_COMPETITION_DAYS
                )
        return errors

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
        # 긴급 청산 뒤에 감시견이 봇을 되살려 곧바로 재진입하면 청산의 의미가
        # 없다. 다시 돌리려면 사람이 직접 시작 버튼을 눌러야 한다.
        self.disable_autorestart()
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
