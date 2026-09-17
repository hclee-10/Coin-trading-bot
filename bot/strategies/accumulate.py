"""무매도 적립식 (관찰용) — 급등엔 숏, 급락엔 롱을 같은 금액으로 계속 사 모은다.

⚠️ 이 계열은 돈을 벌려는 전략이 아니라 **자본 소요를 재는 실험 장치**다.
회당 고정 금액(예: 10달러 = `risk.notional_tiers` 첫 칸)으로 급변의 반대
방향을 적립만 하고 **팔지 않는다** — 익절도, 손절 청산도 없다. 그렇게 두면
추세가 한 방향으로 갈 때 미실현 손실과 최대 낙폭이 순위표에 그대로 쌓이는데,
그 최대치가 곧 "이 방식을 버티는 데 필요했던 자금"이다. 수수료와 펀딩비는
모의매매가 매 적립·매 정산마다 실제처럼 부과하므로 관찰값에 포함되어 있다.

급등/급락의 기준은 하나로 정하기 어려워 **세 가지 자로 분리**했다. 순위표에서
세 변형의 낙폭 곡선을 비교하면 어떤 기준이 이 시장의 '진짜 급변'을 잡는지
드러난다:

* `dca_atr`     — 변동성 기준: 3봉 누적 변화가 ATR14 의 2배 이상
* `dca_pct`     — 고정 % 기준: 3봉 누적 변화가 1.5% 이상
* `dca_channel` — 범위 기준: 직전 20봉 최고가 돌파(급등) / 최저가 이탈(급락)

실거래에서도 같은 방식으로 동작한다 — 실행기의 넷 적립 모드가 방향과 무관하게
고정 금액 시장가 주문 하나만 내고, 단방향(one-way) 모드에서 같은 방향은 쌓이고
반대 방향은 자동 상계된다(순포지션 감소/역전). 보호주문은 걸지 않는다. 시스템
규칙상 신호에는 손절가가 붙어야 해서 명목값(롱 -98% / 숏 +200%)을 달았지만
실거래에는 손절 주문이 나가지 않고, 사실상 도달하지도 않는다 — 레버리지
격리라면 그 훨씬 전이 강제청산 가격이고, 크로스라면 계좌 전체가 증거금이다.
"""

from __future__ import annotations

from bot.indicators import atr, donchian
from bot.models import Candle, Conviction, PositionSide, Signal, SignalAction
from bot.strategies.base import Strategy, StrategyContext, register_strategy

_DESCRIPTION_COMMON = """
급변이 오면 그 반대 방향으로 회당 고정 금액(기본 `notional_tiers` = 10달러)을
주문한다 — 보유 여부와 무관하게, 급등이면 숏 10달러, 급락이면 롱 10달러다.
같은 방향이면 수량이 쌓여 평균 단가를 끌어오고, 반대 방향이면 순포지션이
상계된다(추세가 길수록 반대쪽 주문이 쌓여 순포지션이 줄거나 뒤집힌다).
**청산 신호는 없다** — 익절도 손절 청산도 내지 않는다. 명목상의 손절
(롱 -98% / 숏 +200%)은 시스템 규칙용일 뿐 실거래에 주문으로 나가지 않는다.

이 전략의 목적은 수익이 아니라 **관찰**이다: 순위표의 미실현 손익·최대 낙폭·
청산위험이 "10달러씩 이 기준으로 사 모으면 추세장에서 얼마까지 물리는가"를
수수료·펀딩비 포함으로 보여 준다. 그 최대치가 이 방식에 필요한 실탄이다.

⚠️ 실거래로 돌리면 이 구조는 추세장 한 번에 계좌를 소진할 수 있다 — 관찰
실험임을 인지하고 잃어도 되는 금액으로만 쓸 것. 노출을 늘리는 쪽 적립은 한도
(자기자본 대비 노출, 기본 10배)에서 멈추고, 상계(반대 방향)는 항상 허용된다.
"""


class _AccumulateBase(Strategy):
    """무매도 적립식의 공통 뼈대. 하위 클래스는 급변 판정만 정의한다."""

    category = "range"

    def setup(self) -> None:
        self.max_exposure_pct = float(self.params.get("max_exposure_pct", 1000.0))
        self._setup_spike()

    def _setup_spike(self) -> None:
        """급변 판정 파라미터. 하위 클래스가 오버라이드한다."""

    def _spike(self, candles: list[Candle]) -> tuple[int, str] | None:
        """이번 봉에 급변이 '막 성립'했으면 (방향, 설명). 아니면 None."""
        raise NotImplementedError

    @property
    def warmup_candles(self) -> int:
        return 40

    def generate(self, ctx: StrategyContext) -> Signal:
        candles = ctx.closed_candles
        if len(candles) < self.warmup_candles:
            return Signal(reason="워밍업 부족")

        price = candles[-1].close
        spike = self._spike(candles)

        if spike is None:
            if ctx.position.is_open:
                return Signal(reason=f"적립 유지 (평단 {ctx.position.entry_price or price:.6g})")
            return Signal(reason="급변 없음")

        # 급변이 왔다 — 보유 여부·방향과 무관하게 반대쪽으로 고정 금액 하나.
        # 같은 방향이면 수량이 쌓이고(물타기), 반대 방향이면 단방향 모드에서
        # 자동으로 상계된다(순포지션 감소/역전). 매도 신호는 영원히 없다.
        direction, label = spike
        side = PositionSide.SHORT if direction == 1 else PositionSide.LONG

        if ctx.position.is_open and ctx.position.side is side:
            # 노출을 '늘리는' 쪽만 한도를 본다. 상계(반대 방향)는 항상 허용 —
            # 노출을 줄이는 주문을 막을 이유가 없다.
            exposure_cap = ctx.equity * self.max_exposure_pct / 100
            if ctx.position.notional >= exposure_cap:
                return Signal(reason=f"적립 한도 도달 (노출 {ctx.position.notional:.0f})")

        if ctx.position.is_open:
            effect = "적립" if ctx.position.side is side else "상계"
            reason = f"{label} — {'숏' if direction == 1 else '롱'} {effect} "                      f"(평단 {ctx.position.entry_price or price:.6g})"
        else:
            reason = f"{label} — {'숏' if direction == 1 else '롱'} 적립 시작"

        return Signal(
            action=SignalAction.ENTER_SHORT if direction == 1 else SignalAction.ENTER_LONG,
            strength=Conviction.LOW.value,
            stop_loss=self._nominal_stop(price, side),
            metadata={"accumulate": True},
            reason=reason,
        )

    @staticmethod
    def _nominal_stop(price: float, side: PositionSide) -> float:
        # 시스템 규칙(손절 필수)을 지키기 위한 명목값. 사실상 도달하지 않는다 —
        # 레버리지 격리라면 그 훨씬 전에 강제청산이고, 그 지점을 재는 게 목적이다.
        return price * 0.02 if side is PositionSide.LONG else price * 3.0


@register_strategy("dca_atr")
class DcaAtrStrategy(_AccumulateBase):
    summary = "무매도 적립식 ① 변동성 기준 — 3봉 변화가 ATR의 2배면 급변"
    description = """
급변의 자로 **평소 변동폭(ATR)**을 쓰는 변형이다. 3봉 누적 변화가 ATR14 의
2배를 넘으면 급변으로 본다. 조용한 장에서는 작은 움직임도 급변으로 잡고,
요동치는 장에서는 큰 움직임만 잡는다 — 기준이 시장에 자동으로 적응하는 것이
장점이고, "2 ATR"이 사람 눈의 급등/급락과 어긋날 수 있는 것이 단점이다.
""" + _DESCRIPTION_COMMON
    algorithm = """
**급변 판정**  |종가 − 3봉 전 종가| ≥ ATR14 × 2.0, 직전 봉 기준으로는 문턱
아래였다가 이번 봉에 넘어선 순간만 (같은 급변에 두 번 반응하지 않음)

**주문**  급변의 반대 방향으로 회당 고정 금액(notional_tiers, 기본 10달러)
시장가 하나 — 보유 여부와 무관. 같은 방향은 적립(평단 갱신), 반대 방향은
상계(순포지션 감소/역전). 보호주문 없음.

**청산 신호 없음**  익절·손절 청산 전부 내지 않는다. 명목 손절(롱 -98% /
숏 +200%)은 시스템 규칙용이며 주문으로 나가지 않는다.

**적립 한도**  노출을 늘리는 쪽만 자기자본 × 1000% 에서 중단. 상계는 항상 허용.

**파라미터**  `spike_atr`(2.0), `lookback`(3), `max_exposure_pct`(1000)
"""

    def _setup_spike(self) -> None:
        self.spike_atr = float(self.params.get("spike_atr", 2.0))
        self.lookback = int(self.params.get("lookback", 3))

    def _spike(self, candles):
        closes = [c.close for c in candles]
        atr_values = atr(candles, 14)
        if not atr_values[-1] or len(closes) < self.lookback + 2:
            return None
        threshold = atr_values[-1] * self.spike_atr
        move = closes[-1] - closes[-1 - self.lookback]
        move_prev = closes[-2] - closes[-2 - self.lookback]
        if move >= threshold and move_prev < threshold:
            return 1, f"급등 {move / atr_values[-1]:.1f} ATR"
        if move <= -threshold and move_prev > -threshold:
            return -1, f"급락 {-move / atr_values[-1]:.1f} ATR"
        return None


@register_strategy("dca_pct")
class DcaPctStrategy(_AccumulateBase):
    summary = "무매도 적립식 ② 고정 % 기준 — 3봉 변화가 1.5%면 급변"
    description = """
급변의 자로 **고정 퍼센트**를 쓰는 변형이다. 3봉 누적 변화가 1.5% 를 넘으면
급변으로 본다. 사람이 차트를 보며 "급등이네"라고 느끼는 직관에 가장 가깝고
기준이 투명한 것이 장점, 시장의 변동성 국면과 무관하게 같은 자를 들이대는
것이 단점이다 — 요동치는 장에서는 1.5% 가 일상이라 적립이 너무 잦아지고,
극도로 조용한 장에서는 급변이 영영 안 잡힐 수 있다. dca_atr 와의 성적 차이가
곧 "적응형 기준의 값어치"다.
""" + _DESCRIPTION_COMMON
    algorithm = """
**급변 판정**  |종가 ÷ 3봉 전 종가 − 1| ≥ 1.5%, 직전 봉 기준으로는 문턱
아래였다가 이번 봉에 넘어선 순간만

**주문**  급변의 반대 방향으로 회당 고정 금액(notional_tiers, 기본 10달러)
시장가 하나 — 보유 여부와 무관. 같은 방향은 적립, 반대 방향은 상계. 보호주문
없음. 순위표의 미실현·최대 낙폭이 관찰값이다.

**청산 신호 없음**  익절·손절 청산 전부 내지 않는다.

**적립 한도**  노출을 늘리는 쪽만 자기자본 × 1000% 에서 중단. 상계는 항상 허용.

**파라미터**  `spike_pct`(1.5), `lookback`(3), `max_exposure_pct`(1000)
"""

    def _setup_spike(self) -> None:
        self.spike_pct = float(self.params.get("spike_pct", 1.5))
        self.lookback = int(self.params.get("lookback", 3))

    def _spike(self, candles):
        closes = [c.close for c in candles]
        if len(closes) < self.lookback + 2:
            return None
        base, base_prev = closes[-1 - self.lookback], closes[-2 - self.lookback]
        if base <= 0 or base_prev <= 0:
            return None
        move = (closes[-1] / base - 1) * 100
        move_prev = (closes[-2] / base_prev - 1) * 100
        if move >= self.spike_pct and move_prev < self.spike_pct:
            return 1, f"급등 {move:+.1f}%"
        if move <= -self.spike_pct and move_prev > -self.spike_pct:
            return -1, f"급락 {move:+.1f}%"
        return None


@register_strategy("dca_channel")
class DcaChannelStrategy(_AccumulateBase):
    summary = "무매도 적립식 ③ 범위 기준 — 20봉 최고가 돌파가 급등, 최저가 이탈이 급락"
    description = """
급변의 자로 **최근 범위**를 쓰는 변형이다. 종가가 직전 20봉 최고가를 넘으면
급등, 최저가를 깨면 급락으로 본다. 움직임의 속도가 아니라 **새로운 영역으로
나갔는가**를 보므로, 천천히 기어올라도 신고가면 급등으로 친다 — 앞의 두 변형이
못 잡는 완만한 일방통행을 잡는 것이 장점이다. 대신 박스 안의 빠른 요동은
아무리 격해도 급변으로 치지 않는다.

터틀(donchian_breakout)이 사는 자리에서 정확히 반대로 파는 셈이라, 두 전략의
성적을 나란히 보면 "돌파는 이어지는가, 되돌아오는가"라는 오래된 논쟁이 이
시장에서 어느 쪽인지 드러난다.
""" + _DESCRIPTION_COMMON
    algorithm = """
**급변 판정**  종가 > 직전 20봉 최고가 → 급등 / 종가 < 직전 20봉 최저가 → 급락.
직전 봉은 채널 안이었어야 한다 (돌파 순간만).

**주문**  급변의 반대 방향으로 회당 고정 금액(notional_tiers, 기본 10달러)
시장가 하나 — 보유 여부와 무관. 같은 방향은 적립, 반대 방향은 상계. 보호주문
없음. 순위표의 미실현·최대 낙폭이 관찰값이다.

**청산 신호 없음**  익절·손절 청산 전부 내지 않는다.

**적립 한도**  노출을 늘리는 쪽만 자기자본 × 1000% 에서 중단. 상계는 항상 허용.

**파라미터**  `period`(20), `max_exposure_pct`(1000)
"""

    def _setup_spike(self) -> None:
        self.period = int(self.params.get("period", 20))

    @property
    def warmup_candles(self) -> int:
        return self.period + 20

    def _spike(self, candles):
        highs, lows = donchian(candles, self.period)
        if highs[-1] is None or highs[-2] is None:
            return None
        price, previous = candles[-1].close, candles[-2].close
        if price > highs[-1] and previous <= highs[-2]:
            return 1, f"{self.period}봉 최고가 돌파"
        if price < lows[-1] and previous >= lows[-2]:
            return -1, f"{self.period}봉 최저가 이탈"
        return None
