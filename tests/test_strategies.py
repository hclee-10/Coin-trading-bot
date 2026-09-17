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
def test_dca_fades_the_spike_and_pyramids_without_selling():
    """급락엔 롱 적립 시작, 보유 중 또 급락이면 pyramid 추가, 평단이 회복돼도 팔지 않는다."""
    strategy = get_strategy("dca_atr", {"spike_atr": 2.0})
    calm = [100.0 + (0.1 if i % 2 else -0.1) for i in range(60)]

    crash = series(calm + [94.0, 94.0])
    first = strategy.generate(context(crash))
    assert first.action is SignalAction.ENTER_LONG
    assert first.strength == Conviction.LOW.value      # 항상 회당 고정 금액

    # 보유 중, 가라앉은 뒤의 새 급락 → 같은 방향 적립 + pyramid 메타데이터
    crash2 = series(calm + [94.0] * 7 + [88.0, 88.0])
    holding = Position(symbol=SYMBOL, side=PositionSide.LONG, contracts=1.0,
                       entry_price=94.0, notional=100.0)
    add = strategy.generate(context(crash2, holding))
    assert add.action is SignalAction.ENTER_LONG
    assert add.metadata.get("accumulate") is True

    # 평단 위로 회복해도(급변이 없으면) 매도하지 않는다 — 무매도 적립식이다.
    recovered = series(calm + [94.0, 88.0, 96.0, 96.0])
    averaged = Position(symbol=SYMBOL, side=PositionSide.LONG, contracts=2.0,
                        entry_price=91.0, notional=200.0)
    held = strategy.generate(context(recovered, averaged))
    assert held.action is not SignalAction.EXIT


def test_dca_nets_against_the_position_on_an_opposite_spike():
    """롱 보유 중 급등이 오면 뒤집지도 참지도 않고 — 반대 방향 10달러(상계)."""
    strategy = get_strategy("dca_atr", {"spike_atr": 2.0})
    calm = [100.0 + (0.1 if i % 2 else -0.1) for i in range(60)]
    pump = series(calm + [106.0, 106.0])
    holding = Position(symbol=SYMBOL, side=PositionSide.LONG, contracts=2.0,
                       entry_price=94.0, notional=200.0)

    signal = strategy.generate(context(pump, holding))

    assert signal.action is SignalAction.ENTER_SHORT
    assert signal.metadata.get("accumulate") is True
    assert "상계" in signal.reason


def test_dca_variants_disagree_on_what_a_spike_is():
    """세 변형은 같은 시세에서 다른 급변을 본다 — 그래서 분리했다.

    아주 조용한 장의 +0.9% 점프: 평소 변동폭 대비로는 큰 급변(ATR 기준 잡음)
    이지만 고정 1.5% 자에는 못 미친다(퍼센트 기준 무시).
    """
    calm = [100.0 + (0.05 if i % 2 else -0.05) for i in range(60)]
    jump = series(calm + [100.9, 100.9], wick=0.02)

    atr_based = get_strategy("dca_atr")
    pct_based = get_strategy("dca_pct")

    assert atr_based.generate(context(jump)).action is SignalAction.ENTER_SHORT
    assert not pct_based.generate(context(jump)).is_entry


def test_dca_stops_adding_at_the_exposure_cap():
    """적립 한도(자기자본 대비 노출)에 닿으면 더 사 모으지 않는다."""
    strategy = get_strategy("dca_atr", {"max_exposure_pct": 30.0})
    calm = [100.0 + (0.1 if i % 2 else -0.1) for i in range(60)]
    crash2 = series(calm + [94.0] * 7 + [88.0, 88.0])
    huge = Position(symbol=SYMBOL, side=PositionSide.LONG, contracts=40.0,
                    entry_price=94.0, notional=3_800.0)   # 10,000 의 38%

    signal = strategy.generate(context(crash2, huge, equity=10_000.0))

    assert not signal.is_entry
    assert "한도" in signal.reason


def test_dca_horizon_reacts_once_per_bar():
    """1시간봉 하나의 급락에 한 번만 반응한다 — 같은 봉이 240번 보여도."""
    strategy = get_strategy("dca_1h")
    # 4시간 횡보 후 한 시간 동안 -3% (1시간봉 하나) + 다음 버킷 시작
    closes = [100.0] * 48 + [100 - (i + 1) * 0.25 for i in range(12)] + [97.0] * 3
    ctx = context(series(closes))

    first = strategy.generate(ctx)
    assert first.action is SignalAction.ENTER_LONG
    assert first.metadata.get("accumulate") is True

    # 같은 창(같은 1시간봉)으로 다시 판단 — 이미 반응한 봉이라 손대지 않는다
    assert not strategy.generate(ctx).is_entry


def test_dca_horizons_disagree_by_design():
    """같은 움직임도 시간 단위에 따라 급변이기도, 아니기도 하다."""
    # 5분봉 하나 -1.2%: 5분 자(±1.0%)에는 급변, 1시간 자(±2.5%)에는 아님
    closes = [100.0] * 59 + [98.8, 98.8]
    ctx = context(series(closes))

    assert get_strategy("dca_5m").generate(ctx).action is SignalAction.ENTER_LONG
    assert not get_strategy("dca_1h").generate(ctx).is_entry
