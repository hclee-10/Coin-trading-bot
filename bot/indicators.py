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
