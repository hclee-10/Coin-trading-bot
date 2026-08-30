"""거래소별 차이 — 자격증명과 레버리지 파라미터."""

import pytest

from bot.config import ConfigError, Credentials, ExchangeConfig, passphrase_required
from bot.exchanges.ccxt_futures import CcxtFuturesExchange

ALL_ENV_VARS = [
    f"{prefix}_API_{suffix}"
    for prefix in ("GATE", "BITGET", "OKX")
    for suffix in ("KEY", "SECRET", "PASSPHRASE")
]


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for name in ALL_ENV_VARS:
        monkeypatch.delenv(name, raising=False)


# --- 자격증명 ------------------------------------------------------------
def test_gate_does_not_use_a_passphrase():
    """Gate 는 API 패스프레이즈 개념이 없다 — 요구하면 로그인 자체가 막힌다."""
    assert passphrase_required("gate") is False


@pytest.mark.parametrize("exchange_id", ["bitget", "okx"])
def test_bitget_and_okx_require_a_passphrase(exchange_id):
    assert passphrase_required(exchange_id) is True


def test_gate_credentials_need_only_key_and_secret(monkeypatch):
    monkeypatch.setenv("GATE_API_KEY", "k")
    monkeypatch.setenv("GATE_API_SECRET", "s")

    creds = Credentials.from_env("gate")

    assert (creds.api_key, creds.secret) == ("k", "s")
    assert creds.password == ""


def test_bitget_still_requires_the_passphrase(monkeypatch):
    monkeypatch.setenv("BITGET_API_KEY", "k")
    monkeypatch.setenv("BITGET_API_SECRET", "s")

    with pytest.raises(ConfigError, match="BITGET_API_PASSPHRASE"):
        Credentials.from_env("bitget")


def test_gate_error_does_not_mention_a_passphrase(monkeypatch):
    monkeypatch.setenv("GATE_API_KEY", "k")

    with pytest.raises(ConfigError) as excinfo:
        Credentials.from_env("gate")

    assert "GATE_API_SECRET" in str(excinfo.value)
    assert "PASSPHRASE" not in str(excinfo.value)


# --- 설정 검증 -----------------------------------------------------------
@pytest.mark.parametrize("exchange_id", ["gate", "bitget", "okx"])
def test_supported_exchanges_validate(exchange_id):
    ExchangeConfig(id=exchange_id).validate()


def test_unsupported_exchange_lists_the_options():
    with pytest.raises(ConfigError, match="gate"):
        ExchangeConfig(id="binance").validate()


# --- 레버리지 파라미터 ---------------------------------------------------
def make(exchange_id: str) -> CcxtFuturesExchange:
    return CcxtFuturesExchange(exchange_id, "k", "s", "p")


def test_bitget_isolated_sets_leverage_for_both_directions():
    """Bitget 격리는 롱/숏에 따로 걸어야 한 쪽만 걸리는 사고가 없다."""
    assert make("bitget")._leverage_params("isolated") == [
        {"holdSide": "long"},
        {"holdSide": "short"},
    ]


def test_bitget_cross_uses_the_unified_param():
    assert make("bitget")._leverage_params("cross") == [{"marginMode": "cross"}]


def test_gate_uses_client_options_not_request_params():
    """Gate 는 params 의 marginMode 를 요청에 그대로 실어 보내 거부될 수 있다."""
    exchange = make("gate")

    params = exchange._leverage_params("isolated")

    assert params == [{}]
    assert exchange._ex.options["marginMode"] == "isolated"


def test_okx_uses_the_unified_param():
    assert make("okx")._leverage_params("isolated") == [{"marginMode": "isolated"}]


def test_margin_mode_call_is_skipped_where_unsupported():
    """Gate 에는 마진 모드 전용 API 가 없다 — 호출하면 매번 경고만 남는다."""
    assert make("gate")._ex.has.get("setMarginMode") is False
    assert make("bitget")._ex.has.get("setMarginMode") is True


# --- 펀딩비 --------------------------------------------------------------
class _FakeCcxt:
    """fetch_funding_rate 만 흉내 내는 최소한의 가짜 ccxt 객체."""

    def __init__(self, payload=None, *, supported=True, raises=None):
        self.has = {"fetchFundingRate": supported}
        self.options = {}
        self._payload = payload or {}
        self._raises = raises

    def fetch_funding_rate(self, symbol):
        if self._raises:
            raise self._raises
        return dict(self._payload, symbol=symbol)


def _exchange_with(fake):
    ex = CcxtFuturesExchange.__new__(CcxtFuturesExchange)
    ex._ex = fake
    ex.id = "gate"
    ex._markets_loaded = True
    ex._call = lambda fn, *a, **kw: fn(*a, **kw)
    ex._require_markets = lambda: None
    return ex


def test_funding_rate_is_parsed_with_its_settlement_time():
    ex = _exchange_with(_FakeCcxt({
        "fundingRate": 0.0001,
        "fundingTimestamp": 1_700_000_000_000,
        "interval": "8h",
    }))
    rate = ex.fetch_funding_rate("BTC/USDT:USDT")
    assert rate.rate == 0.0001
    assert rate.next_time_ms == 1_700_000_000_000
    assert rate.interval_hours == 8.0


def test_a_four_hour_funding_interval_is_read_correctly():
    """일부 종목은 4시간마다 정산한다. 8시간으로 넘겨짚으면 절반만 문다."""
    ex = _exchange_with(_FakeCcxt({"fundingRate": 0.0002, "interval": "4h"}))
    assert ex.fetch_funding_rate("BTC/USDT:USDT").interval_hours == 4.0


def test_an_unsupported_or_failing_funding_call_returns_none_not_an_error():
    """펀딩비는 부가 정보다. 이것 때문에 매매 주기가 통째로 실패하면 안 된다."""
    assert _exchange_with(_FakeCcxt(supported=False)).fetch_funding_rate("X") is None

    boom = _exchange_with(_FakeCcxt(raises=RuntimeError("거래소 장애")))
    assert boom.fetch_funding_rate("X") is None
