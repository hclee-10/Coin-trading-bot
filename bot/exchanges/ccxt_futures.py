"""ccxt 기반 무기한 선물 어댑터 (Bitget / Gate / OKX 공용).

거래소별 차이는 이 파일 안에서만 다룬다:

* **수량 단위** — OKX 와 Gate 스왑은 주문 수량이 *계약 수*이고 1계약 =
  `contractSize` 베이스 코인이다(OKX BTC-USDT-SWAP 은 0.01 BTC, Gate BTC_USDT
  는 0.0001 BTC). Bitget 스왑은 베이스 코인 단위(contractSize = 1)다. 상위
  계층은 항상 베이스 코인 수량으로 생각하고, 계약 수 환산은
  `base_to_contracts()` 가 맡는다.
* **레버리지/마진 모드** — 셋이 전부 다르다. Bitget 격리는 롱/숏에 따로 걸어야
  하고, Gate 는 마진 모드 전용 API 가 없어 클라이언트 옵션으로 지정하며, OKX 는
  통합 파라미터를 받는다. 포지션이 열려 있으면 변경이 거부되므로 실패해도 봇을
  죽이지 않고 경고만 남긴다.
* **조건부 주문** — 손절/익절은 ccxt 통합 파라미터
  `stopLossPrice`/`takeProfitPrice` 로 보낸다. 취소·조회는 일반 주문과
  트리거 주문을 각각 훑는다.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Callable, TypeVar

import ccxt

from bot.exchanges.base import ExchangeError, FuturesExchange
from bot.models import (
    Balance,
    Candle,
    Fill,
    Market,
    Order,
    Position,
    PositionSide,
    Side,
    Ticker,
)

log = logging.getLogger(__name__)

T = TypeVar("T")

_RETRYABLE = (ccxt.NetworkError, ccxt.RequestTimeout, ccxt.DDoSProtection, ccxt.ExchangeNotAvailable)


class CcxtFuturesExchange(FuturesExchange):
    """ccxt 로 Bitget/OKX USDT 무기한 선물을 다루는 어댑터."""

    def __init__(
        self,
        exchange_id: str,
        api_key: str,
        secret: str,
        password: str,
        *,
        timeout_ms: int = 15_000,
        max_retries: int = 3,
    ) -> None:
        self.id = exchange_id
        self._max_retries = max_retries
        try:
            klass = getattr(ccxt, exchange_id)
        except AttributeError as exc:  # pragma: no cover - config 단계에서 걸러진다
            raise ExchangeError(f"ccxt 가 '{exchange_id}' 를 모릅니다") from exc
        self._ex = klass(
            {
                "apiKey": api_key,
                "secret": secret,
                "password": password,
                "timeout": timeout_ms,
                "enableRateLimit": True,
                "options": {"defaultType": "swap"},
            }
        )
        self._markets_loaded = False

    # ------------------------------------------------------------------
    # 내부 헬퍼
    # ------------------------------------------------------------------
    def _call(self, fn: Callable[..., T], *args: Any, **kwargs: Any) -> T:
        """일시적 네트워크 오류는 지수 백오프로 재시도하고, 그 외는 즉시 올린다."""
        delay = 1.0
        last: Exception | None = None
        for attempt in range(1, self._max_retries + 1):
            try:
                return fn(*args, **kwargs)
            except _RETRYABLE as exc:
                last = exc
                if attempt == self._max_retries:
                    break
                log.warning(
                    "%s 일시 오류 (%s/%s): %s — %.1fs 후 재시도",
                    fn.__name__, attempt, self._max_retries, exc, delay,
                )
                time.sleep(delay)
                delay *= 2
            except ccxt.BaseError as exc:
                raise ExchangeError(f"{self.id}.{fn.__name__} 실패: {exc}") from exc
        raise ExchangeError(f"{self.id}.{fn.__name__} 재시도 소진: {last}") from last

    def _require_markets(self) -> None:
        if not self._markets_loaded:
            self.load_markets()

    def _raw_market(self, symbol: str) -> dict[str, Any]:
        self._require_markets()
        try:
            return self._ex.market(symbol)
        except ccxt.BadSymbol as exc:
            raise ExchangeError(
                f"{self.id} 에 '{symbol}' 심볼이 없습니다. "
                "무기한 선물은 'BTC/USDT:USDT' 형식입니다."
            ) from exc

    # ------------------------------------------------------------------
    # 시장 데이터
    # ------------------------------------------------------------------
    def load_markets(self) -> None:
        self._call(self._ex.load_markets)
        self._markets_loaded = True

    def market(self, symbol: str) -> Market:
        m = self._raw_market(symbol)
        if not m.get("swap"):
            raise ExchangeError(f"'{symbol}' 은 무기한 선물(swap) 심볼이 아닙니다")
        limits = m.get("limits") or {}
        precision = m.get("precision") or {}
        return Market(
            symbol=m["symbol"],
            base=m["base"],
            quote=m["quote"],
            contract_size=float(m.get("contractSize") or 1.0),
            min_amount=_maybe_float((limits.get("amount") or {}).get("min")),
            max_amount=_maybe_float((limits.get("amount") or {}).get("max")),
            min_notional=_maybe_float((limits.get("cost") or {}).get("min")),
            amount_precision=_maybe_float(precision.get("amount")),
            price_precision=_maybe_float(precision.get("price")),
        )

    def fetch_candles(self, symbol: str, timeframe: str, limit: int) -> list[Candle]:
        self._require_markets()
        rows = self._call(self._ex.fetch_ohlcv, symbol, timeframe, None, limit)
        candles = [
            Candle(
                timestamp=int(r[0]),
                open=float(r[1]),
                high=float(r[2]),
                low=float(r[3]),
                close=float(r[4]),
                volume=float(r[5] or 0.0),
            )
            for r in rows
            if r and r[4] is not None
        ]
        candles.sort(key=lambda c: c.timestamp)
        return candles

    def fetch_ticker(self, symbol: str) -> Ticker:
        self._require_markets()
        t = self._call(self._ex.fetch_ticker, symbol)
        last = t.get("last") or t.get("close")
        if last is None:
            raise ExchangeError(f"{symbol} 의 최종가를 받지 못했습니다")
        return Ticker(
            symbol=t.get("symbol", symbol),
            last=float(last),
            bid=_maybe_float(t.get("bid")),
            ask=_maybe_float(t.get("ask")),
            timestamp=t.get("timestamp"),
        )

    # ------------------------------------------------------------------
    # 계좌
    # ------------------------------------------------------------------
    def fetch_balance(self, currency: str) -> Balance:
        self._require_markets()
        data = self._call(self._ex.fetch_balance, {"type": "swap"})
        entry = data.get(currency) or {}
        total = _maybe_float(entry.get("total"))
        if total is None:
            raise ExchangeError(
                f"{self.id} 선물 계좌에서 {currency} 잔고를 찾지 못했습니다. "
                "자금이 선물(스왑) 계좌로 이체되어 있는지 확인하세요."
            )
        return Balance(
            currency=currency,
            free=_maybe_float(entry.get("free")) or 0.0,
            used=_maybe_float(entry.get("used")) or 0.0,
            total=total,
        )

    def fetch_position(self, symbol: str) -> Position:
        self._require_markets()
        raw_positions = self._call(self._ex.fetch_positions, [symbol])
        for p in raw_positions or []:
            if p.get("symbol") != symbol:
                continue
            contracts = _maybe_float(p.get("contracts")) or 0.0
            if contracts <= 0:
                continue
            side_raw = (p.get("side") or "").lower()
            side = PositionSide.LONG if side_raw == "long" else PositionSide.SHORT
            return Position(
                symbol=symbol,
                side=side,
                contracts=contracts,
                entry_price=_maybe_float(p.get("entryPrice")),
                notional=abs(_maybe_float(p.get("notional")) or 0.0),
                leverage=_maybe_float(p.get("leverage")),
                unrealized_pnl=_maybe_float(p.get("unrealizedPnl")) or 0.0,
                liquidation_price=_maybe_float(p.get("liquidationPrice")),
                raw=p,
            )
        return Position.flat(symbol)

    def set_leverage(self, symbol: str, leverage: float, margin_mode: str) -> None:
        """레버리지·마진 모드 설정.

        포지션이나 미체결 주문이 있으면 거래소가 거부한다. 그 경우 기존 설정을
        그대로 쓰는 것이 맞으므로 경고만 남기고 진행한다.
        """
        self._require_markets()

        # Gate 에는 마진 모드 전용 엔드포인트가 없다 — set_leverage 가 옵션으로
        # 읽어 간다. 지원하지 않는 거래소에서 굳이 호출해 경고를 남기지 않는다.
        if self._ex.has.get("setMarginMode"):
            try:
                self._ex.set_margin_mode(margin_mode, symbol, {"leverage": leverage})
            except ccxt.BaseError as exc:
                log.warning("%s %s 마진 모드(%s) 설정 실패 — 기존 설정 유지: %s",
                            self.id, symbol, margin_mode, exc)

        for params in self._leverage_params(margin_mode):
            try:
                self._ex.set_leverage(leverage, symbol, params)
            except ccxt.BaseError as exc:
                log.warning("%s %s 레버리지(%s) 설정 실패 — 기존 설정 유지: %s",
                            self.id, symbol, leverage, exc)

    def _leverage_params(self, margin_mode: str) -> list[dict[str, Any]]:
        """거래소별 set_leverage 파라미터. 여러 개면 순서대로 모두 호출한다."""
        if self.id == "bitget" and margin_mode == "isolated":
            # Bitget 격리 마진은 방향(holdSide)별로 레버리지를 따로 잡는다.
            return [{"holdSide": "long"}, {"holdSide": "short"}]
        if self.id == "gate":
            # Gate 는 params 로 넘긴 marginMode 를 걸러내지 않고 그대로 요청에
            # 실어 보낸다. 클라이언트 옵션으로 지정하면 그 경로를 피할 수 있다.
            self._ex.options["marginMode"] = margin_mode
            return [{}]
        return [{"marginMode": margin_mode}]

    # ------------------------------------------------------------------
    # 주문
    # ------------------------------------------------------------------
    def create_market_order(
        self, symbol: str, side: Side, amount: float, *, reduce_only: bool = False
    ) -> Order:
        params: dict[str, Any] = {}
        if reduce_only:
            params["reduceOnly"] = True
        raw = self._call(
            self._ex.create_order, symbol, "market", side.value, amount, None, params
        )
        return _to_order(raw, symbol, side, amount, reduce_only)

    def create_stop_loss_order(
        self, symbol: str, side: Side, amount: float, stop_price: float
    ) -> Order:
        params = {"reduceOnly": True, "stopLossPrice": self.price_to_precision(symbol, stop_price)}
        raw = self._call(
            self._ex.create_order, symbol, "market", side.value, amount, None, params
        )
        return _to_order(raw, symbol, side, amount, True)

    def create_take_profit_order(
        self, symbol: str, side: Side, amount: float, take_profit_price: float
    ) -> Order:
        params = {
            "reduceOnly": True,
            "takeProfitPrice": self.price_to_precision(symbol, take_profit_price),
        }
        raw = self._call(
            self._ex.create_order, symbol, "market", side.value, amount, None, params
        )
        return _to_order(raw, symbol, side, amount, True)

    def fetch_open_orders(self, symbol: str) -> list[Order]:
        self._require_markets()
        orders: list[Order] = []
        for params in ({}, {"trigger": True}):
            try:
                raw_list = self._ex.fetch_open_orders(symbol, params=params)
            except ccxt.BaseError as exc:
                log.debug("%s %s 미체결 조회 실패 (params=%s): %s", self.id, symbol, params, exc)
                continue
            for raw in raw_list or []:
                side = Side.BUY if (raw.get("side") or "buy").lower() == "buy" else Side.SELL
                orders.append(
                    _to_order(raw, symbol, side, _maybe_float(raw.get("amount")) or 0.0,
                              bool(raw.get("reduceOnly")))
                )
        return orders

    def fetch_my_trades(self, symbol: str, since: int | None = None) -> list[Fill]:
        self._require_markets()
        raw_list = self._call(self._ex.fetch_my_trades, symbol, since, None, {})
        fills: list[Fill] = []
        for raw in raw_list or []:
            trade_id = raw.get("id") or raw.get("order")
            timestamp = raw.get("timestamp")
            price = _maybe_float(raw.get("price"))
            amount = _maybe_float(raw.get("amount"))
            if not trade_id or timestamp is None or price is None or amount is None:
                continue
            fee = raw.get("fee") or {}
            fills.append(
                Fill(
                    id=str(trade_id),
                    symbol=raw.get("symbol") or symbol,
                    timestamp=int(timestamp),
                    side=Side.BUY if (raw.get("side") or "buy").lower() == "buy" else Side.SELL,
                    price=price,
                    amount=abs(amount),
                    cost=_maybe_float(raw.get("cost")) or 0.0,
                    fee=abs(_maybe_float(fee.get("cost")) or 0.0),
                )
            )
        fills.sort(key=lambda f: f.timestamp)
        return fills

    def cancel_all_orders(self, symbol: str) -> None:
        """일반 주문과 트리거(손절/익절) 주문을 모두 취소한다.

        거래소마다 대량 취소 엔드포인트의 트리거 주문 포함 여부가 달라서,
        조회 후 개별 취소하는 방식이 가장 이식성이 높다.
        """
        self._require_markets()
        failures: list[str] = []
        for params in ({}, {"trigger": True}):
            try:
                raw_list = self._ex.fetch_open_orders(symbol, params=params)
            except ccxt.BaseError as exc:
                log.debug("%s %s 미체결 조회 실패 (params=%s): %s", self.id, symbol, params, exc)
                continue
            for raw in raw_list or []:
                order_id = raw.get("id")
                if not order_id:
                    continue
                try:
                    self._ex.cancel_order(order_id, symbol, params)
                except ccxt.OrderNotFound:
                    pass  # 그 사이 체결/취소됨 — 원하던 상태다
                except ccxt.BaseError as exc:
                    failures.append(f"{order_id}: {exc}")
        if failures:
            raise ExchangeError(f"{symbol} 주문 취소 실패 — " + "; ".join(failures))

    # ------------------------------------------------------------------
    # 규격 보정
    # ------------------------------------------------------------------
    def amount_to_precision(self, symbol: str, amount: float) -> float:
        self._require_markets()
        return float(self._ex.amount_to_precision(symbol, amount))

    def price_to_precision(self, symbol: str, price: float) -> float:
        self._require_markets()
        return float(self._ex.price_to_precision(symbol, price))

    def close(self) -> None:
        closer = getattr(self._ex, "close", None)
        if callable(closer):
            try:
                closer()
            except Exception:  # pragma: no cover - 종료 경로에서 삼킨다
                log.debug("%s 세션 종료 중 오류", self.id, exc_info=True)


def _maybe_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _to_order(
    raw: dict[str, Any], symbol: str, side: Side, amount: float, reduce_only: bool
) -> Order:
    return Order(
        id=raw.get("id"),
        symbol=raw.get("symbol") or symbol,
        side=side,
        type=raw.get("type") or "market",
        amount=_maybe_float(raw.get("amount")) or amount,
        price=_maybe_float(raw.get("price")),
        status=raw.get("status"),
        filled=_maybe_float(raw.get("filled")) or 0.0,
        average=_maybe_float(raw.get("average")),
        reduce_only=reduce_only,
        raw=raw,
    )
