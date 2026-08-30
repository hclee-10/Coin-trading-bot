"""시간대 변환 — 다중 시간대 전략의 토대라 경계가 정확해야 한다."""

import pytest

from bot.models import Candle, Position, Ticker
from bot.strategies.base import StrategyContext
from bot.timeframes import resample, timeframe_to_ms

SYMBOL = "BTC/USDT:USDT"


def candle(ts, o, h, l, c, v=1.0):
    return Candle(timestamp=ts, open=o, high=h, low=l, close=c, volume=v)


def five_minute_series(count, start=0):
    """정시(0ms) 기준으로 정렬된 5분봉."""
    return [
        candle(start + i * 300_000, 100.0 + i, 101.0 + i, 99.0 + i, 100.5 + i)
        for i in range(count)
    ]


# --- 파싱 -----------------------------------------------------------------
def test_timeframe_to_ms_parses_common_frames():
    assert timeframe_to_ms("5m") == 300_000
    assert timeframe_to_ms("1h") == 3_600_000
    assert timeframe_to_ms("4h") == 14_400_000
    assert timeframe_to_ms("1d") == 86_400_000


@pytest.mark.parametrize("bad", ["", "h", "5x", "0m", "abc"])
def test_timeframe_to_ms_rejects_garbage(bad):
    with pytest.raises(ValueError):
        timeframe_to_ms(bad)


# --- 리샘플 ---------------------------------------------------------------
def test_resample_aggregates_ohlcv():
    """1시간봉 하나 = 5분봉 12개의 시가·최고·최저·종가·거래량 합."""
    source = five_minute_series(24)  # 정확히 2시간
    out = resample(source, "1h")

    assert len(out) == 2
    first = out[0]
    assert first.open == source[0].open
    assert first.close == source[11].close
    assert first.high == max(c.high for c in source[:12])
    assert first.low == min(c.low for c in source[:12])
    assert first.volume == pytest.approx(12.0)


def test_resample_drops_the_incomplete_last_bucket():
    """진행 중인 상위 봉은 값이 계속 바뀐다 — 지표 계산에 넣으면 신호가 흔들린다."""
    source = five_minute_series(18)  # 1시간 + 30분
    assert len(resample(source, "1h")) == 1
    assert len(resample(source, "1h", complete_only=False)) == 2


def test_resample_empty_input():
    assert resample([], "1h") == []


# --- StrategyContext 연동 --------------------------------------------------
def make_context(candles, mtf=None):
    return StrategyContext(
        symbol=SYMBOL, timeframe="5m", candles=candles,
        ticker=Ticker(symbol=SYMBOL, last=candles[-1].close, bid=None, ask=None, timestamp=0),
        position=Position.flat(SYMBOL), equity=10_000.0,
        mtf_candles=mtf or {},
    )


def test_context_prefers_supplied_mtf_candles():
    """엔진이 준 거래소 캔들이 리샘플 근사보다 정확하다 — 있으면 그걸 쓴다."""
    base = five_minute_series(24)
    supplied = five_minute_series(50, start=999_000_000)
    ctx = make_context(base, mtf={"1h": supplied})

    got = ctx.closed_candles_for("1h")

    assert got == supplied[:-1]  # 마지막 미완성 봉은 뺀다


def test_context_falls_back_to_resampling():
    """공급이 없으면(백테스트 등) 기본 시간대에서 합성한다."""
    base = five_minute_series(25)  # 확정 24개 → 1h 2개
    ctx = make_context(base)

    got = ctx.closed_candles_for("1h")

    assert len(got) == 2
    assert got[0].open == base[0].open


def test_context_same_timeframe_is_passthrough():
    base = five_minute_series(10)
    ctx = make_context(base)
    assert ctx.closed_candles_for("5m") == base[:-1]
