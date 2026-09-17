"""AI 경쟁 매매 — AI 가 프로그래밍한 봇 스펙이 규칙대로 체결·차단되어야 한다."""

import pytest

from bot.ai_traders import AITrader, build_traders, validate_spec
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


# --- 스펙 검증 --------------------------------------------------------------
def test_spec_validation_names_every_violation():
    """오류 메시지는 AI 가 읽고 고친다 — 무엇이 왜 틀렸는지 구체적이어야 한다."""
    _, errors = validate_spec({
        "leverage": 20,                                  # 한도 10배
        "orders": [{"side": "long", "notional": 6000}],  # 회당 한도 5000
        "rules": [{"timeframe": "10s", "bars": 3, "op": "lte",
                   "change_pct": -1, "side": "long", "notional": 100}],
    })

    assert any("leverage" in e for e in errors)
    assert any("5000" in e for e in errors)
    assert any("timeframe" in e for e in errors)


def test_a_valid_spec_is_accepted_and_cleaned():
    trader = AITrader("ai_gpt", "GPT")
    errors = trader.set_spec({
        "leverage": 5,
        "orders": [{"side": "long", "notional": 1000}],
        "rules": [{"name": "dip", "timeframe": "5m", "bars": 3, "op": "lte",
                   "change_pct": -0.6, "side": "long", "notional": 500,
                   "cooldown_min": 30}],
        "stop_loss_pct": 2.5,
        "memo": "테스트",
    })

    assert errors == []
    assert trader.leverage == 5
    assert trader.extra_timeframes == ("5m",)
    assert trader.pending() == [{"action": "long", "notional": 1000.0}]


# --- 즉시 주문 --------------------------------------------------------------
def test_spec_orders_execute_once_with_the_given_notional():
    trader = AITrader("ai_gpt", "GPT")
    arena = arena_with_trader(trader)
    trader.set_spec({"leverage": 3, "orders": [{"side": "long", "notional": 250}]})

    arena.step(SYMBOL, bars(100.0), tick(100.0))
    arena.step(SYMBOL, bars(100.0), tick(100.0))   # 두 번째 주기에 또 나가면 안 된다

    position = arena._positions[("ai_gpt", SYMBOL)]
    assert position.notional == pytest.approx(250.0)
    assert trader.pending() == []


def test_stop_loss_from_the_spec_lands_on_the_position():
    trader = AITrader("ai_gpt", "GPT")
    arena = arena_with_trader(trader)
    trader.set_spec({"leverage": 3, "stop_loss_pct": 2.0,
                     "orders": [{"side": "long", "notional": 100}]})

    arena.step(SYMBOL, bars(100.0), tick(100.0))

    position = arena._positions[("ai_gpt", SYMBOL)]
    assert position.stop_loss == pytest.approx(98.0)


# --- 상시 규칙 --------------------------------------------------------------
def test_rule_fires_once_per_bar_and_respects_cooldown():
    trader = AITrader("ai_grok", "그록")
    arena = arena_with_trader(trader)
    trader.set_spec({
        "leverage": 3,
        "rules": [{"name": "dip", "timeframe": "5m", "bars": 1, "op": "lte",
                   "change_pct": -1.0, "side": "long", "notional": 100,
                   "cooldown_min": 0}],
    })

    # 마지막 확정 봉이 -2% → 발동
    candles = bars(100.0, count=50)
    from bot.models import Candle
    drop = Candle(timestamp=candles[-1].timestamp + 300_000, open=100.0,
                  high=100.0, low=98.0, close=98.0, volume=1.0)
    tail = Candle(timestamp=drop.timestamp + 300_000, open=98.0,
                  high=98.0, low=98.0, close=98.0, volume=1.0)
    series = candles + [drop, tail]

    arena.step(SYMBOL, series, tick(98.0))
    position = arena._positions[("ai_grok", SYMBOL)]
    first_amount = position.amount
    assert first_amount > 0

    # 같은 봉으로 다시 판단 → 재발동하지 않는다
    arena.step(SYMBOL, series, tick(98.0))
    assert arena._positions[("ai_grok", SYMBOL)].amount == pytest.approx(first_amount)


def test_leverage_caps_total_exposure():
    """총 노출은 자기자본 × 자기 레버리지를 넘을 수 없다."""
    trader = AITrader("ai_gpt", "GPT")
    ctx_equity = 10_000.0
    arena = arena_with_trader(trader)
    trader.set_spec({"leverage": 1, "orders": [
        {"side": "long", "notional": 5000},
        {"side": "long", "notional": 5000},
        {"side": "long", "notional": 5000},   # 세 번째는 한도(1만) 초과 → 보류
    ]})

    for _ in range(3):
        arena.step(SYMBOL, bars(100.0), tick(100.0))

    position = arena._positions[("ai_gpt", SYMBOL)]
    assert position.notional <= ctx_equity * 1.0 + 1e-6


# --- 대회 규칙: 시장 대비 -5%p ----------------------------------------------
def test_falling_5pct_behind_the_market_blocks_new_exposure():
    """시장보다 5%p 이상 뒤지면 노출을 늘리는 주문이 차단된다."""
    trader = AITrader("ai_gpt", "GPT")
    arena = arena_with_trader(trader)
    trader.set_spec({"leverage": 10, "orders": [{"side": "short", "notional": 5000}]})

    arena.step(SYMBOL, bars(100.0), tick(100.0))   # 기준가 100, 숏 5000
    # 가격 +12%: 숏 -600(-6%), 시장 +12% → 시장 대비 -18%p
    trader.submit("short", 1000)
    arena.step(SYMBOL, bars(112.0), tick(112.0))

    position = arena._positions[("ai_gpt", SYMBOL)]
    assert position.amount == pytest.approx(50.0)   # 5000/100 그대로 — 추가 숏 차단됨

    # 반대 방향(상계)은 허용 — 위험을 줄이는 길은 열려 있다
    trader.submit("long", 1000)
    arena.step(SYMBOL, bars(112.0), tick(112.0))
    assert arena._positions[("ai_gpt", SYMBOL)].amount < position.amount + 1e-9


# --- 기존 동작 유지 ----------------------------------------------------------
def test_manual_close_still_works():
    trader = AITrader("ai_gpt", "GPT")
    arena = arena_with_trader(trader)
    trader.submit("long", 100.0)
    arena.step(SYMBOL, bars(100.0), tick(100.0))

    trader.submit("close")
    arena.step(SYMBOL, bars(110.0), tick(110.0))

    assert ("ai_gpt", SYMBOL) not in arena._positions
    trades = arena.store.paper_trades("ai_gpt")
    assert len(trades) == 1
    assert trades[0]["pnl"] == pytest.approx(10.0)


def test_ai_positions_survive_a_restart():
    store = Store(None)
    first = AITrader("ai_claude", "클로드")
    arena = arena_with_trader(first, store)
    first.submit("short", 150.0)
    arena.step(SYMBOL, bars(100.0), tick(100.0))

    revived = arena_with_trader(AITrader("ai_claude", "클로드"), store)

    position = revived._positions[("ai_claude", SYMBOL)]
    assert position.side.value == "short"
    assert position.notional == pytest.approx(150.0)


def test_build_traders_covers_the_four_ais():
    traders = build_traders()
    assert set(traders) == {"ai_claude", "ai_gpt", "ai_grok", "ai_gemini"}
    for trader in traders.values():
        assert trader.summary
        assert trader.category == "ai"
