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
    take_profit: float = 0.0           # 0 = 익절 없음
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
    long_orders: int = 0                # 롱 방향으로 낸 주문 횟수 (적립·상계 포함)
    short_orders: int = 0               # 숏 방향으로 낸 주문 횟수
    long_avg_price: float = 0.0         # 롱 주문들의 수량가중 평균 체결가
    short_avg_price: float = 0.0        # 숏 주문들의 수량가중 평균 체결가
    long_notional: float = 0.0          # 롱 주문 합계 금액 (USDT)
    short_notional: float = 0.0         # 숏 주문 합계 금액 (USDT)
    required_equity: float = 0.0        # 청산을 버티는 데 필요했던 최소 자기자본
    position_side: str = ""             # 현재 순포지션 방향 (long | short | "")
    position_amount: float = 0.0        # 현재 순포지션 수량 (베이스 코인)
    position_entry: float = 0.0         # 현재 순포지션 평균 단가
    position_notional: float = 0.0      # 현재 순포지션 명목가 (USDT)
    error: str | None = None

    @property
    def realized_pnl(self) -> float:
        """닫힌 거래에서 확정된 손익. 화면에서 평가손익과 분리해 보여준다 —
        합쳐 놓으면 '보유 포지션이 물려 있는 것'과 '거래로 잃은 것'이 섞여
        어디서 손실이 나는지 읽을 수 없다."""
        return self.equity - self.start_equity

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
        extra_strategies: dict[str, Strategy] | None = None,
    ) -> None:
        self.config = config
        self.store = store
        self.start_equity = start_equity
        self.taker_fee = taker_fee
        self.risk = RiskManager(config.risk, leverage=config.exchange.leverage)

        # 기본은 등록된 전략 전부. 주입하면 그 목록만 경쟁시킨다.
        self._strategies: dict[str, Strategy] = dict(strategies) if strategies else {}
        # 카탈로그 밖의 참가자(AI 수동 매매 등). 카탈로그 빌드와 무관하게
        # 합류하며, 저장된 포지션 복원 전에 넣어야 그들의 포지션도 살아난다.
        if extra_strategies:
            self._strategies.update(extra_strategies)
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
                take_profit=row["take_profit"] if "take_profit" in row.keys() else 0.0,
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

        # 3) 익절 확인. 같은 봉에서 손절과 익절이 둘 다 스쳤으면 손절이 먼저
        #    걸린 것으로 본다 — 봉 안의 순서를 모르므로 불리한 쪽을 가정한다.
        if position is not None and position.take_profit > 0:
            best = price
            if candles:
                best = (
                    candles[-1].high if position.side is PositionSide.LONG
                    else candles[-1].low
                )
            reached = (
                best >= position.take_profit
                if position.side is PositionSide.LONG
                else best <= position.take_profit
            )
            if reached:
                self._close(name, position, position.take_profit, now_ms, "target")
                position = None

        # 4) 전략 판단
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

        # 5) 체결
        if position is not None:
            if signal.action is SignalAction.EXIT:
                self._close(name, position, price, now_ms, "signal")
            elif (
                signal.is_entry
                and signal.target_side is not position.side
                and not signal.metadata.get("accumulate")
            ):
                # 방향이 뒤집혔다 — 닫고 새로 연다.
                self._close(name, position, price, now_ms, "reverse")
                self._open(name, symbol, signal, price, now_ms, equity)
            elif (
                signal.is_entry
                and signal.target_side is position.side
                and (signal.metadata.get("accumulate") or signal.metadata.get("pyramid"))
            ):
                # 같은 방향 추가 진입(적립·물타기). 전략이 metadata 로 명시할
                # 때만 허용한다 — 일반 전략의 중복 진입 신호가 적립이 되면
                # 안 된다. 평균 단가와 수량이 합쳐진다.
                self._add(name, position, signal, price, now_ms, equity)
            elif (
                signal.is_entry
                and signal.target_side is not position.side
                and signal.metadata.get("accumulate")
            ):
                # 반대 방향 적립 = 상계. 실거래의 단방향 모드와 같은 규칙으로,
                # 주문 금액만큼 순포지션이 줄고, 넘치면 뒤집힌다.
                self._net_reduce(name, position, signal, price, now_ms, equity)
        elif signal.is_entry:
            self._open(name, symbol, signal, price, now_ms, equity)

        # 최대 낙폭을 위해 고점을 갱신한다. 실현손익은 위에서 이미 집계했으므로
        # 이번 주기에 닫힌 거래만 더하면 되지만, 정확성을 위해 다시 읽는다 —
        # 주기가 15초라 비용보다 정확성이 중요하다.
        open_now = [
            p for (owner, _), p in self._positions.items() if owner == name
        ]
        unrealized = sum(p.unrealized_net(price, self.taker_fee) for p in open_now)
        marked = self._equity(name, account) + unrealized
        if marked > account["peak_equity"]:
            self.store.update_paper_peak(name, marked)

        # "얼마가 있어야 청산을 안 당했는가" — 이 실험의 핵심 답. 매 주기,
        # 포지션 증거금(명목가/레버리지)에 그 순간의 평가손실을 더한 값이
        # 그 순간 계좌에 있어야 했던 최소 자기자본이고, 러닝 맥스를 남긴다.
        exposure = sum(abs(price * p.amount) for p in open_now)
        needed = exposure / max(self.config.exchange.leverage, 1.0) + max(
            0.0, -unrealized
        )
        if needed > account.get("required_equity", 0.0):
            self.store.update_paper_required(name, needed)

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
            take_profit=decision.take_profit or 0.0,
        )
        self._positions[(name, symbol)] = position
        # next_funding_ms 는 다음 주기의 _settle_funding 이 채운다. 방금 연
        # 포지션은 아직 정산 시각을 지나지 않았으므로 그래도 된다.
        self._save_position(name, position)
        self._record_order(name, symbol, signal.target_side.value, price,
                           decision.base_amount, decision.notional, now_ms, "open")

    def _add(
        self,
        name: str,
        position: PaperPosition,
        signal: Signal,
        price: float,
        now_ms: int,
        equity: float,
    ) -> None:
        """보유 포지션에 같은 방향으로 수량을 더한다 (평균 단가 갱신).

        손절가는 첫 진입 때 것을 유지한다 — 물타기가 손절까지 미루기 시작하면
        전략이 의도한 마지막 안전판이 사라진다. 실거래 실행기는 추가 진입을
        지원하지 않으므로 이 동작은 모의매매 전용이다.
        """
        decision = self.risk.evaluate_entry(
            signal=signal, entry_price=price, equity=equity, open_positions=0
        )
        if not decision.approved:
            return

        add_amount = decision.base_amount
        add_fee = decision.notional * self.taker_fee
        total_amount = position.amount + add_amount
        if total_amount <= 0:
            return
        position.entry_price = (
            position.entry_price * position.amount + price * add_amount
        ) / total_amount
        position.amount = total_amount
        position.notional += decision.notional
        position.entry_fee += add_fee
        self._save_position(name, position)
        self._record_order(name, position.symbol, signal.target_side.value, price,
                           add_amount, decision.notional, now_ms, "add")

    def _net_reduce(
        self,
        name: str,
        position: PaperPosition,
        signal: Signal,
        price: float,
        now_ms: int,
        equity: float,
    ) -> None:
        """반대 방향 적립 주문으로 포지션을 상계한다.

        주문 금액이 보유 수량보다 작으면 그만큼만 줄이고(부분 실현), 크면 전량
        청산 후 남는 수량으로 반대 포지션을 연다 — 실거래 단방향 모드의 넷팅과
        같은 규칙이다. 줄어든 부분의 진입 수수료·펀딩비는 비례로 함께 실현한다.
        """
        decision = self.risk.evaluate_entry(
            signal=signal, entry_price=price, equity=equity, open_positions=0
        )
        if not decision.approved:
            return

        reduce_amount = min(decision.base_amount, position.amount)
        if reduce_amount <= 0:
            return
        # 상계도 주문은 주문이다 — 신호 방향(반대쪽)으로 한 건 기록한다.
        self._record_order(name, position.symbol, signal.target_side.value, price,
                           decision.base_amount, decision.notional, now_ms, "net")
        order_fee = decision.base_amount * price * self.taker_fee
        reduce_fee = order_fee * (reduce_amount / decision.base_amount)
        portion = reduce_amount / position.amount

        direction = 1 if position.side is PositionSide.LONG else -1
        gross = (price - position.entry_price) * reduce_amount * direction
        entry_fee_part = position.entry_fee * portion
        funding_part = position.funding_paid * portion
        self.store.record_paper_trade({
            "strategy": name,
            "symbol": position.symbol,
            "side": position.side.value,
            "opened_at": position.opened_at,
            "closed_at": now_ms,
            "entry_price": position.entry_price,
            "exit_price": price,
            "amount": reduce_amount,
            "notional": position.notional * portion,
            "pnl": gross - reduce_fee - entry_fee_part - funding_part,
            "fee": reduce_fee + entry_fee_part,
            "funding": funding_part,
            "exit_reason": "net",
            "conviction": position.conviction,
            "worst_excursion_pct": max(
                position.worst_excursion_pct, position.excursion_pct(price)
            ),
        })

        remainder = decision.base_amount - reduce_amount
        if portion >= 1.0:
            # 전량 상계됐다. 남는 수량이 있으면 반대 포지션으로 뒤집힌다.
            self._positions.pop((name, position.symbol), None)
            self.store.delete_paper_position(name, position.symbol)
            if remainder * price >= 1.0:   # 1 USDT 미만의 부스러기는 무시
                flipped = PaperPosition(
                    symbol=position.symbol,
                    side=signal.target_side,
                    opened_at=now_ms,
                    entry_price=price,
                    amount=remainder,
                    notional=remainder * price,
                    stop_loss=decision.stop_loss,
                    entry_fee=order_fee - reduce_fee,
                    conviction=signal.strength,
                    take_profit=decision.take_profit or 0.0,
                )
                self._positions[(name, position.symbol)] = flipped
                self._save_position(name, flipped)
            return

        position.amount -= reduce_amount
        position.notional *= 1 - portion
        position.entry_fee -= entry_fee_part
        position.funding_paid -= funding_part
        self._save_position(name, position)

    def _record_order(
        self, name: str, symbol: str, side: str, price: float,
        amount: float, notional: float, now_ms: int, kind: str,
    ) -> None:
        """주문 한 건을 남긴다. 적립식은 주문 여러 개가 포지션 하나로 합쳐지므로
        "롱을 몇 번, 숏을 몇 번 잡았는지" 는 여기서만 셀 수 있다."""
        self.store.record_paper_order({
            "strategy": name, "symbol": symbol, "ts": now_ms, "side": side,
            "price": price, "amount": amount, "notional": notional, "kind": kind,
        })

    def _save_position(self, name: str, position: PaperPosition) -> None:
        self.store.save_paper_position(name, position.symbol, {
            "side": position.side.value, "opened_at": position.opened_at,
            "entry_price": position.entry_price, "amount": position.amount,
            "notional": position.notional, "stop_loss": position.stop_loss,
            "entry_fee": position.entry_fee, "conviction": position.conviction,
            "worst_excursion_pct": position.worst_excursion_pct,
            "funding_paid": position.funding_paid,
            "next_funding_ms": position.next_funding_ms,
            "take_profit": position.take_profit,
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
        order_stats = self.store.paper_order_stats()
        rows: list[StrategyStats] = []

        for name in self._strategies:
            account = accounts.get(name)
            if account is None:
                continue
            trades = self.store.paper_trades(name)
            entry = catalog.get(name, {})
            # 카탈로그 밖의 참가자(AI 수동 매매)는 전략 객체가 들고 있는
            # 표기를 쓴다.
            strategy_obj = self._strategies.get(name)
            stats = StrategyStats(
                name=name,
                summary=entry.get("summary", "") or getattr(strategy_obj, "summary", ""),
                category=entry.get("category") or getattr(strategy_obj, "category", "other"),
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
                long_orders=int(order_stats.get(name, {}).get("long", {}).get("count", 0)),
                short_orders=int(order_stats.get(name, {}).get("short", {}).get("count", 0)),
                long_avg_price=order_stats.get(name, {}).get("long", {}).get("avg_price", 0.0),
                short_avg_price=order_stats.get(name, {}).get("short", {}).get("avg_price", 0.0),
                long_notional=order_stats.get(name, {}).get("long", {}).get("notional", 0.0),
                short_notional=order_stats.get(name, {}).get("short", {}).get("notional", 0.0),
                required_equity=(
                    account["required_equity"]
                    if "required_equity" in account else 0.0
                ),
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
            if open_positions:
                # 심볼 하나만 굴리므로 첫 포지션이 곧 순포지션이다.
                held = open_positions[0]
                stats.position_side = held.side.value
                stats.position_amount = held.amount
                stats.position_entry = held.entry_price
                stats.position_notional = abs(
                    prices.get(held.symbol, held.entry_price) * held.amount
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
