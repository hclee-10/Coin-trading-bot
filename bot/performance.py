"""체결 내역에서 왕복 거래와 성과를 복원한다.

봇이 낸 주문만으로는 기록이 불완전하다 — 손절·익절은 거래소에 걸어 두므로 봇이
모르는 사이에 체결된다. 그래서 성과는 거래소에서 받아 온 **실제 체결**을 기준으로
계산한다.

단방향(one-way) 모드를 전제한다. 포지션 방향이 뒤집히면 그 지점에서 왕복 하나가
닫히고 새 왕복이 열린 것으로 본다.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from bot.store import EquityPoint, Fill


@dataclass(frozen=True)
class RoundTrip:
    """진입부터 청산까지 한 번의 왕복."""

    symbol: str
    side: str            # long | short
    opened_at: int       # ms
    closed_at: int       # ms
    entry_price: float
    exit_price: float
    amount: float        # 청산된 계약 수
    pnl: float           # 수수료를 뺀 실현 손익 (견적통화)
    fee: float
    # 명목가를 계산하려면 계약 크기가 필요하다. Gate 는 1계약이 0.0001 BTC 라
    # 이걸 빼먹으면 명목가가 1만 배로 부풀어 수익률이 0 으로 보인다.
    contract_size: float = 1.0

    @property
    def is_win(self) -> bool:
        return self.pnl > 0

    @property
    def notional(self) -> float:
        """진입 시점 명목가(견적통화)."""
        return abs(self.entry_price * self.amount * self.contract_size)

    @property
    def return_pct(self) -> float:
        """진입 명목가 대비 수익률(%)."""
        return (self.pnl / self.notional * 100) if self.notional else 0.0


@dataclass
class _OpenPosition:
    """왕복을 복원하는 동안 들고 있는 진행 중 포지션."""

    side: str
    amount: float = 0.0        # 항상 양수
    avg_entry: float = 0.0
    opened_at: int = 0
    fee: float = 0.0


def round_trips(fills: list[Fill], *, contract_size: float = 1.0) -> list[RoundTrip]:
    """체결 목록(오래된 순)에서 닫힌 왕복만 뽑아낸다."""
    trips: list[RoundTrip] = []
    open_position: _OpenPosition | None = None

    for fill in fills:
        remaining = abs(fill.amount)
        if remaining <= 0:
            continue
        incoming_side = "long" if fill.side == "buy" else "short"
        # 수수료는 체결 수량에 비례해 나눠 붙인다.
        fee_per_unit = (fill.fee or 0.0) / remaining

        # 1) 반대 방향이면 먼저 기존 포지션을 줄인다.
        if open_position is not None and open_position.side != incoming_side:
            closing = min(open_position.amount, remaining)
            direction = 1.0 if open_position.side == "long" else -1.0
            gross = (fill.price - open_position.avg_entry) * closing * contract_size * direction
            # 진입 수수료는 청산되는 비율만큼, 청산 수수료는 이번 물량만큼.
            entry_fee = open_position.fee * (closing / open_position.amount)
            exit_fee = fee_per_unit * closing
            trips.append(
                RoundTrip(
                    symbol=fill.symbol,
                    side=open_position.side,
                    opened_at=open_position.opened_at,
                    closed_at=fill.timestamp,
                    entry_price=open_position.avg_entry,
                    exit_price=fill.price,
                    amount=closing,
                    pnl=gross - entry_fee - exit_fee,
                    fee=entry_fee + exit_fee,
                    contract_size=contract_size,
                )
            )
            open_position.fee -= entry_fee
            open_position.amount -= closing
            remaining -= closing
            if open_position.amount <= 1e-12:
                open_position = None

        if remaining <= 1e-12:
            continue

        # 2) 남은 물량은 새 포지션이거나 기존 포지션에 더해지는 물량이다.
        if open_position is None:
            open_position = _OpenPosition(
                side=incoming_side,
                amount=remaining,
                avg_entry=fill.price,
                opened_at=fill.timestamp,
                fee=fee_per_unit * remaining,
            )
        else:
            total = open_position.amount + remaining
            open_position.avg_entry = (
                open_position.avg_entry * open_position.amount + fill.price * remaining
            ) / total
            open_position.amount = total
            open_position.fee += fee_per_unit * remaining

    return trips


@dataclass
class Performance:
    """대시보드가 보여 주는 성과 요약."""

    trade_count: int = 0
    win_count: int = 0
    loss_count: int = 0
    realized_pnl: float = 0.0
    total_fee: float = 0.0
    best_pnl: float = 0.0
    worst_pnl: float = 0.0
    start_equity: float | None = None
    current_equity: float | None = None
    started_at: int | None = None
    trips: list[RoundTrip] = field(default_factory=list)

    @property
    def win_rate(self) -> float | None:
        """승률(%). 닫힌 거래가 없으면 None — 0% 로 보이면 오해를 부른다."""
        return (self.win_count / self.trade_count * 100) if self.trade_count else None

    @property
    def total_return_pct(self) -> float | None:
        """기록 시작 시점 자기자본 대비 수익률(%)."""
        if not self.start_equity or self.current_equity is None:
            return None
        return (self.current_equity - self.start_equity) / self.start_equity * 100

    @property
    def equity_change(self) -> float | None:
        if self.start_equity is None or self.current_equity is None:
            return None
        return self.current_equity - self.start_equity


def summarize(
    fills: list[Fill],
    equity: list[EquityPoint],
    *,
    contract_size: float = 1.0,
) -> Performance:
    """체결과 자기자본 스냅샷에서 성과 요약을 만든다.

    수익률은 왕복 손익의 합이 아니라 **자기자본 변화**로 계산한다. 자금을
    입출금하지 않는 한 그쪽이 실제로 번 돈이고, 미실현 손익과 펀딩비까지
    반영되기 때문이다.
    """
    trips = round_trips(fills, contract_size=contract_size)
    wins = [t for t in trips if t.is_win]

    performance = Performance(
        trade_count=len(trips),
        win_count=len(wins),
        loss_count=len(trips) - len(wins),
        realized_pnl=sum(t.pnl for t in trips),
        total_fee=sum(t.fee for t in trips),
        best_pnl=max((t.pnl for t in trips), default=0.0),
        worst_pnl=min((t.pnl for t in trips), default=0.0),
        trips=trips,
    )
    if equity:
        performance.start_equity = equity[0].equity
        performance.current_equity = equity[-1].equity
        performance.started_at = equity[0].timestamp
    return performance
