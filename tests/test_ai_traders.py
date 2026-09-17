"""AI 경쟁 매매 — 웹으로 전달받은 판단이 그 AI 의 가상 계좌로 체결되어야 한다."""

import pytest

from bot.ai_traders import ManualTrader, build_traders
from bot.paper import PaperArena
from bot.store import Store
from tests.test_paper import SYMBOL, bars, make_config, tick


def arena_with_trader(trader, store=None):
    """AI 트레이더 하나만 참가시킨 아레나. 카탈로그 전략은 빼서 빠르게 돈다."""
    return PaperArena(
        make_config(max_position_notional_pct=1000.0),
        store or Store(None),
        taker_fee=0.0,
        strategies={},
        extra_strategies={trader.name: trader},
    )


def test_submitted_order_opens_a_position_with_the_given_notional():
    """AI 가 정한 금액이 회당 고정 금액(tiers) 대신 쓰여야 한다."""
    trader = ManualTrader("ai_gpt", "GPT")
    arena = arena_with_trader(trader)
    trader.submit("long", 250.0)

    arena.step(SYMBOL, bars(100.0), tick(100.0))

    position = arena._positions[("ai_gpt", SYMBOL)]
    assert position.side.value == "long"
    assert position.notional == pytest.approx(250.0)   # tiers 기본(100)이 아니라 지정 금액
    assert trader.pending() == []                       # 큐가 비워졌다


def test_no_order_means_hold_forever():
    trader = ManualTrader("ai_grok", "그록")
    arena = arena_with_trader(trader)

    arena.step(SYMBOL, bars(100.0), tick(100.0))
    arena.step(SYMBOL, bars(120.0), tick(120.0))

    assert ("ai_grok", SYMBOL) not in arena._positions
    assert arena.store.paper_trades("ai_grok") == []


def test_opposite_order_nets_like_the_dca_family():
    """롱 보유 중 숏 지시는 뒤집지 않고 상계한다 — 단방향 모드와 같은 규칙."""
    trader = ManualTrader("ai_claude", "클로드")
    arena = arena_with_trader(trader)
    trader.submit("long", 200.0)
    arena.step(SYMBOL, bars(100.0), tick(100.0))       # 롱 2코인 @100

    trader.submit("short", 100.0)
    arena.step(SYMBOL, bars(100.0), tick(100.0))       # 숏 100 → 1코인 상계

    position = arena._positions[("ai_claude", SYMBOL)]
    assert position.side.value == "long"
    assert position.amount == pytest.approx(1.0)
    trades = arena.store.paper_trades("ai_claude")
    assert len(trades) == 1 and trades[0]["exit_reason"] == "net"


def test_close_order_exits_the_whole_position():
    trader = ManualTrader("ai_gpt", "GPT")
    arena = arena_with_trader(trader)
    trader.submit("long", 100.0)
    arena.step(SYMBOL, bars(100.0), tick(100.0))

    trader.submit("close")
    arena.step(SYMBOL, bars(110.0), tick(110.0))

    assert ("ai_gpt", SYMBOL) not in arena._positions
    trades = arena.store.paper_trades("ai_gpt")
    assert len(trades) == 1
    assert trades[0]["pnl"] == pytest.approx(10.0)     # 100→110, 1코인


def test_close_without_a_position_is_ignored():
    trader = ManualTrader("ai_gpt", "GPT")
    arena = arena_with_trader(trader)
    trader.submit("close")

    arena.step(SYMBOL, bars(100.0), tick(100.0))       # 오류 없이 지나가야 한다

    assert arena._errors.get("ai_gpt") is None
    assert arena.store.paper_trades("ai_gpt") == []


def test_invalid_orders_are_rejected_at_submit_time():
    trader = ManualTrader("ai_gpt", "GPT")
    with pytest.raises(ValueError):
        trader.submit("hedge", 100.0)
    with pytest.raises(ValueError):
        trader.submit("long", 0.0)                     # 금액 없는 롱


def test_ai_positions_survive_a_restart():
    """재기동해도 AI 의 가상 포지션이 복원되어야 한다 — 6시간 주기 실험이므로."""
    store = Store(None)
    first = ManualTrader("ai_claude", "클로드")
    arena = arena_with_trader(first, store)
    first.submit("short", 150.0)
    arena.step(SYMBOL, bars(100.0), tick(100.0))

    revived = arena_with_trader(ManualTrader("ai_claude", "클로드"), store)

    position = revived._positions[("ai_claude", SYMBOL)]
    assert position.side.value == "short"
    assert position.notional == pytest.approx(150.0)


def test_build_traders_covers_the_three_ais():
    traders = build_traders()
    assert set(traders) == {"ai_claude", "ai_gpt", "ai_grok"}
    for trader in traders.values():
        assert trader.summary                          # 순위표에 표기가 있어야 한다
        assert trader.category == "ai"
