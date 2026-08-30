"""전략 경쟁 모의매매.

등록된 **모든** 전략을 동시에 굴린다. 실거래는 설정에 지정한 전략 하나만 하지만,
나머지도 같은 시세를 보며 가상으로 매매해서 성적을 남긴다. 그래야 "지금 쓰는 게
제일 나은가" 를 실시간 데이터로 판단할 수 있다.

새 전략을 추가하면 다음 기동 때 자동으로 합류하고, **자기 시작 시점부터** 수익률이
계산된다. 늦게 합류한 전략이 앞선 전략의 성적을 물려받지 않는다.

몇 가지는 일부러 불리하게 잡았다 — 모의 성적이 실제보다 좋아 보이면 판단이
어긋난다:

* 수수료는 항상 taker(0.05%)로 계산한다. 지정가로 체결됐을 수도 있지만 그렇게
  가정하면 성적이 부풀려진다.
* 진입·청산은 현재가에 즉시 체결된다고 본다. 슬리피지는 반영하지 않는다.
* 손절은 봉의 저가/고가까지 확인한다 — 폴링 사이에 스쳤어도 걸린 것으로 본다.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone

from bot.config import Config
from bot.models import (
    Candle,
    FundingRate,
    Position,
    PositionSide,
    Signal,
    SignalAction,
    Ticker,
)
from bot.risk import RiskManager
from bot.store import Store
from bot.strategies import Strategy, StrategyContext, get_strategy, strategy_catalog

log = logging.getLogger(__name__)

DEFAULT_START_EQUITY = 10_000.0
TAKER_FEE = 0.0005   # 0.05% — 모의매매는 항상 불리한 쪽으로 잡는다

# 거래소가 펀딩비율을 알려 주지 않을 때 쓰는 값. 이때는 방향과 무관하게 항상
# 비용으로 잡는다 — 모르는 값을 수입으로 계산해 성적이 좋아 보이면 안 된다.
DEFAULT_FUNDING_RATE = 0.0001   # 0.01% / 8시간 (하루 0.03%)
DEFAULT_FUNDING_INTERVAL_HOURS = 8.0


@dataclass
class PaperPosition:
    symbol: str
    side: PositionSide
    opened_at: int
    entry_price: float
    amount: float          # 베이스 코인 수량
    notional: float
    stop_loss: float
    entry_fee: float
    conviction: float
    worst_excursion_pct: float = 0.0   # 진입가 대비 최대 역행폭(%)
    funding_paid: float = 0.0          # 보유하는 동안 낸 펀딩비 누계(수입이면 음수)
    next_funding_ms: int = 0           # 다음 정산 시각 (0 = 아직 정하지 않음)

    def unrealized(self, price: float) -> float:
        direction = 1 if self.side is PositionSide.LONG else -1
        return (price - self.entry_price) * self.amount * direction

    def unrealized_net(self, price: float, taker_fee: float) -> float:
        """지금 닫으면 실제로 손에 남는 금액.

        이미 낸 진입 수수료와 닫을 때 낼 수수료, 그리고 보유하는 동안 정산된
        펀딩비까지 뺀다. 이것을 빼지 않으면 보유 중인 전략의 성적만 좋아 보이고,
        그래서 순위표에서 회전이 잦은 전략과 오래 들고 가는 전략이 둘 다 실제보다
        유리하게 나온다.
        """
        exit_fee = abs(price * self.amount) * taker_fee
        return self.unrealized(price) - self.entry_fee - exit_fee - self.funding_paid

    def excursion_pct(self, price: float) -> float:
        """진입가 대비 불리한 쪽으로 얼마나 갔는지(%). 유리하면 0."""
        direction = 1 if self.side is PositionSide.LONG else -1
        move = (price - self.entry_price) / self.entry_price * 100 * direction
        return max(0.0, -move)


@dataclass
class StrategyStats:
    """순위표 한 줄."""

    name: str
    summary: str = ""
    category: str = "other"
    started_at: int = 0
    start_equity: float = DEFAULT_START_EQUITY
    equity: float = DEFAULT_START_EQUITY
    open_positions: int = 0
    unrealized: float = 0.0
    trade_count: int = 0
    wins: int = 0
    losses: int = 0
    stop_outs: int = 0
    total_fee: float = 0.0
    total_funding: float = 0.0   # 보유하는 동안 낸 펀딩비 (수입이면 음수)
    best_pnl: float = 0.0
    worst_pnl: float = 0.0
    max_drawdown_pct: float = 0.0
    liquidation_risk_pct: float = 0.0   # 청산가까지 간 비율의 최댓값
    error: str | None = None

    @property
    def net_pnl(self) -> float:
        return self.equity + self.unrealized - self.start_equity

    @property
    def return_pct(self) -> float:
        return (self.net_pnl / self.start_equity * 100) if self.start_equity else 0.0

    @property
    def win_rate(self) -> float | None:
        return (self.wins / self.trade_count * 100) if self.trade_count else None

    @property
    def stop_out_rate(self) -> float | None:
        """손절로 끝난 비율. 높으면 손절이 타이트하거나 진입 타이밍이 나쁘다."""
        return (self.stop_outs / self.trade_count * 100) if self.trade_count else None


class PaperArena:
    """등록된 모든 전략을 동시에 모의매매로 굴린다."""

    def __init__(
        self,
        config: Config,
        store: Store,
        *,
        start_equity: float = DEFAULT_START_EQUITY,
        taker_fee: float = TAKER_FEE,
        strategies: dict[str, Strategy] | None = None,
    ) -> None:
        self.config = config
        self.store = store
        self.start_equity = start_equity
        self.taker_fee = taker_fee
        self.risk = RiskManager(config.risk, leverage=config.exchange.leverage)

        # 기본은 등록된 전략 전부. 주입하면 그 목록만 경쟁시킨다.
        self._strategies: dict[str, Strategy] = dict(strategies) if strategies else {}
        self._positions: dict[tuple[str, str], PaperPosition] = {}
        self._errors: dict[str, str] = {}
        self._load(build_strategies=strategies is None)

    # ------------------------------------------------------------------
    def _load(self, *, build_strategies: bool = True) -> None:
        """전략을 준비하고, 저장된 가상 포지션을 복원한다."""
        if build_strategies:
            for entry in strategy_catalog():
                if not entry["summary"]:
                    continue   # hold/template 은 경쟁에서 뺀다
                try:
                    self._strategies[entry["name"]] = get_strategy(entry["name"])
                except Exception as exc:
                    self._errors[entry["name"]] = str(exc)
                    log.warning("모의매매에서 '%s' 전략을 만들 수 없습니다: %s",
                                entry["name"], exc)

        for row in self.store.paper_positions():
            if row["strategy"] not in self._strategies:
                continue   # 코드에서 사라진 전략의 포지션은 무시한다
            self._positions[(row["strategy"], row["symbol"])] = PaperPosition(
                symbol=row["symbol"],
                side=PositionSide(row["side"]),
                opened_at=row["opened_at"],
                entry_price=row["entry_price"],
                amount=row["amount"],
                notional=row["notional"],
                stop_loss=row["stop_loss"],
                entry_fee=row["entry_fee"],
                conviction=row["conviction"],
                worst_excursion_pct=row["worst_excursion_pct"],
                # 예전 DB 에는 없던 열이다. 마이그레이션으로 채워지지만
                # 안전하게 기본값을 둔다.
                funding_paid=row["funding_paid"] if "funding_paid" in row.keys() else 0.0,
                next_funding_ms=(
                    row["next_funding_ms"] if "next_funding_ms" in row.keys() else 0
                ),
            )
        log.info(
            "모의매매 준비 — 전략 %d개, 진행 중인 가상 포지션 %d개",
            len(self._strategies), len(self._positions),
        )

    @property
    def strategy_names(self) -> list[str]:
        return sorted(self._strategies)

    @property
    def extra_timeframes(self) -> set[str]:
        """경쟁 중인 전략들이 필요로 하는 상위 시간대의 합집합."""
        out: set[str] = set()
        for strategy in self._strategies.values():
            out.update(strategy.extra_timeframes)
        return out

    # ------------------------------------------------------------------
    def step(
        self,
        symbol: str,
        candles: list[Candle],
        ticker: Ticker,
        funding: FundingRate | None = None,
        *,
        mtf_candles: dict[str, list[Candle]] | None = None,
    ) -> None:
        """한 주기. 모든 전략에 같은 시세를 먹인다.

        전략 하나가 터져도 나머지는 계속 굴러야 한다 — 비교가 목적이므로 한
        전략의 버그로 전체 데이터를 잃으면 안 된다.
        """
        now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
        for name, strategy in self._strategies.items():
            try:
                self._step_one(
                    name, strategy, symbol, candles, ticker, now_ms, funding,
                    mtf_candles=mtf_candles,
                )
                self._errors.pop(name, None)
            except Exception as exc:
                self._errors[name] = str(exc)
                log.exception("모의매매 '%s' 전략 오류 — 나머지는 계속합니다", name)

    def _step_one(
        self,
        name: str,
        strategy: Strategy,
        symbol: str,
        candles: list[Candle],
        ticker: Ticker,
        now_ms: int,
        funding: FundingRate | None = None,
        *,
        mtf_candles: dict[str, list[Candle]] | None = None,
    ) -> None:
        account = self.store.paper_account(
            name, start_equity=self.start_equity, now_ms=now_ms
        )
        position = self._positions.get((name, symbol))
        price = ticker.last

        # 1) 펀딩비 정산. 정산 시각을 지났으면 손절을 보기 전에 먼저 반영한다 —
        #    실제 거래소도 보유 중인 포지션에 그대로 부과한다.
        if position is not None:
            self._settle_funding(name, position, price, now_ms, funding)

        # 2) 손절 확인. 폴링 사이에 스쳤을 수 있으므로 진행 중인 봉의 저가/고가까지 본다.
        if position is not None:
            worst = price
            if candles:
                worst = candles[-1].low if position.side is PositionSide.LONG else candles[-1].high
            position.worst_excursion_pct = max(
                position.worst_excursion_pct, position.excursion_pct(worst)
            )
            hit = (
                worst <= position.stop_loss
                if position.side is PositionSide.LONG
                else worst >= position.stop_loss
            )
            if hit:
                self._close(name, position, position.stop_loss, now_ms, "stop")
                position = None

        # 3) 전략 판단
        equity = self._equity(name, account)
        model_position = (
            Position(
                symbol=symbol, side=position.side, contracts=position.amount,
                entry_price=position.entry_price, notional=position.notional,
                unrealized_pnl=position.unrealized(price),
            )
            if position
            else Position.flat(symbol)
        )
        signal = strategy.generate(
            StrategyContext(
                symbol=symbol,
                timeframe=self.config.trading.timeframe,
                candles=candles,
                ticker=ticker,
                position=model_position,
                equity=equity,
                mtf_candles=mtf_candles or {},
            )
        )

        # 4) 체결
        if position is not None:
            if signal.action is SignalAction.EXIT:
                self._close(name, position, price, now_ms, "signal")
            elif signal.is_entry and signal.target_side is not position.side:
                # 방향이 뒤집혔다 — 닫고 새로 연다.
                self._close(name, position, price, now_ms, "reverse")
                self._open(name, symbol, signal, price, now_ms, equity)
            return

        if signal.is_entry:
            self._open(name, symbol, signal, price, now_ms, equity)

        # 최대 낙폭을 위해 고점을 갱신한다. 실현손익은 위에서 이미 집계했으므로
        # 이번 주기에 닫힌 거래만 더하면 되지만, 정확성을 위해 다시 읽는다 —
        # 주기가 15초라 비용보다 정확성이 중요하다.
        marked = self._equity(name, account) + sum(
            p.unrealized_net(price, self.taker_fee)
            for (owner, _), p in self._positions.items()
            if owner == name
        )
        if marked > account["peak_equity"]:
            self.store.update_paper_peak(name, marked)

    # ------------------------------------------------------------------
    def _settle_funding(
        self,
        name: str,
        position: PaperPosition,
        price: float,
        now_ms: int,
        funding: FundingRate | None,
    ) -> None:
        """정산 시각을 지났으면 펀딩비를 부과한다.

        실제 거래소와 같은 방식으로 **8시간마다 한 번씩만** 부과한다. 보유 시간에
        비례해 조금씩 떼면 정산 시각을 넘기지 않은 짧은 매매까지 비용을 무는데,
        그건 실제로는 내지 않는 돈이다.

        비율이 양수면 롱이 숏에게 낸다. 거래소가 비율을 알려 주지 않으면 방향과
        무관하게 기본값을 비용으로 잡는다 — 모르는 값을 수입으로 계산해 성적이
        좋아 보이는 쪽이 훨씬 위험하다.
        """
        interval_ms = int(
            (funding.interval_hours if funding else DEFAULT_FUNDING_INTERVAL_HOURS)
            * 3_600_000
        )
        if interval_ms <= 0:
            return

        # 진입 시각보다 이른 정산 시각은 있을 수 없다 — 아직 정하지 않았거나
        # (0) 값이 망가진 경우다. 그대로 두면 아래 루프가 1970년부터 밀린 것으로
        # 계산해 터무니없는 금액을 문다.
        if position.next_funding_ms <= position.opened_at:
            position.next_funding_ms = self._next_funding_ms(now_ms, interval_ms, funding)
            self._save_position(name, position)
            return

        charged = 0.0
        # 봇이 오래 멈춰 있었다면 여러 번 밀려 있을 수 있다. 밀린 만큼 전부 문다.
        while now_ms >= position.next_funding_ms:
            notional = abs(price * position.amount)
            if funding is not None:
                direction = 1.0 if position.side is PositionSide.LONG else -1.0
                charged += funding.rate * notional * direction
            else:
                charged += DEFAULT_FUNDING_RATE * notional
            position.next_funding_ms += interval_ms

        if charged == 0.0:
            return
        position.funding_paid += charged
        self._save_position(name, position)
        log.debug(
            "모의매매 '%s' 펀딩비 %.4f 정산 (누계 %.4f)",
            name, charged, position.funding_paid,
        )

    @staticmethod
    def _next_funding_ms(
        now_ms: int, interval_ms: int, funding: FundingRate | None
    ) -> int:
        """다음 정산 시각. 거래소가 알려 주면 그 값을, 아니면 8시간 경계를 쓴다."""
        if funding is not None and funding.next_time_ms:
            if funding.next_time_ms > now_ms:
                return int(funding.next_time_ms)
        # epoch 기준 8시간 경계는 UTC 00/08/16시와 정확히 맞는다.
        return ((now_ms // interval_ms) + 1) * interval_ms

    # ------------------------------------------------------------------
    def _open(
        self, name: str, symbol: str, signal: Signal, price: float, now_ms: int, equity: float
    ) -> None:
        open_count = sum(1 for owner, _ in self._positions if owner == name)
        decision = self.risk.evaluate_entry(
            signal=signal, entry_price=price, equity=equity, open_positions=open_count
        )
        if not decision.approved:
            return

        entry_fee = decision.notional * self.taker_fee
        position = PaperPosition(
            symbol=symbol,
            side=signal.target_side,
            opened_at=now_ms,
            entry_price=price,
            amount=decision.base_amount,
            notional=decision.notional,
            stop_loss=decision.stop_loss,
            entry_fee=entry_fee,
            conviction=signal.strength,
        )
        self._positions[(name, symbol)] = position
        # next_funding_ms 는 다음 주기의 _settle_funding 이 채운다. 방금 연
        # 포지션은 아직 정산 시각을 지나지 않았으므로 그래도 된다.
        self._save_position(name, position)

    def _save_position(self, name: str, position: PaperPosition) -> None:
        self.store.save_paper_position(name, position.symbol, {
            "side": position.side.value, "opened_at": position.opened_at,
            "entry_price": position.entry_price, "amount": position.amount,
            "notional": position.notional, "stop_loss": position.stop_loss,
            "entry_fee": position.entry_fee, "conviction": position.conviction,
            "worst_excursion_pct": position.worst_excursion_pct,
            "funding_paid": position.funding_paid,
            "next_funding_ms": position.next_funding_ms,
        })

    def _close(
        self, name: str, position: PaperPosition, price: float, now_ms: int, reason: str
    ) -> None:
        gross = position.unrealized(price)
        exit_fee = abs(price * position.amount) * self.taker_fee
        self.store.record_paper_trade({
            "strategy": name,
            "symbol": position.symbol,
            "side": position.side.value,
            "opened_at": position.opened_at,
            "closed_at": now_ms,
            "entry_price": position.entry_price,
            "exit_price": price,
            "amount": position.amount,
            "notional": position.notional,
            "pnl": gross - exit_fee - position.entry_fee - position.funding_paid,
            "fee": exit_fee + position.entry_fee,
            "funding": position.funding_paid,
            "exit_reason": reason,
            "conviction": position.conviction,
            "worst_excursion_pct": max(
                position.worst_excursion_pct, position.excursion_pct(price)
            ),
        })
        self._positions.pop((name, position.symbol), None)
        self.store.delete_paper_position(name, position.symbol)

    def _equity(self, name: str, account: dict) -> float:
        """실현 손익까지 반영한 가상 자기자본."""
        realized = sum(t["pnl"] for t in self.store.paper_trades(name))
        return account["start_equity"] + realized

    # ------------------------------------------------------------------
    def leaderboard(self, price_hint: dict[str, float] | None = None) -> list[StrategyStats]:
        """전략별 성적. 수익률 내림차순."""
        prices = price_hint or {}
        # 청산까지의 거리. 격리 마진에서 대략 1/레버리지 만큼 움직이면 청산된다.
        liquidation_distance = 100.0 / max(self.config.exchange.leverage, 1.0)

        catalog = {e["name"]: e for e in strategy_catalog()}
        accounts = {a["strategy"]: a for a in self.store.paper_accounts()}
        rows: list[StrategyStats] = []

        for name in self._strategies:
            account = accounts.get(name)
            if account is None:
                continue
            trades = self.store.paper_trades(name)
            entry = catalog.get(name, {})
            stats = StrategyStats(
                name=name,
                summary=entry.get("summary", ""),
                category=entry.get("category", "other"),
                started_at=account["started_at"],
                start_equity=account["start_equity"],
                equity=account["start_equity"] + sum(t["pnl"] for t in trades),
                trade_count=len(trades),
                wins=sum(1 for t in trades if t["pnl"] > 0),
                losses=sum(1 for t in trades if t["pnl"] <= 0),
                stop_outs=sum(1 for t in trades if t["exit_reason"] == "stop"),
                total_fee=sum(t["fee"] for t in trades),
                total_funding=sum(t["funding"] for t in trades),
                best_pnl=max((t["pnl"] for t in trades), default=0.0),
                worst_pnl=min((t["pnl"] for t in trades), default=0.0),
                error=self._errors.get(name),
            )

            open_positions = [
                p for (owner, _), p in self._positions.items() if owner == name
            ]
            stats.open_positions = len(open_positions)
            stats.unrealized = sum(
                p.unrealized_net(prices.get(p.symbol, p.entry_price), self.taker_fee)
                for p in open_positions
            )

            peak = max(account["peak_equity"], stats.equity + stats.unrealized)
            if peak > 0:
                stats.max_drawdown_pct = max(
                    0.0, (peak - (stats.equity + stats.unrealized)) / peak * 100
                )

            worst_excursion = max(
                [t["worst_excursion_pct"] for t in trades]
                + [p.worst_excursion_pct for p in open_positions]
                + [0.0]
            )
            # 청산가까지의 거리 대비 얼마나 갔는지. 100% 면 청산이다.
            stats.liquidation_risk_pct = (
                worst_excursion / liquidation_distance * 100 if liquidation_distance else 0.0
            )
            rows.append(stats)

        rows.sort(key=lambda s: s.return_pct, reverse=True)
        return rows

    def reset(self) -> None:
        self.store.reset_paper()
        self._positions.clear()
