"""아무것도 하지 않는 기본 전략.

배선(데이터 수집 → 판단 → 리스크 → 주문)을 실거래 없이 점검할 때 쓴다.
`strategy.name: hold` 로 두면 봇은 계속 돌지만 주문은 한 건도 나가지 않는다.
"""

from __future__ import annotations

from bot.models import Signal, SignalAction
from bot.strategies.base import Strategy, StrategyContext, register_strategy


@register_strategy("hold")
class HoldStrategy(Strategy):
    def generate(self, ctx: StrategyContext) -> Signal:
        return Signal(action=SignalAction.HOLD, reason="hold 전략 — 진입하지 않음")
