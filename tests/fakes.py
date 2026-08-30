"""네트워크 없이 엔진 전체 경로를 돌리기 위한 가짜 거래소."""

from __future__ import annotations

import itertools

from bot.exchanges.base import FuturesExchange
from bot.models import Balance, Candle, Fill, Market, Order, Position, Ticker


class FakeExchange(FuturesExchange):
    id = "fake"

    def __init__(
        self,
        *,
        price: float = 100.0,
        equity: float = 1_000.0,
        contract_size: float = 1.0,
        candles: int = 100,
    ) -> None:
        self.price = price
        self.equity = equity
        self.contract_size = contract_size
        self.candle_count = candles
        self.positions: dict[str, Position] = {}
        self.my_trades: list[Fill] = []
        self.open_orders: dict[str, Order] = {}
        self.sent_orders: list[Order] = []
        self.cancelled: list[str] = []
        self.leverage_calls: list[tuple[str, float, str]] = []
        self._ids = itertools.count(1)

    # --- 시장 데이터 ---
    def load_markets(self) -> None:
        pass

    def market(self, symbol: str) -> Market:
        return Market(
            symbol=symbol,
            base=symbol.split("/")[0],
            quote="USDT",
            contract_size=self.contract_size,
            min_amount=0.001,
            min_notional=5.0,
            amount_precision=0.001,
            price_precision=0.01,
        )

    def fetch_candles(
        self, symbol: str, timeframe: str, limit: int, since: int | None = None
    ) -> list[Candle]:
        return [
            Candle(
                timestamp=1_700_000_000_000 + i * 60_000,
                open=self.price, high=self.price, low=self.price,
                close=self.price, volume=1.0,
            )
            for i in range(min(limit, self.candle_count))
        ]

    def fetch_ticker(self, symbol: str) -> Ticker:
        return Ticker(symbol=symbol, last=self.price, bid=self.price, ask=self.price, timestamp=0)

    # --- 계좌 ---
    def fetch_balance(self, currency: str) -> Balance:
        return Balance(currency=currency, free=self.equity, used=0.0, total=self.equity)

    def fetch_position(self, symbol: str) -> Position:
        return self.positions.get(symbol) or Position.flat(symbol)

    def set_leverage(self, symbol: str, leverage: float, margin_mode: str) -> None:
        self.leverage_calls.append((symbol, leverage, margin_mode))

    # --- 주문 ---
    def _record(self, symbol, side, amount, otype, reduce_only, price=None) -> Order:
        order = Order(
            id=f"o{next(self._ids)}", symbol=symbol, side=side, type=otype,
            amount=amount, price=price, status="closed", filled=amount,
            average=self.price, reduce_only=reduce_only,
        )
        self.sent_orders.append(order)
        return order

    def create_market_order(self, symbol, side, amount, *, reduce_only=False) -> Order:
        return self._record(symbol, side, amount, "market", reduce_only)

    def create_limit_order(self, symbol, side, amount, price, *,
                           reduce_only=False, post_only=True):
        order = self._record(symbol, side, amount, "limit", reduce_only, price)
        # 기본은 미체결 상태로 둔다 — 테스트가 원할 때 fill_order() 로 채운다.
        self.open_orders[order.id] = Order(
            id=order.id, symbol=symbol, side=side, type="limit", amount=amount,
            price=price, status="open", filled=0.0, reduce_only=reduce_only,
        )
        return self.open_orders[order.id]

    def fetch_order(self, order_id, symbol):
        return self.open_orders.get(order_id)

    def cancel_order(self, order_id, symbol):
        order = self.open_orders.get(order_id)
        if order:
            self.open_orders[order_id] = Order(
                id=order.id, symbol=order.symbol, side=order.side, type=order.type,
                amount=order.amount, price=order.price, status="canceled",
                filled=order.filled, reduce_only=order.reduce_only,
            )

    def fill_order(self, order_id, *, price=None):
        """테스트용 — 미체결 주문을 체결 처리한다."""
        order = self.open_orders[order_id]
        self.open_orders[order_id] = Order(
            id=order.id, symbol=order.symbol, side=order.side, type=order.type,
            amount=order.amount, price=price or order.price, status="closed",
            filled=order.amount, average=price or order.price,
            reduce_only=order.reduce_only,
        )

    def create_stop_loss_order(self, symbol, side, amount, stop_price) -> Order:
        return self._record(symbol, side, amount, "stop", True, stop_price)

    def create_take_profit_order(self, symbol, side, amount, take_profit_price) -> Order:
        return self._record(symbol, side, amount, "take_profit", True, take_profit_price)

    def fetch_open_orders(self, symbol):
        return []

    def fetch_my_trades(self, symbol, since=None):
        trades = [t for t in self.my_trades if t.symbol == symbol]
        if since is not None:
            trades = [t for t in trades if t.timestamp >= since]
        return sorted(trades, key=lambda t: t.timestamp)

    def cancel_all_orders(self, symbol: str) -> None:
        self.cancelled.append(symbol)

    # --- 규격 ---
    def amount_to_precision(self, symbol: str, amount: float) -> float:
        return round(amount, 3)

    def price_to_precision(self, symbol: str, price: float) -> float:
        return round(price, 2)
