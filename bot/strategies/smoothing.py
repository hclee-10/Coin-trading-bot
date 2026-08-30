"""평활·회귀 기반 추세 전략 (trend.py, momentum.py 의 확장).

이동평균 계열의 근본 딜레마는 하나다: **부드러움과 빠름은 맞바꿈이다.** 여기
모인 전략들은 그 맞바꿈을 각자 다른 수학으로 우회하려는 시도들이다 — 가중
조합으로 지연을 상쇄(헐 MA), 여러 겹을 부채처럼 펼침(EMA 리본), 세 겹 평활 뒤
변화율만 읽음(TRIX), 중간가의 두 평균 차이(어썸 오실레이터), 아예 최소제곱
직선을 그음(선형회귀).

같은 추세장을 다섯 가지 수학으로 보는 셈이라, 순위표에서 이 다섯과 ema_cross
를 비교하면 "지연을 줄이는 값어치"가 실제로 있는지 드러난다.
"""

from __future__ import annotations

from bot.indicators import atr, awesome_oscillator, ema, hma, linear_regression, trix
from bot.models import Conviction, PositionSide, Signal, SignalAction
from bot.strategies.base import Strategy, StrategyContext, register_strategy
from bot.strategies.trend import _atr_stop, _gap_conviction


@register_strategy("hull_trend")
class HullTrendStrategy(Strategy):
    summary = "지연을 상쇄한 헐 이동평균의 기울기가 뒤집히면 진입"
    category = "trend"
    description = """
헐 이동평균(HMA)은 "이동평균은 늦다"는 문제를 정면으로 공격한 지표다. 반 기간
가중평균을 두 배로 키워 전체 기간 평균을 빼면 지연이 대부분 상쇄되는데, 그
결과를 다시 √기간으로 살짝 평활해 매끄러움을 회복한다. 같은 기간의 EMA 보다
눈에 띄게 빨리 꺾이면서도 SMA 만큼 매끈하다.

전략은 단순하다 — **HMA 의 기울기**가 상승으로 뒤집히면 매수, 하락으로 뒤집히면
매도. 선 자체가 빠르므로 교차 대신 기울기 반전만으로 충분하다.

빠름의 대가는 과민함이다. 지연을 수학으로 상쇄한 만큼 노이즈도 증폭되어, 횡보
에서는 기울기가 자주 뒤집힌다. 그래서 기울기의 크기(ATR 대비)로 확신을 나눠
약한 반전에는 적게 건다.

**강점**: 추세 전환을 이동평균 계열 중 가장 일찍 잡는 축이다.
**약점**: 횡보 과민. 지연 상쇄는 공짜가 아니다 — 가격이 던지는 가짜 움직임도
그대로 증폭해서 따라간다.
"""
    algorithm = """
**지표**  HMA(16), ATR(14)

**진입**
- 롱: HMA 기울기가 하락 → 상승 전환 (직전 봉 기울기 ≤ 0, 이번 봉 > 0)
- 숏: 그 반대

**청산**  기울기가 다시 반대로 뒤집히면 청산.

**손절**  진입가 ∓ (ATR14 × 2.0)

**확신도**  |기울기| ÷ ATR (봉당 변화량)
- ≥ 0.3 → VERY_HIGH · ≥ 0.2 → HIGH · ≥ 0.1 → MEDIUM · 그 외 LOW

**파라미터**  `period`, `atr_multiplier`
"""

    def setup(self) -> None:
        self.period = int(self.params.get("period", 16))
        self.atr_multiplier = float(self.params.get("atr_multiplier", 2.0))

    @property
    def warmup_candles(self) -> int:
        return self.period + 30

    def generate(self, ctx: StrategyContext) -> Signal:
        candles = ctx.closed_candles
        if len(candles) < self.warmup_candles:
            return Signal(reason="워밍업 부족")

        line = hma([c.close for c in candles], self.period)
        atr_values = atr(candles, 14)
        if line[-1] is None or line[-2] is None or line[-3] is None or atr_values[-1] is None:
            return Signal(reason="지표 계산 불가")

        slope, slope_prev = line[-1] - line[-2], line[-2] - line[-3]
        price = candles[-1].close

        if ctx.position.side is PositionSide.LONG and slope < 0:
            return Signal(action=SignalAction.EXIT, reason="HMA 기울기 하락 전환")
        if ctx.position.side is PositionSide.SHORT and slope > 0:
            return Signal(action=SignalAction.EXIT, reason="HMA 기울기 상승 전환")
        if ctx.position.is_open:
            return Signal(reason="기울기 유지")

        ratio = abs(slope) / atr_values[-1]
        conviction = (
            Conviction.VERY_HIGH.value if ratio >= 0.3
            else Conviction.HIGH.value if ratio >= 0.2
            else Conviction.MEDIUM.value if ratio >= 0.1
            else Conviction.LOW.value
        )
        if slope_prev <= 0 < slope:
            return Signal(action=SignalAction.ENTER_LONG, strength=conviction,
                          stop_loss=_atr_stop(candles, len(candles) - 1, price,
                                              PositionSide.LONG, self.atr_multiplier),
                          reason=f"HMA 상승 전환 (기울기 {ratio:.2f} ATR/봉)")
        if slope_prev >= 0 > slope:
            return Signal(action=SignalAction.ENTER_SHORT, strength=conviction,
                          stop_loss=_atr_stop(candles, len(candles) - 1, price,
                                              PositionSide.SHORT, self.atr_multiplier),
                          reason=f"HMA 하락 전환 (기울기 {ratio:.2f} ATR/봉)")
        return Signal(reason="기울기 전환 없음")


@register_strategy("ema_ribbon")
class EmaRibbonStrategy(Strategy):
    summary = "EMA 5겹 리본이 완전히 정렬되는 순간 진입, 정렬 비율로 확신"
    category = "trend"
    description = """
기간이 다른 EMA 다섯 개(8·13·21·34·55)를 부채처럼 겹쳐 놓으면 추세의 상태가
리본의 모양으로 보인다 — 다섯 선이 짧은 것부터 순서대로 늘어서면(완전 정렬)
추세가 익은 것이고, 뒤엉켜 꼬이면 횡보다. triple_ma 의 확장판인데, 선이 다섯이라
"얼마나 정렬됐나"를 0~10점(인접 쌍 4개 + 전체 순서)이 아닌 **정렬 쌍의 비율**로
연속적으로 잴 수 있는 것이 차이다.

진입은 완전 정렬이 **막 완성되는 봉**이다. 확신도는 리본의 펼쳐진 폭 — 정렬돼
있어도 다섯 선이 좁게 붙어 있으면 갓 시작한 추세거나 가짜이므로 적게 걸고,
활짝 펼쳐졌으면 크게 건다.

청산은 완전 정렬의 붕괴가 아니라 **가장 짧은 선이 두 번째 선을 반대로 교차**할
때다. 완전 붕괴를 기다리면 이익 반납이 크다.

**강점**: 추세의 성숙도가 연속값으로 읽힌다. 가짜 교차에 강하다.
**약점**: 다섯 조건이 모두 맞을 즈음엔 추세 중반이다. 짧은 추세는 정렬이
완성되기 전에 끝난다.
"""
    algorithm = """
**지표**  EMA(8, 13, 21, 34, 55), ATR(14)

**진입**  완전 정렬이 막 완성되는 봉.
- 롱: EMA8 > EMA13 > EMA21 > EMA34 > EMA55 (직전 봉은 미완성)
- 숏: 역순 정렬 완성

**청산**  EMA8 이 EMA13 을 반대로 교차하면 청산.

**손절**  진입가 ∓ (ATR14 × 2.0)

**확신도**  리본 폭 = (EMA8 − EMA55) / EMA55 × 100 을 이격 기준으로
(≥1.5% VERY_HIGH · ≥0.8% HIGH · ≥0.3% MEDIUM · LOW)

**파라미터**  `periods`(기본 8,13,21,34,55), `atr_multiplier`
"""

    def setup(self) -> None:
        periods = self.params.get("periods", [8, 13, 21, 34, 55])
        self.periods = [int(p) for p in periods]
        if sorted(self.periods) != self.periods or len(self.periods) < 3:
            raise ValueError("periods 는 오름차순 3개 이상이어야 합니다")
        self.atr_multiplier = float(self.params.get("atr_multiplier", 2.0))

    @property
    def warmup_candles(self) -> int:
        return self.periods[-1] + 20

    def generate(self, ctx: StrategyContext) -> Signal:
        candles = ctx.closed_candles
        if len(candles) < self.warmup_candles:
            return Signal(reason="워밍업 부족")

        closes = [c.close for c in candles]
        ribbon = [ema(closes, p) for p in self.periods]
        if any(line[-1] is None or line[-2] is None for line in ribbon):
            return Signal(reason="지표 계산 불가")

        now = [line[-1] for line in ribbon]
        before = [line[-2] for line in ribbon]
        bull_now = all(a > b for a, b in zip(now, now[1:]))
        bear_now = all(a < b for a, b in zip(now, now[1:]))
        bull_before = all(a > b for a, b in zip(before, before[1:]))
        bear_before = all(a < b for a, b in zip(before, before[1:]))
        price = closes[-1]

        if ctx.position.side is PositionSide.LONG and now[0] < now[1]:
            return Signal(action=SignalAction.EXIT, reason="선두 EMA 하향 교차")
        if ctx.position.side is PositionSide.SHORT and now[0] > now[1]:
            return Signal(action=SignalAction.EXIT, reason="선두 EMA 상향 교차")
        if ctx.position.is_open:
            return Signal(reason="리본 정렬 유지")

        width_pct = abs(now[0] - now[-1]) / now[-1] * 100
        if bull_now and not bull_before:
            return Signal(action=SignalAction.ENTER_LONG,
                          strength=_gap_conviction(width_pct),
                          stop_loss=_atr_stop(candles, len(candles) - 1, price,
                                              PositionSide.LONG, self.atr_multiplier),
                          reason=f"리본 정배열 완성 (폭 {width_pct:.2f}%)")
        if bear_now and not bear_before:
            return Signal(action=SignalAction.ENTER_SHORT,
                          strength=_gap_conviction(width_pct),
                          stop_loss=_atr_stop(candles, len(candles) - 1, price,
                                              PositionSide.SHORT, self.atr_multiplier),
                          reason=f"리본 역배열 완성 (폭 {width_pct:.2f}%)")
        return Signal(reason="리본 미정렬")


@register_strategy("trix_cross")
class TrixCrossStrategy(Strategy):
    summary = "세 겹 평활한 TRIX가 0선을 넘으면 진입 — 노이즈에 가장 둔감한 추세 신호"
    category = "trend"
    description = """
TRIX 는 EMA 를 **세 번** 겹쳐 평활한 뒤 그 변화율만 읽는 지표다. 한 번의 평활을
통과할 때마다 노이즈가 걸러지므로, 세 겹을 통과하고도 남아 있는 방향은 웬만한
잔파동이 아니라는 뜻이다. MACD(두 EMA 의 차이)보다 한 층 더 보수적인 모멘텀
지표라고 보면 된다.

진입은 TRIX 가 0선을 넘는 순간이다 — 세 겹 평활선이 실제로 방향을 바꿨다는
확정 신호다. 그만큼 신호가 늦고 드물지만, 횡보의 잔신호는 구조적으로 거의
나오지 않는다. 이 파일의 헐 MA(가장 빠름)와 정확히 반대편 끝에 있는 선택이라,
순위표에서 둘을 비교하면 "빠름 vs 확실함"의 값이 드러난다.

확신도는 0선을 넘는 순간의 기울기 — 가파르게 뚫을수록 추세의 힘이 실렸다고
본다.

**강점**: 가짜 신호가 구조적으로 적다. 큰 추세는 놓치지 않는다.
**약점**: 세 겹 평활의 지연이 크다. 추세의 마지막 구간에 진입하는 일이 잦고,
짧은 추세는 신호가 나오기 전에 끝난다.
"""
    algorithm = """
**지표**  TRIX(15), ATR(14)

**진입**
- 롱: TRIX 가 0 이하 → 0 초과로 전환
- 숏: 0 이상 → 0 미만으로 전환

**청산**  TRIX 가 반대로 0선을 넘으면 청산.

**손절**  진입가 ∓ (ATR14 × 2.5) — 신호가 느린 만큼 손절은 넉넉히 둔다.

**확신도**  전환 봉의 TRIX 변화량 (기울기)
- 상위권일수록 올린다: |ΔTRIX| ≥ 0.02 → VERY_HIGH · ≥ 0.01 → HIGH ·
  ≥ 0.005 → MEDIUM · 그 외 LOW

**파라미터**  `period`, `atr_multiplier`
"""

    def setup(self) -> None:
        self.period = int(self.params.get("period", 15))
        self.atr_multiplier = float(self.params.get("atr_multiplier", 2.5))

    @property
    def warmup_candles(self) -> int:
        return self.period * 3 + 20

    def generate(self, ctx: StrategyContext) -> Signal:
        candles = ctx.closed_candles
        if len(candles) < self.warmup_candles:
            return Signal(reason="워밍업 부족")

        line = trix([c.close for c in candles], self.period)
        if line[-1] is None or line[-2] is None:
            return Signal(reason="지표 계산 불가")

        current, previous = line[-1], line[-2]
        price = candles[-1].close

        if ctx.position.side is PositionSide.LONG and current < 0:
            return Signal(action=SignalAction.EXIT, reason="TRIX 0선 하향")
        if ctx.position.side is PositionSide.SHORT and current > 0:
            return Signal(action=SignalAction.EXIT, reason="TRIX 0선 상향")
        if ctx.position.is_open:
            return Signal(reason="TRIX 방향 유지")

        delta = abs(current - previous)
        conviction = (
            Conviction.VERY_HIGH.value if delta >= 0.02
            else Conviction.HIGH.value if delta >= 0.01
            else Conviction.MEDIUM.value if delta >= 0.005
            else Conviction.LOW.value
        )
        if previous <= 0 < current:
            return Signal(action=SignalAction.ENTER_LONG, strength=conviction,
                          stop_loss=_atr_stop(candles, len(candles) - 1, price,
                                              PositionSide.LONG, self.atr_multiplier),
                          reason=f"TRIX 상향 전환 ({current:+.3f})")
        if previous >= 0 > current:
            return Signal(action=SignalAction.ENTER_SHORT, strength=conviction,
                          stop_loss=_atr_stop(candles, len(candles) - 1, price,
                                              PositionSide.SHORT, self.atr_multiplier),
                          reason=f"TRIX 하향 전환 ({current:+.3f})")
        return Signal(reason="0선 전환 없음")


@register_strategy("ao_cross")
class AwesomeOscillatorStrategy(Strategy):
    summary = "어썸 오실레이터(중간가 SMA5−SMA34)가 0선을 넘으면 진입"
    category = "trend"
    description = """
빌 윌리엄스의 어썸 오실레이터(AO)는 **종가가 아니라 중간가 (고+저)/2** 의 짧은
평균과 긴 평균의 차이를 본다. 종가는 봉 막판의 힘겨루기 결과 하나로 정해지지만,
중간가는 봉 전체 범위의 중심이라 세력의 '평균 체류 가격'에 가깝다 — 막판 스푸핑
성 움직임에 덜 속는다는 것이 이 지표의 주장이다.

진입은 AO 의 0선 교차다. 짧은 평균(5)이 긴 평균(34)을 넘었다는 뜻이므로 EMA
교차와 논리는 같지만, 재료(중간가)와 기간 조합(5/34)이 달라 신호의 결이 다르다.
macd_trend, ema_cross 와 순위표에서 나란히 비교해 보라고 넣었다.

확신도는 0선 돌파 직후의 AO 크기를 ATR 로 정규화해 잰다 — 얕게 걸친 돌파보다
확실히 넘어선 돌파가 이어질 가능성이 높다.

**강점**: 봉 막판 노이즈(종가 조작성 움직임)에 상대적으로 강하다.
**약점**: SMA 기반이라 EMA 계열보다 반응이 둔하다. 0선 근처 횡보에서는 여느
교차 지표처럼 잔신호를 낸다.
"""
    algorithm = """
**지표**  AO = SMA5(중간가) − SMA34(중간가), ATR(14)

**진입**
- 롱: AO 가 0 이하 → 0 초과로 전환
- 숏: 0 이상 → 0 미만으로 전환

**청산**  AO 가 반대로 0선을 넘으면 청산.

**손절**  진입가 ∓ (ATR14 × 2.0)

**확신도**  |AO| ÷ ATR14
- ≥ 0.6 → VERY_HIGH · ≥ 0.35 → HIGH · ≥ 0.15 → MEDIUM · 그 외 LOW

**파라미터**  `fast`, `slow`, `atr_multiplier`
"""

    def setup(self) -> None:
        self.fast = int(self.params.get("fast", 5))
        self.slow = int(self.params.get("slow", 34))
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

        line = awesome_oscillator(candles, self.fast, self.slow)
        atr_values = atr(candles, 14)
        if line[-1] is None or line[-2] is None or atr_values[-1] is None:
            return Signal(reason="지표 계산 불가")

        current, previous = line[-1], line[-2]
        price = candles[-1].close

        if ctx.position.side is PositionSide.LONG and current < 0:
            return Signal(action=SignalAction.EXIT, reason="AO 0선 하향")
        if ctx.position.side is PositionSide.SHORT and current > 0:
            return Signal(action=SignalAction.EXIT, reason="AO 0선 상향")
        if ctx.position.is_open:
            return Signal(reason="AO 방향 유지")

        ratio = abs(current) / atr_values[-1]
        conviction = (
            Conviction.VERY_HIGH.value if ratio >= 0.6
            else Conviction.HIGH.value if ratio >= 0.35
            else Conviction.MEDIUM.value if ratio >= 0.15
            else Conviction.LOW.value
        )
        if previous <= 0 < current:
            return Signal(action=SignalAction.ENTER_LONG, strength=conviction,
                          stop_loss=_atr_stop(candles, len(candles) - 1, price,
                                              PositionSide.LONG, self.atr_multiplier),
                          reason=f"AO 상향 교차 ({ratio:.2f} ATR)")
        if previous >= 0 > current:
            return Signal(action=SignalAction.ENTER_SHORT, strength=conviction,
                          stop_loss=_atr_stop(candles, len(candles) - 1, price,
                                              PositionSide.SHORT, self.atr_multiplier),
                          reason=f"AO 하향 교차 ({ratio:.2f} ATR)")
        return Signal(reason="0선 교차 없음")


@register_strategy("linreg_trend")
class LinregTrendStrategy(Strategy):
    summary = "최소제곱 회귀선의 기울기가 뚜렷해지면 진입, 잔차로 질을 판정"
    category = "trend"
    description = """
이동평균 대신 최근 40봉에 **최소제곱 직선**을 긋는다. 직선의 기울기가 추세의
방향과 속도이고, 가격이 직선에서 흩어진 정도(잔차 표준편차)가 추세의 '깨끗함'
이다. 같은 기울기라도 가격이 직선에 착 붙어 오르는 추세와 널뛰며 오르는 추세는
다르다 — 이동평균은 이 둘을 구분하지 못하지만 회귀는 한다.

진입은 **기울기 ÷ 잔차** (통계의 t 값과 비슷한 신호 대 잡음비)가 문턱을 넘는
순간이다. 기울기가 커서가 아니라 **잡음 대비** 커야 신호로 치는 것이 핵심이다.
조용히 꾸준한 추세는 작은 기울기로도 문턱을 넘고, 요동치는 장은 큰 기울기도
문턱에 못 미친다.

확신도 역시 신호 대 잡음비로 잰다. 청산은 기울기가 반대 부호로 돌아설 때다.

**강점**: 추세의 질(깨끗함)이 판단에 내장된다. 변동성이 큰 장에서 자동으로
보수적이 된다.
**약점**: 회귀 창(40봉)이 고정이라 창보다 긴 곡선 추세에서는 기울기가 평균화
된다. 창 안에 급변점이 있으면 직선이 둘 다 놓친다.
"""
    algorithm = """
**지표**  선형회귀(40봉): 기울기 b, 잔차 표준편차 σ, ATR(14)

**신호 대 잡음비**  snr = (b × 40) ÷ σ — 창 전체의 이동량을 잡음으로 나눈 값

**진입**
- 롱: snr 이 문턱(+2.0)을 상향 돌파 (직전 봉 ≤ 문턱, 이번 봉 > 문턱)
- 숏: snr 이 −2.0 을 하향 돌파

**청산**  기울기 부호가 반대로 바뀌면 청산.

**손절**  진입가 ∓ (ATR14 × 2.0)

**확신도**  |snr|
- ≥ 4 → VERY_HIGH · ≥ 3 → HIGH · ≥ 2.5 → MEDIUM · 그 외 LOW

**파라미터**  `period`, `entry_snr`, `atr_multiplier`
"""

    def setup(self) -> None:
        self.period = int(self.params.get("period", 40))
        self.entry_snr = float(self.params.get("entry_snr", 2.0))
        self.atr_multiplier = float(self.params.get("atr_multiplier", 2.0))

    @property
    def warmup_candles(self) -> int:
        return self.period + 20

    def _snr(self, closes) -> float | None:
        slope, _, sigma = linear_regression(closes, self.period)
        if slope[-1] is None or sigma[-1] is None or sigma[-1] == 0:
            return None
        return slope[-1] * self.period / sigma[-1]

    def generate(self, ctx: StrategyContext) -> Signal:
        candles = ctx.closed_candles
        if len(candles) < self.warmup_candles:
            return Signal(reason="워밍업 부족")

        closes = [c.close for c in candles]
        snr = self._snr(closes)
        snr_prev = self._snr(closes[:-1])
        if snr is None or snr_prev is None:
            return Signal(reason="지표 계산 불가")
        price = closes[-1]

        if ctx.position.side is PositionSide.LONG and snr < 0:
            return Signal(action=SignalAction.EXIT, reason="기울기 반전")
        if ctx.position.side is PositionSide.SHORT and snr > 0:
            return Signal(action=SignalAction.EXIT, reason="기울기 반전")
        if ctx.position.is_open:
            return Signal(reason=f"추세 유지 (snr {snr:+.1f})")

        conviction = (
            Conviction.VERY_HIGH.value if abs(snr) >= 4
            else Conviction.HIGH.value if abs(snr) >= 3
            else Conviction.MEDIUM.value if abs(snr) >= 2.5
            else Conviction.LOW.value
        )
        if snr_prev <= self.entry_snr < snr:
            return Signal(action=SignalAction.ENTER_LONG, strength=conviction,
                          stop_loss=_atr_stop(candles, len(candles) - 1, price,
                                              PositionSide.LONG, self.atr_multiplier),
                          reason=f"회귀 추세 점화 (snr {snr:+.1f})")
        if snr_prev >= -self.entry_snr > snr:
            return Signal(action=SignalAction.ENTER_SHORT, strength=conviction,
                          stop_loss=_atr_stop(candles, len(candles) - 1, price,
                                              PositionSide.SHORT, self.atr_multiplier),
                          reason=f"회귀 추세 점화 (snr {snr:+.1f})")
        return Signal(reason=f"신호 대 잡음비 부족 (snr {snr:+.1f})")
