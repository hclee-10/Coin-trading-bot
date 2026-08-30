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


def context(candles, position=None, equity=10_000.0, mtf=None):
    return StrategyContext(
        symbol=SYMBOL, timeframe="5m", candles=candles,
        ticker=Ticker(symbol=SYMBOL, last=candles[-1].close, bid=None, ask=None, timestamp=0),
        position=position or Position.flat(SYMBOL), equity=equity,
        mtf_candles=mtf or {},
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


def test_stochastic_reversion_waits_for_the_turn_not_the_fall():
    """RSI 계열과 같은 원칙 — 바닥권에 있는 동안이 아니라 빠져나올 때 산다."""
    strategy = get_strategy("stochastic_reversion")
    falling = series([100 - i for i in range(60)])

    assert strategy.generate(context(falling)).action is SignalAction.HOLD


def test_range_fade_buys_the_bottom_and_sells_the_top_of_the_box():
    """돈치안을 터틀과 정반대로 쓴다 — 채널 끝에서 반대로 진입."""
    strategy = get_strategy("range_fade", {"period": 40})
    box = [100.0 + (3.0 if i % 20 < 10 else -3.0) for i in range(80)]

    near_bottom = series(box + [97.2, 97.2])
    near_top = series(box + [102.8, 102.8])

    assert strategy.generate(context(near_bottom)).action is SignalAction.ENTER_LONG
    assert strategy.generate(context(near_top)).action is SignalAction.ENTER_SHORT


def test_squeeze_breakout_ignores_a_breakout_from_wide_bands():
    """스퀴즈 없는 돌파는 추세의 끝물일 가능성이 높아 받지 않는다."""
    strategy = get_strategy("squeeze_breakout")
    # 변동성이 내내 큰 상태(폭 비율 ≈ 1)에서의 돌파
    wild = [100.0 + (5.0 if i % 2 else -5.0) for i in range(90)] + [120.0, 120.0]

    signal = strategy.generate(context(series(wild)))

    assert signal.action is SignalAction.HOLD
    assert "스퀴즈" in signal.reason


def test_triple_ma_needs_full_alignment():
    """두 선만 교차해서는 안 되고 세 선이 정배열되어야 한다."""
    strategy = get_strategy("triple_ma", {"fast": 5, "mid": 10, "slow": 20})
    closes = [100 - i * 0.3 for i in range(60)] + [82 + i * 1.5 for i in range(25)]

    actions = []
    candles = series(closes)
    for end in range(40, len(candles)):
        actions.append(strategy.generate(context(candles[:end])).action)

    assert SignalAction.ENTER_LONG in actions


def test_adx_trend_skips_crosses_in_chop():
    """횡보(ADX 낮음)에서의 DI 교차는 무시해야 톱니 손실을 피한다."""
    strategy = get_strategy("adx_trend", {"adx_threshold": 20})
    flat = series([100.0 + (0.2 if i % 2 else -0.2) for i in range(120)], wick=0.05)

    for end in range(80, 120):
        assert not strategy.generate(context(flat[:end])).is_entry


def test_obv_trend_requires_volume_confirmation():
    """가격이 돌파해도 거래량 흐름(OBV)이 받쳐주지 않으면 들어가지 않는다."""
    strategy = get_strategy("obv_trend")
    # 내내 하락(OBV 음수 누적)하다 마지막에 가격만 반짝 돌파
    closes = [130 - i * 0.5 for i in range(60)] + [101 + i * 1.2 for i in range(3)]

    signal = strategy.generate(context(series(closes)))

    assert signal.action is not SignalAction.ENTER_LONG


def test_macd_rsi_does_not_chase_an_overbought_turn():
    """MACD 가 돌아서도 RSI 가 과열이면 추격하지 않는다 — 필터의 존재 이유."""
    strategy = get_strategy("macd_rsi", {"rsi_ceiling": 65})
    # 급등 직후의 상향 전환 — RSI 가 과열 상태다
    closes = [100.0] * 40 + [100 + i * 2.5 for i in range(30)]

    candles = series(closes)
    for end in range(50, len(candles)):
        signal = strategy.generate(context(candles[:end]))
        if signal.is_entry:
            # 진입했다면 그 시점 RSI 는 과열이 아니었어야 한다
            assert "여유" in signal.reason


def test_ichimoku_cloud_needs_a_real_breakout():
    """구름 안에서의 등락은 신호가 아니다."""
    strategy = get_strategy("ichimoku_cloud")
    flat = series([100.0] * 130, wick=3.0)  # 두꺼운 범위 안에서 횡보

    assert not strategy.generate(context(flat)).is_entry


def test_ichimoku_cloud_enters_on_a_cloud_breakout():
    strategy = get_strategy("ichimoku_cloud")
    closes = [100.0] * 110 + [100 + i * 0.8 for i in range(14)]

    actions = []
    candles = series(closes)
    for end in range(105, len(candles)):
        actions.append(strategy.generate(context(candles[:end])).action)

    assert SignalAction.ENTER_LONG in actions


def test_ichimoku_mtf_respects_the_higher_timeframe_cloud():
    """일봉 구름이 반대편이면 기본 시간대 돌파가 나와도 들어가지 않는다.

    가중치가 기본 1 vs 일봉 4 라서, 일봉의 반대 신호가 합의를 음수로 끌어내린다.
    """
    solo = get_strategy("ichimoku_mtf")
    vetoed = get_strategy("ichimoku_mtf")
    base = series([100.0] * 115 + [100 + i * 0.8 for i in range(14)])
    daily_down = series([400 - i for i in range(140)])  # 구름 아래로 꾸준한 하락

    entered_alone = entered_vetoed = False
    for end in range(110, len(base)):
        if solo.generate(context(base[:end])).is_entry:
            entered_alone = True
        if vetoed.generate(context(base[:end], mtf={"1d": daily_down})).is_entry:
            entered_vetoed = True

    assert entered_alone, "기본 시간대 단독으로는 진입했어야 합니다"
    assert not entered_vetoed, "일봉 구름이 반대인데 진입했습니다"


def test_ichimoku_mtf_conviction_grows_with_agreement():
    """상위 시간대가 같은 방향으로 강하게 동의하면 확신이 올라간다."""
    alone = get_strategy("ichimoku_mtf")
    backed = get_strategy("ichimoku_mtf")
    base = series([100.0] * 115 + [100 + i * 0.8 for i in range(14)])
    daily_up = series([100 + i for i in range(140)])  # 구름 위로 꾸준한 상승

    best_alone = best_backed = 0.0
    for end in range(110, len(base)):
        s1 = alone.generate(context(base[:end]))
        s2 = backed.generate(context(base[:end], mtf={"1d": daily_up}))
        if s1.is_entry:
            best_alone = max(best_alone, s1.strength)
        if s2.is_entry:
            best_backed = max(best_backed, s2.strength)

    assert best_backed > best_alone


def test_ichimoku_rsi_never_buys_below_the_cloud():
    """구름이 방향 필터다 — 하락 추세에서는 RSI 반등이 나와도 매수하지 않는다."""
    strategy = get_strategy("ichimoku_rsi")
    falling = series([300 - i * 0.7 for i in range(220)])

    for end in range(105, 220):
        assert strategy.generate(context(falling[:end])).action is not SignalAction.ENTER_LONG


def test_ichimoku_macd_rejects_a_breakout_without_momentum():
    """긴 하락 끝의 반짝 돌파 — 위치는 맞지만 속도(MACD)가 아직 음수라 거른다."""
    strategy = get_strategy("ichimoku_macd")
    closes = [200 - i * 0.5 for i in range(150)] + [141.0, 141.0]

    signal = strategy.generate(context(series(closes)))

    assert not signal.is_entry


def test_ichimoku_sanyaku_enters_only_on_completion():
    """삼역호전이 완성되는 봉에서 한 번만 — 지속 상태에는 올라타지 않는다."""
    strategy = get_strategy("ichimoku_sanyaku")
    # 완성 시점이 워밍업(100봉) 이후에 오도록 횡보를 길게 둔다.
    closes = [100.0] * 130 + [100 + i * 0.5 for i in range(120)]

    entries = 0
    candles = series(closes)
    for end in range(105, len(candles)):
        if strategy.generate(context(candles[:end])).is_entry:
            entries += 1

    assert entries == 1


def test_exit_signals_never_carry_a_size():
    """청산은 방향만 있으면 된다 — 크기를 붙이면 실행 계층이 혼동한다."""
    strategy = get_strategy("supertrend")
    rising = series([100 + i for i in range(80)])
    position = Position(symbol=SYMBOL, side=PositionSide.SHORT, contracts=1.0, entry_price=150.0)

    signal = strategy.generate(context(rising, position))

    if signal.action is SignalAction.EXIT:
        assert signal.stop_loss is None
