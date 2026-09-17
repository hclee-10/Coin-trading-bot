import pytest

from bot.config import RiskConfig
from bot.execution import Executor
from bot.models import Position, PositionSide, Side, Signal, SignalAction
from bot.risk import RiskManager
from tests.fakes import FakeExchange

SYMBOL = "BTC/USDT:USDT"


def make_executor(exchange, *, dry_run=False, allow_reverse=False, **risk_kwargs) -> Executor:
    risk = RiskManager(RiskConfig(**risk_kwargs), leverage=3.0)
    return Executor(exchange, risk, dry_run=dry_run, allow_reverse=allow_reverse)


def risk_mode(**overrides):
    """위험비율 사이징을 쓰는 설정. 기본은 확신도 기반 고정 금액이다."""
    overrides.setdefault("sizing_mode", "risk")
    return overrides


def long_position(contracts=1.0) -> Position:
    return Position(
        symbol=SYMBOL, side=PositionSide.LONG, contracts=contracts,
        entry_price=100.0, notional=contracts * 100.0,
    )


def test_entry_sends_market_order_plus_stop_and_take_profit():
    ex = FakeExchange(price=100.0, equity=10_000.0)
    # 익절은 기본으로 꺼져 있다 — 이 테스트는 켰을 때의 동작을 본다
    executor = make_executor(ex, default_take_profit_pct=2.0)

    result = executor.handle(
        symbol=SYMBOL,
        signal=Signal(action=SignalAction.ENTER_LONG, reason="테스트"),
        position=Position.flat(SYMBOL),
        price=100.0, equity=10_000.0, open_positions=0,
    )

    assert result.action == "entered"
    types = [o.type for o in ex.sent_orders]
    assert types == ["market", "stop", "take_profit"]
    entry, stop, tp = ex.sent_orders
    assert entry.side is Side.BUY and not entry.reduce_only
    # 보호주문은 반대 방향이고 reduce-only 여야 한다
    assert stop.side is Side.SELL and stop.reduce_only
    assert tp.side is Side.SELL and tp.reduce_only
    assert stop.amount == entry.amount == tp.amount


def test_okx_style_contract_size_is_converted():
    """1계약=0.01 BTC 인 거래소에서는 주문 수량이 계약 수로 나가야 한다."""
    ex = FakeExchange(price=100.0, equity=10_000.0, contract_size=0.01)
    executor = make_executor(ex, **risk_mode(max_position_notional_pct=20.0))

    executor.handle(
        symbol=SYMBOL,
        signal=Signal(action=SignalAction.ENTER_LONG),
        position=Position.flat(SYMBOL),
        price=100.0, equity=10_000.0, open_positions=0,
    )
    # 명목가 상한 2000 USDT / 100 = 베이스 20 개 → 계약 2000 개
    assert ex.sent_orders[0].amount == pytest.approx(2000.0)


def test_dry_run_sends_nothing():
    ex = FakeExchange()
    executor = make_executor(ex, dry_run=True)

    result = executor.handle(
        symbol=SYMBOL,
        signal=Signal(action=SignalAction.ENTER_LONG),
        position=Position.flat(SYMBOL),
        price=100.0, equity=10_000.0, open_positions=0,
    )

    assert result.action == "entered"
    assert result.detail.startswith("[DRY-RUN]")
    assert ex.sent_orders == []


def test_exit_cancels_protective_orders_then_closes_reduce_only():
    ex = FakeExchange()
    executor = make_executor(ex)

    result = executor.handle(
        symbol=SYMBOL,
        signal=Signal(action=SignalAction.EXIT, reason="청산"),
        position=long_position(2.0),
        price=100.0, equity=10_000.0, open_positions=1,
    )

    assert result.action == "exited"
    assert ex.cancelled == [SYMBOL]
    (order,) = ex.sent_orders
    assert order.side is Side.SELL and order.reduce_only and order.amount == 2.0


def test_same_direction_signal_does_not_pyramid():
    ex = FakeExchange()
    executor = make_executor(ex)

    result = executor.handle(
        symbol=SYMBOL,
        signal=Signal(action=SignalAction.ENTER_LONG),
        position=long_position(),
        price=100.0, equity=10_000.0, open_positions=1,
    )

    assert result.action == "none"
    assert ex.sent_orders == []


def test_opposite_signal_is_ignored_unless_reverse_allowed():
    ex = FakeExchange()
    executor = make_executor(ex, allow_reverse=False)

    result = executor.handle(
        symbol=SYMBOL,
        signal=Signal(action=SignalAction.ENTER_SHORT),
        position=long_position(),
        price=100.0, equity=10_000.0, open_positions=1,
    )

    assert result.action == "none"
    assert "allow_reverse" in result.detail
    assert ex.sent_orders == []


def test_reverse_closes_then_opens_opposite():
    ex = FakeExchange()
    executor = make_executor(ex, allow_reverse=True)

    result = executor.handle(
        symbol=SYMBOL,
        signal=Signal(action=SignalAction.ENTER_SHORT),
        position=long_position(),
        price=100.0, equity=10_000.0, open_positions=1,
    )

    assert result.action == "reversed"
    close, entry = ex.sent_orders[0], ex.sent_orders[1]
    assert close.side is Side.SELL and close.reduce_only
    assert entry.side is Side.SELL and not entry.reduce_only  # 숏 진입


def test_exit_without_position_is_a_noop():
    ex = FakeExchange()
    executor = make_executor(ex)

    result = executor.handle(
        symbol=SYMBOL,
        signal=Signal(action=SignalAction.EXIT),
        position=Position.flat(SYMBOL),
        price=100.0, equity=10_000.0, open_positions=0,
    )

    assert result.action == "none"
    assert ex.sent_orders == []


def test_rejected_when_size_rounds_to_zero():
    ex = FakeExchange(price=100.0, equity=10_000.0, contract_size=1_000_000.0)
    executor = make_executor(ex)

    result = executor.handle(
        symbol=SYMBOL,
        signal=Signal(action=SignalAction.ENTER_LONG),
        position=Position.flat(SYMBOL),
        price=100.0, equity=10_000.0, open_positions=0,
    )

    assert result.action == "rejected"
    assert ex.sent_orders == []


# --- 넷 적립 (accumulate) ---------------------------------------------------
ACC_LONG = Signal(action=SignalAction.ENTER_LONG, strength=0.25,
                  stop_loss=2.0, metadata={"accumulate": True}, reason="적립")
ACC_SHORT = Signal(action=SignalAction.ENTER_SHORT, strength=0.25,
                   stop_loss=300.0, metadata={"accumulate": True}, reason="상계")


def test_accumulate_adds_to_the_same_side_without_protective_orders():
    """같은 방향 적립은 시장가 하나만 — 손절/익절 보호주문이 나가면 안 된다."""
    ex = FakeExchange(price=100.0)
    executor = make_executor(ex, notional_tiers=[10.0], max_position_notional_pct=1000.0)

    result = executor.handle(symbol=SYMBOL, signal=ACC_LONG, position=long_position(),
                             price=100.0, equity=10_000.0, open_positions=1)

    assert result.action == "accumulated"
    assert [o.type for o in ex.sent_orders] == ["market"]
    assert ex.sent_orders[0].side is Side.BUY


def test_accumulate_nets_the_opposite_side_instead_of_reversing():
    """반대 방향 적립은 전량 청산+뒤집기가 아니라 10달러짜리 주문 하나다."""
    ex = FakeExchange(price=100.0)
    executor = make_executor(ex, notional_tiers=[10.0], max_position_notional_pct=1000.0)

    result = executor.handle(symbol=SYMBOL, signal=ACC_SHORT, position=long_position(5.0),
                             price=100.0, equity=10_000.0, open_positions=1)

    assert result.action == "accumulated"
    assert [o.type for o in ex.sent_orders] == ["market"]   # 청산 주문 없음
    assert ex.sent_orders[0].side is Side.SELL
    assert ex.sent_orders[0].amount == pytest.approx(0.1)   # 10 USDT / 100


def test_accumulate_from_flat_is_a_plain_entry_without_stops():
    ex = FakeExchange(price=100.0)
    executor = make_executor(ex, notional_tiers=[10.0], max_position_notional_pct=1000.0)

    result = executor.handle(symbol=SYMBOL, signal=ACC_LONG, position=Position.flat(SYMBOL),
                             price=100.0, equity=10_000.0, open_positions=0)

    assert result.action == "entered"
    assert [o.type for o in ex.sent_orders] == ["market"]
