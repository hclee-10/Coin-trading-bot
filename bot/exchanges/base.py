"""선물 거래소 어댑터 인터페이스.

상위 계층은 이 인터페이스만 알면 되고, 거래소를 추가할 때는 이 클래스를
구현하고 factory 에 등록하면 된다.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from bot.models import Balance, Candle, Fill, Market, Order, Position, Side, Ticker


class ExchangeError(Exception):
    """어댑터 계층에서 올라오는 거래소 오류."""


class FuturesExchange(ABC):
    """USDT 무기한 선물 거래소 어댑터 (단방향 모드 기준)."""

    id: str

    # --- 시장 데이터 ---
    @abstractmethod
    def load_markets(self) -> None:
        """심볼 메타데이터를 미리 받아 둔다. 다른 호출 전에 한 번 호출한다."""

    @abstractmethod
    def market(self, symbol: str) -> Market:
        """심볼의 수량·가격 규격을 반환한다."""

    @abstractmethod
    def fetch_candles(
        self, symbol: str, timeframe: str, limit: int, since: int | None = None
    ) -> list[Candle]:
        """과거 캔들을 오래된 것부터 정렬해 반환한다.

        `since`(ms)를 주면 그 시각 이후부터 가져온다. 백테스트용 과거 데이터를
        페이지 단위로 받을 때 쓴다.
        """

    @abstractmethod
    def fetch_ticker(self, symbol: str) -> Ticker:
        ...

    # --- 계좌 ---
    @abstractmethod
    def fetch_balance(self, currency: str) -> Balance:
        """선물 계좌의 해당 통화 잔고."""

    @abstractmethod
    def fetch_position(self, symbol: str) -> Position:
        """포지션이 없으면 Position.flat(symbol) 을 반환한다."""

    @abstractmethod
    def set_leverage(self, symbol: str, leverage: float, margin_mode: str) -> None:
        """레버리지와 마진 모드를 설정한다. 이미 같은 값이면 무시해도 된다."""

    # --- 주문 ---
    @abstractmethod
    def create_market_order(
        self, symbol: str, side: Side, amount: float, *, reduce_only: bool = False
    ) -> Order:
        ...

    @abstractmethod
    def create_limit_order(
        self,
        symbol: str,
        side: Side,
        amount: float,
        price: float,
        *,
        reduce_only: bool = False,
        post_only: bool = True,
    ) -> Order:
        """지정가 주문. `post_only` 면 즉시 체결되는 경우 주문이 거부된다.

        post-only 는 maker 수수료를 보장하는 대신, 가격이 이미 지나갔으면
        체결 없이 취소된다. 그 경우를 호출자가 처리해야 한다.
        """

    @abstractmethod
    def fetch_order(self, order_id: str, symbol: str) -> Order | None:
        """주문 하나의 현재 상태. 없으면 None."""

    @abstractmethod
    def cancel_order(self, order_id: str, symbol: str) -> None:
        """주문 하나를 취소한다. 이미 체결·취소되었으면 조용히 넘어간다."""

    @abstractmethod
    def create_stop_loss_order(
        self, symbol: str, side: Side, amount: float, stop_price: float
    ) -> Order:
        """포지션을 줄이는 방향의 손절 주문(reduce-only)."""

    @abstractmethod
    def create_take_profit_order(
        self, symbol: str, side: Side, amount: float, take_profit_price: float
    ) -> Order:
        """포지션을 줄이는 방향의 익절 주문(reduce-only)."""

    @abstractmethod
    def fetch_open_orders(self, symbol: str) -> list[Order]:
        ...

    @abstractmethod
    def fetch_my_trades(self, symbol: str, since: int | None = None) -> list[Fill]:
        """내 체결 내역을 오래된 것부터 반환한다.

        봇이 낸 주문만으로는 기록이 불완전하다 — 손절·익절은 거래소에 걸어 두므로
        봇이 모르는 사이에 체결된다. 성과 계산은 이쪽을 근거로 해야 맞다.
        """

    @abstractmethod
    def cancel_all_orders(self, symbol: str) -> None:
        """미체결 주문을 모두 취소한다(조건부 주문 포함)."""

    # --- 규격 보정 ---
    @abstractmethod
    def amount_to_precision(self, symbol: str, amount: float) -> float:
        ...

    @abstractmethod
    def price_to_precision(self, symbol: str, price: float) -> float:
        ...

    # --- 수량 단위 환산 ---
    def base_to_contracts(self, symbol: str, base_amount: float) -> float:
        """베이스 코인 수량을 거래소 주문 단위로 환산하고 정밀도를 맞춘다.

        OKX 스왑은 주문 수량이 계약 수(1계약 = contractSize 베이스 코인)이고
        Bitget 스왑은 베이스 코인 단위(contractSize = 1)다. 상위 계층은 항상
        베이스 코인으로 생각하고, 이 한 줄이 두 거래소를 같게 만들어 준다.
        """
        contract_size = self.market(symbol).contract_size or 1.0
        return self.amount_to_precision(symbol, base_amount / contract_size)

    def contracts_to_base(self, symbol: str, contracts: float) -> float:
        """계약 수를 베이스 코인 수량으로 되돌린다."""
        return contracts * (self.market(symbol).contract_size or 1.0)

    def close(self) -> None:
        """네트워크 자원 정리. 필요 없으면 그대로 둔다."""
