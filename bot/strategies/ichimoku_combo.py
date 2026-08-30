"""일목구름 + 다른 지표 조합 전략.

일목구름 하나로도 추세의 유무는 읽히지만, 구름은 **어디**에서 사야 하는지
(타이밍)와 **얼마나 센** 추세인지(세기)는 말해 주지 않는다. 이 파일의 전략들은
그 빈칸을 서로 다른 지표로 메운다:

* `ichimoku_rsi`    — 구름으로 방향, RSI 로 눌림목 타이밍
* `ichimoku_macd`   — 구름 돌파를 MACD 모멘텀으로 이중 확인
* `ichimoku_adx`    — 구름 위 전환/기준 교차를 ADX 세기로 걸러냄
* `ichimoku_sanyaku`— 삼역호전 완성 + OBV 매집 확인 (가장 드물고 가장 무겁게)

공통된 선택: **빈도를 버리고 질을 산다.** 조건이 겹칠수록 거래는 드물어지지만,
남는 신호는 여러 관점이 동시에 동의한 것들이다. 순위표에서 볼 때 거래 수가
적다는 점을 감안하고, 승률과 손익비를 함께 봐야 한다.
"""

from __future__ import annotations

from bot.indicators import adx, atr, macd, obv, rsi, sma
from bot.models import Conviction, PositionSide, Signal, SignalAction
from bot.strategies.base import Strategy, StrategyContext, register_strategy
from bot.strategies.ichimoku import _cloud_state
from bot.strategies.trend import _atr_stop


def _kijun_stop(state: dict, price: float, side: PositionSide, candles, multiplier: float):
    """기준선을 손절로 쓴다 — 일목에서 기준선 이탈은 추세 부정이다.

    기준선이 진입가와 같은 편이거나 너무 멀면 ATR 손절로 대체한다.
    """
    kijun = state["kijun"]
    atr_fallback = _atr_stop(candles, len(candles) - 1, price, side, multiplier)
    if side is PositionSide.LONG:
        return kijun if atr_fallback < kijun < price else atr_fallback
    return kijun if price < kijun < atr_fallback else atr_fallback


@register_strategy("ichimoku_rsi")
class IchimokuRsiStrategy(Strategy):
    summary = "구름 위에서만, RSI 눌림이 풀리는 순간 매수"
    category = "combo"
    description = """
trend_pullback(EMA + RSI)의 일목판이다. 방향 필터를 이동평균 대신 **구름**으로
바꿨다 — 종가가 구름 위면 매수만, 아래면 매도만 한다. 구름 안이면 아무것도 하지
않는다. 이동평균은 선이라 가격이 스치기만 해도 방향 판정이 흔들리지만, 구름은
띠라서 "위/안/아래"가 명확히 갈리고, '안'이라는 **판단 보류 지대**가 공짜로
생긴다. 애매한 구간에서 거래하지 않는 것이 이 조합의 첫 번째 강점이다.

타이밍은 RSI 가 잡는다. 구름 위에서도 아무 때나 사면 단기 과열에 물리므로,
RSI 가 45 아래로 눌렸다가 **다시 45 위로 돌아서는 순간**만 잡는다. 구름이라는
지지 지대 위에서 눌림이 풀리는 지점이라, 진입가와 손절선(기준선)이 가깝다.

확신도는 눌림의 깊이와 구름 상태로 정한다 — 깊이 눌렸다 돌아설수록, 그리고
구름이 방향과 같은 색일수록 크게 건다.

**강점**: 진입가가 유리해 손절 폭이 좁다. 구름 안 횡보에서는 자동으로 쉰다.
**약점**: 강한 추세에서는 RSI 가 45 까지 눌리지 않아 통째로 놓친다. 구름이
꺾이는 전환점에서는 방향 필터가 한발 늦는다.
"""
    algorithm = """
**지표**  일목균형표(9, 26, 52, 시프트 26), RSI(14), ATR(14)

**진입**  방향과 타이밍이 모두 맞아야 한다.
- 롱: 종가 > 구름 상단, 직전 RSI < 45 이고 이번 RSI ≥ 45
- 숏: 종가 < 구름 하단, 직전 RSI > 55 이고 이번 RSI ≤ 55
- 구름 안이면 진입하지 않는다.

**청산**  RSI 가 65(숏 35)에 닿거나, 종가가 기준선을 반대로 넘으면 청산.

**손절**  기준선 — 일목에서 기준선 이탈은 추세 부정이다. 기준선이 진입가와
같은 편이거나 ATR × 1.5 보다 멀면 진입가 ∓ ATR × 1.5 로 대체.

**확신도**  눌림 깊이(직전 RSI 가 45 에서 얼마나 아래였나) + 구름 색 일치
- 깊이 ≥ 10 이고 색 일치 → VERY_HIGH · 깊이 ≥ 10 또는 색 일치 → HIGH
- 깊이 ≥ 4 → MEDIUM · 그 외 LOW

**파라미터**  `tenkan`, `kijun`, `senkou_b`, `shift`, `rsi_period`,
`pullback_level`, `exit_level`, `atr_multiplier`
"""

    def setup(self) -> None:
        self.tenkan = int(self.params.get("tenkan", 9))
        self.kijun = int(self.params.get("kijun", 26))
        self.senkou_b = int(self.params.get("senkou_b", 52))
        self.shift = int(self.params.get("shift", 26))
        self.rsi_period = int(self.params.get("rsi_period", 14))
        self.pullback_level = float(self.params.get("pullback_level", 45))
        self.exit_level = float(self.params.get("exit_level", 65))
        self.atr_multiplier = float(self.params.get("atr_multiplier", 1.5))

    @property
    def warmup_candles(self) -> int:
        return self.senkou_b + self.shift + 22

    def generate(self, ctx: StrategyContext) -> Signal:
        candles = ctx.closed_candles
        if len(candles) < self.warmup_candles:
            return Signal(reason="워밍업 부족")

        state = _cloud_state(candles, self.tenkan, self.kijun, self.senkou_b, self.shift)
        momentum = rsi([c.close for c in candles], self.rsi_period)
        if state is None or momentum[-1] is None or momentum[-2] is None:
            return Signal(reason="지표 계산 불가")

        price = candles[-1].close
        current, previous = momentum[-1], momentum[-2]

        if ctx.position.side is PositionSide.LONG:
            if current >= self.exit_level or price < state["kijun"]:
                return Signal(action=SignalAction.EXIT,
                              reason="목표 도달" if current >= self.exit_level else "기준선 이탈")
            return Signal(reason=f"RSI {current:.0f} 상승 대기")
        if ctx.position.side is PositionSide.SHORT:
            if current <= (100 - self.exit_level) or price > state["kijun"]:
                return Signal(action=SignalAction.EXIT, reason="목표 도달 또는 기준선 이탈")
            return Signal(reason=f"RSI {current:.0f} 하락 대기")

        if state["dir"] == 0:
            return Signal(reason="구름 안 — 판단 보류")

        level = self.pullback_level
        if state["dir"] > 0 and previous < level <= current:
            depth = level - previous
            return Signal(
                action=SignalAction.ENTER_LONG,
                strength=_dip_conviction(depth, state["bullish_cloud"]),
                stop_loss=_kijun_stop(state, price, PositionSide.LONG,
                                      candles, self.atr_multiplier),
                reason=f"구름 위 눌림 해소 (RSI {previous:.0f} → {current:.0f})",
            )
        if state["dir"] < 0 and previous > (100 - level) >= current:
            depth = previous - (100 - level)
            return Signal(
                action=SignalAction.ENTER_SHORT,
                strength=_dip_conviction(depth, not state["bullish_cloud"]),
                stop_loss=_kijun_stop(state, price, PositionSide.SHORT,
                                      candles, self.atr_multiplier),
                reason=f"구름 아래 반등 소진 (RSI {previous:.0f} → {current:.0f})",
            )
        side = "위" if state["dir"] > 0 else "아래"
        return Signal(reason=f"구름 {side}, 눌림 대기 (RSI {current:.0f})")


def _dip_conviction(depth: float, cloud_agrees: bool) -> float:
    if depth >= 10 and cloud_agrees:
        return Conviction.VERY_HIGH.value
    if depth >= 10 or cloud_agrees:
        return Conviction.HIGH.value
    if depth >= 4:
        return Conviction.MEDIUM.value
    return Conviction.LOW.value


@register_strategy("ichimoku_macd")
class IchimokuMacdStrategy(Strategy):
    summary = "구름 돌파와 MACD 모멘텀이 같은 방향일 때만 진입"
    category = "combo"
    description = """
구름 돌파(ichimoku_cloud)의 가장 흔한 실패는 **힘 빠진 돌파**다 — 가격이 구름
상단을 겨우 넘었는데 상승 속도는 이미 줄고 있는 경우, 돌파 직후 구름 안으로
되밀린다. 위치(구름)만 보고 속도(모멘텀)를 안 보기 때문에 생기는 문제다.

이 전략은 MACD 히스토그램을 속도계로 붙인다. 진입은 두 경로 중 하나다:
① 구름 돌파가 났는데 히스토그램이 이미 양수(속도가 실린 돌파), ② 가격은 이미
구름 위인데 히스토그램이 막 양전환(쉬었다가 재점화). 위치와 속도가 **둘 다**
같은 방향을 가리킬 때만 들어가는 것이다.

확신도는 히스토그램의 크기(속도)를 ATR 대비로 잰다 — 같은 돌파라도 속도가
실릴수록 이어질 가능성이 높다고 본다.

**강점**: 힘없는 돌파와 모멘텀만 있는 가짜 신호를 서로 걸러 준다. 재점화 경로
덕에 돌파를 놓쳐도 두 번째 기회가 있다.
**약점**: 둘 다 후행 지표라 진입이 늦다. 급반전 장에서는 두 지표가 함께 속는다
— 필터를 겹쳐도 같은 종류(후행)의 약점은 사라지지 않는다.
"""
    algorithm = """
**지표**  일목균형표(9, 26, 52, 시프트 26), MACD(12, 26, 9), ATR(14)

**진입**  위치와 속도가 모두 같은 방향일 때. 두 경로:
- 롱 ①: 이번 봉에 구름 상방 돌파 그리고 히스토그램 > 0
- 롱 ②: 종가가 구름 위 그리고 히스토그램이 0 이하 → 초과로 막 전환
- 숏: 각각의 거울상

**청산**  히스토그램이 반대로 전환되거나, 종가가 기준선을 반대로 넘으면 청산.

**손절**  기준선 (멀면 진입가 ∓ ATR × 2.0 으로 대체)

**확신도**  |히스토그램| ÷ ATR14
- ≥ 0.5 → VERY_HIGH · ≥ 0.3 → HIGH · ≥ 0.15 → MEDIUM · 그 외 LOW

**파라미터**  `tenkan`, `kijun`, `senkou_b`, `shift`, `fast`, `slow`, `signal`,
`atr_multiplier`
"""

    def setup(self) -> None:
        self.tenkan = int(self.params.get("tenkan", 9))
        self.kijun = int(self.params.get("kijun", 26))
        self.senkou_b = int(self.params.get("senkou_b", 52))
        self.shift = int(self.params.get("shift", 26))
        self.fast = int(self.params.get("fast", 12))
        self.slow = int(self.params.get("slow", 26))
        self.signal_period = int(self.params.get("signal", 9))
        self.atr_multiplier = float(self.params.get("atr_multiplier", 2.0))

    @property
    def warmup_candles(self) -> int:
        return max(self.senkou_b + self.shift, self.slow + self.signal_period) + 22

    def generate(self, ctx: StrategyContext) -> Signal:
        candles = ctx.closed_candles
        if len(candles) < self.warmup_candles:
            return Signal(reason="워밍업 부족")

        state = _cloud_state(candles, self.tenkan, self.kijun, self.senkou_b, self.shift)
        closes = [c.close for c in candles]
        _, _, histogram = macd(closes, self.fast, self.slow, self.signal_period)
        if state is None or histogram[-1] is None or histogram[-2] is None:
            return Signal(reason="지표 계산 불가")

        price = closes[-1]
        hist, hist_prev = histogram[-1], histogram[-2]

        if ctx.position.side is PositionSide.LONG:
            if hist < 0 or price < state["kijun"]:
                return Signal(action=SignalAction.EXIT,
                              reason="모멘텀 소멸" if hist < 0 else "기준선 이탈")
            return Signal(reason="위치·속도 유지")
        if ctx.position.side is PositionSide.SHORT:
            if hist > 0 or price > state["kijun"]:
                return Signal(action=SignalAction.EXIT, reason="모멘텀 소멸 또는 기준선 이탈")
            return Signal(reason="위치·속도 유지")

        flipped_up = hist_prev <= 0 < hist
        flipped_down = hist_prev >= 0 > hist
        long_setup = (state["broke_up"] and hist > 0) or (state["dir"] > 0 and flipped_up)
        short_setup = (state["broke_down"] and hist < 0) or (state["dir"] < 0 and flipped_down)

        if not (long_setup or short_setup):
            if state["broke_up"] or state["broke_down"]:
                return Signal(reason="돌파했으나 모멘텀이 받쳐주지 않음")
            return Signal(reason="위치·속도 불일치")

        conviction = _speed_conviction(abs(hist), candles)
        if long_setup:
            trigger = "돌파+모멘텀" if state["broke_up"] else "구름 위 재점화"
            return Signal(action=SignalAction.ENTER_LONG, strength=conviction,
                          stop_loss=_kijun_stop(state, price, PositionSide.LONG,
                                                candles, self.atr_multiplier),
                          reason=f"{trigger} (히스토그램 {hist:+.4f})")
        trigger = "이탈+모멘텀" if state["broke_down"] else "구름 아래 재점화"
        return Signal(action=SignalAction.ENTER_SHORT, strength=conviction,
                      stop_loss=_kijun_stop(state, price, PositionSide.SHORT,
                                            candles, self.atr_multiplier),
                      reason=f"{trigger} (히스토그램 {hist:+.4f})")


def _speed_conviction(magnitude: float, candles) -> float:
    atr_values = atr(candles, 14)
    if not atr_values[-1]:
        return Conviction.LOW.value
    ratio = magnitude / atr_values[-1]
    if ratio >= 0.5:
        return Conviction.VERY_HIGH.value
    if ratio >= 0.3:
        return Conviction.HIGH.value
    if ratio >= 0.15:
        return Conviction.MEDIUM.value
    return Conviction.LOW.value


@register_strategy("ichimoku_adx")
class IchimokuAdxStrategy(Strategy):
    summary = "구름 위 전환/기준 교차를 ADX 세기로 걸러서 진입"
    category = "combo"
    description = """
일목 이론에서 전환선/기준선 교차는 **어디서 났는지**에 따라 등급이 갈린다 —
구름 위에서 난 상향 교차는 강한 매수, 구름 안은 중간, 구름 아래는 약한 신호다.
이 전략은 그중 **최상급 교차만** 받는다: 롱은 구름 위에서 난 상향 교차,
숏은 구름 아래에서 난 하향 교차. 나머지 등급은 전부 버린다.

여기에 ADX 를 한 겹 더 얹는다. 최상급 교차라도 추세 자체가 흐물흐물하면
(ADX < 20) 횡보 속 잔교차일 가능성이 높으므로 무시한다. 결국 "추세가 실제로
서 있고(ADX), 가격이 유리한 지대에 있고(구름), 단기 흐름이 막 방향을 틀었다
(교차)"의 3중 확인이다.

거래는 드물다. 그 대신 남는 신호는 일목의 등급과 통계적 추세 세기가 동시에
동의한 것들이라, 이 파일에서 손익비가 가장 좋게 나오도록 설계된 조합이다.

**강점**: 최상급 신호만 받아 가짜 교차 비율이 낮다. 진입 근거가 겹겹이다.
**약점**: 신호가 매우 드물다 — 백테스트 거래 수가 적으면 성적의 통계적 의미도
약하다는 점을 감안할 것. ADX 가 느려 추세 초입을 자주 놓친다.
"""
    algorithm = """
**지표**  일목균형표(9, 26, 52, 시프트 26), ADX(14), ATR(14)

**진입**  ADX ≥ 20 인 상태에서, 두 최상급 구도 중 하나:
- ① 구름 위(숏은 아래)에서 전환선이 기준선을 교차 — 추세 중의 눌림이 풀리는 지점
- ② 이번 봉의 구름 돌파인데 전환선/기준선이 이미 그 방향으로 정렬 —
  단기 흐름이 앞서 돌아선 뒤의 돌파라 힘이 실린 경우

**청산**  전환선이 기준선을 반대로 교차하거나, 종가가 구름 안으로 되돌아오면
청산.

**손절**  기준선 (멀면 진입가 ∓ ATR × 2.0 으로 대체)

**확신도**  ADX 세기 + 구름 색 일치
- ADX ≥ 30 이고 색 일치 → VERY_HIGH · ADX ≥ 30 또는 색 일치 → HIGH
- ADX ≥ 25 → MEDIUM · 그 외 LOW

**파라미터**  `tenkan`, `kijun`, `senkou_b`, `shift`, `adx_period`,
`adx_threshold`, `atr_multiplier`
"""

    def setup(self) -> None:
        self.tenkan = int(self.params.get("tenkan", 9))
        self.kijun = int(self.params.get("kijun", 26))
        self.senkou_b = int(self.params.get("senkou_b", 52))
        self.shift = int(self.params.get("shift", 26))
        self.adx_period = int(self.params.get("adx_period", 14))
        self.adx_threshold = float(self.params.get("adx_threshold", 20))
        self.atr_multiplier = float(self.params.get("atr_multiplier", 2.0))

    @property
    def warmup_candles(self) -> int:
        return self.senkou_b + self.shift + 22

    def generate(self, ctx: StrategyContext) -> Signal:
        candles = ctx.closed_candles
        if len(candles) < self.warmup_candles:
            return Signal(reason="워밍업 부족")

        state = _cloud_state(candles, self.tenkan, self.kijun, self.senkou_b, self.shift)
        _, _, strength = adx(candles, self.adx_period)
        if state is None or strength[-1] is None:
            return Signal(reason="지표 계산 불가")

        price = candles[-1].close
        crossed_up = state["tenkan_prev"] <= state["kijun_prev"] and state["tenkan"] > state["kijun"]
        crossed_down = state["tenkan_prev"] >= state["kijun_prev"] and state["tenkan"] < state["kijun"]

        if ctx.position.side is PositionSide.LONG:
            if crossed_down or state["dir"] <= 0:
                return Signal(action=SignalAction.EXIT,
                              reason="교차 반전" if crossed_down else "구름 복귀")
            return Signal(reason="최상급 신호 유지")
        if ctx.position.side is PositionSide.SHORT:
            if crossed_up or state["dir"] >= 0:
                return Signal(action=SignalAction.EXIT, reason="교차 반전 또는 구름 복귀")
            return Signal(reason="최상급 신호 유지")

        trending = strength[-1] >= self.adx_threshold
        aligned_up = state["tenkan"] > state["kijun"]
        aligned_down = state["tenkan"] < state["kijun"]
        # ① 구름 위 교차(추세 중 눌림 해소) ② 정렬된 돌파(단기 흐름이 앞선 돌파)
        long_setup = (state["dir"] > 0 and crossed_up) or (state["broke_up"] and aligned_up)
        short_setup = (state["dir"] < 0 and crossed_down) or (
            state["broke_down"] and aligned_down
        )

        if long_setup:
            if not trending:
                return Signal(reason=f"최상급 구도이나 ADX {strength[-1]:.0f} 부족")
            trigger = "구름 위 상향 교차" if crossed_up else "정렬 돌파"
            return Signal(
                action=SignalAction.ENTER_LONG,
                strength=_grade_conviction(strength[-1], state["bullish_cloud"]),
                stop_loss=_kijun_stop(state, price, PositionSide.LONG,
                                      candles, self.atr_multiplier),
                reason=f"{trigger} + ADX {strength[-1]:.0f}",
            )
        if short_setup:
            if not trending:
                return Signal(reason=f"최상급 구도이나 ADX {strength[-1]:.0f} 부족")
            trigger = "구름 아래 하향 교차" if crossed_down else "정렬 이탈"
            return Signal(
                action=SignalAction.ENTER_SHORT,
                strength=_grade_conviction(strength[-1], not state["bullish_cloud"]),
                stop_loss=_kijun_stop(state, price, PositionSide.SHORT,
                                      candles, self.atr_multiplier),
                reason=f"{trigger} + ADX {strength[-1]:.0f}",
            )
        if crossed_up or crossed_down:
            return Signal(reason="교차했으나 구름 등급 미달")
        return Signal(reason="구도 없음")


def _grade_conviction(adx_value: float, cloud_agrees: bool) -> float:
    if adx_value >= 30 and cloud_agrees:
        return Conviction.VERY_HIGH.value
    if adx_value >= 30 or cloud_agrees:
        return Conviction.HIGH.value
    if adx_value >= 25:
        return Conviction.MEDIUM.value
    return Conviction.LOW.value


@register_strategy("ichimoku_sanyaku")
class IchimokuSanyakuStrategy(Strategy):
    summary = "삼역호전이 완성되는 봉에서만 진입, OBV 매집이면 최대 확신"
    category = "combo"
    description = """
삼역호전(三役好轉)은 일목균형표에서 가장 무겁게 치는 매수 구도다 — ① 전환선이
기준선 위, ② 종가가 구름 위, ③ 종가가 26봉 전 종가(후행스팬 자리) 위. 셋이
동시에 성립하면 단기 흐름·중기 지대·과거 대비 위치가 전부 상승을 가리키는
것이다. 반대로 셋 다 뒤집힌 것이 삼역역전(매도)이다.

이 전략은 **세 조건이 막 완성되는 봉**에서만 진입한다. 이미 완성된 상태가
지속되는 동안에는 들어가지 않는다 — 늦게 올라타느니 다음 완성을 기다린다.
그래서 거래가 매우 드물다. 대신 완성 순간의 진입은 구도가 무너지는 것(조건
하나라도 깨짐)을 청산 신호로 쓸 수 있어, 진입과 청산의 논리가 한 몸이다.

거래량은 등급을 정한다. OBV 가 자기 평균 위(매집 중)에서 완성된 삼역호전은
최대 확신, 거래량이 받쳐주지 않으면 등급을 낮춘다 — 신호를 버리지는 않되
돈은 적게 건다.

**강점**: 일목 이론이 정의하는 최상위 구도만 거래한다. 진입·청산 논리가 대칭.
**약점**: 신호가 극히 드물어 성적이 쌓이는 데 오래 걸린다. 세 조건이 모두
후행이라 완성 시점엔 추세가 이미 상당히 진행돼 있다.
"""
    algorithm = """
**지표**  일목균형표(9, 26, 52, 시프트 26), OBV 와 OBV 의 SMA(20), ATR(14)

**삼역호전(롱)**  ① 전환선 > 기준선 ② 종가 > 구름 상단 ③ 종가 > 26봉 전 종가
**삼역역전(숏)**  세 조건의 거울상

**진입**  직전 봉에서는 셋 중 하나라도 아니었는데 이번 봉에 셋이 모두 성립
(= 완성 봉). 이미 완성된 상태의 지속에는 진입하지 않는다.

**청산**  세 조건 중 하나라도 깨지면 청산 — 진입 근거의 소멸이 곧 청산이다.

**손절**  기준선 (멀면 진입가 ∓ ATR × 2.0 으로 대체)

**확신도**  OBV 위치 + 구름 색
- OBV > 평균 이고 색 일치 → VERY_HIGH · 둘 중 하나 → HIGH · 둘 다 아니면 MEDIUM

**파라미터**  `tenkan`, `kijun`, `senkou_b`, `shift`, `obv_period`, `atr_multiplier`
"""

    def setup(self) -> None:
        self.tenkan = int(self.params.get("tenkan", 9))
        self.kijun = int(self.params.get("kijun", 26))
        self.senkou_b = int(self.params.get("senkou_b", 52))
        self.shift = int(self.params.get("shift", 26))
        self.obv_period = int(self.params.get("obv_period", 20))
        self.atr_multiplier = float(self.params.get("atr_multiplier", 2.0))

    @property
    def warmup_candles(self) -> int:
        return self.senkou_b + self.shift + 22

    def _roles(self, candles) -> tuple[dict, int, int] | None:
        """(구름 상태, 이번 봉 삼역 방향, 직전 봉 삼역 방향). 방향: +1/0/-1."""
        state = _cloud_state(candles, self.tenkan, self.kijun, self.senkou_b, self.shift)
        prev_state = _cloud_state(
            candles[:-1], self.tenkan, self.kijun, self.senkou_b, self.shift
        )
        if state is None or prev_state is None:
            return None
        return state, self._verdict(state, candles), self._verdict(prev_state, candles[:-1])

    def _verdict(self, state: dict, candles) -> int:
        if len(candles) <= self.shift:
            return 0
        close = candles[-1].close
        lagging_ref = candles[-1 - self.shift].close
        bull = state["tenkan"] > state["kijun"] and state["dir"] > 0 and close > lagging_ref
        bear = state["tenkan"] < state["kijun"] and state["dir"] < 0 and close < lagging_ref
        return 1 if bull else -1 if bear else 0

    def generate(self, ctx: StrategyContext) -> Signal:
        candles = ctx.closed_candles
        if len(candles) < self.warmup_candles:
            return Signal(reason="워밍업 부족")

        roles = self._roles(candles)
        if roles is None:
            return Signal(reason="지표 계산 불가")
        state, verdict, verdict_prev = roles
        price = candles[-1].close

        if ctx.position.side is PositionSide.LONG:
            if verdict != 1:
                return Signal(action=SignalAction.EXIT, reason="삼역호전 붕괴")
            return Signal(reason="삼역호전 유지")
        if ctx.position.side is PositionSide.SHORT:
            if verdict != -1:
                return Signal(action=SignalAction.EXIT, reason="삼역역전 붕괴")
            return Signal(reason="삼역역전 유지")

        completed_long = verdict == 1 and verdict_prev != 1
        completed_short = verdict == -1 and verdict_prev != -1
        if not (completed_long or completed_short):
            label = {1: "삼역호전 지속", -1: "삼역역전 지속", 0: "구도 미완성"}[verdict]
            return Signal(reason=label)

        flow = obv(candles)
        flow_avg = sma(flow, self.obv_period)
        accumulating = flow_avg[-1] is not None and (
            flow[-1] > flow_avg[-1] if completed_long else flow[-1] < flow_avg[-1]
        )
        cloud_agrees = state["bullish_cloud"] == completed_long
        conviction = (
            Conviction.VERY_HIGH.value if accumulating and cloud_agrees
            else Conviction.HIGH.value if accumulating or cloud_agrees
            else Conviction.MEDIUM.value
        )
        volume_note = "OBV 매집" if accumulating else "OBV 미확인"

        if completed_long:
            return Signal(action=SignalAction.ENTER_LONG, strength=conviction,
                          stop_loss=_kijun_stop(state, price, PositionSide.LONG,
                                                candles, self.atr_multiplier),
                          reason=f"삼역호전 완성 ({volume_note})")
        return Signal(action=SignalAction.ENTER_SHORT, strength=conviction,
                      stop_loss=_kijun_stop(state, price, PositionSide.SHORT,
                                            candles, self.atr_multiplier),
                      reason=f"삼역역전 완성 ({volume_note})")
