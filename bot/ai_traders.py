"""AI 경쟁 매매 — 클로드·GPT·그록이 각자 가상 계좌로 경쟁한다.

전략 코드가 아니라 **AI 의 판단을 사람이 전달**해서 매매한다. 흐름:

1. 대시보드에서 "프롬프트 복사" — 현재 시세·그 AI 의 계좌 상태가 담긴
   동일한 질문지가 만들어진다 (모든 AI 가 같은 정보를 받는다).
2. 그 프롬프트를 각 AI 의 웹(챗지피티, 그록, 클로드)에 붙여넣는다.
3. AI 의 답(롱/숏 금액, 청산, 관망)을 대시보드 주문칸에 입력한다.
4. 다음 봇 주기(15초)에 그 AI 의 가상 계좌로 체결된다.

전략 카탈로그에는 등록하지 않는다 — 스스로 신호를 만들지 못하므로 전략 계약
테스트(무작위 데이터에서 진입해야 함 등)의 대상이 아니다. 대신 모의매매
아레나에 `extra_strategies` 로 합류해 같은 시세·같은 수수료·같은 펀딩 규칙로
순위표에서 알고리즘 전략들과 직접 경쟁한다.

주문 큐는 메모리에만 있다 — 재배포되면 아직 체결되지 않은 주문은 사라진다.
입력 후 15초 안에 체결되므로 실제로 잃을 수 있는 창은 짧고, 유실되면
대시보드의 대기 목록에서 사라진 것이 보인다.
"""

from __future__ import annotations

import threading
from collections import deque
from typing import Any

from bot.models import Conviction, PositionSide, Signal, SignalAction
from bot.strategies.base import Strategy, StrategyContext

# 이름 → 화면 표기. 이름은 계좌 키가 되므로 바꾸면 새 계좌로 취급된다.
AI_TRADERS: dict[str, str] = {
    "ai_claude": "클로드",
    "ai_gpt": "GPT",
    "ai_grok": "그록",
}

VALID_ACTIONS = ("long", "short", "close")


class ManualTrader(Strategy):
    """대시보드로 전달받은 주문을 다음 주기에 그대로 내는 '전략'.

    주문이 없으면 영원히 HOLD 다. 판단은 전부 바깥(AI)에서 온다.
    """

    category = "ai"

    def __init__(self, name: str, label: str) -> None:
        self.name = name
        self.label = label
        self.summary = f"AI 수동 매매 — {label}의 판단을 대시보드로 전달받아 체결"
        self._lock = threading.Lock()
        self._queue: deque[dict[str, Any]] = deque()
        super().__init__({})

    # ------------------------------------------------------------------
    def submit(self, action: str, notional: float = 0.0) -> dict[str, Any]:
        """주문을 큐에 넣는다. 다음 봇 주기에 체결된다."""
        if action not in VALID_ACTIONS:
            raise ValueError(f"알 수 없는 동작 '{action}' (long/short/close)")
        if action != "close" and notional <= 0:
            raise ValueError("롱/숏 주문에는 금액(USDT)이 필요합니다")
        order = {"action": action, "notional": float(notional)}
        with self._lock:
            self._queue.append(order)
        return order

    def pending(self) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(o) for o in self._queue]

    def clear_pending(self) -> int:
        with self._lock:
            count = len(self._queue)
            self._queue.clear()
        return count

    # ------------------------------------------------------------------
    def generate(self, ctx: StrategyContext) -> Signal:
        with self._lock:
            order = self._queue.popleft() if self._queue else None
        if order is None:
            if ctx.position.is_open:
                return Signal(reason=f"{self.label} 지시 대기 중 (보유 유지)")
            return Signal(reason=f"{self.label} 지시 대기 중")

        price = ctx.last_price
        if order["action"] == "close":
            if not ctx.position.is_open:
                return Signal(reason=f"{self.label} 청산 지시 — 보유 포지션 없음 (무시)")
            return Signal(action=SignalAction.EXIT, reason=f"{self.label} 청산 지시")

        side = PositionSide.LONG if order["action"] == "long" else PositionSide.SHORT
        # 손절은 걸지 않는다 — AI 가 청산 지시로 직접 관리한다. 시스템 규칙
        # (진입에는 손절가 필수)을 지키기 위한 명목값만 붙인다.
        stop = price * 0.02 if side is PositionSide.LONG else price * 3.0
        return Signal(
            action=SignalAction.ENTER_LONG if side is PositionSide.LONG
            else SignalAction.ENTER_SHORT,
            strength=Conviction.LOW.value,
            stop_loss=stop,
            # accumulate: 같은 방향은 쌓고 반대 방향은 상계 — 알고리즘 적립식과
            # 같은 넷팅 규칙. notional: AI 가 정한 금액이 회당 고정 금액 대신
            # 쓰인다.
            metadata={"accumulate": True, "notional": order["notional"]},
            reason=f"{self.label} 지시 — {order['action']} {order['notional']:.0f} USDT",
        )


def build_traders() -> dict[str, ManualTrader]:
    return {name: ManualTrader(name, label) for name, label in AI_TRADERS.items()}
