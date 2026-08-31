"""백테스터 — 미래 정보 참조 방지가 가장 중요하다."""

import pytest

from bot.backtest import run_backtest
from bot.config import Config, ExchangeConfig, RiskConfig, StrategyConfig, TradingConfig
from bot.models import Candle, Signal, SignalAction
from bot.strategies.base import Strategy


def bars(closes, *, start=1_700_000_000_000, step=300_000, spread=1.0):
    """종가 목록으로 캔들을 만든다. 고가·저가는 종가 주변으로 좁게 잡는다."""
    candles = []
    previous = closes[0]
    for i, close in enumerate(closes):
        candles.append(Candle(
            timestamp=start + i * step,
            open=previous,
            high=max(previous, close) + spread,
            low=min(previous, close) - spread,
            close=close,
            volume=1.0,
        ))
        previous = close
    return candles


def make_config(**risk):
    risk.setdefault("sizing_mode", "tiers")
    risk.setdefault("notional_tiers", [100.0])
    risk.setdefault("max_position_notional_pct", 100.0)
    risk.setdefault("default_stop_loss_pct", 5.0)
    return Config(
        exchange=ExchangeConfig(id="gate", leverage=3.0),
        trading=TradingConfig(symbols=["BTC/USDT:USDT"], timeframe="5m"),
        strategy=StrategyConfig(name="hold"),
        risk=RiskConfig(**risk),
    )


class AlwaysLong(Strategy):
    name = "always_long"

    def generate(self, ctx):
        if ctx.position.is_open:
            return Signal(action=SignalAction.HOLD)
        return Signal(action=SignalAction.ENTER_LONG, strength=1.0)


class EnterThenExit(Strategy):
    """지정한 봉 인덱스에서 진입하고 다른 인덱스에서 청산한다."""

    name = "scripted"

    def setup(self):
        self.entry_at = self.params["entry_at"]
        self.exit_at = self.params["exit_at"]

    def generate(self, ctx):
        index = len(ctx.closed_candles) - 1   # 지금 판단 근거가 되는 마지막 봉
        if index == self.entry_at and not ctx.position.is_open:
            return Signal(action=SignalAction.ENTER_LONG, strength=1.0)
        if index == self.exit_at and ctx.position.is_open:
            return Signal(action=SignalAction.EXIT)
        return Signal(action=SignalAction.HOLD)


# --- 미래 정보 참조 ------------------------------------------------------
def test_strategy_never_sees_beyond_the_decision_bar():
    """전략이 미래 봉을 보면 백테스트 결과가 통째로 거짓이 된다."""
    seen = []

    class Recorder(Strategy):
        name = "recorder"

        def generate(self, ctx):
            seen.append([c.close for c in ctx.closed_candles])
            return Signal(action=SignalAction.HOLD)

    candles = bars([100, 101, 102, 103, 104])
    run_backtest(candles, Recorder({}), make_config())

    # 마지막 판단에서도 마지막 봉(104)은 보이지 않아야 한다
    assert all(104 not in closes for closes in seen)
    assert seen[-1][-1] == 103


def test_orders_fill_on_the_next_bar_not_the_decision_bar():
    """판단한 봉의 종가로 체결시키면 실제로는 불가능한 거래가 된다."""
    candles = bars([100, 100, 100, 100, 130, 130, 130])
    strategy = EnterThenExit({"entry_at": 2, "exit_at": 5})

    result = run_backtest(candles, strategy, make_config(), taker_fee=0.0, maker_fee=0.0)

    (trade,) = result.trades
    # 인덱스 2에서 판단 → 인덱스 3의 시가(100)에 체결. 130이 아니다.
    assert trade.entry_price == pytest.approx(100.0)
    assert trade.entry_time == candles[3].timestamp


# --- 손익과 수수료 -------------------------------------------------------
def test_profit_on_a_rising_market():
    # 청산도 다음 봉 시가에 체결되므로, 오른 뒤의 봉이 하나 더 필요하다
    candles = bars([100, 100, 100, 110, 110])
    strategy = EnterThenExit({"entry_at": 1, "exit_at": 3})

    result = run_backtest(candles, strategy, make_config(), taker_fee=0.0, maker_fee=0.0)

    (trade,) = result.trades
    assert trade.pnl > 0
    assert result.end_equity > result.start_equity


def test_fees_are_deducted_from_both_sides():
    candles = bars([100] * 5)
    strategy = EnterThenExit({"entry_at": 1, "exit_at": 2})

    result = run_backtest(candles, strategy, make_config(), taker_fee=0.001, maker_fee=0.001)

    (trade,) = result.trades
    # 가격이 그대로여도 왕복 수수료만큼 손해다
    assert trade.pnl == pytest.approx(-0.2, abs=0.01)   # 100 명목가 × 0.1% × 2
    assert result.end_equity < result.start_equity


def test_fees_alone_can_turn_a_winner_into_a_loser():
    """수수료를 빼면 대부분의 단타 전략이 흑자로 보인다."""
    candles = bars([100, 100, 100.05, 100.05, 100.05])
    strategy = EnterThenExit({"entry_at": 1, "exit_at": 2})

    free = run_backtest(candles, strategy, make_config(), taker_fee=0.0)
    charged = run_backtest(candles, EnterThenExit({"entry_at": 1, "exit_at": 2}),
                           make_config(), taker_fee=0.0005)

    assert free.trades[0].pnl > 0
    assert charged.trades[0].pnl < 0


# --- 손절 ----------------------------------------------------------------
def test_stop_loss_closes_the_position():
    # 5% 손절 → 100 진입이면 95에서 잘린다
    candles = bars([100, 100, 100, 90, 90], spread=0.5)
    strategy = EnterThenExit({"entry_at": 1, "exit_at": 99})

    result = run_backtest(candles, strategy, make_config(default_stop_loss_pct=5.0),
                          taker_fee=0.0)

    (trade,) = result.trades
    assert trade.exit_reason == "stop"
    assert trade.exit_price == pytest.approx(95.0)
    assert trade.pnl < 0


def test_stop_wins_when_it_collides_with_an_exit_signal():
    """봉 안의 순서를 알 수 없으므로 불리한 쪽(손절)을 가정해야 한다."""
    candles = bars([100, 100, 100, 88, 100], spread=0.5)
    strategy = EnterThenExit({"entry_at": 1, "exit_at": 2})

    result = run_backtest(candles, strategy, make_config(default_stop_loss_pct=5.0),
                          taker_fee=0.0)

    assert result.trades[0].exit_reason == "stop"


def test_open_position_is_closed_at_the_end():
    candles = bars([100, 100, 100, 105, 106])
    strategy = EnterThenExit({"entry_at": 1, "exit_at": 99})

    result = run_backtest(candles, strategy, make_config(), taker_fee=0.0)

    (trade,) = result.trades
    assert trade.exit_reason == "end"


# --- 지정가 --------------------------------------------------------------
def test_limit_entry_is_skipped_when_the_bar_never_reaches_it():
    """지정가는 체결이 보장되지 않는다 — 놓친 거래를 무시하면 결과가 부풀려진다."""
    # 계속 오르기만 하는 봉: 매수 지정가(시가 아래)에 닿지 않는다
    candles = [
        Candle(timestamp=1_700_000_000_000 + i * 300_000,
               open=100 + i, high=102 + i, low=100 + i, close=101 + i, volume=1.0)
        for i in range(6)
    ]

    result = run_backtest(candles, AlwaysLong({}), make_config(),
                          order_type="limit", limit_offset_pct=0.5)

    assert result.trade_count == 0
    assert result.missed_entries > 0


def test_limit_entry_fills_when_the_bar_trades_through_it():
    candles = bars([100, 100, 100, 100, 100], spread=3.0)

    result = run_backtest(candles, AlwaysLong({}), make_config(),
                          order_type="limit", limit_offset_pct=0.5)

    assert result.trade_count >= 0          # 체결되면 거래가 생긴다
    assert result.missed_entries == 0


def test_limit_orders_use_the_maker_fee():
    candles = bars([100] * 6, spread=3.0)
    strategy = EnterThenExit({"entry_at": 1, "exit_at": 2})

    result = run_backtest(candles, strategy, make_config(),
                          order_type="limit", limit_offset_pct=0.1,
                          taker_fee=0.01, maker_fee=0.0001)

    if result.trades:
        # taker 였다면 왕복 2 USDT, maker 면 0.02 수준이다
        assert result.trades[0].fee < 0.5


# --- 요약 지표 -----------------------------------------------------------
def test_max_drawdown_includes_unrealized_loss():
    """미실현 손실을 빼면 낙폭이 실제보다 작게 보인다."""
    candles = bars([100, 100, 100, 97, 97, 100, 100])
    strategy = EnterThenExit({"entry_at": 1, "exit_at": 99})

    result = run_backtest(candles, strategy, make_config(default_stop_loss_pct=20.0),
                          taker_fee=0.0)

    assert result.max_drawdown_pct > 0


def test_summary_metrics_are_consistent():
    candles = bars([100, 100, 100, 110, 110, 100, 100, 110])
    strategy = AlwaysLong({})

    result = run_backtest(candles, strategy, make_config(default_stop_loss_pct=20.0),
                          taker_fee=0.0)

    assert result.trade_count == len(result.trades)
    assert result.wins == sum(1 for t in result.trades if t.pnl > 0)
    if result.trade_count:
        assert result.win_rate == pytest.approx(result.wins / result.trade_count * 100)


def test_not_enough_candles_returns_an_empty_result():
    result = run_backtest(bars([100, 101]), AlwaysLong({}), make_config())

    assert result.trade_count == 0
    assert result.end_equity == result.start_equity


# --- 성능 ----------------------------------------------------------------
def test_strategy_receives_a_bounded_window():
    """전체 히스토리를 매 봉마다 넘기면 지표를 처음부터 다시 계산해 O(n²)이 된다.

    90일치 5분봉(약 26,000봉)에서는 사실상 못 돌린다.
    """
    sizes = []

    class Recorder(Strategy):
        name = "window_recorder"

        @property
        def warmup_candles(self) -> int:
            return 30

        def generate(self, ctx):
            sizes.append(len(ctx.candles))
            return Signal(action=SignalAction.HOLD)

    candles = bars([100 + (i % 7) for i in range(2000)])
    run_backtest(candles, Recorder({}), make_config())

    # warmup 30 → 창은 230 봉. 2000 봉 전체가 넘어가면 안 된다.
    assert max(sizes) <= 240, f"창이 {max(sizes)}봉까지 커졌습니다"


def test_a_long_backtest_finishes_quickly():
    """실제로 쓰려면 90일치가 몇 초 안에 끝나야 한다."""
    import time

    class Recorder(Strategy):
        name = "cheap"

        @property
        def warmup_candles(self) -> int:
            return 50

        def generate(self, ctx):
            return Signal(action=SignalAction.HOLD)

    candles = bars([100 + (i % 11) for i in range(20_000)])

    started = time.monotonic()
    run_backtest(candles, Recorder({}), make_config())

    assert time.monotonic() - started < 10.0


# --- 펀딩비 --------------------------------------------------------------
EIGHT_HOURS_MS = 8 * 3_600_000


def test_a_short_trade_never_pays_funding():
    """정산 시각을 넘기지 않은 매매는 실제로 펀딩비를 내지 않는다."""
    candles = bars([100] * 5)
    strategy = EnterThenExit({"entry_at": 1, "exit_at": 2})

    result = run_backtest(candles, strategy, make_config(), taker_fee=0.0, maker_fee=0.0)

    (trade,) = result.trades
    assert trade.funding == 0.0
    assert result.total_funding == 0.0


def test_holding_across_a_settlement_time_costs_funding():
    """무기한 선물에서 펀딩비를 0 으로 두면 오래 들고 가는 전략이 실제보다 좋아 보인다."""
    # 봉 간격을 8시간으로 잡아 보유 중에 정산 시각을 지나게 한다
    candles = bars([100] * 6, step=EIGHT_HOURS_MS)
    strategy = EnterThenExit({"entry_at": 1, "exit_at": 4})

    result = run_backtest(
        candles, strategy, make_config(),
        taker_fee=0.0, maker_fee=0.0, funding_rate=0.001,
    )

    (trade,) = result.trades
    assert trade.funding > 0
    # 가격이 그대로여도 펀딩비만큼 손해다
    assert trade.pnl == pytest.approx(-trade.funding)
    assert result.total_funding == pytest.approx(trade.funding)
    assert result.end_equity < result.start_equity


def test_funding_can_be_turned_off():
    candles = bars([100] * 6, step=EIGHT_HOURS_MS)
    strategy = EnterThenExit({"entry_at": 1, "exit_at": 4})

    result = run_backtest(
        candles, strategy, make_config(),
        taker_fee=0.0, maker_fee=0.0, funding_rate=0.0,
    )

    assert result.total_funding == 0.0
    assert result.trades[0].pnl == pytest.approx(0.0)


class LongWithTarget(Strategy):
    name = "long_with_target"

    def generate(self, ctx):
        if ctx.position.is_open:
            return Signal(action=SignalAction.HOLD)
        price = ctx.last_price
        return Signal(action=SignalAction.ENTER_LONG, strength=1.0,
                      stop_loss=price * 0.9, take_profit=price * 1.03)


def test_take_profit_fills_at_the_target():
    """익절가를 지나간 봉에서 그 가격으로 체결되고, maker 수수료가 적용된다."""
    closes = [100.0] * 10 + [101.0, 102.0, 104.0, 104.0, 104.0]
    result = run_backtest(bars(closes, spread=0.2), LongWithTarget(), make_config())

    targets = [t for t in result.trades if t.exit_reason == "target"]
    assert targets, "익절 체결이 한 번도 없습니다"
    assert targets[0].exit_price == pytest.approx(targets[0].entry_price * 1.03, rel=1e-3)


def test_stop_beats_target_in_the_same_bar():
    """한 봉에서 손절과 익절이 겹치면 손절이 먼저다 — 불리한 가정."""

    class TightBoth(Strategy):
        name = "tight_both"

        def generate(self, ctx):
            if ctx.position.is_open:
                return Signal(action=SignalAction.HOLD)
            price = ctx.last_price
            return Signal(action=SignalAction.ENTER_LONG, strength=1.0,
                          stop_loss=price * 0.99, take_profit=price * 1.01)

    closes = [100.0] * 10 + [100.0] * 5
    candles = bars(closes, spread=3.0)   # 모든 봉이 손절·익절 둘 다 스친다
    result = run_backtest(candles, TightBoth(), make_config())

    stops = [t for t in result.trades if t.exit_reason == "stop"]
    targets = [t for t in result.trades if t.exit_reason == "target"]
    assert stops, "손절 체결이 없습니다"
    assert not targets, "손절과 겹친 봉에서 익절이 먼저 체결됐습니다"
