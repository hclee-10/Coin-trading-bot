"""전략 — 모든 전략이 지켜야 할 계약과, 각 전략의 핵심 동작."""

import pytest

from bot.models import Candle, Conviction, Position, PositionSide, Signal, SignalAction, Ticker
from bot.strategies import get_strategy, strategy_catalog
from bot.strategies.base import StrategyContext

SYMBOL = "BTC/USDT:USDT"
TRADING_STRATEGIES = [e["name"] for e in strategy_catalog() if e["summary"]]


def series(closes, *, wick=0.15):
    candles, previous = [], closes[0]
    for i, close in enumerate(closes):
        candles.append(Candle(
            timestamp=1_700_000_000_000 + i * 300_000,
            open=previous, high=max(previous, close) + wick,
            low=min(previous, close) - wick, close=close, volume=1.0,
        ))
        previous = close
    return candles


def context(candles, position=None, equity=10_000.0):
    return StrategyContext(
        symbol=SYMBOL, timeframe="5m", candles=candles,
        ticker=Ticker(symbol=SYMBOL, last=candles[-1].close, bid=None, ask=None, timestamp=0),
        position=position or Position.flat(SYMBOL), equity=equity,
    )


# --- 모든 전략이 지켜야 할 계약 -------------------------------------------
@pytest.mark.parametrize("name", TRADING_STRATEGIES)
def test_every_strategy_has_a_description(name):
    """설명 없는 전략은 왜 진입했는지 나중에 알 수 없다."""
    entry = next(e for e in strategy_catalog() if e["name"] == name)
    assert entry["summary"] and len(entry["description"]) > 200
    assert entry["category"] in ("trend", "reversion", "breakout", "combo", "range")


@pytest.mark.parametrize("name", TRADING_STRATEGIES)
def test_strategy_holds_during_warmup(name):
    """데이터가 모자랄 때 진입하면 지표가 계산되지도 않은 채 돈이 나간다."""
    strategy = get_strategy(name)
    signal = strategy.generate(context(series([100.0] * 5)))

    assert signal.action is SignalAction.HOLD


@pytest.mark.parametrize("name", TRADING_STRATEGIES)
def test_strategy_returns_a_signal_on_random_data(name):
    import random

    random.seed(11)
    closes = [100.0]
    for _ in range(400):
        closes.append(max(1.0, closes[-1] * (1 + random.gauss(0, 0.004))))
    strategy = get_strategy(name)

    signal = strategy.generate(context(series(closes)))

    assert isinstance(signal, Signal)
    assert 0.0 <= signal.strength <= 1.0


@pytest.mark.parametrize("name", TRADING_STRATEGIES)
def test_entry_signals_always_carry_a_stop_loss(name):
    """손절 없는 진입은 허용하지 않는다 — 전략이 직접 손절가를 줘야 한다."""
    import random

    random.seed(5)
    strategy = get_strategy(name)
    closes = [100.0]
    for i in range(900):
        # 뚜렷한 추세와 되돌림이 반복되어야 모든 계열이 한 번은 진입한다
        drift = 0.006 if (i // 150) % 2 == 0 else -0.006
        closes.append(max(1.0, closes[-1] * (1 + drift + random.gauss(0, 0.006))))
    candles = series(closes)

    entries = []
    for end in range(250, len(candles)):
        signal = strategy.generate(context(candles[:end]))
        if signal.is_entry:
            entries.append(signal)

    assert entries, f"{name} 이 한 번도 진입하지 않았습니다"
    for signal in entries:
        assert signal.stop_loss is not None and signal.stop_loss > 0
        assert signal.reason


@pytest.mark.parametrize("name", TRADING_STRATEGIES)
def test_conviction_falls_on_one_of_the_four_levels(name):
    """확신도가 네 등급 중 하나여야 주문 금액이 의도대로 결정된다."""
    import random

    random.seed(9)
    strategy = get_strategy(name)
    closes = [100.0]
    for i in range(600):
        drift = 0.004 if (i // 120) % 2 == 0 else -0.004
        closes.append(max(1.0, closes[-1] * (1 + drift + random.gauss(0, 0.005))))
    candles = series(closes)
    levels = {c.value for c in Conviction}

    for end in range(200, len(candles)):
        signal = strategy.generate(context(candles[:end]))
        if signal.is_entry:
            assert signal.strength in levels, f"{name}: {signal.strength}"


# --- 개별 전략의 핵심 동작 -------------------------------------------------
def test_ema_cross_enters_long_on_an_upward_cross():
    strategy = get_strategy("ema_cross", {"fast": 5, "slow": 20})
    # 길게 내려가다 급반등 → 빠른 선이 느린 선을 위로 뚫는다
    closes = [100 - i * 0.3 for i in range(80)] + [76 + i * 2.0 for i in range(20)]

    actions = []
    candles = series(closes)
    for end in range(60, len(candles)):
        actions.append(strategy.generate(context(candles[:end])).action)

    assert SignalAction.ENTER_LONG in actions


def test_rsi_reversion_waits_for_the_turn_not_the_fall():
    """떨어지는 도중에 사면 계속 물린다 — 돌아서는 것을 확인해야 한다."""
    strategy = get_strategy("rsi_reversion", {"period": 14})
    falling = series([100 - i for i in range(60)])

    signal = strategy.generate(context(falling))

    assert signal.action is SignalAction.HOLD


def test_donchian_needs_a_real_breakout():
    strategy = get_strategy("donchian_breakout", {"entry_period": 20})
    flat = series([100.0] * 60, wick=0.5)

    assert strategy.generate(context(flat)).action is SignalAction.HOLD


def test_grid_enters_against_the_deviation():
    """그리드는 기준선에서 벌어진 반대 방향으로 들어간다."""
    strategy = get_strategy("grid", {"period": 20, "step_pct": 1.0})
    # 평평하다가 아래로 크게 이탈. 마지막 봉은 closed_candles 에서 빠지므로
    # 이탈한 봉이 확정되도록 뒤에 한 봉을 더 붙인다.
    closes = [100.0] * 60 + [95.0, 95.0]

    signal = strategy.generate(context(series(closes)))

    assert signal.action is SignalAction.ENTER_LONG
    assert signal.stop_loss < 95.0


def test_trend_pullback_does_not_fight_the_trend():
    """하락 추세에서는 매수하지 않는다."""
    strategy = get_strategy("trend_pullback", {"trend_period": 50})
    falling = series([200 - i * 0.5 for i in range(200)])

    for end in range(100, 200):
        signal = strategy.generate(context(falling[:end]))
        assert signal.action is not SignalAction.ENTER_LONG


def test_bollinger_pair_reads_the_same_move_oppositely():
    """같은 지표를 쓰지만 정반대로 해석한다 — 시장 성격을 판별하는 데 쓴다."""
    breakout = get_strategy("bollinger_breakout", {"period": 20})
    reversion = get_strategy("bollinger_reversion", {"period": 20})
    closes = [100.0] * 60 + [112.0, 112.0]
    ctx = context(series(closes))

    assert breakout.generate(ctx).action is SignalAction.ENTER_LONG
    assert reversion.generate(ctx).action is not SignalAction.ENTER_LONG


def test_exit_signals_never_carry_a_size():
    """청산은 방향만 있으면 된다 — 크기를 붙이면 실행 계층이 혼동한다."""
    strategy = get_strategy("supertrend")
    rising = series([100 + i for i in range(80)])
    position = Position(symbol=SYMBOL, side=PositionSide.SHORT, contracts=1.0, entry_price=150.0)

    signal = strategy.generate(context(rising, position))

    if signal.action is SignalAction.EXIT:
        assert signal.stop_loss is None
