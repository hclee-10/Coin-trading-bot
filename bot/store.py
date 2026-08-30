"""체결 내역과 자기자본 스냅샷을 담는 저장소.

컨테이너는 재배포하면 파일시스템이 초기화되므로, 수익률을 보려면 기록이
볼륨처럼 살아남는 곳에 있어야 한다. 외부 서비스를 붙이지 않고 SQLite 파일
하나로 해결한다.

쓸 수 없는 경로가 주어져도 봇을 죽이지 않는다 — 볼륨이 아직 안 붙었거나 권한이
없으면 메모리 DB 로 내려가고 경고만 남긴다. 기록이 사라지는 것보다 매매가
멈추는 쪽이 더 나쁘다.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

log = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS fills (
    id          TEXT PRIMARY KEY,   -- 거래소 체결 id (중복 삽입 방지)
    symbol      TEXT NOT NULL,
    timestamp   INTEGER NOT NULL,   -- ms
    side        TEXT NOT NULL,      -- buy | sell
    price       REAL NOT NULL,
    amount      REAL NOT NULL,      -- 계약 수
    cost        REAL NOT NULL,      -- 견적통화 기준 체결금액
    fee         REAL NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_fills_symbol_ts ON fills(symbol, timestamp);

CREATE TABLE IF NOT EXISTS equity (
    timestamp   INTEGER PRIMARY KEY,  -- ms
    equity      REAL NOT NULL
);

-- 전략 경쟁 모의매매. 전략마다 독립된 가상 계좌를 굴린다.
CREATE TABLE IF NOT EXISTS paper_accounts (
    strategy     TEXT PRIMARY KEY,
    start_equity REAL NOT NULL,
    started_at   INTEGER NOT NULL,
    peak_equity  REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS paper_trades (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    strategy     TEXT NOT NULL,
    symbol       TEXT NOT NULL,
    side         TEXT NOT NULL,          -- long | short
    opened_at    INTEGER NOT NULL,
    closed_at    INTEGER NOT NULL,
    entry_price  REAL NOT NULL,
    exit_price   REAL NOT NULL,
    amount       REAL NOT NULL,
    notional     REAL NOT NULL,
    pnl          REAL NOT NULL,
    fee          REAL NOT NULL,
    exit_reason  TEXT NOT NULL,          -- signal | stop | reverse
    conviction   REAL NOT NULL,
    worst_excursion_pct REAL NOT NULL DEFAULT 0  -- 청산가까지 얼마나 갔는지
);
CREATE INDEX IF NOT EXISTS idx_paper_trades_strategy ON paper_trades(strategy, closed_at);

-- 진행 중인 가상 포지션. 재시작해도 이어서 굴리기 위해 저장한다.
CREATE TABLE IF NOT EXISTS paper_positions (
    strategy     TEXT NOT NULL,
    symbol       TEXT NOT NULL,
    side         TEXT NOT NULL,
    opened_at    INTEGER NOT NULL,
    entry_price  REAL NOT NULL,
    amount       REAL NOT NULL,
    notional     REAL NOT NULL,
    stop_loss    REAL NOT NULL,
    entry_fee    REAL NOT NULL,
    conviction   REAL NOT NULL,
    worst_excursion_pct REAL NOT NULL DEFAULT 0,
    PRIMARY KEY (strategy, symbol)
);
"""


@dataclass(frozen=True)
class Fill:
    """거래소에서 실제로 체결된 한 건."""

    id: str
    symbol: str
    timestamp: int
    side: str
    price: float
    amount: float
    cost: float
    fee: float = 0.0


@dataclass(frozen=True)
class EquityPoint:
    timestamp: int
    equity: float


class Store:
    """SQLite 저장소. 여러 스레드에서 안전하게 쓸 수 있다."""

    def __init__(self, path: str | Path | None, *, durable: bool = True) -> None:
        """`durable` 은 이 경로가 **컨테이너 재배포를 넘어** 살아남는지를 뜻한다.

        파일에 쓰는 데 성공하는 것(persistent)과 그 파일이 다음 배포에도 남아
        있는 것은 다른 문제다. 볼륨 없이 컨테이너 안에 쓰면 기록은 멀쩡히
        저장되지만 재배포 한 번에 통째로 사라진다 — 경고 없이 며칠치 모의매매
        성적을 잃는 경로가 바로 이것이다.
        """
        self._lock = threading.Lock()
        self.path = str(path) if path else ":memory:"
        self.persistent = self.path != ":memory:"
        self.durable = bool(durable) and self.persistent

        if self.persistent:
            try:
                Path(self.path).parent.mkdir(parents=True, exist_ok=True)
                connection = sqlite3.connect(self.path, check_same_thread=False)
            except (OSError, sqlite3.Error) as exc:
                log.warning(
                    "거래 기록 DB '%s' 를 열 수 없어 메모리에만 보관합니다 "
                    "(재시작하면 사라집니다): %s",
                    self.path, exc,
                )
                self.path, self.persistent, self.durable = ":memory:", False, False
                connection = sqlite3.connect(":memory:", check_same_thread=False)
        else:
            connection = sqlite3.connect(":memory:", check_same_thread=False)

        connection.row_factory = sqlite3.Row
        self._db = connection
        with self._lock:
            self._db.executescript(_SCHEMA)
            self._db.commit()

    # ------------------------------------------------------------------
    def record_fills(self, fills: Iterable[Fill]) -> int:
        """체결을 저장한다. 이미 있는 id 는 건너뛰고, 새로 넣은 건수를 돌려준다.

        같은 체결을 여러 번 받아 오는 것은 정상이다 — 동기화는 시각 기준으로
        겹치게 조회하므로, 중복은 여기서 걸러야 한다.
        """
        rows = [
            (f.id, f.symbol, f.timestamp, f.side, f.price, f.amount, f.cost, f.fee)
            for f in fills
        ]
        if not rows:
            return 0
        with self._lock:
            before = self._db.total_changes
            self._db.executemany(
                "INSERT OR IGNORE INTO fills "
                "(id, symbol, timestamp, side, price, amount, cost, fee) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                rows,
            )
            self._db.commit()
            return self._db.total_changes - before

    def fills(self, symbol: str | None = None, limit: int = 500) -> list[Fill]:
        """최근 체결을 오래된 것부터 돌려준다."""
        query = "SELECT * FROM fills"
        params: list[Any] = []
        if symbol:
            query += " WHERE symbol = ?"
            params.append(symbol)
        query += " ORDER BY timestamp DESC LIMIT ?"
        params.append(limit)
        with self._lock:
            rows = self._db.execute(query, params).fetchall()
        return [
            Fill(
                id=r["id"], symbol=r["symbol"], timestamp=r["timestamp"], side=r["side"],
                price=r["price"], amount=r["amount"], cost=r["cost"], fee=r["fee"],
            )
            for r in reversed(rows)
        ]

    def last_fill_timestamp(self, symbol: str) -> int | None:
        """이 심볼에서 마지막으로 기록한 체결 시각(ms)."""
        with self._lock:
            row = self._db.execute(
                "SELECT MAX(timestamp) AS ts FROM fills WHERE symbol = ?", (symbol,)
            ).fetchone()
        return row["ts"] if row and row["ts"] is not None else None

    # ------------------------------------------------------------------
    def record_equity(self, timestamp: int, equity: float) -> None:
        """자기자본 스냅샷. 같은 시각이면 덮어쓴다."""
        with self._lock:
            self._db.execute(
                "INSERT OR REPLACE INTO equity (timestamp, equity) VALUES (?, ?)",
                (timestamp, equity),
            )
            self._db.commit()

    def equity_curve(self, limit: int = 1000) -> list[EquityPoint]:
        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM equity ORDER BY timestamp DESC LIMIT ?", (limit,)
            ).fetchall()
        return [EquityPoint(timestamp=r["timestamp"], equity=r["equity"]) for r in reversed(rows)]

    def first_equity(self) -> EquityPoint | None:
        """기록의 시작점. 수익률의 기준이 된다."""
        with self._lock:
            row = self._db.execute(
                "SELECT * FROM equity ORDER BY timestamp ASC LIMIT 1"
            ).fetchone()
        return EquityPoint(timestamp=row["timestamp"], equity=row["equity"]) if row else None

    # ------------------------------------------------------------------
    # 전략 경쟁 모의매매
    # ------------------------------------------------------------------
    def paper_account(self, strategy: str, *, start_equity: float, now_ms: int) -> dict[str, Any]:
        """전략의 가상 계좌를 가져오거나 처음이면 만든다.

        새 전략이 나중에 합류해도 자신의 시작 시점부터 수익률이 계산된다.
        """
        with self._lock:
            row = self._db.execute(
                "SELECT * FROM paper_accounts WHERE strategy = ?", (strategy,)
            ).fetchone()
            if row is None:
                self._db.execute(
                    "INSERT INTO paper_accounts (strategy, start_equity, started_at, peak_equity)"
                    " VALUES (?, ?, ?, ?)",
                    (strategy, start_equity, now_ms, start_equity),
                )
                self._db.commit()
                return {
                    "strategy": strategy, "start_equity": start_equity,
                    "started_at": now_ms, "peak_equity": start_equity,
                }
            return dict(row)

    def update_paper_peak(self, strategy: str, peak_equity: float) -> None:
        with self._lock:
            self._db.execute(
                "UPDATE paper_accounts SET peak_equity = ? WHERE strategy = ?",
                (peak_equity, strategy),
            )
            self._db.commit()

    def paper_accounts(self) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(r) for r in self._db.execute("SELECT * FROM paper_accounts").fetchall()]

    def save_paper_position(self, strategy: str, symbol: str, data: dict[str, Any]) -> None:
        with self._lock:
            self._db.execute(
                "INSERT OR REPLACE INTO paper_positions (strategy, symbol, side, opened_at,"
                " entry_price, amount, notional, stop_loss, entry_fee, conviction,"
                " worst_excursion_pct) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (strategy, symbol, data["side"], data["opened_at"], data["entry_price"],
                 data["amount"], data["notional"], data["stop_loss"], data["entry_fee"],
                 data["conviction"], data.get("worst_excursion_pct", 0.0)),
            )
            self._db.commit()

    def delete_paper_position(self, strategy: str, symbol: str) -> None:
        with self._lock:
            self._db.execute(
                "DELETE FROM paper_positions WHERE strategy = ? AND symbol = ?",
                (strategy, symbol),
            )
            self._db.commit()

    def paper_positions(self) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(r) for r in self._db.execute("SELECT * FROM paper_positions").fetchall()]

    def record_paper_trade(self, trade: dict[str, Any]) -> None:
        with self._lock:
            self._db.execute(
                "INSERT INTO paper_trades (strategy, symbol, side, opened_at, closed_at,"
                " entry_price, exit_price, amount, notional, pnl, fee, exit_reason,"
                " conviction, worst_excursion_pct)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (trade["strategy"], trade["symbol"], trade["side"], trade["opened_at"],
                 trade["closed_at"], trade["entry_price"], trade["exit_price"],
                 trade["amount"], trade["notional"], trade["pnl"], trade["fee"],
                 trade["exit_reason"], trade["conviction"], trade.get("worst_excursion_pct", 0.0)),
            )
            self._db.commit()

    def paper_trades(self, strategy: str | None = None, limit: int = 2000) -> list[dict[str, Any]]:
        query = "SELECT * FROM paper_trades"
        params: list[Any] = []
        if strategy:
            query += " WHERE strategy = ?"
            params.append(strategy)
        query += " ORDER BY closed_at DESC LIMIT ?"
        params.append(limit)
        with self._lock:
            return [dict(r) for r in self._db.execute(query, params).fetchall()]

    def reset_paper(self) -> None:
        """모의매매 기록을 전부 지운다. 조건을 바꿔 다시 비교할 때 쓴다."""
        with self._lock:
            for table in ("paper_trades", "paper_positions", "paper_accounts"):
                self._db.execute(f"DELETE FROM {table}")
            self._db.commit()

    def close(self) -> None:
        with self._lock:
            self._db.close()
