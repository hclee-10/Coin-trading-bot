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

    def __init__(self, path: str | Path | None) -> None:
        self._lock = threading.Lock()
        self.path = str(path) if path else ":memory:"
        self.persistent = self.path != ":memory:"

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
                self.path, self.persistent = ":memory:", False
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

    def close(self) -> None:
        with self._lock:
            self._db.close()
