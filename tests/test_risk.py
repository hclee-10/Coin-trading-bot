from datetime import datetime, timedelta, timezone

import pytest

from bot.config import RiskConfig
from bot.models import Signal, SignalAction
from bot.risk import RiskManager


def make_risk(**overrides) -> RiskManager:
    """위험비율 방식(손절 폭에서 수량을 역산)을 검증할 때 쓴다.

    기본 사이징은 확신도 기반 고정 금액이므로 여기서는 명시적으로 지정한다.
    """
    overrides.setdefault("sizing_mode", "risk")
    return RiskManager(RiskConfig(**overrides), leverage=3.0)


def test_size_is_derived_from_stop_distance():
    # 명목가 상한을 넉넉히 풀어 손절폭 기반 사이징만 확인한다
    risk = make_risk(
        risk_per_trade_pct=1.0, default_stop_loss_pct=2.0, max_position_notional_pct=100.0
    )
    signal = Signal(action=SignalAction.ENTER_LONG)

    decision = risk.evaluate_entry(
        signal=signal, entry_price=100.0, equity=10_000.0, open_positions=0
    )

    # 자기자본의 1% = 100 USDT 위험, 손절폭 2 USDT → 50 코인
    assert decision.approved
    assert decision.stop_loss == pytest.approx(98.0)
    assert decision.base_amount == pytest.approx(50.0)


def test_notional_is_capped_by_equity_share():
    risk = make_risk(
        risk_per_trade_pct=10.0, default_stop_loss_pct=0.5, max_position_notional_pct=20.0
    )
    decision = risk.evaluate_entry(
        signal=Signal(action=SignalAction.ENTER_LONG),
        entry_price=100.0, equity=1_000.0, open_positions=0,
    )
    assert decision.approved
    assert decision.notional == pytest.approx(200.0)  # 1000 의 20%


def test_strength_scales_position_size():
    risk = make_risk(
        risk_per_trade_pct=1.0, default_stop_loss_pct=2.0, max_position_notional_pct=100.0
    )
    half = risk.evaluate_entry(
        signal=Signal(action=SignalAction.ENTER_LONG, strength=0.5),
        entry_price=100.0, equity=10_000.0, open_positions=0,
    )
    assert half.base_amount == pytest.approx(25.0)


def test_short_stop_and_take_profit_are_mirrored():
    risk = make_risk(default_stop_loss_pct=1.0, default_take_profit_pct=2.0)
    decision = risk.evaluate_entry(
        signal=Signal(action=SignalAction.ENTER_SHORT),
        entry_price=100.0, equity=10_000.0, open_positions=0,
    )
    assert decision.stop_loss == pytest.approx(101.0)
    assert decision.take_profit == pytest.approx(98.0)


def test_stop_on_wrong_side_is_rejected():
    risk = make_risk()
    decision = risk.evaluate_entry(
        signal=Signal(action=SignalAction.ENTER_LONG, stop_loss=105.0),
        entry_price=100.0, equity=10_000.0, open_positions=0,
    )
    assert not decision.approved
    assert "방향" in decision.reason


def test_max_open_positions_blocks_entry():
    risk = make_risk(max_open_positions=1)
    decision = risk.evaluate_entry(
        signal=Signal(action=SignalAction.ENTER_LONG),
        entry_price=100.0, equity=10_000.0, open_positions=1,
    )
    assert not decision.approved
    assert "한도" in decision.reason


def test_dust_orders_are_rejected():
    risk = make_risk(risk_per_trade_pct=0.001, min_order_notional=5.0)
    decision = risk.evaluate_entry(
        signal=Signal(action=SignalAction.ENTER_LONG),
        entry_price=100.0, equity=100.0, open_positions=0,
    )
    assert not decision.approved
    assert "최소 주문금액" in decision.reason


def test_kill_switch_trips_and_blocks_entries():
    risk = make_risk(max_daily_loss_pct=3.0)
    day = datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc)
    risk.update_equity(1_000.0, now=day)
    assert not risk.halted

    risk.update_equity(969.0, now=day + timedelta(hours=1))
    assert risk.halted

    decision = risk.evaluate_entry(
        signal=Signal(action=SignalAction.ENTER_LONG),
        entry_price=100.0, equity=969.0, open_positions=0,
    )
    assert not decision.approved
    assert "킬스위치" in decision.reason


def test_kill_switch_resets_on_new_utc_day():
    risk = make_risk(max_daily_loss_pct=3.0)
    day = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
    risk.update_equity(1_000.0, now=day)
    risk.update_equity(900.0, now=day + timedelta(hours=1))
    assert risk.halted

    risk.update_equity(900.0, now=day + timedelta(days=1))
    assert not risk.halted
    assert risk.day_start_equity == 900.0


# --- 확신도 기반 고정 금액 사이징 ------------------------------------------
def tier_risk(**overrides) -> RiskManager:
    cfg = RiskConfig(sizing_mode="tiers", max_position_notional_pct=100.0, **overrides)
    return RiskManager(cfg, leverage=3.0)


@pytest.mark.parametrize(
    "conviction,expected",
    [(0.25, 50.0), (0.50, 100.0), (0.75, 150.0), (1.00, 200.0)],
)
def test_conviction_maps_to_its_notional_tier(conviction, expected):
    assert tier_risk().notional_for(conviction) == expected


@pytest.mark.parametrize(
    "strength,expected",
    [(0.01, 50.0), (0.26, 100.0), (0.60, 150.0), (0.80, 200.0), (1.5, 200.0)],
)
def test_strengths_between_tiers_round_into_a_band(strength, expected):
    """전략이 중간값을 내도 등급 하나로 떨어져야 한다."""
    assert tier_risk().notional_for(strength) == expected


def test_tier_sizing_uses_the_notional_not_the_stop_distance():
    """고정 금액 방식에서는 손절 폭이 수량을 바꾸지 않는다."""
    risk = tier_risk()

    tight = risk.evaluate_entry(
        signal=Signal(action=SignalAction.ENTER_LONG, strength=0.5, stop_loss=99.0),
        entry_price=100.0, equity=10_000.0, open_positions=0,
    )
    wide = risk.evaluate_entry(
        signal=Signal(action=SignalAction.ENTER_LONG, strength=0.5, stop_loss=90.0),
        entry_price=100.0, equity=10_000.0, open_positions=0,
    )

    assert tight.notional == wide.notional == pytest.approx(100.0)
    assert tight.base_amount == wide.base_amount == pytest.approx(1.0)


def test_tier_sizing_still_requires_a_stop_loss():
    """금액이 고정이어도 손절 없는 진입은 허용하지 않는다."""
    decision = tier_risk().evaluate_entry(
        signal=Signal(action=SignalAction.ENTER_LONG, strength=0.5, stop_loss=100.0),
        entry_price=100.0, equity=10_000.0, open_positions=0,
    )

    assert not decision.approved
    assert "손절가와 진입가가 같습니다" in decision.reason


def test_tier_sizing_is_still_capped_by_equity_share():
    """소액 계좌에서 고정 금액이 계좌를 넘어서면 안 된다."""
    risk = RiskManager(
        RiskConfig(sizing_mode="tiers", max_position_notional_pct=10.0), leverage=3.0
    )

    decision = risk.evaluate_entry(
        signal=Signal(action=SignalAction.ENTER_LONG, strength=1.0),
        entry_price=100.0, equity=1_000.0, open_positions=0,
    )

    assert decision.notional == pytest.approx(100.0)  # 200 이 아니라 1000 의 10%


def test_no_take_profit_by_default():
    """수익률 제한을 두지 않는다 — 익절 주문을 걸지 않는다."""
    risk = tier_risk()

    decision = risk.evaluate_entry(
        signal=Signal(action=SignalAction.ENTER_LONG, strength=0.5),
        entry_price=100.0, equity=10_000.0, open_positions=0,
    )

    assert decision.take_profit is None
    assert decision.stop_loss == pytest.approx(99.0)


def test_risk_mode_still_derives_size_from_the_stop():
    """예전 방식도 선택지로 남아 있어야 한다."""
    risk = RiskManager(
        RiskConfig(sizing_mode="risk", risk_per_trade_pct=1.0,
                   max_position_notional_pct=100.0, default_stop_loss_pct=2.0),
        leverage=3.0,
    )

    decision = risk.evaluate_entry(
        signal=Signal(action=SignalAction.ENTER_LONG),
        entry_price=100.0, equity=10_000.0, open_positions=0,
    )

    assert decision.base_amount == pytest.approx(50.0)
