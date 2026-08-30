"""전략 경쟁 모의매매 — 전략마다 독립된 가상 계좌가 유지되어야 한다."""

import pytest

from bot.config import Config, ExchangeConfig, RiskConfig, StrategyConfig, TradingConfig
from bot.models import Candle, Signal, SignalAction, Ticker
from bot.paper import PaperArena
from bot.store import Store
from bot.strategies.base import Strategy

SYMBOL = "BTC/USDT:USDT"


def make_config(**risk):
    risk.setdefault("sizing_mode", "tiers")
    risk.setdefault("notional_tiers", [100.0])
    risk.setdefault("max_position_notional_pct", 100.0)
    risk.setdefault("default_stop_loss_pct", 2.0)
    return Config(
        exchange=ExchangeConfig(id="gate", leverage=3.0),
        trading=TradingConfig(symbols=[SYMBOL], timeframe="5m"),
        strategy=StrategyConfig(name="hold"),
        risk=RiskConfig(**risk),
    )


def bars(price, *, low=None, high=None, count=3):
    return [
        Candle(timestamp=1_700_000_000_000 + i * 300_000, open=price,
               high=high if high is not None else price,
               low=low if low is not None else price,
               close=price, volume=1.0)
        for i in range(count)
    ]


def tick(price):
    return Ticker(symbol=SYMBOL, last=price, bid=None, ask=None, timestamp=0)


def arena_with(strategies, store=None, **config_kwargs):
    """지정한 전략만 경쟁시키는 아레나."""
    return PaperArena(
        make_config(**config_kwargs), store or Store(None),
        taker_fee=0.0, strategies=strategies,
    )


class Scripted(Strategy):
    """지정한 신호를 순서대로 낸다."""

    def __init__(self, name, signals):
        self.name = name
        self._signals = list(signals)
        super().__init__({})

    def generate(self, ctx):
        return self._signals.pop(0) if self._signals else Signal()


LONG = Signal(action=SignalAction.ENTER_LONG, strength=0.5, reason="테스트")
EXIT = Signal(action=SignalAction.EXIT, reason="테스트")
HOLD = Signal()


# --- 독립성 -------------------------------------------------------------
def test_each_strategy_keeps_its_own_book():
    """한 전략의 손익이 다른 전략의 성적에 섞이면 비교가 무의미해진다."""
    winner = Scripted("winner", [LONG, EXIT])
    loser = Scripted("loser", [LONG, EXIT])
    arena = arena_with({"winner": winner, "loser": loser})

    arena.step(SYMBOL, bars(100.0), tick(100.0))    # 둘 다 진입
    arena.step(SYMBOL, bars(110.0), tick(110.0))    # winner 청산
    # loser 는 신호를 다 썼으므로 HOLD — 아직 포지션 보유

    rows = {s.name: s for s in arena.leaderboard({SYMBOL: 110.0})}
    assert rows["winner"].trade_count == 1
    assert rows["winner"].net_pnl > 0
    assert rows["loser"].trade_count == 1


def test_one_broken_strategy_does_not_stop_the_others():
    """비교가 목적이므로 전략 하나의 버그로 전체 데이터를 잃으면 안 된다."""

    class Broken(Strategy):
        name = "broken"

        def generate(self, ctx):
            raise RuntimeError("전략 버그")

    good = Scripted("good", [LONG])
    arena = arena_with({"broken": Broken({}), "good": good})

    arena.step(SYMBOL, bars(100.0), tick(100.0))

    rows = {s.name: s for s in arena.leaderboard()}
    assert rows["good"].open_positions == 1
    assert "전략 버그" in rows["broken"].error


def test_a_late_joining_strategy_starts_from_its_own_baseline():
    """나중에 추가된 전략이 앞선 전략의 성적을 물려받으면 안 된다."""
    store = Store(None)
    early = arena_with({"early": Scripted("early", [LONG, EXIT])}, store)
    early.step(SYMBOL, bars(100.0), tick(100.0))
    early.step(SYMBOL, bars(120.0), tick(120.0))

    late = arena_with({"late": Scripted("late", [HOLD])}, store)
    late.step(SYMBOL, bars(120.0), tick(120.0))

    rows = {s.name: s for s in late.leaderboard()}
    assert rows["late"].trade_count == 0
    assert rows["late"].return_pct == pytest.approx(0.0)
    assert rows["late"].start_equity == pytest.approx(10_000.0)


# --- 손익 계산 -----------------------------------------------------------
def test_profit_is_recorded_on_exit():
    arena = arena_with({"s": Scripted("s", [LONG, EXIT])})

    arena.step(SYMBOL, bars(100.0), tick(100.0))
    arena.step(SYMBOL, bars(110.0), tick(110.0))

    (row,) = arena.leaderboard()
    assert row.trade_count == 1 and row.wins == 1
    assert row.net_pnl == pytest.approx(10.0)   # 명목가 100 → 수량 1, +10
    assert row.return_pct == pytest.approx(0.1)


def test_fees_are_charged_on_both_sides():
    """모의 성적이 실제보다 좋아 보이면 판단이 어긋난다."""
    arena = PaperArena(make_config(), Store(None), taker_fee=0.001,
                       strategies={"s": Scripted("s", [LONG, EXIT])})

    arena.step(SYMBOL, bars(100.0), tick(100.0))
    arena.step(SYMBOL, bars(100.0), tick(100.0))

    (row,) = arena.leaderboard()
    assert row.net_pnl == pytest.approx(-0.2)   # 100 × 0.1% × 2


# --- 손절 ----------------------------------------------------------------
def test_stop_loss_triggers_on_the_bar_low_not_just_the_last_price():
    """폴링 사이에 스쳤어도 걸린 것으로 봐야 실제와 맞는다."""
    arena = arena_with({"s": Scripted("s", [LONG, HOLD])}, default_stop_loss_pct=2.0)

    arena.step(SYMBOL, bars(100.0), tick(100.0))
    # 현재가는 99 지만 봉의 저가가 97 까지 내려갔다 — 손절가 98 을 스쳤다
    arena.step(SYMBOL, bars(99.0, low=97.0), tick(99.0))

    (row,) = arena.leaderboard()
    assert row.trade_count == 1
    assert row.stop_outs == 1
    assert row.stop_out_rate == pytest.approx(100.0)


def test_stop_out_rate_is_none_without_trades():
    arena = arena_with({"s": Scripted("s", [HOLD])})
    arena.step(SYMBOL, bars(100.0), tick(100.0))

    (row,) = arena.leaderboard()
    assert row.stop_out_rate is None and row.win_rate is None


def test_liquidation_risk_is_measured_against_the_leverage_distance():
    """3배 레버리지면 약 33% 역행에서 청산된다. 그 거리 대비 얼마나 갔는지를 잰다."""
    arena = arena_with({"s": Scripted("s", [LONG, HOLD])}, default_stop_loss_pct=50.0)

    arena.step(SYMBOL, bars(100.0), tick(100.0))
    arena.step(SYMBOL, bars(90.0, low=90.0), tick(90.0))   # 10% 역행

    (row,) = arena.leaderboard({SYMBOL: 90.0})
    # 10% / 33.3% ≈ 30%
    assert row.liquidation_risk_pct == pytest.approx(30.0, abs=1.0)


# --- 방향 전환과 복원 -----------------------------------------------------
def test_reversal_closes_and_reopens():
    short = Signal(action=SignalAction.ENTER_SHORT, strength=0.5)
    arena = arena_with({"s": Scripted("s", [LONG, short])})

    arena.step(SYMBOL, bars(100.0), tick(100.0))
    arena.step(SYMBOL, bars(110.0), tick(110.0))

    (row,) = arena.leaderboard({SYMBOL: 110.0})
    assert row.trade_count == 1
    assert row.open_positions == 1


def test_open_positions_survive_a_restart():
    """재배포해도 진행 중인 가상 포지션이 이어져야 성적이 왜곡되지 않는다."""
    store = Store(None)
    first = arena_with({"s": Scripted("s", [LONG])}, store)
    first.step(SYMBOL, bars(100.0), tick(100.0))

    revived = arena_with({"s": Scripted("s", [EXIT])}, store)
    revived.step(SYMBOL, bars(110.0), tick(110.0))

    (row,) = revived.leaderboard()
    assert row.trade_count == 1
    assert row.net_pnl == pytest.approx(10.0)


def test_reset_clears_everything():
    arena = arena_with({"s": Scripted("s", [LONG, EXIT])})
    arena.step(SYMBOL, bars(100.0), tick(100.0))
    arena.step(SYMBOL, bars(110.0), tick(110.0))

    arena.reset()

    assert arena.leaderboard() == []


# --- 실제 전략 등록 ------------------------------------------------------
def test_all_registered_strategies_join_automatically():
    """새 전략을 추가하면 다음 기동 때 자동으로 합류해야 한다."""
    from bot.strategies import strategy_catalog

    arena = PaperArena(make_config(), Store(None))
    expected = {e["name"] for e in strategy_catalog() if e["summary"]}

    assert set(arena.strategy_names) == expected
    assert len(expected) >= 10


def test_leaderboard_is_sorted_by_return():
    good = Scripted("good", [LONG, EXIT])
    bad = Scripted("bad", [LONG, EXIT])
    arena = arena_with({"bad": bad, "good": good})

    arena.step(SYMBOL, bars(100.0), tick(100.0))
    # good 은 오르고 bad 는 내린 상태에서 청산되도록 따로 굴린다
    arena._close(  # noqa: SLF001 — 테스트에서 결과를 만들기 위한 직접 호출
        "bad", arena._positions[("bad", SYMBOL)], 90.0, 2_000, "signal"
    )
    arena.step(SYMBOL, bars(110.0), tick(110.0))

    names = [s.name for s in arena.leaderboard()]
    assert names[0] == "good"


def test_unrealized_includes_the_round_trip_fee():
    """보유 중인 포지션도 왕복 수수료를 반영해야 순위 비교가 공정하다."""
    from bot.models import PositionSide
    from bot.paper import PaperPosition

    position = PaperPosition(
        symbol="BTC/USDT:USDT",
        side=PositionSide.LONG,
        opened_at=0,
        entry_price=100.0,
        amount=1.0,
        notional=100.0,
        stop_loss=99.0,
        entry_fee=0.05,          # 진입 때 이미 낸 0.05%
        conviction=0.5,
    )

    # 가격이 그대로면 총손익은 0 이지만, 지금 닫으면 왕복 수수료만큼 손해다
    assert position.unrealized(100.0) == 0.0
    assert position.unrealized_net(100.0, 0.0005) == pytest.approx(-0.10)

    # 오른 경우에도 수수료가 빠진다
    assert position.unrealized(101.0) == pytest.approx(1.0)
    assert position.unrealized_net(101.0, 0.0005) == pytest.approx(1.0 - 0.05 - 0.0505)
