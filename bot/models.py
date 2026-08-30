"""거래소·전략·실행 계층이 공유하는 데이터 모델.

거래소별 응답 포맷을 여기서 한 번 정규화하므로, 상위 계층(전략·리스크·실행)은
Bitget인지 OKX인지 알 필요가 없다.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class Side(str, Enum):
    BUY = "buy"
    SELL = "sell"

    @property
    def opposite(self) -> "Side":
        return Side.SELL if self is Side.BUY else Side.BUY


class PositionSide(str, Enum):
    LONG = "long"
    SHORT = "short"
    FLAT = "flat"


class SignalAction(str, Enum):
    """전략이 엔진에 전달하는 의사결정."""

    HOLD = "hold"
    ENTER_LONG = "enter_long"
    ENTER_SHORT = "enter_short"
    EXIT = "exit"


@dataclass(frozen=True)
class Candle:
    timestamp: int  # ms, 캔들 시작 시각
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass(frozen=True)
class Ticker:
    symbol: str
    last: float
    bid: float | None
    ask: float | None
    timestamp: int | None


@dataclass(frozen=True)
class Balance:
    currency: str
    free: float
    used: float
    total: float


@dataclass(frozen=True)
class Position:
    """단방향(one-way) 모드 기준 포지션 스냅샷."""

    symbol: str
    side: PositionSide
    contracts: float = 0.0
    entry_price: float | None = None
    notional: float = 0.0
    leverage: float | None = None
    unrealized_pnl: float = 0.0
    liquidation_price: float | None = None
    raw: dict[str, Any] = field(default_factory=dict, repr=False)

    @property
    def is_open(self) -> bool:
        return self.side is not PositionSide.FLAT and self.contracts > 0

    @classmethod
    def flat(cls, symbol: str) -> "Position":
        return cls(symbol=symbol, side=PositionSide.FLAT)


@dataclass(frozen=True)
class Order:
    id: str | None
    symbol: str
    side: Side
    type: str
    amount: float
    price: float | None = None
    status: str | None = None
    filled: float = 0.0
    average: float | None = None
    reduce_only: bool = False
    raw: dict[str, Any] = field(default_factory=dict, repr=False)


@dataclass(frozen=True)
class Market:
    """주문 수량·가격을 거래소 규격에 맞추는 데 필요한 심볼 메타데이터."""

    symbol: str
    base: str
    quote: str
    contract_size: float = 1.0
    min_amount: float | None = None
    max_amount: float | None = None
    min_notional: float | None = None
    amount_precision: float | None = None
    price_precision: float | None = None


@dataclass(frozen=True)
class Signal:
    """전략 출력.

    stop_loss / take_profit 은 절대 가격이다. 전략이 비워 두면 리스크 설정의
    기본 손절·익절 비율로 채워진다. 포지션 크기는 전략이 아니라 RiskManager가
    정한다 — 전략은 `strength`(0~1)로 확신도만 표현하고, 그 값이 사이징
    배수로 쓰인다.
    """

    action: SignalAction = SignalAction.HOLD
    strength: float = 1.0
    stop_loss: float | None = None
    take_profit: float | None = None
    reason: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def is_entry(self) -> bool:
        return self.action in (SignalAction.ENTER_LONG, SignalAction.ENTER_SHORT)

    @property
    def target_side(self) -> PositionSide:
        if self.action is SignalAction.ENTER_LONG:
            return PositionSide.LONG
        if self.action is SignalAction.ENTER_SHORT:
            return PositionSide.SHORT
        return PositionSide.FLAT
