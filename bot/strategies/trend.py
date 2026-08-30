"""추세추종 전략.

공통된 전제: **가격은 한 방향으로 움직이기 시작하면 한동안 그 방향을 유지한다.**
그래서 이 계열은 추세가 뚜렷할 때 크게 벌고, 방향 없이 오르내리는 횡보장에서는
신호가 계속 뒤집히며 수수료와 작은 손실을 반복해서 잃는다(톱니 손실).

승률은 대체로 50% 미만이다. 대신 이기는 거래 몇 번이 지는 거래 여러 번을
덮는 구조라, **손절을 짧게 유지하는 것이 생명이다.**
"""

from __future__ import annotations

from bot.indicators import atr, bollinger, donchian, ema, macd, supertrend
from bot.models import Conviction, PositionSide, Signal, SignalAction
from bot.strategies.base import Strategy, StrategyContext, register_strategy


def _atr_stop(candles, index, price, side, multiplier, fallback_pct=1.0):
    """ATR 로 손절가를 잡는다. 변동성이 클수록 손절을 넓게 둔다.

    고정 비율 손절은 조용한 장에서는 너무 넓고 요동치는 장에서는 너무 좁아서,
    같은 전략의 성적이 시장 상태에 따라 크게 흔들린다.
    """
    atr_values = atr(candles, 14)
    distance = (
        atr_values[index] * multiplier
        if index < len(atr_values) and atr_values[index]
        else price * (fallback_pct / 100.0)
    )
    return price - distance if side is PositionSide.LONG else price + distance


@register_strategy("ema_cross")
class EmaCrossStrategy(Strategy):
    summary = "빠른 이동평균이 느린 이동평균을 위로 뚫으면 매수"
    category = "trend"
    description = """
가장 고전적인 추세추종이다. 짧은 기간의 평균값(기본 20봉)이 긴 기간의 평균값
(기본 50봉)을 아래에서 위로 뚫으면 상승 추세가 시작됐다고 보고 매수하고,
반대로 뚫으면 청산한다.

두 평균선의 벌어진 정도로 확신을 나눈다 — 크게 벌어질수록 추세가 뚜렷하다는
뜻이므로 더 큰 금액을 건다.

**강점**: 큰 흐름을 놓치지 않는다. 규칙이 단순해서 왜 진입했는지 항상 설명된다.
**약점**: 횡보장에서 신호가 계속 뒤집혀 손실이 쌓인다. 평균선은 지나간 값이라
신호가 늦게 나온다 — 바닥에서 사고 꼭대기에서 팔 수는 없다.
"""
    algorithm = """
**지표**  EMA(fast=20), EMA(slow=50), ATR(14)

**진입**
- 롱: 직전 봉에서 `EMA20 ≤ EMA50` 이었는데 이번 봉에서 `EMA20 > EMA50` (상향 교차)
- 숏: 그 반대 (하향 교차)

**청산**  반대 방향 교차가 나오면 청산. 그 전까지는 유지.

**손절**  진입가 ∓ (ATR14 × 2.0)

**확신도**  두 선의 이격 `|EMA20 − EMA50| / EMA50 × 100`
- ≥ 1.5% → VERY_HIGH (200 USDT)
- ≥ 0.8% → HIGH (150)
- ≥ 0.3% → MEDIUM (100)
- 그 외 → LOW (50)

**파라미터**  `fast`, `slow`, `atr_multiplier`
"""

    def setup(self) -> None:
        self.fast = int(self.params.get("fast", 20))
        self.slow = int(self.params.get("slow", 50))
        self.atr_multiplier = float(self.params.get("atr_multiplier", 2.0))
        if self.fast >= self.slow:
            raise ValueError("fast 는 slow 보다 작아야 합니다")

    @property
    def warmup_candles(self) -> int:
        return self.slow + 20

    def generate(self, ctx: StrategyContext) -> Signal:
        candles = ctx.closed_candles
        if len(candles) < self.warmup_candles:
            return Signal(reason="워밍업 부족")

        closes = [c.close for c in candles]
        fast, slow = ema(closes, self.fast), ema(closes, self.slow)
        if fast[-1] is None or slow[-1] is None or fast[-2] is None or slow[-2] is None:
            return Signal(reason="지표 계산 불가")

        crossed_up = fast[-2] <= slow[-2] and fast[-1] > slow[-1]
        crossed_down = fast[-2] >= slow[-2] and fast[-1] < slow[-1]
        price = candles[-1].close
        gap_pct = abs(fast[-1] - slow[-1]) / slow[-1] * 100

        if ctx.position.side is PositionSide.LONG and crossed_down:
            return Signal(action=SignalAction.EXIT, reason="이동평균 하향 교차")
        if ctx.position.side is PositionSide.SHORT and crossed_up:
            return Signal(action=SignalAction.EXIT, reason="이동평균 상향 교차")
        if ctx.position.is_open:
            return Signal(reason="추세 유지 중")

        conviction = _gap_conviction(gap_pct)
        if crossed_up:
            return Signal(
                action=SignalAction.ENTER_LONG,
                strength=conviction,
                stop_loss=_atr_stop(candles, len(candles) - 1, price,
                                    PositionSide.LONG, self.atr_multiplier),
                reason=f"상향 교차 (이격 {gap_pct:.2f}%)",
            )
        if crossed_down:
            return Signal(
                action=SignalAction.ENTER_SHORT,
                strength=conviction,
                stop_loss=_atr_stop(candles, len(candles) - 1, price,
                                    PositionSide.SHORT, self.atr_multiplier),
                reason=f"하향 교차 (이격 {gap_pct:.2f}%)",
            )
        return Signal(reason="교차 없음")


def _gap_conviction(gap_pct: float) -> float:
    """이격이 클수록 추세가 뚜렷하다고 보고 확신을 올린다."""
    if gap_pct >= 1.5:
        return Conviction.VERY_HIGH.value
    if gap_pct >= 0.8:
        return Conviction.HIGH.value
    if gap_pct >= 0.3:
        return Conviction.MEDIUM.value
    return Conviction.LOW.value


@register_strategy("donchian_breakout")
class DonchianBreakoutStrategy(Strategy):
    summary = "최근 N봉의 최고가를 넘으면 매수 (터틀 트레이딩)"
    category = "trend"
    description = """
1980년대 '터틀 트레이딩'으로 알려진 방식이다. 최근 N봉(기본 20봉) 중 가장 높은
가격을 넘어서면 "새로운 영역에 진입했다"고 보고 매수한다. 청산은 더 짧은 기간
(기본 10봉)의 최저가를 깨면 한다.

돌파 폭이 클수록 확신을 올린다. 살짝 스친 돌파보다 확실히 뚫고 나간 쪽이
이어질 가능성이 높다는 판단이다.

**강점**: 큰 추세의 초입을 잡는다. 규칙에 재량이 전혀 없어 흔들리지 않는다.
**약점**: 가짜 돌파에 자주 물린다. 뚫자마자 되돌아오는 일이 흔하고, 그때마다
손절이 나간다. 승률이 30~40%대로 낮은 것이 정상이라 심리적으로 견디기 어렵다.
"""
    algorithm = """
**지표**  돈치안 채널(진입 20봉, 청산 10봉), ATR(14)
채널은 **직전 봉까지만** 본다 — 현재 봉을 포함하면 "최고가 돌파"가 항상 참이 된다.

**진입**
- 롱: 종가 > 직전 20봉 중 최고가
- 숏: 종가 < 직전 20봉 중 최저가

**청산**
- 롱: 종가 < 직전 10봉 중 최저가
- 숏: 종가 > 직전 10봉 중 최고가

**손절**  진입가 ∓ (ATR14 × 2.0)

**확신도**  돌파 폭 `(종가 − 채널고가) / 채널고가 × 100` 의 3배를 이격으로 환산해
EMA 교차와 같은 기준으로 나눈다.

**파라미터**  `entry_period`, `exit_period`, `atr_multiplier`
"""

    def setup(self) -> None:
        self.entry_period = int(self.params.get("entry_period", 20))
        self.exit_period = int(self.params.get("exit_period", 10))
        self.atr_multiplier = float(self.params.get("atr_multiplier", 2.0))

    @property
    def warmup_candles(self) -> int:
        return self.entry_period + 20

    def generate(self, ctx: StrategyContext) -> Signal:
        candles = ctx.closed_candles
        if len(candles) < self.warmup_candles:
            return Signal(reason="워밍업 부족")

        entry_high, entry_low = donchian(candles, self.entry_period)
        exit_high, exit_low = donchian(candles, self.exit_period)
        price = candles[-1].close
        if entry_high[-1] is None or exit_low[-1] is None:
            return Signal(reason="지표 계산 불가")

        if ctx.position.side is PositionSide.LONG and price < exit_low[-1]:
            return Signal(action=SignalAction.EXIT, reason=f"{self.exit_period}봉 최저가 이탈")
        if ctx.position.side is PositionSide.SHORT and price > exit_high[-1]:
            return Signal(action=SignalAction.EXIT, reason=f"{self.exit_period}봉 최고가 돌파")
        if ctx.position.is_open:
            return Signal(reason="채널 안 유지")

        if price > entry_high[-1]:
            margin = (price - entry_high[-1]) / entry_high[-1] * 100
            return Signal(
                action=SignalAction.ENTER_LONG,
                strength=_gap_conviction(margin * 3),
                stop_loss=_atr_stop(candles, len(candles) - 1, price,
                                    PositionSide.LONG, self.atr_multiplier),
                reason=f"{self.entry_period}봉 최고가 돌파 (+{margin:.2f}%)",
            )
        if price < entry_low[-1]:
            margin = (entry_low[-1] - price) / entry_low[-1] * 100
            return Signal(
                action=SignalAction.ENTER_SHORT,
                strength=_gap_conviction(margin * 3),
                stop_loss=_atr_stop(candles, len(candles) - 1, price,
                                    PositionSide.SHORT, self.atr_multiplier),
                reason=f"{self.entry_period}봉 최저가 이탈 (-{margin:.2f}%)",
            )
        return Signal(reason="채널 안")


@register_strategy("supertrend")
class SupertrendStrategy(Strategy):
    summary = "변동성으로 만든 추세선을 가격이 넘으면 방향 전환"
    category = "trend"
    description = """
ATR(평균 변동폭)로 가격 위아래에 밴드를 만들고, 가격이 그 밴드를 넘을 때만
방향을 바꾼다. 밴드는 추세 방향으로만 조여지기 때문에 **추적 손절처럼** 동작한다 —
오르는 동안 밑에서 따라 올라오다가, 가격이 밴드를 깨면 청산한다.

변동성에 맞춰 밴드 폭이 자동으로 조절되는 것이 핵심이다. 조용한 장에서는 좁게,
요동치는 장에서는 넓게 잡혀서 잔파동에 덜 흔들린다.

**강점**: 추세를 오래 끌고 갈 수 있다. 손절 위치가 자동으로 따라온다.
**약점**: 급반전에 취약하다. 방향이 바뀌는 순간은 이미 꽤 밀린 뒤다.
"""
    algorithm = """
**지표**  슈퍼트렌드(period=10, multiplier=3.0) — ATR 기반

**계산**  각 봉에서 중간값 `(고가+저가)/2` 위아래로 `ATR × 3` 만큼 밴드를 만든다.
밴드는 추세 방향으로만 조여진다(상승 중에는 하단 밴드가 올라가기만 함).
종가가 반대편 밴드를 넘으면 방향이 뒤집힌다.

**진입**  방향이 막 뒤집힌 봉에서 그 방향으로 진입.

**청산**  방향이 다시 뒤집히면 청산.

**손절**  슈퍼트렌드 선 자체를 손절가로 쓴다. 추세가 이어지는 동안 선이 따라
올라오므로 **추적 손절**처럼 동작한다.

**확신도**  현재가에서 추세선까지의 거리(= 손절 폭)
- < 0.5% → VERY_HIGH · < 1.0% → HIGH · < 2.0% → MEDIUM · 그 외 LOW

가까울수록 손실 한도가 작으니 크게 건다.

**파라미터**  `period`, `multiplier`
"""

    def setup(self) -> None:
        self.period = int(self.params.get("period", 10))
        self.multiplier = float(self.params.get("multiplier", 3.0))

    @property
    def warmup_candles(self) -> int:
        return self.period + 30

    def generate(self, ctx: StrategyContext) -> Signal:
        candles = ctx.closed_candles
        if len(candles) < self.warmup_candles:
            return Signal(reason="워밍업 부족")

        line, trend = supertrend(candles, self.period, self.multiplier)
        if trend[-1] is None or trend[-2] is None or line[-1] is None:
            return Signal(reason="지표 계산 불가")

        flipped = trend[-1] != trend[-2]
        price = candles[-1].close
        # 추세선까지의 거리가 곧 손절 폭이다 — 가까울수록 손실이 작으니 확신을 올린다.
        distance_pct = abs(price - line[-1]) / price * 100
        conviction = (
            Conviction.VERY_HIGH.value if distance_pct < 0.5
            else Conviction.HIGH.value if distance_pct < 1.0
            else Conviction.MEDIUM.value if distance_pct < 2.0
            else Conviction.LOW.value
        )

        if ctx.position.is_open:
            wrong_way = (
                (ctx.position.side is PositionSide.LONG and trend[-1] == -1)
                or (ctx.position.side is PositionSide.SHORT and trend[-1] == 1)
            )
            return Signal(
                action=SignalAction.EXIT if wrong_way else SignalAction.HOLD,
                reason="추세 반전" if wrong_way else "추세 유지",
            )

        if flipped and trend[-1] == 1:
            return Signal(action=SignalAction.ENTER_LONG, strength=conviction,
                          stop_loss=line[-1], reason="상승 추세 전환")
        if flipped and trend[-1] == -1:
            return Signal(action=SignalAction.ENTER_SHORT, strength=conviction,
                          stop_loss=line[-1], reason="하락 추세 전환")
        return Signal(reason="추세 전환 없음")


@register_strategy("macd_trend")
class MacdTrendStrategy(Strategy):
    summary = "MACD 히스토그램이 0선을 넘으면 진입"
    category = "trend"
    description = """
MACD 는 두 지수이동평균의 차이(MACD 선)와 그것의 평활값(시그널선)을 비교한다.
둘의 차이인 히스토그램이 0 위로 올라오면 상승 모멘텀이 붙었다고 보고 매수한다.

이동평균 교차와 비슷하지만 **모멘텀의 가속을 본다**는 점이 다르다. 가격이 오르고
있어도 오르는 속도가 줄면 히스토그램이 먼저 꺾여서, 교차보다 조금 이르게 신호가
나온다.

**강점**: 추세의 강약을 함께 본다. 널리 쓰여 검증된 지표다.
**약점**: 여전히 후행 지표다. 횡보장에서는 히스토그램이 0 근처를 계속 넘나들며
잦은 가짜 신호를 만든다.
"""
    algorithm = """
**지표**  MACD(12, 26, 9), ATR(14)
- MACD선 = EMA12 − EMA26
- 시그널선 = MACD선의 EMA9
- 히스토그램 = MACD선 − 시그널선

**진입**
- 롱: 히스토그램이 0 이하 → 0 초과로 전환
- 숏: 0 이상 → 0 미만으로 전환

**청산**  히스토그램이 반대로 전환되면 청산.

**손절**  진입가 ∓ (ATR14 × 2.0)

**확신도**  `|히스토그램| / 현재가 × 100` 의 10배를 이격으로 환산해 나눈다.

**파라미터**  `fast`, `slow`, `signal`, `atr_multiplier`
"""

    def setup(self) -> None:
        self.fast = int(self.params.get("fast", 12))
        self.slow = int(self.params.get("slow", 26))
        self.signal_period = int(self.params.get("signal", 9))
        self.atr_multiplier = float(self.params.get("atr_multiplier", 2.0))

    @property
    def warmup_candles(self) -> int:
        return self.slow + self.signal_period + 20

    def generate(self, ctx: StrategyContext) -> Signal:
        candles = ctx.closed_candles
        if len(candles) < self.warmup_candles:
            return Signal(reason="워밍업 부족")

        closes = [c.close for c in candles]
        _, _, histogram = macd(closes, self.fast, self.slow, self.signal_period)
        if histogram[-1] is None or histogram[-2] is None:
            return Signal(reason="지표 계산 불가")

        crossed_up = histogram[-2] <= 0 < histogram[-1]
        crossed_down = histogram[-2] >= 0 > histogram[-1]
        price = candles[-1].close
        strength_pct = abs(histogram[-1]) / price * 100

        if ctx.position.side is PositionSide.LONG and crossed_down:
            return Signal(action=SignalAction.EXIT, reason="모멘텀 하락 전환")
        if ctx.position.side is PositionSide.SHORT and crossed_up:
            return Signal(action=SignalAction.EXIT, reason="모멘텀 상승 전환")
        if ctx.position.is_open:
            return Signal(reason="모멘텀 유지")

        conviction = _gap_conviction(strength_pct * 10)
        if crossed_up:
            return Signal(action=SignalAction.ENTER_LONG, strength=conviction,
                          stop_loss=_atr_stop(candles, len(candles) - 1, price,
                                              PositionSide.LONG, self.atr_multiplier),
                          reason="MACD 히스토그램 상향 전환")
        if crossed_down:
            return Signal(action=SignalAction.ENTER_SHORT, strength=conviction,
                          stop_loss=_atr_stop(candles, len(candles) - 1, price,
                                              PositionSide.SHORT, self.atr_multiplier),
                          reason="MACD 히스토그램 하향 전환")
        return Signal(reason="전환 없음")


@register_strategy("bollinger_breakout")
class BollingerBreakoutStrategy(Strategy):
    summary = "볼린저밴드 상단을 뚫으면 추세 시작으로 보고 매수"
    category = "trend"
    description = """
볼린저밴드는 이동평균 위아래로 표준편차만큼 떨어진 선이다. 가격은 보통 이 안에서
움직이므로 **밴드 밖으로 나간다는 것은 평소와 다른 일이 벌어졌다는 뜻**이다.
이 전략은 그것을 추세의 시작으로 해석해 그 방향으로 따라간다.

같은 지표를 쓰는 `bollinger_reversion` 과 **정반대 논리**다. 그쪽은 밴드를 벗어난
가격이 평균으로 돌아온다고 보고 역방향으로 진입한다. 둘을 함께 백테스트하면 이
시장이 추세형인지 횡보형인지 판단할 수 있다.

**강점**: 변동성이 터지는 순간을 잡는다. 큰 움직임의 초반에 올라탄다.
**약점**: 밴드를 살짝 넘었다가 되돌아오는 경우가 많다. 횡보장에서는 이 전략이
계속 지고 `bollinger_reversion` 이 이긴다.
"""
    algorithm = """
**지표**  볼린저밴드(20봉, ±2σ), ATR(14)

**진입**
- 롱: 종가 > 상단 밴드
- 숏: 종가 < 하단 밴드

**청산**  가격이 중심선(SMA20)으로 돌아오면 청산.

**손절**  진입가 ∓ (ATR14 × 2.0)

**확신도**  밴드 폭 `(상단−하단)/중심 × 100`
- < 1.0% → VERY_HIGH · < 2.0% → HIGH · < 4.0% → MEDIUM · 그 외 LOW

밴드가 **좁을 때의 돌파**가 더 의미 있다 — 변동성 수축 뒤의 확장이기 때문이다.

**파라미터**  `period`, `deviations`, `atr_multiplier`
"""

    def setup(self) -> None:
        self.period = int(self.params.get("period", 20))
        self.deviations = float(self.params.get("deviations", 2.0))
        self.atr_multiplier = float(self.params.get("atr_multiplier", 2.0))

    @property
    def warmup_candles(self) -> int:
        return self.period + 20

    def generate(self, ctx: StrategyContext) -> Signal:
        candles = ctx.closed_candles
        if len(candles) < self.warmup_candles:
            return Signal(reason="워밍업 부족")

        closes = [c.close for c in candles]
        upper, middle, lower = bollinger(closes, self.period, self.deviations)
        if upper[-1] is None or middle[-1] is None or lower[-1] is None:
            return Signal(reason="지표 계산 불가")

        price = closes[-1]
        if ctx.position.is_open:
            back_inside = (
                (ctx.position.side is PositionSide.LONG and price < middle[-1])
                or (ctx.position.side is PositionSide.SHORT and price > middle[-1])
            )
            return Signal(
                action=SignalAction.EXIT if back_inside else SignalAction.HOLD,
                reason="중심선 복귀" if back_inside else "밴드 밖 유지",
            )

        # 밴드 폭이 좁을 때의 돌파가 더 의미 있다 — 변동성 수축 뒤의 확장이다.
        width_pct = (upper[-1] - lower[-1]) / middle[-1] * 100
        conviction = (
            Conviction.VERY_HIGH.value if width_pct < 1.0
            else Conviction.HIGH.value if width_pct < 2.0
            else Conviction.MEDIUM.value if width_pct < 4.0
            else Conviction.LOW.value
        )
        if price > upper[-1]:
            return Signal(action=SignalAction.ENTER_LONG, strength=conviction,
                          stop_loss=_atr_stop(candles, len(candles) - 1, price,
                                              PositionSide.LONG, self.atr_multiplier),
                          reason=f"상단 돌파 (밴드폭 {width_pct:.2f}%)")
        if price < lower[-1]:
            return Signal(action=SignalAction.ENTER_SHORT, strength=conviction,
                          stop_loss=_atr_stop(candles, len(candles) - 1, price,
                                              PositionSide.SHORT, self.atr_multiplier),
                          reason=f"하단 이탈 (밴드폭 {width_pct:.2f}%)")
        return Signal(reason="밴드 안")
