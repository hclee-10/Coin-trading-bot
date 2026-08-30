"""YAML 설정 + 환경변수 시크릿 로딩.

API 키는 절대 YAML에 두지 않는다. 파일에는 거래 파라미터만 두고, 키/시크릿/
패스프레이즈는 `<EXCHANGE>_API_KEY` 형태의 환경변수에서만 읽는다.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any

import yaml

SUPPORTED_EXCHANGES = ("bitget", "okx")


class ConfigError(Exception):
    """설정 파일이 없거나 값이 유효하지 않을 때."""


@dataclass
class Credentials:
    api_key: str
    secret: str
    password: str  # 두 거래소 모두 API 패스프레이즈를 요구한다

    @classmethod
    def from_env(cls, exchange_id: str) -> "Credentials":
        prefix = exchange_id.upper()
        key = os.getenv(f"{prefix}_API_KEY", "")
        secret = os.getenv(f"{prefix}_API_SECRET", "")
        password = os.getenv(f"{prefix}_API_PASSPHRASE", "")
        missing = [
            name
            for name, value in (
                (f"{prefix}_API_KEY", key),
                (f"{prefix}_API_SECRET", secret),
                (f"{prefix}_API_PASSPHRASE", password),
            )
            if not value
        ]
        if missing:
            raise ConfigError(
                f"{exchange_id} 자격증명 누락: {', '.join(missing)}. "
                ".env.example 을 복사해 .env 를 만들고 값을 채우세요."
            )
        return cls(api_key=key, secret=secret, password=password)


@dataclass
class ExchangeConfig:
    id: str = "okx"
    margin_mode: str = "isolated"  # isolated | cross
    leverage: float = 3.0
    hedge_mode: bool = False  # 이 봇은 단방향(one-way) 모드만 지원한다
    request_timeout_ms: int = 15_000

    def validate(self) -> None:
        if self.id not in SUPPORTED_EXCHANGES:
            raise ConfigError(
                f"지원하지 않는 거래소 '{self.id}'. 가능: {', '.join(SUPPORTED_EXCHANGES)}"
            )
        if self.margin_mode not in ("isolated", "cross"):
            raise ConfigError(f"margin_mode 는 isolated 또는 cross 여야 합니다: {self.margin_mode}")
        if self.leverage <= 0:
            raise ConfigError("leverage 는 0보다 커야 합니다")
        if self.hedge_mode:
            raise ConfigError(
                "hedge_mode 는 아직 지원하지 않습니다. 거래소에서 단방향(one-way) 모드로 "
                "설정하고 hedge_mode: false 로 두세요."
            )


@dataclass
class TradingConfig:
    symbols: list[str] = field(default_factory=lambda: ["BTC/USDT:USDT"])
    timeframe: str = "5m"
    candle_limit: int = 300
    poll_interval_sec: float = 15.0
    quote_currency: str = "USDT"
    allow_reverse: bool = False  # 반대 신호에서 청산 후 즉시 반대 진입 허용 여부
    close_positions_on_exit: bool = False

    def validate(self) -> None:
        if not self.symbols:
            raise ConfigError("symbols 가 비어 있습니다")
        for symbol in self.symbols:
            if ":" not in symbol:
                raise ConfigError(
                    f"'{symbol}' 은 무기한 선물 심볼이 아닙니다. "
                    "'BTC/USDT:USDT' 형식을 사용하세요."
                )
        if self.poll_interval_sec < 1:
            raise ConfigError("poll_interval_sec 은 1초 이상이어야 합니다 (레이트리밋 보호)")
        if self.candle_limit < 2:
            raise ConfigError("candle_limit 은 2 이상이어야 합니다")


@dataclass
class StrategyConfig:
    name: str = "hold"
    params: dict[str, Any] = field(default_factory=dict)


@dataclass
class RiskConfig:
    risk_per_trade_pct: float = 0.5      # 손절까지 갔을 때 잃을 자기자본 비율(%)
    max_position_notional_pct: float = 20.0  # 자기자본 대비 포지션 명목가 상한(%)
    max_leverage: float = 5.0
    max_open_positions: int = 2
    max_daily_loss_pct: float = 3.0      # 일일 손실이 이 값을 넘으면 킬스위치
    min_order_notional: float = 5.0      # 이보다 작은 주문은 보내지 않는다
    default_stop_loss_pct: float = 1.0   # 전략이 손절가를 안 주면 사용
    default_take_profit_pct: float = 2.0  # 0 이면 익절 주문을 걸지 않는다

    def validate(self) -> None:
        if not 0 < self.risk_per_trade_pct <= 100:
            raise ConfigError("risk_per_trade_pct 는 0 초과 100 이하여야 합니다")
        if not 0 < self.max_position_notional_pct <= 1000:
            raise ConfigError("max_position_notional_pct 는 0 초과 1000 이하여야 합니다")
        if self.max_leverage <= 0:
            raise ConfigError("max_leverage 는 0보다 커야 합니다")
        if self.max_open_positions < 1:
            raise ConfigError("max_open_positions 는 1 이상이어야 합니다")
        if self.default_stop_loss_pct <= 0:
            raise ConfigError("default_stop_loss_pct 는 0보다 커야 합니다")
        if self.max_daily_loss_pct <= 0:
            raise ConfigError("max_daily_loss_pct 는 0보다 커야 합니다")


@dataclass
class LoggingConfig:
    level: str = "INFO"
    file: str | None = "logs/bot.log"


@dataclass
class Config:
    exchange: ExchangeConfig = field(default_factory=ExchangeConfig)
    trading: TradingConfig = field(default_factory=TradingConfig)
    strategy: StrategyConfig = field(default_factory=StrategyConfig)
    risk: RiskConfig = field(default_factory=RiskConfig)
    logging: LoggingConfig = field(default_factory=LoggingConfig)

    def validate(self) -> None:
        self.exchange.validate()
        self.trading.validate()
        self.risk.validate()
        if self.exchange.leverage > self.risk.max_leverage:
            raise ConfigError(
                f"exchange.leverage({self.exchange.leverage}) 가 "
                f"risk.max_leverage({self.risk.max_leverage}) 를 초과합니다"
            )

    @classmethod
    def load(cls, path: str | Path) -> "Config":
        path = Path(path)
        if not path.exists():
            raise ConfigError(
                f"설정 파일을 찾을 수 없습니다: {path}. "
                "config.example.yaml 을 config.yaml 로 복사해 수정하세요."
            )
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        if not isinstance(raw, dict):
            raise ConfigError(f"{path} 의 최상위는 매핑(dict)이어야 합니다")
        config = cls(
            exchange=_build(ExchangeConfig, raw.get("exchange"), "exchange"),
            trading=_build(TradingConfig, raw.get("trading"), "trading"),
            strategy=_build(StrategyConfig, raw.get("strategy"), "strategy"),
            risk=_build(RiskConfig, raw.get("risk"), "risk"),
            logging=_build(LoggingConfig, raw.get("logging"), "logging"),
        )
        config.validate()
        return config


def _build(cls: type, raw: Any, section: str):
    """알 수 없는 키를 조용히 넘기지 않고 에러로 알린다(오타로 인한 설정 무시 방지)."""
    if raw is None:
        return cls()
    if not isinstance(raw, dict):
        raise ConfigError(f"'{section}' 섹션은 매핑(dict)이어야 합니다")
    known = {f.name for f in fields(cls)}
    unknown = set(raw) - known
    if unknown:
        raise ConfigError(
            f"'{section}' 섹션에 알 수 없는 키: {', '.join(sorted(unknown))}. "
            f"사용 가능: {', '.join(sorted(known))}"
        )
    return cls(**raw)
