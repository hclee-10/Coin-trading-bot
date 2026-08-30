"""FastAPI 애플리케이션.

설계 원칙:

* **시크릿은 어떤 응답에도 담기지 않는다.** API 키는 물론 비밀번호 해시도
  나가지 않는다. `/api/config` 는 거래 파라미터만 돌려준다.
* **제어 동작은 확인 문구를 요구한다.** 실거래 시작과 긴급 청산은 CLI 와 같은
  방식으로 정확한 문구를 받아야 실행된다 — 오탭 한 번에 자금이 움직이지 않게.
* **제어 동작은 접속 IP 와 함께 기록된다.** 실제 자금이 걸린 만큼 누가 언제
  무엇을 눌렀는지가 로그에 남아야 한다.
* **요청 스레드는 거래소를 직접 부르지 않는다.** supervisor 모듈의 동시성
  원칙을 따른다.
"""

from __future__ import annotations

import logging
from dataclasses import asdict
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.responses import FileResponse, JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from bot.config import Config
from bot.logging_utils import LogBuffer
from bot.web.auth import Account, LoginThrottle, TokenStore
from bot.web.totp import UsedCodeTracker
from bot.web.supervisor import BotSupervisor, SupervisorError

log = logging.getLogger(__name__)

LIVE_CONFIRMATION = "LIVE"
CLOSE_CONFIRMATION = "CLOSE"

_bearer = HTTPBearer(auto_error=False)


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=256)
    password: str = Field(min_length=1, max_length=512)
    # 2단계 인증을 쓰지 않는 설정에서는 비워 둔다.
    code: str = Field(default="", max_length=16)


class StartRequest(BaseModel):
    live: bool = False
    # 실거래로 시작할 때만 필요하다. DRY-RUN 은 그냥 시작된다.
    confirm: str = ""


class CloseAllRequest(BaseModel):
    confirm: str = ""


def create_app(
    config: Config,
    supervisor: BotSupervisor,
    log_buffer: LogBuffer,
    account: Account | None,  # None = 계정 미설정. 로그인이 전부 거부된다.
    *,
    static_dir: Path | None = None,
    token_store: TokenStore | None = None,
    throttle: LoginThrottle | None = None,
    trust_proxy: bool = False,
    proxy_hops: int = 1,
    startup_error: str | None = None,
) -> FastAPI:
    tokens = token_store or TokenStore()
    login_throttle = throttle or LoginThrottle()
    used_codes = UsedCodeTracker()

    app = FastAPI(title="Coin Trading Bot", docs_url=None, redoc_url=None, openapi_url=None)

    # ------------------------------------------------------------------
    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        # 대시보드는 자체 자산만 쓴다 — 외부 스크립트가 끼어들 여지를 없앤다.
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; "
            "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
        )
        return response

    def client_ip(request: Request) -> str:
        """요청을 보낸 실제 클라이언트 IP.

        리버스 프록시(Railway, nginx) 뒤에서는 TCP 피어가 항상 프록시라서
        `request.client.host` 로는 접속자를 구분할 수 없다. 그대로 두면 누군가
        비밀번호를 5번 틀리는 순간 프록시 IP 하나가 잠기면서 **모든 사용자가**
        로그인하지 못한다.

        `X-Forwarded-For` 는 `클라이언트, 프록시1, 프록시2` 순으로 쌓이는데,
        **맨 왼쪽 값은 클라이언트가 직접 위조할 수 있다** — 헤더를 넣어 보내면
        프록시가 그 뒤에 진짜 IP 를 덧붙일 뿐이다. 그래서 왼쪽에서 읽으면
        공격자가 매 시도마다 다른 IP 를 위장해 시도 제한을 통째로 우회한다.
        신뢰하는 프록시 단 수만큼 **오른쪽에서** 세어야 위조할 수 없는 값을
        얻는다.
        """
        if trust_proxy:
            forwarded = request.headers.get("x-forwarded-for", "")
            hops = [part.strip() for part in forwarded.split(",") if part.strip()]
            if len(hops) >= proxy_hops:
                return hops[-proxy_hops]
        return request.client.host if request.client else "unknown"

    def require_auth(
        request: Request,
        credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
    ) -> str:
        token = credentials.credentials if credentials else None
        if not tokens.validate(token):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="로그인이 필요합니다",
                headers={"WWW-Authenticate": "Bearer"},
            )
        return token  # type: ignore[return-value]

    # ------------------------------------------------------------------
    @app.get("/healthz")
    def healthz() -> dict:
        """업타임 점검용. 인증 없이 열리므로 아무 정보도 담지 않는다."""
        return {"ok": True}

    @app.get("/api/login-options")
    def login_options() -> dict:
        """로그인 화면이 코드 입력칸을 띄울지 판단하는 데만 쓴다.

        2단계 인증을 켰는지 여부는 비밀이 아니다 — 코드를 요구하면 어차피
        드러난다. 이 정보가 없으면 사용자가 빈 칸 앞에서 헤맨다.
        """
        return {"totp_required": bool(account and account.totp_enabled)}

    @app.post("/api/login")
    def login(body: LoginRequest, request: Request) -> dict:
        ip = client_ip(request)
        if account is None:
            # 계정이 없으면 아무도 로그인할 수 없다. 죽은 사이트를 보여 주는
            # 대신 로그인 화면에서 원인을 알려 준다 — 제어권이 열리는 것은 아니다.
            #
            # 둘 중 어느 변수가 문제인지까지 말해 준다. 이 상태에서는 로그인이
            # 아예 불가능하고 값이 아니라 변수 이름만 나가므로, 배포 로그를 뒤지지
            # 않고 원인을 아는 편의가 훨씬 크다.
            log.error("로그인 시도했으나 서버에 계정이 설정되어 있지 않습니다 — ip=%s", ip)
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=startup_error
                or "서버에 로그인 계정이 설정되지 않았습니다. "
                "WEB_USERNAME 과 WEB_PASSWORD_HASH 환경변수를 확인하세요.",
            )
        locked = login_throttle.locked_for(ip)
        if locked > 0:
            log.warning("로그인 잠금 상태에서 시도 — ip=%s 남은 %.0f초", ip, locked)
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail=f"로그인 시도가 너무 많습니다. {int(locked)}초 후 다시 시도하세요.",
            )
        if not account.verify(body.username, body.password):
            login_throttle.record_failure(ip)
            # 어느 쪽이 틀렸는지 알려 주지 않는다 — 아이디 존재 여부가 새어 나간다.
            log.warning("로그인 실패 — ip=%s", ip)
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="아이디 또는 비밀번호가 올바르지 않습니다",
            )
        if account.totp_enabled:
            counter = account.verify_totp(body.code)
            if counter is None:
                # 비밀번호는 맞았지만 코드가 틀렸다. 이것도 실패로 세야 한다 —
                # 아니면 비밀번호를 맞춘 뒤 코드만 무한히 시도할 수 있다.
                login_throttle.record_failure(ip)
                log.warning("2단계 인증 실패 — ip=%s", ip)
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail="인증 코드가 올바르지 않습니다",
                )
            if not used_codes.claim(counter):
                # 코드는 30초간 유효하다. 그 사이 새어 나간 코드가 그대로 다시
                # 통하면 2단계 인증의 의미가 반감된다.
                login_throttle.record_failure(ip)
                log.warning("이미 사용된 2단계 코드 재사용 시도 — ip=%s", ip)
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail="이미 사용된 코드입니다. 다음 코드를 기다렸다가 입력하세요.",
                )

        login_throttle.reset(ip)
        session = tokens.create()
        log.info("로그인 성공 — ip=%s user=%s", ip, body.username)
        return {"token": session.token, "expires_at": session.expires_at}

    @app.post("/api/logout")
    def logout(token: str = Depends(require_auth)) -> dict:
        tokens.revoke(token)
        return {"ok": True}

    # ------------------------------------------------------------------
    def status_payload() -> dict:
        """상태에 기동 단계 오류를 얹는다.

        설정이나 자격증명이 잘못된 채로 뜬 경우, 사용자가 배포 로그를 뒤지지
        않고 화면에서 바로 원인을 볼 수 있어야 한다.
        """
        payload = asdict(supervisor.snapshot())
        payload["startup_error"] = startup_error
        return payload

    @app.get("/api/status")
    def get_status(_: str = Depends(require_auth)) -> dict:
        return status_payload()

    @app.get("/api/config")
    def get_config(_: str = Depends(require_auth)) -> dict:
        """거래 파라미터만. 자격증명은 애초에 Config 에 들어 있지 않다."""
        return {
            "exchange": {
                "id": config.exchange.id,
                "margin_mode": config.exchange.margin_mode,
                "leverage": config.exchange.leverage,
            },
            "trading": {
                "symbols": list(config.trading.symbols),
                "timeframe": config.trading.timeframe,
                "poll_interval_sec": config.trading.poll_interval_sec,
                "quote_currency": config.trading.quote_currency,
                "allow_reverse": config.trading.allow_reverse,
            },
            "strategy": {"name": config.strategy.name},
            "risk": asdict(config.risk),
        }

    @app.get("/api/positions")
    def get_positions(_: str = Depends(require_auth)) -> dict:
        """봇이 돌면 최신 주기 결과를, 멈춰 있으면 거래소에 직접 물어본다."""
        if supervisor.running:
            snapshot = supervisor.snapshot()
            return {
                "source": "last_cycle",
                "at": snapshot.last_cycle_at,
                "positions": [asdict(p) for p in snapshot.positions],
            }
        try:
            positions = supervisor.fetch_positions_live()
        except SupervisorError as exc:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
        except Exception as exc:
            log.exception("포지션 조회 실패")
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY, detail=f"거래소 조회 실패: {exc}"
            ) from exc
        return {"source": "exchange", "at": None, "positions": [asdict(p) for p in positions]}

    @app.get("/api/chart")
    def get_chart(symbol: str | None = None, _: str = Depends(require_auth)) -> dict:
        """캔들과 내 체결 지점. 차트에 매수/매도를 표시하는 데 쓴다."""
        target = symbol or (config.trading.symbols[0] if config.trading.symbols else None)
        if target is None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail="심볼이 설정되지 않았습니다"
            )
        if target not in config.trading.symbols:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"'{target}' 은 감시 중인 심볼이 아닙니다",
            )

        candles = supervisor.candles(target)
        performance = supervisor.performance(target)
        markers = [
            {
                "time": trip.opened_at // 1000,
                "side": trip.side,
                "kind": "entry",
                "price": trip.entry_price,
            }
            for trip in performance.trips
        ] + [
            {
                "time": trip.closed_at // 1000,
                "side": trip.side,
                "kind": "exit",
                "price": trip.exit_price,
                "pnl": trip.pnl,
            }
            for trip in performance.trips
        ]
        markers.sort(key=lambda m: m["time"])
        return {
            "symbol": target,
            "timeframe": config.trading.timeframe,
            "candles": candles,
            "markers": markers,
        }

    @app.get("/api/performance")
    def get_performance(symbol: str | None = None, _: str = Depends(require_auth)) -> dict:
        """자동매매 성과. 수익률은 자기자본 변화로, 승률은 닫힌 왕복으로 센다."""
        performance = supervisor.performance(symbol)
        recent = list(reversed(performance.trips))[:50]
        return {
            "trade_count": performance.trade_count,
            "win_count": performance.win_count,
            "loss_count": performance.loss_count,
            "win_rate": performance.win_rate,
            "realized_pnl": performance.realized_pnl,
            "total_fee": performance.total_fee,
            "best_pnl": performance.best_pnl,
            "worst_pnl": performance.worst_pnl,
            "start_equity": performance.start_equity,
            "current_equity": performance.current_equity,
            "equity_change": performance.equity_change,
            "total_return_pct": performance.total_return_pct,
            "started_at": performance.started_at,
            "persistent": bool(supervisor.store and supervisor.store.persistent),
            "trades": [
                {
                    "symbol": t.symbol,
                    "side": t.side,
                    "opened_at": t.opened_at,
                    "closed_at": t.closed_at,
                    "entry_price": t.entry_price,
                    "exit_price": t.exit_price,
                    "amount": t.amount,
                    "pnl": t.pnl,
                    "fee": t.fee,
                    "return_pct": t.return_pct,
                }
                for t in recent
            ],
        }

    @app.get("/api/equity")
    def get_equity(_: str = Depends(require_auth)) -> dict:
        """자기자본 곡선."""
        if supervisor.store is None:
            return {"points": []}
        return {
            "points": [
                {"time": p.timestamp // 1000, "value": p.equity}
                for p in supervisor.store.equity_curve()
            ]
        }

    @app.get("/api/logs")
    def get_logs(since: int = 0, limit: int = 200, _: str = Depends(require_auth)) -> dict:
        limit = max(1, min(limit, 500))
        entries = log_buffer.since(since, limit=limit)
        return {
            "entries": [asdict(e) for e in entries],
            "latest_seq": log_buffer.latest_seq,
        }

    # ------------------------------------------------------------------
    @app.post("/api/bot/start")
    def start_bot(body: StartRequest, request: Request, _: str = Depends(require_auth)) -> dict:
        ip = client_ip(request)
        if startup_error:
            # 설정이 깨진 상태에서 봇이 돌기 시작하는 일은 없어야 한다.
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"{startup_error} — 환경변수를 고치고 다시 배포하세요.",
            )
        if body.live and body.confirm != LIVE_CONFIRMATION:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"실거래로 시작하려면 확인 문구 '{LIVE_CONFIRMATION}' 이 필요합니다",
            )
        try:
            supervisor.start(live=body.live)
        except SupervisorError as exc:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
        except Exception as exc:
            log.exception("봇 시작 실패")
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY, detail=f"봇 시작 실패: {exc}"
            ) from exc
        log.warning(
            "대시보드에서 봇 시작 — ip=%s 모드=%s", ip, "실거래" if body.live else "DRY-RUN"
        )
        return status_payload()

    @app.post("/api/bot/stop")
    def stop_bot(request: Request, _: str = Depends(require_auth)) -> dict:
        ip = client_ip(request)
        stopped = supervisor.stop()
        log.warning("대시보드에서 봇 정지 요청 — ip=%s 결과=%s", ip, "정지" if stopped else "실패")
        if not stopped:
            raise HTTPException(
                status_code=status.HTTP_504_GATEWAY_TIMEOUT,
                detail="봇이 제한 시간 안에 멈추지 않았습니다. 로그를 확인하세요.",
            )
        return status_payload()

    @app.post("/api/positions/close-all")
    def close_all(
        body: CloseAllRequest, request: Request, _: str = Depends(require_auth)
    ) -> dict:
        ip = client_ip(request)
        if body.confirm != CLOSE_CONFIRMATION:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"긴급 청산에는 확인 문구 '{CLOSE_CONFIRMATION}' 이 필요합니다",
            )
        log.warning("대시보드에서 긴급 청산 요청 — ip=%s", ip)
        try:
            messages = supervisor.close_all_positions()
        except SupervisorError as exc:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
        except Exception as exc:
            log.exception("긴급 청산 실패")
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=f"청산 실패: {exc} — 거래소에서 직접 확인하세요",
            ) from exc
        return {"messages": messages, "status": status_payload()}

    # ------------------------------------------------------------------
    if static_dir is not None and static_dir.is_dir():
        assets = static_dir / "assets"
        if assets.is_dir():
            app.mount("/assets", StaticFiles(directory=assets), name="assets")

        index = static_dir / "index.html"

        @app.get("/")
        def serve_index() -> FileResponse:
            return FileResponse(index)
    else:
        @app.get("/")
        def missing_frontend() -> JSONResponse:
            return JSONResponse(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                content={
                    "detail": "프론트엔드가 빌드되지 않았습니다. "
                    "frontend/ 에서 `npm install && npm run build` 를 실행하세요."
                },
            )

    return app
