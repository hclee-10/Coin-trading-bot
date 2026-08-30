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


# --- 평문 비밀번호 방식 ---------------------------------------------------
def test_plain_password_env_is_hashed_at_startup(monkeypatch):
    """해시를 손으로 옮기지 않고 비밀번호를 그대로 넣어도 되어야 한다."""
    from bot.web.auth import load_account

    monkeypatch.delenv("WEB_PASSWORD_HASH", raising=False)
    monkeypatch.setenv("WEB_USERNAME", "uta24")
    monkeypatch.setenv("WEB_PASSWORD", "1234")

    account = load_account()

    assert account.verify("uta24", "1234") is True
    assert account.verify("uta24", "9999") is False
    # 평문이 그대로 저장되지 않는다
    assert "1234" not in account.password_hash


def test_hash_env_wins_when_both_are_set(monkeypatch):
    from bot.web.auth import load_account

    monkeypatch.setenv("WEB_USERNAME", "uta24")
    monkeypatch.setenv("WEB_PASSWORD", "1234")
    monkeypatch.setenv("WEB_PASSWORD_HASH", hash_password("the-real-one"))

    account = load_account()

    assert account.verify("uta24", "the-real-one") is True
    assert account.verify("uta24", "1234") is False


def test_missing_password_names_the_simple_option(monkeypatch):
    from bot.web.auth import AuthError, load_account

    monkeypatch.delenv("WEB_PASSWORD_HASH", raising=False)
    monkeypatch.delenv("WEB_PASSWORD", raising=False)
    monkeypatch.setenv("WEB_USERNAME", "uta24")

    with pytest.raises(AuthError, match="WEB_PASSWORD"):
        load_account()


def test_whitespace_around_the_plain_password_is_trimmed(monkeypatch):
    """붙여넣기에 줄바꿈이 섞였다고 로그인이 막히면 안 된다."""
    from bot.web.auth import load_account

    monkeypatch.delenv("WEB_PASSWORD_HASH", raising=False)
    monkeypatch.setenv("WEB_USERNAME", "uta24")
    monkeypatch.setenv("WEB_PASSWORD", "  1234\n")

    assert load_account().verify("uta24", "1234") is True


# --- 차트 / 성과 ----------------------------------------------------------
@pytest.fixture
def traded_env():
    """체결 기록이 있는 서버."""
    from bot.models import Fill as ModelFill
    from bot.models import Side
    from bot.store import Store

    exchange = FakeExchange(price=100.0, equity=10_500.0)
    exchange.my_trades = [
        ModelFill("t1", SYMBOL, 1_700_000_000_000, Side.BUY, 100.0, 10, 1000.0, 0.5),
        ModelFill("t2", SYMBOL, 1_700_000_600_000, Side.SELL, 110.0, 10, 1100.0, 0.5),
    ]
    config = make_config()
    store = Store(None)
    supervisor = BotSupervisor(config, exchange_factory=lambda: exchange, store=store)
    app = create_app(
        config,
        supervisor,
        LogBuffer(capacity=20),
        Account(username=USERNAME, password_hash=hash_password(PASSWORD)),
        token_store=TokenStore(ttl_seconds=60),
    )
    client = TestClient(app)
    yield client, supervisor, store
    supervisor.stop()


def test_performance_reports_the_closed_round_trip(traded_env):
    client, supervisor, _ = traded_env
    headers = login(client)
    supervisor.start(live=False)
    assert wait_for(lambda: supervisor.snapshot().last_cycle_at is not None)

    body = client.get("/api/performance", headers=headers).json()

    assert body["trade_count"] == 1
    assert body["win_count"] == 1
    assert body["win_rate"] == pytest.approx(100.0)
    assert body["realized_pnl"] == pytest.approx(100.0 - 1.0)  # 수수료 0.5 + 0.5
    (trade,) = body["trades"]
    assert trade["side"] == "long"
    assert trade["entry_price"] == pytest.approx(100.0)
    assert trade["exit_price"] == pytest.approx(110.0)


def test_performance_is_empty_before_any_trading(env):
    client, *_ = env
    body = client.get("/api/performance", headers=login(client)).json()

    assert body["trade_count"] == 0
    assert body["win_rate"] is None
    assert body["trades"] == []


def test_chart_returns_candles_and_markers(traded_env):
    client, supervisor, _ = traded_env
    headers = login(client)
    supervisor.start(live=False)
    assert wait_for(lambda: supervisor.snapshot().last_cycle_at is not None)

    body = client.get("/api/chart", headers=headers).json()

    assert body["symbol"] == SYMBOL
    assert len(body["candles"]) > 0
    assert all({"time", "open", "high", "low", "close"} <= set(c) for c in body["candles"])
    # 진입과 청산이 각각 하나씩 표시된다
    kinds = [m["kind"] for m in body["markers"]]
    assert kinds.count("entry") == 1 and kinds.count("exit") == 1


def test_chart_rejects_a_symbol_the_bot_is_not_watching(env):
    client, *_ = env
    response = client.get("/api/chart?symbol=DOGE/USDT:USDT", headers=login(client))

    assert response.status_code == 400
    assert "감시 중인 심볼이 아닙니다" in response.json()["detail"]


def test_equity_curve_is_recorded(traded_env):
    client, supervisor, _ = traded_env
    headers = login(client)
    supervisor.start(live=False)
    assert wait_for(lambda: supervisor.snapshot().last_cycle_at is not None)

    body = client.get("/api/equity", headers=headers).json()

    assert len(body["points"]) >= 1
    assert body["points"][0]["value"] == pytest.approx(10_500.0)


@pytest.mark.parametrize("path", ["/api/chart", "/api/performance", "/api/equity"])
def test_new_endpoints_require_auth(env, path):
    client, *_ = env
    assert client.get(path).status_code == 401


# --- 로그인 2단계 인증 ----------------------------------------------------
@pytest.fixture
def totp_env():
    """2단계 인증을 켠 서버."""
    from bot.web import totp

    secret = totp.generate_secret()
    exchange = FakeExchange()
    config = make_config()
    supervisor = BotSupervisor(config, exchange_factory=lambda: exchange)
    app = create_app(
        config,
        supervisor,
        LogBuffer(capacity=20),
        Account(
            username=USERNAME,
            password_hash=hash_password(PASSWORD),
            totp_secret=secret,
        ),
        token_store=TokenStore(ttl_seconds=60),
        throttle=LoginThrottle(max_attempts=10, lockout_seconds=60),
    )
    return TestClient(app), secret


def test_login_options_tells_the_screen_to_ask_for_a_code(totp_env):
    client, _ = totp_env
    assert client.get("/api/login-options").json()["totp_required"] is True


def test_login_options_without_totp(env):
    client, *_ = env
    assert client.get("/api/login-options").json()["totp_required"] is False


def test_correct_password_without_a_code_is_rejected(totp_env):
    """2단계 인증의 요점 — 비밀번호만으로는 못 들어온다."""
    client, _ = totp_env

    response = client.post(
        "/api/login", json={"username": USERNAME, "password": PASSWORD}
    )

    assert response.status_code == 401
    assert "코드" in response.json()["detail"]


def test_correct_password_with_a_wrong_code_is_rejected(totp_env):
    client, secret = totp_env
    from bot.web import totp

    correct = totp.generate_code(secret)
    wrong = "000000" if correct != "000000" else "111111"

    response = client.post(
        "/api/login",
        json={"username": USERNAME, "password": PASSWORD, "code": wrong},
    )

    assert response.status_code == 401


def test_correct_password_and_code_logs_in(totp_env):
    client, secret = totp_env
    from bot.web import totp

    response = client.post(
        "/api/login",
        json={
            "username": USERNAME,
            "password": PASSWORD,
            "code": totp.generate_code(secret),
        },
    )

    assert response.status_code == 200
    headers = {"Authorization": f"Bearer {response.json()['token']}"}
    assert client.get("/api/status", headers=headers).status_code == 200


def test_the_same_code_cannot_be_replayed(totp_env):
    """코드가 30초간 유효하므로, 새어 나간 코드의 재사용을 막아야 한다."""
    client, secret = totp_env
    from bot.web import totp

    code = totp.generate_code(secret)
    body = {"username": USERNAME, "password": PASSWORD, "code": code}

    assert client.post("/api/login", json=body).status_code == 200

    second = client.post("/api/login", json=body)
    assert second.status_code == 401
    assert "이미 사용된" in second.json()["detail"]


def test_failed_code_attempts_count_toward_the_lockout():
    """비밀번호를 맞춘 뒤 코드만 무한히 시도할 수 있으면 안 된다."""
    from bot.web import totp

    config = make_config()
    app = create_app(
        config,
        BotSupervisor(config, exchange_factory=lambda: FakeExchange()),
        LogBuffer(capacity=10),
        Account(
            username=USERNAME,
            password_hash=hash_password(PASSWORD),
            totp_secret=totp.generate_secret(),
        ),
        token_store=TokenStore(ttl_seconds=60),
        throttle=LoginThrottle(max_attempts=2, lockout_seconds=60),
    )
    client = TestClient(app)
    body = {"username": USERNAME, "password": PASSWORD, "code": "000000"}

    assert client.post("/api/login", json=body).status_code in (401, 429)
    assert client.post("/api/login", json=body).status_code in (401, 429)
    assert client.post("/api/login", json=body).status_code == 429


# --- 캐시된 옛 화면 감지 ---------------------------------------------------
def test_index_is_not_cacheable(tmp_path):
    """index.html 이 캐시되면 재배포해도 브라우저가 옛 화면을 계속 띄운다."""
    static = tmp_path / "static"
    (static / "assets").mkdir(parents=True)
    (static / "index.html").write_text(
        '<script src="/assets/index-ABC123.js"></script>', encoding="utf-8"
    )
    (static / "assets" / "index-ABC123.js").write_text("//", encoding="utf-8")

    config = make_config()
    app = create_app(
        config,
        BotSupervisor(config, exchange_factory=lambda: FakeExchange()),
        LogBuffer(capacity=5),
        Account(username=USERNAME, password_hash=hash_password(PASSWORD)),
        static_dir=static,
    )
    client = TestClient(app)

    response = client.get("/")

    assert response.status_code == 200
    assert "no-cache" in response.headers.get("cache-control", "")


def test_build_endpoint_reports_the_served_bundle(tmp_path):
    """브라우저가 실행 중인 번들과 비교해 화면이 낡았는지 판단한다."""
    static = tmp_path / "static"
    static.mkdir()
    (static / "index.html").write_text(
        '<script type="module" src="/assets/index-XyZ789.js"></script>', encoding="utf-8"
    )

    config = make_config()
    app = create_app(
        config,
        BotSupervisor(config, exchange_factory=lambda: FakeExchange()),
        LogBuffer(capacity=5),
        Account(username=USERNAME, password_hash=hash_password(PASSWORD)),
        static_dir=static,
    )

    body = TestClient(app).get("/api/build").json()

    assert body["bundle"] == "index-XyZ789.js"


def test_build_endpoint_without_a_frontend(env):
    client, *_ = env
    assert client.get("/api/build").json()["bundle"] is None


# --- 봇이 멈춰 있을 때의 차트 ---------------------------------------------
def test_chart_works_while_the_bot_is_stopped(traded_env):
    """차트를 보려고 봇을 켜야 할 이유는 없다."""
    client, supervisor, _ = traded_env

    assert not supervisor.running
    body = client.get("/api/chart", headers=login(client)).json()

    assert len(body["candles"]) > 0


def test_stopped_chart_calls_the_exchange_only_once_per_ttl():
    """대시보드가 2초마다 폴링한다 — 캐시가 없으면 레이트리밋을 태운다."""
    from bot.store import Store

    class CountingExchange(FakeExchange):
        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            self.candle_queries = 0

        def fetch_candles(self, symbol, timeframe, limit, since=None):
            self.candle_queries += 1
            return super().fetch_candles(symbol, timeframe, limit, since)

    exchange = CountingExchange(price=100.0)
    config = make_config()
    supervisor = BotSupervisor(
        config, exchange_factory=lambda: exchange, store=Store(None),
        positions_cache_ttl=5.0,
    )

    for offset in (0.0, 2.0, 4.0):
        supervisor.candles(SYMBOL, now=offset)

    assert exchange.candle_queries == 1


def test_chart_falls_back_to_the_bot_cache_while_running(traded_env):
    """봇이 도는 동안 요청 스레드가 거래소를 만지면 ccxt 세션이 경쟁한다."""
    client, supervisor, _ = traded_env
    headers = login(client)
    supervisor.start(live=False)
    try:
        assert wait_for(lambda: supervisor.snapshot().last_cycle_at is not None)
        body = client.get("/api/chart", headers=headers).json()
        assert len(body["candles"]) > 0
    finally:
        supervisor.stop()


# --- 전략 경쟁 순위표 -----------------------------------------------------
def test_leaderboard_lists_every_strategy(traded_env):
    """새 전략이 추가되면 자동으로 합류해야 한다."""
    client, supervisor, _ = traded_env
    headers = login(client)
    supervisor.start(live=False)
    try:
        assert wait_for(lambda: supervisor.snapshot().last_cycle_at is not None)
        body = client.get("/api/leaderboard", headers=headers).json()
    finally:
        supervisor.stop()

    names = {s["name"] for s in body["strategies"]}
    assert len(names) >= 10
    assert "ema_cross" in names and "grid" in names
    assert body["active"] == "hold"


def test_leaderboard_rows_carry_every_metric(traded_env):
    client, supervisor, _ = traded_env
    headers = login(client)
    supervisor.start(live=False)
    try:
        assert wait_for(lambda: supervisor.snapshot().last_cycle_at is not None)
        body = client.get("/api/leaderboard", headers=headers).json()
    finally:
        supervisor.stop()

    required = {
        "return_pct", "net_pnl", "trade_count", "win_rate", "stop_out_rate",
        "liquidation_risk_pct", "max_drawdown_pct", "open_positions", "started_at",
    }
    assert required <= set(body["strategies"][0])


def test_leaderboard_reset_requires_confirmation(traded_env):
    client, *_ = traded_env
    headers = login(client)

    response = client.post("/api/leaderboard/reset", json={}, headers=headers)

    assert response.status_code == 400
    assert "RESET" in response.json()["detail"]


def test_leaderboard_reset_clears_records(traded_env):
    client, supervisor, store = traded_env
    headers = login(client)
    supervisor.start(live=False)
    try:
        assert wait_for(lambda: supervisor.snapshot().last_cycle_at is not None)
    finally:
        supervisor.stop()

    response = client.post(
        "/api/leaderboard/reset", json={"confirm": "RESET"}, headers=headers
    )

    assert response.status_code == 200
    assert store.paper_accounts() == []


def test_strategies_endpoint_carries_the_algorithm(env):
    """전략 상세에 실제 규칙이 있어야 왜 진입했는지 알 수 있다."""
    client, *_ = env
    body = client.get("/api/strategies", headers=login(client)).json()

    for entry in body["strategies"]:
        assert len(entry["algorithm"]) > 200, entry["name"]
        assert "진입" in entry["algorithm"] and "손절" in entry["algorithm"]


@pytest.mark.parametrize("path", ["/api/leaderboard"])
def test_leaderboard_requires_auth(env, path):
    client, *_ = env
    assert client.get(path).status_code == 401
