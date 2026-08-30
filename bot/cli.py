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
from bot.strategies import available_strategies

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
    sub.add_parser("strategies", help="등록된 전략 이름을 출력한다")
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
        print("등록된 전략:")
        for name in available_strategies():
            print(f"  - {name}")
        return 0

    if args.command == "hash-password":
        return _cmd_hash_password()

    load_dotenv(args.env_file)

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
        # web 은 거래소를 봇 시작 시점에 만들지만, 키가 없는 채로 서버를 띄우면
        # 시작 버튼을 눌러야만 문제를 알게 된다. 여기서 미리 확인한다.
        credentials = Credentials.from_env(config.exchange.id)
    except ConfigError as exc:
        print(f"인증 오류: {exc}", file=sys.stderr)
        return 2

    if args.command == "web":
        return _cmd_web(config, args)

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


def _cmd_hash_password() -> int:
    """대시보드 계정을 만들어 환경변수 두 줄로 출력한다.

    비밀번호 원문은 화면에 찍히지 않고 어디에도 저장되지 않는다.
    """
    from bot.web.auth import PASSWORD_ENV, USERNAME_ENV, AuthError, hash_password

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
    print("\n아래 두 줄을 .env(로컬) 또는 Railway 환경변수에 추가하세요:\n")
    print(f"{USERNAME_ENV}={username}")
    print(f"{PASSWORD_ENV}={encoded}")
    return 0


def _cmd_web(config: Config, args) -> int:
    """웹 대시보드 서버를 실행한다."""
    import uvicorn

    from bot.logging_utils import LogBuffer
    from bot.web.app import create_app
    from bot.web.auth import AuthError, load_account
    from bot.web.supervisor import BotSupervisor

    try:
        account = load_account()
    except AuthError as exc:
        print(f"인증 설정 오류: {exc}", file=sys.stderr)
        return 2

    # 로그를 파일·콘솔과 함께 메모리 버퍼로도 흘려보낸다. 대시보드가 이걸 읽는다.
    log_buffer = LogBuffer(capacity=1000)
    setup_logging(args.log_level or config.logging.level, config.logging.file, log_buffer)

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

    supervisor = BotSupervisor(config)
    app = create_app(
        config,
        supervisor,
        log_buffer,
        account,
        static_dir=STATIC_DIR,
        trust_proxy=args.trust_proxy,
        proxy_hops=args.proxy_hops,
    )

    log.info("대시보드 서버 시작 — http://%s:%s", args.host, args.port)
    try:
        uvicorn.run(app, host=args.host, port=args.port, log_config=None, access_log=False)
    finally:
        supervisor.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
