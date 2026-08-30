import pytest

from bot.config import (
    Config,
    ConfigError,
    Credentials,
    ExchangeConfig,
    RiskConfig,
    TradingConfig,
)


def write(tmp_path, text):
    path = tmp_path / "config.yaml"
    path.write_text(text, encoding="utf-8")
    return path


def test_example_config_is_valid():
    Config.load("config.example.yaml")


def test_missing_file_names_the_example(tmp_path):
    with pytest.raises(ConfigError, match="config.example.yaml"):
        Config.load(tmp_path / "nope.yaml")


def test_typo_in_key_is_rejected(tmp_path):
    path = write(tmp_path, "risk:\n  risk_per_trade: 1.0\n")
    with pytest.raises(ConfigError, match="알 수 없는 키"):
        Config.load(path)


def test_spot_symbol_is_rejected():
    with pytest.raises(ConfigError, match="무기한 선물 심볼이 아닙니다"):
        TradingConfig(symbols=["BTC/USDT"]).validate()


def test_hedge_mode_is_rejected():
    with pytest.raises(ConfigError, match="hedge_mode"):
        ExchangeConfig(hedge_mode=True).validate()


def test_unsupported_exchange_is_rejected():
    with pytest.raises(ConfigError, match="지원하지 않는 거래소"):
        ExchangeConfig(id="binance").validate()


def test_leverage_above_risk_cap_is_rejected():
    config = Config(
        exchange=ExchangeConfig(leverage=10.0), risk=RiskConfig(max_leverage=5.0)
    )
    with pytest.raises(ConfigError, match="max_leverage"):
        config.validate()


def test_too_fast_polling_is_rejected():
    with pytest.raises(ConfigError, match="poll_interval_sec"):
        TradingConfig(poll_interval_sec=0.2).validate()


def test_credentials_error_lists_missing_vars(monkeypatch):
    for suffix in ("API_KEY", "API_SECRET", "API_PASSPHRASE"):
        monkeypatch.delenv(f"OKX_{suffix}", raising=False)
    with pytest.raises(ConfigError, match="OKX_API_KEY"):
        Credentials.from_env("okx")


def test_credentials_are_read_from_env(monkeypatch):
    monkeypatch.setenv("BITGET_API_KEY", "k")
    monkeypatch.setenv("BITGET_API_SECRET", "s")
    monkeypatch.setenv("BITGET_API_PASSPHRASE", "p")
    creds = Credentials.from_env("bitget")
    assert (creds.api_key, creds.secret, creds.password) == ("k", "s", "p")


# --- 컨테이너 배포용 설정 주입 -------------------------------------------
def test_config_yaml_env_var_wins_over_the_file(monkeypatch, tmp_path):
    """컨테이너에는 설정 파일을 올리기 번거로워 환경변수로 넣을 수 있어야 한다."""
    path = write(tmp_path, "exchange:\n  id: okx\n")
    monkeypatch.setenv(
        "CONFIG_YAML",
        'exchange:\n  id: bitget\n  leverage: 2\ntrading:\n  symbols: ["ETH/USDT:USDT"]\n',
    )
    config = Config.load(path)
    assert config.exchange.id == "bitget"
    assert config.trading.symbols == ["ETH/USDT:USDT"]


def test_file_is_used_when_env_var_is_blank(monkeypatch):
    monkeypatch.setenv("CONFIG_YAML", "   ")
    assert Config.load("config.example.yaml").exchange.id == "okx"


def test_broken_yaml_in_env_var_names_the_source(monkeypatch):
    monkeypatch.setenv("CONFIG_YAML", "exchange: [열린괄호")
    with pytest.raises(ConfigError, match="CONFIG_YAML"):
        Config.load("config.example.yaml")


def test_missing_file_mentions_the_env_var_alternative(tmp_path, monkeypatch):
    monkeypatch.delenv("CONFIG_YAML", raising=False)
    with pytest.raises(ConfigError, match="CONFIG_YAML"):
        Config.load(tmp_path / "nope.yaml")
