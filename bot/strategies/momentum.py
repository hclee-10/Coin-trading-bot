"""모멘텀·추세 전략 (trend.py 의 확장판).

trend.py 의 고전들(EMA 교차, 돈치안, 슈퍼트렌드, MACD)과 같은 전제 —
**움직이기 시작한 가격은 한동안 그 방향을 유지한다** — 를 공유하되, 추세를
읽는 방법이 다른 것들을 모았다. 속도(ROC), 정배열(삼중 이동평균), 가속점
(파라볼릭 SAR), 평활 봉의 색(하이킨아시), 범위의 중간값(일목 전환·기준선),
추세의 세기(ADX)까지 — 같은 추세장을 서로 다른 각도에서 본다.

어느 각도가 이 시장에 맞는지는 모의매매 순위표가 알려준다.
"""

from __future__ import annotations

from bot.indicators import adx, atr, ema, heikin_ashi, ichimoku, psar, roc, sma
from bot.models import Conviction, PositionSide, Signal, SignalAction
from bot.strategies.base import Strategy, StrategyContext, register_strategy
from bot.strategies.trend import _atr_stop, _gap_conviction


@register_strategy("roc_momentum")
class RocMomentumStrategy(Strategy):
    summary = "N봉 전 대비 상승률이 문턱을 넘으면 그 방향으로 진입"
    category = "trend"
    description = """
가장 원초적인 모멘텀 전략이다. "10봉 전보다 1% 이상 올랐다"면 오르는 힘이
붙었다고 보고 매수한다. 이동평균도 밴드도 없이 **변화율(ROC) 하나**만 본다.

이렇게 단순한 게 왜 통하나 — 모멘텀은 금융시장에서 가장 오래, 가장 널리 확인된
현상(anomaly) 중 하나이기 때문이다. 오르던 것이 계속 오르는 경향은 주식·선물·
코인을 가리지 않고 관측되어 왔다. 복잡한 지표 대부분은 결국 이 현상을 다른
방식으로 재고 있을 뿐이다.

진입은 ROC 가 문턱(기본 ±1%)을 **넘어서는 순간**이고, 청산은 ROC 가 0 아래로
꺾이면(모멘텀 소멸) 한다. 문턱을 두는 이유는 0 근처의 잔떨림을 걸러내기
위해서다.

**강점**: 지연이 거의 없다. 규칙이 투명해서 성적이 나쁘면 원인도 명확하다.
**약점**: 문턱 값에 예민하다. 낮으면 횡보장 노이즈에 털리고, 높으면 추세의
후반에야 진입한다. 봉주기마다 적정 문턱이 다르다.
"""
    algorithm = """
**지표**  ROC(10) = (종가 ÷ 10봉 전 종가 − 1) × 100, ATR(14)

**진입**  문턱을 **넘어서는 순간** (직전 봉은 문턱 안, 이번 봉은 밖).
- 롱: 직전 ROC ≤ +1.0% 이고 이번 ROC > +1.0%
- 숏: 직전 ROC ≥ -1.0% 이고 이번 ROC < -1.0%

**청산**  ROC 가 0 을 반대로 넘으면(모멘텀 소멸) 청산.

**손절**  진입가 ∓ (ATR14 × 2.0)

**확신도**  ROC 가 문턱을 초과한 폭
- ≥ 문턱×1.0 → VERY_HIGH · ≥ 문턱×0.5 → HIGH · ≥ 문턱×0.2 → MEDIUM · 그 외 LOW

**파라미터**  `period`, `threshold_pct`, `atr_multiplier`
"""

    def setup(self) -> None:
        self.period = int(self.params.get("period", 10))
        self.threshold_pct = float(self.params.get("threshold_pct", 1.0))
        self.atr_multiplier = float(self.params.get("atr_multiplier", 2.0))

    @property
    def warmup_candles(self) -> int:
        return self.period + 30

    def generate(self, ctx: StrategyContext) -> Signal:
        candles = ctx.closed_candles
        if len(candles) < self.warmup_candles:
            return Signal(reason="워밍업 부족")

        values = roc([c.close for c in candles], self.period)
        if values[-1] is None or values[-2] is None:
            return Signal(reason="지표 계산 불가")

        current, previous = values[-1], values[-2]
        price = candles[-1].close

        if ctx.position.side is PositionSide.LONG and current < 0:
            return Signal(action=SignalAction.EXIT, reason="상승 모멘텀 소멸")
        if ctx.position.side is PositionSide.SHORT and current > 0:
            return Signal(action=SignalAction.EXIT, reason="하락 모멘텀 소멸")
        if ctx.position.is_open:
            return Signal(reason=f"ROC {current:+.2f}% 유지")

        threshold = self.threshold_pct
        if previous <= threshold < current:
            return Signal(
                action=SignalAction.ENTER_LONG,
                strength=_roc_conviction(current - threshold, threshold),
                stop_loss=_atr_stop(candles, len(candles) - 1, price,
                                    PositionSide.LONG, self.atr_multiplier),
                reason=f"모멘텀 점화 (ROC {current:+.2f}%)",
            )
        if previous >= -threshold > current:
            return Signal(
                action=SignalAction.ENTER_SHORT,
                strength=_roc_conviction(-threshold - current, threshold),
                stop_loss=_atr_stop(candles, len(candles) - 1, price,
                                    PositionSide.SHORT, self.atr_multiplier),
                reason=f"하락 모멘텀 점화 (ROC {current:+.2f}%)",
            )
        return Signal(reason=f"ROC {current:+.2f}% 문턱 안")


def _roc_conviction(excess: float, threshold: float) -> float:
    if excess >= threshold * 1.0:
        return Conviction.VERY_HIGH.value
    if excess >= threshold * 0.5:
        return Conviction.HIGH.value
    if excess >= threshold * 0.2:
        return Conviction.MEDIUM.value
    return Conviction.LOW.value


@register_strategy("triple_ma")
class TripleMaStrategy(Strategy):
    summary = "단기·중기·장기 이동평균이 정배열되는 순간 진입"
    category = "trend"
    description = """
이동평균 세 개(기본 10·20·40봉)를 겹쳐 놓고, 셋이 **정배열**(단기 > 중기 > 장기)
로 늘어서는 순간 매수한다. 역배열이 완성되면 매도한다.

두 이동평균의 교차(ema_cross)보다 조건이 하나 더 붙은 셈이라 신호는 늦고 적다.
대신 그만큼 가짜가 걸러진다 — 잠깐의 반등으로 단기선이 중기선을 넘는 일은
흔하지만, 세 선이 순서대로 늘어서려면 어느 정도 지속된 흐름이 필요하기 때문이다.
ema_cross 와 순위표에서 비교하면 "빠르고 잦은 신호 vs 늦고 확실한 신호" 중 어느
쪽이 이 시장에 맞는지 드러난다.

청산은 단기선이 중기선을 반대로 교차하면 한다. 정배열이 완전히 무너지기를
기다리면 이익 반납이 너무 크다.

**강점**: 가짜 교차에 덜 속는다. 큰 추세는 놓치지 않는다.
**약점**: 세 조건이 모두 맞을 즈음엔 추세가 이미 진행된 뒤다. 짧은 추세에서는
진입하자마자 청산 신호가 나온다.
"""
    algorithm = """
**지표**  SMA(10), SMA(20), SMA(40), ATR(14)

**진입**  정배열이 **완성되는 순간** (직전 봉은 아니었는데 이번 봉에 성립).
- 롱: SMA10 > SMA20 > SMA40
- 숏: SMA10 < SMA20 < SMA40

**청산**  단기선(SMA10)이 중기선(SMA20)을 반대로 교차하면 청산.

**손절**  진입가 ∓ (ATR14 × 2.0)

**확신도**  단기선과 장기선의 이격 `|SMA10 − SMA40| / SMA40 × 100`
을 ema_cross 와 같은 기준으로 나눈다 (≥1.5% VERY_HIGH …).

**파라미터**  `fast`, `mid`, `slow`, `atr_multiplier`
"""

    def setup(self) -> None:
        self.fast = int(self.params.get("fast", 10))
        self.mid = int(self.params.get("mid", 20))
        self.slow = int(self.params.get("slow", 40))
        self.atr_multiplier = float(self.params.get("atr_multiplier", 2.0))
        if not (self.fast < self.mid < self.slow):
            raise ValueError("fast < mid < slow 순서여야 합니다")

    @property
    def warmup_candles(self) -> int:
        return self.slow + 20

    def generate(self, ctx: StrategyContext) -> Signal:
        candles = ctx.closed_candles
        if len(candles) < self.warmup_candles:
            return Signal(reason="워밍업 부족")

        closes = [c.close for c in candles]
        fast = sma(closes, self.fast)
        mid = sma(closes, self.mid)
        slow = sma(closes, self.slow)
        if any(s[-1] is None or s[-2] is None for s in (fast, mid, slow)):
            return Signal(reason="지표 계산 불가")

        bull_now = fast[-1] > mid[-1] > slow[-1]
        bear_now = fast[-1] < mid[-1] < slow[-1]
        bull_before = fast[-2] > mid[-2] > slow[-2]
        bear_before = fast[-2] < mid[-2] < slow[-2]
        price = closes[-1]

        if ctx.position.side is PositionSide.LONG and fast[-1] < mid[-1]:
            return Signal(action=SignalAction.EXIT, reason="단기선이 중기선 하향 이탈")
        if ctx.position.side is PositionSide.SHORT and fast[-1] > mid[-1]:
            return Signal(action=SignalAction.EXIT, reason="단기선이 중기선 상향 돌파")
        if ctx.position.is_open:
            return Signal(reason="배열 유지 중")

        gap_pct = abs(fast[-1] - slow[-1]) / slow[-1] * 100
        if bull_now and not bull_before:
            return Signal(
                action=SignalAction.ENTER_LONG,
                strength=_gap_conviction(gap_pct),
                stop_loss=_atr_stop(candles, len(candles) - 1, price,
                                    PositionSide.LONG, self.atr_multiplier),
                reason=f"정배열 완성 (이격 {gap_pct:.2f}%)",
            )
        if bear_now and not bear_before:
            return Signal(
                action=SignalAction.ENTER_SHORT,
                strength=_gap_conviction(gap_pct),
                stop_loss=_atr_stop(candles, len(candles) - 1, price,
                                    PositionSide.SHORT, self.atr_multiplier),
                reason=f"역배열 완성 (이격 {gap_pct:.2f}%)",
            )
        return Signal(reason="배열 미완성")


@register_strategy("psar_trend")
class PsarTrendStrategy(Strategy):
    summary = "파라볼릭 SAR 점을 가격이 건드리면 추세 전환으로 보고 진입"
    category = "trend"
    description = """
파라볼릭 SAR 는 가격 아래(상승 중) 또는 위(하락 중)에 점을 찍는다. 점은 추세가
이어질수록 **가속하며** 가격을 따라붙고, 가격이 점을 건드리는 순간 추세가 뒤집힌
것으로 보고 점이 반대편으로 넘어간다.

슈퍼트렌드와 닮았지만 결정적 차이가 있다 — 슈퍼트렌드의 밴드 폭은 변동성(ATR)이
정하고, SAR 의 간격은 **추세의 지속 시간**이 정한다. 추세가 오래 갈수록 가속
계수가 커져 점이 바짝 따라붙기 때문에, 오래된 추세일수록 작은 되돌림에도 청산이
나온다. "추세 초반엔 여유 있게, 후반엔 타이트하게"가 지표 안에 내장된 셈이다.

진입은 점이 반대편으로 넘어가는 순간이고, SAR 점 자체를 손절가로 쓴다. 추세가
이어지는 동안 점이 따라오므로 추적 손절로 동작한다.

**강점**: 이익 보호가 빠르다. 추세 후반의 급반전에서 살아남는다.
**약점**: 횡보장에서는 점이 위아래로 계속 넘어가며 톱니 손실을 만든다. 슈퍼트렌드
보다도 신호가 잦다.
"""
    algorithm = """
**지표**  파라볼릭 SAR (가속 0.02 시작, 0.02 증가, 최대 0.2)

**진입**  SAR 점이 반대편으로 **막 넘어간 봉**에서 그 방향으로.
- 롱: 하락 SAR(가격 위) → 상승 SAR(가격 아래) 전환
- 숏: 그 반대

**청산**  점이 다시 반대편으로 넘어가면 청산.

**손절**  SAR 점 자체. 추세가 이어지는 동안 점이 따라오므로 **추적 손절**이다.

**확신도**  현재가에서 SAR 까지의 거리(= 손절 폭)
- < 0.5% → VERY_HIGH · < 1.0% → HIGH · < 2.0% → MEDIUM · 그 외 LOW
전환 직후일수록 점이 가깝고 손실 한도가 작으니 크게 건다.

**파라미터**  `af_start`, `af_step`, `af_max`
"""

    def setup(self) -> None:
        self.af_start = float(self.params.get("af_start", 0.02))
        self.af_step = float(self.params.get("af_step", 0.02))
        self.af_max = float(self.params.get("af_max", 0.2))

    @property
    def warmup_candles(self) -> int:
        return 40

    def generate(self, ctx: StrategyContext) -> Signal:
        candles = ctx.closed_candles
        if len(candles) < self.warmup_candles:
            return Signal(reason="워밍업 부족")

        line, trend = psar(candles, self.af_start, self.af_step, self.af_max)
        if trend[-1] is None or trend[-2] is None or line[-1] is None:
            return Signal(reason="지표 계산 불가")

        flipped = trend[-1] != trend[-2]
        price = candles[-1].close
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
                reason="SAR 반전" if wrong_way else "SAR 추세 유지",
            )

        if flipped and trend[-1] == 1:
            return Signal(action=SignalAction.ENTER_LONG, strength=conviction,
                          stop_loss=line[-1], reason="SAR 상승 전환")
        if flipped and trend[-1] == -1:
            return Signal(action=SignalAction.ENTER_SHORT, strength=conviction,
                          stop_loss=line[-1], reason="SAR 하락 전환")
        return Signal(reason="SAR 전환 없음")


@register_strategy("heikin_trend")
class HeikinTrendStrategy(Strategy):
    summary = "하이킨아시 봉의 색이 바뀌면 추세 전환으로 보고 진입"
    category = "trend"
    description = """
하이킨아시는 봉을 이전 봉과 섞어서 다시 그리는 방식이다. 시가를 직전 하이킨아시
봉의 중간값으로 두기 때문에 잔파동이 뭉개지고, 추세 중에는 같은 색(양봉/음봉)이
길게 이어진다. 원래 차트에서는 양봉·음봉이 뒤죽박죽인 구간도 하이킨아시로 보면
색이 한쪽으로 정리되어 있는 경우가 많다.

이 전략은 그 성질을 그대로 쓴다 — **색이 바뀌는 순간을 추세 전환으로 본다.**
직전까지 음봉이 이어지다가 양봉이 나오면 매수, 반대면 매도다. 잔파동이 이미
평활되어 있으므로 이동평균 교차보다 신호가 깔끔하게 나오는 경향이 있다.

새로 나온 봉의 몸통이 클수록(전환의 힘이 셀수록) 확신을 올린다. 꼬리만 긴
전환봉은 힘이 약하다고 본다.

**강점**: 추세 중의 잔파동에 흔들리지 않는다. 규칙이 눈으로 바로 확인된다.
**약점**: 평활의 대가로 신호가 반 박자 늦는다. 하이킨아시 가격은 실제 체결가가
아니므로 "얼마에 신호가 났나"와 "얼마에 체결되나"가 다르다.
"""
    algorithm = """
**지표**  하이킨아시 변환 봉, ATR(14)
- HA종가 = (시+고+저+종)/4, HA시가 = 직전 HA봉의 (시가+종가)/2

**진입**  색 전환 봉에서.
- 롱: 직전 HA봉 음봉(종<시) 이고 이번 HA봉 양봉(종>시)
- 숏: 그 반대

**청산**  색이 다시 반대로 바뀌면 청산.

**손절**  진입가 ∓ (ATR14 × 2.0)

**확신도**  전환봉 몸통 크기 `|HA종가 − HA시가| / 가격` 을 ATR14 와 비교
- ≥ ATR×0.8 → VERY_HIGH · ≥ ATR×0.5 → HIGH · ≥ ATR×0.25 → MEDIUM · 그 외 LOW

**파라미터**  `atr_multiplier`
"""

    def setup(self) -> None:
        self.atr_multiplier = float(self.params.get("atr_multiplier", 2.0))

    @property
    def warmup_candles(self) -> int:
        return 40

    def generate(self, ctx: StrategyContext) -> Signal:
        candles = ctx.closed_candles
        if len(candles) < self.warmup_candles:
            return Signal(reason="워밍업 부족")

        ha = heikin_ashi(candles)
        current, previous = ha[-1], ha[-2]
        bull_now, bull_before = current.close > current.open, previous.close > previous.open
        price = candles[-1].close

        if ctx.position.side is PositionSide.LONG and not bull_now:
            return Signal(action=SignalAction.EXIT, reason="하이킨아시 음전환")
        if ctx.position.side is PositionSide.SHORT and bull_now:
            return Signal(action=SignalAction.EXIT, reason="하이킨아시 양전환")
        if ctx.position.is_open:
            return Signal(reason="하이킨아시 색 유지")

        atr_values = atr(candles, 14)
        if atr_values[-1] is None:
            return Signal(reason="지표 계산 불가")
        body = abs(current.close - current.open)
        conviction = (
            Conviction.VERY_HIGH.value if body >= atr_values[-1] * 0.8
            else Conviction.HIGH.value if body >= atr_values[-1] * 0.5
            else Conviction.MEDIUM.value if body >= atr_values[-1] * 0.25
            else Conviction.LOW.value
        )

        if bull_now and not bull_before:
            return Signal(action=SignalAction.ENTER_LONG, strength=conviction,
                          stop_loss=_atr_stop(candles, len(candles) - 1, price,
                                              PositionSide.LONG, self.atr_multiplier),
                          reason="하이킨아시 양전환")
        if not bull_now and bull_before:
            return Signal(action=SignalAction.ENTER_SHORT, strength=conviction,
                          stop_loss=_atr_stop(candles, len(candles) - 1, price,
                                              PositionSide.SHORT, self.atr_multiplier),
                          reason="하이킨아시 음전환")
        return Signal(reason="색 전환 없음")


@register_strategy("ichimoku_cross")
class IchimokuCrossStrategy(Strategy):
    summary = "일목 전환선이 기준선을 교차하면 진입"
    category = "trend"
    description = """
일목균형표의 전환선(9봉 고저 중간값)과 기준선(26봉 고저 중간값)의 교차를 쓴다.
이동평균 교차와 형태는 같지만 재료가 다르다 — 이동평균은 **종가의 평균**이고,
일목의 선은 **고가·저가 범위의 중간**이다.

이 차이가 실전에서 뜻하는 것: 범위의 중간값은 가격이 기존 범위 안에서 오르내리는
동안에는 **전혀 움직이지 않는다.** 새로운 고가나 저가가 나와야 비로소 움직인다.
그래서 일목의 교차는 "평균적으로 올랐다"가 아니라 "**범위 자체가 위로 이동했다**"
는 신호다. 횡보 중의 미세한 교차가 이동평균보다 적게 나온다.

확신도는 가격과 기준선의 거리로 잰다. 기준선은 일목 이론에서 되돌림의 목표가로
쓰일 만큼 무게가 있는 선이라, 가격이 그 위에 확실히 떠 있을수록 추세가 건강하다고
본다.

**강점**: 횡보 중의 잔교차가 적다. 기준선이 지지/저항 역할을 겸한다.
**약점**: 26봉 범위가 기준이라 신호가 늦다. 급락 후 V자 반등처럼 범위가 급변하는
장에서는 선이 가격을 한참 뒤에서 쫓아간다.
"""
    algorithm = """
**지표**  일목 전환선(9), 기준선(26), ATR(14)
- 전환선 = 최근 9봉 (최고가+최저가)/2, 기준선 = 최근 26봉 (최고가+최저가)/2

**진입**
- 롱: 직전 봉에서 전환선 ≤ 기준선, 이번 봉에서 전환선 > 기준선 (상향 교차)
- 숏: 그 반대

**청산**  반대 교차가 나오면 청산.

**손절**  진입가 ∓ (ATR14 × 2.0)

**확신도**  가격과 기준선의 거리 `|가격 − 기준선| / 기준선 × 100`
- ≥ 1.5% → VERY_HIGH · ≥ 0.8% → HIGH · ≥ 0.3% → MEDIUM · 그 외 LOW

**파라미터**  `tenkan`, `kijun`, `atr_multiplier`
"""

    def setup(self) -> None:
        self.tenkan = int(self.params.get("tenkan", 9))
        self.kijun = int(self.params.get("kijun", 26))
        self.atr_multiplier = float(self.params.get("atr_multiplier", 2.0))
        if self.tenkan >= self.kijun:
            raise ValueError("tenkan 은 kijun 보다 작아야 합니다")

    @property
    def warmup_candles(self) -> int:
        return self.kijun + 20

    def generate(self, ctx: StrategyContext) -> Signal:
        candles = ctx.closed_candles
        if len(candles) < self.warmup_candles:
            return Signal(reason="워밍업 부족")

        tenkan, kijun = ichimoku(candles, self.tenkan, self.kijun)
        if tenkan[-1] is None or kijun[-1] is None or tenkan[-2] is None or kijun[-2] is None:
            return Signal(reason="지표 계산 불가")

        crossed_up = tenkan[-2] <= kijun[-2] and tenkan[-1] > kijun[-1]
        crossed_down = tenkan[-2] >= kijun[-2] and tenkan[-1] < kijun[-1]
        price = candles[-1].close

        if ctx.position.side is PositionSide.LONG and crossed_down:
            return Signal(action=SignalAction.EXIT, reason="전환선 하향 교차")
        if ctx.position.side is PositionSide.SHORT and crossed_up:
            return Signal(action=SignalAction.EXIT, reason="전환선 상향 교차")
        if ctx.position.is_open:
            return Signal(reason="교차 유지 중")

        conviction = _gap_conviction(abs(price - kijun[-1]) / kijun[-1] * 100)
        if crossed_up:
            return Signal(action=SignalAction.ENTER_LONG, strength=conviction,
                          stop_loss=_atr_stop(candles, len(candles) - 1, price,
                                              PositionSide.LONG, self.atr_multiplier),
                          reason="일목 상향 교차")
        if crossed_down:
            return Signal(action=SignalAction.ENTER_SHORT, strength=conviction,
                          stop_loss=_atr_stop(candles, len(candles) - 1, price,
                                              PositionSide.SHORT, self.atr_multiplier),
                          reason="일목 하향 교차")
        return Signal(reason="교차 없음")


@register_strategy("adx_trend")
class AdxTrendStrategy(Strategy):
    summary = "+DI/−DI 교차로 방향을, ADX로 추세의 세기를 확인하고 진입"
    category = "trend"
    description = """
추세추종의 최대 약점은 횡보장에서 신호가 계속 뒤집히는 것이다. ADX 는 바로 그
문제를 위해 만들어진 지표다 — 방향과 무관하게 **추세가 얼마나 뚜렷한지**를 재서,
횡보(ADX 낮음)와 추세장(ADX 높음)을 구분한다.

이 전략은 두 단계로 판단한다. 방향은 +DI(오르는 힘)와 −DI(내리는 힘)의 교차로
정하고, 그 교차가 **ADX 가 문턱(기본 20) 이상일 때만** 유효하다고 본다. 즉 같은
교차라도 횡보 중의 교차는 무시하고 추세장의 교차만 받는다.

ema_cross 같은 순수 추세추종과 순위표에서 비교하면 "횡보 필터가 실제로 손실을
줄이는가, 아니면 좋은 진입까지 걸러버리는가"를 확인할 수 있다.

**강점**: 톱니 손실(횡보장 반복 손절)이 구조적으로 줄어든다.
**약점**: ADX 는 이중으로 평활된 지표라 **매우 느리다.** 추세가 확인될 즈음엔
이미 상당히 진행된 뒤고, 짧고 굵은 추세는 통째로 놓친다.
"""
    algorithm = """
**지표**  +DI(14), −DI(14), ADX(14), ATR(14)

**진입**  DI 교차 **그리고** ADX ≥ 20 이 함께 성립할 때.
- 롱: +DI 가 −DI 를 상향 교차했거나, 교차 상태에서 ADX 가 막 20 을 넘어섬
- 숏: 그 반대

**청산**  DI 가 반대로 교차하면 청산. (ADX 는 청산에는 쓰지 않는다 —
추세가 약해지는 것과 꺾이는 것은 다르다.)

**손절**  진입가 ∓ (ATR14 × 2.0)

**확신도**  ADX 값
- ≥ 40 → VERY_HIGH · ≥ 30 → HIGH · ≥ 25 → MEDIUM · 그 외 LOW

**파라미터**  `period`, `adx_threshold`, `atr_multiplier`
"""

    def setup(self) -> None:
        self.period = int(self.params.get("period", 14))
        self.adx_threshold = float(self.params.get("adx_threshold", 20))
        self.atr_multiplier = float(self.params.get("atr_multiplier", 2.0))

    @property
    def warmup_candles(self) -> int:
        return self.period * 3 + 20

    def generate(self, ctx: StrategyContext) -> Signal:
        candles = ctx.closed_candles
        if len(candles) < self.warmup_candles:
            return Signal(reason="워밍업 부족")

        plus, minus, strength = adx(candles, self.period)
        if any(s[-1] is None or s[-2] is None for s in (plus, minus)) or strength[-1] is None:
            return Signal(reason="지표 계산 불가")

        crossed_up = plus[-2] <= minus[-2] and plus[-1] > minus[-1]
        crossed_down = plus[-2] >= minus[-2] and plus[-1] < minus[-1]
        price = candles[-1].close

        if ctx.position.side is PositionSide.LONG and crossed_down:
            return Signal(action=SignalAction.EXIT, reason="DI 하향 교차")
        if ctx.position.side is PositionSide.SHORT and crossed_up:
            return Signal(action=SignalAction.EXIT, reason="DI 상향 교차")
        if ctx.position.is_open:
            return Signal(reason="DI 방향 유지")

        trending = strength[-1] >= self.adx_threshold
        # 교차 자체가 이번 봉에 났거나, 교차 상태에서 ADX 가 막 문턱을 넘었을 때.
        adx_ignited = (
            strength[-2] is not None
            and strength[-2] < self.adx_threshold <= strength[-1]
        )
        conviction = (
            Conviction.VERY_HIGH.value if strength[-1] >= 40
            else Conviction.HIGH.value if strength[-1] >= 30
            else Conviction.MEDIUM.value if strength[-1] >= 25
            else Conviction.LOW.value
        )

        long_setup = (crossed_up and trending) or (adx_ignited and plus[-1] > minus[-1])
        short_setup = (crossed_down and trending) or (adx_ignited and plus[-1] < minus[-1])
        if long_setup:
            return Signal(action=SignalAction.ENTER_LONG, strength=conviction,
                          stop_loss=_atr_stop(candles, len(candles) - 1, price,
                                              PositionSide.LONG, self.atr_multiplier),
                          reason=f"+DI 우위 + ADX {strength[-1]:.0f}")
        if short_setup:
            return Signal(action=SignalAction.ENTER_SHORT, strength=conviction,
                          stop_loss=_atr_stop(candles, len(candles) - 1, price,
                                              PositionSide.SHORT, self.atr_multiplier),
                          reason=f"−DI 우위 + ADX {strength[-1]:.0f}")
        if crossed_up or crossed_down:
            return Signal(reason=f"DI 교차했으나 ADX {strength[-1]:.0f} < {self.adx_threshold:.0f} (횡보)")
        return Signal(reason="교차 없음")


@register_strategy("vortex_cross")
class VortexCrossStrategy(Strategy):
    summary = "볼텍스 VI+/VI−가 교차하면 진입 — DI보다 빠른 방향 전환 감지"
    category = "trend"
    description = """
볼텍스 지표는 "상승 소용돌이"와 "하락 소용돌이"의 세기를 잰다 — VI+ 는 이번
고가가 직전 저가에서 얼마나 뻗었는지, VI− 는 이번 저가가 직전 고가에서 얼마나
꽂혔는지의 누적이다. 봉과 봉 사이의 **교차 움직임**을 직접 재기 때문에, 같은
방향 지표인 +DI/−DI 보다 평활이 덜하고 반응이 빠르다.

진입은 두 선의 교차다. adx_trend 가 DI 교차에 ADX 필터를 얹어 늦고 확실한
신호를 고르는 것과 달리, 이쪽은 필터 없이 교차를 그대로 받아 빠르고 잦다 —
같은 '방향성' 계열 안에서 빠름(볼텍스)과 확실함(ADX 필터)의 짝 비교다.

확신도는 교차 순간 두 선의 벌어진 정도. 살짝 스친 교차보다 확실히 벌어진
교차가 이어질 가능성이 높다.

**강점**: 방향 전환을 DI 계열 중 가장 빨리 잡는다. 계산이 투명하다.
**약점**: 평활이 덜한 만큼 횡보에서 교차가 잦다. 필터가 없는 것이 설계지만,
그만큼 톱니 손실도 그대로 받는다.
"""
    algorithm = """
**지표**  볼텍스(14), ATR(14)

**진입**
- 롱: VI+ 가 VI− 를 상향 교차 (직전 봉 ≤, 이번 봉 >)
- 숏: 하향 교차

**청산**  반대 교차가 나오면 청산.

**손절**  진입가 ∓ (ATR14 × 2.0)

**확신도**  교차 직후 |VI+ − VI−|
- ≥ 0.20 → VERY_HIGH · ≥ 0.12 → HIGH · ≥ 0.05 → MEDIUM · 그 외 LOW

**파라미터**  `period`, `atr_multiplier`
"""

    def setup(self) -> None:
        self.period = int(self.params.get("period", 14))
        self.atr_multiplier = float(self.params.get("atr_multiplier", 2.0))

    @property
    def warmup_candles(self) -> int:
        return self.period + 30

    def generate(self, ctx: StrategyContext) -> Signal:
        candles = ctx.closed_candles
        if len(candles) < self.warmup_candles:
            return Signal(reason="워밍업 부족")

        from bot.indicators import vortex
        plus, minus = vortex(candles, self.period)
        if any(s[-1] is None or s[-2] is None for s in (plus, minus)):
            return Signal(reason="지표 계산 불가")

        crossed_up = plus[-2] <= minus[-2] and plus[-1] > minus[-1]
        crossed_down = plus[-2] >= minus[-2] and plus[-1] < minus[-1]
        price = candles[-1].close

        if ctx.position.side is PositionSide.LONG and crossed_down:
            return Signal(action=SignalAction.EXIT, reason="볼텍스 하향 교차")
        if ctx.position.side is PositionSide.SHORT and crossed_up:
            return Signal(action=SignalAction.EXIT, reason="볼텍스 상향 교차")
        if ctx.position.is_open:
            return Signal(reason="소용돌이 방향 유지")

        gap = abs(plus[-1] - minus[-1])
        conviction = (
            Conviction.VERY_HIGH.value if gap >= 0.20
            else Conviction.HIGH.value if gap >= 0.12
            else Conviction.MEDIUM.value if gap >= 0.05
            else Conviction.LOW.value
        )
        if crossed_up:
            return Signal(action=SignalAction.ENTER_LONG, strength=conviction,
                          stop_loss=_atr_stop(candles, len(candles) - 1, price,
                                              PositionSide.LONG, self.atr_multiplier),
                          reason=f"VI+ 상향 교차 (격차 {gap:.2f})")
        if crossed_down:
            return Signal(action=SignalAction.ENTER_SHORT, strength=conviction,
                          stop_loss=_atr_stop(candles, len(candles) - 1, price,
                                              PositionSide.SHORT, self.atr_multiplier),
                          reason=f"VI− 하향 교차 (격차 {gap:.2f})")
        return Signal(reason="교차 없음")
