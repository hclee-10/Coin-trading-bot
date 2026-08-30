"""지표 — 경계 조건에서 무엇을 돌려주는지가 핵심이다.

데이터가 모자랄 때 조용히 이상한 값을 내놓는 지표는 전략을 통째로 망친다.
"""

import pytest

from bot.indicators import (
    adx, atr, bollinger, cci, donchian, ema, heikin_ashi, highest, ichimoku,
    ichimoku_cloud, keltner, lowest, macd, mfi, obv, psar, roc, rolling_vwap, rsi,
    sma, stddev, stochastic, supertrend, williams_r,
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


# --- 오실레이터 -----------------------------------------------------------
def test_stochastic_reads_position_in_the_range():
    """범위의 천장 근처면 %K 가 높아야 한다."""
    rows = [(110, 90, 100)] * 20 + [(110, 90, 109)] * 3
    k, d = stochastic(candles(rows), 14, 3, 3)
    assert k[-1] > 80
    assert 0 <= d[-1] <= 100


def test_stochastic_is_neutral_when_the_range_is_zero():
    k, _ = stochastic(candles([(100, 100, 100)] * 30), 14, 3, 3)
    assert k[-1] == pytest.approx(50.0)


def test_williams_r_mirrors_stochastic():
    """%R 은 평활 없는 %K 를 -100 쪽으로 뒤집은 값이다."""
    rows = [(110, 90, 100)] * 20 + [(110, 90, 91)]
    values = williams_r(candles(rows), 14)
    assert values[-1] < -80  # 바닥권


def test_cci_is_zero_on_flat_prices():
    assert cci(candles([(101, 99, 100)] * 30), 20)[-1] == pytest.approx(0.0)


def test_cci_goes_positive_on_a_jump():
    rows = [(101, 99, 100)] * 25 + [(111, 109, 110)]
    assert cci(candles(rows), 20)[-1] > 100


def test_mfi_is_100_when_everything_rises():
    rows = [(100 + i + 1, 100 + i - 1, 100 + i) for i in range(30)]
    assert mfi(candles(rows), 14)[-1] == pytest.approx(100.0)


def test_roc_measures_percent_change():
    values = roc([100.0] * 10 + [110.0], 10)
    assert values[-1] == pytest.approx(10.0)


# --- 거래량 지표 ----------------------------------------------------------
def test_obv_accumulates_signed_volume():
    rows = [(101, 99, 100), (102, 100, 101), (103, 101, 102), (102, 100, 101)]
    values = obv(candles(rows))
    assert values == [0.0, 1.0, 2.0, 1.0]


def test_rolling_vwap_equals_typical_mean_with_constant_volume():
    rows = [(102, 98, 100)] * 30
    assert rolling_vwap(candles(rows), 20)[-1] == pytest.approx(100.0)


# --- 추세 지표 ------------------------------------------------------------
def test_adx_rises_in_a_trend():
    """추세장의 ADX 가 횡보장보다 높아야 필터로 쓸 수 있다."""
    trending = candles([(101 + i, 99 + i, 100 + i) for i in range(60)])
    choppy = candles([(101 + (i % 2), 99 - (i % 2), 100 + (i % 2)) for i in range(60)])
    _, _, strong = adx(trending, 14)
    _, _, weak = adx(choppy, 14)
    assert strong[-1] > weak[-1]
    assert strong[-1] > 25


def test_adx_plus_di_dominates_in_an_uptrend():
    plus, minus, _ = adx(candles([(101 + i, 99 + i, 100 + i) for i in range(60)]), 14)
    assert plus[-1] > minus[-1]


def test_keltner_straddles_the_middle():
    upper, middle, lower = keltner(candles([(102, 98, 100)] * 40), 20, 10, 2.0)
    assert lower[-1] < middle[-1] < upper[-1]


def test_psar_stays_below_price_in_an_uptrend_and_flips():
    rising = [(101 + i, 99 + i, 100 + i) for i in range(40)]
    falling = [(141 - i * 3, 139 - i * 3, 140 - i * 3) for i in range(15)]
    line, trend = psar(candles(rising + falling))
    assert trend[39] == 1
    assert line[39] < 99 + 39   # 상승 중엔 저가 아래
    assert trend[-1] == -1


def test_heikin_ashi_keeps_one_color_through_a_trend():
    """잔파동 평활이 목적이다 — 꾸준한 추세에서는 색이 유지되어야 한다."""
    rising = candles([(101 + i, 99 + i, 100 + i) for i in range(30)])
    ha = heikin_ashi(rising)
    assert all(c.close > c.open for c in ha[5:])


def test_ichimoku_lines_are_range_midpoints():
    tenkan, kijun = ichimoku(candles([(110, 90, 100)] * 40), 9, 26)
    assert tenkan[-1] == pytest.approx(100.0)
    assert kijun[-1] == pytest.approx(100.0)


def test_ichimoku_stalls_inside_the_range():
    """범위 안의 등락에는 움직이지 않는다 — 이동평균과의 결정적 차이."""
    rows = [(110, 90, 95 + (i % 10)) for i in range(40)]  # 종가만 오르내림
    tenkan, _ = ichimoku(candles(rows), 9, 26)
    assert tenkan[-1] == pytest.approx(tenkan[-5])


def test_ichimoku_cloud_spans_are_shifted_back_to_the_present():
    """스팬의 i 번째 값은 i-26 시점에 계산된 것이어야 '지금 자리의 구름'이 된다."""
    rows = [(101 + i, 99 + i, 100 + i) for i in range(120)]
    tenkan, kijun, span_a, span_b = ichimoku_cloud(candles(rows), 9, 26, 52, 26)

    assert span_a[-1] == pytest.approx((tenkan[-27] + kijun[-27]) / 2)
    # 상승장에서 구름은 가격 아래에 있다 (26봉 전의 낮은 값이므로)
    assert max(span_a[-1], span_b[-1]) < rows[-1][2]


def test_ichimoku_cloud_is_none_before_enough_data():
    rows = [(101, 99, 100)] * 30
    _, _, span_a, span_b = ichimoku_cloud(candles(rows), 9, 26, 52, 26)
    assert span_a[-1] is None and span_b[-1] is None
