"""CLI 기본값 — 특히 컨테이너 배포에서 바뀌는 것들."""

import pytest

from bot.cli import build_parser

RAILWAY_VARS = ("RAILWAY_ENVIRONMENT", "RAILWAY_ENVIRONMENT_NAME",
                "RAILWAY_PROJECT_ID", "RAILWAY_SERVICE_ID")


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for name in RAILWAY_VARS + ("PORT", "TRUST_PROXY", "PROXY_HOPS"):
        monkeypatch.delenv(name, raising=False)


def parse_web(*args):
    return build_parser().parse_args(["web", *args])


def test_local_default_binds_localhost_only():
    args = parse_web()
    assert args.host == "127.0.0.1"
    assert args.port == 8000
    assert args.trust_proxy is False


def test_port_env_switches_to_all_interfaces():
    """PaaS 가 PORT 를 주면 컨테이너 안이라는 뜻 — 0.0.0.0 이어야 라우터가 붙는다."""
    import os

    os.environ["PORT"] = "4321"
    try:
        args = parse_web()
    finally:
        del os.environ["PORT"]
    assert args.host == "0.0.0.0"
    assert args.port == 4321


@pytest.mark.parametrize("var", RAILWAY_VARS)
def test_railway_turns_on_proxy_trust(monkeypatch, var):
    """프록시 신뢰를 안 켜면 모든 접속자가 같은 IP 로 보여 함께 잠긴다."""
    monkeypatch.setenv(var, "production")
    assert parse_web().trust_proxy is True


def test_trust_proxy_can_be_forced_by_env(monkeypatch):
    monkeypatch.setenv("TRUST_PROXY", "true")
    assert parse_web().trust_proxy is True


@pytest.mark.parametrize("value", ["", "0", "false", "no", "off"])
def test_trust_proxy_stays_off_for_falsy_values(monkeypatch, value):
    monkeypatch.setenv("TRUST_PROXY", value)
    assert parse_web().trust_proxy is False


def test_proxy_hops_comes_from_env(monkeypatch):
    monkeypatch.setenv("PROXY_HOPS", "2")
    assert parse_web().proxy_hops == 2


def test_explicit_flags_beat_env_defaults(monkeypatch):
    monkeypatch.setenv("PORT", "4321")
    args = parse_web("--host", "127.0.0.1", "--port", "9999")
    assert (args.host, args.port) == ("127.0.0.1", 9999)
