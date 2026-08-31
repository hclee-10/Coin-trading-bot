"""시장 구조 전략.

지표 없이(혹은 보조로만 쓰고) **가격이 남긴 구조** — 스윙 고점/저점, 되돌림
비율, 전일의 기준 가격대 — 를 직접 읽는다. 재량 트레이더들이 차트에 손으로
긋는 것들을 규칙으로 옮긴 계열이다.

스윙 판정에는 프랙탈 피봇을 쓴다: 좌우 몇 봉보다 높은(낮은) 봉만 스윙으로
친다. 오른쪽 봉들이 확정되어야 피봇이 성립하므로 **판정이 몇 봉 늦는 대신
미래 정보를 쓰지 않는다** — 백테스트가 거짓말하지 않게 하는 대가다.
"""

from __future__ import annotations

from bot.indicators import atr, ema, pivot_highs, pivot_lows, rsi
from bot.models import Conviction, PositionSide, Signal, SignalAction
from bot.strategies.base import Strategy, StrategyContext, register_strategy
from bot.strategies.trend import _atr_stop


@register_strategy("swing_break")
class SwingBreakStrategy(Strategy):
    summary = "저점이 높아지는 구조에서 직전 스윙 고점을 넘으면 매수"
    category = "trend"
    description = """
"고점과 저점이 함께 높아지면 상승 추세"라는 다우 이론의 정의를 그대로 규칙으로
만든 전략이다. 프랙탈 피봇으로 스윙 고점/저점을 찾고, **최근 두 스윙 저점이
높아지는 중**(상승 구조)일 때 **직전 스윙 고점을 종가가 넘으면** 매수한다.
구조와 돌파가 둘 다 있어야 한다 — 저점이 낮아지는 중의 고점 돌파는 받지 않는다.

이동평균 없이 구조만 보므로, 평균이 왜곡되는 급변 구간에서도 판정이 담백하다.
손절은 마지막 스윙 저점 바로 아래다. 그 저점이 깨지면 "저점이 높아진다"는 진입
근거 자체가 무너진 것이므로, 진입과 손절의 논리가 한 몸이다.

확신도는 구조의 기울기 — 두 저점의 상승 폭을 ATR 대비로 잰다. 가파르게 높아진
저점 구조에서의 돌파일수록 크게 건다.

**강점**: 근거가 차트에 눈으로 보인다. 손절 위치가 구조적이다.
**약점**: 피봇 확정 지연(우측 봉 대기) 때문에 신호가 몇 봉 늦는다. 스윙이
빽빽한 횡보장에서는 구조 판정이 자주 뒤집힌다.
"""
    algorithm = """
**지표**  프랙탈 피봇(좌우 2봉), ATR(14)

**구조 판정**
- 상승 구조: 최근 두 스윙 저점이 높아짐 (L2 > L1)
- 하락 구조: 최근 두 스윙 고점이 낮아짐

**진입**
- 롱: 상승 구조 그리고 종가가 직전 스윙 고점을 이번 봉에 상향 돌파
- 숏: 하락 구조 그리고 종가가 직전 스윙 저점을 이번 봉에 하향 이탈

**청산**  마지막 스윙 저점(롱)/고점(숏)을 종가가 반대로 넘으면 청산.

**손절**  마지막 스윙 저점 − ATR × 0.25 (롱) — 구조의 부정 지점.

**확신도**  두 저점의 상승 폭 ÷ ATR — ≥1.5 VERY_HIGH · ≥1.0 HIGH · ≥0.5 MEDIUM · LOW

**파라미터**  `strength`(피봇 좌우 봉 수), `atr_multiplier`
"""

    def setup(self) -> None:
        self.strength = int(self.params.get("strength", 2))
        self.atr_multiplier = float(self.params.get("atr_multiplier", 2.0))

    @property
    def warmup_candles(self) -> int:
        return 60

    def generate(self, ctx: StrategyContext) -> Signal:
        candles = ctx.closed_candles
        if len(candles) < self.warmup_candles:
            return Signal(reason="워밍업 부족")

        highs = pivot_highs(candles, self.strength)
        lows = pivot_lows(candles, self.strength)
        atr_values = atr(candles, 14)
        if len(highs) < 2 or len(lows) < 2 or atr_values[-1] is None:
            return Signal(reason="스윙 부족")

        last_high = candles[highs[-1]].high
        last_low = candles[lows[-1]].low
        price, previous = candles[-1].close, candles[-2].close

        if ctx.position.side is PositionSide.LONG and price < last_low:
            return Signal(action=SignalAction.EXIT, reason="스윙 저점 이탈")
        if ctx.position.side is PositionSide.SHORT and price > last_high:
            return Signal(action=SignalAction.EXIT, reason="스윙 고점 돌파")
        if ctx.position.is_open:
            return Signal(reason="구조 유지 중")

        rising_lows = candles[lows[-1]].low > candles[lows[-2]].low
        falling_highs = candles[highs[-1]].high < candles[highs[-2]].high
        broke_high = previous <= last_high < price
        broke_low = previous >= last_low > price

        if rising_lows and broke_high:
            slope = (candles[lows[-1]].low - candles[lows[-2]].low) / atr_values[-1]
            return Signal(
                action=SignalAction.ENTER_LONG,
                strength=_slope_conviction(slope),
                stop_loss=last_low - atr_values[-1] * 0.25,
                reason=f"상승 구조 + 스윙 고점 돌파 (저점 상승 {slope:.1f} ATR)",
            )
        if falling_highs and broke_low:
            slope = (candles[highs[-2]].high - candles[highs[-1]].high) / atr_values[-1]
            return Signal(
                action=SignalAction.ENTER_SHORT,
                strength=_slope_conviction(slope),
                stop_loss=last_high + atr_values[-1] * 0.25,
                reason=f"하락 구조 + 스윙 저점 이탈 (고점 하락 {slope:.1f} ATR)",
            )
        if broke_high or broke_low:
            return Signal(reason="돌파했으나 구조 불일치")
        return Signal(reason="구조 대기")


def _slope_conviction(slope_atr: float) -> float:
    if slope_atr >= 1.5:
        return Conviction.VERY_HIGH.value
    if slope_atr >= 1.0:
        return Conviction.HIGH.value
    if slope_atr >= 0.5:
        return Conviction.MEDIUM.value
    return Conviction.LOW.value


@register_strategy("fib_pullback")
class FibPullbackStrategy(Strategy):
    summary = "직전 상승 파동의 38~62% 되돌림 지대에서 돌아서면 매수"
    category = "combo"
    description = """
추세는 직선이 아니라 파동으로 간다 — 밀고, 일부 되돌리고, 다시 민다. 이 전략은
"일부 되돌리는" 지점을 피보나치 비율로 잰다. 최근 파동(스윙 저점 → 스윙 고점)을
잡고, 가격이 그 파동의 **38.2%~61.8% 를 되돌린 지대**에 들어왔다가 다시 파동
방향으로 돌아서는 봉에서 진입한다.

왜 이 비율인가에 신비는 없다 — 얕은 되돌림(38% 미만)은 아직 눌림이 덜 익었고,
깊은 되돌림(62% 초과)은 되돌림이 아니라 반전일 가능성이 커진다. 그 사이가
"추세는 살아 있는데 진입가는 싸진" 구간이라는 경험칙이고, 많은 트레이더가 이
지대를 보고 있다는 사실 자체가 지대를 실재하게 만든다.

방향 필터로 EMA(50) 을 함께 본다 — 파동과 EMA 방향이 같을 때만 거래한다.
손절은 61.8% 지대 바닥 아래: 거기까지 밀리면 되돌림이 아니었던 것이다.

**강점**: 진입가가 유리하고 손절 근거(지대 이탈)가 명확하다.
**약점**: 파동 선정이 스윙 판정에 달려 있어, 스윙이 애매한 장에서는 지대 자체가
애매하다. 강한 추세는 38% 도 안 눌리고 가버린다.
"""
    algorithm = """
**지표**  프랙탈 피봇(좌우 2봉)으로 최근 파동, EMA(50), ATR(14)

**파동**  마지막 스윙 저점 → 그 뒤의 마지막 스윙 고점 (롱 기준. 숏은 반대)

**되돌림 지대**  고점 − (고점−저점) × 0.382 ~ 0.618

**진입**
- 롱: 종가 > EMA50 방향 일치, 직전 종가가 지대 안, 이번 종가가 직전 종가보다
  상승 (지대에서 돌아섬)
- 숏: 거울상

**청산**  파동 고점(롱)을 종가가 넘으면(파동 연장 확인) 혹은 EMA50 반대 이탈 시 청산.

**손절**  61.8% 레벨 − ATR × 0.25 — 지대가 깨지면 되돌림이 아니었던 것.

**확신도**  되돌림 깊이가 50% 에 가까울수록 + EMA 와의 거리
- 지대 중심부(45~55%)면 한 단계 올린다. 기본은 파동 크기 ÷ ATR 로 등급.

**파라미터**  `strength`, `trend_period`, `shallow`(0.382), `deep`(0.618)
"""

    def setup(self) -> None:
        self.strength = int(self.params.get("strength", 2))
        self.trend_period = int(self.params.get("trend_period", 50))
        self.shallow = float(self.params.get("shallow", 0.382))
        self.deep = float(self.params.get("deep", 0.618))

    @property
    def warmup_candles(self) -> int:
        return self.trend_period + 20

    def generate(self, ctx: StrategyContext) -> Signal:
        candles = ctx.closed_candles
        if len(candles) < self.warmup_candles:
            return Signal(reason="워밍업 부족")

        closes = [c.close for c in candles]
        trend = ema(closes, self.trend_period)
        atr_values = atr(candles, 14)
        highs = pivot_highs(candles, self.strength)
        lows = pivot_lows(candles, self.strength)
        if trend[-1] is None or atr_values[-1] is None or not highs or not lows:
            return Signal(reason="스윙 부족")

        price, previous = closes[-1], closes[-2]

        if ctx.position.side is PositionSide.LONG:
            if price < trend[-1]:
                return Signal(action=SignalAction.EXIT, reason="EMA50 이탈")
            wave_high = candles[highs[-1]].high
            if price > wave_high:
                return Signal(action=SignalAction.EXIT, reason="파동 고점 도달")
            return Signal(reason="파동 연장 대기")
        if ctx.position.side is PositionSide.SHORT:
            if price > trend[-1]:
                return Signal(action=SignalAction.EXIT, reason="EMA50 이탈")
            wave_low = candles[lows[-1]].low
            if price < wave_low:
                return Signal(action=SignalAction.EXIT, reason="파동 저점 도달")
            return Signal(reason="파동 연장 대기")

        # 롱: 저점 → 그 이후의 고점으로 만든 상승 파동
        if price > trend[-1] and highs[-1] > lows[-1]:
            low = candles[lows[-1]].low
            high = candles[highs[-1]].high
            wave = high - low
            if wave > 0:
                zone_top = high - wave * self.shallow
                zone_bottom = high - wave * self.deep
                if zone_bottom <= previous <= zone_top and price > previous:
                    depth = (high - previous) / wave
                    return Signal(
                        action=SignalAction.ENTER_LONG,
                        strength=_fib_conviction(wave, atr_values[-1], depth),
                        stop_loss=zone_bottom - atr_values[-1] * 0.25,
                        reason=f"되돌림 {depth:.0%} 지대 반등",
                    )
        if price < trend[-1] and lows[-1] > highs[-1]:
            high = candles[highs[-1]].high
            low = candles[lows[-1]].low
            wave = high - low
            if wave > 0:
                zone_bottom = low + wave * self.shallow
                zone_top = low + wave * self.deep
                if zone_bottom <= previous <= zone_top and price < previous:
                    depth = (previous - low) / wave
                    return Signal(
                        action=SignalAction.ENTER_SHORT,
                        strength=_fib_conviction(wave, atr_values[-1], depth),
                        stop_loss=zone_top + atr_values[-1] * 0.25,
                        reason=f"반등 {depth:.0%} 지대 꺾임",
                    )
        return Signal(reason="지대 밖")


def _fib_conviction(wave: float, atr_value: float, depth: float) -> float:
    """파동이 클수록, 되돌림이 지대 중심(50%)에 가까울수록 확신을 올린다."""
    base = (
        2 if wave >= atr_value * 8
        else 1 if wave >= atr_value * 5
        else 0
    )
    if 0.45 <= depth <= 0.55:
        base += 1
    levels = [Conviction.LOW, Conviction.MEDIUM, Conviction.HIGH, Conviction.VERY_HIGH]
    return levels[min(base, 3)].value


@register_strategy("rsi_divergence")
class RsiDivergenceStrategy(Strategy):
    summary = "가격은 저점을 낮췄는데 RSI는 높였으면(다이버전스) 매수"
    category = "reversion"
    description = """
다이버전스는 가격과 모멘텀이 서로 다른 말을 하는 순간이다. 가격이 직전 저점보다
**더 낮은 저점**을 만들었는데 그 자리의 RSI 는 직전보다 **높다**면, 하락의
힘이 빠지고 있다는 뜻이다 — 신저가를 만들 만큼 팔긴 했는데 그 강도가 예전만
못한 것이다. 평균회귀 신호 중에서 가장 "이르게" 나오는 축에 속한다.

구현은 프랙탈 피봇으로 한다: 최근 두 스윙 저점에서 가격은 낮아지고 RSI 는
높아졌으면 강세 다이버전스, 두 스윙 고점에서 가격은 높아지고 RSI 는 낮아졌으면
약세 다이버전스다. 진입은 다이버전스가 확정된 직후(두 번째 피봇이 확정되는
봉 근처)에만 받고, 오래된 다이버전스는 버린다.

확신도는 RSI 격차와 두 번째 저점의 RSI 절대 수준으로 잰다 — 과매도권에서 나온
큰 격차의 다이버전스가 최상급이다.

**강점**: 반전을 지표 교차보다 일찍 잡는다. 손절(신저가 이탈)이 명확하다.
**약점**: 다이버전스는 추세 중에 여러 번 "미리" 나올 수 있다 — 하락이 강하면
다이버전스가 3~4번 실패한 뒤에야 진짜 바닥이 온다. 손절 준수가 생명이다.
"""
    algorithm = """
**지표**  RSI(14), 프랙탈 피봇(좌우 3봉), ATR(14)

**다이버전스** (강세 기준)
- 최근 두 스윙 저점: 가격 저점은 낮아짐, 같은 자리의 RSI 는 높아짐
- 두 번째 저점의 RSI < 50 (하락 맥락에서만)
- 두 번째 피봇이 확정된 지 5봉 이내 (신선한 것만)

**진입**  다이버전스 확인 봉에서. 약세는 거울상.

**청산**  RSI 가 중립(50)을 회복하면 청산.

**손절**  두 번째 저점 − ATR × 0.25 — 신저가가 또 나오면 다이버전스 무효.

**확신도**  RSI 격차(두 저점의 RSI 차) + 절대 수준
- 격차 ≥ 8 이고 RSI ≤ 35 → VERY_HIGH · 격차 ≥ 8 또는 RSI ≤ 35 → HIGH
- 격차 ≥ 4 → MEDIUM · 그 외 LOW

**파라미터**  `rsi_period`, `strength`, `freshness`(기본 5봉)
"""

    def setup(self) -> None:
        self.rsi_period = int(self.params.get("rsi_period", 14))
        self.strength = int(self.params.get("strength", 3))
        self.freshness = int(self.params.get("freshness", 5))

    @property
    def warmup_candles(self) -> int:
        return 70

    def generate(self, ctx: StrategyContext) -> Signal:
        candles = ctx.closed_candles
        if len(candles) < self.warmup_candles:
            return Signal(reason="워밍업 부족")

        momentum = rsi([c.close for c in candles], self.rsi_period)
        atr_values = atr(candles, 14)
        if momentum[-1] is None or atr_values[-1] is None:
            return Signal(reason="지표 계산 불가")

        price = candles[-1].close
        if ctx.position.side is PositionSide.LONG and momentum[-1] >= 50:
            return Signal(action=SignalAction.EXIT, reason="RSI 중립 회복")
        if ctx.position.side is PositionSide.SHORT and momentum[-1] <= 50:
            return Signal(action=SignalAction.EXIT, reason="RSI 중립 회복")
        if ctx.position.is_open:
            return Signal(reason="반전 진행 대기")

        lows = pivot_lows(candles, self.strength)
        highs = pivot_highs(candles, self.strength)

        if len(lows) >= 2 and momentum[lows[-1]] is not None and momentum[lows[-2]] is not None:
            fresh = len(candles) - 1 - lows[-1] <= self.strength + self.freshness
            price_lower = candles[lows[-1]].low < candles[lows[-2]].low
            rsi_higher = momentum[lows[-1]] > momentum[lows[-2]]
            in_context = momentum[lows[-1]] < 50
            if fresh and price_lower and rsi_higher and in_context:
                gap = momentum[lows[-1]] - momentum[lows[-2]]
                return Signal(
                    action=SignalAction.ENTER_LONG,
                    strength=_divergence_conviction(gap, momentum[lows[-1]], oversold=True),
                    stop_loss=candles[lows[-1]].low - atr_values[-1] * 0.25,
                    reason=f"강세 다이버전스 (RSI 격차 +{gap:.0f})",
                )
        if len(highs) >= 2 and momentum[highs[-1]] is not None and momentum[highs[-2]] is not None:
            fresh = len(candles) - 1 - highs[-1] <= self.strength + self.freshness
            price_higher = candles[highs[-1]].high > candles[highs[-2]].high
            rsi_lower = momentum[highs[-1]] < momentum[highs[-2]]
            in_context = momentum[highs[-1]] > 50
            if fresh and price_higher and rsi_lower and in_context:
                gap = momentum[highs[-2]] - momentum[highs[-1]]
                return Signal(
                    action=SignalAction.ENTER_SHORT,
                    strength=_divergence_conviction(gap, momentum[highs[-1]], oversold=False),
                    stop_loss=candles[highs[-1]].high + atr_values[-1] * 0.25,
                    reason=f"약세 다이버전스 (RSI 격차 -{gap:.0f})",
                )
        return Signal(reason="다이버전스 없음")


def _divergence_conviction(gap: float, level: float, *, oversold: bool) -> float:
    extreme = level <= 35 if oversold else level >= 65
    if gap >= 8 and extreme:
        return Conviction.VERY_HIGH.value
    if gap >= 8 or extreme:
        return Conviction.HIGH.value
    if gap >= 4:
        return Conviction.MEDIUM.value
    return Conviction.LOW.value


@register_strategy("pivot_bounce")
class PivotBounceStrategy(Strategy):
    summary = "전일 가격으로 만든 피봇 지지선(S1)에서 튕기면 매수"
    category = "range"
    extra_timeframes = ("1d",)
    description = """
피봇 포인트는 **전일의 고가·저가·종가**로 오늘의 기준선들을 미리 계산해 두는
방식이다: 중심 피봇 P = (고+저+종)/3, 지지 S1 = 2P − 전일 고가, 저항
R1 = 2P − 전일 저가. 장중 트레이더들이 수십 년째 쓰는 고전이라, 많은 눈이 같은
선을 보고 있다는 것 자체가 이 선들을 실재하는 지지/저항으로 만든다.

이 전략은 레인지 관점으로 쓴다 — 가격이 S1 아래로 밀렸다가 **다시 S1 위로
복귀하면**(지지 확인) 매수하고 중심 피봇 P 를 목표로 한다. R1 에서 튕겨 내려오면
매도하고 역시 P 가 목표다. 전일 범위 안에서 오늘이 도는 날에 먹는 구조다.

전일 기준값은 엔진이 공급하는 일봉 캔들에서 얻는다(다중 시간대 기능). 확신도는
전일 범위의 크기 — 넓은 범위의 S1/R1 일수록 의미 있는 레벨로 친다.

**강점**: 레벨이 하루 동안 고정이라 신호가 안정적이다. 목표(P)가 내장돼 있다.
**약점**: 추세일이면 S1, S2 가 차례로 깨진다. 전일 범위가 좁으면 레벨들이
다닥다닥 붙어 의미가 없다.
"""
    algorithm = """
**지표**  마지막 확정 일봉의 고가 H, 저가 L, 종가 C (엔진 공급 일봉 또는 리샘플)
- P = (H+L+C)/3, S1 = 2P − H, R1 = 2P − L

**진입**
- 롱: 직전 종가 < S1 이고 이번 종가 ≥ S1 (지지 복귀)
- 숏: 직전 종가 > R1 이고 이번 종가 ≤ R1 (저항 확인)

**청산**  중심 피봇 P 도달 시 청산.

**손절**  S1 − (전일 범위 × 0.25) (롱) — S2 방향으로 밀리면 레인지 가정 폐기.

**확신도**  전일 범위 ÷ ATR14×24 상당 (범위가 평소 대비 넓을수록)
- ≥ 1.5배 → VERY_HIGH · ≥ 1.0배 → HIGH · ≥ 0.6배 → MEDIUM · 그 외 LOW

**파라미터**  없음 (피봇 공식 고정)
"""

    @property
    def warmup_candles(self) -> int:
        return 40

    def generate(self, ctx: StrategyContext) -> Signal:
        candles = ctx.closed_candles
        if len(candles) < self.warmup_candles:
            return Signal(reason="워밍업 부족")

        daily = ctx.closed_candles_for("1d")
        if not daily:
            return Signal(reason="일봉 데이터 부족")
        reference = daily[-1]
        pivot = (reference.high + reference.low + reference.close) / 3
        s1 = 2 * pivot - reference.high
        r1 = 2 * pivot - reference.low
        day_range = reference.high - reference.low
        if day_range <= 0:
            return Signal(reason="전일 범위 없음")

        price, previous = candles[-1].close, candles[-2].close

        if ctx.position.side is PositionSide.LONG and price >= pivot:
            return Signal(action=SignalAction.EXIT, reason="중심 피봇 도달")
        if ctx.position.side is PositionSide.SHORT and price <= pivot:
            return Signal(action=SignalAction.EXIT, reason="중심 피봇 도달")
        if ctx.position.is_open:
            return Signal(reason="피봇 복귀 대기")

        atr_values = atr(candles, 14)
        typical_day = (atr_values[-1] or 0.0) * 24
        ratio = day_range / typical_day if typical_day > 0 else 0.0
        conviction = (
            Conviction.VERY_HIGH.value if ratio >= 1.5
            else Conviction.HIGH.value if ratio >= 1.0
            else Conviction.MEDIUM.value if ratio >= 0.6
            else Conviction.LOW.value
        )

        if previous < s1 <= price:
            return Signal(action=SignalAction.ENTER_LONG, strength=conviction,
                          stop_loss=s1 - day_range * 0.25,
                          take_profit=pivot,   # 중심 피봇이 목표다
                          reason=f"S1 지지 복귀 (목표 P {pivot:.2f})")
        if previous > r1 >= price:
            return Signal(action=SignalAction.ENTER_SHORT, strength=conviction,
                          stop_loss=r1 + day_range * 0.25,
                          take_profit=pivot,
                          reason=f"R1 저항 확인 (목표 P {pivot:.2f})")
        return Signal(reason="피봇 레벨 사이")


@register_strategy("double_bottom")
class DoubleBottomStrategy(Strategy):
    summary = "쌍바닥(W) 넥라인 돌파에서 매수, 패턴 높이만큼을 목표로"
    category = "reversion"
    description = """
쌍바닥은 가장 유명한 반전 패턴이다 — 같은 가격대에서 두 번 바닥을 찍으면(첫
바닥의 저점이 두 번째에도 지켜지면) 그 가격대에 실제 매수 수요가 있다는 것이
두 번 검증된 셈이다. 진입은 바닥 확인이 아니라 **넥라인(두 바닥 사이의 반등
고점) 돌파**에서 한다 — 돌파 전의 쌍바닥은 아직 그냥 횡보다.

이 패턴의 매력은 **목표가가 패턴 안에 정의**된다는 것이다: 고전 규칙대로
넥라인에서 패턴 높이(넥라인 − 바닥)만큼 위를 목표로 잡고, 그 가격을 익절가로
건다. 손절은 두 번째 바닥 아래 — 바닥이 깨지면 패턴 자체가 무효다. 목표와
손절이 모두 구조에서 나오므로 손익비가 진입 전에 계산된다.

두 번째 바닥은 첫 바닥과 정확히 같은 레벨일 필요는 없다 — 첫 반등을 일부
되돌리며 첫 바닥 위에서 버텼으면 인정하되, 되돌림이 깊을수록(같은 레벨에
가까울수록) 확신을 올린다. 두 번째 바닥이 첫 바닥보다 높은
'higher-low 쌍바닥'은 수요가 더 위에서 받아졌다는 뜻이라 오히려 강세 변형으로
친다. 쌍봉(M)은 전체가 거울상으로 숏이다.

**강점**: 목표·손절·손익비가 진입 전에 확정된다. 패턴 근거(수요 이중 검증)가
직관적이다.
**약점**: 넥라인 돌파를 기다리는 동안 되돌림의 상당 부분이 지나간다. 횡보장의
모든 굴곡이 쌍바닥처럼 보이는 착시가 있다 — 프랙탈 확정 지연이 그 일부를 걸러
줄 뿐이다.
"""
    algorithm = """
**지표**  프랙탈 피봇(좌우 3봉), ATR(14)

**패턴** (쌍바닥, 롱 기준 — 쌍봉은 거울상)
- 첫 바닥: 그 앞 최대 5개 스윙 저점 중 60봉 이내의 것. 넥라인 = 두 저점 사이
  최고가, 높이 = 넥라인 − 첫 바닥
- 두 번째 바닥: 15봉 이내에 확정된 마지막 스윙 저점. 첫 바닥 위에서 버텼고
  (저점 ≥ 첫 바닥 − ATR × 0.25), 첫 반등의 15% 이상을 되돌렸다
  (되돌림 비율 = (넥라인 − 두 번째 저점) ÷ 높이 ≥ 0.15 — 얕은 되돌림은
  확신도 LOW 로만 진입하고, 깊을수록 등급이 올라간다)

**진입**  종가가 넥라인 위 (피봇 확정 지연 중에 지나간 돌파도 인정 — 최근
strength+1 봉 안에 넥라인 아래 종가가 있었어야 한다). 단, 넥라인에서 패턴
높이의 30% 이상 지나쳤으면 추격하지 않는다.

**청산**  목표가(넥라인 + 패턴 높이) 도달 — 진입 시 **익절가**로 걸어 둔다.
신호 청산은 없다: 목표 아니면 손절, 구조가 전부 정한다.

**손절**  두 번째 바닥 − ATR × 0.25 — 바닥이 깨지면 패턴 무효.

**확신도**  되돌림 비율 (1.0 = 완전한 같은 레벨 쌍바닥)
- ≥ 0.8 → VERY_HIGH · ≥ 0.6 → HIGH · ≥ 0.45 → MEDIUM · 그 외 LOW

**파라미터**  `strength`, `min_retest`(0.15), `freshness`(15), `max_span`(60)
"""

    def setup(self) -> None:
        self.strength = int(self.params.get("strength", 3))
        self.min_retest = float(self.params.get("min_retest", 0.15))
        self.freshness = int(self.params.get("freshness", 15))
        self.max_span = int(self.params.get("max_span", 60))

    @property
    def warmup_candles(self) -> int:
        return 80

    def generate(self, ctx: StrategyContext) -> Signal:
        candles = ctx.closed_candles
        if len(candles) < self.warmup_candles:
            return Signal(reason="워밍업 부족")

        atr_values = atr(candles, 14)
        if atr_values[-1] is None or atr_values[-1] == 0:
            return Signal(reason="지표 계산 불가")
        price, previous = candles[-1].close, candles[-2].close

        if ctx.position.is_open:
            # 목표(익절)와 손절이 구조를 전부 정한다 — 신호 청산은 없다.
            return Signal(reason="목표/손절 대기")

        lows = pivot_lows(candles, self.strength)
        highs = pivot_highs(candles, self.strength)

        # 마지막 피봇이 신선해야 하고, 첫 바닥은 그 앞 몇 개 중에서 찾는다.
        # 두 번째 바닥은 첫 바닥 위에서 버티며 첫 반등의 30% 이상을 되돌렸으면
        # 인정한다 — 정확히 같은 레벨만 고집하면 higher-low 쌍바닥을 다 버린다.
        buffer = atr_values[-1] * 0.25
        if len(lows) >= 2:
            second = lows[-1]
            fresh = len(candles) - 1 - second <= self.strength + self.freshness
            if fresh:
                second_low = candles[second].low
                for first in reversed(lows[-6:-1]):
                    if second - first > self.max_span:
                        break  # 너무 먼 짝은 패턴이 아니라 시장 한 주기다
                    first_low = candles[first].low
                    neckline = max(c.high for c in candles[first : second + 1])
                    height = neckline - first_low
                    if height <= 0 or second_low < first_low - buffer:
                        continue
                    retest = (neckline - second_low) / height
                    if retest < self.min_retest:
                        continue
                    # 피봇 확정 지연(strength봉) 사이에 돌파가 지나갔을 수 있다 —
                    # 최근 몇 봉 안에 넥라인 아래가 있었고 지금 위라면 돌파로 치되,
                    # 높이의 30% 이상 지나쳤으면 추격하지 않는다.
                    recently_below = any(
                        c.close <= neckline
                        for c in candles[-2 - self.strength : -1]
                    )
                    broke = (
                        price > neckline and recently_below
                        and price - neckline <= height * 0.3
                    )
                    if broke:
                        return Signal(
                            action=SignalAction.ENTER_LONG,
                            strength=_retest_conviction(retest),
                            stop_loss=second_low - buffer,
                            take_profit=neckline + height,
                            reason=f"쌍바닥 넥라인 돌파 (되돌림 {retest:.0%}, 목표 +{height:.6g})",
                        )
                    break  # 유효한 짝을 찾았으면 돌파 여부와 무관하게 종료
        if len(highs) >= 2:
            second = highs[-1]
            fresh = len(candles) - 1 - second <= self.strength + self.freshness
            if fresh:
                second_high = candles[second].high
                for first in reversed(highs[-6:-1]):
                    if second - first > self.max_span:
                        break
                    first_high = candles[first].high
                    neckline = min(c.low for c in candles[first : second + 1])
                    height = first_high - neckline
                    if height <= 0 or second_high > first_high + buffer:
                        continue
                    retest = (second_high - neckline) / height
                    if retest < self.min_retest:
                        continue
                    recently_above = any(
                        c.close >= neckline
                        for c in candles[-2 - self.strength : -1]
                    )
                    broke = (
                        price < neckline and recently_above
                        and neckline - price <= height * 0.3
                    )
                    if broke:
                        return Signal(
                            action=SignalAction.ENTER_SHORT,
                            strength=_retest_conviction(retest),
                            stop_loss=second_high + buffer,
                            take_profit=neckline - height,
                            reason=f"쌍봉 넥라인 이탈 (되돌림 {retest:.0%}, 목표 -{height:.6g})",
                        )
                    break
        return Signal(reason="패턴 없음")


def _retest_conviction(retest: float) -> float:
    """되돌림이 깊을수록(1.0 = 같은 레벨 쌍바닥) 확신을 올린다."""
    if retest >= 0.8:
        return Conviction.VERY_HIGH.value
    if retest >= 0.6:
        return Conviction.HIGH.value
    if retest >= 0.45:
        return Conviction.MEDIUM.value
    return Conviction.LOW.value


@register_strategy("macd_divergence")
class MacdDivergenceStrategy(Strategy):
    summary = "가격은 저점을 낮췄는데 MACD 히스토그램은 높였으면 매수"
    category = "reversion"
    description = """
rsi_divergence 와 같은 발상을 MACD 히스토그램으로 잰 변형이다. 가격이 신저가를
만드는데 히스토그램의 골이 얕아지고 있으면, 하락을 미는 모멘텀이 줄고 있다는
뜻이다 — 브레이크를 밟은 채 내려가는 차와 같다.

RSI 판과의 차이: RSI 는 0~100 으로 정규화된 강도라 극단에서 뭉개지지만(포화),
히스토그램은 정규화가 없어 **모멘텀의 절대 크기 감소**가 그대로 보인다. 급락
후의 다이버전스에서는 포화가 없는 MACD 쪽이 더 정직하다는 것이 통설이다. 두
다이버전스 전략을 순위표에서 비교하면 어느 자가 이 시장에 맞는지 드러난다.

구현은 rsi_divergence 와 같은 프랙탈 피봇 방식이다 — 최근 두 스윙 저점에서
가격은 낮아지고 히스토그램은 높아졌으면(덜 깊은 골) 강세 다이버전스.

**강점**: 모멘텀 포화가 없어 깊은 급락에서도 격차가 읽힌다.
**약점**: 히스토그램 스케일이 심볼·시기마다 달라 격차의 절대 기준을 정할 수
없다 — 확신도는 상대 비교로만 잰다. 다이버전스 공통의 조기 진입 위험도 그대로다.
"""
    algorithm = """
**지표**  MACD(12, 26, 9) 히스토그램, 프랙탈 피봇(좌우 3봉), ATR(14)

**다이버전스** (강세 기준)
- 최근 두 스윙 저점: 가격 저점 낮아짐, 같은 자리 히스토그램은 높아짐(덜 깊음)
- 두 번째 저점의 히스토그램 < 0 (하락 맥락에서만)
- 두 번째 피봇 확정 후 5봉 이내

**진입**  다이버전스 확인 봉. 약세는 거울상.

**청산**  히스토그램이 0선을 넘으면(모멘텀 회복 완료) 청산.

**손절**  두 번째 저점 − ATR × 0.25.

**확신도**  히스토그램 개선 비율 (두 골의 깊이 비)
- 격차 ≥ 50% 개선 → VERY_HIGH · ≥ 30% → HIGH · ≥ 10% → MEDIUM · 그 외 LOW

**파라미터**  `fast`, `slow`, `signal`, `strength`, `freshness`
"""

    def setup(self) -> None:
        self.fast = int(self.params.get("fast", 12))
        self.slow = int(self.params.get("slow", 26))
        self.signal_period = int(self.params.get("signal", 9))
        self.strength = int(self.params.get("strength", 3))
        self.freshness = int(self.params.get("freshness", 5))

    @property
    def warmup_candles(self) -> int:
        return self.slow + self.signal_period + 40

    def generate(self, ctx: StrategyContext) -> Signal:
        candles = ctx.closed_candles
        if len(candles) < self.warmup_candles:
            return Signal(reason="워밍업 부족")

        from bot.indicators import macd
        _, _, histogram = macd([c.close for c in candles],
                               self.fast, self.slow, self.signal_period)
        atr_values = atr(candles, 14)
        if histogram[-1] is None or atr_values[-1] is None:
            return Signal(reason="지표 계산 불가")

        price = candles[-1].close
        if ctx.position.side is PositionSide.LONG and histogram[-1] >= 0:
            return Signal(action=SignalAction.EXIT, reason="모멘텀 회복 완료")
        if ctx.position.side is PositionSide.SHORT and histogram[-1] <= 0:
            return Signal(action=SignalAction.EXIT, reason="모멘텀 회복 완료")
        if ctx.position.is_open:
            return Signal(reason="반전 진행 대기")

        lows = pivot_lows(candles, self.strength)
        highs = pivot_highs(candles, self.strength)

        if len(lows) >= 2 and histogram[lows[-1]] is not None and histogram[lows[-2]] is not None:
            fresh = len(candles) - 1 - lows[-1] <= self.strength + self.freshness
            price_lower = candles[lows[-1]].low < candles[lows[-2]].low
            hist_now, hist_before = histogram[lows[-1]], histogram[lows[-2]]
            improving = hist_now > hist_before and hist_now < 0
            if fresh and price_lower and improving:
                improvement = (
                    (hist_now - hist_before) / abs(hist_before) if hist_before != 0 else 0.0
                )
                return Signal(
                    action=SignalAction.ENTER_LONG,
                    strength=_improvement_conviction(improvement),
                    stop_loss=candles[lows[-1]].low - atr_values[-1] * 0.25,
                    reason=f"MACD 강세 다이버전스 (골 {improvement:.0%} 개선)",
                )
        if len(highs) >= 2 and histogram[highs[-1]] is not None and histogram[highs[-2]] is not None:
            fresh = len(candles) - 1 - highs[-1] <= self.strength + self.freshness
            price_higher = candles[highs[-1]].high > candles[highs[-2]].high
            hist_now, hist_before = histogram[highs[-1]], histogram[highs[-2]]
            fading = hist_now < hist_before and hist_now > 0
            if fresh and price_higher and fading:
                improvement = (
                    (hist_before - hist_now) / abs(hist_before) if hist_before != 0 else 0.0
                )
                return Signal(
                    action=SignalAction.ENTER_SHORT,
                    strength=_improvement_conviction(improvement),
                    stop_loss=candles[highs[-1]].high + atr_values[-1] * 0.25,
                    reason=f"MACD 약세 다이버전스 (봉우리 {improvement:.0%} 감소)",
                )
        return Signal(reason="다이버전스 없음")


def _improvement_conviction(improvement: float) -> float:
    if improvement >= 0.5:
        return Conviction.VERY_HIGH.value
    if improvement >= 0.3:
        return Conviction.HIGH.value
    if improvement >= 0.1:
        return Conviction.MEDIUM.value
    return Conviction.LOW.value
