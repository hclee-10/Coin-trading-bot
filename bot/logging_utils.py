"""콘솔 + 회전 파일 로깅 설정."""

from __future__ import annotations

import logging
import logging.handlers
import threading
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

_FORMAT = "%(asctime)s %(levelname)-7s %(name)s | %(message)s"


@dataclass(frozen=True)
class LogEntry:
    seq: int
    at: str        # ISO-8601 UTC
    level: str
    logger: str
    message: str


class LogBuffer:
    """최근 로그를 메모리에 들고 있는 링버퍼.

    웹 대시보드가 `since(seq)` 로 새로 생긴 줄만 받아 간다. 봇 스레드가 쓰고
    요청 스레드가 읽으므로 락으로 감싼다. 오래된 줄은 조용히 밀려난다 —
    영구 기록은 파일 핸들러 몫이다.
    """

    def __init__(self, capacity: int = 1000) -> None:
        self._entries: deque[LogEntry] = deque(maxlen=capacity)
        self._lock = threading.Lock()
        self._seq = 0

    def append(self, level: str, logger_name: str, message: str) -> None:
        with self._lock:
            self._seq += 1
            self._entries.append(
                LogEntry(
                    seq=self._seq,
                    at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
                    level=level,
                    logger=logger_name,
                    message=message,
                )
            )

    def since(self, seq: int = 0, limit: int = 500) -> list[LogEntry]:
        """seq 보다 큰 항목만 오래된 순으로 돌려준다."""
        with self._lock:
            return [e for e in self._entries if e.seq > seq][-limit:]

    @property
    def latest_seq(self) -> int:
        with self._lock:
            return self._seq

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()


class BufferHandler(logging.Handler):
    """로그 레코드를 LogBuffer 로 흘려보내는 핸들러."""

    def __init__(self, buffer: LogBuffer) -> None:
        super().__init__()
        self.buffer = buffer

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.buffer.append(record.levelname, record.name, record.getMessage())
        except Exception:  # pragma: no cover - 로깅이 앱을 죽이면 안 된다
            self.handleError(record)


def setup_logging(
    level: str = "INFO", file: str | None = None, buffer: LogBuffer | None = None
) -> None:
    root = logging.getLogger()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    for handler in list(root.handlers):
        root.removeHandler(handler)

    formatter = logging.Formatter(_FORMAT)
    console = logging.StreamHandler()
    console.setFormatter(formatter)
    root.addHandler(console)

    if file:
        path = Path(file)
        path.parent.mkdir(parents=True, exist_ok=True)
        rotating = logging.handlers.RotatingFileHandler(
            path, maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8"
        )
        rotating.setFormatter(formatter)
        root.addHandler(rotating)

    if buffer is not None:
        buffer_handler = BufferHandler(buffer)
        buffer_handler.setFormatter(formatter)
        root.addHandler(buffer_handler)

    # ccxt 는 DEBUG 에서 요청 본문을 통째로 찍는다 — 키가 로그에 남을 수 있어 올린다.
    logging.getLogger("ccxt").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)
