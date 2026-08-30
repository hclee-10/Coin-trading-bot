import time

import pytest

from bot.config import Config, ExchangeConfig, RiskConfig, StrategyConfig, TradingConfig
from bot.models import Position, PositionSide
from bot.web.supervisor import BotSupervisor, SupervisorError
from tests.fakes import FakeExchange

SYMBOL = "BTC/USDT:USDT"


def make_config() -> Config:
    # validate() 를 거치지 않으므로 테스트에서는 폴링을 아주 짧게 둘 수 있다
    trading = TradingConfig(symbols=[SYMBOL])
    trading.poll_interval_sec = 0.02
    return Config(
        exchange=ExchangeConfig(id="okx", leverage=3.0),
        trading=trading,
        strategy=StrategyConfig(name="hold"),
        risk=RiskConfig(),
    )


def make_supervisor(exchange: FakeExchange | None = None):
    ex = exchange or FakeExchange(price=100.0, equity=1_000.0)
    return BotSupervisor(make_config(), exchange_factory=lambda: ex, join_timeout=5.0), ex


def wait_for(predicate, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


def test_start_runs_cycles_and_stop_joins_the_thread():
    sup, ex = make_supervisor()
    sup.start(live=False)
    try:
        assert wait_for(lambda: sup.snapshot().last_cycle_at is not None), "주기가 돌지 않았습니다"
        snap = sup.snapshot()
        assert snap.running
        assert snap.equity == 1_000.0
    finally:
        assert sup.stop() is True
    assert not sup.running


def test_starting_twice_is_rejected():
    sup, _ = make_supervisor()
    sup.start(live=False)
    try:
        with pytest.raises(SupervisorError, match="이미 실행 중"):
            sup.start(live=False)
    finally:
        sup.stop()


def test_snapshot_before_start_has_config_but_no_runtime_data():
    sup, _ = make_supervisor()
    snap = sup.snapshot()
    assert snap.running is False
    assert snap.live is False
    assert snap.exchange == "okx"
    assert snap.symbols == [SYMBOL]
    assert snap.equity is None
    assert snap.positions == []


def test_snapshot_reports_open_positions_from_the_last_cycle():
    ex = FakeExchange(price=100.0, equity=1_000.0)
    ex.positions[SYMBOL] = Position(
        symbol=SYMBOL, side=PositionSide.LONG, contracts=2.0,
        entry_price=95.0, notional=200.0, unrealized_pnl=10.0,
    )
    sup, _ = make_supervisor(ex)
    sup.start(live=False)
    try:
        assert wait_for(lambda: sup.snapshot().positions)
        (view,) = sup.snapshot().positions
        assert view.side == "long"
        assert view.contracts == 2.0
        assert view.unrealized_pnl == 10.0
    finally:
        sup.stop()


def test_dry_run_start_is_not_reported_as_live():
    sup, _ = make_supervisor()
    sup.start(live=False)
    try:
        assert sup.snapshot().live is False
    finally:
        sup.stop()


def test_live_start_is_reported_as_live():
    sup, _ = make_supervisor()
    sup.start(live=True)
    try:
        assert wait_for(lambda: sup.snapshot().running)
        assert sup.snapshot().live is True
    finally:
        sup.stop()


def test_live_positions_are_refused_while_the_bot_runs():
    """실행 중에 요청 스레드가 거래소를 만지면 ccxt 세션이 경쟁한다."""
    sup, _ = make_supervisor()
    sup.start(live=False)
    try:
        with pytest.raises(SupervisorError, match="실행 중"):
            sup.fetch_positions_live()
    finally:
        sup.stop()


def test_live_positions_query_works_when_stopped():
    ex = FakeExchange()
    ex.positions[SYMBOL] = Position(
        symbol=SYMBOL, side=PositionSide.SHORT, contracts=1.0, entry_price=100.0
    )
    sup, _ = make_supervisor(ex)
    (view,) = sup.fetch_positions_live()
    assert view.side == "short"


def test_close_all_stops_the_bot_before_closing():
    ex = FakeExchange(price=100.0, equity=1_000.0)
    ex.positions[SYMBOL] = Position(
        symbol=SYMBOL, side=PositionSide.LONG, contracts=3.0, entry_price=100.0
    )
    sup, _ = make_supervisor(ex)
    sup.start(live=False)
    assert wait_for(lambda: sup.snapshot().last_cycle_at is not None)

    messages = sup.close_all_positions()

    assert not sup.running, "청산 전에 봇이 멈춰 있어야 재진입 사고를 막는다"
    assert any(SYMBOL in m for m in messages)
    (order,) = ex.sent_orders
    assert order.reduce_only and order.amount == 3.0


def test_close_all_with_no_positions_reports_so():
    sup, ex = make_supervisor()
    messages = sup.close_all_positions()
    assert messages == ["청산할 포지션이 없습니다"]
    assert ex.sent_orders == []


def test_stop_is_idempotent():
    sup, _ = make_supervisor()
    assert sup.stop() is True
    sup.start(live=False)
    assert sup.stop() is True
    assert sup.stop() is True


# --- 포지션 조회 캐시 -----------------------------------------------------
class CountingExchange(FakeExchange):
    """fetch_position 호출 횟수를 세는 거래소."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.fetch_calls = 0
        self.fail_with: Exception | None = None

    def fetch_position(self, symbol):
        self.fetch_calls += 1
        if self.fail_with is not None:
            raise self.fail_with
        return super().fetch_position(symbol)


def test_positions_are_cached_so_polling_does_not_burn_rate_limits():
    ex = CountingExchange()
    sup = BotSupervisor(
        make_config(), exchange_factory=lambda: ex, positions_cache_ttl=5.0
    )

    # 대시보드가 2초마다 폴링하는 상황을 흉내 낸다
    for offset in (0.0, 2.0, 4.0):
        sup.fetch_positions_live(now=offset)

    assert ex.fetch_calls == 1, "TTL 안에서는 거래소를 한 번만 불러야 한다"


def test_positions_cache_expires_after_ttl():
    ex = CountingExchange()
    sup = BotSupervisor(
        make_config(), exchange_factory=lambda: ex, positions_cache_ttl=5.0
    )

    sup.fetch_positions_live(now=0.0)
    sup.fetch_positions_live(now=6.0)

    assert ex.fetch_calls == 2


def test_failures_are_cached_too_so_a_broken_exchange_is_not_hammered():
    ex = CountingExchange()
    ex.fail_with = RuntimeError("거래소 연결 불가")
    sup = BotSupervisor(
        make_config(), exchange_factory=lambda: ex, positions_cache_ttl=5.0
    )

    for offset in (0.0, 1.0, 2.0):
        with pytest.raises(RuntimeError, match="거래소 연결 불가"):
            sup.fetch_positions_live(now=offset)

    assert ex.fetch_calls == 1, "장애 중에도 재시도가 폭주하면 안 된다"


def test_cache_is_dropped_after_closing_positions():
    ex = CountingExchange()
    ex.positions[SYMBOL] = Position(
        symbol=SYMBOL, side=PositionSide.LONG, contracts=1.0, entry_price=100.0
    )
    sup = BotSupervisor(
        make_config(), exchange_factory=lambda: ex, positions_cache_ttl=60.0
    )

    assert len(sup.fetch_positions_live()) == 1
    ex.positions.clear()
    sup.close_all_positions()

    # 캐시가 남아 있으면 청산된 포지션이 60초간 계속 보인다
    assert sup.fetch_positions_live() == []


def test_slow_failures_are_still_cached(monkeypatch):
    """거래소가 느리게 실패해도 재시도 폭주를 막아야 한다.

    캐시 시각을 조회 *시작* 시점으로 잡으면, 재시도 백오프로 조회가 몇 초씩
    걸릴 때 결과를 받자마자 캐시가 만료돼 폴링마다 거래소를 다시 때린다.
    """
    import time as time_module

    clock = {"t": 0.0}
    monkeypatch.setattr(time_module, "monotonic", lambda: clock["t"])

    class SlowFailingExchange(CountingExchange):
        def fetch_position(self, symbol):
            self.fetch_calls += 1
            clock["t"] += 3.5  # 재시도 백오프만큼 시간이 흐른다
            raise RuntimeError("거래소 연결 불가")

    ex = SlowFailingExchange()
    sup = BotSupervisor(
        make_config(),
        exchange_factory=lambda: ex,
        positions_cache_ttl=5.0,
        positions_error_cache_ttl=15.0,
    )

    # 대시보드가 2초 간격으로 5번 폴링하는 상황
    for _ in range(5):
        with pytest.raises(RuntimeError):
            sup.fetch_positions_live()
        clock["t"] += 2.0

    assert ex.fetch_calls == 1, (
        f"거래소를 {ex.fetch_calls}번 불렀습니다 — 장애 중에는 한 번이어야 합니다"
    )


def test_successful_results_use_the_shorter_ttl():
    """성공 결과는 오래 붙들면 화면이 낡는다 — 짧은 TTL 을 쓴다."""
    ex = CountingExchange()
    sup = BotSupervisor(
        make_config(),
        exchange_factory=lambda: ex,
        positions_cache_ttl=5.0,
        positions_error_cache_ttl=15.0,
    )

    sup.fetch_positions_live(now=0.0)
    sup.fetch_positions_live(now=4.0)   # TTL 안 — 캐시
    sup.fetch_positions_live(now=6.0)   # TTL 밖 — 재조회

    assert ex.fetch_calls == 2
