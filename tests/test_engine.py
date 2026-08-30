from bot.config import Config, ExchangeConfig, RiskConfig, StrategyConfig, TradingConfig
from bot.engine import TradingEngine
from bot.exchanges.base import ExchangeError
from bot.models import Position, PositionSide, Signal, SignalAction
from bot.strategies.base import Strategy, StrategyContext
from tests.fakes import FakeExchange

SYMBOL = "BTC/USDT:USDT"


class ScriptedStrategy(Strategy):
    """지정한 신호를 순서대로 내뱉는 테스트용 전략."""

    name = "scripted"

    def __init__(self, signals, warmup=0):
        self._signals = list(signals)
        self._warmup = warmup
        self.seen: list[StrategyContext] = []
        super().__init__({})

    @property
    def warmup_candles(self) -> int:
        return self._warmup

    def generate(self, ctx: StrategyContext) -> Signal:
        self.seen.append(ctx)
        return self._signals.pop(0) if self._signals else Signal()


class BrokenStrategy(Strategy):
    name = "broken"

    def generate(self, ctx):
        raise RuntimeError("전략 내부 버그")


def make_config(**trading_overrides) -> Config:
    trading = TradingConfig(symbols=[SYMBOL], poll_interval_sec=1, **trading_overrides)
    return Config(
        exchange=ExchangeConfig(id="okx", leverage=3.0),
        trading=trading,
        strategy=StrategyConfig(name="hold"),
        risk=RiskConfig(max_position_notional_pct=100.0),
    )


def test_prepare_sets_leverage_for_each_symbol():
    ex = FakeExchange()
    engine = TradingEngine(make_config(), ex, dry_run=True)

    engine.prepare()

    assert ex.leverage_calls == [(SYMBOL, 3.0, "isolated")]


def test_cycle_routes_entry_signal_to_orders():
    ex = FakeExchange(price=100.0, equity=10_000.0)
    strategy = ScriptedStrategy([Signal(action=SignalAction.ENTER_LONG, reason="테스트")])
    engine = TradingEngine(make_config(), ex, dry_run=False, strategy=strategy)

    report = engine.run_cycle()

    assert report.equity == 10_000.0
    assert [r.action for r in report.results] == ["entered"]
    assert report.open_positions == 1
    assert ex.sent_orders  # 진입 + 보호주문


def test_strategy_sees_position_and_equity():
    ex = FakeExchange(price=100.0, equity=5_000.0)
    ex.positions[SYMBOL] = Position(
        symbol=SYMBOL, side=PositionSide.LONG, contracts=1.0, entry_price=90.0
    )
    strategy = ScriptedStrategy([Signal()])
    engine = TradingEngine(make_config(), ex, dry_run=True, strategy=strategy)

    engine.run_cycle()

    ctx = strategy.seen[0]
    assert ctx.equity == 5_000.0
    assert ctx.position.side is PositionSide.LONG
    assert ctx.symbol == SYMBOL


def test_warmup_defers_strategy_call():
    ex = FakeExchange(candles=10)
    strategy = ScriptedStrategy([Signal(action=SignalAction.ENTER_LONG)], warmup=50)
    engine = TradingEngine(make_config(), ex, dry_run=True, strategy=strategy)

    report = engine.run_cycle()

    assert strategy.seen == []
    assert "워밍업" in report.results[0].detail
    assert ex.sent_orders == []


def test_strategy_exception_does_not_kill_the_loop():
    ex = FakeExchange()
    engine = TradingEngine(make_config(), ex, dry_run=True, strategy=BrokenStrategy({}))

    report = engine.run_cycle()

    assert report.results[0].action == "rejected"
    assert "내부 오류" in report.results[0].detail


def test_kill_switch_blocks_entries_but_allows_exits():
    ex = FakeExchange(price=100.0, equity=1_000.0)
    ex.positions[SYMBOL] = Position(
        symbol=SYMBOL, side=PositionSide.LONG, contracts=1.0, entry_price=100.0
    )
    strategy = ScriptedStrategy(
        [
            Signal(),  # 1주기: 기준 자기자본만 기록
            Signal(action=SignalAction.ENTER_LONG),
            Signal(action=SignalAction.EXIT),
        ]
    )
    engine = TradingEngine(make_config(), ex, dry_run=False, strategy=strategy)

    engine.run_cycle()          # 기준 자기자본 1000 설정
    ex.equity = 900.0           # -10%, 한도 3% 초과
    entry_blocked = engine.run_cycle()
    assert entry_blocked.halted
    assert entry_blocked.results[0].action == "rejected"
    assert "킬스위치" in entry_blocked.results[0].detail

    exit_allowed = engine.run_cycle()
    assert exit_allowed.results[0].action == "exited"


def test_position_fetch_failure_is_isolated_per_symbol():
    class FlakyExchange(FakeExchange):
        def fetch_position(self, symbol):
            raise ExchangeError("일시적 오류")

    ex = FlakyExchange()
    strategy = ScriptedStrategy([Signal()])
    engine = TradingEngine(make_config(), ex, dry_run=True, strategy=strategy)

    report = engine.run_cycle()  # 예외가 밖으로 새면 안 된다

    assert strategy.seen[0].position.side is PositionSide.FLAT
    assert report.open_positions == 0
