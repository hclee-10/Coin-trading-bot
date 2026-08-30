"""신호를 실제 주문으로 옮기는 계층.

여기가 주문이 나가는 유일한 지점이다. `dry_run=True` 면 사이징·규격 보정까지
전부 수행하고 전송만 건너뛰므로, 실제 자금 없이 전체 경로를 점검할 수 있다.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

from bot.exchanges.base import ExchangeError, FuturesExchange
from bot.models import Position, PositionSide, Side, Signal, SignalAction
from bot.risk import RiskManager, SizingDecision

log = logging.getLogger(__name__)


@dataclass
class PendingEntry:
    """아직 체결되지 않은 지정가 진입 주문.

    체결을 확인한 다음에야 손절 주문을 걸 수 있으므로, 그 사이 정보를 들고 있어야
    한다. 봇이 재시작되면 이 상태가 사라지고 주문만 거래소에 남는다 — 그래서
    기동 시 미체결 주문을 로그로 알린다.
    """

    order_id: str
    symbol: str
    side: Side
    target_side: PositionSide
    amount: float
    price: float
    stop_loss: float
    take_profit: float | None
    placed_at: float
    reason: str

    def expired(self, timeout: float, *, now: float | None = None) -> bool:
        return (now if now is not None else time.monotonic()) - self.placed_at >= timeout


@dataclass
class ExecutionResult:
    """한 심볼에 대한 이번 주기의 처리 결과."""

    symbol: str
    action: str            # "none" | "entered" | "exited" | "reversed" | "rejected"
    detail: str = ""
    orders: list[str] = field(default_factory=list)  # 전송한 주문 id (dry-run 은 비어 있음)

    @property
    def traded(self) -> bool:
        return self.action in ("entered", "exited", "reversed")


class Executor:
    """신호 + 리스크 판정 → 주문."""

    def __init__(
        self,
        exchange: FuturesExchange,
        risk: RiskManager,
        *,
        dry_run: bool = True,
        allow_reverse: bool = False,
        order_type: str = "market",
        limit_offset_pct: float = 0.02,
        limit_timeout_sec: float = 60.0,
        limit_fallback_market: bool = False,
    ) -> None:
        self.exchange = exchange
        self.risk = risk
        self.dry_run = dry_run
        self.allow_reverse = allow_reverse
        self.order_type = order_type
        self.limit_offset_pct = limit_offset_pct
        self.limit_timeout_sec = limit_timeout_sec
        self.limit_fallback_market = limit_fallback_market
        self.pending: dict[str, PendingEntry] = {}

    # ------------------------------------------------------------------
    def reconcile(self, symbol: str, position: Position) -> ExecutionResult | None:
        """미체결 지정가 주문의 상태를 확인한다. 매 주기 전략보다 먼저 호출한다.

        체결됐으면 그때서야 손절 주문을 건다. 시간이 지나도 안 채워졌으면
        취소한다 — 신호가 나온 지 한참 지난 가격에 체결되는 것이 더 나쁘다.
        """
        entry = self.pending.get(symbol)
        if entry is None:
            return None

        try:
            order = self.exchange.fetch_order(entry.order_id, symbol)
        except ExchangeError as exc:
            log.warning("%s 미체결 주문 조회 실패 — 다음 주기에 다시 확인합니다: %s", symbol, exc)
            return None

        status = (order.status or "").lower() if order else "canceled"
        if order is None or status in ("canceled", "expired", "rejected"):
            self.pending.pop(symbol, None)
            return ExecutionResult(symbol, "none", f"지정가 주문 취소됨 — {entry.reason}")

        if status == "closed" or (order.filled and order.filled >= entry.amount * 0.999):
            self.pending.pop(symbol, None)
            filled_price = order.average or entry.price
            ids = [entry.order_id] + self._place_protective_orders(
                symbol, entry.side.opposite, entry.amount,
                SizingDecision(True, "", stop_loss=entry.stop_loss,
                               take_profit=entry.take_profit),
            )
            log.info("%s 지정가 진입 체결 @ %s — 보호주문 등록", symbol, filled_price)
            return ExecutionResult(
                symbol, "entered",
                f"{entry.target_side.value} 지정가 체결 {entry.amount} @ {filled_price} "
                f"(손절 {entry.stop_loss:.6g}) — {entry.reason}",
                ids,
            )

        if entry.expired(self.limit_timeout_sec):
            return self._cancel_pending(symbol, entry, position)

        return ExecutionResult(symbol, "none", f"지정가 체결 대기 중 @ {entry.price}")

    def _cancel_pending(
        self, symbol: str, entry: PendingEntry, position: Position
    ) -> ExecutionResult:
        try:
            self.exchange.cancel_order(entry.order_id, symbol)
        except ExchangeError:
            log.warning("%s 미체결 주문 취소 실패 — 다음 주기에 다시 시도합니다", symbol,
                        exc_info=True)
            return ExecutionResult(symbol, "none", "지정가 주문 취소 실패")

        self.pending.pop(symbol, None)
        if not self.limit_fallback_market:
            return ExecutionResult(
                symbol, "none",
                f"지정가 미체결로 취소 ({self.limit_timeout_sec:.0f}초 경과) — {entry.reason}",
            )

        # 시장가로 잡으러 간다. 신호를 놓치지 않는 대신 taker 수수료를 낸다.
        log.info("%s 지정가 미체결 → 시장가로 진입합니다", symbol)
        order = self.exchange.create_market_order(symbol, entry.side, entry.amount)
        ids = [order.id] if order.id else []
        ids += self._place_protective_orders(
            symbol, entry.side.opposite, entry.amount,
            SizingDecision(True, "", stop_loss=entry.stop_loss, take_profit=entry.take_profit),
        )
        return ExecutionResult(
            symbol, "entered",
            f"{entry.target_side.value} 시장가 대체 진입 {entry.amount} — {entry.reason}",
            ids,
        )

    def handle(
        self,
        *,
        symbol: str,
        signal: Signal,
        position: Position,
        price: float,
        equity: float,
        open_positions: int,
    ) -> ExecutionResult:
        if signal.action is SignalAction.HOLD:
            return ExecutionResult(symbol, "none", signal.reason or "hold")

        if signal.action is SignalAction.EXIT:
            if not position.is_open:
                return ExecutionResult(symbol, "none", "청산할 포지션이 없습니다")
            return self._close(symbol, position, reason=signal.reason or "전략 청산 신호",
                               price=price)

        # 여기부터는 진입 신호
        target = signal.target_side
        if position.is_open:
            if position.side is target:
                return ExecutionResult(
                    symbol, "none", f"이미 {target.value} 포지션 보유 — 추가 진입하지 않음"
                )
            if not self.allow_reverse:
                return ExecutionResult(
                    symbol,
                    "none",
                    f"반대({target.value}) 신호이나 allow_reverse=false — 유지",
                )
            close_result = self._close(symbol, position, reason="반대 신호로 전환", price=price)
            if close_result.action != "exited":
                return close_result
            position = Position.flat(symbol)
            open_positions = max(0, open_positions - 1)
            entry = self._open(symbol, signal, price, equity, open_positions)
            if entry.action == "entered":
                return ExecutionResult(
                    symbol, "reversed",
                    f"{close_result.detail} → {entry.detail}",
                    close_result.orders + entry.orders,
                )
            return ExecutionResult(
                symbol, "exited", f"{close_result.detail} (재진입 실패: {entry.detail})",
                close_result.orders,
            )

        return self._open(symbol, signal, price, equity, open_positions)

    # ------------------------------------------------------------------
    def _open(
        self, symbol: str, signal: Signal, price: float, equity: float, open_positions: int
    ) -> ExecutionResult:
        market = self.exchange.market(symbol)
        decision = self.risk.evaluate_entry(
            signal=signal,
            entry_price=price,
            equity=equity,
            open_positions=open_positions,
            min_notional=market.min_notional,
        )
        if not decision.approved:
            return ExecutionResult(symbol, "rejected", decision.reason)

        amount = self.exchange.base_to_contracts(symbol, decision.base_amount)
        if amount <= 0:
            return ExecutionResult(
                symbol, "rejected",
                f"수량을 거래소 규격에 맞추면 0이 됩니다 (요청 {decision.base_amount:.8f})",
            )
        if market.min_amount is not None and amount < market.min_amount:
            return ExecutionResult(
                symbol, "rejected",
                f"수량 {amount} 이 최소 주문수량 {market.min_amount} 미만입니다",
            )

        side = Side.BUY if signal.target_side is PositionSide.LONG else Side.SELL
        exit_side = side.opposite
        use_limit = self.order_type == "limit"
        limit_price = self._limit_price(symbol, price, side)
        summary = (
            f"{signal.target_side.value} {'지정가' if use_limit else '시장가'} 진입 "
            f"{amount} @ {limit_price if use_limit else f'~{price}'} "
            f"(손절 {decision.stop_loss:.6g}"
            + (f", 익절 {decision.take_profit:.6g}" if decision.take_profit else "")
            + f") — {signal.reason or decision.reason}"
        )

        if self.dry_run:
            log.info("[DRY-RUN] %s %s", symbol, summary)
            return ExecutionResult(symbol, "entered", f"[DRY-RUN] {summary}")

        if use_limit:
            order = self.exchange.create_limit_order(
                symbol, side, amount, limit_price, post_only=True
            )
            if not order.id:
                return ExecutionResult(symbol, "rejected", "지정가 주문 id 를 받지 못했습니다")
            # 손절은 체결을 확인한 뒤에 건다 — 체결되지도 않은 포지션에
            # reduce-only 주문을 걸면 거래소가 거부하거나 엉뚱하게 남는다.
            self.pending[symbol] = PendingEntry(
                order_id=order.id, symbol=symbol, side=side,
                target_side=signal.target_side, amount=amount, price=limit_price,
                stop_loss=decision.stop_loss, take_profit=decision.take_profit,
                placed_at=time.monotonic(), reason=signal.reason or decision.reason,
            )
            log.info("%s 지정가 주문 등록: %s", symbol, summary)
            return ExecutionResult(symbol, "none", f"지정가 주문 등록 — {summary}", [order.id])

        order_ids: list[str] = []
        entry_order = self.exchange.create_market_order(symbol, side, amount)
        if entry_order.id:
            order_ids.append(entry_order.id)
        log.info("%s 진입 체결 요청: %s", symbol, summary)

        # 보호주문은 진입 이후에만 의미가 있다. 실패해도 포지션은 이미 열렸으므로
        # 예외로 죽지 말고, 대신 크게 경고해서 사람이 개입할 수 있게 한다.
        protective = self._place_protective_orders(
            symbol, exit_side, amount, decision
        )
        order_ids.extend(protective)
        return ExecutionResult(symbol, "entered", summary, order_ids)

    def _place_protective_orders(
        self, symbol: str, exit_side: Side, amount: float, decision: SizingDecision
    ) -> list[str]:
        ids: list[str] = []
        try:
            sl = self.exchange.create_stop_loss_order(
                symbol, exit_side, amount, decision.stop_loss
            )
            if sl.id:
                ids.append(sl.id)
        except ExchangeError:
            log.error(
                "%s 손절 주문(%s) 등록 실패 — 포지션이 보호되지 않은 상태입니다. "
                "거래소에서 직접 손절을 걸어 주세요.",
                symbol, decision.stop_loss, exc_info=True,
            )
        if decision.take_profit:
            try:
                tp = self.exchange.create_take_profit_order(
                    symbol, exit_side, amount, decision.take_profit
                )
                if tp.id:
                    ids.append(tp.id)
            except ExchangeError:
                log.warning("%s 익절 주문 등록 실패 — 손절만 유지됩니다", symbol, exc_info=True)
        return ids

    # ------------------------------------------------------------------
    def _limit_price(self, symbol: str, price: float, side: Side) -> float:
        """지정가를 현재가에서 유리한 쪽으로 벌린다.

        매수는 조금 아래, 매도는 조금 위에 건다. post-only 가 거부되지 않게 하고
        maker 수수료를 확보하기 위해서다. 너무 벌리면 체결되지 않는다.
        """
        offset = price * (self.limit_offset_pct / 100.0)
        target = price - offset if side is Side.BUY else price + offset
        return self.exchange.price_to_precision(symbol, target)

    def _close(
        self, symbol: str, position: Position, *, reason: str, price: float = 0.0
    ) -> ExecutionResult:
        side = Side.SELL if position.side is PositionSide.LONG else Side.BUY
        summary = f"{position.side.value} {position.contracts} 청산 — {reason}"

        if self.dry_run:
            log.info("[DRY-RUN] %s %s", symbol, summary)
            return ExecutionResult(symbol, "exited", f"[DRY-RUN] {summary}")

        # 먼저 보호주문을 걷어낸다. 남겨 두면 청산 뒤에 반대 포지션을 열 수 있다.
        try:
            self.exchange.cancel_all_orders(symbol)
        except ExchangeError:
            log.warning("%s 기존 주문 취소 실패 — 청산은 계속 진행합니다", symbol, exc_info=True)

        # 청산도 지정가로 시도하되, post-only 를 쓰지 않는다 — 포지션을 못 닫고
        # 주문이 거부되는 것보다 조금 불리한 가격에라도 나가는 편이 낫다.
        if self.order_type == "limit" and price > 0:
            try:
                order = self.exchange.create_limit_order(
                    symbol, side, position.contracts,
                    self._limit_price(symbol, price, side),
                    reduce_only=True, post_only=False,
                )
                log.info("%s %s (지정가)", symbol, summary)
                return ExecutionResult(symbol, "exited", summary, [order.id] if order.id else [])
            except ExchangeError:
                log.warning("%s 지정가 청산 실패 — 시장가로 청산합니다", symbol, exc_info=True)

        order = self.exchange.create_market_order(
            symbol, side, position.contracts, reduce_only=True
        )
        log.info("%s %s", symbol, summary)
        return ExecutionResult(symbol, "exited", summary, [order.id] if order.id else [])

    # ------------------------------------------------------------------
