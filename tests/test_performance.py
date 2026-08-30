"""왕복 거래 복원과 성과 계산 — 돈이 걸린 계산이라 경계를 촘촘히 본다."""

import pytest

from bot.performance import round_trips, summarize
from bot.store import EquityPoint, Fill

SYMBOL = "BTC/USDT:USDT"


def fill(fid, ts, side, price, amount, fee=0.0):
    return Fill(
        id=fid, symbol=SYMBOL, timestamp=ts, side=side,
        price=price, amount=amount, cost=price * amount, fee=fee,
    )


def test_long_round_trip_profit():
    trips = round_trips([
        fill("1", 1000, "buy", 100.0, 10),
        fill("2", 2000, "sell", 110.0, 10),
    ])

    (trip,) = trips
    assert trip.side == "long"
    assert trip.pnl == pytest.approx(100.0)   # (110-100) * 10
    assert trip.opened_at == 1000 and trip.closed_at == 2000
    assert trip.is_win


def test_short_round_trip_profit():
    """숏은 가격이 내려야 이익이다 — 부호를 뒤집지 않으면 손익이 반대로 나온다."""
    trips = round_trips([
        fill("1", 1000, "sell", 100.0, 10),
        fill("2", 2000, "buy", 90.0, 10),
    ])

    (trip,) = trips
    assert trip.side == "short"
    assert trip.pnl == pytest.approx(100.0)
    assert trip.is_win


def test_short_round_trip_loss():
    trips = round_trips([
        fill("1", 1000, "sell", 100.0, 10),
        fill("2", 2000, "buy", 110.0, 10),
    ])

    assert trips[0].pnl == pytest.approx(-100.0)
    assert not trips[0].is_win


def test_contract_size_scales_pnl():
    """Gate 는 1계약이 0.0001 BTC 다 — 빠뜨리면 손익이 1만 배로 보인다."""
    trips = round_trips([
        fill("1", 1000, "buy", 60000.0, 1000),
        fill("2", 2000, "sell", 61000.0, 1000),
    ], contract_size=0.0001)

    assert trips[0].pnl == pytest.approx(100.0)  # 1000 * 1000 * 0.0001


def test_partial_close_leaves_the_rest_open():
    trips = round_trips([
        fill("1", 1000, "buy", 100.0, 10),
        fill("2", 2000, "sell", 110.0, 4),
    ])

    (trip,) = trips
    assert trip.amount == pytest.approx(4)
    assert trip.pnl == pytest.approx(40.0)


def test_averaging_in_uses_weighted_entry():
    """추가 진입하면 평균 단가로 계산해야 한다."""
    trips = round_trips([
        fill("1", 1000, "buy", 100.0, 10),
        fill("2", 1500, "buy", 120.0, 10),   # 평균 110
        fill("3", 2000, "sell", 130.0, 20),
    ])

    (trip,) = trips
    assert trip.entry_price == pytest.approx(110.0)
    assert trip.pnl == pytest.approx(400.0)   # (130-110) * 20


def test_reversal_closes_one_trip_and_opens_another():
    """반대 방향으로 크게 뒤집으면 왕복 하나가 닫히고 새 포지션이 열린다."""
    trips = round_trips([
        fill("1", 1000, "buy", 100.0, 10),
        fill("2", 2000, "sell", 110.0, 25),   # 롱 10 청산 + 숏 15 진입
        fill("3", 3000, "buy", 100.0, 15),    # 숏 청산
    ])

    assert len(trips) == 2
    assert trips[0].side == "long" and trips[0].pnl == pytest.approx(100.0)
    assert trips[1].side == "short" and trips[1].pnl == pytest.approx(150.0)


def test_open_position_is_not_counted_as_a_trade():
    """아직 안 닫힌 포지션을 손익에 넣으면 수익률이 거짓말이 된다."""
    assert round_trips([fill("1", 1000, "buy", 100.0, 10)]) == []


def test_fees_are_split_across_partial_closes():
    trips = round_trips([
        fill("1", 1000, "buy", 100.0, 10, fee=2.0),
        fill("2", 2000, "sell", 110.0, 5, fee=1.0),
        fill("3", 3000, "sell", 110.0, 5, fee=1.0),
    ])

    assert len(trips) == 2
    # 진입 수수료 2.0 이 절반씩, 청산 수수료는 각 1.0
    assert trips[0].fee == pytest.approx(2.0)
    assert trips[1].fee == pytest.approx(2.0)
    assert sum(t.fee for t in trips) == pytest.approx(4.0)
    assert sum(t.pnl for t in trips) == pytest.approx(100.0 - 4.0)


def test_return_pct_is_relative_to_entry_notional():
    trips = round_trips([
        fill("1", 1000, "buy", 100.0, 10),
        fill("2", 2000, "sell", 110.0, 10),
    ])

    assert trips[0].return_pct == pytest.approx(10.0)


# --- 요약 ----------------------------------------------------------------
def test_summary_counts_wins_and_losses():
    fills = [
        fill("1", 1000, "buy", 100.0, 10),
        fill("2", 2000, "sell", 110.0, 10),   # +100
        fill("3", 3000, "buy", 100.0, 10),
        fill("4", 4000, "sell", 90.0, 10),    # -100
    ]

    summary = summarize(fills, [])

    assert (summary.trade_count, summary.win_count, summary.loss_count) == (2, 1, 1)
    assert summary.win_rate == pytest.approx(50.0)
    assert summary.realized_pnl == pytest.approx(0.0)
    assert summary.best_pnl == pytest.approx(100.0)
    assert summary.worst_pnl == pytest.approx(-100.0)


def test_return_comes_from_equity_not_from_trade_pnl():
    """자기자본 변화가 실제로 번 돈이다 — 미실현 손익과 펀딩비까지 반영된다."""
    equity = [EquityPoint(1000, 10_000.0), EquityPoint(9000, 10_500.0)]

    summary = summarize([], equity)

    assert summary.start_equity == 10_000.0
    assert summary.current_equity == 10_500.0
    assert summary.equity_change == pytest.approx(500.0)
    assert summary.total_return_pct == pytest.approx(5.0)


def test_win_rate_is_none_without_closed_trades():
    """거래가 없는데 승률 0% 로 보이면 지고 있는 것처럼 오해된다."""
    summary = summarize([], [EquityPoint(1000, 10_000.0)])

    assert summary.win_rate is None
    assert summary.trade_count == 0


def test_return_is_none_without_equity_history():
    assert summarize([], []).total_return_pct is None


def test_return_pct_accounts_for_contract_size():
    """Gate 는 1계약이 0.0001 BTC 다.

    명목가를 진입가 × 수량으로만 계산하면 1만 배로 부풀어 수익률이 0% 로 보인다.
    """
    trips = round_trips([
        fill("1", 1000, "buy", 60_800.0, 4000),
        fill("2", 2000, "sell", 61_950.0, 4000),
    ], contract_size=0.0001)

    (trip,) = trips
    assert trip.notional == pytest.approx(24_320.0)      # 60800 * 4000 * 0.0001
    assert trip.pnl == pytest.approx(460.0)              # 1150 * 4000 * 0.0001
    assert trip.return_pct == pytest.approx(1.891, abs=0.001)


def test_return_pct_without_contract_size_is_plain_percent():
    trips = round_trips([
        fill("1", 1000, "buy", 100.0, 10),
        fill("2", 2000, "sell", 105.0, 10),
    ])

    assert trips[0].notional == pytest.approx(1000.0)
    assert trips[0].return_pct == pytest.approx(5.0)
