"""일목균형표(일목구름) 전략.

일목균형표는 다섯 개의 선으로 "지금 가격이 균형점 대비 어디에 있나"를 본다.
핵심은 **구름(선행스팬 A·B 사이의 띠)**이다 — 26봉 전에 계산되어 현재 위치에
그려져 있는 지지/저항 지대라서, 가격이 구름 위면 상승 우위, 아래면 하락 우위,
안이면 균형(방향 없음)으로 읽는다.

여기 두 전략의 관계:

* `ichimoku_cloud` — 봇이 도는 시간대 하나에서 구름 돌파를 잡는 기본형.
* `ichimoku_mtf` — 같은 판단을 **1시간·4시간·일봉에서 각각** 내린 뒤 가중
  평균한다. 긴 시간대일수록 무겁게 치되, 신호의 질(정렬·구름 두께)이 나쁘면
  긴 시간대라도 약하게 반영한다.

순위표에서 둘을 비교하면 "상위 시간대 확인이 실제로 값어치를 하는가"가 드러난다.
"""

from __future__ import annotations

from bot.indicators import atr, ichimoku_cloud
from bot.models import Candle, Conviction, PositionSide, Signal, SignalAction
from bot.strategies.base import Strategy, StrategyContext, register_strategy
from bot.strategies.trend import _atr_stop
from bot.timeframes import timeframe_to_ms


def _cloud_state(candles: list[Candle], tenkan_p: int, kijun_p: int,
                 senkou_b_p: int, shift: int) -> dict | None:
    """한 시간대의 일목 판단. 데이터가 모자라면 None.

    dir: 가격이 구름 위(+1)/안(0)/아래(-1)
    quality: 0~1. 전환/기준 정렬, 구름 색, 기준선 위치가 방향과 일치할수록,
             그리고 구름이 두꺼울수록 높다.
    """
    if len(candles) < senkou_b_p + shift + 2:
        return None
    tenkan, kijun, span_a, span_b = ichimoku_cloud(
        candles, tenkan_p, kijun_p, senkou_b_p, shift
    )
    if any(s[-1] is None or s[-2] is None for s in (tenkan, kijun, span_a, span_b)):
        return None

    close = candles[-1].close
    top = max(span_a[-1], span_b[-1])
    bottom = min(span_a[-1], span_b[-1])
    direction = 1 if close > top else -1 if close < bottom else 0

    quality = 0.4
    if direction != 0:
        if (tenkan[-1] - kijun[-1]) * direction > 0:      # 전환/기준 정렬
            quality += 0.2
        if (span_a[-1] - span_b[-1]) * direction > 0:     # 구름 색 일치
            quality += 0.2
        if (close - kijun[-1]) * direction > 0:           # 기준선 같은 편
            quality += 0.2
    # 얇은 구름은 뚫려도 의미가 약하다 — 지지/저항 지대가 사실상 없는 것이다.
    atr_values = atr(candles, 14)
    if atr_values[-1] and (top - bottom) < atr_values[-1] * 0.5:
        quality *= 0.5

    prev_top = max(span_a[-2], span_b[-2])
    prev_bottom = min(span_a[-2], span_b[-2])
    prev_close = candles[-2].close
    return {
        "dir": direction,
        "quality": quality,
        "score": direction * quality,
        "top": top,
        "bottom": bottom,
        "kijun": kijun[-1],
        "tenkan": tenkan[-1],
        "kijun_prev": kijun[-2],
        "tenkan_prev": tenkan[-2],
        "bullish_cloud": span_a[-1] > span_b[-1],
        # 이번 봉에서 구름을 뚫었는지 (진입 트리거)
        "broke_up": prev_close <= prev_top and close > top,
        "broke_down": prev_close >= prev_bottom and close < bottom,
    }


@register_strategy("ichimoku_cloud")
class IchimokuCloudStrategy(Strategy):
    summary = "일목구름을 종가가 뚫으면 진입, 확인 개수로 확신을 조절"
    category = "trend"
    description = """
일목균형표의 구름(선행스팬 A·B 사이의 띠)은 26봉 전에 계산되어 지금 자리에
그려져 있는 지지/저항 지대다. 가격이 이 띠를 아래에서 위로 뚫으면 저항 지대를
소화했다는 뜻으로 보고 매수하고, 위에서 아래로 뚫으면 매도한다.

단순 이동평균 돌파와 다른 점은 구름이 **면적**이라는 것이다. 선은 스치듯 뚫리지만
띠는 두께만큼의 공방을 거쳐야 넘어간다. 그래서 두꺼운 구름의 돌파는 얇은 구름의
돌파보다 무겁게 친다 — 얇은 구름은 애초에 저항이 아니었기 때문이다.

확신도는 일목의 고전적인 삼역호전(三役好轉) 확인을 따른다: ① 전환선이 기준선
위 ② 구름이 양운(스팬A > 스팬B) ③ 후행스팬 자리(26봉 전 종가)보다 지금 종가가
위. 세 확인이 모두 맞으면 최대로, 하나도 없으면 최소로 건다.

**강점**: 진입 근거(저항 소화)와 손절 근거(구름 재진입 = 돌파 실패)가 명확히
대응된다. 확인 조건이 지표 하나에 다 들어 있다.
**약점**: 26봉 시프트 탓에 신호가 늦다. 구름 안에서 오르내리는 횡보장에서는
돌파-복귀가 반복되며 잔손실이 쌓인다.
"""
    algorithm = """
**지표**  일목균형표(전환 9, 기준 26, 선행B 52, 시프트 26), ATR(14)
구름 상단 = max(스팬A, 스팬B), 하단 = min. 스팬은 26봉 전 값을 현재 위치로 정렬.

**진입**
- 롱: 직전 종가 ≤ 구름 상단 이고 이번 종가 > 구름 상단 (상방 돌파)
- 숏: 직전 종가 ≥ 구름 하단 이고 이번 종가 < 구름 하단 (하방 이탈)

**청산**  종가가 기준선을 반대로 넘으면 청산.

**손절**  구름 반대편 끝 ∓ (ATR × 0.25) 버퍼 — 구름을 도로 뚫고 반대편으로
나가면 돌파가 실패한 것이다. 다만 그 거리가 ATR × 3 을 넘으면 진입가 ∓ ATR × 2 로 좁힌다.

**확신도**  세 가지 확인의 개수 (전환>기준, 양운, 종가 > 26봉 전 종가)
- 3개 → VERY_HIGH · 2개 → HIGH · 1개 → MEDIUM · 0개 → LOW
얇은 구름(두께 < ATR × 0.5)의 돌파는 한 단계 낮춘다.

**파라미터**  `tenkan`, `kijun`, `senkou_b`, `shift`, `atr_multiplier`
"""

    def setup(self) -> None:
        self.tenkan = int(self.params.get("tenkan", 9))
        self.kijun = int(self.params.get("kijun", 26))
        self.senkou_b = int(self.params.get("senkou_b", 52))
        self.shift = int(self.params.get("shift", 26))
        self.atr_multiplier = float(self.params.get("atr_multiplier", 2.0))

    @property
    def warmup_candles(self) -> int:
        return self.senkou_b + self.shift + 20

    def generate(self, ctx: StrategyContext) -> Signal:
        candles = ctx.closed_candles
        if len(candles) < self.warmup_candles:
            return Signal(reason="워밍업 부족")

        state = _cloud_state(candles, self.tenkan, self.kijun, self.senkou_b, self.shift)
        if state is None:
            return Signal(reason="지표 계산 불가")
        price = candles[-1].close

        if ctx.position.side is PositionSide.LONG and price < state["kijun"]:
            return Signal(action=SignalAction.EXIT, reason="기준선 하향 이탈")
        if ctx.position.side is PositionSide.SHORT and price > state["kijun"]:
            return Signal(action=SignalAction.EXIT, reason="기준선 상향 돌파")
        if ctx.position.is_open:
            return Signal(reason="기준선 안쪽 유지")

        if not (state["broke_up"] or state["broke_down"]):
            where = "위" if state["dir"] > 0 else "아래" if state["dir"] < 0 else "안"
            return Signal(reason=f"돌파 없음 (구름 {where})")

        long_side = state["broke_up"]
        side = PositionSide.LONG if long_side else PositionSide.SHORT
        direction = 1 if long_side else -1

        # 삼역호전 확인: 전환/기준 정렬, 구름 색, 후행스팬 자리와의 비교.
        confirms = 0
        if (state["tenkan"] - state["kijun"]) * direction > 0:
            confirms += 1
        if state["bullish_cloud"] == long_side:
            confirms += 1
        lagging_ref = candles[-1 - self.shift].close if len(candles) > self.shift else None
        if lagging_ref is not None and (price - lagging_ref) * direction > 0:
            confirms += 1

        atr_values = atr(candles, 14)
        thin = bool(
            atr_values[-1] and (state["top"] - state["bottom"]) < atr_values[-1] * 0.5
        )
        if thin:
            confirms = max(0, confirms - 1)
        conviction = (
            Conviction.VERY_HIGH.value if confirms >= 3
            else Conviction.HIGH.value if confirms == 2
            else Conviction.MEDIUM.value if confirms == 1
            else Conviction.LOW.value
        )

        # 손절: 구름 반대편 끝 — 도로 뚫리면 돌파 실패다. 너무 멀면 ATR 로 좁힌다.
        buffer = (atr_values[-1] or 0.0) * 0.25
        edge = state["bottom"] - buffer if long_side else state["top"] + buffer
        if atr_values[-1] and abs(price - edge) > atr_values[-1] * 3:
            edge = _atr_stop(candles, len(candles) - 1, price, side, self.atr_multiplier)

        return Signal(
            action=SignalAction.ENTER_LONG if long_side else SignalAction.ENTER_SHORT,
            strength=conviction,
            stop_loss=edge,
            reason=(
                f"구름 {'상방 돌파' if long_side else '하방 이탈'} "
                f"(확인 {confirms}/3{', 얇은 구름' if thin else ''})"
            ),
        )


@register_strategy("ichimoku_mtf")
class IchimokuMtfStrategy(Strategy):
    summary = "1시간·4시간·일봉 일목구름의 합의로 진입 — 긴 시간대일수록 무겁게"
    category = "combo"
    extra_timeframes = ("1h", "4h", "1d")
    description = """
같은 일목구름 판단을 여러 시간대에서 내리고 합산한다. 봇이 도는 기본 시간대에
더해 **1시간·4시간·일봉**을 각각 보고, 시간대마다 "가격이 구름 위(+)인가
아래(−)인가"에 신호의 질을 곱한 점수를 낸다. 가중치는 기본 시간대 1, 1시간 2,
4시간 3, 일봉 4 — **긴 시간대의 구름일수록 무겁게 친다.** 일봉 구름 위에서의
1시간 돌파와, 일봉 구름 아래에서의 같은 돌파는 전혀 다른 거래라는 생각이다.

다만 시간이 길다고 무조건 세게 치지는 않는다. 각 시간대의 점수에는 **질**이
곱해진다 — 전환/기준 정렬이 어긋났거나, 구름 색이 반대거나, 구름이 얇으면
(지지/저항 지대가 사실상 없으면) 그 시간대의 점수는 일봉이라도 반으로 깎인다.
"신호가 약하면 시간이 길어도 약하게"다.

진입은 기본 시간대의 구름 돌파를 방아쇠로 쓰되, **가중 합의가 같은 방향으로
충분히 기울었을 때만** 당긴다. 상위 시간대가 반대편이면 돌파가 나와도 들어가지
않는다. 상위 캔들이 아직 준비되지 않은 환경(짧은 백테스트 등)에서는 계산 가능한
시간대만으로 합의를 내며, 판단에 쓴 시간대가 사유에 표시된다.

**강점**: 상위 시간대를 거스르는 역방향 돌파 진입을 구조적으로 거른다.
**약점**: 일봉 구름은 매우 느리다 — 큰 전환의 초입에서는 상위 시간대가 아직
반대편이라 좋은 진입을 거를 수 있다. 조건이 겹쳐 거래 수가 적다.
"""
    algorithm = """
**지표**  각 시간대의 일목균형표(9, 26, 52, 시프트 26), ATR(14)

**시간대와 가중치**  기본 시간대 ×1, 1시간 ×2, 4시간 ×3, 일봉 ×4
(캔들이 모자라 구름을 계산할 수 없는 시간대는 빼고 남은 것끼리 가중 평균)

**시간대별 점수**  방향 × 질
- 방향: 종가가 구름 위 +1 / 안 0 / 아래 −1
- 질: 0.4 + 전환>기준 일치 0.2 + 구름 색 일치 0.2 + 기준선 같은 편 0.2,
  얇은 구름(두께 < ATR × 0.5)이면 ×0.5

**합의 점수**  Σ(가중치 × 점수) ÷ Σ가중치, −1 ~ +1

**진입**  가격이 구름 밖(방향 일치)이고 합의가 같은 방향으로 ±0.35 이상이며,
다음 둘 중 하나가 이번 봉에 일어났을 때:
- 기본 시간대의 구름 돌파
- 합의 점수가 문턱을 막 넘어섬 (가격은 이미 구름 밖인데 정렬·상위 시간대가
  뒤늦게 동의한 경우 — "합의 점화")

**청산**  합의 점수가 0 을 반대로 넘거나, 기본 시간대 종가가 기준선을 반대로
넘으면 청산.

**손절**  진입가 ∓ (ATR14 × 2.0)

**확신도**  |합의 점수|
- ≥ 0.75 → VERY_HIGH · ≥ 0.6 → HIGH · ≥ 0.45 → MEDIUM · 그 외 LOW

**파라미터**  `tenkan`, `kijun`, `senkou_b`, `shift`, `entry_score`, `atr_multiplier`
"""

    #: 시간대 → 가중치. 기본 시간대는 setup 에서 1로 추가된다.
    WEIGHTS = (("1h", 2.0), ("4h", 3.0), ("1d", 4.0))

    def setup(self) -> None:
        self.tenkan = int(self.params.get("tenkan", 9))
        self.kijun = int(self.params.get("kijun", 26))
        self.senkou_b = int(self.params.get("senkou_b", 52))
        self.shift = int(self.params.get("shift", 26))
        self.entry_score = float(self.params.get("entry_score", 0.35))
        self.atr_multiplier = float(self.params.get("atr_multiplier", 2.0))

    @property
    def warmup_candles(self) -> int:
        return self.senkou_b + self.shift + 22

    def _consensus(self, ctx: StrategyContext) -> tuple[float, float, dict, list[str]]:
        """(합의 점수, 직전 봉 합의 점수, 기본 시간대 상태, 시간대별 표기).

        직전 봉 점수는 기본 시간대만 한 봉 물려 다시 계산한다 — 상위 시간대의
        봉은 폴링 사이에 거의 바뀌지 않으므로 그대로 두는 근사가 맞다.
        """
        base = _cloud_state(
            ctx.closed_candles, self.tenkan, self.kijun, self.senkou_b, self.shift
        )
        base_prev = _cloud_state(
            ctx.closed_candles[:-1], self.tenkan, self.kijun, self.senkou_b, self.shift
        )
        higher_total = higher_weight = 0.0
        notes: list[str] = []
        if base is not None:
            notes.append(f"{ctx.timeframe}{_arrow(base['dir'])}")
        base_ms = timeframe_to_ms(ctx.timeframe)
        for timeframe, weight in self.WEIGHTS:
            if timeframe_to_ms(timeframe) <= base_ms:
                continue  # 기본 시간대와 같거나 더 짧으면 중복이다
            candles = ctx.closed_candles_for(timeframe)
            state = _cloud_state(candles, self.tenkan, self.kijun, self.senkou_b, self.shift)
            if state is None:
                continue  # 캔들 부족 — 이 시간대는 판단 불가
            higher_total += weight * state["score"]
            higher_weight += weight
            notes.append(f"{timeframe}{_arrow(state['dir'])}")

        def combine(base_state: dict | None) -> float:
            total, weight_sum = higher_total, higher_weight
            if base_state is not None:
                total += 1.0 * base_state["score"]
                weight_sum += 1.0
            return total / weight_sum if weight_sum else 0.0

        return combine(base), combine(base_prev), base or {}, notes

    def generate(self, ctx: StrategyContext) -> Signal:
        candles = ctx.closed_candles
        if len(candles) < self.warmup_candles:
            return Signal(reason="워밍업 부족")

        score, score_prev, base, notes = self._consensus(ctx)
        if not base:
            return Signal(reason="지표 계산 불가")
        price = candles[-1].close
        label = " ".join(notes)

        if ctx.position.is_open:
            long_pos = ctx.position.side is PositionSide.LONG
            flipped = score <= 0 if long_pos else score >= 0
            kijun_broken = price < base["kijun"] if long_pos else price > base["kijun"]
            if flipped or kijun_broken:
                return Signal(
                    action=SignalAction.EXIT,
                    reason=f"{'합의 붕괴' if flipped else '기준선 이탈'} ({score:+.2f} {label})",
                )
            return Signal(reason=f"합의 유지 ({score:+.2f} {label})")

        # 방아쇠는 두 가지다: 이번 봉의 구름 돌파, 또는 합의가 문턱을 막 넘어섬
        # (가격은 이미 구름 위인데 정렬·상위 시간대가 뒤늦게 동의한 경우).
        ignited_up = score >= self.entry_score and score_prev < self.entry_score
        ignited_down = score <= -self.entry_score and score_prev > -self.entry_score
        long_setup = base["dir"] > 0 and score >= self.entry_score and (
            base["broke_up"] or ignited_up
        )
        short_setup = base["dir"] < 0 and score <= -self.entry_score and (
            base["broke_down"] or ignited_down
        )
        if long_setup:
            trigger = "구름 상방 돌파" if base["broke_up"] else "합의 점화"
            return Signal(
                action=SignalAction.ENTER_LONG,
                strength=_score_conviction(score),
                stop_loss=_atr_stop(candles, len(candles) - 1, price,
                                    PositionSide.LONG, self.atr_multiplier),
                reason=f"{trigger} + 합의 {score:+.2f} ({label})",
            )
        if short_setup:
            trigger = "구름 하방 이탈" if base["broke_down"] else "합의 점화"
            return Signal(
                action=SignalAction.ENTER_SHORT,
                strength=_score_conviction(score),
                stop_loss=_atr_stop(candles, len(candles) - 1, price,
                                    PositionSide.SHORT, self.atr_multiplier),
                reason=f"{trigger} + 합의 {score:+.2f} ({label})",
            )
        if base["broke_up"] or base["broke_down"]:
            return Signal(reason=f"돌파했으나 합의 부족 ({score:+.2f} {label})")
        return Signal(reason=f"돌파 없음 ({score:+.2f} {label})")


def _arrow(direction: int) -> str:
    return "↑" if direction > 0 else "↓" if direction < 0 else "·"


def _score_conviction(score: float) -> float:
    magnitude = abs(score)
    if magnitude >= 0.75:
        return Conviction.VERY_HIGH.value
    if magnitude >= 0.6:
        return Conviction.HIGH.value
    if magnitude >= 0.45:
        return Conviction.MEDIUM.value
    return Conviction.LOW.value
