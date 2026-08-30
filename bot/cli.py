"""커맨드라인 진입점.

    python -m bot check                 # 연결·잔고·심볼 점검 (주문 없음)
    python -m bot strategies            # 등록된 전략 목록
    python -m bot positions             # 현재 포지션
    python -m bot run                   # DRY-RUN 으로 루프 실행
    python -m bot run --live            # 실거래로 루프 실행
    python -m bot close --live          # 모든 포지션 시장가 청산
    python -m bot hash-password         # 웹 대시보드 비밀번호 해시 생성
    python -m bot web                   # 웹 대시보드 서버 실행
"""

from __future__ import annotations

import argparse
import getpass
import logging
import os
import sys
from pathlib import Path

from bot.config import Config, ConfigError, Credentials
from bot.engine import TradingEngine
from bot.exchanges import create_exchange
from bot.exchanges.base import ExchangeError
from bot.logging_utils import setup_logging
from bot.models import Signal, SignalAction


log = logging.getLogger(__name__)

DEFAULT_CONFIG = "config.yaml"
STATIC_DIR = Path(__file__).parent / "web" / "static"
LOCAL_HOSTS = ("127.0.0.1", "localhost", "::1")


def _env_flag(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in ("1", "true", "yes", "on")


def _on_railway() -> bool:
    """Railway 컨테이너 안에서 도는지 판단한다.

    Railway 는 이 변수들을 자동으로 넣어 준다. 여기서 참이면 컨테이너는 이미
    Railway 의 엣지 프록시 뒤에 있으므로, 0.0.0.0 바인딩이 정상이고
    X-Forwarded-For 를 신뢰해야 접속자를 구분할 수 있다.
    """
    return any(
        os.getenv(name) for name in ("RAILWAY_ENVIRONMENT", "RAILWAY_ENVIRONMENT_NAME",
                                     "RAILWAY_PROJECT_ID", "RAILWAY_SERVICE_ID")
    )



def load_dotenv(path: str | Path = ".env") -> None:
    """`.env` 의 KEY=VALUE 를 환경변수로 읽어들인다(이미 있는 값은 유지).

    의존성을 하나 줄이려고 직접 파싱한다. 주석과 따옴표만 처리하면 충분하다.
    """
    p = Path(path)
    if not p.exists():
        return
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="bot", description="Bitget/OKX USDT 무기한 선물 자동매매 봇"
    )
    parser.add_argument("-c", "--config", default=DEFAULT_CONFIG, help="설정 YAML 경로")
    parser.add_argument("--env-file", default=".env", help="환경변수 파일 경로")
    parser.add_argument("--log-level", default=None, help="설정의 로그 레벨을 덮어씀")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("check", help="API 연결, 잔고, 심볼 규격을 점검한다 (주문 없음)")
    sub.add_parser("strategies", help="등록된 전략과 설명을 출력한다")

    detail = sub.add_parser("strategy", help="전략 하나의 자세한 설명을 출력한다")
    detail.add_argument("name", help="전략 이름")
    sub.add_parser("positions", help="설정된 심볼의 현재 포지션을 출력한다")

    run_cmd = sub.add_parser("run", help="트레이딩 루프를 실행한다")
    run_cmd.add_argument(
        "--live",
        action="store_true",
        help="실제 주문을 전송한다. 이 플래그가 없으면 DRY-RUN 으로만 동작한다.",
    )
    run_cmd.add_argument(
        "--yes", action="store_true", help="--live 확인 프롬프트를 건너뛴다 (무인 실행용)"
    )

    close_cmd = sub.add_parser("close", help="보유 포지션을 시장가로 전부 청산한다")
    close_cmd.add_argument("--live", action="store_true", help="실제 청산 주문을 전송한다")
    close_cmd.add_argument("--yes", action="store_true", help="확인 프롬프트를 건너뛴다")

    sub.add_parser(
        "hash-password", help="웹 대시보드용 비밀번호 해시를 만든다 (WEB_PASSWORD_HASH)"
    )

    sub.add_parser(
        "setup-2fa",
        help="2단계 인증 비밀키와 QR 코드를 만든다 (WEB_TOTP_SECRET)",
    )

    sub.add_parser(
        "check-env",
        help="지금 이 환경에 어떤 환경변수가 들어와 있는지 점검한다 (값은 출력하지 않음)",
    )

    bt = sub.add_parser("backtest", help="과거 캔들로 전략을 시험한다")
    bt.add_argument("--strategy", help="전략 이름. 생략하면 전체를 비교한다")
    bt.add_argument("--days", type=int, default=90, help="과거 며칠 (기본 90)")
    bt.add_argument("--timeframe", help="봉 주기. 생략하면 설정값을 쓴다")
    bt.add_argument("--symbol", help="심볼. 생략하면 설정의 첫 심볼")
    bt.add_argument("--equity", type=float, default=10_000.0, help="시작 자기자본")
    bt.add_argument(
        "--order-type", choices=["market", "limit"], default=None,
        help="limit 이면 지정가를 흉내 낸다 — 수수료가 낮은 대신 체결되지 않는 신호가 생긴다",
    )
    bt.add_argument("--taker-fee", type=float, default=0.05, help="taker 수수료 %% (기본 0.05)")
    bt.add_argument("--maker-fee", type=float, default=0.02, help="maker 수수료 %% (기본 0.02)")
    bt.add_argument(
        "--funding-rate", type=float, default=0.01,
        help="8시간당 펀딩비 %% 가정 (기본 0.01, 0 이면 계산하지 않음)",
    )

    web_cmd = sub.add_parser("web", help="웹 대시보드 서버를 실행한다")
    # PORT 는 Railway 등 PaaS 가 주입한다. 있으면 컨테이너 안이라는 뜻이므로
    # 모든 인터페이스에 바인드해야 플랫폼 라우터가 접속할 수 있다.
    web_cmd.add_argument(
        "--host",
        default="0.0.0.0" if os.getenv("PORT") else "127.0.0.1",
        help="바인드 주소. 기본값은 localhost 전용(PORT 환경변수가 있으면 0.0.0.0).",
    )
    web_cmd.add_argument(
        "--port", type=int, default=int(os.getenv("PORT") or 8000), help="포트 (기본 8000)"
    )
    web_cmd.add_argument(
        "--trust-proxy",
        action="store_true",
        default=_env_flag("TRUST_PROXY") or _on_railway(),
        help="X-Forwarded-For 로 실제 접속자 IP 를 판별한다. 리버스 프록시 뒤에서만 켤 것.",
    )
    web_cmd.add_argument(
        "--proxy-hops",
        type=int,
        default=int(os.getenv("PROXY_HOPS") or 1),
        help="앞단에 있는 신뢰하는 프록시 단 수 (기본 1).",
    )
    return parser


def confirm_live(action: str, skip: bool) -> bool:
    """실거래 진입 전 사람의 확인을 받는다."""
    if skip:
        return True
    if not sys.stdin.isatty():
        print("실거래에는 확인이 필요합니다. 비대화형 실행이면 --yes 를 붙이세요.", file=sys.stderr)
        return False
    answer = input(f"실제 자금으로 '{action}' 을 실행합니다. 계속하려면 LIVE 를 입력하세요: ")
    return answer.strip() == "LIVE"


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if args.command == "strategies":
        return _cmd_strategies()

    if args.command == "strategy":
        return _cmd_strategy_detail(args.name)

    if args.command == "hash-password":
        return _cmd_hash_password()

    if args.command == "setup-2fa":
        return _cmd_setup_2fa()

    load_dotenv(args.env_file)

    if args.command == "check-env":
        return _cmd_check_env(args)

    # 대시보드는 설정이 잘못돼도 일단 뜬다 — 아래 _cmd_web 참조.
    if args.command == "web":
        return _cmd_web(args)

    try:
        config = Config.load(args.config)
    except ConfigError as exc:
        print(f"설정 오류: {exc}", file=sys.stderr)
        return 2

    # 컨테이너에서는 볼륨 마운트 경로를 환경변수로 주는 편이 YAML 을 고치는 것보다 쉽다.
    log_file = os.getenv("LOG_FILE", "").strip()
    if log_file:
        config.logging.file = log_file

    setup_logging(args.log_level or config.logging.level, config.logging.file)

    try:
        credentials = Credentials.from_env(config.exchange.id)
    except ConfigError as exc:
        print(f"인증 오류: {exc}", file=sys.stderr)
        return 2

    try:
        exchange = create_exchange(config.exchange, credentials)
    except ExchangeError as exc:
        print(f"거래소 연결 실패: {exc}", file=sys.stderr)
        return 3

    try:
        if args.command == "check":
            return _cmd_check(config, exchange)
        if args.command == "positions":
            return _cmd_positions(config, exchange)
        if args.command == "close":
            return _cmd_close(config, exchange, args)
        if args.command == "run":
            return _cmd_run(config, exchange, args)
        if args.command == "backtest":
            return _cmd_backtest(config, exchange, args)
    except ExchangeError as exc:
        log.error("거래소 오류: %s", exc)
        return 3
    except KeyboardInterrupt:
        log.info("사용자 중단")
        return 130
    finally:
        exchange.close()
    return 0


def _cmd_check(config: Config, exchange) -> int:
    balance = exchange.fetch_balance(config.trading.quote_currency)
    print(f"거래소   : {config.exchange.id}")
    print(f"잔고     : {balance.total:.4f} {balance.currency} (가용 {balance.free:.4f})")
    print(f"전략     : {config.strategy.name}")
    print(f"레버리지 : {config.exchange.leverage}x ({config.exchange.margin_mode})")
    print("심볼     :")
    for symbol in config.trading.symbols:
        market = exchange.market(symbol)
        ticker = exchange.fetch_ticker(symbol)
        print(
            f"  {symbol}: 현재가 {ticker.last} | 1계약={market.contract_size} {market.base} "
            f"| 최소수량={market.min_amount} | 최소금액={market.min_notional}"
        )
    print("\n점검 완료 — 주문은 전송하지 않았습니다.")
    return 0


def _cmd_positions(config: Config, exchange) -> int:
    any_open = False
    for symbol in config.trading.symbols:
        position = exchange.fetch_position(symbol)
        if position.is_open:
            any_open = True
            print(
                f"{symbol}: {position.side.value} {position.contracts} "
                f"@ {position.entry_price} | 명목가 {position.notional:.2f} "
                f"| 미실현 {position.unrealized_pnl:+.4f} "
                f"| 청산가 {position.liquidation_price}"
            )
        else:
            print(f"{symbol}: 포지션 없음")
    if not any_open:
        print("\n보유 포지션이 없습니다.")
    return 0


def _cmd_close(config: Config, exchange, args) -> int:
    if args.live and not confirm_live("전 포지션 청산", args.yes):
        print("취소했습니다.", file=sys.stderr)
        return 1

    from bot.execution import Executor
    from bot.risk import RiskManager

    executor = Executor(
        exchange,
        RiskManager(config.risk, leverage=config.exchange.leverage),
        dry_run=not args.live,
    )
    closed = 0
    for symbol in config.trading.symbols:
        position = exchange.fetch_position(symbol)
        if not position.is_open:
            continue
        result = executor.handle(
            symbol=symbol,
            signal=Signal(action=SignalAction.EXIT, reason="수동 청산 명령"),
            position=position,
            price=0.0,
            equity=0.0,
            open_positions=1,
        )
        print(f"{symbol}: {result.detail}")
        closed += 1
    if closed == 0:
        print("청산할 포지션이 없습니다.")
    return 0


def _cmd_run(config: Config, exchange, args) -> int:
    if args.live:
        if not confirm_live(f"{config.strategy.name} 전략 자동매매", args.yes):
            print("취소했습니다.", file=sys.stderr)
            return 1
        log.warning("실거래 모드로 시작합니다 — 실제 자금이 사용됩니다")
    else:
        log.info("DRY-RUN 모드 — 주문은 전송되지 않습니다. 실거래는 --live 를 붙이세요.")

    engine = TradingEngine(config, exchange, dry_run=not args.live)
    engine.run()
    return 0


def _cmd_strategies() -> int:
    """전략 목록과 설명을 출력한다."""
    from bot.strategies import strategy_catalog

    labels = {
        "trend": "추세추종", "reversion": "평균회귀", "breakout": "돌파",
        "combo": "조합", "range": "횡보 전용", "other": "기타",
    }
    for entry in strategy_catalog():
        if not entry["summary"]:
            continue   # hold/template 은 목록에서 뺀다
        print(f"\n  {entry['name']}  [{labels.get(entry['category'], entry['category'])}]")
        print(f"    {entry['summary']}")
    print("\n자세한 설명은 `python -m bot strategy <이름>` 으로 볼 수 있습니다.\n")
    return 0


def _cmd_strategy_detail(name: str) -> int:
    from bot.strategies import strategy_catalog

    for entry in strategy_catalog():
        if entry["name"] == name:
            print(f"\n{entry['name']} — {entry['summary']}\n")
            print(entry["description"] or "(설명 없음)")
            if entry["algorithm"]:
                print("\n--- 알고리즘 ---")
                print(entry["algorithm"])
            print()
            return 0
    print(f"'{name}' 전략을 찾을 수 없습니다.", file=sys.stderr)
    return 1


def _cmd_backtest(config: Config, exchange, args) -> int:
    """과거 캔들을 받아 전략을 돌리고 결과를 표로 출력한다."""
    import time as time_module

    from bot.backtest import run_backtest
    from bot.strategies import get_strategy, strategy_catalog

    symbol = args.symbol or config.trading.symbols[0]
    if args.timeframe:
        config.trading.timeframe = args.timeframe
    timeframe = config.trading.timeframe

    since = int((time_module.time() - args.days * 86_400) * 1000)
    print(f"{symbol} {timeframe} 캔들을 받는 중 (최근 {args.days}일)...")
    candles = exchange.download_history(symbol, timeframe, since)
    if len(candles) < 100:
        print(f"캔들이 {len(candles)}개뿐이라 백테스트할 수 없습니다.", file=sys.stderr)
        return 1

    market = exchange.market(symbol)
    span_days = (candles[-1].timestamp - candles[0].timestamp) / 86_400_000
    print(f"캔들 {len(candles)}개 ({span_days:.0f}일), 1계약 = {market.contract_size} {market.base}\n")

    if args.strategy:
        names = [args.strategy]
    else:
        names = [e["name"] for e in strategy_catalog() if e["summary"]]

    results = []
    for name in names:
        try:
            strategy = get_strategy(name, config.strategy.params if args.strategy else None)
        except KeyError as exc:
            print(f"{exc}", file=sys.stderr)
            return 1
        result = run_backtest(
            candles, strategy, config,
            symbol=symbol,
            start_equity=args.equity,
            contract_size=market.contract_size,
            taker_fee=args.taker_fee / 100.0,
            maker_fee=args.maker_fee / 100.0,
            order_type=args.order_type or config.trading.order_type,
            funding_rate=args.funding_rate / 100.0,
        )
        results.append(result)

    results.sort(key=lambda r: r.return_pct, reverse=True)
    _print_backtest_table(results, args)
    return 0


def _print_backtest_table(results, args) -> None:
    header = (
        f"{'전략':<22}{'수익률':>10}{'최대낙폭':>10}{'거래':>7}"
        f"{'승률':>8}{'손익비':>8}{'수수료':>10}{'펀딩비':>10}"
    )
    print(header)
    print("-" * len(header.encode("utf-8").decode("utf-8")) * 1)
    for r in results:
        win = f"{r.win_rate:.0f}%" if r.win_rate is not None else "—"
        factor = (
            "∞" if r.profit_factor == float("inf")
            else f"{r.profit_factor:.2f}" if r.profit_factor is not None
            else "—"
        )
        print(
            f"{r.strategy:<22}{r.return_pct:>9.2f}%{r.max_drawdown_pct:>9.2f}%"
            f"{r.trade_count:>7}{win:>8}{factor:>8}{r.total_fee:>10.2f}"
            f"{r.total_funding:>10.2f}"
        )

    print()
    if any(r.missed_entries for r in results):
        missed = sum(r.missed_entries for r in results)
        print(f"지정가 미체결로 넘긴 신호: {missed}건")
    print(
        f"수익률은 수수료와 펀딩비를 뺀 값입니다. 펀딩비는 과거 실제 비율이 아니라\n"
        f"8시간당 {args.funding_rate}%% 를 방향과 무관하게 비용으로 가정한 값입니다\n"
        f"(--funding-rate 로 바꾸거나 0 으로 끌 수 있습니다).\n"
        "거래 횟수가 30건 미만이면 우연일 가능성이 크니 결과를 그대로 믿지 마세요.\n"
        "최대낙폭은 미실현 손실을 포함합니다."
    )


def _cmd_hash_password() -> int:
    """대시보드 계정을 만들어 환경변수 두 줄로 출력한다.

    비밀번호 원문은 화면에 찍히지 않고 어디에도 저장되지 않는다.
    """
    from bot.web.auth import (
        PASSWORD_ENV,
        RECOMMENDED_PASSWORD_LENGTH,
        USERNAME_ENV,
        AuthError,
        hash_password,
    )

    try:
        username = input("웹 대시보드 아이디: ").strip()
        password = getpass.getpass("비밀번호: ")
        again = getpass.getpass("다시 입력: ")
    except (EOFError, KeyboardInterrupt):
        print("\n취소했습니다.", file=sys.stderr)
        return 1
    if not username:
        print("아이디를 입력하세요.", file=sys.stderr)
        return 1
    if password != again:
        print("두 입력이 일치하지 않습니다.", file=sys.stderr)
        return 1
    try:
        encoded = hash_password(password)
    except AuthError as exc:
        print(f"오류: {exc}", file=sys.stderr)
        return 1

    if len(password) < RECOMMENDED_PASSWORD_LENGTH:
        print(
            f"\n⚠️  경고: 비밀번호가 {len(password)}자입니다 "
            f"(권장 {RECOMMENDED_PASSWORD_LENGTH}자 이상).\n"
            "   이 대시보드는 인터넷에 공개된 주소에서 계좌를 제어합니다. "
            "로그인 시도 제한은\n"
            "   IP 단위라 프록시를 돌리는 공격자에게는 잘 듣지 않습니다. "
            "짧게 쓰시려면\n"
            "   2단계 인증이나 Cloudflare Access 같은 관문을 함께 두시길 권합니다.",
            file=sys.stderr,
        )
    print("\n아래 두 줄을 .env(로컬) 또는 배포 환경의 환경변수에 넣으세요.")
    print("해시는 공백 없는 한 덩어리입니다 — 줄 끝까지 통째로 복사하세요.\n")
    print(f"{USERNAME_ENV}={username}")
    print(f"{PASSWORD_ENV}={encoded}")
    return 0


def _cmd_setup_2fa() -> int:
    """2단계 인증 비밀키를 만들고 터미널에 QR 코드를 그린다.

    비밀키는 만든 사람만 보고 지나간다 — 서버에 저장하는 것은 환경변수 하나뿐이고,
    여기서는 아무것도 파일로 남기지 않는다.
    """
    from bot.web import totp

    account = os.getenv("WEB_USERNAME", "").strip() or "dashboard"
    secret = totp.generate_secret()
    uri = totp.provisioning_uri(secret, account=account)

    print("2단계 인증 설정\n")
    try:
        import qrcode

        qr = qrcode.QRCode(border=1)
        qr.add_data(uri)
        qr.print_ascii()
    except ImportError:
        print("(QR 코드를 그리려면 `pip install qrcode` — 없으면 아래 키를 직접 입력하세요)\n")

    print("인증 앱(Google Authenticator, Authy 등)으로 위 QR 을 스캔하거나,")
    print("직접 입력하려면 아래 키를 넣으세요:\n")
    print(f"  {secret}\n")
    print("앱에 6자리 코드가 뜨는 것을 확인한 뒤, 아래 줄을 환경변수에 추가하세요:\n")
    print(f"{totp.TOTP_SECRET_ENV}={secret}\n")
    print("⚠️  이 키는 다시 볼 수 없습니다. 폰을 잃어버리면 이 키로만 복구할 수 있으니,")
    print("    안전한 곳(비밀번호 관리자 등)에 따로 보관하세요.")
    return 0


def _cmd_check_env(args) -> int:
    """서버가 실제로 보고 있는 환경변수 상태를 출력한다.

    배포 환경에서 "분명히 넣었는데 왜 안 되냐" 를 추측으로 풀지 않기 위한
    명령이다. 컨테이너 안에서 실행하면 서버와 같은 환경을 본다.
    **값은 절대 출력하지 않고 이름과 상태만 보여 준다.**
    """
    from bot.web.auth import (
        PASSWORD_ENV,
        PLAIN_PASSWORD_ENV,
        USERNAME_ENV,
        is_valid_password_hash,
    )

    print("환경변수 점검\n")

    ok = True
    try:
        config = Config.load(args.config)
        print(f"  CONFIG_YAML          설정됨 (거래소={config.exchange.id}, "
              f"심볼={', '.join(config.trading.symbols)})")
    except ConfigError as exc:
        config = Config()
        ok = False
        print(f"  CONFIG_YAML          ✗ {exc}")

    username = os.getenv(USERNAME_ENV, "").strip()
    if username:
        print(f"  {USERNAME_ENV:20s} 설정됨 ({len(username)}자)")
    else:
        ok = False
        print(f"  {USERNAME_ENV:20s} ✗ 비어 있음")

    encoded = os.getenv(PASSWORD_ENV, "").strip()
    plain = os.getenv(PLAIN_PASSWORD_ENV, "").strip()
    if encoded:
        if is_valid_password_hash(encoded):
            print(f"  {PASSWORD_ENV:20s} 설정됨 ({len(encoded)}자, 형식 정상)")
        else:
            ok = False
            print(f"  {PASSWORD_ENV:20s} ✗ 값이 깨졌습니다 ({len(encoded)}자)")
            print("                       `hash-password` 출력을 통째로 다시 넣거나,")
            print(f"                       이 변수를 지우고 {PLAIN_PASSWORD_ENV} 를 쓰세요.")
    elif plain:
        print(f"  {PLAIN_PASSWORD_ENV:20s} 설정됨 ({len(plain)}자) — 평문 방식")
    else:
        ok = False
        print(f"  {PLAIN_PASSWORD_ENV:20s} ✗ 비밀번호가 설정되지 않았습니다")
        print("                       이 변수에 비밀번호를 그대로 넣으면 됩니다.")

    from bot.web import totp

    totp_secret = os.getenv(totp.TOTP_SECRET_ENV, "").strip()
    if not totp_secret:
        print(f"  {totp.TOTP_SECRET_ENV:20s} 없음 — 2단계 인증이 꺼져 있습니다")
    elif totp.is_valid_secret(totp_secret):
        print(f"  {totp.TOTP_SECRET_ENV:20s} 설정됨 — 2단계 인증 사용 중")
    else:
        ok = False
        print(f"  {totp.TOTP_SECRET_ENV:20s} ✗ 형식이 올바르지 않습니다")

    prefix = config.exchange.id.upper()
    try:
        Credentials.from_env(config.exchange.id)
        print(f"  {prefix + '_API_*':20s} 설정됨")
    except ConfigError as exc:
        ok = False
        print(f"  {prefix + '_API_*':20s} ✗ {exc.args[0].split('.')[0]}")

    # 기록이 어디에 쌓이고 있는지. "볼륨을 붙였는데 왜 경고가 뜨냐" 를
    # 추측이 아니라 실제 상태로 답하기 위한 항목이다.
    state_dir, why, durable = resolve_state_dir(config)
    print()
    print("기록 저장 위치")
    print(f"  경로                 {Path(state_dir) / DB_FILENAME}")
    print(f"  판단                 {why}")
    if durable:
        print("  재배포               기록이 유지됩니다 ✓")
    else:
        # 환경변수 문제가 아니므로 종료 코드는 건드리지 않는다. 로그인은 되지만
        # 기록이 안 남는 상태이고, 그건 별개의 항목으로 보여 주는 편이 맞다.
        print("  재배포               ✗ 사라집니다")
        print("                       Railway 서비스에 Volume 을 추가하고")
        print("                       Mount path 를 정확히 /data 로 지정한 뒤")
        print("                       재배포하세요. 변수는 추가할 필요 없습니다.")
        mounts = sorted(m for m in _mount_points() if m.startswith("/") and m.count("/") <= 2)
        if mounts:
            print(f"                       (지금 붙어 있는 마운트: {', '.join(mounts[:12])})")

    print()
    if not durable:
        print("⚠ 기록이 재배포마다 초기화됩니다. 모의매매 성적을 며칠씩 모으려면")
        print("  볼륨을 붙여야 합니다.")
        print()
    if ok:
        print("모두 정상입니다. 대시보드에 로그인할 수 있어야 합니다.")
    else:
        print("✗ 표시된 항목을 고치세요. 환경변수를 바꾸면 서비스가 다시 시작되어야")
        print("  적용됩니다 — Console 에서 값을 만드는 것만으로는 적용되지 않습니다.")
    return 0 if ok else 1


# Railway·Fly 등에서 볼륨을 붙이는 관례적인 위치. 환경변수를 따로 넣지 않아도
# 볼륨만 마운트하면 기록이 재배포를 넘어 살아남게 하려는 것이다.
VOLUME_CANDIDATES = ("/data", "/mnt/data", "/var/data")

DB_FILENAME = "bot.db"


def _writable_dir(path: str) -> bool:
    return os.path.isdir(path) and os.access(path, os.W_OK)


def _mount_points() -> set[str]:
    """커널이 보고하는 마운트 지점들. 리눅스가 아니면 빈 집합."""
    try:
        with open("/proc/self/mountinfo", encoding="utf-8") as handle:
            # 다섯 번째 필드가 마운트 지점이다.
            return {line.split()[4] for line in handle if len(line.split()) > 4}
    except (OSError, IndexError):
        return set()


def is_mounted_volume(path: str) -> bool:
    """이 경로가 진짜 마운트된 볼륨인지.

    디렉터리가 존재하는지만 보면 안 된다. 볼륨을 안 붙여도 코드가 `/data` 를
    만들어 버리기 때문에, 존재 여부로 판단하면 컨테이너 파일시스템에 쓰면서
    "잘 저장되고 있다" 고 착각하게 된다. 재배포하면 그대로 날아간다.

    반대 방향의 오판은 더 나쁘다 — 볼륨을 제대로 붙였는데 아니라고 하면 사용자가
    멀쩡한 설정을 계속 고치게 된다. `os.path.ismount` 는 같은 파일시스템 안에서
    바인드 마운트한 경우를 놓치므로, 커널의 마운트 목록도 함께 본다.
    """
    if not _writable_dir(path):
        return False
    return os.path.ismount(path) or os.path.realpath(path) in _mount_points()


def resolve_state_dir(config: Config) -> tuple[str, str, bool]:
    """기록 DB 를 둘 디렉터리, 그렇게 정한 이유, 재배포를 넘어 남는지.

    우선순위는 STATE_DIR > 마운트된 볼륨 > 로그 디렉터리다. 마지막 것은 컨테이너
    안이라 재배포하면 사라진다.
    """
    explicit = os.getenv("STATE_DIR", "").strip()
    if explicit:
        # 사람이 직접 지정한 경로는 볼륨일 것으로 본다. 다만 실제로 마운트돼
        # 있으면 그렇다고 확실히 말해 준다.
        durable = is_mounted_volume(explicit) or any(
            explicit.rstrip("/").startswith(v) and is_mounted_volume(v)
            for v in VOLUME_CANDIDATES
        )
        return explicit, f"STATE_DIR={explicit}", durable
    log_dir = Path(config.logging.file or ".").parent
    for candidate in VOLUME_CANDIDATES:
        if not is_mounted_volume(candidate):
            continue
        # 예전에는 로그 디렉터리(볼륨 안의 하위 폴더)에 DB 를 만들었다. 그쪽에
        # 기록이 이미 쌓여 있으면 그대로 이어 쓴다 — 경로를 바꾸는 바람에
        # 며칠치 성적이 사라진 것처럼 보이면 안 된다.
        legacy = log_dir / DB_FILENAME
        if (
            not (Path(candidate) / DB_FILENAME).exists()
            and legacy.exists()
            and str(log_dir.resolve()).startswith(str(Path(candidate).resolve()))
        ):
            return str(log_dir), f"볼륨 {candidate} 안의 기존 기록을 이어서 씁니다", True
        return candidate, f"마운트된 볼륨 {candidate} 을 자동으로 찾았습니다", True
    return str(log_dir), "볼륨이 없어 컨테이너 내부에 저장합니다 (재배포하면 사라짐)", False


# 자동 시작 설정. 재배포해도 봇이 알아서 다시 켜지게 한다.
AUTOSTART_ENV = "AUTOSTART"


def resolve_autostart() -> tuple[bool, bool]:
    """(자동 시작 여부, 실거래 여부).

    기본값은 DRY-RUN 자동 시작이다. 모의매매 순위표는 봇 루프가 돌아야 기록되기
    때문에, 꺼져 있으면 며칠치 데이터가 통째로 비어 버린다. 실거래는 절대
    기본값이 되지 않는다 — 자기 돈이 걸린 일은 사람이 명시적으로 켜야 한다.
    """
    raw = os.getenv(AUTOSTART_ENV, "").strip().lower()
    if raw in ("off", "0", "false", "no", "none"):
        return False, False
    if raw == "live":
        return True, True
    return True, False


def _log_env_diagnostics(config: Config) -> None:
    """어떤 환경변수가 들어와 있는지 기동 로그에 남긴다.

    배포 환경에서 변수 하나가 빠지거나 이름이 틀렸을 때, 값을 노출하지 않고도
    무엇이 문제인지 로그만 보고 알 수 있어야 한다. **값은 절대 찍지 않는다.**
    """
    from bot.config import passphrase_required
    from bot.web.auth import (
        PASSWORD_ENV,
        PLAIN_PASSWORD_ENV,
        USERNAME_ENV,
        is_valid_password_hash,
    )

    prefix = config.exchange.id.upper()
    # 비밀번호는 두 방식 중 실제로 쓰이는 쪽만 보고한다.
    password_env = PASSWORD_ENV if os.getenv(PASSWORD_ENV, "").strip() else PLAIN_PASSWORD_ENV
    names = [USERNAME_ENV, password_env, "CONFIG_YAML",
             f"{prefix}_API_KEY", f"{prefix}_API_SECRET"]
    if passphrase_required(config.exchange.id):
        names.append(f"{prefix}_API_PASSPHRASE")

    report = []
    for name in names:
        value = os.getenv(name, "")
        if not value.strip():
            state = "없음"
        elif name == PASSWORD_ENV and not is_valid_password_hash(value):  # 해시 방식일 때만
            # $ 가 많은 값이라 셸을 거치면 변수 확장으로 중간이 날아가기 쉽다.
            state = "값이 깨짐(hash-password 출력을 통째로 넣으세요)"
        else:
            state = "설정됨"
        report.append(f"{name}={state}")
    log.info("환경변수 점검 — %s", ", ".join(report))


def _cmd_web(args) -> int:
    """웹 대시보드 서버를 실행한다.

    **설정이나 거래소 자격증명이 잘못돼도 서버는 뜬다.** 대시보드는 무엇이
    잘못됐는지 보여주기 위한 물건인데, 여기서 프로세스를 죽여 버리면 정작
    문제가 생겼을 때 아무것도 안 보이고 배포 로그를 뒤져야 한다. 문제는
    화면에 띄우고, 그 상태에서는 봇 시작을 막는다.

    로그인 계정이 없을 때도 마찬가지로 뜬다. 계정이 없으면 애초에 로그인이
    성공할 수 없어 제어권이 열리지 않는다 — 죽은 사이트를 남기는 것보다 로그인
    화면에서 원인을 알려 주는 편이 낫다.
    """
    import uvicorn

    from bot.logging_utils import LogBuffer
    from bot.store import Store
    from bot.web.app import create_app
    from bot.web.auth import AuthError, load_account
    from bot.web.supervisor import BotSupervisor

    startup_error: str | None = None

    account = None
    try:
        account = load_account()
    except AuthError as exc:
        startup_error = f"로그인 계정 오류: {exc}"

    try:
        config = Config.load(args.config)
    except ConfigError as exc:
        # 설정을 못 읽으면 화면을 띄우기 위한 자리표시자를 쓴다. 이 상태에서는
        # 봇 시작이 막히므로 잘못된 설정으로 매매가 시작될 일은 없다.
        config = Config()
        startup_error = f"설정 오류: {exc}"

    log_file = os.getenv("LOG_FILE", "").strip()
    if log_file:
        config.logging.file = log_file

    # 로그를 파일·콘솔과 함께 메모리 버퍼로도 흘려보낸다. 대시보드가 이걸 읽는다.
    log_buffer = LogBuffer(capacity=1000)
    setup_logging(args.log_level or config.logging.level, config.logging.file, log_buffer)

    if startup_error is None:
        try:
            Credentials.from_env(config.exchange.id)
        except ConfigError as exc:
            startup_error = f"거래소 자격증명 오류: {exc}"

    _log_env_diagnostics(config)
    if startup_error:
        log.error("%s — 대시보드는 뜨지만 봇은 시작할 수 없습니다", startup_error)

    if args.host not in LOCAL_HOSTS and not args.trust_proxy:
        # 프록시 뒤라면 0.0.0.0 이 정상이다. 그게 아니라면 HTTPS 없이 평문으로
        # 비밀번호와 세션 토큰이 오간다는 뜻이므로 경고한다.
        log.warning(
            "서버를 %s 로 직접 노출합니다. HTTPS 종료를 담당하는 리버스 프록시 뒤에 "
            "두세요 — 그대로 열면 비밀번호와 세션 토큰이 평문으로 오갑니다. "
            "README 의 배포 절을 확인하세요.",
            args.host,
        )
    if args.trust_proxy:
        log.info(
            "프록시 신뢰 모드 — X-Forwarded-For 의 오른쪽에서 %d번째 값을 접속자 IP 로 "
            "사용합니다. 반드시 리버스 프록시 뒤에서만 켜세요.",
            args.proxy_hops,
        )
    if not STATIC_DIR.is_dir():
        log.warning(
            "프론트엔드 빌드가 없습니다 (%s). frontend/ 에서 "
            "`npm install && npm run build` 를 실행하세요. API 는 그대로 동작합니다.",
            STATIC_DIR,
        )

    # 거래 기록 DB. 볼륨(/data)에 두면 재배포해도 살아남는다.
    state_dir, why, durable = resolve_state_dir(config)
    store = Store(Path(state_dir) / DB_FILENAME, durable=durable)
    log.info("기록 DB — %s (%s)", store.path, why)
    if not store.persistent:
        log.warning("거래 기록이 메모리에만 남습니다 — 재시작하면 성과 기록이 사라집니다")
    elif not store.durable:
        # 파일은 정상적으로 만들어진다. 다만 그 파일이 컨테이너 안에 있어서
        # 재배포 한 번에 통째로 사라진다 — 모의매매 성적이 매번 초기화되는
        # 원인이 대개 이것이고, 쓰기가 성공하니 조용히 지나간다.
        log.warning(
            "기록을 컨테이너 내부(%s)에 저장합니다 — 재배포하면 사라집니다. "
            "Railway 에서 Volume 을 추가하고 마운트 경로를 /data 로 지정하세요.",
            state_dir,
        )

    supervisor = BotSupervisor(config, store=store)
    app = create_app(
        config,
        supervisor,
        log_buffer,
        account,
        static_dir=STATIC_DIR,
        trust_proxy=args.trust_proxy,
        proxy_hops=args.proxy_hops,
        startup_error=startup_error,
        storage_note=why,
    )

    # 봇을 자동으로 띄운다. 모의매매 순위표는 봇 루프가 돌아야 기록되므로,
    # 재배포 때마다 사람이 시작 버튼을 눌러야 한다면 데이터에 구멍이 생긴다.
    autostart, autostart_live = resolve_autostart()
    if autostart and startup_error:
        log.error("설정 오류로 자동 시작을 건너뜁니다 — 화면에서 원인을 확인하세요")
    elif autostart:
        supervisor.enable_autorestart(live=autostart_live)
    else:
        log.info("%s=off — 봇은 화면에서 직접 시작해야 합니다", AUTOSTART_ENV)

    log.info("대시보드 서버 시작 — http://%s:%s", args.host, args.port)
    try:
        uvicorn.run(app, host=args.host, port=args.port, log_config=None, access_log=False)
    finally:
        supervisor.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
