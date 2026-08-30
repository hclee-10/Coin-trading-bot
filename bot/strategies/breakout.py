"""돌파·조합 전략."""

from __future__ import annotations

from bot.indicators import ema, rsi
from bot.models import Conviction, PositionSide, Signal, SignalAction
from bot.strategies.base import Strategy, StrategyContext, register_strategy
from bot.strategies.trend import _atr_stop


@register_strategy("volatility_breakout")
class VolatilityBreakoutStrategy(Strategy):
    summary = "직전 봉 변동폭의 k배를 넘어서면 그 방향으로 진입"
    category = "breakout"
    description = """
래리 윌리엄스가 알린 방식이다. 직전 봉의 고가-저가 폭(변동폭)에 계수 k(기본 0.5)를
곱한 값을 현재 봉 시가에 더한 가격을 '돌파 기준선'으로 잡는다. 가격이 그 선을
넘으면 매수, 아래로 뚫으면 매도한다.

논리는 이렇다 — 평소 움직이던 폭의 절반을 한 방향으로 단숨에 움직였다면, 그건
우연한 흔들림이 아니라 **누군가 강하게 밀고 있다는 뜻**이라는 것이다.

원래는 일봉 기준으로 쓰던 방식이라 짧은 봉에서는 신호가 훨씬 잦아진다. 거래가
늘어나면 수수료 부담이 커지므로, 백테스트에서 거래 횟수를 꼭 확인해야 한다.

**강점**: 급등락의 초입을 잡는다. 계산이 단순하고 지연이 거의 없다.
**약점**: 거래 빈도가 높아 수수료에 취약하다. 가짜 돌파가 잦다.
"""
    algorithm = """
**지표**  직전 봉의 변동폭 `고가 − 저가`, EMA(10), ATR(14)

**진입**  현재 봉의 시가에서 직전 변동폭의 k배(기본 0.5)만큼 움직였는지 본다.
- 롱: 종가 > 시가 + (직전 변동폭 × 0.5)
- 숏: 종가 < 시가 − (직전 변동폭 × 0.5)

**청산**  EMA10 을 반대로 이탈하면 청산.

**손절**  진입가 ∓ (ATR14 × 1.5)

**확신도**  기준선을 넘어선 초과폭 ÷ 직전 변동폭
- ≥ 0.5 → VERY_HIGH · ≥ 0.25 → HIGH · ≥ 0.1 → MEDIUM · 그 외 LOW

원래 일봉용 방식이라 짧은 봉에서는 신호가 훨씬 잦다. **거래 횟수를 꼭 확인할 것.**

**파라미터**  `k`, `exit_period`, `atr_multiplier`
"""

    def setup(self) -> None:
        self.k = float(self.params.get("k", 0.5))
        self.atr_multiplier = float(self.params.get("atr_multiplier", 1.5))
        self.exit_period = int(self.params.get("exit_period", 10))

    @property
    def warmup_candles(self) -> int:
        return max(self.exit_period, 20) + 20

    def generate(self, ctx: StrategyContext) -> Signal:
        candles = ctx.closed_candles
        if len(candles) < self.warmup_candles:
            return Signal(reason="워밍업 부족")

        reference, current = candles[-2], candles[-1]
        range_size = reference.high - reference.low
        if range_size <= 0:
            return Signal(reason="직전 봉 변동폭 없음")

        long_trigger = current.open + range_size * self.k
        short_trigger = current.open - range_size * self.k
        price = current.close

        if ctx.position.is_open:
            fast = ema([c.close for c in candles], self.exit_period)
            if fast[-1] is None:
                return Signal(reason="지표 계산 불가")
            wrong_way = (
                (ctx.position.side is PositionSide.LONG and price < fast[-1])
                or (ctx.position.side is PositionSide.SHORT and price > fast[-1])
            )
            return Signal(
                action=SignalAction.EXIT if wrong_way else SignalAction.HOLD,
                reason="단기 추세 이탈" if wrong_way else "돌파 방향 유지",
            )

        # 기준선을 얼마나 크게 넘어섰는지로 확신을 나눈다.
        if price > long_trigger:
            excess = (price - long_trigger) / range_size
            return Signal(action=SignalAction.ENTER_LONG,
                          strength=_excess_conviction(excess),
                          stop_loss=_atr_stop(candles, len(candles) - 1, price,
                                              PositionSide.LONG, self.atr_multiplier),
                          reason=f"상방 돌파 (기준선 +{excess:.2f}배)")
        if price < short_trigger:
            excess = (short_trigger - price) / range_size
            return Signal(action=SignalAction.ENTER_SHORT,
                          strength=_excess_conviction(excess),
                          stop_loss=_atr_stop(candles, len(candles) - 1, price,
                                              PositionSide.SHORT, self.atr_multiplier),
                          reason=f"하방 돌파 (기준선 -{excess:.2f}배)")
        return Signal(reason="돌파 없음")


def _excess_conviction(excess: float) -> float:
    if excess >= 0.5:
        return Conviction.VERY_HIGH.value
    if excess >= 0.25:
        return Conviction.HIGH.value
    if excess >= 0.1:
        return Conviction.MEDIUM.value
    return Conviction.LOW.value


@register_strategy("trend_pullback")
class TrendPullbackStrategy(Strategy):
    summary = "상승 추세에서 눌릴 때만 매수 (추세 + 타이밍 조합)"
    category = "combo"
    description = """
두 접근의 장점을 합친 전략이다. 먼저 긴 이동평균(기본 100봉)으로 **큰 방향**을
정하고, 그 방향으로만 거래한다. 그다음 RSI 로 **눌림목**을 기다렸다가 들어간다.

즉 상승 추세에서는 매수만 하되, 아무 때나 사는 게 아니라 일시적으로 떨어졌을 때
(RSI 40 아래로 갔다가 회복)만 산다. 하락 추세에서는 반대로 한다.

추세추종의 "방향은 맞히되 진입가가 나쁘다"는 약점과, 평균회귀의 "진입가는 좋지만
방향이 틀리면 크게 잃는다"는 약점을 서로 메우는 구조다.

**강점**: 진입가가 유리해서 손절 폭이 좁다. 추세를 거스르지 않는다.
**약점**: 조건이 둘 다 맞아야 해서 **거래 기회가 훨씬 적다.** 백테스트에서
거래 횟수가 너무 적으면 결과를 신뢰하기 어렵다는 점을 감안해야 한다.
"""
    algorithm = """
**지표**  EMA(100) 으로 방향, RSI(14) 로 타이밍, ATR(14) 로 손절

**진입**  두 조건이 **모두** 맞아야 한다.
- 롱: 종가 > EMA100 (상승 추세) **그리고** 직전 RSI < 40 이고 이번 RSI ≥ 40
- 숏: 종가 < EMA100 (하락 추세) **그리고** 직전 RSI > 60 이고 이번 RSI ≤ 60

**청산**  RSI 가 65(숏은 35)에 도달하거나, **추세 방향이 바뀌면** 즉시 청산.

**손절**  진입가 ∓ (ATR14 × 1.5)

**확신도**  현재가와 EMA100 의 거리 `|가격 − EMA100| / EMA100 × 100`
- ≥ 3% → VERY_HIGH · ≥ 1.5% → HIGH · ≥ 0.5% → MEDIUM · 그 외 LOW

조건이 둘 다 맞아야 해서 **거래 기회가 적다.** 백테스트 거래 횟수가 너무 적으면
결과를 신뢰하기 어렵다는 점을 감안할 것.

**파라미터**  `trend_period`, `rsi_period`, `pullback_level`, `exit_level`, `atr_multiplier`
"""

    def setup(self) -> None:
        self.trend_period = int(self.params.get("trend_period", 100))
        self.rsi_period = int(self.params.get("rsi_period", 14))
        self.pullback_level = float(self.params.get("pullback_level", 40))
        self.exit_level = float(self.params.get("exit_level", 65))
        self.atr_multiplier = float(self.params.get("atr_multiplier", 1.5))

    @property
    def warmup_candles(self) -> int:
        return self.trend_period + 30

    def generate(self, ctx: StrategyContext) -> Signal:
        candles = ctx.closed_candles
        if len(candles) < self.warmup_candles:
            return Signal(reason="워밍업 부족")

        closes = [c.close for c in candles]
        trend = ema(closes, self.trend_period)
        momentum = rsi(closes, self.rsi_period)
        if trend[-1] is None or momentum[-1] is None or momentum[-2] is None:
            return Signal(reason="지표 계산 불가")

        price = closes[-1]
        uptrend = price > trend[-1]
        current, previous = momentum[-1], momentum[-2]

        if ctx.position.side is PositionSide.LONG:
            if current >= self.exit_level or not uptrend:
                return Signal(action=SignalAction.EXIT,
                              reason="목표 도달" if current >= self.exit_level else "추세 이탈")
            return Signal(reason=f"RSI {current:.0f} 상승 대기")
        if ctx.position.side is PositionSide.SHORT:
            if current <= (100 - self.exit_level) or uptrend:
                return Signal(action=SignalAction.EXIT, reason="목표 도달 또는 추세 이탈")
            return Signal(reason=f"RSI {current:.0f} 하락 대기")

        # 추세 방향과 눌림 정도가 모두 맞을 때만 들어간다.
        distance_pct = abs(price - trend[-1]) / trend[-1] * 100
        conviction = (
            Conviction.VERY_HIGH.value if distance_pct >= 3
            else Conviction.HIGH.value if distance_pct >= 1.5
            else Conviction.MEDIUM.value if distance_pct >= 0.5
            else Conviction.LOW.value
        )
        if uptrend and previous < self.pullback_level <= current:
            return Signal(action=SignalAction.ENTER_LONG, strength=conviction,
                          stop_loss=_atr_stop(candles, len(candles) - 1, price,
                                              PositionSide.LONG, self.atr_multiplier),
                          reason=f"상승 추세 눌림목 (RSI {previous:.0f} → {current:.0f})")
        if not uptrend and previous > (100 - self.pullback_level) >= current:
            return Signal(action=SignalAction.ENTER_SHORT, strength=conviction,
                          stop_loss=_atr_stop(candles, len(candles) - 1, price,
                                              PositionSide.SHORT, self.atr_multiplier),
                          reason=f"하락 추세 반등 (RSI {previous:.0f} → {current:.0f})")
        return Signal(reason="조건 미충족")
