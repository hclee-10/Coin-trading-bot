import pytest
from fastapi.testclient import TestClient

from bot.logging_utils import LogBuffer
from bot.models import Position, PositionSide
from bot.web.app import create_app
from bot.web.auth import Account, LoginThrottle, TokenStore, hash_password
from bot.web.supervisor import BotSupervisor
from tests.fakes import FakeExchange
from tests.test_supervisor import SYMBOL, make_config, wait_for

USERNAME = "trader"
PASSWORD = "dashboard-password-1"


@pytest.fixture
def env():
    exchange = FakeExchange(price=100.0, equity=1_000.0)
    config = make_config()
    supervisor = BotSupervisor(
        config, exchange_factory=lambda: exchange, join_timeout=5.0
    )
    buffer = LogBuffer(capacity=50)
    app = create_app(
        config,
        supervisor,
        buffer,
        Account(username=USERNAME, password_hash=hash_password(PASSWORD)),
        token_store=TokenStore(ttl_seconds=60),
        throttle=LoginThrottle(max_attempts=3, lockout_seconds=60),
    )
    client = TestClient(app)
    yield client, supervisor, exchange, buffer
    supervisor.stop()


def login(client) -> dict:
    response = client.post(
        "/api/login", json={"username": USERNAME, "password": PASSWORD}
    )
    assert response.status_code == 200
    return {"Authorization": f"Bearer {response.json()['token']}"}


# --- 인증 ----------------------------------------------------------------
def test_health_needs_no_auth_and_leaks_nothing(env):
    client, *_ = env
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"ok": True}


@pytest.mark.parametrize(
    "method,path",
    [
        ("get", "/api/status"),
        ("get", "/api/config"),
        ("get", "/api/positions"),
        ("get", "/api/logs"),
        ("post", "/api/bot/start"),
        ("post", "/api/bot/stop"),
        ("post", "/api/positions/close-all"),
    ],
)
def test_every_control_endpoint_requires_auth(env, method, path):
    client, *_ = env
    kwargs = {"json": {}} if method == "post" else {}
    response = getattr(client, method)(path, **kwargs)
    assert response.status_code == 401


def test_wrong_password_is_rejected(env):
    client, *_ = env
    response = client.post(
        "/api/login", json={"username": USERNAME, "password": "nope"}
    )
    assert response.status_code == 401


def test_wrong_username_is_rejected(env):
    client, *_ = env
    response = client.post(
        "/api/login", json={"username": "someone-else", "password": PASSWORD}
    )
    assert response.status_code == 401


def test_error_message_does_not_reveal_which_field_was_wrong(env):
    """아이디가 존재하는지 알려 주면 공격자가 절반을 확정하고 시작한다."""
    client, *_ = env
    bad_user = client.post(
        "/api/login", json={"username": "nobody", "password": PASSWORD}
    ).json()["detail"]
    bad_pass = client.post(
        "/api/login", json={"username": USERNAME, "password": "nope"}
    ).json()["detail"]
    assert bad_user == bad_pass


def test_brute_force_is_locked_out(env):
    client, *_ = env
    bad = {"username": USERNAME, "password": "nope"}
    for _ in range(3):
        assert client.post("/api/login", json=bad).status_code == 401
    assert client.post("/api/login", json=bad).status_code == 429
    # 잠긴 동안에는 올바른 자격증명도 막힌다
    good = {"username": USERNAME, "password": PASSWORD}
    assert client.post("/api/login", json=good).status_code == 429


def test_logout_revokes_the_token(env):
    client, *_ = env
    headers = login(client)
    assert client.get("/api/status", headers=headers).status_code == 200
    assert client.post("/api/logout", headers=headers).status_code == 200
    assert client.get("/api/status", headers=headers).status_code == 401


def test_bogus_token_is_rejected(env):
    client, *_ = env
    headers = {"Authorization": "Bearer not-a-real-token"}
    assert client.get("/api/status", headers=headers).status_code == 401


# --- 시크릿 노출 ----------------------------------------------------------
def test_config_endpoint_exposes_no_secrets(env):
    client, *_ = env
    body = client.get("/api/config", headers=login(client)).json()
    serialized = str(body).lower()
    for leaked in ("api_key", "secret", "passphrase", "password", "scrypt"):
        assert leaked not in serialized, f"'{leaked}' 가 응답에 노출되었습니다"
    assert body["exchange"]["id"] == "okx"
    assert body["risk"]["max_daily_loss_pct"] == 3.0


def test_security_headers_are_set(env):
    client, *_ = env
    headers = client.get("/healthz").headers
    assert headers["X-Frame-Options"] == "DENY"
    assert headers["X-Content-Type-Options"] == "nosniff"
    assert "frame-ancestors 'none'" in headers["Content-Security-Policy"]


# --- 봇 제어 --------------------------------------------------------------
def test_start_dry_run_then_stop(env):
    client, supervisor, _, _ = env
    headers = login(client)

    body = client.post("/api/bot/start", json={"live": False}, headers=headers).json()
    assert body["running"] is True
    assert body["live"] is False

    assert wait_for(lambda: supervisor.snapshot().last_cycle_at is not None)
    status_body = client.get("/api/status", headers=headers).json()
    assert status_body["equity"] == 1_000.0

    body = client.post("/api/bot/stop", headers=headers).json()
    assert body["running"] is False


def test_live_start_without_confirmation_is_refused(env):
    client, supervisor, exchange, _ = env
    headers = login(client)

    response = client.post("/api/bot/start", json={"live": True}, headers=headers)

    assert response.status_code == 400
    assert "LIVE" in response.json()["detail"]
    assert not supervisor.running, "확인 없이 실거래가 시작되면 안 됩니다"


def test_live_start_with_confirmation_is_accepted(env):
    client, supervisor, _, _ = env
    headers = login(client)

    body = client.post(
        "/api/bot/start", json={"live": True, "confirm": "LIVE"}, headers=headers
    ).json()

    assert body["live"] is True
    supervisor.stop()


def test_starting_twice_returns_conflict(env):
    client, *_ = env
    headers = login(client)
    client.post("/api/bot/start", json={"live": False}, headers=headers)
    response = client.post("/api/bot/start", json={"live": False}, headers=headers)
    assert response.status_code == 409


# --- 긴급 청산 ------------------------------------------------------------
def test_close_all_without_confirmation_is_refused(env):
    client, _, exchange, _ = env
    exchange.positions[SYMBOL] = Position(
        symbol=SYMBOL, side=PositionSide.LONG, contracts=1.0, entry_price=100.0
    )
    response = client.post("/api/positions/close-all", json={}, headers=login(client))
    assert response.status_code == 400
    assert exchange.sent_orders == [], "확인 없이 주문이 나가면 안 됩니다"


def test_close_all_with_confirmation_closes_and_stops_the_bot(env):
    client, supervisor, exchange, _ = env
    exchange.positions[SYMBOL] = Position(
        symbol=SYMBOL, side=PositionSide.LONG, contracts=2.0, entry_price=100.0
    )
    headers = login(client)
    client.post("/api/bot/start", json={"live": False}, headers=headers)
    assert wait_for(lambda: supervisor.snapshot().last_cycle_at is not None)

    body = client.post(
        "/api/positions/close-all", json={"confirm": "CLOSE"}, headers=headers
    ).json()

    assert any(SYMBOL in m for m in body["messages"])
    assert body["status"]["running"] is False
    (order,) = exchange.sent_orders
    assert order.reduce_only and order.amount == 2.0


# --- 조회 -----------------------------------------------------------------
def test_positions_come_from_exchange_when_stopped(env):
    client, _, exchange, _ = env
    exchange.positions[SYMBOL] = Position(
        symbol=SYMBOL, side=PositionSide.SHORT, contracts=1.0, entry_price=100.0
    )
    body = client.get("/api/positions", headers=login(client)).json()
    assert body["source"] == "exchange"
    assert body["positions"][0]["side"] == "short"


def test_positions_come_from_last_cycle_while_running(env):
    client, supervisor, exchange, _ = env
    exchange.positions[SYMBOL] = Position(
        symbol=SYMBOL, side=PositionSide.LONG, contracts=1.0, entry_price=100.0
    )
    headers = login(client)
    client.post("/api/bot/start", json={"live": False}, headers=headers)
    assert wait_for(lambda: supervisor.snapshot().positions)

    body = client.get("/api/positions", headers=headers).json()

    assert body["source"] == "last_cycle"
    assert body["positions"][0]["side"] == "long"


def test_logs_are_returned_incrementally(env):
    client, _, _, buffer = env
    headers = login(client)
    for i in range(5):
        buffer.append("INFO", "test", f"줄 {i}")

    first = client.get("/api/logs", headers=headers).json()
    assert len(first["entries"]) == 5

    buffer.append("WARNING", "test", "새 줄")
    second = client.get(f"/api/logs?since={first['latest_seq']}", headers=headers).json()

    assert [e["message"] for e in second["entries"]] == ["새 줄"]
    assert second["entries"][0]["level"] == "WARNING"


def test_frontend_missing_returns_a_helpful_message(env):
    client, *_ = env
    response = client.get("/")
    assert response.status_code == 503
    assert "npm run build" in response.json()["detail"]


# --- 프록시 뒤 클라이언트 IP -------------------------------------------
def make_client(*, trust_proxy: bool, max_attempts: int = 2):
    """로그인 시도 제한만 확인하는 최소 구성의 앱."""
    exchange = FakeExchange()
    config = make_config()
    supervisor = BotSupervisor(config, exchange_factory=lambda: exchange)
    app = create_app(
        config,
        supervisor,
        LogBuffer(capacity=10),
        Account(username=USERNAME, password_hash=hash_password(PASSWORD)),
        token_store=TokenStore(ttl_seconds=60),
        throttle=LoginThrottle(max_attempts=max_attempts, lockout_seconds=60),
        trust_proxy=trust_proxy,
    )
    return TestClient(app)


def attempt(client, xff: str | None):
    headers = {"X-Forwarded-For": xff} if xff else {}
    return client.post(
        "/api/login", json={"username": USERNAME, "password": "nope"}, headers=headers
    )


def test_proxy_header_is_ignored_when_not_trusted():
    """프록시 뒤가 아니면 헤더를 믿지 않는다 — 아니면 누구나 IP 를 위장한다."""
    client = make_client(trust_proxy=False, max_attempts=2)

    assert attempt(client, "1.1.1.1").status_code == 401
    assert attempt(client, "2.2.2.2").status_code == 401
    # IP 를 바꿔 보냈어도 같은 버킷으로 세어 잠겨야 한다
    assert attempt(client, "3.3.3.3").status_code == 429


def test_throttle_buckets_by_real_client_behind_a_proxy():
    """서로 다른 접속자가 남의 실패 때문에 잠기면 안 된다."""
    client = make_client(trust_proxy=True, max_attempts=2)

    assert attempt(client, "1.1.1.1").status_code == 401
    assert attempt(client, "1.1.1.1").status_code == 401
    assert attempt(client, "1.1.1.1").status_code == 429  # 이 사람만 잠김

    assert attempt(client, "9.9.9.9").status_code == 401  # 다른 사람은 멀쩡


def test_spoofed_left_entry_cannot_bypass_the_throttle():
    """X-Forwarded-For 왼쪽은 클라이언트가 위조할 수 있다.

    프록시는 위조된 값 뒤에 진짜 IP 를 덧붙이므로, 왼쪽에서 읽으면 공격자가
    매 시도마다 다른 IP 를 넣어 시도 제한을 통째로 우회한다.
    """
    client = make_client(trust_proxy=True, max_attempts=2)

    # 공격자(5.5.5.5)가 매번 다른 값을 위조해 앞에 붙인다
    assert attempt(client, "111.111.111.111, 5.5.5.5").status_code == 401
    assert attempt(client, "222.222.222.222, 5.5.5.5").status_code == 401
    assert attempt(client, "333.333.333.333, 5.5.5.5").status_code == 429


def test_missing_header_behind_a_proxy_falls_back_to_the_peer():
    client = make_client(trust_proxy=True, max_attempts=2)

    assert attempt(client, None).status_code == 401
    assert attempt(client, None).status_code == 401
    assert attempt(client, None).status_code == 429


# --- 기동 단계 오류 -------------------------------------------------------
@pytest.fixture
def broken_env():
    """설정이나 자격증명이 잘못된 채로 뜬 서버."""
    exchange = FakeExchange()
    config = make_config()
    supervisor = BotSupervisor(config, exchange_factory=lambda: exchange)
    app = create_app(
        config,
        supervisor,
        LogBuffer(capacity=10),
        Account(username=USERNAME, password_hash=hash_password(PASSWORD)),
        token_store=TokenStore(ttl_seconds=60),
        startup_error="거래소 자격증명 오류: BITGET_API_PASSPHRASE 누락",
    )
    return TestClient(app), supervisor, exchange


def test_server_still_serves_when_configuration_is_broken(broken_env):
    """설정이 깨졌다고 사이트가 죽으면 원인을 볼 방법이 없어진다."""
    client, _, _ = broken_env

    assert client.get("/healthz").status_code == 200
    body = client.get("/api/status", headers=login(client)).json()
    assert "BITGET_API_PASSPHRASE" in body["startup_error"]


def test_broken_configuration_blocks_starting_the_bot(broken_env):
    client, supervisor, exchange = broken_env
    headers = login(client)

    response = client.post("/api/bot/start", json={"live": False}, headers=headers)

    assert response.status_code == 409
    assert "BITGET_API_PASSPHRASE" in response.json()["detail"]
    assert not supervisor.running
    assert exchange.sent_orders == []


def test_live_start_is_blocked_too_even_with_the_confirmation(broken_env):
    client, supervisor, _ = broken_env

    response = client.post(
        "/api/bot/start", json={"live": True, "confirm": "LIVE"}, headers=login(client)
    )

    assert response.status_code == 409
    assert not supervisor.running


def test_healthy_server_reports_no_startup_error(env):
    client, *_ = env
    assert client.get("/api/status", headers=login(client)).json()["startup_error"] is None


# --- 계정 미설정 ----------------------------------------------------------
def test_server_serves_without_an_account_but_refuses_every_login():
    """계정이 없어도 사이트는 떠야 한다 — 로그인이 불가능하니 제어권은 안 열린다."""
    exchange = FakeExchange()
    config = make_config()
    app = create_app(
        config,
        BotSupervisor(config, exchange_factory=lambda: exchange),
        LogBuffer(capacity=10),
        None,  # 계정 미설정
        startup_error="로그인 계정 오류: WEB_PASSWORD_HASH 가 설정되지 않았습니다",
    )
    client = TestClient(app)

    assert client.get("/healthz").status_code == 200

    response = client.post(
        "/api/login", json={"username": "anyone", "password": "anything"}
    )
    assert response.status_code == 503
    # 어느 변수가 문제인지까지 알려 줘야 배포 로그를 뒤지지 않는다
    assert "WEB_PASSWORD_HASH" in response.json()["detail"]

    # 제어 엔드포인트는 여전히 잠겨 있다
    assert client.get("/api/status").status_code == 401
    assert client.post("/api/bot/start", json={"live": False}).status_code == 401
    assert exchange.sent_orders == []


# --- 해시 형식 검증 -------------------------------------------------------
@pytest.mark.parametrize(
    "label,encoded",
    [
        ("빈 값", ""),
        ("공백만", "   "),
        ("비밀번호 원문을 그대로 넣음", "my-plain-password"),
        ("예전 형식이 셸 확장으로 날아감", "scrypt$$$"),
        ("예전 형식이 잘림", "scrypt$32768$8$1$abcd"),
        ("예전 형식이 16진수가 아님", "scrypt$32768$8$1$zzzz$zzzz"),
        ("예전 형식 파라미터가 0", "scrypt$0$8$1$abcd$abcd"),
    ],
)
def test_broken_password_hashes_are_rejected(label, encoded):
    """깨진 값을 통과시키면 원인이 '비밀번호 오류'로 보여 영영 못 찾는다."""
    from bot.web.auth import is_valid_password_hash

    assert is_valid_password_hash(encoded) is False, label


def test_a_real_hash_is_accepted():
    from bot.web.auth import is_valid_password_hash

    assert is_valid_password_hash(hash_password("correct-horse-battery")) is True


# --- 토큰 형식 -----------------------------------------------------------
def test_token_has_no_characters_that_break_copying_or_shells():
    """웹 폼으로 손으로 옮기는 값이라 특수문자가 없어야 한다.

    예전 `scrypt$32768$8$1$...` 형식은 `$` 가 셸에서 변수로 확장돼 중간이
    날아갔고, 더블클릭 선택도 `$` 에서 끊겨 일부만 복사되기 쉬웠다.
    """
    token = hash_password("correct-horse-battery")

    assert all(c.isalnum() or c in "-_" for c in token)
    assert "$" not in token and " " not in token


def test_truncated_token_is_detected():
    """base64 는 앞부분만 잘라도 디코딩돼서, 길이 검사만으로는 못 잡는다."""
    from bot.web.auth import is_valid_password_hash

    token = hash_password("correct-horse-battery")

    assert is_valid_password_hash(token[:40]) is False
    assert is_valid_password_hash(token[:-1]) is False


def test_single_character_corruption_is_detected():
    from bot.web.auth import is_valid_password_hash

    token = hash_password("correct-horse-battery")
    swapped = "B" if token[10] != "B" else "C"
    corrupted = token[:10] + swapped + token[11:]

    assert is_valid_password_hash(corrupted) is False


def test_legacy_hashes_still_work():
    """예전 형식으로 만들어 둔 값이 갑자기 막히면 안 된다."""
    from bot.web.auth import is_valid_password_hash, verify_password

    legacy = (
        "scrypt$32768$8$1$20c9c23c35346afc92e9d891660619f5$"
        "3e26eed9fadda2743f0dd8baeb7737c3fd98561ab56c05f47de4af06504c3169"
    )

    assert is_valid_password_hash(legacy) is True
    assert verify_password("correct-horse-battery", legacy) is True
    assert verify_password("wrong", legacy) is False


def test_surrounding_whitespace_is_tolerated():
    """붙여넣기에 줄바꿈이 섞여도 계정이 못 쓰게 되면 안 된다."""
    from bot.web.auth import verify_password

    token = hash_password("correct-horse-battery")

    assert verify_password("correct-horse-battery", f"  {token}\n") is True


def test_broken_hash_env_fails_loudly_instead_of_looking_like_a_wrong_password(monkeypatch):
    from bot.web.auth import AuthError, load_account

    monkeypatch.setenv("WEB_USERNAME", "trader")
    monkeypatch.setenv("WEB_PASSWORD_HASH", "scrypt$$$")

    with pytest.raises(AuthError, match="WEB_PASSWORD_HASH"):
        load_account()


# --- 비밀번호 길이 정책 ---------------------------------------------------
def test_short_passwords_are_allowed():
    """길이 판단은 사용자 몫이다 — 막지 않고 경고만 한다."""
    from bot.web.auth import verify_password

    token = hash_password("1234")

    assert verify_password("1234", token) is True
    assert verify_password("1235", token) is False


def test_passwords_below_the_floor_are_rejected():
    from bot.web.auth import MIN_PASSWORD_LENGTH, AuthError, hash_password as make

    with pytest.raises(AuthError, match=str(MIN_PASSWORD_LENGTH)):
        make("a" * (MIN_PASSWORD_LENGTH - 1))


def test_short_password_still_produces_a_valid_token():
    """길이를 줄여도 토큰 형식과 체크섬은 그대로 지켜져야 한다."""
    from bot.web.auth import is_valid_password_hash

    token = hash_password("1234")

    assert is_valid_password_hash(token) is True
    assert is_valid_password_hash(token[:-1]) is False
