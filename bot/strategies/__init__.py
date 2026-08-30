"""전략 패키지.

여기서 import 하는 것만 레지스트리에 등록된다. 새 전략을 추가하면 아래에
import 한 줄을 더한다.
"""

from bot.strategies.base import (
    Strategy,
    StrategyContext,
    available_strategies,
    get_strategy,
    register_strategy,
)
from bot.strategies import hold, template  # noqa: F401  등록 트리거

__all__ = [
    "Strategy",
    "StrategyContext",
    "available_strategies",
    "get_strategy",
    "register_strategy",
]
