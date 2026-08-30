"""TOTP 2단계 인증 (RFC 6238).

외부 라이브러리를 쓰지 않는다. 알고리즘이 짧고 규격이 명확해서, 의존성을 하나
늘리는 것보다 표준 라이브러리로 구현하고 RFC 의 공식 테스트 벡터로 검증하는
편이 낫다.

Google Authenticator, Authy, 1Password 등 표준 TOTP 앱과 호환된다.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import struct
import threading
import time
from urllib.parse import quote

DIGITS = 6
PERIOD = 30          # 코드 유효 시간(초)
# 폰과 서버의 시계가 조금 어긋나도 통과시킨다. ±1 이면 앞뒤 30초.
DEFAULT_WINDOW = 1
SECRET_BYTES = 20    # RFC 4226 권장 (160비트)

TOTP_SECRET_ENV = "WEB_TOTP_SECRET"


def generate_secret() -> str:
    """새 TOTP 비밀키를 base32 로 만든다. 인증 앱에 등록할 값이다."""
    return base64.b32encode(secrets.token_bytes(SECRET_BYTES)).decode("ascii").rstrip("=")


def normalize_secret(secret: str) -> str:
    """사람이 옮겨 적은 비밀키를 정규화한다.

    인증 앱은 보기 좋으라고 네 글자씩 끊어 보여 주고, 대소문자도 섞인다.
    그대로 받으면 정상적인 키가 거부된다.
    """
    return secret.replace(" ", "").replace("-", "").upper()


def is_valid_secret(secret: str) -> bool:
    return _decode_secret(secret) is not None


def _decode_secret(secret: str) -> bytes | None:
    cleaned = normalize_secret(secret or "")
    if not cleaned:
        return None
    try:
        padded = cleaned + "=" * (-len(cleaned) % 8)
        raw = base64.b32decode(padded, casefold=True)
    except (ValueError, TypeError):
        return None
    return raw or None


def generate_code(secret: str, *, counter: int | None = None, at: float | None = None) -> str:
    """지정한 시각의 6자리 코드. counter 를 주면 그 시간 구간을 직접 지정한다."""
    key = _decode_secret(secret)
    if key is None:
        raise ValueError("TOTP 비밀키 형식이 올바르지 않습니다")
    if counter is None:
        counter = int((at if at is not None else time.time()) // PERIOD)

    digest = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    # RFC 4226 동적 절단(dynamic truncation)
    offset = digest[-1] & 0x0F
    code = struct.unpack(">I", digest[offset : offset + 4])[0] & 0x7FFFFFFF
    return str(code % (10**DIGITS)).zfill(DIGITS)


def verify_code(
    secret: str,
    code: str,
    *,
    at: float | None = None,
    window: int = DEFAULT_WINDOW,
) -> int | None:
    """코드가 맞으면 사용된 시간 구간(counter)을, 틀리면 None 을 반환한다.

    counter 를 돌려주는 이유는 호출자가 **같은 코드의 재사용을 막기** 위해서다.
    코드는 30초간 유효하므로, 그 사이에 코드가 새면 그대로 재사용될 수 있다.
    """
    cleaned = (code or "").strip().replace(" ", "").replace("-", "")
    if not cleaned.isdigit() or len(cleaned) != DIGITS:
        return None
    if _decode_secret(secret) is None:
        return None

    now = at if at is not None else time.time()
    current = int(now // PERIOD)
    for drift in range(-window, window + 1):
        candidate = current + drift
        # 상수 시간 비교로 타이밍 정보를 흘리지 않는다.
        if hmac.compare_digest(generate_code(secret, counter=candidate), cleaned):
            return candidate
    return None


def provisioning_uri(secret: str, *, account: str, issuer: str = "Coin Trading Bot") -> str:
    """인증 앱이 읽는 otpauth:// URI. QR 코드로 만들어 스캔하면 된다."""
    label = quote(f"{issuer}:{account}", safe="")
    return (
        f"otpauth://totp/{label}"
        f"?secret={normalize_secret(secret)}"
        f"&issuer={quote(issuer, safe='')}"
        f"&algorithm=SHA1&digits={DIGITS}&period={PERIOD}"
    )


class UsedCodeTracker:
    """이미 쓴 코드를 기억해 재사용을 막는다.

    코드는 30초간 유효하다. 어깨너머로 보였거나 로그에 남은 코드가 그 사이
    그대로 다시 통하면 2단계 인증의 의미가 반감된다.
    """

    def __init__(self, retain_periods: int = 4) -> None:
        self._used: dict[int, float] = {}
        self._retain = retain_periods
        self._lock = threading.Lock()

    def claim(self, counter: int, *, now: float | None = None) -> bool:
        """이 구간을 처음 쓰는 것이면 True. 이미 썼으면 False."""
        now = now if now is not None else time.time()
        current = int(now // PERIOD)
        with self._lock:
            for old in [c for c in self._used if c < current - self._retain]:
                del self._used[old]
            if counter in self._used:
                return False
            self._used[counter] = now
            return True
