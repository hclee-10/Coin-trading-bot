"""웹 대시보드 인증.

이 서버는 실제 자금을 움직이는 봇을 제어한다. 인터넷에 노출되면 URL 을 아는
누구나 두드릴 수 있으므로, 다음을 전제로 만든다:

* **기본 계정은 없다.** `WEB_USERNAME` 과 `WEB_PASSWORD_HASH` 가 모두 없으면
  서버가 뜨지 않는다.
* 비밀번호는 scrypt 로 해시해 저장하고, 원문은 어디에도 남기지 않는다.
* 세션 토큰은 서버 메모리에만 있다 — 프로세스를 재시작하면 전부 무효가 되고,
  로그아웃이 즉시 반영된다(JWT 와 달리 폐기가 확실하다).
* 로그인 실패는 IP 단위로 세어 잠근다.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets
import struct
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

# 해시는 사람이 웹 폼으로 손으로 옮겨야 하는 값이다. 그래서 구분자 없는 단일
# base64url 토큰으로 인코딩한다:
#
#   * 셸을 거쳐도 안전하다 — 예전 `scrypt$32768$8$1$...` 형식은 `$32768` 이
#     변수로 확장돼 중간이 통째로 날아갔고, 그래도 `scrypt$` 로는 시작해서
#     한동안 "비밀번호가 틀렸다" 로만 보였다.
#   * 더블클릭으로 한 번에 선택된다 — `$` 는 단어 경계라 일부만 복사되기 쉽다.
#
# 예전 형식으로 만들어 둔 값도 계속 검증한다.
_TOKEN_VERSION = 1
_LEGACY_PREFIX = "scrypt$"
# 토큰 끝에 붙는 체크섬. 값이 한 글자라도 깨지거나 잘리면 걸러진다 — base64 는
# 앞부분만 잘라도 그럴듯하게 디코딩되기 때문에 길이 검사만으로는 부족하다.
_CHECKSUM_BYTES = 4

# 비밀번호 길이 하한. 짧은 비밀번호는 막지 않되, 권장 길이에 못 미치면 생성
# 단계에서 경고한다 — 인터넷에 공개된 주소에서 계좌 제어권을 지키는 값이고,
# 로그인 시도 제한은 IP 단위라 프록시를 돌리는 공격자에게는 잘 듣지 않는다.
MIN_PASSWORD_LENGTH = 4
RECOMMENDED_PASSWORD_LENGTH = 12

USERNAME_ENV = "WEB_USERNAME"
PASSWORD_ENV = "WEB_PASSWORD_HASH"
PLAIN_PASSWORD_ENV = "WEB_PASSWORD"


class AuthError(Exception):
    """인증 설정이 없거나 잘못되었을 때."""


def hash_password(password: str) -> str:
    """저장용 해시 토큰을 만든다. 구분자 없는 단일 base64url 문자열."""
    if len(password) < MIN_PASSWORD_LENGTH:
        raise AuthError(f"비밀번호는 {MIN_PASSWORD_LENGTH}자 이상이어야 합니다")
    salt = secrets.token_bytes(_SALT_BYTES)
    digest = hashlib.scrypt(
        password.encode("utf-8"), salt=salt, n=_SCRYPT_N, r=_SCRYPT_R,
        p=_SCRYPT_P, dklen=_SCRYPT_DKLEN, maxmem=_SCRYPT_MAXMEM,
    )
    body = (
        struct.pack(">BIHHB", _TOKEN_VERSION, _SCRYPT_N, _SCRYPT_R, _SCRYPT_P, len(salt))
        + salt
        + digest
    )
    payload = body + _checksum(body)
    return base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")


def _checksum(body: bytes) -> bytes:
    return hashlib.sha256(body).digest()[:_CHECKSUM_BYTES]


def _decode_token(encoded: str) -> tuple[int, int, int, bytes, bytes] | None:
    """토큰에서 (n, r, p, salt, digest) 를 꺼낸다. 깨져 있으면 None."""
    try:
        payload = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
    except ValueError:
        return None
    if len(payload) <= _CHECKSUM_BYTES:
        return None

    body, checksum = payload[:-_CHECKSUM_BYTES], payload[-_CHECKSUM_BYTES:]
    # 체크섬이 맞지 않으면 값이 잘렸거나 깨진 것이다.
    if not hmac.compare_digest(_checksum(body), checksum):
        return None

    try:
        version, n, r, p, salt_len = struct.unpack(">BIHHB", body[:10])
    except struct.error:
        return None
    if version != _TOKEN_VERSION or salt_len == 0 or min(n, r, p) <= 0:
        return None
    salt, digest = body[10 : 10 + salt_len], body[10 + salt_len :]
    if len(salt) != salt_len or not digest:
        return None
    return n, r, p, salt, digest


def _decode_legacy(encoded: str) -> tuple[int, int, int, bytes, bytes] | None:
    """예전 `scrypt$n$r$p$salt$hash` 형식으로 만들어 둔 값도 계속 받아 준다."""
    parts = encoded.split("$")
    if len(parts) != 6 or parts[0] != "scrypt":
        return None
    try:
        n, r, p = (int(v) for v in parts[1:4])
        salt, digest = bytes.fromhex(parts[4]), bytes.fromhex(parts[5])
    except ValueError:
        return None
    if min(n, r, p) <= 0 or not salt or not digest:
        return None
    return n, r, p, salt, digest


def _decode(encoded: str) -> tuple[int, int, int, bytes, bytes] | None:
    encoded = encoded.strip()
    if not encoded:
        return None
    if encoded.startswith(_LEGACY_PREFIX):
        return _decode_legacy(encoded)
    return _decode_token(encoded)


def is_valid_password_hash(encoded: str) -> bool:
    """저장된 해시가 구조적으로 온전한지 확인한다.

    형식을 어림짐작하면 안 된다. 값이 중간에 잘리거나 깨져도 그럴듯해 보이면
    통과해 버리고, 그러면 사용자는 "비밀번호가 틀렸다"는 메시지만 보고 원인을
    영영 못 찾는다.
    """
    return _decode(encoded) is not None


def verify_password(password: str, encoded: str) -> bool:
    """저장된 해시와 대조한다. 형식이 깨져 있으면 조용히 False."""
    decoded = _decode(encoded)
    if decoded is None:
        return False
    n, r, p, salt, expected = decoded
    try:
        digest = hashlib.scrypt(
            password.encode("utf-8"), salt=salt, n=n, r=r, p=p,
            dklen=len(expected), maxmem=128 * n * r * p * 2,
        )
    except ValueError:
        return False
    # 타이밍 공격을 막기 위해 상수 시간 비교를 쓴다.
    return hmac.compare_digest(digest, expected)


@dataclass(frozen=True)
class Account:
    """대시보드에 로그인할 수 있는 단일 계정."""

    username: str
    password_hash: str

    def verify(self, username: str, password: str) -> bool:
        """아이디와 비밀번호를 함께 확인한다.

        아이디가 틀려도 비밀번호 해시 계산을 건너뛰지 않는다. 건너뛰면 응답
        시간 차이로 "이 아이디는 존재한다"가 새어 나간다.
        """
        username_ok = hmac.compare_digest(username, self.username)
        password_ok = verify_password(password, self.password_hash)
        return username_ok and password_ok


def load_account() -> Account:
    """환경변수에서 계정을 읽는다.

    비밀번호는 두 가지 방식 중 하나로 준다:

    * `WEB_PASSWORD` — 비밀번호를 그대로 넣는다. 서버가 기동할 때 해시로 바꾼다.
      설정이 간단한 대신, 배포 환경의 변수를 볼 수 있는 사람은 비밀번호를 그대로
      보게 된다. (다만 그 사람은 바로 옆의 거래소 API 키도 이미 볼 수 있다.)
    * `WEB_PASSWORD_HASH` — `hash-password` 로 미리 만든 해시를 넣는다.
      비밀번호가 어디에도 평문으로 남지 않는다. 다른 곳에서도 쓰는 비밀번호라면
      이쪽을 쓴다.

    둘 다 있으면 해시가 우선한다.
    """
    username = os.getenv(USERNAME_ENV, "").strip()
    encoded = os.getenv(PASSWORD_ENV, "").strip()
    # 붙여넣기에 섞여 들어온 줄바꿈·공백 때문에 로그인이 막히는 일이 잦다.
    plain = os.getenv(PLAIN_PASSWORD_ENV, "").strip()

    if not username:
        raise AuthError(
            f"{USERNAME_ENV} 가 설정되지 않았습니다. 대시보드에 쓸 아이디를 넣으세요."
        )

    if encoded:
        if not is_valid_password_hash(encoded):
            raise AuthError(
                f"{PASSWORD_ENV} 값이 깨졌거나 형식이 올바르지 않습니다. "
                "`python -m bot hash-password` 가 출력한 한 줄을 처음부터 끝까지 "
                f"그대로 붙여넣으세요. 더 간단하게 하려면 {PASSWORD_ENV} 를 지우고 "
                f"{PLAIN_PASSWORD_ENV} 에 비밀번호를 그대로 넣어도 됩니다."
            )
        return Account(username=username, password_hash=encoded)

    if plain:
        # 기동 시 한 번 해시한다. 메모리에도 평문을 들고 있지 않게 된다.
        return Account(username=username, password_hash=hash_password(plain))

    raise AuthError(
        f"비밀번호가 설정되지 않았습니다. {PLAIN_PASSWORD_ENV} 에 비밀번호를 그대로 "
        f"넣거나, `python -m bot hash-password` 로 만든 값을 {PASSWORD_ENV} 에 넣으세요."
    )


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
