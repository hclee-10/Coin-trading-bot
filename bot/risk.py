"""포지션 사이징과 계좌 보호 장치.

전략은 "어느 방향으로 갈지"만 정하고, "얼마나 걸지"와 "언제 멈출지"는 전부
여기서 정한다. 전략을 갈아 끼워도 계좌 보호 규칙은 그대로 남는다.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from datetime import datetime, timezone

from bot.config import RiskConfig
from bot.models import PositionSide, Signal

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class SizingDecision:
    """진입 요청에 대한 리스크 계층의 판정."""

    approved: bool
    reason: str
    base_amount: float = 0.0   # 베이스 코인 수량 (계약 환산은 어댑터가 한다)
    notional: float = 0.0      # 견적통화(USDT) 명목가
    stop_loss: float = 0.0
    take_profit: float | None = None


class RiskManager:
    """사이징 계산과 계좌 단위 한도·킬스위치를 담당한다."""

    def __init__(self, cfg: RiskConfig, leverage: float) -> None:
        self.cfg = cfg
        self.leverage = leverage
        self._day_key: str | None = None
        self._day_start_equity: float | None = None
        self._halted = False
        self._halt_reason = ""

    # ------------------------------------------------------------------
    # 킬스위치 — 일일 손실 한도
    # ------------------------------------------------------------------
    def update_equity(self, equity: float, *, now: datetime | None = None) -> None:
        """매 폴링 주기에 호출한다. UTC 일자가 바뀌면 기준 자기자본을 재설정한다."""
        now = now or datetime.now(timezone.utc)
        day_key = now.strftime("%Y-%m-%d")
        if day_key != self._day_key:
            self._day_key = day_key
            self._day_start_equity = equity
            if self._halted:
                log.info("UTC 일자 변경 — 킬스위치 해제 (기준 자기자본 %.2f)", equity)
            self._halted = False
            self._halt_reason = ""
            return

        if self._day_start_equity is None or self._day_start_equity <= 0:
            self._day_start_equity = equity
            return

        drawdown_pct = (self._day_start_equity - equity) / self._day_start_equity * 100
        if drawdown_pct >= self.cfg.max_daily_loss_pct and not self._halted:
            self._halted = True
            self._halt_reason = (
                f"일일 손실 한도 도달: -{drawdown_pct:.2f}% "
                f"(한도 {self.cfg.max_daily_loss_pct}%, "
                f"{self._day_start_equity:.2f} → {equity:.2f})"
            )
            log.error("킬스위치 발동 — %s", self._halt_reason)

    @property
    def halted(self) -> bool:
        """True 이면 신규 진입을 막는다. 청산은 계속 허용된다."""
        return self._halted

    @property
    def halt_reason(self) -> str:
        return self._halt_reason

    @property
    def day_start_equity(self) -> float | None:
        return self._day_start_equity

    # ------------------------------------------------------------------
    # 사이징
    # ------------------------------------------------------------------
    def evaluate_entry(
        self,
        *,
        signal: Signal,
        entry_price: float,
        equity: float,
        open_positions: int,
        min_notional: float | None = None,
    ) -> SizingDecision:
        """진입 신호를 실제 수량으로 바꾸거나, 이유를 붙여 거절한다."""
        side = signal.target_side
        if side is PositionSide.FLAT:
            return SizingDecision(False, "진입 신호가 아닙니다")
        if self._halted:
            return SizingDecision(False, f"킬스위치 작동 중 — {self._halt_reason}")
        if equity <= 0:
            return SizingDecision(False, "자기자본이 0 이하입니다")
        if entry_price <= 0:
            return SizingDecision(False, "진입가가 유효하지 않습니다")
        if open_positions >= self.cfg.max_open_positions:
            return SizingDecision(
                False,
                f"동시 보유 포지션 한도 초과 ({open_positions}/{self.cfg.max_open_positions})",
            )

        strength = max(0.0, min(1.0, signal.strength))
        if strength <= 0:
            return SizingDecision(False, "신호 강도가 0입니다")

        stop_loss = self.resolve_stop_loss(signal, entry_price, side)
        stop_distance = abs(entry_price - stop_loss)
        if stop_distance <= 0:
            return SizingDecision(False, "손절가와 진입가가 같습니다")
        if (side is PositionSide.LONG and stop_loss >= entry_price) or (
            side is PositionSide.SHORT and stop_loss <= entry_price
        ):
            return SizingDecision(
                False, f"손절가({stop_loss})가 {side.value} 포지션 방향과 맞지 않습니다"
            )

        if self.cfg.sizing_mode == "tiers":
            notional = self.notional_for(strength)
            base_amount = notional / entry_price
            basis = f"확신도 {strength:.2f} → 명목가 {notional:.0f}"
        else:
            # 손절까지 갔을 때 잃을 금액을 먼저 고정하고, 거기서 수량을 역산한다.
            risk_amount = equity * (self.cfg.risk_per_trade_pct / 100.0) * strength
            base_amount = risk_amount / stop_distance
            notional = base_amount * entry_price
            basis = f"위험금액 {risk_amount:.2f} / 손절폭 {stop_distance:.6f}"

        # 상한 두 개를 적용한다: 자기자본 대비 비중, 그리고 레버리지가 허용하는 한도.
        caps = [
            ("자기자본 대비 비중", equity * (self.cfg.max_position_notional_pct / 100.0)),
            ("레버리지 한도", equity * min(self.leverage, self.cfg.max_leverage)),
        ]
        for label, cap in caps:
            if cap > 0 and notional > cap:
                log.info("%s 상한 적용: 명목가 %.2f → %.2f", label, notional, cap)
                notional = cap
                base_amount = notional / entry_price

        floor = max(self.cfg.min_order_notional, min_notional or 0.0)
        if notional < floor:
            return SizingDecision(
                False,
                f"주문 명목가 {notional:.2f} USDT 가 최소 주문금액 {floor:.2f} 미만입니다",
            )

        return SizingDecision(
            approved=True,
            reason=f"{basis} → 수량 {base_amount:.8f} (명목가 {notional:.2f})",
            base_amount=base_amount,
            notional=notional,
            stop_loss=stop_loss,
            take_profit=self.resolve_take_profit(signal, entry_price, side),
        )

    def notional_for(self, strength: float) -> float:
        """확신도를 주문 명목가로 바꾼다.

        구간을 등분해서 매핑한다 — 등급이 4개면 0~0.25 가 첫 등급, 0.75~1.0 이
        마지막 등급이다. 전략은 Conviction 의 네 값 중 하나를 쓰면 정확히
        의도한 등급으로 떨어진다.
        """
        tiers = list(self.cfg.notional_tiers)
        if not tiers:
            return 0.0
        strength = max(0.0, min(1.0, strength))
        # 0 초과 값이 첫 등급에 들어가도록 올림 방식으로 나눈다.
        index = min(len(tiers) - 1, max(0, math.ceil(strength * len(tiers)) - 1))
        return tiers[index]

    # ------------------------------------------------------------------
    # 손절 / 익절 기본값
    # ------------------------------------------------------------------
    def resolve_stop_loss(
        self, signal: Signal, entry_price: float, side: PositionSide
    ) -> float:
        """전략이 손절가를 주면 그대로, 아니면 설정 비율로 만든다.

        손절 없는 진입은 허용하지 않는다 — 사이징 자체가 손절폭에 기반한다.
        """
        if signal.stop_loss is not None and signal.stop_loss > 0:
            return signal.stop_loss
        pct = self.cfg.default_stop_loss_pct / 100.0
        return entry_price * (1 - pct) if side is PositionSide.LONG else entry_price * (1 + pct)

    def resolve_take_profit(
        self, signal: Signal, entry_price: float, side: PositionSide
    ) -> float | None:
        """익절가. 설정이 0 이고 전략도 값을 안 주면 익절 주문을 걸지 않는다."""
        if signal.take_profit is not None and signal.take_profit > 0:
            return signal.take_profit
        if self.cfg.default_take_profit_pct <= 0:
            return None
        pct = self.cfg.default_take_profit_pct / 100.0
        return entry_price * (1 + pct) if side is PositionSide.LONG else entry_price * (1 - pct)
