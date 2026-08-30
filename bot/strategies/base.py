"""전략 인터페이스와 레지스트리.

전략이 하는 일은 하나다: 시장 상태를 보고 `Signal` 을 낸다. 포지션 크기,
손절 폭 강제, 킬스위치는 RiskManager 소관이고 주문 전송은 Executor 소관이다.
이 경계를 지키면 전략을 갈아 끼워도 나머지가 그대로 돌아간다.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Callable, Type

from bot.models import Candle, Position, Signal, Ticker


@dataclass(frozen=True)
class StrategyContext:
    """전략이 한 번의 판단에 쓸 수 있는 모든 것."""

    symbol: str
    timeframe: str
    candles: list[Candle]   # 오래된 것 → 최신 순. 마지막 캔들은 아직 미완성일 수 있다.
    ticker: Ticker
    position: Position
    equity: float           # 선물 계좌 총 자기자본(견적통화 기준)

    @property
    def last_price(self) -> float:
        return self.ticker.last

    @property
    def closed_candles(self) -> list[Candle]:
        """미완성 캔들을 제외한 확정 캔들.

        마지막 캔들은 진행 중이라 값이 계속 바뀐다. 지표는 보통 이쪽을 써야
        같은 신호가 흔들리지 않는다.
        """
        return self.candles[:-1] if self.candles else []


class Strategy(ABC):
    """모든 전략의 베이스 클래스."""

    name: str = "unnamed"
    # 대시보드와 `python -m bot strategies` 에 그대로 표시된다.
    summary: str = ""
    description: str = ""
    # 실제 규칙. 왜 되는지(description)와 별개로, 코드가 무엇을 하는지를
    # 진입/청산/손절/확신도 순으로 구체적으로 적는다.
    algorithm: str = ""
    # trend | reversion | breakout | combo | range
    category: str = "other"

    def __init__(self, params: dict[str, Any] | None = None) -> None:
        self.params = params or {}
        self.setup()

    def setup(self) -> None:
        """파라미터 검증·상태 초기화. 필요하면 오버라이드한다."""

    @property
    def warmup_candles(self) -> int:
        """신호를 내기 전에 필요한 최소 캔들 수."""
        return 0

    @abstractmethod
    def generate(self, ctx: StrategyContext) -> Signal:
        """매 폴링 주기마다 호출된다. 부작용 없이 Signal 만 반환할 것."""


_REGISTRY: dict[str, Type[Strategy]] = {}


def register_strategy(name: str) -> Callable[[Type[Strategy]], Type[Strategy]]:
    """전략 클래스를 이름으로 등록하는 데코레이터."""

    def decorator(cls: Type[Strategy]) -> Type[Strategy]:
        key = name.lower()
        if key in _REGISTRY and _REGISTRY[key] is not cls:
            raise ValueError(f"전략 이름 '{name}' 이 이미 등록되어 있습니다")
        cls.name = key
        _REGISTRY[key] = cls
        return cls

    return decorator


def get_strategy(name: str, params: dict[str, Any] | None = None) -> Strategy:
    key = name.lower()
    if key not in _REGISTRY:
        raise KeyError(
            f"알 수 없는 전략 '{name}'. 등록된 전략: {', '.join(available_strategies()) or '(없음)'}"
        )
    return _REGISTRY[key](params)


def available_strategies() -> list[str]:
    return sorted(_REGISTRY)


def strategy_catalog() -> list[dict[str, str]]:
    """등록된 전략의 이름·분류·설명 목록. 화면과 CLI 가 함께 쓴다."""
    return [
        {
            "name": name,
            "category": cls.category,
            "summary": cls.summary,
            "description": (cls.description or "").strip(),
            "algorithm": (cls.algorithm or "").strip(),
        }
        for name, cls in sorted(_REGISTRY.items())
    ]
