"""지표 조합 전략.

지표 하나는 한 가지밖에 못 본다 — 추세 지표는 타이밍을 모르고, 오실레이터는
방향을 모르고, 가격 지표는 거래량을 모른다. 서로 다른 것을 보는 지표를 겹치면
한쪽의 가짜 신호를 다른 쪽이 걸러 준다.

대가는 언제나 같다: **신호 수가 준다.** 조건이 늘수록 진입 기회는 곱으로
줄어들므로, 모의매매 성적을 볼 때 거래 횟수가 충분한지부터 확인해야 한다.
"""

from __future__ import annotations

from bot.indicators import ema, macd, obv, rsi, sma, stochastic
from bot.models import Conviction, PositionSide, Signal, SignalAction
from bot.strategies.base import Strategy, StrategyContext, register_strategy
from bot.strategies.trend import _atr_stop, _gap_conviction


@register_strategy("macd_rsi")
class MacdRsiStrategy(Strategy):
    summary = "MACD 전환 신호를 RSI로 검증해서 과열 추격을 걸러낸다"
    category = "combo"
    description = """
macd_trend 의 가장 흔한 실패 유형은 **이미 오를 만큼 오른 지점에서의 매수 신호**다.
히스토그램이 0선을 넘는 시점은 모멘텀이 확인된 시점이지만, 급등 후반에도 같은
신호가 나온다 — 그때 들어가면 꼭대기를 산다.

이 전략은 MACD 신호에 RSI 필터를 겹친다. 히스토그램이 상향 전환해도 **RSI 가
이미 65 를 넘어 과열이면 매수하지 않는다.** 하향 전환 매도도 마찬가지로 RSI 35
아래의 과매도 상태면 걸러낸다. "모멘텀은 붙었지만 아직 과열은 아닌" 구간만
골라 타는 것이다.

확신도도 RSI 의 여유분으로 잰다 — RSI 50 근처에서 나온 전환(위로 갈 방이 많음)이
64 에서 나온 전환(방이 거의 없음)보다 낫다고 본다.

**강점**: 추격 매수/투매 추종이라는 모멘텀 전략의 최악수를 구조적으로 피한다.
**약점**: 강한 추세 초입엔 RSI 가 이미 과열권이라 **좋은 진입도 함께 걸러진다.**
필터는 언제나 양날이다 — macd_trend 와 순위표에서 비교해 값어치를 확인하라.
"""
    algorithm = """
**지표**  MACD(12, 26, 9), RSI(14), ATR(14)

**진입**  MACD 히스토그램 전환 **그리고** RSI 여유 확인.
- 롱: 히스토그램 0 이하 → 초과 전환, 그리고 RSI < 65
- 숏: 히스토그램 0 이상 → 미만 전환, 그리고 RSI > 35

**청산**  히스토그램이 반대로 전환되면 청산.

**손절**  진입가 ∓ (ATR14 × 2.0)

**확신도**  RSI 의 남은 방 (롱은 `65 − RSI`, 숏은 `RSI − 35`)
- ≥ 20 → VERY_HIGH · ≥ 12 → HIGH · ≥ 5 → MEDIUM · 그 외 LOW

**파라미터**  `fast`, `slow`, `signal`, `rsi_period`, `rsi_ceiling`, `rsi_floor`, `atr_multiplier`
"""

    def setup(self) -> None:
        self.fast = int(self.params.get("fast", 12))
        self.slow = int(self.params.get("slow", 26))
        self.signal_period = int(self.params.get("signal", 9))
        self.rsi_period = int(self.params.get("rsi_period", 14))
        self.rsi_ceiling = float(self.params.get("rsi_ceiling", 65))
        self.rsi_floor = float(self.params.get("rsi_floor", 35))
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
        momentum = rsi(closes, self.rsi_period)
        if histogram[-1] is None or histogram[-2] is None or momentum[-1] is None:
            return Signal(reason="지표 계산 불가")

        crossed_up = histogram[-2] <= 0 < histogram[-1]
        crossed_down = histogram[-2] >= 0 > histogram[-1]
        price = closes[-1]
        current_rsi = momentum[-1]

        if ctx.position.side is PositionSide.LONG and crossed_down:
            return Signal(action=SignalAction.EXIT, reason="모멘텀 하락 전환")
        if ctx.position.side is PositionSide.SHORT and crossed_up:
            return Signal(action=SignalAction.EXIT, reason="모멘텀 상승 전환")
        if ctx.position.is_open:
            return Signal(reason="모멘텀 유지")

        if crossed_up:
            if current_rsi >= self.rsi_ceiling:
                return Signal(reason=f"상향 전환이나 RSI {current_rsi:.0f} 과열 — 추격 안 함")
            return Signal(
                action=SignalAction.ENTER_LONG,
                strength=_headroom_conviction(self.rsi_ceiling - current_rsi),
                stop_loss=_atr_stop(candles, len(candles) - 1, price,
                                    PositionSide.LONG, self.atr_multiplier),
                reason=f"MACD 상향 전환 + RSI {current_rsi:.0f} 여유",
            )
        if crossed_down:
            if current_rsi <= self.rsi_floor:
                return Signal(reason=f"하향 전환이나 RSI {current_rsi:.0f} 과매도 — 투매 추종 안 함")
            return Signal(
                action=SignalAction.ENTER_SHORT,
                strength=_headroom_conviction(current_rsi - self.rsi_floor),
                stop_loss=_atr_stop(candles, len(candles) - 1, price,
                                    PositionSide.SHORT, self.atr_multiplier),
                reason=f"MACD 하향 전환 + RSI {current_rsi:.0f} 여유",
            )
        return Signal(reason="전환 없음")


def _headroom_conviction(headroom: float) -> float:
    """RSI 가 갈 수 있는 방이 많이 남았을수록 확신을 올린다."""
    if headroom >= 20:
        return Conviction.VERY_HIGH.value
    if headroom >= 12:
        return Conviction.HIGH.value
    if headroom >= 5:
        return Conviction.MEDIUM.value
    return Conviction.LOW.value


@register_strategy("stochastic_pullback")
class StochasticPullbackStrategy(Strategy):
    summary = "장기 추세 방향으로, 스토캐스틱 눌림이 풀릴 때만 진입"
    category = "combo"
    description = """
trend_pullback 과 같은 뼈대(장기 추세 필터 + 눌림목 타이밍)에서 타이밍 지표만
RSI 대신 스토캐스틱으로 바꾼 변형이다. EMA100 위에서는 매수만, 아래에서는 매도만
하되, 아무 때나 들어가지 않고 스토캐스틱이 눌림권(%K 30 아래)에 있는 동안
**%K 가 %D 를 위로 교차하는 순간** — 반등이 막 시작되는 지점 — 을 기다린다.

왜 굳이 변형을 두나 — 스토캐스틱은 **범위 내 위치**를 재기 때문에 RSI 보다 눌림에
훨씬 자주, 깊게 닿는다. 상승 추세 중의 얕은 조정에서 RSI 는 40 근처까지밖에 안
내려와 신호를 놓치지만 스토캐스틱은 20 아래까지 내려와 잡아낸다. 즉 이 변형은
trend_pullback 보다 **거래 기회가 많은 대신 개별 신호의 질이 낮다.** 순위표에서
둘을 비교하면 이 시장의 조정 깊이에 어느 쪽 자가 맞는지 드러난다.

트리거를 "눌림권을 벗어나는 순간"이 아니라 "눌림권 **안에서의** K/D 교차"로 잡은
것도 의도적이다 — 깊은 조정에서는 %K 가 바닥에 오래 머무는데, 그동안의 첫 반등
시도(K/D 교차)를 잡아야 눌림권 탈출을 기다리는 것보다 진입가가 좋다.

**강점**: 추세를 거스르지 않으면서 trend_pullback 보다 기회가 많다.
**약점**: 예민한 만큼 얕은 반등에도 진입해 잔손실이 는다. 추세 필터가 꺾이는
전환점에서는 어느 변형이든 약하다.
"""
    algorithm = """
**지표**  EMA(100) 으로 방향, 스토캐스틱(14, 3, 3) 으로 타이밍, ATR(14)

**진입**  세 조건이 **모두** 맞아야 한다.
- 롱: 종가 > EMA100 (상승 추세), %K < 30 (눌림권), %K 가 %D 를 상향 교차
- 숏: 종가 < EMA100 (하락 추세), %K > 70 (반등권), %K 가 %D 를 하향 교차

**청산**  %K 가 반대 극단(롱 75 / 숏 25)에 닿거나 추세(EMA100)가 꺾이면 청산.

**손절**  진입가 ∓ (ATR14 × 1.5)

**확신도**  가격과 EMA100 의 거리 — trend_pullback 과 같은 기준
- ≥ 3% → VERY_HIGH · ≥ 1.5% → HIGH · ≥ 0.5% → MEDIUM · 그 외 LOW

**파라미터**  `trend_period`, `k_period`, `pullback_level`, `exit_level`, `atr_multiplier`
"""

    def setup(self) -> None:
        self.trend_period = int(self.params.get("trend_period", 100))
        self.k_period = int(self.params.get("k_period", 14))
        self.pullback_level = float(self.params.get("pullback_level", 30))
        self.exit_level = float(self.params.get("exit_level", 75))
        self.atr_multiplier = float(self.params.get("atr_multiplier", 1.5))

    @property
    def warmup_candles(self) -> int:
        return self.trend_period + 30

    def generate(self, ctx: StrategyContext) -> Signal:
        candles = ctx.closed_candles
        if len(candles) < self.warmup_candles:
            return Signal(reason="워밍업 부족")

        closes = [c.close for c in candles]
        trend = ema(closes, self.trend_period)
        k, d = stochastic(candles, self.k_period)
        if (trend[-1] is None or k[-1] is None or k[-2] is None
                or d[-1] is None or d[-2] is None):
            return Signal(reason="지표 계산 불가")

        price = closes[-1]
        uptrend = price > trend[-1]
        current, previous = k[-1], k[-2]

        if ctx.position.side is PositionSide.LONG:
            if current >= self.exit_level or not uptrend:
                return Signal(action=SignalAction.EXIT,
                              reason="목표 도달" if current >= self.exit_level else "추세 이탈")
            return Signal(reason=f"%K {current:.0f} 상승 대기")
        if ctx.position.side is PositionSide.SHORT:
            if current <= (100 - self.exit_level) or uptrend:
                return Signal(action=SignalAction.EXIT, reason="목표 도달 또는 추세 이탈")
            return Signal(reason=f"%K {current:.0f} 하락 대기")

        distance_pct = abs(price - trend[-1]) / trend[-1] * 100
        conviction = (
            Conviction.VERY_HIGH.value if distance_pct >= 3
            else Conviction.HIGH.value if distance_pct >= 1.5
            else Conviction.MEDIUM.value if distance_pct >= 0.5
            else Conviction.LOW.value
        )
        crossed_up = previous <= d[-2] and current > d[-1]
        crossed_down = previous >= d[-2] and current < d[-1]
        if uptrend and current < self.pullback_level and crossed_up:
            return Signal(action=SignalAction.ENTER_LONG, strength=conviction,
                          stop_loss=_atr_stop(candles, len(candles) - 1, price,
                                              PositionSide.LONG, self.atr_multiplier),
                          reason=f"상승 추세 눌림권 K/D 상향 교차 (%K {current:.0f})")
        if not uptrend and current > (100 - self.pullback_level) and crossed_down:
            return Signal(action=SignalAction.ENTER_SHORT, strength=conviction,
                          stop_loss=_atr_stop(candles, len(candles) - 1, price,
                                              PositionSide.SHORT, self.atr_multiplier),
                          reason=f"하락 추세 반등권 K/D 하향 교차 (%K {current:.0f})")
        return Signal(reason="조건 미충족")


@register_strategy("obv_trend")
class ObvTrendStrategy(Strategy):
    summary = "가격 추세와 거래량 흐름(OBV)이 같은 방향일 때만 진입"
    category = "combo"
    description = """
"거래량은 가격에 선행한다"는 오래된 격언을 규칙으로 만든 전략이다. OBV(온밸런스
볼륨)는 오른 봉의 거래량은 더하고 내린 봉의 거래량은 빼서 누적한 값 — **돈이
쌓이는 중인지 빠지는 중인지**를 잰다.

가격만 보는 추세추종의 맹점은 거래량 없는 상승이다. 얇은 호가를 밀어 올린 상승은
거래량이 실리지 않고, OBV 가 따라 오르지 않는다. 이 전략은 가격 신호(종가가
EMA20 상향 돌파)가 나와도 **OBV 가 자기 이동평균 위에 있을 때만** 매수한다.
가격과 거래량이 같은 말을 할 때만 믿는 것이다.

청산은 가격이 EMA20 을 반대로 이탈하면 한다. 거래량 조건은 진입에만 쓴다 —
이미 잡은 추세에서 거래량이 줄어드는 것은 자연스러운 일이라 청산 사유로 쓰면
너무 일찍 내리게 된다.

**강점**: 거래량 없는 가짜 돌파를 거른다. 세력 매집이 실린 움직임에만 탄다.
**약점**: 거래량 데이터의 질에 성적이 좌우된다. 조건이 하나 더 붙는 만큼 신호가
줄고, OBV 도 결국 후행 누적치라 급반전은 못 피한다.
"""
    algorithm = """
**지표**  EMA(20), OBV 와 OBV 의 SMA(20), ATR(14)

**진입**  가격과 거래량 흐름이 **같은 방향**일 때만.
- 롱: 종가가 EMA20 을 상향 돌파 (직전 봉 이하 → 이번 봉 초과), 그리고 OBV > OBV-SMA20
- 숏: 종가가 EMA20 을 하향 이탈, 그리고 OBV < OBV-SMA20

**청산**  종가가 EMA20 을 반대로 넘으면 청산. (거래량 조건은 진입에만 쓴다.)

**손절**  진입가 ∓ (ATR14 × 2.0)

**확신도**  OBV 가 자기 평균에서 벗어난 정도를 최근 OBV 변동폭과 비교
- ≥ 1.0배 → VERY_HIGH · ≥ 0.6배 → HIGH · ≥ 0.3배 → MEDIUM · 그 외 LOW

**파라미터**  `price_period`, `obv_period`, `atr_multiplier`
"""

    def setup(self) -> None:
        self.price_period = int(self.params.get("price_period", 20))
        self.obv_period = int(self.params.get("obv_period", 20))
        self.atr_multiplier = float(self.params.get("atr_multiplier", 2.0))

    @property
    def warmup_candles(self) -> int:
        return max(self.price_period, self.obv_period) + 30

    def generate(self, ctx: StrategyContext) -> Signal:
        candles = ctx.closed_candles
        if len(candles) < self.warmup_candles:
            return Signal(reason="워밍업 부족")

        closes = [c.close for c in candles]
        baseline = ema(closes, self.price_period)
        flow = obv(candles)
        flow_avg = sma(flow, self.obv_period)
        if baseline[-1] is None or baseline[-2] is None or flow_avg[-1] is None:
            return Signal(reason="지표 계산 불가")

        price, previous = closes[-1], closes[-2]
        crossed_up = previous <= baseline[-2] and price > baseline[-1]
        crossed_down = previous >= baseline[-2] and price < baseline[-1]

        if ctx.position.side is PositionSide.LONG and price < baseline[-1]:
            return Signal(action=SignalAction.EXIT, reason="EMA 하향 이탈")
        if ctx.position.side is PositionSide.SHORT and price > baseline[-1]:
            return Signal(action=SignalAction.EXIT, reason="EMA 상향 돌파")
        if ctx.position.is_open:
            return Signal(reason="추세 유지 중")

        obv_gap = flow[-1] - flow_avg[-1]
        # OBV 는 스케일이 심볼마다 다르므로 최근 변동폭으로 정규화한다.
        window = flow[-self.obv_period:]
        obv_span = max(window) - min(window)
        normalized = abs(obv_gap) / obv_span if obv_span > 0 else 0.0
        conviction = (
            Conviction.VERY_HIGH.value if normalized >= 1.0
            else Conviction.HIGH.value if normalized >= 0.6
            else Conviction.MEDIUM.value if normalized >= 0.3
            else Conviction.LOW.value
        )

        if crossed_up:
            if obv_gap <= 0:
                return Signal(reason="가격은 돌파했으나 거래량 흐름이 받쳐주지 않음")
            return Signal(action=SignalAction.ENTER_LONG, strength=conviction,
                          stop_loss=_atr_stop(candles, len(candles) - 1, price,
                                              PositionSide.LONG, self.atr_multiplier),
                          reason="가격 돌파 + OBV 매집 확인")
        if crossed_down:
            if obv_gap >= 0:
                return Signal(reason="가격은 이탈했으나 거래량 흐름이 받쳐주지 않음")
            return Signal(action=SignalAction.ENTER_SHORT, strength=conviction,
                          stop_loss=_atr_stop(candles, len(candles) - 1, price,
                                              PositionSide.SHORT, self.atr_multiplier),
                          reason="가격 이탈 + OBV 분산 확인")
        return Signal(reason="돌파 없음")
