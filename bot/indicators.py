"""기술적 지표.

외부 라이브러리를 쓰지 않는다. 계산이 짧고, 무엇보다 **경계 조건에서 무엇을
돌려주는지 명확히 하고 싶어서**다 — 데이터가 모자랄 때 조용히 이상한 값을
내놓는 지표는 전략을 통째로 망친다. 모자라면 `None` 을 돌려준다.

모든 함수는 종가 등의 실수 목록을 받아 같은 길이의 목록을 돌려주고, 계산할 수
없는 앞부분은 `None` 으로 채운다. 인덱스가 어긋나지 않게 하려는 것이다.
"""

from __future__ import annotations

from bot.models import Candle

Series = list[float | None]


def sma(values: list[float], period: int) -> Series:
    """단순이동평균."""
    if period <= 0:
        raise ValueError("period 는 1 이상이어야 합니다")
    out: Series = [None] * len(values)
    if len(values) < period:
        return out
    total = sum(values[:period])
    out[period - 1] = total / period
    for i in range(period, len(values)):
        total += values[i] - values[i - period]
        out[i] = total / period
    return out


def ema(values: list[float], period: int) -> Series:
    """지수이동평균. 첫 값은 SMA 로 시작한다(관례)."""
    if period <= 0:
        raise ValueError("period 는 1 이상이어야 합니다")
    out: Series = [None] * len(values)
    if len(values) < period:
        return out
    multiplier = 2 / (period + 1)
    previous = sum(values[:period]) / period
    out[period - 1] = previous
    for i in range(period, len(values)):
        previous = (values[i] - previous) * multiplier + previous
        out[i] = previous
    return out


def stddev(values: list[float], period: int) -> Series:
    """표본이 아닌 모집단 표준편차 — 볼린저밴드의 관례를 따른다."""
    out: Series = [None] * len(values)
    if len(values) < period:
        return out
    for i in range(period - 1, len(values)):
        window = values[i - period + 1 : i + 1]
        mean = sum(window) / period
        out[i] = (sum((v - mean) ** 2 for v in window) / period) ** 0.5
    return out


def rsi(values: list[float], period: int = 14) -> Series:
    """상대강도지수. Wilder 의 평활 방식을 쓴다."""
    out: Series = [None] * len(values)
    if len(values) <= period:
        return out

    gains = losses = 0.0
    for i in range(1, period + 1):
        change = values[i] - values[i - 1]
        gains += max(change, 0.0)
        losses += max(-change, 0.0)
    avg_gain, avg_loss = gains / period, losses / period
    out[period] = _rsi_value(avg_gain, avg_loss)

    for i in range(period + 1, len(values)):
        change = values[i] - values[i - 1]
        avg_gain = (avg_gain * (period - 1) + max(change, 0.0)) / period
        avg_loss = (avg_loss * (period - 1) + max(-change, 0.0)) / period
        out[i] = _rsi_value(avg_gain, avg_loss)
    return out


def _rsi_value(avg_gain: float, avg_loss: float) -> float:
    # 하락이 전혀 없으면 RSI 는 100 이다. 0 으로 나누지 않도록 따로 처리한다.
    if avg_loss == 0:
        return 100.0 if avg_gain > 0 else 50.0
    return 100.0 - (100.0 / (1 + avg_gain / avg_loss))


def true_range(candles: list[Candle]) -> Series:
    """진폭. 전 종가와의 갭까지 포함한다."""
    out: Series = [None] * len(candles)
    for i, candle in enumerate(candles):
        if i == 0:
            out[i] = candle.high - candle.low
            continue
        previous_close = candles[i - 1].close
        out[i] = max(
            candle.high - candle.low,
            abs(candle.high - previous_close),
            abs(candle.low - previous_close),
        )
    return out


def atr(candles: list[Candle], period: int = 14) -> Series:
    """평균 진폭. 변동성에 맞춰 손절 폭을 잡는 데 쓴다."""
    ranges = true_range(candles)
    values = [r for r in ranges if r is not None]
    out: Series = [None] * len(candles)
    if len(values) < period:
        return out
    previous = sum(values[:period]) / period
    out[period - 1] = previous
    for i in range(period, len(candles)):
        previous = (previous * (period - 1) + ranges[i]) / period
        out[i] = previous
    return out


def macd(
    values: list[float], fast: int = 12, slow: int = 26, signal: int = 9
) -> tuple[Series, Series, Series]:
    """MACD 선, 시그널선, 히스토그램."""
    fast_ema, slow_ema = ema(values, fast), ema(values, slow)
    line: Series = [
        (f - s) if (f is not None and s is not None) else None
        for f, s in zip(fast_ema, slow_ema)
    ]
    # 시그널선은 MACD 선이 존재하는 구간에서만 계산한다.
    defined = [v for v in line if v is not None]
    signal_tail = ema(defined, signal)
    signal_line: Series = [None] * (len(line) - len(signal_tail)) + signal_tail
    histogram: Series = [
        (m - s) if (m is not None and s is not None) else None
        for m, s in zip(line, signal_line)
    ]
    return line, signal_line, histogram


def bollinger(
    values: list[float], period: int = 20, deviations: float = 2.0
) -> tuple[Series, Series, Series]:
    """볼린저밴드 (상단, 중심, 하단)."""
    middle = sma(values, period)
    spread = stddev(values, period)
    upper: Series = [
        (m + deviations * s) if (m is not None and s is not None) else None
        for m, s in zip(middle, spread)
    ]
    lower: Series = [
        (m - deviations * s) if (m is not None and s is not None) else None
        for m, s in zip(middle, spread)
    ]
    return upper, middle, lower


def donchian(candles: list[Candle], period: int = 20) -> tuple[Series, Series]:
    """돈치안 채널 (최고가, 최저가).

    **직전 봉까지만** 본다. 현재 봉을 포함하면 "현재가가 최고가를 돌파했다" 가
    항상 참이 되어 신호가 의미를 잃는다.
    """
    highs: Series = [None] * len(candles)
    lows: Series = [None] * len(candles)
    for i in range(period, len(candles)):
        window = candles[i - period : i]
        highs[i] = max(c.high for c in window)
        lows[i] = min(c.low for c in window)
    return highs, lows


def supertrend(
    candles: list[Candle], period: int = 10, multiplier: float = 3.0
) -> tuple[Series, list[int | None]]:
    """슈퍼트렌드 선과 추세 방향(1 = 상승, -1 = 하락).

    ATR 로 만든 밴드를 가격이 넘을 때만 방향이 바뀌고, 그전까지는 밴드가 한
    방향으로만 조여진다 — 그래서 추적 손절처럼 동작한다.
    """
    atr_values = atr(candles, period)
    line: Series = [None] * len(candles)
    trend: list[int | None] = [None] * len(candles)

    upper = lower = None
    direction = 1
    for i, candle in enumerate(candles):
        if atr_values[i] is None:
            continue
        mid = (candle.high + candle.low) / 2
        basic_upper = mid + multiplier * atr_values[i]
        basic_lower = mid - multiplier * atr_values[i]

        if upper is None:
            upper, lower = basic_upper, basic_lower
        else:
            previous_close = candles[i - 1].close
            upper = basic_upper if (basic_upper < upper or previous_close > upper) else upper
            lower = basic_lower if (basic_lower > lower or previous_close < lower) else lower

        if direction == 1 and candle.close < lower:
            direction = -1
        elif direction == -1 and candle.close > upper:
            direction = 1

        trend[i] = direction
        line[i] = lower if direction == 1 else upper
    return line, trend


def stochastic(
    candles: list[Candle], k_period: int = 14, smooth_k: int = 3, d_period: int = 3
) -> tuple[Series, Series]:
    """스토캐스틱 (%K, %D).

    %K 는 최근 k_period 봉의 고저 범위에서 현재 종가가 어디쯤인지(0~100)를
    smooth_k 로 평활한 값이고, %D 는 %K 의 SMA 다. 범위가 0이면(모든 봉이
    같은 가격) 중립인 50 을 돌려준다.
    """
    raw: Series = [None] * len(candles)
    for i in range(k_period - 1, len(candles)):
        window = candles[i - k_period + 1 : i + 1]
        high = max(c.high for c in window)
        low = min(c.low for c in window)
        raw[i] = 50.0 if high == low else (candles[i].close - low) / (high - low) * 100
    defined = [v for v in raw if v is not None]
    k_tail = sma(defined, smooth_k)
    k: Series = [None] * (len(raw) - len(k_tail)) + k_tail
    k_defined = [v for v in k if v is not None]
    d_tail = sma(k_defined, d_period)
    d: Series = [None] * (len(k) - len(d_tail)) + d_tail
    return k, d


def williams_r(candles: list[Candle], period: int = 14) -> Series:
    """윌리엄스 %R. 0 ~ -100 스케일 (0 이 과매수, -100 이 과매도)."""
    out: Series = [None] * len(candles)
    for i in range(period - 1, len(candles)):
        window = candles[i - period + 1 : i + 1]
        high = max(c.high for c in window)
        low = min(c.low for c in window)
        out[i] = -50.0 if high == low else (high - candles[i].close) / (high - low) * -100
    return out


def cci(candles: list[Candle], period: int = 20) -> Series:
    """상품채널지수. 대표가가 평균에서 평균편차의 몇 배 벗어났는지."""
    typical = [(c.high + c.low + c.close) / 3 for c in candles]
    out: Series = [None] * len(candles)
    for i in range(period - 1, len(candles)):
        window = typical[i - period + 1 : i + 1]
        mean = sum(window) / period
        mean_dev = sum(abs(v - mean) for v in window) / period
        out[i] = 0.0 if mean_dev == 0 else (typical[i] - mean) / (0.015 * mean_dev)
    return out


def mfi(candles: list[Candle], period: int = 14) -> Series:
    """자금흐름지수. 거래량을 가중한 RSI 라고 보면 된다."""
    out: Series = [None] * len(candles)
    if len(candles) <= period:
        return out
    typical = [(c.high + c.low + c.close) / 3 for c in candles]
    flows = [0.0] + [typical[i] * candles[i].volume for i in range(1, len(candles))]
    for i in range(period, len(candles)):
        positive = negative = 0.0
        for j in range(i - period + 1, i + 1):
            if typical[j] > typical[j - 1]:
                positive += flows[j]
            elif typical[j] < typical[j - 1]:
                negative += flows[j]
        if negative == 0:
            out[i] = 100.0 if positive > 0 else 50.0
        else:
            out[i] = 100.0 - 100.0 / (1 + positive / negative)
    return out


def roc(values: list[float], period: int = 10) -> Series:
    """변화율. period 봉 전 대비 몇 % 움직였는지."""
    out: Series = [None] * len(values)
    for i in range(period, len(values)):
        base = values[i - period]
        if base != 0:
            out[i] = (values[i] / base - 1) * 100
    return out


def obv(candles: list[Candle]) -> list[float]:
    """온밸런스볼륨. 오른 봉의 거래량은 더하고 내린 봉은 뺀 누적값."""
    out: list[float] = [0.0] * len(candles)
    for i in range(1, len(candles)):
        if candles[i].close > candles[i - 1].close:
            out[i] = out[i - 1] + candles[i].volume
        elif candles[i].close < candles[i - 1].close:
            out[i] = out[i - 1] - candles[i].volume
        else:
            out[i] = out[i - 1]
    return out


def rolling_vwap(candles: list[Candle], period: int = 50) -> Series:
    """이동 거래량가중평균가. 거래량이 전부 0이면 대표가 평균으로 대체한다."""
    typical = [(c.high + c.low + c.close) / 3 for c in candles]
    out: Series = [None] * len(candles)
    for i in range(period - 1, len(candles)):
        window = candles[i - period + 1 : i + 1]
        volume = sum(c.volume for c in window)
        if volume > 0:
            out[i] = sum(t * c.volume for t, c in
                         zip(typical[i - period + 1 : i + 1], window)) / volume
        else:
            out[i] = sum(typical[i - period + 1 : i + 1]) / period
    return out


def adx(candles: list[Candle], period: int = 14) -> tuple[Series, Series, Series]:
    """방향성 지표 (+DI, −DI, ADX). Wilder 평활을 쓴다.

    +DI/−DI 는 오르는 힘과 내리는 힘, ADX 는 그 차이의 평활값 — 즉 방향과
    무관하게 **추세가 얼마나 뚜렷한지**를 나타낸다(25 이상이면 추세장이라는
    것이 관례).
    """
    n = len(candles)
    plus_di: Series = [None] * n
    minus_di: Series = [None] * n
    adx_out: Series = [None] * n
    if n <= period:
        return plus_di, minus_di, adx_out

    ranges = true_range(candles)
    plus_dm = [0.0] * n
    minus_dm = [0.0] * n
    for i in range(1, n):
        up = candles[i].high - candles[i - 1].high
        down = candles[i - 1].low - candles[i].low
        plus_dm[i] = up if (up > down and up > 0) else 0.0
        minus_dm[i] = down if (down > up and down > 0) else 0.0

    tr_sum = sum(ranges[i] for i in range(1, period + 1))
    plus_sum = sum(plus_dm[1 : period + 1])
    minus_sum = sum(minus_dm[1 : period + 1])
    dx_values: list[float] = []
    for i in range(period, n):
        if i > period:
            tr_sum = tr_sum - tr_sum / period + ranges[i]
            plus_sum = plus_sum - plus_sum / period + plus_dm[i]
            minus_sum = minus_sum - minus_sum / period + minus_dm[i]
        p = 0.0 if tr_sum == 0 else plus_sum / tr_sum * 100
        m = 0.0 if tr_sum == 0 else minus_sum / tr_sum * 100
        plus_di[i], minus_di[i] = p, m
        dx = 0.0 if (p + m) == 0 else abs(p - m) / (p + m) * 100
        dx_values.append(dx)
        if len(dx_values) == period:
            adx_out[i] = sum(dx_values) / period
        elif len(dx_values) > period:
            adx_out[i] = (adx_out[i - 1] * (period - 1) + dx) / period
    return plus_di, minus_di, adx_out


def keltner(
    candles: list[Candle], period: int = 20, atr_period: int = 10, multiplier: float = 2.0
) -> tuple[Series, Series, Series]:
    """켈트너 채널 (상단, 중심, 하단). 중심은 EMA, 폭은 ATR 배수.

    볼린저밴드와 달리 폭이 표준편차가 아니라 ATR 이라 급등락에 덜 예민하다.
    """
    closes = [c.close for c in candles]
    middle = ema(closes, period)
    atr_values = atr(candles, atr_period)
    upper: Series = [
        (m + multiplier * a) if (m is not None and a is not None) else None
        for m, a in zip(middle, atr_values)
    ]
    lower: Series = [
        (m - multiplier * a) if (m is not None and a is not None) else None
        for m, a in zip(middle, atr_values)
    ]
    return upper, middle, lower


def psar(
    candles: list[Candle], af_start: float = 0.02, af_step: float = 0.02, af_max: float = 0.2
) -> tuple[Series, list[int | None]]:
    """파라볼릭 SAR 과 추세 방향(1 = 상승, -1 = 하락).

    SAR 점은 추세 방향으로만 가속하며 따라온다. 가격이 점을 건드리면 추세가
    뒤집히고 점이 반대편으로 넘어간다 — 슈퍼트렌드처럼 추적 손절로 쓸 수 있다.
    """
    n = len(candles)
    out: Series = [None] * n
    trend: list[int | None] = [None] * n
    if n < 2:
        return out, trend

    direction = 1 if candles[1].close >= candles[0].close else -1
    sar = candles[0].low if direction == 1 else candles[0].high
    extreme = candles[1].high if direction == 1 else candles[1].low
    af = af_start

    for i in range(1, n):
        sar = sar + af * (extreme - sar)
        if direction == 1:
            # SAR 는 직전 두 봉의 저가보다 위로 올라올 수 없다(관례).
            sar = min(sar, candles[i - 1].low, candles[i - 2].low if i >= 2 else candles[i - 1].low)
            if candles[i].low < sar:
                direction, sar = -1, extreme
                extreme, af = candles[i].low, af_start
            elif candles[i].high > extreme:
                extreme, af = candles[i].high, min(af + af_step, af_max)
        else:
            sar = max(sar, candles[i - 1].high, candles[i - 2].high if i >= 2 else candles[i - 1].high)
            if candles[i].high > sar:
                direction, sar = 1, extreme
                extreme, af = candles[i].high, af_start
            elif candles[i].low < extreme:
                extreme, af = candles[i].low, min(af + af_step, af_max)
        out[i], trend[i] = sar, direction
    return out, trend


def heikin_ashi(candles: list[Candle]) -> list[Candle]:
    """하이킨아시 변환. 잔파동을 평활해 추세의 색(양봉/음봉)을 읽기 쉽게 한다.

    되돌려주는 캔들의 시가·종가는 실제 체결가가 아니다 — **판단용으로만** 쓰고
    주문 가격으로 쓰면 안 된다.
    """
    out: list[Candle] = []
    for c in candles:
        ha_close = (c.open + c.high + c.low + c.close) / 4
        ha_open = (c.open + c.close) / 2 if not out else (out[-1].open + out[-1].close) / 2
        out.append(Candle(
            timestamp=c.timestamp,
            open=ha_open,
            high=max(c.high, ha_open, ha_close),
            low=min(c.low, ha_open, ha_close),
            close=ha_close,
            volume=c.volume,
        ))
    return out


def ichimoku(
    candles: list[Candle], tenkan_period: int = 9, kijun_period: int = 26
) -> tuple[Series, Series]:
    """일목균형표의 전환선·기준선. 각 기간의 (최고가+최저가)/2 다.

    이동평균과 달리 종가가 아니라 **범위의 중간**을 보므로, 횡보 중에는 수평으로
    멈춰 있다가 범위를 벗어나야 움직인다.
    """
    def midline(period: int) -> Series:
        out: Series = [None] * len(candles)
        for i in range(period - 1, len(candles)):
            window = candles[i - period + 1 : i + 1]
            out[i] = (max(c.high for c in window) + min(c.low for c in window)) / 2
        return out

    return midline(tenkan_period), midline(kijun_period)


def ichimoku_cloud(
    candles: list[Candle],
    tenkan_period: int = 9,
    kijun_period: int = 26,
    senkou_b_period: int = 52,
    shift: int = 26,
) -> tuple[Series, Series, Series, Series]:
    """일목균형표 전체 — (전환선, 기준선, 선행스팬A, 선행스팬B).

    선행스팬은 원래 26봉 **앞에** 그려지는 선이다. 여기서는 반대로 정렬한다 —
    돌려주는 스팬의 i 번째 값은 **i 봉 시점에 그 자리에 그려져 있던 구름**,
    즉 26봉 전에 계산된 값이다. 전략은 `span_a[-1]`, `span_b[-1]` 만 보면
    "지금 가격 위치의 구름"과 비교할 수 있다.

    구름 상단은 `max(span_a, span_b)`, 하단은 `min(...)`, 색은 A>B 면 양운이다.
    """
    tenkan, kijun = ichimoku(candles, tenkan_period, kijun_period)

    def midline(period: int) -> Series:
        out: Series = [None] * len(candles)
        for i in range(period - 1, len(candles)):
            window = candles[i - period + 1 : i + 1]
            out[i] = (max(c.high for c in window) + min(c.low for c in window)) / 2
        return out

    raw_a: Series = [
        (t + k) / 2 if (t is not None and k is not None) else None
        for t, k in zip(tenkan, kijun)
    ]
    raw_b = midline(senkou_b_period)
    # i 시점의 구름 = i-shift 시점에 계산된 스팬.
    span_a: Series = [None] * shift + raw_a[: len(candles) - shift]
    span_b: Series = [None] * shift + raw_b[: len(candles) - shift]
    return tenkan, kijun, span_a, span_b


def highest(values: list[float], period: int) -> Series:
    out: Series = [None] * len(values)
    for i in range(period - 1, len(values)):
        out[i] = max(values[i - period + 1 : i + 1])
    return out


def lowest(values: list[float], period: int) -> Series:
    out: Series = [None] * len(values)
    for i in range(period - 1, len(values)):
        out[i] = min(values[i - period + 1 : i + 1])
    return out
