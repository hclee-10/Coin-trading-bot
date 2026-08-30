"""웹 대시보드 인증.

이 서버는 실제 자금을 움직이는 봇을 제어한다. 인터넷에 노출되면 URL 을 아는
누구나 두드릴 수 있으므로, 다음을 전제로 만든다:

* **기본 비밀번호는 없다.** `WEB_PASSWORD_HASH` 가 없으면 서버가 뜨지 않는다.
* 비밀번호는 scrypt 로 해시해 저장하고, 원문은 어디에도 남기지 않는다.
* 세션 토큰은 서버 메모리에만 있다 — 프로세스를 재시작하면 전부 무효가 되고,
  로그아웃이 즉시 반영된다(JWT 와 달리 폐기가 확실하다).
* 로그인 실패는 IP 단위로 세어 잠근다.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import threading
import time
from dataclasses import dataclass

# scrypt 파라미터. n 을 올리면 무차별 대입이 비싸지지만 로그인도 느려진다.
# n=2^15 는 이 하드웨어에서 약 0.1초 — 사람이 못 느끼고 공격자에겐 충분히 비싸다.
_SCRYPT_N = 2**15
_SCRYPT_R = 8
_SCRYPT_P = 1
_SCRYPT_DKLEN = 32
_SCRYPT_MAXMEM = 128 * _SCRYPT_N * _SCRYPT_R * _SCRYPT_P * 2
_SALT_BYTES = 16

PASSWORD_ENV = "WEB_PASSWORD_HASH"


class AuthError(Exception):
    """인증 설정이 없거나 잘못되었을 때."""


def hash_password(password: str) -> str:
    """`scrypt$n$r$p$salt$hash` 형태의 저장용 문자열을 만든다."""
    if len(password) < 12:
        raise AuthError("비밀번호는 12자 이상이어야 합니다 (인터넷에 노출되는 서버입니다)")
    salt = secrets.token_bytes(_SALT_BYTES)
    digest = hashlib.scrypt(
        password.encode("utf-8"), salt=salt, n=_SCRYPT_N, r=_SCRYPT_R,
        p=_SCRYPT_P, dklen=_SCRYPT_DKLEN, maxmem=_SCRYPT_MAXMEM,
    )
    return f"scrypt${_SCRYPT_N}${_SCRYPT_R}${_SCRYPT_P}${salt.hex()}${digest.hex()}"


def verify_password(password: str, encoded: str) -> bool:
    """저장된 해시와 대조한다. 형식이 깨져 있으면 조용히 False."""
    try:
        scheme, n, r, p, salt_hex, hash_hex = encoded.split("$")
        if scheme != "scrypt":
            return False
        digest = hashlib.scrypt(
            password.encode("utf-8"),
            salt=bytes.fromhex(salt_hex),
            n=int(n), r=int(r), p=int(p),
            dklen=len(bytes.fromhex(hash_hex)),
            maxmem=128 * int(n) * int(r) * int(p) * 2,
        )
    except (ValueError, TypeError):
        return False
    # 타이밍 공격을 막기 위해 상수 시간 비교를 쓴다.
    return hmac.compare_digest(digest.hex(), hash_hex)


def load_password_hash() -> str:
    """환경변수에서 비밀번호 해시를 읽는다. 없으면 서버를 띄우지 않는다."""
    encoded = os.getenv(PASSWORD_ENV, "").strip()
    if not encoded:
        raise AuthError(
            f"{PASSWORD_ENV} 가 설정되지 않았습니다. "
            "`python -m bot hash-password` 로 해시를 만들어 .env 에 넣으세요."
        )
    if not encoded.startswith("scrypt$"):
        raise AuthError(
            f"{PASSWORD_ENV} 형식이 올바르지 않습니다. "
            "비밀번호 원문이 아니라 `python -m bot hash-password` 의 출력이어야 합니다."
        )
    return encoded


@dataclass(frozen=True)
class Session:
    token: str
    expires_at: float


class TokenStore:
    """만료되는 세션 토큰을 메모리에 보관한다."""

    def __init__(self, ttl_seconds: float = 12 * 3600, max_sessions: int = 20) -> None:
        self.ttl = ttl_seconds
        self.max_sessions = max_sessions
        self._sessions: dict[str, float] = {}
        self._lock = threading.Lock()

    def create(self, *, now: float | None = None) -> Session:
        now = now if now is not None else time.time()
        token = secrets.token_urlsafe(32)
        with self._lock:
            self._purge(now)
            # 토큰이 무한정 쌓이지 않게 가장 오래된 것부터 밀어낸다.
            while len(self._sessions) >= self.max_sessions:
                oldest = min(self._sessions, key=self._sessions.__getitem__)
                del self._sessions[oldest]
            expires_at = now + self.ttl
            self._sessions[token] = expires_at
        return Session(token=token, expires_at=expires_at)

    def validate(self, token: str | None, *, now: float | None = None) -> bool:
        if not token:
            return False
        now = now if now is not None else time.time()
        with self._lock:
            self._purge(now)
            return token in self._sessions

    def revoke(self, token: str | None) -> None:
        if not token:
            return
        with self._lock:
            self._sessions.pop(token, None)

    def revoke_all(self) -> None:
        with self._lock:
            self._sessions.clear()

    @property
    def active_count(self) -> int:
        with self._lock:
            self._purge(time.time())
            return len(self._sessions)

    def _purge(self, now: float) -> None:
        """만료된 토큰 제거. 호출자가 락을 잡고 있어야 한다."""
        for token in [t for t, exp in self._sessions.items() if exp <= now]:
            del self._sessions[token]


class LoginThrottle:
    """IP 단위 로그인 실패 제한.

    비밀번호 하나로 계좌 제어권이 열리므로 무차별 대입을 반드시 막아야 한다.
    """

    def __init__(self, max_attempts: int = 5, lockout_seconds: float = 300.0) -> None:
        self.max_attempts = max_attempts
        self.lockout_seconds = lockout_seconds
        self._failures: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    def locked_for(self, ip: str, *, now: float | None = None) -> float:
        """남은 잠금 시간(초). 0 이면 시도할 수 있다."""
        now = now if now is not None else time.time()
        with self._lock:
            recent = self._recent(ip, now)
            if len(recent) < self.max_attempts:
                return 0.0
            return max(0.0, recent[-1] + self.lockout_seconds - now)

    def record_failure(self, ip: str, *, now: float | None = None) -> None:
        now = now if now is not None else time.time()
        with self._lock:
            recent = self._recent(ip, now)
            recent.append(now)
            self._failures[ip] = recent

    def reset(self, ip: str) -> None:
        with self._lock:
            self._failures.pop(ip, None)

    def _recent(self, ip: str, now: float) -> list[float]:
        """잠금 창 안의 실패만 남긴다. 호출자가 락을 잡고 있어야 한다."""
        return [t for t in self._failures.get(ip, []) if now - t < self.lockout_seconds]
