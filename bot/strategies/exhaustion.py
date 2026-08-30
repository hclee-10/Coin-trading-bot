"""과열 소진 반전 전략.

평균회귀 계열이지만 "얼마나 벗어났나"(가격 기준)가 아니라 **"팔 사람이 다
팔았나"**(참여자 소진)를 본다. 스토캐스틱 RSI 는 모멘텀의 상대적 소진을,
거래량 클라이맥스는 물량의 소진을 잰다.
"""

from __future__ import annotations

from bot.indicators import atr, sma, stoch_rsi
from bot.models import Conviction, PositionSide, Signal, SignalAction
from bot.strategies.base import Strategy, StrategyContext, register_strategy
from bot.strategies.trend import _atr_stop


@register_strategy("stochrsi_reversion")
class StochRsiReversionStrategy(Strategy):
    summary = "스토캐스틱 RSI가 바닥권을 벗어나면 매수 — 조용한 장의 상대 과열까지 잡는다"
    category = "reversion"
    description = """
RSI 의 약점 하나: 변동성이 낮은 조용한 장에서는 RSI 가 40~60 사이만 오가서
과매도(30)에 닿지 않는다 — rsi_reversion 이 몇 주씩 놀게 되는 이유다. 스토캐스틱
RSI 는 이 문제를 정규화로 푼다. **RSI 자체를 최근 14봉 범위 안에서 다시 0~100
으로 펴서**, "절대적으로 과매도"가 아니라 "최근 기준 상대적으로 바닥"을 잡는다.

그래서 이 지표는 어떤 장에서든 극단에 규칙적으로 닿는다. 신호가 RSI 보다 훨씬
잦고, 그만큼 개별 신호의 무게는 가볍다 — rsi_reversion(드물고 무거움)과 정확히
반대 성향이라 순위표 비교 짝으로 넣었다.

진입은 관례대로 20 아래로 갔다가 올라오는 순간, 청산은 중앙(50) 회복이다.
확신도는 직전 극단의 깊이로 잰다.

**강점**: 조용한 장에서도 신호가 나온다. 되돌림 타이밍이 빠르다.
**약점**: 정규화의 대가로 "얼마나 극단인가"의 절대 정보가 사라진다 — 진짜
투매와 사소한 눌림이 같은 20 으로 보인다. 신호가 잦아 수수료 부담이 크다.
"""
    algorithm = """
**지표**  StochRSI(RSI 14 → 스토캐스틱 14, 평활 3), ATR(14)

**진입**
- 롱: 직전 StochRSI < 20 이고 이번 ≥ 20
- 숏: 직전 > 80 이고 이번 ≤ 80

**청산**  StochRSI 가 50 에 도달하면 청산.

**손절**  진입가 ∓ (ATR14 × 1.5)

**확신도**  직전 값의 극단 깊이
- ≥ 15 → VERY_HIGH · ≥ 8 → HIGH · ≥ 3 → MEDIUM · 그 외 LOW

**파라미터**  `rsi_period`, `stoch_period`, `smooth`, `oversold`, `overbought`, `atr_multiplier`
"""

    def setup(self) -> None:
        self.rsi_period = int(self.params.get("rsi_period", 14))
        self.stoch_period = int(self.params.get("stoch_period", 14))
        self.smooth = int(self.params.get("smooth", 3))
        self.oversold = float(self.params.get("oversold", 20))
        self.overbought = float(self.params.get("overbought", 80))
        self.atr_multiplier = float(self.params.get("atr_multiplier", 1.5))

    @property
    def warmup_candles(self) -> int:
        return self.rsi_period + self.stoch_period + self.smooth + 20

    def generate(self, ctx: StrategyContext) -> Signal:
        candles = ctx.closed_candles
        if len(candles) < self.warmup_candles:
            return Signal(reason="워밍업 부족")

        values = stoch_rsi([c.close for c in candles],
                           self.rsi_period, self.stoch_period, self.smooth)
        if values[-1] is None or values[-2] is None:
            return Signal(reason="지표 계산 불가")

        current, previous = values[-1], values[-2]
        price = candles[-1].close

        if ctx.position.side is PositionSide.LONG and current >= 50:
            return Signal(action=SignalAction.EXIT, reason="StochRSI 중앙 회복")
        if ctx.position.side is PositionSide.SHORT and current <= 50:
            return Signal(action=SignalAction.EXIT, reason="StochRSI 중앙 회복")
        if ctx.position.is_open:
            return Signal(reason=f"StochRSI {current:.0f} 회복 대기")

        if previous < self.oversold <= current:
            return Signal(
                action=SignalAction.ENTER_LONG,
                strength=_depth_conviction(self.oversold - previous),
                stop_loss=_atr_stop(candles, len(candles) - 1, price,
                                    PositionSide.LONG, self.atr_multiplier),
                reason=f"StochRSI 바닥권 탈출 ({previous:.0f} → {current:.0f})",
            )
        if previous > self.overbought >= current:
            return Signal(
                action=SignalAction.ENTER_SHORT,
                strength=_depth_conviction(previous - self.overbought),
                stop_loss=_atr_stop(candles, len(candles) - 1, price,
                                    PositionSide.SHORT, self.atr_multiplier),
                reason=f"StochRSI 천장권 이탈 ({previous:.0f} → {current:.0f})",
            )
        return Signal(reason=f"StochRSI {current:.0f} 중립")


def _depth_conviction(depth: float) -> float:
    if depth >= 15:
        return Conviction.VERY_HIGH.value
    if depth >= 8:
        return Conviction.HIGH.value
    if depth >= 3:
        return Conviction.MEDIUM.value
    return Conviction.LOW.value


@register_strategy("volume_climax")
class VolumeClimaxStrategy(Strategy):
    summary = "거래량이 폭증한 투매 봉에서 꼬리가 확인되면 반대로 진입"
    category = "reversion"
    description = """
클라이맥스(절정 매도)는 하락의 끝에서 자주 나오는 구도다 — 견디던 보유자들이
한꺼번에 던지면서 **거래량이 폭증**하고, 그 물량을 받은 손이 가격을 도로 밀어
올려 **긴 아래꼬리**가 남는다. "팔 사람이 다 팔았다"의 물증이 거래량과 꼬리에
동시에 찍히는 것이다. pin_bar 와 모양은 비슷하지만, 이쪽은 **거래량 폭증을
필수 조건**으로 요구해 훨씬 드물고 무거운 신호만 받는다.

조건: 봉의 거래량이 20봉 평균의 2배 이상 + 범위가 ATR 1.5배 이상(패닉의 크기)
+ 종가가 봉 범위의 위쪽 40% 안(받아친 흔적). 셋이 겹친 봉의 저가가 단기 바닥일
가능성에 건다. 매수 클라이맥스(폭증 + 위꼬리)는 거울상으로 숏이다.

거래량 데이터가 없는 환경(백테스트 합성 데이터 등)에서는 거래량 조건을 건너뛰고
등급만 낮춘다 — 신호를 잃는 것보다 낫다.

**강점**: 반전 신호 중 근거(물량 소진)가 가장 물리적이다. 손절이 꼬리 끝으로
명확하다.
**약점**: 진짜 폭락 초입의 1차 투매를 바닥으로 오인할 수 있다 — 클라이맥스는
여러 번 올 수 있다. 거래량 데이터 품질에 의존한다.
"""
    algorithm = """
**지표**  거래량 SMA(20), ATR(14)

**클라이맥스 봉** (매도 클라이맥스, 롱 기준)
- 거래량 ≥ 20봉 평균 × 2 (데이터가 무의미하면 이 조건은 건너뛰고 등급 하향)
- 봉 범위 ≥ ATR × 1.5
- 종가 위치 ≥ 봉 범위의 60% 지점 (아래에서 받아쳐 올린 흔적)
- 매수 클라이맥스(숏): 거울상 (종가 위치 ≤ 40% 지점)

**진입**  클라이맥스 봉 확인 즉시.

**청산**  SMA20 (종가 기준선) 도달 시 청산.

**손절**  클라이맥스 봉의 반대쪽 끝 − ATR × 0.1.

**확신도**  거래량 배율 × 봉 크기
- 배율 ≥ 3 이고 범위 ≥ ATR×2 → VERY_HIGH · 둘 중 하나 → HIGH · 기본 MEDIUM
- 거래량 조건을 건너뛴 경우 한 단계 하향

**파라미터**  `volume_multiple`, `range_atr`, `close_position`
"""

    def setup(self) -> None:
        self.volume_multiple = float(self.params.get("volume_multiple", 2.0))
        self.range_atr = float(self.params.get("range_atr", 1.5))
        self.close_position = float(self.params.get("close_position", 0.6))

    @property
    def warmup_candles(self) -> int:
        return 40

    def generate(self, ctx: StrategyContext) -> Signal:
        candles = ctx.closed_candles
        if len(candles) < self.warmup_candles:
            return Signal(reason="워밍업 부족")

        closes = [c.close for c in candles]
        baseline = sma(closes, 20)
        atr_values = atr(candles, 14)
        if baseline[-1] is None or atr_values[-1] is None or atr_values[-1] == 0:
            return Signal(reason="지표 계산 불가")

        c = candles[-1]
        price = c.close

        if ctx.position.side is PositionSide.LONG and price >= baseline[-1]:
            return Signal(action=SignalAction.EXIT, reason="기준선 도달")
        if ctx.position.side is PositionSide.SHORT and price <= baseline[-1]:
            return Signal(action=SignalAction.EXIT, reason="기준선 도달")
        if ctx.position.is_open:
            return Signal(reason="되돌림 대기")

        volumes = [x.volume for x in candles[-21:-1]]
        avg_volume = sum(volumes) / len(volumes)
        spread = max(volumes) - min(volumes)
        # 거래량이 전부 같으면(합성 데이터 등) 배율 조건이 무의미하다 —
        # 조건을 건너뛰되 등급을 낮춰서, 데이터 부재가 신호 부재가 되지 않게 한다.
        volume_usable = avg_volume > 0 and spread > 0
        multiple = c.volume / avg_volume if volume_usable else 0.0
        volume_ok = (multiple >= self.volume_multiple) if volume_usable else True

        span = c.high - c.low
        big_bar = span >= atr_values[-1] * self.range_atr
        if span <= 0 or not big_bar or not volume_ok:
            return Signal(reason="클라이맥스 없음")

        close_pos = (c.close - c.low) / span
        sell_climax = close_pos >= self.close_position
        buy_climax = close_pos <= 1 - self.close_position

        grade = 0
        if volume_usable and multiple >= self.volume_multiple * 1.5:
            grade += 1
        if span >= atr_values[-1] * self.range_atr * 1.33:
            grade += 1
        if not volume_usable:
            grade -= 1
        levels = [Conviction.LOW, Conviction.MEDIUM, Conviction.HIGH, Conviction.VERY_HIGH]
        conviction = levels[max(0, min(3, grade + 2))].value
        volume_note = f"거래량 ×{multiple:.1f}" if volume_usable else "거래량 미확인"

        if sell_climax:
            return Signal(action=SignalAction.ENTER_LONG, strength=conviction,
                          stop_loss=c.low - atr_values[-1] * 0.1,
                          reason=f"매도 클라이맥스 ({volume_note}, 범위 {span / atr_values[-1]:.1f} ATR)")
        if buy_climax:
            return Signal(action=SignalAction.ENTER_SHORT, strength=conviction,
                          stop_loss=c.high + atr_values[-1] * 0.1,
                          reason=f"매수 클라이맥스 ({volume_note}, 범위 {span / atr_values[-1]:.1f} ATR)")
        return Signal(reason="폭증 봉이나 꼬리 미확인")
