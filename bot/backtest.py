"""과거 캔들로 전략을 돌려 보는 백테스터.

**설계에서 가장 중요한 것은 미래 정보를 쓰지 않는 것이다.** 백테스터가 거짓말을
하는 가장 흔한 이유가 이것이고, 그러면 실계좌에서 재현되지 않는 성적표가 나온다.
그래서:

* 전략은 **i 번째 봉의 종가까지만** 본다.
* 주문은 **i+1 번째 봉에서** 체결된다 — 판단한 그 봉의 종가로 체결시키면
  실제로는 불가능한 거래가 된다.
* 한 봉 안에서 손절과 청산 신호가 겹치면 **손절이 먼저** 일어난 것으로 본다.
  봉 안의 순서를 알 수 없으므로 불리한 쪽을 가정한다.

수수료는 실제 값을 넣는다. 이걸 빼면 대부분의 단타 전략이 흑자로 보인다.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from bot.config import Config
from bot.models import Candle, Position, PositionSide, SignalAction, Ticker
from bot.risk import RiskManager
from bot.strategies import Strategy, StrategyContext

log = logging.getLogger(__name__)

# Gate 무기한 선물 기본 수수료. VIP 등급에 따라 낮아진다.
DEFAULT_TAKER_FEE = 0.0005   # 0.05%
DEFAULT_MAKER_FEE = 0.0002   # 0.02%


@dataclass
class BacktestTrade:
    side: str            # long | short
    entry_time: int
    exit_time: int
    entry_price: float
    exit_price: float
    amount: float        # 베이스 코인 수량
    notional: float
    pnl: float           # 수수료를 뺀 순손익
    fee: float
    exit_reason: str     # signal | stop | end
    conviction: float

    @property
    def is_win(self) -> bool:
        return self.pnl > 0

    @property
    def return_pct(self) -> float:
        return (self.pnl / self.notional * 100) if self.notional else 0.0


@dataclass
class BacktestResult:
    strategy: str
    symbol: str
    timeframe: str
    start_time: int = 0
    end_time: int = 0
    bars: int = 0
    start_equity: float = 0.0
    end_equity: float = 0.0
    trades: list[BacktestTrade] = field(default_factory=list)
    equity_curve: list[tuple[int, float]] = field(default_factory=list)
    max_drawdown_pct: float = 0.0
    total_fee: float = 0.0
    missed_entries: int = 0     # 지정가가 체결되지 않아 넘긴 신호

    @property
    def trade_count(self) -> int:
        return len(self.trades)

    @property
    def wins(self) -> int:
        return sum(1 for t in self.trades if t.is_win)

    @property
    def win_rate(self) -> float | None:
        return (self.wins / self.trade_count * 100) if self.trade_count else None

    @property
    def net_pnl(self) -> float:
        return self.end_equity - self.start_equity

    @property
    def return_pct(self) -> float:
        return (self.net_pnl / self.start_equity * 100) if self.start_equity else 0.0

    @property
    def profit_factor(self) -> float | None:
        """총이익 / 총손실. 1 미만이면 손해다."""
        gains = sum(t.pnl for t in self.trades if t.pnl > 0)
        losses = -sum(t.pnl for t in self.trades if t.pnl < 0)
        if losses <= 0:
            return None if gains <= 0 else float("inf")
        return gains / losses

    @property
    def avg_trade_pnl(self) -> float:
        return (sum(t.pnl for t in self.trades) / self.trade_count) if self.trade_count else 0.0


@dataclass
class _OpenTrade:
    side: PositionSide
    entry_time: int
    entry_price: float
    amount: float
    notional: float
    stop_loss: float
    entry_fee: float
    conviction: float


def run_backtest(
    candles: list[Candle],
    strategy: Strategy,
    config: Config,
    *,
    symbol: str = "BACKTEST",
    start_equity: float = 10_000.0,
    contract_size: float = 1.0,
    taker_fee: float = DEFAULT_TAKER_FEE,
    maker_fee: float = DEFAULT_MAKER_FEE,
    order_type: str = "market",
    limit_offset_pct: float = 0.02,
) -> BacktestResult:
    """전략을 과거 캔들에 돌린다.

    `order_type="limit"` 이면 진입·청산을 지정가로 흉내 낸다. 수수료가 maker 로
    낮아지는 대신, 봉이 그 가격을 지나가지 않으면 **체결되지 않고 신호를 놓친다**.
    이 맞바꿈이 실제로 이득인지 보라고 넣은 것이다.
    """
    result = BacktestResult(
        strategy=strategy.name,
        symbol=symbol,
        timeframe=config.trading.timeframe,
        start_equity=start_equity,
        end_equity=start_equity,
    )
    warmup = max(strategy.warmup_candles, 1)
    # 전략에 넘길 캔들 창의 크기.
    #
    # 전체 히스토리를 매 봉마다 넘기면 지표를 처음부터 다시 계산하게 되어
    # 전체가 O(n²) 이 된다. 90일치 5분봉(약 26,000봉)이면 사실상 못 돌린다.
    # 전략은 warmup 만큼만 뒤를 보므로 창을 잘라도 결과는 같다 — 다만 EMA 처럼
    # 재귀적인 지표는 시작점에 따라 값이 미세하게 달라지므로, 수렴할 만큼
    # 넉넉한 여유를 둔다.
    window = max(warmup * 3, warmup + 200)
    if len(candles) <= warmup + 1:
        log.warning("캔들이 %d개뿐이라 워밍업(%d)에 못 미칩니다", len(candles), warmup)
        return result

    risk = RiskManager(config.risk, leverage=config.exchange.leverage)
    equity = start_equity
    peak_equity = start_equity
    open_trade: _OpenTrade | None = None

    result.start_time = candles[warmup].timestamp
    result.end_time = candles[-1].timestamp
    result.bars = len(candles)

    # i 번째 봉의 종가까지 보고, i+1 번째 봉에서 체결한다.
    for i in range(warmup, len(candles) - 1):
        decision_bar = candles[i]
        fill_bar = candles[i + 1]

        # --- 1) 먼저 손절을 확인한다. 봉 안의 순서를 모르므로 불리하게 가정한다.
        if open_trade is not None:
            hit = (
                fill_bar.low <= open_trade.stop_loss
                if open_trade.side is PositionSide.LONG
                else fill_bar.high >= open_trade.stop_loss
            )
            if hit:
                equity, trade = _close(
                    open_trade, open_trade.stop_loss, fill_bar.timestamp,
                    equity, contract_size, taker_fee, "stop",
                )
                result.trades.append(trade)
                result.total_fee += trade.fee
                open_trade = None

        # --- 2) 전략 판단 (i 번째 봉까지만 본다)
        position = (
            Position(
                symbol=symbol, side=open_trade.side,
                contracts=open_trade.amount / contract_size,
                entry_price=open_trade.entry_price,
                notional=open_trade.notional,
            )
            if open_trade
            else Position.flat(symbol)
        )
        # +2 로 잘라야 전략의 closed_candles 가 i 번째 봉까지가 된다.
        visible = candles[max(0, i + 2 - window) : i + 2]
        context = StrategyContext(
            symbol=symbol,
            timeframe=config.trading.timeframe,
            candles=visible,
            ticker=Ticker(symbol=symbol, last=decision_bar.close, bid=None, ask=None,
                          timestamp=decision_bar.timestamp),
            position=position,
            equity=equity,
        )
        signal = strategy.generate(context)

        # --- 3) 체결
        if open_trade is not None and signal.action is SignalAction.EXIT:
            price, fee_rate = _fill_price(
                fill_bar,
                exit_side_is_sell=open_trade.side is PositionSide.LONG,
                order_type=order_type,
                limit_offset_pct=limit_offset_pct,
                taker_fee=taker_fee,
                maker_fee=maker_fee,
            )
            if price is not None:
                equity, trade = _close(
                    open_trade, price, fill_bar.timestamp, equity,
                    contract_size, fee_rate, "signal",
                )
                result.trades.append(trade)
                result.total_fee += trade.fee
                open_trade = None

        elif open_trade is None and signal.is_entry:
            is_long = signal.target_side is PositionSide.LONG
            price, fee_rate = _fill_price(
                fill_bar,
                exit_side_is_sell=not is_long,   # 롱 진입 = 매수
                order_type=order_type,
                limit_offset_pct=limit_offset_pct,
                taker_fee=taker_fee,
                maker_fee=maker_fee,
            )
            if price is None:
                result.missed_entries += 1
            else:
                sizing = risk.evaluate_entry(
                    signal=signal, entry_price=price, equity=equity, open_positions=0,
                )
                if sizing.approved:
                    entry_fee = sizing.notional * fee_rate
                    equity -= entry_fee
                    result.total_fee += entry_fee
                    open_trade = _OpenTrade(
                        side=signal.target_side,
                        entry_time=fill_bar.timestamp,
                        entry_price=price,
                        amount=sizing.base_amount,
                        notional=sizing.notional,
                        stop_loss=sizing.stop_loss,
                        entry_fee=entry_fee,
                        conviction=signal.strength,
                    )

        # --- 4) 평가금액 기록 (미실현 손익 포함) — 최대 낙폭은 이걸로 재야 정확하다
        marked = equity
        if open_trade is not None:
            direction = 1 if open_trade.side is PositionSide.LONG else -1
            marked += (fill_bar.close - open_trade.entry_price) * open_trade.amount * direction
        result.equity_curve.append((fill_bar.timestamp, marked))
        peak_equity = max(peak_equity, marked)
        if peak_equity > 0:
            drawdown = (peak_equity - marked) / peak_equity * 100
            result.max_drawdown_pct = max(result.max_drawdown_pct, drawdown)

    # 남은 포지션은 마지막 종가로 정리한다.
    if open_trade is not None:
        equity, trade = _close(
            open_trade, candles[-1].close, candles[-1].timestamp,
            equity, contract_size, taker_fee, "end",
        )
        result.trades.append(trade)
        result.total_fee += trade.fee

    result.end_equity = equity
    return result


def _fill_price(
    bar: Candle,
    *,
    exit_side_is_sell: bool,
    order_type: str,
    limit_offset_pct: float,
    taker_fee: float,
    maker_fee: float,
) -> tuple[float | None, float]:
    """이 봉에서 체결될 가격과 적용 수수료율.

    시장가는 봉 시가에 체결된다고 본다. 지정가는 시가에서 유리한 쪽으로
    offset 만큼 떨어진 곳에 걸고, **봉이 그 가격을 지나갈 때만** 체결된다 —
    지나가지 않으면 None 이다. 이 "놓친 거래" 를 무시하면 지정가 전략이 실제보다
    좋아 보인다.
    """
    if order_type != "limit":
        return bar.open, taker_fee

    offset = bar.open * (limit_offset_pct / 100.0)
    if exit_side_is_sell:
        # 파는 쪽 — 시가보다 위에 걸고, 고가가 거기까지 올라와야 체결된다.
        price = bar.open + offset
        return (price, maker_fee) if bar.high >= price else (None, maker_fee)
    price = bar.open - offset
    return (price, maker_fee) if bar.low <= price else (None, maker_fee)


def _close(
    trade: _OpenTrade,
    exit_price: float,
    exit_time: int,
    equity: float,
    contract_size: float,
    fee_rate: float,
    reason: str,
) -> tuple[float, BacktestTrade]:
    direction = 1 if trade.side is PositionSide.LONG else -1
    gross = (exit_price - trade.entry_price) * trade.amount * direction
    exit_fee = abs(exit_price * trade.amount) * fee_rate
    pnl = gross - exit_fee - trade.entry_fee
    # 진입 수수료는 진입 시점에 이미 뺐으므로 여기서는 총손익과 청산 수수료만 반영한다.
    new_equity = equity + gross - exit_fee
    return new_equity, BacktestTrade(
        side=trade.side.value,
        entry_time=trade.entry_time,
        exit_time=exit_time,
        entry_price=trade.entry_price,
        exit_price=exit_price,
        amount=trade.amount,
        notional=trade.notional,
        pnl=pnl,
        fee=trade.entry_fee + exit_fee,
        exit_reason=reason,
        conviction=trade.conviction,
    )
