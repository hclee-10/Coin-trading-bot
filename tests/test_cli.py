"""CLI 기본값 — 특히 컨테이너 배포에서 바뀌는 것들."""

import pytest

from bot.cli import build_parser, resolve_autostart, resolve_state_dir

RAILWAY_VARS = ("RAILWAY_ENVIRONMENT", "RAILWAY_ENVIRONMENT_NAME",
                "RAILWAY_PROJECT_ID", "RAILWAY_SERVICE_ID")


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for name in RAILWAY_VARS + ("PORT", "TRUST_PROXY", "PROXY_HOPS",
                                "STATE_DIR", "AUTOSTART"):
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


# --- check-env ------------------------------------------------------------
def test_check_env_reports_a_broken_hash(monkeypatch, capsys):
    """배포 환경에서 '분명히 넣었는데 왜 안 되냐'를 추측으로 풀지 않기 위한 명령."""
    from bot.cli import main

    monkeypatch.setenv("WEB_USERNAME", "trader")
    monkeypatch.setenv("WEB_PASSWORD_HASH", "scrypt$$$")
    monkeypatch.setenv("GATE_API_KEY", "k")
    monkeypatch.setenv("GATE_API_SECRET", "s")
    monkeypatch.setenv(
        "CONFIG_YAML",
        'exchange: {id: gate}\ntrading: {symbols: ["BTC/USDT:USDT"]}\n',
    )

    assert main(["check-env"]) == 1
    out = capsys.readouterr().out
    assert "WEB_PASSWORD_HASH" in out and "깨졌" in out


def test_check_env_passes_when_everything_is_set(monkeypatch, capsys):
    from bot.cli import main
    from bot.web.auth import hash_password

    monkeypatch.setenv("WEB_USERNAME", "trader")
    monkeypatch.setenv("WEB_PASSWORD_HASH", hash_password("1234"))
    monkeypatch.setenv("GATE_API_KEY", "k")
    monkeypatch.setenv("GATE_API_SECRET", "s")
    monkeypatch.setenv(
        "CONFIG_YAML",
        'exchange: {id: gate}\ntrading: {symbols: ["BTC/USDT:USDT"]}\n',
    )

    assert main(["check-env"]) == 0
    assert "모두 정상" in capsys.readouterr().out


def test_check_env_never_prints_values(monkeypatch, capsys):
    from bot.cli import main
    from bot.web.auth import hash_password

    secret = "super-secret-api-key-value"
    monkeypatch.setenv("WEB_USERNAME", "trader")
    monkeypatch.setenv("WEB_PASSWORD_HASH", hash_password("1234"))
    monkeypatch.setenv("GATE_API_KEY", secret)
    monkeypatch.setenv("GATE_API_SECRET", secret)
    monkeypatch.setenv(
        "CONFIG_YAML",
        'exchange: {id: gate}\ntrading: {symbols: ["BTC/USDT:USDT"]}\n',
    )

    main(["check-env"])

    assert secret not in capsys.readouterr().out


# --- 기록을 둘 곳 ----------------------------------------------------------
def make_config(log_file="logs/bot.log"):
    from bot.config import Config
    config = Config()
    config.logging.file = log_file
    return config


def test_state_dir_prefers_an_explicit_env_var(monkeypatch, tmp_path):
    monkeypatch.setenv("STATE_DIR", str(tmp_path))
    path, why, _ = resolve_state_dir(make_config())
    assert path == str(tmp_path)
    assert "STATE_DIR" in why


def test_state_dir_finds_a_mounted_volume_without_any_env_var(monkeypatch, tmp_path):
    """볼륨만 붙이면 변수 없이도 기록이 살아남아야 한다 — 설정 하나를 빠뜨려
    며칠치 모의매매가 날아가는 것이 실제로 일어난 사고다."""
    volume = tmp_path / "data"
    volume.mkdir()
    monkeypatch.setattr("bot.cli.VOLUME_CANDIDATES", (str(volume),))
    monkeypatch.setattr("bot.cli.os.path.ismount", lambda p: p == str(volume))
    path, why, durable = resolve_state_dir(make_config())
    assert path == str(volume)
    assert "볼륨" in why
    assert durable is True


def test_an_unmounted_data_directory_is_not_treated_as_a_volume(monkeypatch, tmp_path):
    """볼륨을 안 붙여도 /data 디렉터리 자체는 만들어진다. 존재만 보고 판단하면
    컨테이너 안에 쓰면서 잘 저장되고 있다고 착각하게 된다."""
    fake_volume = tmp_path / "data"
    fake_volume.mkdir()          # 존재하고 쓸 수도 있지만 마운트는 아니다
    monkeypatch.setattr("bot.cli.VOLUME_CANDIDATES", (str(fake_volume),))
    monkeypatch.setattr("bot.cli.os.path.ismount", lambda p: False)
    path, why, durable = resolve_state_dir(make_config("logs/bot.log"))
    assert path == "logs"
    assert durable is False
    assert "재배포하면 사라짐" in why


def test_state_dir_falls_back_to_the_log_directory_and_says_so(monkeypatch):
    monkeypatch.setattr("bot.cli.VOLUME_CANDIDATES", ("/definitely-not-mounted-xyz",))
    path, why, durable = resolve_state_dir(make_config("logs/bot.log"))
    assert path == "logs"
    assert "볼륨이 없어" in why
    assert durable is False


# --- 자동 시작 -------------------------------------------------------------
def test_autostart_defaults_to_dry_run():
    """순위표는 봇이 돌아야 기록된다. 기본이 꺼짐이면 데이터에 구멍이 난다."""
    assert resolve_autostart() == (True, False)


def test_autostart_live_requires_an_explicit_opt_in(monkeypatch):
    monkeypatch.setenv("AUTOSTART", "live")
    assert resolve_autostart() == (True, True)


@pytest.mark.parametrize("value", ["off", "0", "false", "no", "none", "OFF"])
def test_autostart_can_be_turned_off(monkeypatch, value):
    monkeypatch.setenv("AUTOSTART", value)
    assert resolve_autostart() == (False, False)


def test_an_unknown_autostart_value_is_dry_run_not_live(monkeypatch):
    """오타가 실거래를 켜는 일은 없어야 한다."""
    monkeypatch.setenv("AUTOSTART", "yes")
    assert resolve_autostart() == (True, False)


def test_a_store_on_a_container_path_reports_itself_as_not_durable(tmp_path):
    """쓰기에 성공하는 것과 재배포를 넘어 남는 것은 다른 문제다."""
    from bot.store import Store

    store = Store(tmp_path / "bot.db", durable=False)
    assert store.persistent is True    # 파일에는 정상적으로 쓴다
    assert store.durable is False      # 하지만 재배포하면 사라진다


def test_an_existing_db_inside_the_volume_is_not_orphaned(monkeypatch, tmp_path):
    """DB 경로를 옮기는 바람에 이미 쌓인 기록이 사라진 것처럼 보이면 안 된다."""
    volume = tmp_path / "data"
    (volume / "logs").mkdir(parents=True)
    (volume / "logs" / "bot.db").write_text("")   # 예전 위치에 기록이 있다
    monkeypatch.setattr("bot.cli.VOLUME_CANDIDATES", (str(volume),))
    monkeypatch.setattr("bot.cli.os.path.ismount", lambda p: p == str(volume))

    path, why, durable = resolve_state_dir(make_config(str(volume / "logs" / "bot.log")))
    assert path == str(volume / "logs")
    assert durable is True
    assert "이어서" in why


def test_a_fresh_volume_gets_the_db_at_its_root(monkeypatch, tmp_path):
    volume = tmp_path / "data"
    (volume / "logs").mkdir(parents=True)
    monkeypatch.setattr("bot.cli.VOLUME_CANDIDATES", (str(volume),))
    monkeypatch.setattr("bot.cli.os.path.ismount", lambda p: p == str(volume))

    path, _, durable = resolve_state_dir(make_config(str(volume / "logs" / "bot.log")))
    assert path == str(volume)
    assert durable is True
