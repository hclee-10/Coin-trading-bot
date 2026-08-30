"""메타 전략 — 다른 전략들의 재료를 조합하거나 상황에 따라 갈아타는 층.

개별 지표 전략이 "한 가지 관점"이라면, 여기 전략들은 **관점들을 어떻게 묶을
것인가** 자체를 규칙으로 만든다. 합류(confluence)는 다수결로 묶고, 국면
전환기는 시장 상태에 따라 아예 다른 전략을 꺼내 쓴다.
"""

from __future__ import annotations

from bot.indicators import adx, bollinger, ema, macd, rsi
from bot.models import Conviction, PositionSide, Signal, SignalAction
from bot.strategies.base import Strategy, StrategyContext, register_strategy
from bot.strategies.trend import _atr_stop


@register_strategy("confluence")
class ConfluenceStrategy(Strategy):
    summary = "다섯 지표의 다수결 — 4표 이상 모이면 그 방향으로 진입"
    category = "combo"
    description = """
서로 다른 각도의 지표 다섯에게 한 표씩 주고 다수결로 정한다: ① EMA20/50 배열
(추세), ② MACD 히스토그램 부호(모멘텀), ③ RSI 50 상하(강도), ④ 볼린저 중심선
상하(위치), ⑤ +DI/−DI 우위(방향성). 각 표는 +1(상승) 또는 −1(하락)이고, 합계가
±5 사이에서 움직인다.

핵심은 **문턱의 통과 순간**만 잡는 것이다 — 합계가 +4 이상으로 **막 올라선**
봉에서 매수한다. 이미 +4인 상태의 지속은 진입이 아니다(늦었다). 확신도는 표
수 그대로다: 만장일치(5표)면 최대, 4표면 그다음.

다섯 지표는 결이 다르게 골랐지만 완전히 독립은 아니다 — 모두 가격에서 나온
파생이라 강한 추세에서는 함께 맞고 함께 틀린다. 다수결은 노이즈를 거르는
장치이지, 근본적으로 새로운 정보를 만들지는 않는다는 한계를 알고 써야 한다.

**강점**: 단일 지표의 변덕이 사라진다. 사유에 표 내역이 남아 복기가 쉽다.
**약점**: 5표가 모일 즈음엔 움직임이 진행된 뒤다. 지표 간 상관 때문에 "다섯
관점"이 실제로는 두세 관점일 수 있다.
"""
    algorithm = """
**지표와 표**
- EMA20 vs EMA50 배열 → ±1
- MACD(12,26,9) 히스토그램 부호 → ±1
- RSI(14) 의 50 상하 → ±1
- 종가 vs 볼린저 중심선(SMA20) → ±1
- +DI vs −DI 우위 → ±1

각 항목에는 **중립 구간**이 있다 (예: RSI 는 50±2 면 0표) — 사실상 평평한
장에서 엡실론 차이로 만장일치가 나오는 것을 막는다.

**진입**  합계가 문턱(±4)을 **막 넘어선 봉**에서만.
- 롱: 직전 합계 < +4 이고 이번 합계 ≥ +4
- 숏: 직전 합계 > −4 이고 이번 합계 ≤ −4

**청산**  합계가 0 을 반대로 넘으면(과반 붕괴) 청산.

**손절**  진입가 ∓ (ATR14 × 2.0)

**확신도**  표 수 — 5표 → VERY_HIGH · 4표 → HIGH
(문턱이 4라 3표 이하로는 진입하지 않는다)

**파라미터**  `threshold`(기본 4), `atr_multiplier`
"""

    def setup(self) -> None:
        self.threshold = int(self.params.get("threshold", 4))
        self.atr_multiplier = float(self.params.get("atr_multiplier", 2.0))

    @property
    def warmup_candles(self) -> int:
        return 80

    def _votes(self, candles) -> tuple[int, list[str]] | None:
        closes = [c.close for c in candles]
        fast, slow = ema(closes, 20), ema(closes, 50)
        _, _, histogram = macd(closes)
        momentum = rsi(closes, 14)
        _, middle, _ = bollinger(closes, 20)
        plus, minus, _ = adx(candles, 14)
        price = closes[-1]
        # 중립 구간: 차이가 이만큼은 벌어져야 표를 준다. 사각지대가 없으면
        # 사실상 평평한 장에서 엡실론 차이로 만장일치가 나온다.
        checks = [
            ("EMA", fast[-1], slow[-1], price * 0.0005),
            ("MACD", histogram[-1], 0.0, price * 0.0001),
            ("RSI", momentum[-1], 50.0, 2.0),
            ("BB", closes[-1], middle[-1], price * 0.0005),
            ("DI", plus[-1], minus[-1], 2.0),
        ]
        total = 0
        notes = []
        for name, a, b, dead_zone in checks:
            if a is None or b is None:
                return None
            vote = 1 if a - b > dead_zone else -1 if b - a > dead_zone else 0
            total += vote
            notes.append(f"{name}{'+' if vote > 0 else '-' if vote < 0 else '·'}")
        return total, notes

    def generate(self, ctx: StrategyContext) -> Signal:
        candles = ctx.closed_candles
        if len(candles) < self.warmup_candles:
            return Signal(reason="워밍업 부족")

        now = self._votes(candles)
        before = self._votes(candles[:-1])
        if now is None or before is None:
            return Signal(reason="지표 계산 불가")
        total, notes = now
        total_prev, _ = before
        price = candles[-1].close
        label = " ".join(notes)

        if ctx.position.side is PositionSide.LONG and total <= 0:
            return Signal(action=SignalAction.EXIT, reason=f"과반 붕괴 ({total:+d})")
        if ctx.position.side is PositionSide.SHORT and total >= 0:
            return Signal(action=SignalAction.EXIT, reason=f"과반 붕괴 ({total:+d})")
        if ctx.position.is_open:
            return Signal(reason=f"합류 유지 ({total:+d} {label})")

        conviction = Conviction.VERY_HIGH.value if abs(total) >= 5 else Conviction.HIGH.value
        if total_prev < self.threshold <= total:
            return Signal(action=SignalAction.ENTER_LONG, strength=conviction,
                          stop_loss=_atr_stop(candles, len(candles) - 1, price,
                                              PositionSide.LONG, self.atr_multiplier),
                          reason=f"상승 합류 {total:+d}표 ({label})")
        if total_prev > -self.threshold >= total:
            return Signal(action=SignalAction.ENTER_SHORT, strength=conviction,
                          stop_loss=_atr_stop(candles, len(candles) - 1, price,
                                              PositionSide.SHORT, self.atr_multiplier),
                          reason=f"하락 합류 {total:+d}표 ({label})")
        return Signal(reason=f"합류 부족 ({total:+d} {label})")


@register_strategy("regime_switch")
class RegimeSwitchStrategy(Strategy):
    summary = "ADX로 시장 국면을 판정해 추세장엔 교차, 횡보장엔 회귀로 갈아탄다"
    category = "combo"
    description = """
추세추종은 횡보에서 죽고 평균회귀는 추세에서 죽는다 — 이 순위표의 거의 모든
전략이 이 한 문장의 양쪽 어딘가에 있다. 이 전략은 그 선택 자체를 자동화한다:
ADX 로 **지금이 어느 장인지 먼저 판정**하고, 추세장(ADX ≥ 25)이면 EMA20/50
교차로 따라가고, 횡보장(ADX ≤ 18)이면 볼린저 복귀로 거슬러 간다. 그 사이의
애매한 구간(18~25)에서는 신규 진입을 하지 않는다.

이론상으로는 양쪽의 장점만 취하는 그림이지만, 실전의 관건은 **국면 판정의
지연**이다. ADX 는 느린 지표라 "추세장이다"라는 판정이 나올 즈음 추세가 끝나고,
"횡보다"라는 판정 직후에 돌파가 터지는 것이 최악의 시나리오다. 그래서 이 전략의
성적은 순수 추세 전략·순수 회귀 전략과 나란히 놓고 봐야 의미가 있다 — 전환

비용(판정 지연)이 전환 이득보다 큰 시장이라면 한쪽만 트는 게 낫다.

확신도는 각 모드의 기존 기준을 그대로 쓰되, 국면 판정의 여유(ADX 가 경계에서
얼마나 먼가)로 한 단계 조정한다.

**강점**: 한 전략으로 두 국면을 산다. 애매한 구간에서 쉬는 것이 내장돼 있다.
**약점**: 국면 판정 지연이라는 새 리스크를 산다. 두 모드의 포지션이 국면 전환
순간에 어색하게 겹칠 수 있다(전환 시 기존 포지션은 그 모드의 규칙으로 청산).
"""
    algorithm = """
**국면 판정**  ADX(14)
- 추세장: ADX ≥ 25 → 교차 모드
- 횡보장: ADX ≤ 18 → 회귀 모드
- 사이: 신규 진입 없음 (보유 포지션은 진입 당시 모드의 규칙으로 관리)

**교차 모드**  EMA20/50 상향 교차 → 롱, 하향 → 숏. 반대 교차로 청산.
**회귀 모드**  볼린저(20, 2σ) 하단 복귀 → 롱, 상단 복귀 → 숏. 중심선 청산.

**손절**  교차 모드 진입가 ∓ ATR × 2.0, 회귀 모드 ∓ ATR × 1.5

**확신도**  국면 판정의 여유
- 추세 모드: ADX ≥ 35 → VERY_HIGH · ≥ 30 → HIGH · 그 외 MEDIUM
- 회귀 모드: ADX ≤ 12 → VERY_HIGH · ≤ 15 → HIGH · 그 외 MEDIUM

**파라미터**  `trend_adx`(25), `range_adx`(18), `atr_multiplier`
"""

    def setup(self) -> None:
        self.trend_adx = float(self.params.get("trend_adx", 25))
        self.range_adx = float(self.params.get("range_adx", 18))
        self.atr_multiplier = float(self.params.get("atr_multiplier", 2.0))

    @property
    def warmup_candles(self) -> int:
        return 80

    def generate(self, ctx: StrategyContext) -> Signal:
        candles = ctx.closed_candles
        if len(candles) < self.warmup_candles:
            return Signal(reason="워밍업 부족")

        closes = [c.close for c in candles]
        _, _, strength = adx(candles, 14)
        fast, slow = ema(closes, 20), ema(closes, 50)
        upper, middle, lower = bollinger(closes, 20)
        if (strength[-1] is None or fast[-1] is None or slow[-1] is None
                or fast[-2] is None or slow[-2] is None or upper[-2] is None):
            return Signal(reason="지표 계산 불가")

        price, previous = closes[-1], closes[-2]
        regime = strength[-1]

        # 보유 중에는 양쪽 모드의 청산 신호를 모두 살핀다 — 진입 모드가 무엇이었든
        # 반대 근거가 나오면 내린다 (국면이 바뀌어 진입 근거가 사라진 경우 포함).
        if ctx.position.side is PositionSide.LONG:
            crossed_down = fast[-2] >= slow[-2] and fast[-1] < slow[-1]
            hit_middle = regime <= self.range_adx and price >= middle[-1]
            if crossed_down or hit_middle:
                return Signal(action=SignalAction.EXIT,
                              reason="교차 반전" if crossed_down else "중심선 도달")
            return Signal(reason=f"보유 유지 (ADX {regime:.0f})")
        if ctx.position.side is PositionSide.SHORT:
            crossed_up = fast[-2] <= slow[-2] and fast[-1] > slow[-1]
            hit_middle = regime <= self.range_adx and price <= middle[-1]
            if crossed_up or hit_middle:
                return Signal(action=SignalAction.EXIT,
                              reason="교차 반전" if crossed_up else "중심선 도달")
            return Signal(reason=f"보유 유지 (ADX {regime:.0f})")

        if regime >= self.trend_adx:
            crossed_up = fast[-2] <= slow[-2] and fast[-1] > slow[-1]
            crossed_down = fast[-2] >= slow[-2] and fast[-1] < slow[-1]
            conviction = (
                Conviction.VERY_HIGH.value if regime >= 35
                else Conviction.HIGH.value if regime >= 30
                else Conviction.MEDIUM.value
            )
            if crossed_up:
                return Signal(action=SignalAction.ENTER_LONG, strength=conviction,
                              stop_loss=_atr_stop(candles, len(candles) - 1, price,
                                                  PositionSide.LONG, self.atr_multiplier),
                              reason=f"추세장 상향 교차 (ADX {regime:.0f})")
            if crossed_down:
                return Signal(action=SignalAction.ENTER_SHORT, strength=conviction,
                              stop_loss=_atr_stop(candles, len(candles) - 1, price,
                                                  PositionSide.SHORT, self.atr_multiplier),
                              reason=f"추세장 하향 교차 (ADX {regime:.0f})")
            return Signal(reason=f"추세장, 교차 대기 (ADX {regime:.0f})")

        if regime <= self.range_adx:
            conviction = (
                Conviction.VERY_HIGH.value if regime <= 12
                else Conviction.HIGH.value if regime <= 15
                else Conviction.MEDIUM.value
            )
            if previous < lower[-2] <= price:
                return Signal(action=SignalAction.ENTER_LONG, strength=conviction,
                              stop_loss=_atr_stop(candles, len(candles) - 1, price,
                                                  PositionSide.LONG, 1.5),
                              reason=f"횡보장 하단 복귀 (ADX {regime:.0f})")
            if previous > upper[-2] >= price:
                return Signal(action=SignalAction.ENTER_SHORT, strength=conviction,
                              stop_loss=_atr_stop(candles, len(candles) - 1, price,
                                                  PositionSide.SHORT, 1.5),
                              reason=f"횡보장 상단 복귀 (ADX {regime:.0f})")
            return Signal(reason=f"횡보장, 밴드 복귀 대기 (ADX {regime:.0f})")

        return Signal(reason=f"국면 애매 (ADX {regime:.0f}) — 관망")
