"""변동성 확장 돌파 전략.

돌파 계열의 또 다른 각도 — 가격의 위치(채널·밴드)가 아니라 **움직임 자체의
크기**를 본다. 평소의 몇 배로 움직인 봉은 새 정보가 시장에 들어왔다는 뜻이고,
정보는 한 봉 만에 소화되지 않는다는 것이 이 계열의 전제다.
"""

from __future__ import annotations

from bot.indicators import atr, donchian, ema, sma, true_range
from bot.models import Conviction, PositionSide, Signal, SignalAction
from bot.strategies.base import Strategy, StrategyContext, register_strategy


@register_strategy("atr_ignition")
class AtrIgnitionStrategy(Strategy):
    summary = "평소 변동폭의 2배로 움직인 봉의 방향을 따라간다"
    category = "breakout"
    description = """
가장 원초적인 변동성 신호다 — 이번 봉의 진폭(True Range)이 평소(ATR)의 2배를
넘으면, 뭔가 큰 일이 시작됐다고 보고 **그 봉의 방향으로** 진입한다. 뉴스, 청산
연쇄, 고래 주문 — 원인이 무엇이든 이 정도 크기의 움직임은 한 봉으로 끝나지 않는
경우가 많다는 경험칙에 건다.

채널 돌파와의 차이: 채널은 "어디를 넘었나"를 보므로 조용히 슬금슬금 넘는 돌파도
잡지만, 이쪽은 "얼마나 세게 움직였나"만 보므로 **폭발적인 움직임만** 잡는다.
가격 위치는 아예 안 본다 — 박스 한가운데서 터져도 따라간다.

방향은 봉의 몸통으로 정하고, 몸통이 진폭의 절반도 안 되면(꼬리 공방이 심하면)
방향 불명으로 보고 거른다. 확신도는 진폭 배율 — 3배, 4배로 커질수록 올린다.

**강점**: 급등락의 첫 봉에서 바로 올라탄다. 지표 지연이 전혀 없다.
**약점**: 그 큰 봉이 소진(클라이맥스)일 수도 있다 — volume_climax 와 정확히
반대 해석이라, 순위표에서 두 전략의 성적 비교가 곧 이 시장의 성격 판정이다.
"""
    algorithm = """
**지표**  True Range(이번 봉), ATR(14, 직전 봉까지), EMA(10)

**점화 판정**
- 이번 봉 TR ≥ 직전 ATR × 2.0
- |몸통| ≥ TR × 0.5 (방향이 분명한 봉만)

**진입**  점화 봉의 몸통 방향으로. 직전 봉도 점화였으면 건너뛴다(추격 방지).

**청산**  EMA10 을 반대로 이탈하면 청산.

**손절**  점화 봉의 반대쪽 끝 — 점화 범위가 전부 반납되면 실패다.

**확신도**  TR ÷ ATR 배율
- ≥ 3.5 → VERY_HIGH · ≥ 2.8 → HIGH · ≥ 2.3 → MEDIUM · 그 외 LOW

**파라미터**  `multiple`(기본 2.0), `body_ratio`(기본 0.5), `exit_period`
"""

    def setup(self) -> None:
        self.multiple = float(self.params.get("multiple", 2.0))
        self.body_ratio = float(self.params.get("body_ratio", 0.5))
        self.exit_period = int(self.params.get("exit_period", 10))

    @property
    def warmup_candles(self) -> int:
        return 40

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
                reason="단기 추세 이탈" if wrong_way else "점화 방향 유지",
            )

        # 직전 봉까지의 ATR — 점화 봉 자신이 ATR 을 부풀리면 판정이 무뎌진다.
        atr_values = atr(candles[:-1], 14)
        ranges = true_range(candles)
        if atr_values[-1] is None or atr_values[-1] == 0 or ranges[-1] is None:
            return Signal(reason="지표 계산 불가")

        c = candles[-1]
        tr = ranges[-1]
        ratio = tr / atr_values[-1]
        body = c.close - c.open
        directional = tr > 0 and abs(body) >= tr * self.body_ratio

        # 직전 봉도 점화였다면 이미 늦었다 — 두 번째 봉 추격은 하지 않는다.
        prev_ranges = true_range(candles[:-1])
        prev_atr = atr(candles[:-2], 14)
        already_ignited = (
            prev_ranges[-1] is not None and prev_atr[-1]
            and prev_ranges[-1] / prev_atr[-1] >= self.multiple
        )

        if ratio < self.multiple or not directional or already_ignited:
            return Signal(reason=f"점화 없음 (TR {ratio:.1f} ATR)")

        conviction = (
            Conviction.VERY_HIGH.value if ratio >= 3.5
            else Conviction.HIGH.value if ratio >= 2.8
            else Conviction.MEDIUM.value if ratio >= 2.3
            else Conviction.LOW.value
        )
        if body > 0:
            return Signal(action=SignalAction.ENTER_LONG, strength=conviction,
                          stop_loss=c.low,
                          reason=f"변동성 점화 상방 (TR {ratio:.1f} ATR)")
        return Signal(action=SignalAction.ENTER_SHORT, strength=conviction,
                      stop_loss=c.high,
                      reason=f"변동성 점화 하방 (TR {ratio:.1f} ATR)")


@register_strategy("volume_breakout")
class VolumeBreakoutStrategy(Strategy):
    summary = "돈치안 돌파를 거래량 폭증이 확인해 줄 때만 진입"
    category = "breakout"
    description = """
돈치안 돌파(터틀)의 최대 약점은 가짜 돌파다 — 최고가를 살짝 넘고 되밀리는 일이
승률을 30%대로 끌어내린다. 이 전략은 고전적인 해독제를 쓴다: **거래량이 실리지
않은 돌파는 버린다.** 진짜 돌파는 그 가격대를 뚫으려는 실제 물량이 필요하므로
거래량이 폭증하고, 가짜 돌파(스탑 사냥)는 얇은 호가만 스치므로 거래량이 없다는
논리다.

진입은 20봉 최고가 돌파 + 거래량이 20봉 평균의 1.5배 이상일 때만. 거래량이
안 실린 돌파는 사유에 남기고 버린다. 청산은 터틀과 같은 10봉 반대 채널이다.

donchian_breakout 과 정확히 한 조건만 다르므로, 순위표에서 둘을 비교하면
"거래량 확인의 값어치"가 그대로 숫자로 나온다. 거래량 데이터가 무의미한
환경에서는 확인을 건너뛰고 등급만 낮춘다.

**강점**: 가짜 돌파의 상당 부분을 구조적으로 거른다.
**약점**: 거래량 확인을 기다리는 사이 진짜 돌파의 진입가가 나빠질 수 있다.
거래량 데이터가 부실한 심볼에서는 필터가 무작위나 다름없어진다.
"""
    algorithm = """
**지표**  돈치안 채널(진입 20, 청산 10), 거래량 SMA(20), ATR(14)

**진입**
- 롱: 종가 > 직전 20봉 최고가 그리고 거래량 ≥ 20봉 평균 × 1.5
- 숏: 종가 < 직전 20봉 최저가 그리고 같은 거래량 조건
- 거래량 데이터가 무의미하면(전부 동일 등) 조건을 건너뛰고 등급 하향

**청산**  10봉 반대 채널 이탈 (터틀과 동일).

**손절**  진입가 ∓ (ATR14 × 2.0)

**확신도**  거래량 배율
- ≥ 3 → VERY_HIGH · ≥ 2 → HIGH · ≥ 1.5 → MEDIUM · 미확인 → LOW

**파라미터**  `entry_period`, `exit_period`, `volume_multiple`, `atr_multiplier`
"""

    def setup(self) -> None:
        self.entry_period = int(self.params.get("entry_period", 20))
        self.exit_period = int(self.params.get("exit_period", 10))
        self.volume_multiple = float(self.params.get("volume_multiple", 1.5))
        self.atr_multiplier = float(self.params.get("atr_multiplier", 2.0))

    @property
    def warmup_candles(self) -> int:
        return self.entry_period + 20

    def generate(self, ctx: StrategyContext) -> Signal:
        candles = ctx.closed_candles
        if len(candles) < self.warmup_candles:
            return Signal(reason="워밍업 부족")

        entry_high, entry_low = donchian(candles, self.entry_period)
        exit_high, exit_low = donchian(candles, self.exit_period)
        atr_values = atr(candles, 14)
        if entry_high[-1] is None or exit_low[-1] is None or atr_values[-1] is None:
            return Signal(reason="지표 계산 불가")

        price = candles[-1].close
        if ctx.position.side is PositionSide.LONG and price < exit_low[-1]:
            return Signal(action=SignalAction.EXIT, reason=f"{self.exit_period}봉 최저가 이탈")
        if ctx.position.side is PositionSide.SHORT and price > exit_high[-1]:
            return Signal(action=SignalAction.EXIT, reason=f"{self.exit_period}봉 최고가 돌파")
        if ctx.position.is_open:
            return Signal(reason="채널 안 유지")

        broke_up = price > entry_high[-1]
        broke_down = price < entry_low[-1]
        if not (broke_up or broke_down):
            return Signal(reason="채널 안")

        volumes = [c.volume for c in candles[-21:-1]]
        avg_volume = sum(volumes) / len(volumes)
        volume_usable = avg_volume > 0 and (max(volumes) - min(volumes)) > 0
        multiple = candles[-1].volume / avg_volume if volume_usable else 0.0

        if volume_usable and multiple < self.volume_multiple:
            return Signal(reason=f"돌파했으나 거래량 부족 (×{multiple:.1f})")

        conviction = (
            Conviction.LOW.value if not volume_usable
            else Conviction.VERY_HIGH.value if multiple >= 3
            else Conviction.HIGH.value if multiple >= 2
            else Conviction.MEDIUM.value
        )
        volume_note = f"거래량 ×{multiple:.1f}" if volume_usable else "거래량 미확인"
        stop_distance = atr_values[-1] * self.atr_multiplier

        if broke_up:
            return Signal(action=SignalAction.ENTER_LONG, strength=conviction,
                          stop_loss=price - stop_distance,
                          reason=f"{self.entry_period}봉 최고가 돌파 + {volume_note}")
        return Signal(action=SignalAction.ENTER_SHORT, strength=conviction,
                      stop_loss=price + stop_distance,
                      reason=f"{self.entry_period}봉 최저가 이탈 + {volume_note}")
