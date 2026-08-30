"""지정가 진입·청산.

수수료가 왕복 0.10%(taker)에서 0.04%(maker)로 줄지만, 체결이 보장되지 않는다는
맞바꿈이 있다. 그 처리를 검증한다.
"""

import pytest

from bot.config import RiskConfig
from bot.execution import Executor
from bot.models import Position, PositionSide, Side, Signal, SignalAction
from bot.risk import RiskManager
from tests.fakes import FakeExchange

SYMBOL = "BTC/USDT:USDT"


def make_executor(exchange, **overrides):
    options = {
        "dry_run": False,
        "order_type": "limit",
        "limit_offset_pct": 0.02,
        "limit_timeout_sec": 60.0,
    }
    options.update(overrides)
    risk = RiskManager(
        RiskConfig(sizing_mode="tiers", notional_tiers=[100.0],
                   max_position_notional_pct=100.0),
        leverage=3.0,
    )
    return Executor(exchange, risk, **options)


def enter(executor, price=100.0):
    return executor.handle(
        symbol=SYMBOL,
        signal=Signal(action=SignalAction.ENTER_LONG, strength=0.5, reason="테스트"),
        position=Position.flat(SYMBOL),
        price=price, equity=10_000.0, open_positions=0,
    )


# --- 진입 ----------------------------------------------------------------
def test_limit_entry_is_placed_below_the_market_for_a_buy():
    """매수 지정가는 현재가보다 아래여야 maker 가 된다."""
    ex = FakeExchange(price=100.0)
    executor = make_executor(ex, limit_offset_pct=0.5)

    enter(executor, price=100.0)

    (order,) = ex.sent_orders
    assert order.type == "limit"
    assert order.price == pytest.approx(99.5)


def test_no_protective_order_until_the_entry_fills():
    """체결되지도 않은 포지션에 reduce-only 주문을 걸면 거래소가 거부한다."""
    ex = FakeExchange(price=100.0)
    executor = make_executor(ex)

    result = enter(executor)

    assert result.action == "none"
    assert [o.type for o in ex.sent_orders] == ["limit"]
    assert SYMBOL in executor.pending


def test_stop_loss_is_placed_once_the_entry_fills():
    ex = FakeExchange(price=100.0)
    executor = make_executor(ex)
    enter(executor)
    order_id = ex.sent_orders[0].id

    ex.fill_order(order_id, price=99.98)
    result = executor.reconcile(SYMBOL, Position.flat(SYMBOL))

    assert result.action == "entered"
    assert "stop" in [o.type for o in ex.sent_orders]
    assert SYMBOL not in executor.pending


def test_pending_entry_blocks_a_second_order():
    """같은 심볼에 주문이 두 번 나가면 의도한 것의 두 배가 잡힌다."""
    ex = FakeExchange(price=100.0)
    executor = make_executor(ex)
    enter(executor)

    result = executor.reconcile(SYMBOL, Position.flat(SYMBOL))

    assert result.action == "none"
    assert "체결 대기" in result.detail
    assert len(ex.sent_orders) == 1


# --- 미체결 처리 ---------------------------------------------------------
def test_unfilled_order_is_cancelled_after_the_timeout():
    """신호가 나온 지 한참 지난 가격에 체결되는 것이 더 나쁘다."""
    ex = FakeExchange(price=100.0)
    executor = make_executor(ex, limit_timeout_sec=0.0)
    enter(executor)
    order_id = ex.sent_orders[0].id

    result = executor.reconcile(SYMBOL, Position.flat(SYMBOL))

    assert result.action == "none"
    assert "미체결로 취소" in result.detail
    assert ex.open_orders[order_id].status == "canceled"
    assert SYMBOL not in executor.pending


def test_fallback_to_market_when_configured():
    ex = FakeExchange(price=100.0)
    executor = make_executor(ex, limit_timeout_sec=0.0, limit_fallback_market=True)
    enter(executor)

    result = executor.reconcile(SYMBOL, Position.flat(SYMBOL))

    assert result.action == "entered"
    types = [o.type for o in ex.sent_orders]
    assert "market" in types and "stop" in types


def test_externally_cancelled_order_clears_the_pending_state():
    """거래소나 사람이 취소했을 때 봇이 영원히 기다리면 안 된다."""
    ex = FakeExchange(price=100.0)
    executor = make_executor(ex)
    enter(executor)
    ex.cancel_order(ex.sent_orders[0].id, SYMBOL)

    result = executor.reconcile(SYMBOL, Position.flat(SYMBOL))

    assert "취소됨" in result.detail
    assert SYMBOL not in executor.pending


def test_reconcile_is_a_noop_without_a_pending_order():
    executor = make_executor(FakeExchange())
    assert executor.reconcile(SYMBOL, Position.flat(SYMBOL)) is None


# --- 청산 ----------------------------------------------------------------
def test_exit_uses_a_limit_order_priced_from_the_current_market():
    """진입가 기준으로 잡으면 시세가 움직인 뒤에는 체결되지 않는다."""
    ex = FakeExchange(price=120.0)
    executor = make_executor(ex, limit_offset_pct=0.5)
    position = Position(symbol=SYMBOL, side=PositionSide.LONG, contracts=1.0,
                        entry_price=100.0)

    executor.handle(
        symbol=SYMBOL, signal=Signal(action=SignalAction.EXIT),
        position=position, price=120.0, equity=10_000.0, open_positions=1,
    )

    exit_order = ex.sent_orders[-1]
    assert exit_order.type == "limit"
    assert exit_order.price == pytest.approx(120.6)   # 100.5 가 아니다
    assert exit_order.side is Side.SELL


def test_exit_limit_is_not_post_only():
    """포지션을 못 닫는 것보다 조금 불리한 가격에라도 나가는 편이 낫다."""
    ex = FakeExchange(price=100.0)
    executor = make_executor(ex)
    position = Position(symbol=SYMBOL, side=PositionSide.LONG, contracts=1.0,
                        entry_price=100.0)

    executor.handle(
        symbol=SYMBOL, signal=Signal(action=SignalAction.EXIT),
        position=position, price=100.0, equity=10_000.0, open_positions=1,
    )

    # post_only=False 로 나가야 한다 (FakeExchange 는 인자를 그대로 받는다)
    assert ex.sent_orders[-1].type == "limit"


# --- 손절은 언제나 시장가 -------------------------------------------------
def test_stop_loss_stays_a_market_order_even_in_limit_mode():
    """급락 중 지정가 손절은 체결되지 않아 무방비가 된다."""
    ex = FakeExchange(price=100.0)
    executor = make_executor(ex)
    enter(executor)
    ex.fill_order(ex.sent_orders[0].id)

    executor.reconcile(SYMBOL, Position.flat(SYMBOL))

    stop_orders = [o for o in ex.sent_orders if o.type == "stop"]
    assert stop_orders, "손절 주문이 없습니다"


# --- 시장가 모드는 그대로 -------------------------------------------------
def test_market_mode_still_enters_in_one_step():
    ex = FakeExchange(price=100.0)
    executor = make_executor(ex, order_type="market")

    result = enter(executor)

    assert result.action == "entered"
    assert [o.type for o in ex.sent_orders] == ["market", "stop"]
    assert not executor.pending
