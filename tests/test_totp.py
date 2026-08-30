"""TOTP 2단계 인증 — RFC 6238 규격과 재사용 방지."""

import base64
import time

import pytest

from bot.web import totp

# RFC 6238 Appendix B 의 공식 테스트 벡터 (SHA1, 6자리로 절단)
RFC_SEED = base64.b32encode(b"12345678901234567890").decode()
RFC_VECTORS = [
    (59, "287082"),
    (1111111109, "081804"),
    (1111111111, "050471"),
    (1234567890, "005924"),
    (2000000000, "279037"),
    (20000000000, "353130"),
]


@pytest.mark.parametrize("at,expected", RFC_VECTORS)
def test_matches_rfc6238_vectors(at, expected):
    """규격을 직접 구현했으므로 공식 벡터로 못을 박아 둔다."""
    assert totp.generate_code(RFC_SEED, at=at) == expected


def test_generated_secret_is_usable():
    secret = totp.generate_secret()

    assert totp.is_valid_secret(secret)
    assert totp.verify_code(secret, totp.generate_code(secret)) is not None


def test_wrong_code_is_rejected():
    secret = totp.generate_secret()
    correct = totp.generate_code(secret)
    wrong = "000000" if correct != "000000" else "111111"

    assert totp.verify_code(secret, wrong) is None


@pytest.mark.parametrize("code", ["", "12345", "1234567", "abcdef", "12 34 56 78"])
def test_malformed_codes_are_rejected(code):
    assert totp.verify_code(totp.generate_secret(), code) is None


def test_clock_drift_within_the_window_is_tolerated():
    """폰과 서버 시계가 조금 어긋나도 로그인은 되어야 한다."""
    secret = totp.generate_secret()
    now = 1_700_000_000.0

    previous = totp.generate_code(secret, at=now - totp.PERIOD)
    following = totp.generate_code(secret, at=now + totp.PERIOD)

    assert totp.verify_code(secret, previous, at=now) is not None
    assert totp.verify_code(secret, following, at=now) is not None


def test_codes_outside_the_window_are_rejected():
    secret = totp.generate_secret()
    now = 1_700_000_000.0
    stale = totp.generate_code(secret, at=now - totp.PERIOD * 5)

    assert totp.verify_code(secret, stale, at=now) is None


def test_secret_from_an_app_with_spaces_and_lowercase_works():
    """인증 앱은 키를 네 글자씩 끊어 보여 준다 — 그대로 붙여넣어도 되어야 한다."""
    secret = totp.generate_secret()
    pretty = " ".join(secret[i : i + 4] for i in range(0, len(secret), 4)).lower()

    assert totp.is_valid_secret(pretty)
    assert totp.generate_code(pretty, at=1000) == totp.generate_code(secret, at=1000)


@pytest.mark.parametrize("secret", ["", "   ", "not-base32!", "1"])
def test_broken_secrets_are_rejected(secret):
    assert totp.is_valid_secret(secret) is False


def test_provisioning_uri_carries_the_secret_and_parameters():
    secret = totp.generate_secret()

    uri = totp.provisioning_uri(secret, account="uta24")

    assert uri.startswith("otpauth://totp/")
    assert f"secret={secret}" in uri
    assert "digits=6" in uri and "period=30" in uri


# --- 코드 재사용 방지 -----------------------------------------------------
def test_a_code_cannot_be_used_twice():
    """코드는 30초간 유효하다 — 새어 나간 코드가 그대로 다시 통하면 안 된다."""
    tracker = totp.UsedCodeTracker()
    now = time.time()
    counter = int(now // totp.PERIOD)

    assert tracker.claim(counter, now=now) is True
    assert tracker.claim(counter, now=now) is False


def test_a_different_period_is_accepted():
    tracker = totp.UsedCodeTracker()
    now = time.time()
    counter = int(now // totp.PERIOD)

    tracker.claim(counter, now=now)

    assert tracker.claim(counter + 1, now=now + totp.PERIOD) is True


def test_old_entries_are_forgotten():
    """무한정 쌓이면 메모리를 먹는다."""
    tracker = totp.UsedCodeTracker(retain_periods=2)
    start = 1_700_000_000.0
    tracker.claim(int(start // totp.PERIOD), now=start)

    later = start + totp.PERIOD * 10
    tracker.claim(int(later // totp.PERIOD), now=later)

    assert len(tracker._used) == 1
