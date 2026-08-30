"""신호를 실제 주문으로 옮기는 계층.

여기가 주문이 나가는 유일한 지점이다. `dry_run=True` 면 사이징·규격 보정까지
전부 수행하고 전송만 건너뛰므로, 실제 자금 없이 전체 경로를 점검할 수 있다.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from bot.exchanges.base import ExchangeError, FuturesExchange
from bot.models import Position, PositionSide, Side, Signal, SignalAction
from bot.risk import RiskManager, SizingDecision

log = logging.getLogger(__name__)


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
    ) -> None:
        self.exchange = exchange
        self.risk = risk
        self.dry_run = dry_run
        self.allow_reverse = allow_reverse

    # ------------------------------------------------------------------
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
            return self._close(symbol, position, reason=signal.reason or "전략 청산 신호")

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
            close_result = self._close(symbol, position, reason="반대 신호로 전환")
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
        summary = (
            f"{signal.target_side.value} 진입 {amount} @ ~{price} "
            f"(손절 {decision.stop_loss:.6g}"
            + (f", 익절 {decision.take_profit:.6g}" if decision.take_profit else "")
            + f") — {signal.reason or decision.reason}"
        )

        if self.dry_run:
            log.info("[DRY-RUN] %s %s", symbol, summary)
            return ExecutionResult(symbol, "entered", f"[DRY-RUN] {summary}")

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
    def _close(self, symbol: str, position: Position, *, reason: str) -> ExecutionResult:
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

        order = self.exchange.create_market_order(
            symbol, side, position.contracts, reduce_only=True
        )
        log.info("%s %s", symbol, summary)
        return ExecutionResult(symbol, "exited", summary, [order.id] if order.id else [])

    # ------------------------------------------------------------------
