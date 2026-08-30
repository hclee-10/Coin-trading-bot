"""캔들 패턴 전략.

지표는 여러 봉을 뭉개서 평균 내지만, 캔들 패턴은 **봉 하나하나의 공방**을 그대로
읽는다 — 누가 밀었고 누가 받아쳤는지가 몸통과 꼬리에 남는다. 계산이 없어 지연도
없다. 대신 패턴 하나만 보면 노이즈에 그대로 속으므로, 여기 전략들은 전부
**맥락 필터**(어디서 나온 패턴인가)를 함께 건다: 하락 뒤의 반전 패턴만, 수축
뒤의 돌파만 받는 식이다.

패턴의 크기는 ATR 로 정규화한다 — 같은 모양이라도 평소 변동폭 대비 큰 패턴이
더 무겁다.
"""

from __future__ import annotations

from bot.indicators import atr, ema, sma
from bot.models import Conviction, PositionSide, Signal, SignalAction
from bot.strategies.base import Strategy, StrategyContext, register_strategy


def _size_conviction(size: float, atr_value: float | None) -> float:
    """패턴 크기를 평소 변동폭(ATR) 대비로 등급화한다."""
    if not atr_value:
        return Conviction.LOW.value
    ratio = size / atr_value
    if ratio >= 1.5:
        return Conviction.VERY_HIGH.value
    if ratio >= 1.0:
        return Conviction.HIGH.value
    if ratio >= 0.6:
        return Conviction.MEDIUM.value
    return Conviction.LOW.value


@register_strategy("engulfing")
class EngulfingStrategy(Strategy):
    summary = "하락 끝의 상승 장악형에서 매수 (몸통이 직전 몸통을 삼킬 때)"
    category = "reversion"
    description = """
장악형(엔걸핑)은 이번 봉의 몸통이 직전 봉의 몸통을 반대 색으로 완전히 덮는
패턴이다. 직전 봉에서 판 사람들이 전부 물린 채로 봉이 끝났다는 뜻이라, 짧은
반전 신호 중에서는 근거가 명확한 편이다.

패턴만 보고 다 사면 횡보 노이즈에 당하므로 **맥락**을 건다 — 상승 장악형은
가격이 SMA20 아래(하락 뒤)일 때만, 하락 장악형은 SMA20 위(상승 뒤)일 때만
받는다. 추세 한가운데의 장악형은 반전이 아니라 눌림일 가능성이 높아서다.

확신도는 장악한 몸통의 크기를 ATR 대비로 잰다. 평소 변동폭보다 큰 몸통으로
삼켰다면 그만큼 반전의 힘이 실렸다고 본다.

**강점**: 지연이 0이다. 패턴의 근거(물린 물량)가 구체적이다.
**약점**: 한 봉짜리 정보라 수명이 짧다. 목표가가 없어 청산을 이동평균 복귀에
맡기는데, 추세로 발전하는 큰 반전은 일찍 내리게 된다.
"""
    algorithm = """
**지표**  직전·이번 봉의 몸통, SMA(20), ATR(14)

**패턴**
- 상승 장악형: 직전 봉 음봉, 이번 봉 양봉, 이번 몸통이 직전 몸통을 완전히 덮음
  (시가 ≤ 직전 종가, 종가 ≥ 직전 시가)
- 하락 장악형: 거울상

**진입**
- 롱: 상승 장악형 그리고 종가 < SMA20 (하락 뒤의 반전만)
- 숏: 하락 장악형 그리고 종가 > SMA20

**청산**  SMA20 에 닿으면 청산 (되돌림 목표).

**손절**  패턴 봉의 반대쪽 끝 (롱은 이번 봉 저가 − ATR × 0.25).

**확신도**  장악 몸통 크기 ÷ ATR14 — ≥1.5 VERY_HIGH · ≥1.0 HIGH · ≥0.6 MEDIUM · 그 외 LOW

**파라미터**  `ma_period`, `atr_multiplier`
"""

    def setup(self) -> None:
        self.ma_period = int(self.params.get("ma_period", 20))
        self.atr_multiplier = float(self.params.get("atr_multiplier", 1.5))

    @property
    def warmup_candles(self) -> int:
        return self.ma_period + 20

    def generate(self, ctx: StrategyContext) -> Signal:
        candles = ctx.closed_candles
        if len(candles) < self.warmup_candles:
            return Signal(reason="워밍업 부족")

        closes = [c.close for c in candles]
        baseline = sma(closes, self.ma_period)
        atr_values = atr(candles, 14)
        if baseline[-1] is None or atr_values[-1] is None:
            return Signal(reason="지표 계산 불가")

        current, previous = candles[-1], candles[-2]
        price = current.close

        if ctx.position.side is PositionSide.LONG and price >= baseline[-1]:
            return Signal(action=SignalAction.EXIT, reason="SMA20 도달")
        if ctx.position.side is PositionSide.SHORT and price <= baseline[-1]:
            return Signal(action=SignalAction.EXIT, reason="SMA20 도달")
        if ctx.position.is_open:
            return Signal(reason="되돌림 대기")

        bull_engulf = (
            previous.close < previous.open
            and current.close > current.open
            and current.open <= previous.close
            and current.close >= previous.open
        )
        bear_engulf = (
            previous.close > previous.open
            and current.close < current.open
            and current.open >= previous.close
            and current.close <= previous.open
        )
        body = abs(current.close - current.open)

        if bull_engulf and price < baseline[-1]:
            return Signal(
                action=SignalAction.ENTER_LONG,
                strength=_size_conviction(body, atr_values[-1]),
                stop_loss=current.low - atr_values[-1] * 0.25,
                reason=f"하락 뒤 상승 장악형 (몸통 {body / atr_values[-1]:.1f} ATR)",
            )
        if bear_engulf and price > baseline[-1]:
            return Signal(
                action=SignalAction.ENTER_SHORT,
                strength=_size_conviction(body, atr_values[-1]),
                stop_loss=current.high + atr_values[-1] * 0.25,
                reason=f"상승 뒤 하락 장악형 (몸통 {body / atr_values[-1]:.1f} ATR)",
            )
        return Signal(reason="장악형 없음")


@register_strategy("liquidity_sweep")
class LiquiditySweepStrategy(Strategy):
    summary = "직전 저점을 훑고(스탑 사냥) 도로 복귀한 봉에서 매수"
    category = "reversion"
    description = """
지지선 바로 아래에는 손절 주문이 쌓인다. 가격이 직전 N봉 저점을 살짝 깨면 그
손절들이 연쇄로 터지며 순간 급락하는데, 그 물량을 큰손이 받아 가면 가격은 곧장
저점 위로 복귀한다 — 이것이 '스탑 사냥' 또는 Wyckoff 의 스프링이다. 핀바의 긴
아래꼬리가 만들어지는 전형적인 이유이기도 하다.

패턴 정의: 이번 봉의 **저가가 직전 20봉 최저가를 깼는데, 종가는 그 최저가 위로
복귀**했다. 깨졌다는 사실보다 **복귀했다는 사실**이 신호다 — 깨지고 눌러앉으면
그냥 하락 지속이다. 반대(직전 고점을 훑고 복귀)는 업스러스트로, 숏이다.

손절은 훑은 저가 바로 아래다. 거기가 다시 깨지면 "받아 간 큰손" 가설이 틀린
것이다. 확신도는 훑은 깊이와 복귀의 완성도(종가가 저점 위로 얼마나 올라왔나)로
잰다.

**강점**: 남의 손절이 내 진입가가 된다 — 구조적으로 유리한 가격. 손절 폭이 좁다.
**약점**: 진짜 붕괴의 첫 봉과 구분이 안 될 때가 있다. 연쇄 청산장에서는 훑고
복귀했다가 다시 무너지는 이중 스윕이 나온다.
"""
    algorithm = """
**지표**  직전 20봉 최저가/최고가(이번 봉 제외), ATR(14)

**패턴**
- 스프링(롱): 이번 봉 저가 < 직전 20봉 최저가, 그리고 종가 ≥ 그 최저가
- 업스러스트(숏): 이번 봉 고가 > 직전 20봉 최고가, 그리고 종가 ≤ 그 최고가

**진입**  패턴 봉 확인 즉시.

**청산**  SMA20 도달 시 청산 (되돌림 목표).

**손절**  훑은 저가 − ATR × 0.1 (롱) — 다시 깨지면 가설 폐기.

**확신도**  훑은 깊이 ÷ ATR — ≥0.8 VERY_HIGH · ≥0.5 HIGH · ≥0.25 MEDIUM · LOW

**파라미터**  `lookback`(기본 20), `ma_period`
"""

    def setup(self) -> None:
        self.lookback = int(self.params.get("lookback", 20))
        self.ma_period = int(self.params.get("ma_period", 20))

    @property
    def warmup_candles(self) -> int:
        return max(self.lookback, self.ma_period) + 20

    def generate(self, ctx: StrategyContext) -> Signal:
        candles = ctx.closed_candles
        if len(candles) < self.warmup_candles:
            return Signal(reason="워밍업 부족")

        closes = [c.close for c in candles]
        baseline = sma(closes, self.ma_period)
        atr_values = atr(candles, 14)
        if baseline[-1] is None or atr_values[-1] is None or atr_values[-1] == 0:
            return Signal(reason="지표 계산 불가")

        c = candles[-1]
        price = c.close

        if ctx.position.side is PositionSide.LONG and price >= baseline[-1]:
            return Signal(action=SignalAction.EXIT, reason="SMA20 도달")
        if ctx.position.side is PositionSide.SHORT and price <= baseline[-1]:
            return Signal(action=SignalAction.EXIT, reason="SMA20 도달")
        if ctx.position.is_open:
            return Signal(reason="되돌림 대기")

        window = candles[-1 - self.lookback : -1]
        prior_low = min(x.low for x in window)
        prior_high = max(x.high for x in window)

        swept_low = c.low < prior_low and price >= prior_low
        swept_high = c.high > prior_high and price <= prior_high

        if swept_low:
            depth = prior_low - c.low
            return Signal(
                action=SignalAction.ENTER_LONG,
                strength=_sweep_conviction(depth, atr_values[-1]),
                stop_loss=c.low - atr_values[-1] * 0.1,
                reason=f"저점 스윕 후 복귀 (깊이 {depth / atr_values[-1]:.1f} ATR)",
            )
        if swept_high:
            depth = c.high - prior_high
            return Signal(
                action=SignalAction.ENTER_SHORT,
                strength=_sweep_conviction(depth, atr_values[-1]),
                stop_loss=c.high + atr_values[-1] * 0.1,
                reason=f"고점 스윕 후 복귀 (깊이 {depth / atr_values[-1]:.1f} ATR)",
            )
        return Signal(reason="스윕 없음")


def _sweep_conviction(depth: float, atr_value: float) -> float:
    ratio = depth / atr_value
    if ratio >= 0.8:
        return Conviction.VERY_HIGH.value
    if ratio >= 0.5:
        return Conviction.HIGH.value
    if ratio >= 0.25:
        return Conviction.MEDIUM.value
    return Conviction.LOW.value


@register_strategy("inside_bar")
class InsideBarStrategy(Strategy):
    summary = "내부봉(범위 수축) 뒤 모봉의 범위를 뚫는 방향으로 진입"
    category = "breakout"
    description = """
내부봉은 직전 봉(모봉)의 고가~저가 범위 안에 통째로 들어가는 봉이다. 시장이
한 봉 만에 합의를 못 보고 **수축**했다는 뜻이고, 수축 다음은 확장이다 — 어느
쪽으로 터질지는 모르지만 터진다는 것은 안다.

그래서 방향을 미리 정하지 않는다. 내부봉이 확인된 뒤, 다음 봉이 **모봉의
고가를 넘으면 매수, 저가를 깨면 매도**한다. 방향은 시장이 정하게 두고 이쪽은
따라붙기만 하는 구조라, 돌파 계열 중에서 가장 겸손한 형태다.

손절은 모봉의 반대쪽 끝이다. 그쪽이 뚫리면 돌파 방향이 틀린 것이므로 구조가
스스로 손절 근거를 준다. 확신도는 수축의 정도 — 내부봉이 모봉 대비 작을수록
(수축이 심할수록) 확장의 힘이 크다고 본다.

**강점**: 방향 편견이 없다. 손절 폭이 모봉 크기로 제한된다.
**약점**: 내부봉은 흔해서 신호가 잦다. 모봉이 큰 봉이면 손절 폭도 커진다.
"""
    algorithm = """
**패턴**  직전 봉(candles[-2])이 그 앞 봉(모봉, candles[-3])의 범위 안에
완전히 들어감: 고가 ≤ 모봉 고가, 저가 ≥ 모봉 저가.

**진입**  내부봉 확인 후 이번 봉에서
- 롱: 종가 > 모봉 고가
- 숏: 종가 < 모봉 저가

**청산**  EMA10 을 반대로 이탈하면 청산 (volatility_breakout 과 같은 방식).

**손절**  모봉의 반대쪽 끝 — 그쪽이 뚫리면 돌파 방향이 틀린 것이다.

**확신도**  수축률 = 1 − (내부봉 범위 ÷ 모봉 범위)
- ≥ 0.6 → VERY_HIGH · ≥ 0.45 → HIGH · ≥ 0.25 → MEDIUM · 그 외 LOW

**파라미터**  `exit_period`
"""

    def setup(self) -> None:
        self.exit_period = int(self.params.get("exit_period", 10))

    @property
    def warmup_candles(self) -> int:
        return max(self.exit_period, 14) + 20

    def generate(self, ctx: StrategyContext) -> Signal:
        candles = ctx.closed_candles
        if len(candles) < self.warmup_candles:
            return Signal(reason="워밍업 부족")

        mother, inner, current = candles[-3], candles[-2], candles[-1]
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

        is_inside = inner.high <= mother.high and inner.low >= mother.low
        if not is_inside:
            return Signal(reason="내부봉 없음")

        mother_span = mother.high - mother.low
        if mother_span <= 0:
            return Signal(reason="모봉 범위 없음")
        contraction = 1 - (inner.high - inner.low) / mother_span
        conviction = (
            Conviction.VERY_HIGH.value if contraction >= 0.6
            else Conviction.HIGH.value if contraction >= 0.45
            else Conviction.MEDIUM.value if contraction >= 0.25
            else Conviction.LOW.value
        )

        if price > mother.high:
            return Signal(action=SignalAction.ENTER_LONG, strength=conviction,
                          stop_loss=mother.low,
                          reason=f"내부봉 상방 확장 (수축 {contraction:.0%})")
        if price < mother.low:
            return Signal(action=SignalAction.ENTER_SHORT, strength=conviction,
                          stop_loss=mother.high,
                          reason=f"내부봉 하방 확장 (수축 {contraction:.0%})")
        return Signal(reason="내부봉 대기 — 모봉 범위 안")


@register_strategy("three_soldiers")
class ThreeSoldiersStrategy(Strategy):
    summary = "적삼병(연속 3양봉)이면 추세 시작으로 보고 매수"
    category = "trend"
    description = """
적삼병은 몸통이 실한 양봉 세 개가 연달아, 각각 직전보다 높게 닫히는 패턴이다.
사는 쪽이 세 봉 내내 주도권을 놓지 않았다는 뜻으로, 짧은 추세의 시작을 알리는
고전 패턴이다. 반대는 흑삼병(세 음봉)이다.

세 봉이라는 조건이 핵심이다 — 한 봉의 강세는 노이즈로 흔하지만, 되돌림 없이
세 번 연속은 우연으로 나오기 어렵다. 다만 세 봉을 기다린 값으로 진입은 그만큼
늦고, 짧은 반등의 꼭대기에서 사게 되는 위험이 있다. 그래서 몸통 크기 조건을
건다: 세 봉 모두 몸통이 ATR 의 절반 이상이어야 하고, 꼬리가 몸통보다 길면
(공방이 치열했다면) 패턴으로 치지 않는다.

확신도는 세 몸통의 합을 ATR 대비로 잰다 — 같은 적삼병이라도 크게 밀어붙인
쪽이 이어질 가능성이 높다.

**강점**: 조건이 명확하고 방향의 근거(3연속 주도권)가 직관적이다.
**약점**: 신호가 늦다. 급등 3봉 뒤에 사는 셈이라 단기 과열에 물릴 수 있다.
"""
    algorithm = """
**지표**  최근 3봉의 몸통, ATR(14), EMA(10)

**패턴** (롱 기준, 숏은 거울상)
- 세 봉 모두 양봉이고 종가가 각각 직전 종가보다 높다
- 세 봉 모두 몸통 ≥ ATR × 0.5
- 세 봉 모두 몸통 ≥ 위꼬리 (밀어붙임이 꼬리로 반납되지 않음)

**진입**  패턴이 완성된 봉에서. 직전 봉까지는 패턴이 아니었어야 한다(재진입 방지).

**청산**  EMA10 을 반대로 이탈하면 청산.

**손절**  첫 병사 봉의 저가 (롱) — 패턴 시작점이 무너지면 실패다.

**확신도**  세 몸통의 합 ÷ ATR — ≥3 VERY_HIGH · ≥2.2 HIGH · ≥1.5 MEDIUM · LOW

**파라미터**  `body_atr`(기본 0.5), `exit_period`
"""

    def setup(self) -> None:
        self.body_atr = float(self.params.get("body_atr", 0.5))
        self.exit_period = int(self.params.get("exit_period", 10))

    @property
    def warmup_candles(self) -> int:
        return 34

    def _pattern(self, bars, atr_value: float, *, bullish: bool) -> bool:
        previous_close = None
        for c in bars:
            body = c.close - c.open if bullish else c.open - c.close
            tail = (c.high - c.close) if bullish else (c.close - c.low)
            if body < atr_value * self.body_atr or tail > body:
                return False
            if previous_close is not None:
                if bullish and c.close <= previous_close:
                    return False
                if not bullish and c.close >= previous_close:
                    return False
            previous_close = c.close
        return True

    def generate(self, ctx: StrategyContext) -> Signal:
        candles = ctx.closed_candles
        if len(candles) < self.warmup_candles:
            return Signal(reason="워밍업 부족")

        atr_values = atr(candles, 14)
        if atr_values[-1] is None:
            return Signal(reason="지표 계산 불가")
        price = candles[-1].close

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
                reason="단기 추세 이탈" if wrong_way else "추세 유지",
            )

        last3 = candles[-3:]
        soldiers = self._pattern(last3, atr_values[-1], bullish=True)
        crows = self._pattern(last3, atr_values[-1], bullish=False)
        # 직전 봉에서 이미 완성돼 있었다면 이번 봉은 재진입이다 — 건너뛴다.
        was_soldiers = self._pattern(candles[-4:-1], atr_values[-1], bullish=True)
        was_crows = self._pattern(candles[-4:-1], atr_values[-1], bullish=False)

        total_body = sum(abs(c.close - c.open) for c in last3)
        conviction = (
            Conviction.VERY_HIGH.value if total_body >= atr_values[-1] * 3
            else Conviction.HIGH.value if total_body >= atr_values[-1] * 2.2
            else Conviction.MEDIUM.value if total_body >= atr_values[-1] * 1.5
            else Conviction.LOW.value
        )
        if soldiers and not was_soldiers:
            return Signal(action=SignalAction.ENTER_LONG, strength=conviction,
                          stop_loss=last3[0].low,
                          reason=f"적삼병 (몸통 합 {total_body / atr_values[-1]:.1f} ATR)")
        if crows and not was_crows:
            return Signal(action=SignalAction.ENTER_SHORT, strength=conviction,
                          stop_loss=last3[0].high,
                          reason=f"흑삼병 (몸통 합 {total_body / atr_values[-1]:.1f} ATR)")
        return Signal(reason="3연속 패턴 없음")


@register_strategy("nr7_breakout")
class Nr7BreakoutStrategy(Strategy):
    summary = "최근 7봉 중 가장 좁은 봉(NR7) 뒤의 돌파를 잡는다"
    category = "breakout"
    description = """
NR7(Narrowest Range 7)은 직전 봉의 고저 범위가 최근 7봉 중 가장 좁은 경우다.
토비 크레이블이 알린 고전 셋업으로, 논리는 내부봉과 같은 수축→확장이지만 자가
다르다 — 내부봉은 직전 봉 하나와 비교하고, NR7 은 **7봉의 맥락**에서 수축을
정의하므로 더 드물고 더 의미 있는 수축을 잡는다.

진입은 NR7 봉이 확정된 다음 봉이 NR7 봉의 고가를 넘으면 매수, 저가를 깨면
매도다. NR7 봉 자체가 좁으니 손절(반대쪽 끝)도 좁다 — 이 셋업의 손익비가 좋은
이유다.

확신도는 수축의 깊이로 잰다: NR7 봉의 범위가 7봉 평균 범위 대비 좁을수록
높인다. 평균의 절반도 안 되는 극단적 수축 뒤의 돌파가 최상급이다.

**강점**: 손절 폭이 구조적으로 좁다. 셋업이 드물어 과매매를 방지한다.
**약점**: 돌파 실패(양쪽 훑기)에 당하면 좁은 손절이 오히려 잦은 손절이 된다.
"""
    algorithm = """
**패턴**  직전 봉(candles[-2])의 범위(고−저)가 직전 7봉 중 최소.

**진입**  이번 봉에서
- 롱: 종가 > NR7 봉의 고가
- 숏: 종가 < NR7 봉의 저가

**청산**  EMA10 을 반대로 이탈하면 청산.

**손절**  NR7 봉의 반대쪽 끝.

**확신도**  수축 깊이 = 1 − (NR7 범위 ÷ 7봉 평균 범위)
- ≥ 0.6 → VERY_HIGH · ≥ 0.45 → HIGH · ≥ 0.3 → MEDIUM · 그 외 LOW

**파라미터**  `lookback`(기본 7), `exit_period`
"""

    def setup(self) -> None:
        self.lookback = int(self.params.get("lookback", 7))
        self.exit_period = int(self.params.get("exit_period", 10))

    @property
    def warmup_candles(self) -> int:
        return self.lookback + 20

    def generate(self, ctx: StrategyContext) -> Signal:
        candles = ctx.closed_candles
        if len(candles) < self.warmup_candles:
            return Signal(reason="워밍업 부족")

        price = candles[-1].close

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

        window = candles[-1 - self.lookback : -1]
        ranges = [c.high - c.low for c in window]
        setup_bar = candles[-2]
        setup_range = setup_bar.high - setup_bar.low
        if setup_range <= 0 or setup_range > min(ranges):
            return Signal(reason="NR7 아님")

        average_range = sum(ranges) / len(ranges)
        depth = 1 - setup_range / average_range if average_range > 0 else 0.0
        conviction = (
            Conviction.VERY_HIGH.value if depth >= 0.6
            else Conviction.HIGH.value if depth >= 0.45
            else Conviction.MEDIUM.value if depth >= 0.3
            else Conviction.LOW.value
        )

        if price > setup_bar.high:
            return Signal(action=SignalAction.ENTER_LONG, strength=conviction,
                          stop_loss=setup_bar.low,
                          reason=f"NR7 상방 돌파 (수축 {depth:.0%})")
        if price < setup_bar.low:
            return Signal(action=SignalAction.ENTER_SHORT, strength=conviction,
                          stop_loss=setup_bar.high,
                          reason=f"NR7 하방 돌파 (수축 {depth:.0%})")
        return Signal(reason="NR7 대기 — 범위 안")
