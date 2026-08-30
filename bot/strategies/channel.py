"""채널·밴드 전략.

가격 주위에 통계적인 '정상 범위'를 그려 놓고, 그 경계에서 매매한다. 경계를
**넘는 것**을 신호로 보면 돌파 전략이 되고(켈트너 돌파, 스퀴즈 돌파), 경계에서
**되돌아오는 것**을 신호로 보면 회귀 전략이 된다(VWAP 회귀, 레인지 페이드).

같은 채널을 정반대로 해석하는 두 계열을 나란히 등록해 두었다 — 볼린저
breakout/reversion 짝과 같은 이유로, 순위표에서 두 계열의 성적 차이가 곧
이 시장의 성격(추세형/횡보형)이다.
"""

from __future__ import annotations

from bot.indicators import atr, bollinger, donchian, keltner, rolling_vwap, sma, stddev
from bot.models import Conviction, PositionSide, Signal, SignalAction
from bot.strategies.base import Strategy, StrategyContext, register_strategy
from bot.strategies.trend import _atr_stop


@register_strategy("keltner_breakout")
class KeltnerBreakoutStrategy(Strategy):
    summary = "켈트너 채널(EMA±ATR) 상단을 종가가 넘으면 매수"
    category = "breakout"
    description = """
켈트너 채널은 EMA 위아래로 ATR 의 배수만큼 그린 밴드다. 볼린저밴드와 겉모습은
같지만 폭을 재는 자가 다르다 — 볼린저는 **표준편차**(종가의 흩어짐), 켈트너는
**ATR**(봉 자체의 진폭)이다.

이 차이가 돌파 전략에서 중요하다. 표준편차는 급등 한 방에 확 부풀어서, 정작
돌파가 나온 직후에 밴드가 가격을 따라 도망가 버린다. ATR 은 그보다 완만하게
반응하므로 켈트너 밴드는 돌파 후에도 제자리를 지키는 편이다. 그래서 같은
돌파라도 켈트너 쪽이 신호가 안정적이고, 대신 volatility 급변은 늦게 반영한다.

진입은 종가가 상단/하단 밴드를 넘을 때, 청산은 중심선(EMA)으로 돌아오면 한다.
bollinger_breakout 과 순위표에서 비교해 보라 — 어느 '자'가 이 시장에 맞는지
드러난다.

**강점**: 밴드가 이상치에 덜 휘둘려 가짜 돌파가 상대적으로 적다.
**약점**: 돌파 계열 공통 — 횡보장에서는 밴드 살짝 넘고 되돌아오는 손절이 반복된다.
"""
    algorithm = """
**지표**  켈트너 채널(EMA20, ATR10 × 2.0), ATR(14)

**진입**
- 롱: 종가 > 상단 밴드
- 숏: 종가 < 하단 밴드

**청산**  가격이 중심선(EMA20)으로 돌아오면 청산.

**손절**  진입가 ∓ (ATR14 × 2.0)

**확신도**  밴드를 넘어선 폭 `(종가 − 밴드) / ATR`
- ≥ 0.5 → VERY_HIGH · ≥ 0.25 → HIGH · ≥ 0.1 → MEDIUM · 그 외 LOW

**파라미터**  `period`, `atr_period`, `multiplier`, `atr_multiplier`
"""

    def setup(self) -> None:
        self.period = int(self.params.get("period", 20))
        self.atr_period = int(self.params.get("atr_period", 10))
        self.multiplier = float(self.params.get("multiplier", 2.0))
        self.atr_multiplier = float(self.params.get("atr_multiplier", 2.0))

    @property
    def warmup_candles(self) -> int:
        return max(self.period, self.atr_period) + 20

    def generate(self, ctx: StrategyContext) -> Signal:
        candles = ctx.closed_candles
        if len(candles) < self.warmup_candles:
            return Signal(reason="워밍업 부족")

        upper, middle, lower = keltner(candles, self.period, self.atr_period, self.multiplier)
        atr_values = atr(candles, 14)
        if upper[-1] is None or middle[-1] is None or atr_values[-1] is None:
            return Signal(reason="지표 계산 불가")

        price = candles[-1].close
        if ctx.position.is_open:
            back_inside = (
                (ctx.position.side is PositionSide.LONG and price < middle[-1])
                or (ctx.position.side is PositionSide.SHORT and price > middle[-1])
            )
            return Signal(
                action=SignalAction.EXIT if back_inside else SignalAction.HOLD,
                reason="중심선 복귀" if back_inside else "밴드 밖 유지",
            )

        if price > upper[-1]:
            excess = (price - upper[-1]) / atr_values[-1]
            return Signal(action=SignalAction.ENTER_LONG,
                          strength=_excess_atr_conviction(excess),
                          stop_loss=_atr_stop(candles, len(candles) - 1, price,
                                              PositionSide.LONG, self.atr_multiplier),
                          reason=f"켈트너 상단 돌파 (+{excess:.2f} ATR)")
        if price < lower[-1]:
            excess = (lower[-1] - price) / atr_values[-1]
            return Signal(action=SignalAction.ENTER_SHORT,
                          strength=_excess_atr_conviction(excess),
                          stop_loss=_atr_stop(candles, len(candles) - 1, price,
                                              PositionSide.SHORT, self.atr_multiplier),
                          reason=f"켈트너 하단 이탈 (-{excess:.2f} ATR)")
        return Signal(reason="채널 안")


def _excess_atr_conviction(excess: float) -> float:
    if excess >= 0.5:
        return Conviction.VERY_HIGH.value
    if excess >= 0.25:
        return Conviction.HIGH.value
    if excess >= 0.1:
        return Conviction.MEDIUM.value
    return Conviction.LOW.value


@register_strategy("squeeze_breakout")
class SqueezeBreakoutStrategy(Strategy):
    summary = "밴드 폭이 수축한 뒤의 돌파만 골라서 진입"
    category = "breakout"
    description = """
변동성은 뭉쳐 다닌다 — 조용한 구간 뒤에 요동치는 구간이 오고, 그 반대도 그렇다.
이 전략은 그 성질을 정면으로 쓴다. **볼린저밴드 폭이 평소보다 좁아진 상태
(스퀴즈)에서 나오는 돌파만** 받고, 밴드가 이미 벌어진 상태의 돌파는 무시한다.

이유: 밴드가 넓다는 것은 큰 움직임이 이미 지나갔다는 뜻이라, 거기서의 돌파는
추세의 끝물일 가능성이 높다. 반대로 밴드가 바짝 조여진 상태의 돌파는 수축된
에너지가 이제 막 터지는 것이라 이어질 여지가 크다.

bollinger_breakout 도 밴드 폭으로 확신도를 조절하지만, 이 전략은 한발 더 나가
**넓은 밴드에서는 아예 진입하지 않는다.** 신호 수를 버리고 질을 사는 선택이다.

**강점**: 돌파 전략의 최악 시나리오(변동성 꼭대기에서 진입)를 구조적으로 피한다.
**약점**: 스퀴즈가 없는 시장에서는 오래 놀게 된다. 스퀴즈 뒤 첫 돌파가 가짜이고
진짜는 반대 방향으로 터지는 경우(헤드페이크)에 당한다.
"""
    algorithm = """
**지표**  볼린저밴드(20, ±2σ), 밴드 폭의 SMA(50), ATR(14)

**스퀴즈 판정**  현재 밴드 폭(상단−하단)이 최근 50봉 평균 폭 × 0.8 미만.

**진입**  스퀴즈 상태(직전 봉 기준)에서 종가가 밴드를 넘을 때.
- 롱: 종가 > 상단 밴드
- 숏: 종가 < 하단 밴드

**청산**  가격이 중심선(SMA20)으로 돌아오면 청산.

**손절**  진입가 ∓ (ATR14 × 2.0)

**확신도**  수축 정도 `현재 폭 / 평균 폭`
- < 0.5 → VERY_HIGH · < 0.65 → HIGH · < 0.8 → MEDIUM · 그 외 LOW
(좁을수록 에너지가 많이 모였다고 본다)

**파라미터**  `period`, `deviations`, `width_lookback`, `squeeze_ratio`, `atr_multiplier`
"""

    def setup(self) -> None:
        self.period = int(self.params.get("period", 20))
        self.deviations = float(self.params.get("deviations", 2.0))
        self.width_lookback = int(self.params.get("width_lookback", 50))
        self.squeeze_ratio = float(self.params.get("squeeze_ratio", 0.8))
        self.atr_multiplier = float(self.params.get("atr_multiplier", 2.0))

    @property
    def warmup_candles(self) -> int:
        return self.period + self.width_lookback + 10

    def generate(self, ctx: StrategyContext) -> Signal:
        candles = ctx.closed_candles
        if len(candles) < self.warmup_candles:
            return Signal(reason="워밍업 부족")

        closes = [c.close for c in candles]
        upper, middle, lower = bollinger(closes, self.period, self.deviations)
        if upper[-1] is None or middle[-1] is None or upper[-2] is None:
            return Signal(reason="지표 계산 불가")

        price = closes[-1]
        if ctx.position.is_open:
            back_inside = (
                (ctx.position.side is PositionSide.LONG and price < middle[-1])
                or (ctx.position.side is PositionSide.SHORT and price > middle[-1])
            )
            return Signal(
                action=SignalAction.EXIT if back_inside else SignalAction.HOLD,
                reason="중심선 복귀" if back_inside else "돌파 방향 유지",
            )

        widths = [
            (u - l) if (u is not None and l is not None) else None
            for u, l in zip(upper, lower)
        ]
        defined = [w for w in widths if w is not None]
        if len(defined) < self.width_lookback + 1:
            return Signal(reason="폭 통계 부족")
        # 직전 봉까지의 평균 폭과 직전 봉의 폭을 비교한다 — 돌파 봉 자체는
        # 폭을 부풀리므로 판정에서 뺀다.
        average_width = sum(defined[-self.width_lookback - 1 : -1]) / self.width_lookback
        ratio = defined[-2] / average_width if average_width > 0 else 1.0
        squeezed = ratio < self.squeeze_ratio

        if not squeezed:
            if price > upper[-1] or price < lower[-1]:
                return Signal(reason=f"돌파했으나 스퀴즈 아님 (폭비율 {ratio:.2f})")
            return Signal(reason=f"스퀴즈 대기 (폭비율 {ratio:.2f})")

        conviction = (
            Conviction.VERY_HIGH.value if ratio < 0.5
            else Conviction.HIGH.value if ratio < 0.65
            else Conviction.MEDIUM.value if ratio < 0.8
            else Conviction.LOW.value
        )
        if price > upper[-1]:
            return Signal(action=SignalAction.ENTER_LONG, strength=conviction,
                          stop_loss=_atr_stop(candles, len(candles) - 1, price,
                                              PositionSide.LONG, self.atr_multiplier),
                          reason=f"스퀴즈 상방 돌파 (폭비율 {ratio:.2f})")
        if price < lower[-1]:
            return Signal(action=SignalAction.ENTER_SHORT, strength=conviction,
                          stop_loss=_atr_stop(candles, len(candles) - 1, price,
                                              PositionSide.SHORT, self.atr_multiplier),
                          reason=f"스퀴즈 하방 돌파 (폭비율 {ratio:.2f})")
        return Signal(reason=f"스퀴즈 중, 돌파 대기 (폭비율 {ratio:.2f})")


@register_strategy("vwap_reversion")
class VwapReversionStrategy(Strategy):
    summary = "거래량 가중 평균가(VWAP)에서 크게 벌어지면 반대로 진입"
    category = "reversion"
    description = """
VWAP(거래량 가중 평균가)은 "이 구간에 들어온 돈의 평균 단가"다. 단순 이동평균과
달리 거래가 몰린 가격대에 무게가 실리므로, **실제 매집이 일어난 가격**에 가깝다.
기관 트레이더들이 집행 벤치마크로 쓰는 바로 그 값이라, 가격이 VWAP 에서 크게
벌어지면 되돌리려는 주문 흐름이 실제로 생기는 경향이 있다.

이 전략은 최근 50봉의 이동 VWAP 을 계산하고, 가격이 거기서 **표준편차 2배 이상**
벌어지면 되돌림을 노리고 반대로 진입한다. 청산은 VWAP 복귀 시점이다. 이격을
고정 비율이 아니라 표준편차로 재기 때문에, 조용한 장에서는 작은 이격에도 반응하고
요동치는 장에서는 큰 이격만 받는다.

grid 전략과 발상이 비슷하지만 기준선(EMA vs VWAP)과 이격의 자(고정 % vs 표준편차)
가 다르다. 순위표에서 비교해 볼 만한 짝이다.

**강점**: 기준선에 거래량 정보가 실려 있다. 이격 기준이 변동성에 자동 적응한다.
**약점**: 평균회귀 공통 — 추세장에서 계속 반대편에 선다. 거래량 데이터가 고르지
않으면 VWAP 자체가 출렁인다.
"""
    algorithm = """
**지표**  이동 VWAP(50) — 대표가×거래량 가중 평균, 종가 표준편차(50), ATR(14)

**진입**  z-점수 = (종가 − VWAP) / 표준편차
- 롱: z ≤ -2.0
- 숏: z ≥ +2.0

**청산**  z 가 0 을 넘으면(VWAP 복귀) 청산.

**손절**  진입가 ∓ (ATR14 × 1.5)

**확신도**  |z|
- ≥ 3.0 → VERY_HIGH · ≥ 2.5 → HIGH · ≥ 2.2 → MEDIUM · 그 외 LOW

**파라미터**  `period`, `entry_z`, `atr_multiplier`
"""

    def setup(self) -> None:
        self.period = int(self.params.get("period", 50))
        self.entry_z = float(self.params.get("entry_z", 2.0))
        self.atr_multiplier = float(self.params.get("atr_multiplier", 1.5))

    @property
    def warmup_candles(self) -> int:
        return self.period + 20

    def generate(self, ctx: StrategyContext) -> Signal:
        candles = ctx.closed_candles
        if len(candles) < self.warmup_candles:
            return Signal(reason="워밍업 부족")

        closes = [c.close for c in candles]
        vwap = rolling_vwap(candles, self.period)
        spread = stddev(closes, self.period)
        if vwap[-1] is None or spread[-1] is None or spread[-1] == 0:
            return Signal(reason="지표 계산 불가")

        price = closes[-1]
        z = (price - vwap[-1]) / spread[-1]

        if ctx.position.side is PositionSide.LONG and z >= 0:
            return Signal(action=SignalAction.EXIT, reason="VWAP 복귀")
        if ctx.position.side is PositionSide.SHORT and z <= 0:
            return Signal(action=SignalAction.EXIT, reason="VWAP 복귀")
        if ctx.position.is_open:
            return Signal(reason=f"VWAP 복귀 대기 (z {z:+.2f})")

        if abs(z) < self.entry_z:
            return Signal(reason=f"이격 부족 (z {z:+.2f})")

        conviction = (
            Conviction.VERY_HIGH.value if abs(z) >= 3.0
            else Conviction.HIGH.value if abs(z) >= 2.5
            else Conviction.MEDIUM.value if abs(z) >= 2.2
            else Conviction.LOW.value
        )
        if z < 0:
            return Signal(action=SignalAction.ENTER_LONG, strength=conviction,
                          stop_loss=_atr_stop(candles, len(candles) - 1, price,
                                              PositionSide.LONG, self.atr_multiplier),
                          reason=f"VWAP 하방 이격 (z {z:+.2f})")
        return Signal(action=SignalAction.ENTER_SHORT, strength=conviction,
                      stop_loss=_atr_stop(candles, len(candles) - 1, price,
                                          PositionSide.SHORT, self.atr_multiplier),
                      reason=f"VWAP 상방 이격 (z {z:+.2f})")


@register_strategy("range_fade")
class RangeFadeStrategy(Strategy):
    summary = "박스권의 바닥에서 사고 천장에서 판다 (돈치안 역방향)"
    category = "range"
    description = """
돈치안 채널을 터틀(donchian_breakout)과 **정반대로** 쓴다. 터틀은 채널 끝을
뚫으면 따라가지만, 이 전략은 채널의 끝에 **닿으면 반대로** 간다 — 최근 40봉
범위의 바닥권(하위 15%)에 오면 매수, 천장권(상위 15%)에 오면 매도, 범위의
중간으로 돌아오면 청산한다.

전제는 "박스권은 유지된다"이다. 지지선 근처에서는 사려는 쪽이, 저항선 근처에서는
팔려는 쪽이 우세해서 가격이 범위 안으로 되밀린다는 것. 물론 박스가 깨지는 날이
반드시 오고, 그날 이 전략은 깨지는 방향의 반대편에 서 있게 된다. 그래서 손절을
**채널 바로 바깥**에 건다 — 박스가 깨졌다면 전제 자체가 무너진 것이므로 미련 없이
나온다.

범위가 좁을수록(변동성이 수축한 박스일수록) 확신을 낮춘다 — 좁은 박스는 곧
터질 가능성이 높아서다. 넓고 안정된 박스가 이 전략의 무대다.

**강점**: 진입 근거(지지/저항)와 손절 근거(박스 붕괴)가 명확히 대응된다.
**약점**: 박스 붕괴가 곧 손절이다. 추세 전환점마다 한 번씩 얻어맞는 구조라
박스장이 길게 유지되는 시장에서만 누적 수익이 난다.
"""
    algorithm = """
**지표**  돈치안 채널(40봉, 직전 봉까지), ATR(14)

**위치**  채널 내 위치 = (종가 − 채널저가) / (채널고가 − 채널저가), 0~1

**진입**
- 롱: 위치 ≤ 0.15 (바닥권)
- 숏: 위치 ≥ 0.85 (천장권)

**청산**  위치가 0.5(중간)를 되넘으면 청산.

**손절**  채널 경계 바깥 ATR14 × 0.5 — 박스가 깨지면 전제가 무너진 것이다.
- 롱: 채널저가 − ATR×0.5 · 숏: 채널고가 + ATR×0.5

**확신도**  채널 폭 `(고가−저가)/저가 × 100` 이 넓고 안정적일수록 올린다
- ≥ 4% → VERY_HIGH · ≥ 2.5% → HIGH · ≥ 1.5% → MEDIUM · 그 외 LOW

**파라미터**  `period`, `edge`, `atr_buffer`
"""

    def setup(self) -> None:
        self.period = int(self.params.get("period", 40))
        self.edge = float(self.params.get("edge", 0.15))
        self.atr_buffer = float(self.params.get("atr_buffer", 0.5))

    @property
    def warmup_candles(self) -> int:
        return self.period + 20

    def generate(self, ctx: StrategyContext) -> Signal:
        candles = ctx.closed_candles
        if len(candles) < self.warmup_candles:
            return Signal(reason="워밍업 부족")

        highs, lows = donchian(candles, self.period)
        atr_values = atr(candles, 14)
        if highs[-1] is None or lows[-1] is None or atr_values[-1] is None:
            return Signal(reason="지표 계산 불가")

        high, low = highs[-1], lows[-1]
        span = high - low
        if span <= 0:
            return Signal(reason="범위 없음")
        price = candles[-1].close
        position_pct = (price - low) / span

        if ctx.position.side is PositionSide.LONG and position_pct >= 0.5:
            return Signal(action=SignalAction.EXIT, reason="박스 중간 복귀")
        if ctx.position.side is PositionSide.SHORT and position_pct <= 0.5:
            return Signal(action=SignalAction.EXIT, reason="박스 중간 복귀")
        if ctx.position.is_open:
            return Signal(reason=f"박스 내 위치 {position_pct:.0%}")

        width_pct = span / low * 100
        conviction = (
            Conviction.VERY_HIGH.value if width_pct >= 4
            else Conviction.HIGH.value if width_pct >= 2.5
            else Conviction.MEDIUM.value if width_pct >= 1.5
            else Conviction.LOW.value
        )
        buffer = atr_values[-1] * self.atr_buffer
        if position_pct <= self.edge:
            return Signal(action=SignalAction.ENTER_LONG, strength=conviction,
                          stop_loss=low - buffer,
                          reason=f"박스 바닥권 (위치 {position_pct:.0%}, 폭 {width_pct:.1f}%)")
        if position_pct >= 1 - self.edge:
            return Signal(action=SignalAction.ENTER_SHORT, strength=conviction,
                          stop_loss=high + buffer,
                          reason=f"박스 천장권 (위치 {position_pct:.0%}, 폭 {width_pct:.1f}%)")
        return Signal(reason=f"박스 중간 (위치 {position_pct:.0%})")
