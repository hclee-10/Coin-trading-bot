"""설정으로부터 거래소 어댑터를 만든다."""

from __future__ import annotations

from bot.config import Credentials, ExchangeConfig
from bot.exchanges.base import FuturesExchange
from bot.exchanges.ccxt_futures import CcxtFuturesExchange


def create_exchange(cfg: ExchangeConfig, credentials: Credentials | None = None) -> FuturesExchange:
    """Bitget/OKX 어댑터를 생성한다. 자격증명을 안 주면 환경변수에서 읽는다."""
    creds = credentials or Credentials.from_env(cfg.id)
    exchange = CcxtFuturesExchange(
        cfg.id,
        api_key=creds.api_key,
        secret=creds.secret,
        password=creds.password,
        timeout_ms=cfg.request_timeout_ms,
    )
    exchange.load_markets()
    return exchange
