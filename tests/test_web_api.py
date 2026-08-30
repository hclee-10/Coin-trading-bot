import pytest
from fastapi.testclient import TestClient

from bot.logging_utils import LogBuffer
from bot.models import Position, PositionSide
from bot.web.app import create_app
from bot.web.auth import LoginThrottle, TokenStore, hash_password
from bot.web.supervisor import BotSupervisor
from tests.fakes import FakeExchange
from tests.test_supervisor import SYMBOL, make_config, wait_for

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
        hash_password(PASSWORD),
        token_store=TokenStore(ttl_seconds=60),
        throttle=LoginThrottle(max_attempts=3, lockout_seconds=60),
    )
    client = TestClient(app)
    yield client, supervisor, exchange, buffer
    supervisor.stop()


def login(client) -> dict:
    response = client.post("/api/login", json={"password": PASSWORD})
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
    assert client.post("/api/login", json={"password": "nope"}).status_code == 401


def test_brute_force_is_locked_out(env):
    client, *_ = env
    for _ in range(3):
        assert client.post("/api/login", json={"password": "nope"}).status_code == 401
    response = client.post("/api/login", json={"password": "nope"})
    assert response.status_code == 429
    # 잠긴 동안에는 올바른 비밀번호도 막힌다
    assert client.post("/api/login", json={"password": PASSWORD}).status_code == 429


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
