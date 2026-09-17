"""AI 경쟁 매매 — 클로드·GPT·그록·제미나이가 각자 봇을 프로그래밍해 경쟁한다.

각 AI 는 **전용 접근 토큰**으로 이 웹의 API 를 호출해 자기 봇을 조작한다:

* `GET  /api/aibot/state` — 시세·자기 계좌·현재 봇 스펙 (토큰 인증)
* `POST /api/aibot/spec`  — 봇 스펙(JSON) 교체 (토큰 인증)

봇 스펙은 제한된 DSL 이다 — 임의 코드가 아니라 검증된 규칙만 받는다. 봇은
매 주기(15초) 스펙의 규칙을 평가해 주문을 내고, AI 는 6시간마다 스펙을
갱신해 성능을 다듬는다. 토큰이 없는(또는 API 를 못 부르는) AI 는 스펙 JSON 을
답으로 내고 사람이 대시보드에 붙여넣으면 된다 — 어느 쪽이든 같은 스펙이다.

대회 규칙(서버가 강제하는 것):
* 회당 주문 금액 ≤ 5,000 USDT
* 레버리지 1~10배 (스펙에서 선택, 노출 한도 = 자기자본 × 레버리지)
* 시장 대비 -5%p 손실 규칙 — 수익률이 (BTC 수익률 - 5%p) 아래로 내려가면
  노출을 늘리는 주문이 차단된다 (청산·상계는 허용). 아레나가 강제한다.
* 수수료 taker 0.05%·펀딩비 8시간마다 — 모의매매가 항상 부과한다.
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
    "ai_gemini": "제미나이",
}

VALID_ACTIONS = ("long", "short", "close")

# 대회 규칙 상수. 스펙 검증과 지시문(MD)이 같은 값을 쓴다.
MAX_ORDER_NOTIONAL = 5_000.0
MAX_LEVERAGE = 10.0
MIN_LEVERAGE = 1.0
MAX_BEHIND_MARKET_PCT = 5.0     # 시장 대비 이만큼 뒤지면 노출 증가 차단
RULE_TIMEFRAMES = ("1m", "5m", "15m", "1h", "4h")
MAX_RULES = 10
MAX_IMMEDIATE_ORDERS = 5


def validate_spec(raw: Any) -> tuple[dict[str, Any], list[str]]:
    """봇 스펙을 검증하고 정리한다. (정리된 스펙, 오류 목록) 을 돌려준다.

    오류 메시지는 AI 가 읽고 고칠 수 있게 무엇이 왜 틀렸는지 구체적으로 쓴다.
    """
    errors: list[str] = []
    if not isinstance(raw, dict):
        return {}, ["스펙은 JSON 객체여야 합니다"]

    clean: dict[str, Any] = {}

    leverage = raw.get("leverage", 3)
    try:
        leverage = float(leverage)
    except (TypeError, ValueError):
        leverage = -1
    if not (MIN_LEVERAGE <= leverage <= MAX_LEVERAGE):
        errors.append(f"leverage 는 {MIN_LEVERAGE:g}~{MAX_LEVERAGE:g} 사이여야 합니다")
    else:
        clean["leverage"] = leverage

    def check_notional(value, where):
        try:
            value = float(value)
        except (TypeError, ValueError):
            errors.append(f"{where}: notional 이 숫자가 아닙니다")
            return None
        if not (1.0 <= value <= MAX_ORDER_NOTIONAL):
            errors.append(
                f"{where}: notional 은 1~{MAX_ORDER_NOTIONAL:.0f} USDT 사이여야 합니다"
                " (회당 주문 한도)"
            )
            return None
        return value

    orders = raw.get("orders", [])
    if not isinstance(orders, list) or len(orders) > MAX_IMMEDIATE_ORDERS:
        errors.append(f"orders 는 최대 {MAX_IMMEDIATE_ORDERS}개의 배열이어야 합니다")
        orders = []
    clean_orders = []
    for i, order in enumerate(orders):
        where = f"orders[{i}]"
        if not isinstance(order, dict) or order.get("side") not in VALID_ACTIONS:
            errors.append(f"{where}: side 는 long/short/close 중 하나여야 합니다")
            continue
        if order["side"] == "close":
            clean_orders.append({"side": "close"})
            continue
        notional = check_notional(order.get("notional"), where)
        if notional is not None:
            clean_orders.append({"side": order["side"], "notional": notional})
    clean["orders"] = clean_orders

    rules = raw.get("rules", [])
    if not isinstance(rules, list) or len(rules) > MAX_RULES:
        errors.append(f"rules 는 최대 {MAX_RULES}개의 배열이어야 합니다")
        rules = []
    clean_rules = []
    for i, rule in enumerate(rules):
        where = f"rules[{i}]"
        if not isinstance(rule, dict):
            errors.append(f"{where}: 객체여야 합니다")
            continue
        ok = True
        timeframe = rule.get("timeframe", "5m")
        if timeframe not in RULE_TIMEFRAMES:
            errors.append(f"{where}: timeframe 은 {'/'.join(RULE_TIMEFRAMES)} 중 하나여야 합니다")
            ok = False
        try:
            bars = int(rule.get("bars", 1))
        except (TypeError, ValueError):
            bars = -1
        if not (1 <= bars <= 96):
            errors.append(f"{where}: bars 는 1~96 사이 정수여야 합니다")
            ok = False
        op = rule.get("op", "lte")
        if op not in ("lte", "gte"):
            errors.append(f"{where}: op 는 lte(이하) 또는 gte(이상)여야 합니다")
            ok = False
        try:
            change = float(rule.get("change_pct"))
        except (TypeError, ValueError):
            errors.append(f"{where}: change_pct 는 숫자(%)여야 합니다")
            ok = False
            change = 0.0
        if abs(change) > 50:
            errors.append(f"{where}: change_pct 는 ±50% 이내여야 합니다")
            ok = False
        side = rule.get("side")
        if side not in VALID_ACTIONS:
            errors.append(f"{where}: side 는 long/short/close 중 하나여야 합니다")
            ok = False
        notional = 0.0
        if side in ("long", "short"):
            checked = check_notional(rule.get("notional"), where)
            if checked is None:
                ok = False
            else:
                notional = checked
        try:
            cooldown = float(rule.get("cooldown_min", 0))
        except (TypeError, ValueError):
            cooldown = -1
        if not (0 <= cooldown <= 1440):
            errors.append(f"{where}: cooldown_min 은 0~1440 사이여야 합니다")
            ok = False
        if ok:
            clean_rules.append({
                "name": str(rule.get("name", f"rule{i}"))[:40],
                "timeframe": timeframe, "bars": bars, "op": op,
                "change_pct": change, "side": side, "notional": notional,
                "cooldown_min": cooldown,
            })
    clean["rules"] = clean_rules

    for key, low, high in (("stop_loss_pct", 0.0, 90.0), ("take_profit_pct", 0.0, 500.0)):
        try:
            value = float(raw.get(key, 0.0))
        except (TypeError, ValueError):
            value = -1
        if not (low <= value <= high):
            errors.append(f"{key} 는 {low:g}~{high:g} 사이여야 합니다 (0 = 사용 안 함)")
        else:
            clean[key] = value

    try:
        max_pos = float(raw.get("max_position_notional", 0.0))
    except (TypeError, ValueError):
        max_pos = -1
    if not (0 <= max_pos <= 200_000):
        errors.append("max_position_notional 은 0~200000 사이여야 합니다 (0 = 레버리지 한도만)")
    else:
        clean["max_position_notional"] = max_pos

    clean["memo"] = str(raw.get("memo", ""))[:500]
    return clean, errors


def default_spec() -> dict[str, Any]:
    """아직 아무 지시가 없을 때의 스펙 — 아무것도 하지 않는다."""
    return {
        "leverage": 3.0, "orders": [], "rules": [],
        "stop_loss_pct": 0.0, "take_profit_pct": 0.0,
        "max_position_notional": 0.0, "memo": "",
    }


class AITrader(Strategy):
    """AI 가 스펙(제한된 DSL)으로 프로그래밍하는 봇.

    매 주기 스펙의 즉시 주문과 규칙을 평가해 신호를 낸다. 판단 로직은 전부
    스펙(=AI)에서 오고, 이 클래스는 검증과 집행만 한다.
    """

    category = "ai"

    def __init__(self, name: str, label: str) -> None:
        self.name = name
        self.label = label
        self.summary = f"AI 봇 — {label}가 전용 토큰으로 직접 프로그래밍하는 매매 규칙"
        self._lock = threading.Lock()
        self._queue: deque[dict[str, Any]] = deque()
        self.spec: dict[str, Any] = default_spec()
        self._rule_state: dict[int, dict[str, int]] = {}
        super().__init__({})

    # ------------------------------------------------------------------
    @property
    def leverage(self) -> float:
        return float(self.spec.get("leverage", 3.0))

    @property
    def extra_timeframes(self) -> tuple[str, ...]:
        """스펙의 규칙이 쓰는 시간대. 엔진이 거래소에서 봉을 받아 온다."""
        return tuple({r["timeframe"] for r in self.spec.get("rules", [])})

    def set_spec(self, raw: Any) -> list[str]:
        """스펙을 교체한다. 오류 목록이 비어 있으면 성공."""
        clean, errors = validate_spec(raw)
        if errors:
            return errors
        with self._lock:
            self.spec = clean
            self._rule_state = {}
            # 즉시 주문은 스펙 교체 시점에 큐로 옮긴다 — 한 번만 나간다.
            for order in clean.get("orders", []):
                action = "close" if order["side"] == "close" else order["side"]
                self._queue.append(
                    {"action": action, "notional": order.get("notional", 0.0)}
                )
        return []

    # 대시보드 수동 입력(비상용)도 같은 큐를 쓴다.
    def submit(self, action: str, notional: float = 0.0) -> dict[str, Any]:
        if action not in VALID_ACTIONS:
            raise ValueError(f"알 수 없는 동작 '{action}' (long/short/close)")
        if action != "close" and not (0 < notional <= MAX_ORDER_NOTIONAL):
            raise ValueError(f"주문 금액은 1~{MAX_ORDER_NOTIONAL:.0f} USDT 여야 합니다")
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
        if order is not None:
            signal = self._order_signal(ctx, order["action"], order.get("notional", 0.0))
            if signal is not None:
                return signal

        for index, rule in enumerate(self.spec.get("rules", [])):
            fired = self._rule_fires(ctx, index, rule)
            if fired:
                signal = self._order_signal(
                    ctx, rule["side"], rule.get("notional", 0.0),
                    label=f"규칙 '{rule['name']}' ({fired})",
                )
                if signal is not None:
                    return signal

        if ctx.position.is_open:
            return Signal(reason=f"{self.label} 봇 — 규칙 대기 (보유 유지)")
        return Signal(reason=f"{self.label} 봇 — 규칙 대기")

    def _rule_fires(self, ctx: StrategyContext, index: int, rule: dict) -> str | None:
        """규칙이 이번 봉에서 막 성립했으면 설명 문자열, 아니면 None."""
        bars = ctx.closed_candles_for(rule["timeframe"])
        if len(bars) <= rule["bars"]:
            return None
        last = bars[-1]
        state = self._rule_state.setdefault(index, {"last_bar": 0, "until": 0})
        if last.timestamp == state["last_bar"] or last.timestamp < state["until"]:
            return None
        base = bars[-1 - rule["bars"]].close
        if base <= 0:
            return None
        change = (last.close / base - 1) * 100
        met = change <= rule["change_pct"] if rule["op"] == "lte" else change >= rule["change_pct"]
        if not met:
            return None
        state["last_bar"] = last.timestamp
        state["until"] = last.timestamp + int(rule["cooldown_min"] * 60_000)
        return f"{rule['timeframe']}×{rule['bars']}봉 {change:+.2f}%"

    def _order_signal(
        self, ctx: StrategyContext, action: str, notional: float, label: str = "지시",
    ) -> Signal | None:
        price = ctx.last_price
        if action == "close":
            if not ctx.position.is_open:
                return None
            return Signal(action=SignalAction.EXIT, reason=f"{self.label} 봇 — {label} 청산")

        side = PositionSide.LONG if action == "long" else PositionSide.SHORT
        notional = min(float(notional), MAX_ORDER_NOTIONAL)
        if notional <= 0:
            return None

        # 노출 한도: 자기자본 × 자기 레버리지 (스펙의 상한이 더 작으면 그것).
        # 늘리는 쪽만 본다 — 반대 방향은 상계라 노출이 줄어든다.
        if not ctx.position.is_open or ctx.position.side is side:
            cap = ctx.equity * self.leverage
            spec_cap = float(self.spec.get("max_position_notional", 0.0))
            if spec_cap > 0:
                cap = min(cap, spec_cap)
            current = ctx.position.notional if ctx.position.is_open else 0.0
            if current + notional > cap:
                return Signal(reason=f"{self.label} 봇 — 노출 한도 초과로 주문 보류 "
                                     f"({current:.0f}+{notional:.0f} > {cap:.0f})")

        sl = float(self.spec.get("stop_loss_pct", 0.0))
        tp = float(self.spec.get("take_profit_pct", 0.0))
        if sl > 0:
            stop = price * (1 - sl / 100) if side is PositionSide.LONG else price * (1 + sl / 100)
        else:
            # 시스템 규칙(진입에는 손절가 필수)용 명목값 — 사실상 도달하지 않는다.
            stop = price * 0.02 if side is PositionSide.LONG else price * 3.0
        take_profit = None
        if tp > 0:
            take_profit = (
                price * (1 + tp / 100) if side is PositionSide.LONG else price * (1 - tp / 100)
            )

        return Signal(
            action=SignalAction.ENTER_LONG if side is PositionSide.LONG
            else SignalAction.ENTER_SHORT,
            strength=Conviction.LOW.value,
            stop_loss=stop,
            take_profit=take_profit,
            metadata={"accumulate": True, "notional": notional},
            reason=f"{self.label} 봇 — {label} {action} {notional:.0f} USDT",
        )


# 하위 호환 (예전 이름). 새 코드는 AITrader 를 쓴다.
ManualTrader = AITrader


def build_traders() -> dict[str, AITrader]:
    return {name: AITrader(name, label) for name, label in AI_TRADERS.items()}
