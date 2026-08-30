"""평균회귀 전략.

공통된 전제: **가격이 평소 범위를 크게 벗어나면 되돌아온다.** 추세추종과 정반대의
믿음이라, 같은 시장에서 한쪽이 벌면 다른 쪽은 잃는다. 둘을 함께 백테스트하는
이유가 그것이다.

승률은 높은 편(60~70%)이지만, **지는 거래 한 번이 이긴 거래 여러 번을 지운다.**
추세가 시작되면 "더 싸졌으니 더 산다"가 되어 손실이 계속 커지기 때문이다.
그래서 이 계열에서는 손절을 지키는 것이 승률보다 중요하다.
"""

from __future__ import annotations

from bot.indicators import bollinger, ema, rsi
from bot.models import Conviction, PositionSide, Signal, SignalAction
from bot.strategies.base import Strategy, StrategyContext, register_strategy
from bot.strategies.trend import _atr_stop


@register_strategy("rsi_reversion")
class RsiReversionStrategy(Strategy):
    summary = "RSI 과매도에서 매수, 과매수에서 매도"
    category = "reversion"
    description = """
RSI 는 최근 상승폭과 하락폭의 비율을 0~100 으로 나타낸 값이다. 30 아래면 너무 많이
떨어졌다(과매도), 70 위면 너무 많이 올랐다(과매수)고 보는 것이 관례다.

이 전략은 RSI 가 30 아래로 내려갔다가 **다시 30 위로 올라오는 순간** 매수한다.
떨어지는 도중이 아니라 돌아서는 것을 확인하고 들어가는 것이 핵심이다 —
과매도는 더 과매도가 될 수 있기 때문이다. 청산은 RSI 가 50(중립)을 회복하면 한다.

RSI 가 극단일수록 확신을 올린다. 20 아래는 25~30 구간보다 되돌림이 클 가능성이
높다고 보는 것이다.

**강점**: 박스권에서 꾸준히 벌고 승률이 높다.
**약점**: 추세장에서 치명적이다. 하락 추세에서는 RSI 가 계속 30 아래에 머물고,
반등할 때마다 사서 계속 물린다. 손절이 없으면 계좌가 녹는 전형적인 패턴이다.
"""

    def setup(self) -> None:
        self.period = int(self.params.get("period", 14))
        self.oversold = float(self.params.get("oversold", 30))
        self.overbought = float(self.params.get("overbought", 70))
        self.exit_level = float(self.params.get("exit_level", 50))
        self.atr_multiplier = float(self.params.get("atr_multiplier", 1.5))

    @property
    def warmup_candles(self) -> int:
        return self.period + 30

    def generate(self, ctx: StrategyContext) -> Signal:
        candles = ctx.closed_candles
        if len(candles) < self.warmup_candles:
            return Signal(reason="워밍업 부족")

        values = rsi([c.close for c in candles], self.period)
        if values[-1] is None or values[-2] is None:
            return Signal(reason="지표 계산 불가")

        current, previous = values[-1], values[-2]
        price = candles[-1].close

        if ctx.position.side is PositionSide.LONG and current >= self.exit_level:
            return Signal(action=SignalAction.EXIT, reason=f"RSI {current:.0f} 중립 회복")
        if ctx.position.side is PositionSide.SHORT and current <= self.exit_level:
            return Signal(action=SignalAction.EXIT, reason=f"RSI {current:.0f} 중립 회복")
        if ctx.position.is_open:
            return Signal(reason=f"RSI {current:.0f} 회복 대기")

        # 떨어지는 도중이 아니라 '돌아서는 순간' 을 잡는다.
        if previous < self.oversold <= current:
            return Signal(
                action=SignalAction.ENTER_LONG,
                strength=_extreme_conviction(previous, oversold=True),
                stop_loss=_atr_stop(candles, len(candles) - 1, price,
                                    PositionSide.LONG, self.atr_multiplier),
                reason=f"RSI 과매도 탈출 ({previous:.0f} → {current:.0f})",
            )
        if previous > self.overbought >= current:
            return Signal(
                action=SignalAction.ENTER_SHORT,
                strength=_extreme_conviction(previous, oversold=False),
                stop_loss=_atr_stop(candles, len(candles) - 1, price,
                                    PositionSide.SHORT, self.atr_multiplier),
                reason=f"RSI 과매수 이탈 ({previous:.0f} → {current:.0f})",
            )
        return Signal(reason=f"RSI {current:.0f} 중립")


def _extreme_conviction(value: float, *, oversold: bool) -> float:
    """극단으로 갈수록 되돌림이 크다고 보고 확신을 올린다."""
    distance = (30 - value) if oversold else (value - 70)
    if distance >= 15:
        return Conviction.VERY_HIGH.value
    if distance >= 8:
        return Conviction.HIGH.value
    if distance >= 3:
        return Conviction.MEDIUM.value
    return Conviction.LOW.value


@register_strategy("bollinger_reversion")
class BollingerReversionStrategy(Strategy):
    summary = "볼린저밴드 밖으로 나간 가격이 안으로 돌아오면 진입"
    category = "reversion"
    description = """
가격이 볼린저밴드 하단 아래로 내려갔다가 **다시 밴드 안으로 복귀할 때** 매수한다.
밴드를 벗어난 것은 일시적인 과열이고 평균으로 돌아온다는 전제다. 청산은 중심선
(이동평균)에 닿으면 한다.

`bollinger_breakout` 과 같은 지표를 쓰지만 **정반대로 해석한다.** 그쪽은 밴드
이탈을 추세의 시작으로 보고 따라가고, 이쪽은 되돌림의 기회로 보고 반대로 간다.
두 전략의 백테스트 성적을 비교하면 이 시장의 성격이 드러난다.

**강점**: 박스권에서 안정적이다. 진입 지점이 명확하다.
**약점**: 밴드 폭이 넓어지는 국면(변동성 확대)에서는 계속 지는 쪽에 선다.
추세가 시작되면 밴드 자체가 따라 움직여서 "복귀" 신호가 늦게, 그것도 손실 상태로
나온다.
"""

    def setup(self) -> None:
        self.period = int(self.params.get("period", 20))
        self.deviations = float(self.params.get("deviations", 2.0))
        self.atr_multiplier = float(self.params.get("atr_multiplier", 1.5))

    @property
    def warmup_candles(self) -> int:
        return self.period + 20

    def generate(self, ctx: StrategyContext) -> Signal:
        candles = ctx.closed_candles
        if len(candles) < self.warmup_candles:
            return Signal(reason="워밍업 부족")

        closes = [c.close for c in candles]
        upper, middle, lower = bollinger(closes, self.period, self.deviations)
        if upper[-1] is None or middle[-1] is None or upper[-2] is None:
            return Signal(reason="지표 계산 불가")

        price, previous = closes[-1], closes[-2]

        if ctx.position.side is PositionSide.LONG and price >= middle[-1]:
            return Signal(action=SignalAction.EXIT, reason="중심선 도달")
        if ctx.position.side is PositionSide.SHORT and price <= middle[-1]:
            return Signal(action=SignalAction.EXIT, reason="중심선 도달")
        if ctx.position.is_open:
            return Signal(reason="중심선 복귀 대기")

        depth = lambda band, p: abs(band - p) / band * 100  # noqa: E731
        if previous < lower[-2] <= price:
            return Signal(
                action=SignalAction.ENTER_LONG,
                strength=_gap_band_conviction(depth(lower[-2], previous)),
                stop_loss=_atr_stop(candles, len(candles) - 1, price,
                                    PositionSide.LONG, self.atr_multiplier),
                reason="하단 밴드 복귀",
            )
        if previous > upper[-2] >= price:
            return Signal(
                action=SignalAction.ENTER_SHORT,
                strength=_gap_band_conviction(depth(upper[-2], previous)),
                stop_loss=_atr_stop(candles, len(candles) - 1, price,
                                    PositionSide.SHORT, self.atr_multiplier),
                reason="상단 밴드 복귀",
            )
        return Signal(reason="밴드 안")


def _gap_band_conviction(depth_pct: float) -> float:
    """밴드를 깊게 벗어났을수록 되돌림이 크다고 본다."""
    if depth_pct >= 1.0:
        return Conviction.VERY_HIGH.value
    if depth_pct >= 0.5:
        return Conviction.HIGH.value
    if depth_pct >= 0.2:
        return Conviction.MEDIUM.value
    return Conviction.LOW.value


@register_strategy("grid")
class GridStrategy(Strategy):
    summary = "기준선에서 일정 간격 벌어지면 반대로 진입 (횡보장 전용)"
    category = "range"
    description = """
이동평균을 기준선으로 두고, 가격이 거기서 일정 비율(기본 1%) 아래로 내려가면
매수, 위로 올라가면 매도한다. 기준선으로 돌아오면 청산한다. 박스권에서 오르내림을
반복적으로 먹는 구조다.

⚠️ **이 전략은 추세장에서 계좌를 날릴 수 있다.** 한 방향으로 계속 밀리면
"더 싸졌으니 더 산다"가 반복되면서 손실이 누적되는데, 레버리지가 걸린 선물에서는
청산까지 간다. 실제로 그리드 봇으로 손실을 본 사례 대부분이 이 경우다.

여기서는 **손절을 반드시 걸고, 한 번에 한 포지션만** 잡도록 제한했다. 흔히 말하는
"그리드"는 여러 층으로 분할 진입하는데, 그 방식은 손실이 무한정 커질 수 있어
넣지 않았다.

**강점**: 방향을 맞출 필요가 없다. 횡보장에서 거래 횟수가 많다.
**약점**: 추세장에서 위험하다. 거래가 잦아 수수료 부담이 크다.
"""

    def setup(self) -> None:
        self.period = int(self.params.get("period", 50))
        self.step_pct = float(self.params.get("step_pct", 1.0))
        self.stop_pct = float(self.params.get("stop_pct", 3.0))

    @property
    def warmup_candles(self) -> int:
        return self.period + 10

    def generate(self, ctx: StrategyContext) -> Signal:
        candles = ctx.closed_candles
        if len(candles) < self.warmup_candles:
            return Signal(reason="워밍업 부족")

        baseline = ema([c.close for c in candles], self.period)
        if baseline[-1] is None:
            return Signal(reason="지표 계산 불가")

        price = candles[-1].close
        deviation = (price - baseline[-1]) / baseline[-1] * 100

        if ctx.position.side is PositionSide.LONG and deviation >= 0:
            return Signal(action=SignalAction.EXIT, reason="기준선 회복")
        if ctx.position.side is PositionSide.SHORT and deviation <= 0:
            return Signal(action=SignalAction.EXIT, reason="기준선 회복")
        if ctx.position.is_open:
            return Signal(reason=f"기준선 대비 {deviation:+.2f}%")

        steps = abs(deviation) / self.step_pct
        if steps < 1:
            return Signal(reason=f"기준선 근처 ({deviation:+.2f}%)")

        conviction = (
            Conviction.VERY_HIGH.value if steps >= 3
            else Conviction.HIGH.value if steps >= 2
            else Conviction.MEDIUM.value if steps >= 1.5
            else Conviction.LOW.value
        )
        # 추세장에서 손실이 무한정 커지지 않도록 손절을 반드시 건다.
        if deviation < 0:
            return Signal(action=SignalAction.ENTER_LONG, strength=conviction,
                          stop_loss=price * (1 - self.stop_pct / 100),
                          reason=f"기준선 아래 {deviation:.2f}%")
        return Signal(action=SignalAction.ENTER_SHORT, strength=conviction,
                      stop_loss=price * (1 + self.stop_pct / 100),
                      reason=f"기준선 위 +{deviation:.2f}%")
