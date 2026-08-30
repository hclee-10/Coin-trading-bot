"""오실레이터 기반 평균회귀 전략.

RSI 외에도 과열/침체를 재는 오실레이터는 여럿 있고, 각각 다른 것을 본다 —
스토캐스틱은 **범위 안에서의 위치**, CCI 는 **평균에서 벗어난 배수**,
윌리엄스 %R 은 스토캐스틱의 거울상, MFI 는 **거래량까지 가중한** RSI 다.

같은 평균회귀라도 어떤 오실레이터가 이 시장·이 봉주기에 잘 맞는지는 돌려봐야
안다. 그래서 일부러 결이 다른 네 가지를 나란히 등록해 두었다 — 모의매매
순위표에서 서로 비교하는 것이 이 파일의 존재 이유다.

공통 원칙은 reversion.py 와 같다: **떨어지는 칼날을 잡지 않는다.** 극단에
있는 동안이 아니라, 극단에서 **빠져나오는 순간**에 진입한다.
"""

from __future__ import annotations

from bot.indicators import cci, mfi, stochastic, williams_r
from bot.models import Conviction, PositionSide, Signal, SignalAction
from bot.strategies.base import Strategy, StrategyContext, register_strategy
from bot.strategies.trend import _atr_stop


@register_strategy("stochastic_reversion")
class StochasticReversionStrategy(Strategy):
    summary = "스토캐스틱 %K가 과매도 구간을 벗어나면 매수"
    category = "reversion"
    description = """
스토캐스틱은 "최근 N봉의 고가~저가 범위에서 지금 종가가 어디쯤인가"를 0~100 으로
나타낸다. 20 아래면 범위의 바닥권(과매도), 80 위면 천장권(과매수)이다.

RSI 와 비슷해 보이지만 보는 것이 다르다 — RSI 는 상승폭과 하락폭의 **비율**을,
스토캐스틱은 범위 안에서의 **위치**를 본다. 그래서 스토캐스틱이 더 예민하게
움직이고 극단에 더 자주 닿는다. 신호가 많은 대신 개별 신호의 질은 떨어진다.

진입은 %K 가 20 아래로 갔다가 **다시 20 위로 올라오는 순간**이다. 바닥권에
머무는 동안이 아니라 빠져나올 때를 잡는다. %K 와 %D(평활선)가 정배열이면,
즉 %K 가 %D 위에 있으면 반등이 이미 시작됐다는 뜻이라 확신을 한 단계 올린다.

**강점**: 박스권에서 신호가 잦고 진입가가 좋다.
**약점**: 추세장에서는 바닥권/천장권에 계속 붙어 있어 역방향 신호를 반복해서
낸다. RSI 계열보다도 예민해서 수수료 부담이 크다.
"""
    algorithm = """
**지표**  스토캐스틱(14, 3, 3), ATR(14)

**진입**  극단 구간을 **빠져나오는 순간**을 잡는다.
- 롱: 직전 %K < 20 이고 이번 %K ≥ 20
- 숏: 직전 %K > 80 이고 이번 %K ≤ 80

**청산**  %K 가 50(중립)에 도달하면 청산.

**손절**  진입가 ∓ (ATR14 × 1.5)

**확신도**  직전 %K 가 얼마나 극단이었는지 + %K/%D 정배열 여부
- 극단 깊이 ≥ 12 이고 %K > %D → VERY_HIGH
- 극단 깊이 ≥ 12 또는 %K > %D → HIGH
- 극단 깊이 ≥ 5 → MEDIUM · 그 외 LOW

**파라미터**  `k_period`, `smooth_k`, `d_period`, `oversold`, `overbought`, `atr_multiplier`
"""

    def setup(self) -> None:
        self.k_period = int(self.params.get("k_period", 14))
        self.smooth_k = int(self.params.get("smooth_k", 3))
        self.d_period = int(self.params.get("d_period", 3))
        self.oversold = float(self.params.get("oversold", 20))
        self.overbought = float(self.params.get("overbought", 80))
        self.atr_multiplier = float(self.params.get("atr_multiplier", 1.5))

    @property
    def warmup_candles(self) -> int:
        return self.k_period + self.smooth_k + self.d_period + 20

    def generate(self, ctx: StrategyContext) -> Signal:
        candles = ctx.closed_candles
        if len(candles) < self.warmup_candles:
            return Signal(reason="워밍업 부족")

        k, d = stochastic(candles, self.k_period, self.smooth_k, self.d_period)
        if k[-1] is None or k[-2] is None or d[-1] is None:
            return Signal(reason="지표 계산 불가")

        current, previous = k[-1], k[-2]
        price = candles[-1].close

        if ctx.position.side is PositionSide.LONG and current >= 50:
            return Signal(action=SignalAction.EXIT, reason=f"%K {current:.0f} 중립 회복")
        if ctx.position.side is PositionSide.SHORT and current <= 50:
            return Signal(action=SignalAction.EXIT, reason=f"%K {current:.0f} 중립 회복")
        if ctx.position.is_open:
            return Signal(reason=f"%K {current:.0f} 회복 대기")

        if previous < self.oversold <= current:
            depth = self.oversold - previous
            return Signal(
                action=SignalAction.ENTER_LONG,
                strength=_stoch_conviction(depth, aligned=current > d[-1]),
                stop_loss=_atr_stop(candles, len(candles) - 1, price,
                                    PositionSide.LONG, self.atr_multiplier),
                reason=f"과매도 탈출 (%K {previous:.0f} → {current:.0f})",
            )
        if previous > self.overbought >= current:
            depth = previous - self.overbought
            return Signal(
                action=SignalAction.ENTER_SHORT,
                strength=_stoch_conviction(depth, aligned=current < d[-1]),
                stop_loss=_atr_stop(candles, len(candles) - 1, price,
                                    PositionSide.SHORT, self.atr_multiplier),
                reason=f"과매수 이탈 (%K {previous:.0f} → {current:.0f})",
            )
        return Signal(reason=f"%K {current:.0f} 중립")


def _stoch_conviction(depth: float, *, aligned: bool) -> float:
    """극단 깊이와 %K/%D 정배열을 함께 본다."""
    if depth >= 12 and aligned:
        return Conviction.VERY_HIGH.value
    if depth >= 12 or aligned:
        return Conviction.HIGH.value
    if depth >= 5:
        return Conviction.MEDIUM.value
    return Conviction.LOW.value


@register_strategy("cci_reversion")
class CciReversionStrategy(Strategy):
    summary = "CCI가 ±100 밖으로 나갔다 돌아오면 반대편으로 진입"
    category = "reversion"
    description = """
CCI(상품채널지수)는 대표가(고+저+종의 평균)가 자기 이동평균에서 **평균편차의
몇 배** 벗어났는지를 잰다. ±100 안이 정상 범위이고, 그 밖은 통계적으로 드문
영역이라는 뜻이다.

볼린저밴드와 발상이 비슷하지만 편차를 표준편차가 아니라 평균편차로 재기 때문에
튀는 값(급등락 한 봉)에 덜 휘둘린다. 또 종가가 아니라 대표가를 쓰므로 꼬리가
긴 봉의 정보도 반영된다.

진입은 -100 아래로 내려갔다가 **다시 -100 위로 복귀하는 순간** 매수하는
식이다. 극단에 있는 동안이 아니라 돌아설 때를 잡는 것은 다른 평균회귀와 같다.
청산은 CCI 가 0(평균 복귀)에 도달하면 한다.

**강점**: 정상/비정상 범위의 기준(±100)이 통계적으로 정의되어 자의성이 적다.
**약점**: 강한 추세에서는 CCI 가 ±100 밖에 오래 머물며 복귀 신호가 반복적으로
틀린다. 평균회귀 공통의 약점이다.
"""
    algorithm = """
**지표**  CCI(20), ATR(14)

**진입**  ±100 밖으로 나갔다가 **복귀하는 순간**.
- 롱: 직전 CCI < -100 이고 이번 CCI ≥ -100
- 숏: 직전 CCI > +100 이고 이번 CCI ≤ +100

**청산**  CCI 가 0 을 넘어서면(평균 복귀) 청산.

**손절**  진입가 ∓ (ATR14 × 1.5)

**확신도**  직전 CCI 가 임계선에서 벗어난 깊이
- ≥ 100 (CCI ±200 밖) → VERY_HIGH · ≥ 50 → HIGH · ≥ 20 → MEDIUM · 그 외 LOW

**파라미터**  `period`, `threshold`, `atr_multiplier`
"""

    def setup(self) -> None:
        self.period = int(self.params.get("period", 20))
        self.threshold = float(self.params.get("threshold", 100))
        self.atr_multiplier = float(self.params.get("atr_multiplier", 1.5))

    @property
    def warmup_candles(self) -> int:
        return self.period + 30

    def generate(self, ctx: StrategyContext) -> Signal:
        candles = ctx.closed_candles
        if len(candles) < self.warmup_candles:
            return Signal(reason="워밍업 부족")

        values = cci(candles, self.period)
        if values[-1] is None or values[-2] is None:
            return Signal(reason="지표 계산 불가")

        current, previous = values[-1], values[-2]
        price = candles[-1].close

        if ctx.position.side is PositionSide.LONG and current >= 0:
            return Signal(action=SignalAction.EXIT, reason=f"CCI {current:.0f} 평균 복귀")
        if ctx.position.side is PositionSide.SHORT and current <= 0:
            return Signal(action=SignalAction.EXIT, reason=f"CCI {current:.0f} 평균 복귀")
        if ctx.position.is_open:
            return Signal(reason=f"CCI {current:.0f} 복귀 대기")

        if previous < -self.threshold <= current:
            return Signal(
                action=SignalAction.ENTER_LONG,
                strength=_cci_conviction(-self.threshold - previous),
                stop_loss=_atr_stop(candles, len(candles) - 1, price,
                                    PositionSide.LONG, self.atr_multiplier),
                reason=f"CCI 침체 복귀 ({previous:.0f} → {current:.0f})",
            )
        if previous > self.threshold >= current:
            return Signal(
                action=SignalAction.ENTER_SHORT,
                strength=_cci_conviction(previous - self.threshold),
                stop_loss=_atr_stop(candles, len(candles) - 1, price,
                                    PositionSide.SHORT, self.atr_multiplier),
                reason=f"CCI 과열 복귀 ({previous:.0f} → {current:.0f})",
            )
        return Signal(reason=f"CCI {current:.0f} 정상 범위")


def _cci_conviction(depth: float) -> float:
    if depth >= 100:
        return Conviction.VERY_HIGH.value
    if depth >= 50:
        return Conviction.HIGH.value
    if depth >= 20:
        return Conviction.MEDIUM.value
    return Conviction.LOW.value


@register_strategy("williams_reversion")
class WilliamsReversionStrategy(Strategy):
    summary = "윌리엄스 %R이 -80 아래서 올라오면 매수, -20 위에서 내려오면 매도"
    category = "reversion"
    description = """
윌리엄스 %R 은 스토캐스틱 %K 를 뒤집은 지표다 — "최근 범위의 **천장에서** 얼마나
떨어져 있나"를 0 ~ -100 으로 나타낸다. -80 아래면 바닥권, -20 위면 천장권이다.

스토캐스틱과 수학적으로는 거울상이지만, 이 전략은 평활을 전혀 하지 않은 원값을
그대로 쓴다는 점이 다르다. 스토캐스틱 %K 는 3봉 평활을 거쳐 부드럽지만 반 박자
늦고, %R 은 거칠지만 즉각적이다. **같은 되돌림을 %R 이 먼저 잡고 스토캐스틱이
확인한다** — 두 전략을 순위표에서 비교하면 이 시장에서 예민함과 안정성 중 어느
쪽이 이득인지 드러난다.

진입은 -80 아래로 갔다가 다시 위로 올라오는 순간 매수, -20 위에서 내려오는 순간
매도다. 청산은 중앙(-50)을 회복하면 한다.

**강점**: 지연이 거의 없다. 짧은 되돌림도 잡는다.
**약점**: 평활이 없어 노이즈에 그대로 노출된다. 잦은 신호 → 잦은 수수료.
"""
    algorithm = """
**지표**  윌리엄스 %R(14), ATR(14)

**진입**
- 롱: 직전 %R < -80 이고 이번 %R ≥ -80
- 숏: 직전 %R > -20 이고 이번 %R ≤ -20

**청산**  %R 이 -50(중앙)을 넘어서면 청산.

**손절**  진입가 ∓ (ATR14 × 1.5)

**확신도**  직전 %R 의 극단 깊이 (임계선에서 벗어난 정도)
- ≥ 15 → VERY_HIGH · ≥ 8 → HIGH · ≥ 3 → MEDIUM · 그 외 LOW

**파라미터**  `period`, `oversold`(-80), `overbought`(-20), `atr_multiplier`
"""

    def setup(self) -> None:
        self.period = int(self.params.get("period", 14))
        self.oversold = float(self.params.get("oversold", -80))
        self.overbought = float(self.params.get("overbought", -20))
        self.atr_multiplier = float(self.params.get("atr_multiplier", 1.5))

    @property
    def warmup_candles(self) -> int:
        return self.period + 30

    def generate(self, ctx: StrategyContext) -> Signal:
        candles = ctx.closed_candles
        if len(candles) < self.warmup_candles:
            return Signal(reason="워밍업 부족")

        values = williams_r(candles, self.period)
        if values[-1] is None or values[-2] is None:
            return Signal(reason="지표 계산 불가")

        current, previous = values[-1], values[-2]
        price = candles[-1].close

        if ctx.position.side is PositionSide.LONG and current >= -50:
            return Signal(action=SignalAction.EXIT, reason=f"%R {current:.0f} 중앙 회복")
        if ctx.position.side is PositionSide.SHORT and current <= -50:
            return Signal(action=SignalAction.EXIT, reason=f"%R {current:.0f} 중앙 회복")
        if ctx.position.is_open:
            return Signal(reason=f"%R {current:.0f} 회복 대기")

        if previous < self.oversold <= current:
            return Signal(
                action=SignalAction.ENTER_LONG,
                strength=_williams_conviction(self.oversold - previous),
                stop_loss=_atr_stop(candles, len(candles) - 1, price,
                                    PositionSide.LONG, self.atr_multiplier),
                reason=f"%R 바닥권 탈출 ({previous:.0f} → {current:.0f})",
            )
        if previous > self.overbought >= current:
            return Signal(
                action=SignalAction.ENTER_SHORT,
                strength=_williams_conviction(previous - self.overbought),
                stop_loss=_atr_stop(candles, len(candles) - 1, price,
                                    PositionSide.SHORT, self.atr_multiplier),
                reason=f"%R 천장권 이탈 ({previous:.0f} → {current:.0f})",
            )
        return Signal(reason=f"%R {current:.0f} 중립")


def _williams_conviction(depth: float) -> float:
    if depth >= 15:
        return Conviction.VERY_HIGH.value
    if depth >= 8:
        return Conviction.HIGH.value
    if depth >= 3:
        return Conviction.MEDIUM.value
    return Conviction.LOW.value


@register_strategy("mfi_reversion")
class MfiReversionStrategy(Strategy):
    summary = "거래량 가중 RSI(MFI)가 과매도를 벗어나면 매수"
    category = "reversion"
    description = """
MFI(자금흐름지수)는 RSI 와 같은 발상이지만 가격 변화에 **거래량을 곱해서** 잰다.
가격이 조금 올라도 거래량이 크면 큰 매수 압력으로, 크게 올라도 거래량이 없으면
약한 압력으로 계산된다. "돈이 실제로 어느 방향으로 흐르는가"를 본다고 해서
자금흐름지수다.

RSI 과매도는 "가격이 많이 빠졌다"는 뜻이지만, MFI 과매도는 "**거래량이 실린
투매가 있었다**"는 뜻이다. 투매 후의 반등이 단순 하락 후의 반등보다 신뢰도가
높다는 것이 이 전략의 전제다. rsi_reversion 과 순위표에서 나란히 비교하면
거래량 정보가 실제로 값어치를 하는지 확인할 수 있다.

진입·청산 구조는 rsi_reversion 과 같다: 20 아래로 갔다가 올라오는 순간 매수,
80 위에서 내려오는 순간 매도, 50 회복 시 청산.

**강점**: 거래량 없는 가짜 급락에 속지 않는다.
**약점**: 거래량 데이터가 부실한 종목·시간대에서는 RSI 보다 나을 게 없다.
추세장에 약한 것은 평균회귀 공통이다.
"""
    algorithm = """
**지표**  MFI(14) — 대표가 × 거래량의 상승/하락 비율, ATR(14)

**진입**
- 롱: 직전 MFI < 20 이고 이번 MFI ≥ 20 (투매 후 회복)
- 숏: 직전 MFI > 80 이고 이번 MFI ≤ 80 (과열 후 이탈)

**청산**  MFI 가 50(중립)에 도달하면 청산.

**손절**  진입가 ∓ (ATR14 × 1.5)

**확신도**  직전 MFI 의 극단 깊이
- ≥ 12 → VERY_HIGH · ≥ 6 → HIGH · ≥ 2 → MEDIUM · 그 외 LOW

**파라미터**  `period`, `oversold`, `overbought`, `atr_multiplier`
"""

    def setup(self) -> None:
        self.period = int(self.params.get("period", 14))
        self.oversold = float(self.params.get("oversold", 20))
        self.overbought = float(self.params.get("overbought", 80))
        self.atr_multiplier = float(self.params.get("atr_multiplier", 1.5))

    @property
    def warmup_candles(self) -> int:
        return self.period + 30

    def generate(self, ctx: StrategyContext) -> Signal:
        candles = ctx.closed_candles
        if len(candles) < self.warmup_candles:
            return Signal(reason="워밍업 부족")

        values = mfi(candles, self.period)
        if values[-1] is None or values[-2] is None:
            return Signal(reason="지표 계산 불가")

        current, previous = values[-1], values[-2]
        price = candles[-1].close

        if ctx.position.side is PositionSide.LONG and current >= 50:
            return Signal(action=SignalAction.EXIT, reason=f"MFI {current:.0f} 중립 회복")
        if ctx.position.side is PositionSide.SHORT and current <= 50:
            return Signal(action=SignalAction.EXIT, reason=f"MFI {current:.0f} 중립 회복")
        if ctx.position.is_open:
            return Signal(reason=f"MFI {current:.0f} 회복 대기")

        if previous < self.oversold <= current:
            return Signal(
                action=SignalAction.ENTER_LONG,
                strength=_mfi_conviction(self.oversold - previous),
                stop_loss=_atr_stop(candles, len(candles) - 1, price,
                                    PositionSide.LONG, self.atr_multiplier),
                reason=f"MFI 투매 회복 ({previous:.0f} → {current:.0f})",
            )
        if previous > self.overbought >= current:
            return Signal(
                action=SignalAction.ENTER_SHORT,
                strength=_mfi_conviction(previous - self.overbought),
                stop_loss=_atr_stop(candles, len(candles) - 1, price,
                                    PositionSide.SHORT, self.atr_multiplier),
                reason=f"MFI 과열 이탈 ({previous:.0f} → {current:.0f})",
            )
        return Signal(reason=f"MFI {current:.0f} 중립")


def _mfi_conviction(depth: float) -> float:
    if depth >= 12:
        return Conviction.VERY_HIGH.value
    if depth >= 6:
        return Conviction.HIGH.value
    if depth >= 2:
        return Conviction.MEDIUM.value
    return Conviction.LOW.value
