"""새 전략을 만들 때 복사해 쓰는 템플릿.

사용법:
    1. 이 파일을 `bot/strategies/my_strategy.py` 로 복사한다.
    2. `@register_strategy("my_strategy")` 로 이름을 바꾸고 클래스명을 고친다.
    3. `bot/strategies/__init__.py` 에 import 를 추가한다(등록 트리거).
    4. `config.yaml` 의 `strategy.name` 을 그 이름으로 바꾼다.

지켜야 할 계약:
    * `generate()` 는 부작용이 없어야 한다 — 주문을 직접 보내지 말 것.
    * 포지션 크기는 반환하지 않는다. RiskManager 가 정한다.
    * `strength`(0~1)로 확신도를 표현하면 사이징에 배수로 반영된다.
    * 진행 중인 마지막 캔들이 흔들리는 게 싫으면 `ctx.closed_candles` 를 쓴다.
"""

from __future__ import annotations

from bot.models import Signal, SignalAction
from bot.strategies.base import Strategy, StrategyContext, register_strategy


@register_strategy("template")
class TemplateStrategy(Strategy):
    """진입 조건이 비어 있는 스켈레톤. 그대로 두면 절대 매매하지 않는다."""

    def setup(self) -> None:
        # config.yaml 의 strategy.params 가 self.params 로 들어온다.
        self.lookback = int(self.params.get("lookback", 20))
        if self.lookback < 1:
            raise ValueError("lookback 은 1 이상이어야 합니다")

    @property
    def warmup_candles(self) -> int:
        # 엔진은 캔들이 이만큼 쌓이기 전에는 generate() 를 부르지 않는다.
        return self.lookback + 1

    def generate(self, ctx: StrategyContext) -> Signal:
        candles = ctx.closed_candles
        if len(candles) < self.warmup_candles:
            return Signal(action=SignalAction.HOLD, reason="워밍업 부족")

        # --- 여기에 지표와 진입/청산 조건을 작성한다 ---------------------
        # 예시 뼈대:
        #
        #   closes = [c.close for c in candles]
        #   fast = sum(closes[-10:]) / 10
        #   slow = sum(closes[-self.lookback:]) / self.lookback
        #
        #   if not ctx.position.is_open and fast > slow:
        #       return Signal(
        #           action=SignalAction.ENTER_LONG,
        #           strength=1.0,
        #           stop_loss=ctx.last_price * 0.99,   # 생략하면 리스크 기본값 사용
        #           reason=f"fast({fast:.2f}) > slow({slow:.2f})",
        #       )
        #   if ctx.position.side is PositionSide.LONG and fast < slow:
        #       return Signal(action=SignalAction.EXIT, reason="추세 이탈")
        # ------------------------------------------------------------------

        return Signal(action=SignalAction.HOLD, reason="조건 미구현 — 템플릿 상태")
