"""지표 — 경계 조건에서 무엇을 돌려주는지가 핵심이다.

데이터가 모자랄 때 조용히 이상한 값을 내놓는 지표는 전략을 통째로 망친다.
"""

import pytest

from bot.indicators import (
    atr, bollinger, donchian, ema, highest, lowest, macd, rsi, sma, stddev, supertrend,
)
from bot.models import Candle


def candles(rows):
    """(고가, 저가, 종가) 목록으로 캔들을 만든다."""
    return [
        Candle(timestamp=1_700_000_000_000 + i * 60_000,
               open=c, high=h, low=l, close=c, volume=1.0)
        for i, (h, l, c) in enumerate(rows)
    ]


# --- 길이와 정렬 ---------------------------------------------------------
@pytest.mark.parametrize("fn", [lambda v: sma(v, 5), lambda v: ema(v, 5),
                                lambda v: rsi(v, 5), lambda v: stddev(v, 5)])
def test_output_length_matches_input(fn):
    """길이가 다르면 인덱스가 어긋나 전략이 엉뚱한 봉을 본다."""
    values = [float(i) for i in range(30)]
    assert len(fn(values)) == len(values)


@pytest.mark.parametrize("fn", [lambda v: sma(v, 10), lambda v: ema(v, 10),
                                lambda v: rsi(v, 10)])
def test_not_enough_data_returns_all_none(fn):
    assert all(v is None for v in fn([1.0, 2.0, 3.0]))


# --- 값 검증 -------------------------------------------------------------
def test_sma_is_the_plain_average():
    assert sma([1.0, 2.0, 3.0, 4.0, 5.0], 5)[-1] == pytest.approx(3.0)


def test_sma_slides_correctly():
    values = sma([1.0, 2.0, 3.0, 4.0, 5.0, 6.0], 3)
    assert values[2] == pytest.approx(2.0)
    assert values[-1] == pytest.approx(5.0)


def test_ema_weights_recent_values_more():
    """같은 데이터에서 EMA 가 SMA 보다 최근 급등을 빨리 반영해야 한다."""
    values = [10.0] * 20 + [20.0]
    assert ema(values, 10)[-1] > sma(values, 10)[-1]


def test_ema_starts_from_the_simple_average():
    values = [float(i) for i in range(1, 21)]
    assert ema(values, 5)[4] == pytest.approx(3.0)


def test_rsi_is_100_when_everything_rises():
    assert rsi([float(i) for i in range(1, 30)], 14)[-1] == pytest.approx(100.0)


def test_rsi_is_0_when_everything_falls():
    assert rsi([float(i) for i in range(30, 1, -1)], 14)[-1] == pytest.approx(0.0)


def test_rsi_stays_within_bounds():
    import random

    random.seed(3)
    values = [100 + random.gauss(0, 5) for _ in range(200)]
    assert all(0 <= v <= 100 for v in rsi(values, 14) if v is not None)


def test_flat_prices_give_a_neutral_rsi():
    """변화가 없으면 0으로 나누게 된다 — 예외 없이 중립값이어야 한다."""
    assert rsi([100.0] * 30, 14)[-1] == pytest.approx(50.0)


def test_bollinger_bands_straddle_the_middle():
    values = [float(i) for i in range(1, 41)]
    upper, middle, lower = bollinger(values, 20, 2.0)
    assert lower[-1] < middle[-1] < upper[-1]


def test_bollinger_collapses_when_price_is_flat():
    upper, middle, lower = bollinger([100.0] * 30, 20)
    assert upper[-1] == pytest.approx(lower[-1]) == pytest.approx(100.0)


def test_macd_histogram_is_the_gap_between_line_and_signal():
    values = [float(i) for i in range(1, 100)]
    line, signal, histogram = macd(values)
    assert histogram[-1] == pytest.approx(line[-1] - signal[-1])


def test_atr_grows_with_volatility():
    calm = candles([(101, 99, 100)] * 30)
    wild = candles([(110, 90, 100)] * 30)
    assert atr(wild, 14)[-1] > atr(calm, 14)[-1]


def test_donchian_excludes_the_current_bar():
    """현재 봉을 포함하면 '최고가 돌파'가 항상 참이 되어 신호가 의미를 잃는다."""
    rows = [(100, 90, 95)] * 25 + [(200, 190, 195)]
    highs, lows = donchian(candles(rows), 20)
    assert highs[-1] == pytest.approx(100.0)   # 200 이 아니다


def test_supertrend_flips_direction_on_a_reversal():
    rising = [(100 + i, 98 + i, 99 + i) for i in range(40)]
    falling = [(140 - i * 3, 138 - i * 3, 139 - i * 3) for i in range(20)]
    _, trend = supertrend(candles(rising + falling), 10, 2.0)
    assert trend[39] == 1
    assert trend[-1] == -1


def test_highest_and_lowest():
    values = [3.0, 1.0, 4.0, 1.0, 5.0]
    assert highest(values, 3)[-1] == 5.0
    assert lowest(values, 3)[-1] == 1.0


def test_zero_period_is_rejected():
    with pytest.raises(ValueError):
        sma([1.0, 2.0], 0)
