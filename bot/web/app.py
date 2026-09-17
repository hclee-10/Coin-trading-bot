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
import re
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


def _frontend_bundle(static_dir: Path | None) -> str | None:
    """index.html 이 참조하는 자바스크립트 번들 파일명을 읽는다."""
    if static_dir is None:
        return None
    index = static_dir / "index.html"
    if not index.is_file():
        return None
    match = re.search(r'/assets/(index-[A-Za-z0-9_-]+\.js)', index.read_text(encoding="utf-8"))
    return match.group(1) if match else None


def _market_snapshot(candles: list[dict], timeframe: str) -> dict:
    """시세 요약. 모든 AI 가 같은 것을 받는다 — 차이는 판단뿐이어야 한다."""
    closes = [c["close"] for c in candles]
    if not closes:
        return {"price": None, "changes": {}, "recent_bars": [], "timeframe": timeframe}
    price = closes[-1]
    changes = {}
    # 봉 수로 기간을 계산한다 (기본 5분봉 → 12개=1시간).
    for label, bars in (("1h", 12), ("6h", 72), ("12h", 144)):
        if len(closes) > bars:
            changes[label] = round((price / closes[-1 - bars] - 1) * 100, 3)
    return {
        "price": price,
        "timeframe": timeframe,
        "changes": changes,
        "recent_bars": [
            {"open": c["open"], "high": c["high"], "low": c["low"], "close": c["close"]}
            for c in candles[-24:]
        ],
    }


_SPEC_SCHEMA_MD = """
```json
{
  "leverage": 5,
  "orders": [ {"side": "long", "notional": 1000} ],
  "rules": [
    {
      "name": "dip_buy",
      "timeframe": "5m",      // 1m | 5m | 15m | 1h | 4h
      "bars": 3,               // 1~96 — 최근 N봉 누적 변화를 본다
      "op": "lte",             // lte(이하) | gte(이상)
      "change_pct": -0.6,      // ±50 이내 (%)
      "side": "long",          // long | short | close
      "notional": 500,          // 1~5000 USDT (회당 한도)
      "cooldown_min": 30        // 발동 후 재발동 금지 시간 (0~1440분)
    }
  ],
  "stop_loss_pct": 2.5,        // 0 = 손절 없음 (0~90)
  "take_profit_pct": 5.0,      // 0 = 익절 없음 (0~500)
  "max_position_notional": 30000,  // 0 = 레버리지 한도만 적용
  "memo": "전략 요지 한두 문장"
}
```
"""


def _ai_handoff_md(*, label: str, token: str, base_url: str, symbol: str) -> str:
    """AI 에게 한 번 건네는 지시문. 대회 규칙·접근 토큰·API 사용법·스펙 형식.

    네 AI 모두 같은 문서를 받는다 — 토큰과 이름만 다르다.
    """
    base = base_url.rstrip("/")
    return f"""# 암호화폐 선물 모의투자 대회 — {label} 봇 운영 지시서

당신({label})은 4개의 AI(클로드·GPT·그록·제미나이)가 같은 조건으로 겨루는
모의투자 대회의 참가자입니다. 당신은 **자기 봇을 직접 프로그래밍**해서 운영합니다.

## 대회 규칙
1. **목표**: 수익률 최대화. 단, 당신의 수익률이 (같은 기간 BTC 현물 보유 수익률 − 5%p)
   아래로 내려가면 서버가 노출을 늘리는 주문을 자동 차단합니다 (청산·상계만 허용).
   즉 시장보다 5%p 이상 뒤지지 않게 리스크를 관리해야 합니다.
2. **자금**: 가상 10,000 USDT. 수수료 taker 0.05%(왕복 0.1%)와 펀딩비(8시간마다,
   실제 비율)가 항상 부과됩니다.
3. **레버리지**: 스펙에서 1~10배 선택. 총 노출(포지션 명목가)은 자기자본 × 레버리지를
   넘을 수 없습니다.
4. **주문 횟수 무제한**, 단 **봇 스펙 수정은 6시간에 1회**만 서버가 받습니다
   (검증에 실패한 시도는 횟수에 세지 않음). 매 기회마다 성능을 개선하세요.
5. **회당 주문 한도 5,000 USDT**. 이보다 큰 주문은 거부됩니다.
6. **파산 = 실격**: 평가 자기자본(미실현 포함)이 0 이하로 내려가면 남은 포지션이
   강제 정리되고 계좌가 동결됩니다. 복구 기회는 없습니다.
7. **대회 기간 4주**: 첫 봇 스펙이 적용된 순간부터 28일. 종료 시점의 수익률이
   최종 순위이고, 종료 후에는 신규 진입과 스펙 수정이 차단됩니다 (정리만 가능).
8. **외부 정보 허용**: 뉴스, 실시간 검색, 자체 분석 등 무엇이든 활용해도 됩니다.
9. **체결**: 시장가 즉시 체결을 가정하되, 체결가는 호가 기준입니다 — 매수는
   매도호가(ask), 매도는 매수호가(bid)에 슬리피지 0.01%를 더한 가격. 스프레드와
   슬리피지가 비용이므로 초단타 회전은 그만큼 불리합니다.
10. 시장: {symbol} 무기한 선물(Gate.io 시세), 단방향 모드 — 같은 방향 주문은 쌓이고
    (평단 갱신), 반대 방향 주문은 그만큼 상계됩니다(넘치면 뒤집힘).
11. 봇은 15초마다 스펙의 규칙을 평가합니다. 스펙의 stop_loss_pct / take_profit_pct 를
    설정하면 포지션에 손절/익절이 자동 적용됩니다.

## 당신의 접근 권한 (비밀 — 다른 참가자와 공유 금지)
- API 주소: `{base}`
- 접근 토큰: `{token}`

## 봇 조작 방법 (둘 중 가능한 쪽을 쓰세요)

### 방법 A — 직접 API 호출 (웹 접근/코드 실행이 가능한 경우)
현재 상태 확인 (시세·내 계좌·현재 스펙):
```
GET {base}/api/aibot/state
X-AI-Token: {token}
```
봇 스펙 교체:
```
POST {base}/api/aibot/spec
X-AI-Token: {token}
Content-Type: application/json

(아래 '봇 스펙 형식'의 JSON 본문)
```
응답의 `errors` 가 비어 있으면 적용된 것입니다. 오류가 있으면 메시지를 읽고 고쳐서
다시 보내세요.

### 방법 B — URL 열기만으로 조작 (브라우징/웹 접근만 가능한 경우)
헤더 없는 GET 요청(URL 열기)만으로도 전부 됩니다:
- 상태 확인: `{base}/api/aibot/state?token={token}` 을 열면 JSON 이 보입니다.
- 스펙 제출: `{base}/api/aibot/spec/set?token={token}&spec=<URL인코딩한 스펙 JSON>`
  을 열면 적용됩니다. 응답의 `ok` 가 true 면 성공, `errors` 가 있으면 고쳐서 다시.

### 방법 C — JSON 으로만 답하기 (웹 접근이 전혀 안 되는 경우)
사용자가 6시간마다 현재 상태(state JSON)를 붙여 줍니다. 당신은 **봇 스펙 JSON 하나만**
코드블록으로 답하세요 — 사용자가 그대로 대시보드에 붙여넣습니다.

## ⏰ 자동 루틴 설정 (이 지시서를 받은 당신이 예약 기능을 지원한다면)
당신의 예약/자동화 기능(Tasks, 예약 작업 등)에 **6시간마다** 아래 작업을 등록하세요:

> {base}/api/aibot/state?token={token} 을 열어 내 계좌·시세·현재 스펙을 확인하고,
> 성적을 분석해 봇 스펙을 개선한 뒤,
> {base}/api/aibot/spec/set?token={token}&spec=(URL인코딩한 새 스펙 JSON) 을 열어
> 제출한다. 응답에 errors 가 있으면 고쳐서 한 번 다시 제출한다.
> 스펙 수정은 6시간에 1회만 받아 주니 실패해도 다음 회차에 반영하면 된다.

예약 기능이 없거나 위 URL 접근이 실패하면, 사용자에게 "6시간마다 상태를 붙여달라"고
요청하고 방법 C 로 참여하세요.

## 봇 스펙 형식
{_SPEC_SCHEMA_MD}
- `orders`: 스펙 적용 즉시 1회 나가는 주문 (최대 5개). 지금 당장 포지션을 잡거나
  정리할 때 씁니다.
- `rules`: 15초마다 평가되는 상시 규칙 (최대 10개). "{{timeframe}} 봉 {{bars}}개 누적
  변화가 {{change_pct}}% {{op}}" 이면 {{side}} {{notional}} USDT 주문. 같은 봉에는 한 번만
  발동하고, `cooldown_min` 동안 재발동하지 않습니다.
- 규칙 위(앞) 순서가 우선순위입니다. 한 주기에 하나만 발동합니다.

## 지금 할 일
1. 상태를 확인하고 (방법 A 또는 B) 첫 봇 스펙을 만들어 제출하세요.
2. 예약 기능이 있다면 위의 **자동 루틴**을 6시간 주기로 등록하세요 — 그러면
   사람 개입 없이 계속 참가할 수 있습니다.
3. 스펙의 `memo` 에 전략 요지를 남기세요. 최종 순위는 4주 뒤 수익률입니다.
"""


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


class OrderNotionalRequest(BaseModel):
    value: float


class AIOrderRequest(BaseModel):
    trader: str = Field(min_length=1, max_length=32)
    action: str = Field(min_length=1, max_length=16)   # long | short | close
    notional: float = 0.0


class AISpecRequest(BaseModel):
    trader: str = Field(min_length=1, max_length=32)
    spec: dict


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
    storage_note: str = "",
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

    @app.get("/api/build")
    def get_build() -> dict:
        """서버가 지금 서빙하는 프론트엔드 번들 이름.

        브라우저가 실행 중인 번들과 다르면 화면이 낡았다는 뜻이다. 재배포 후
        옛 화면을 보면서 "왜 안 바뀌지" 로 헤매지 않게 하려는 것이다.
        """
        return {"bundle": _frontend_bundle(static_dir)}

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
        # 기록이 어디에 쌓이는지. "볼륨을 붙였는데 왜 경고가 뜨냐" 를 화면에서
        # 바로 확인할 수 있어야 한다 — 경로와 판단 근거를 함께 보낸다.
        store = supervisor.store
        payload["storage"] = {
            "path": store.path if store else None,
            "durable": bool(store and store.durable),
            "note": storage_note,
        }
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
                "order_type": config.trading.order_type,
                "limit_offset_pct": config.trading.limit_offset_pct,
                "limit_timeout_sec": config.trading.limit_timeout_sec,
                "limit_fallback_market": config.trading.limit_fallback_market,
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

    @app.get("/api/strategies")
    def get_strategies(_: str = Depends(require_auth)) -> dict:
        """전략 목록과 설명. 지금 무엇이 왜 돌고 있는지 화면에서 보이게 한다."""
        from bot.strategies import strategy_catalog

        catalog = [e for e in strategy_catalog() if e["summary"]]
        return {"active": config.strategy.name, "strategies": catalog}

    @app.get("/api/leaderboard")
    def get_leaderboard(_: str = Depends(require_auth)) -> dict:
        """전략 경쟁 순위표. 모든 전략이 같은 시세로 모의매매한 결과다."""
        rows = supervisor.leaderboard()
        return {
            "active": config.strategy.name,
            # 실거래 배지는 실제로 실거래 모드로 돌 때만 '실거래'로 표기해야
            # 한다 — 지정만 된 상태를 실거래로 보이면 사용자가 놀란다.
            "live": bool(supervisor.running and supervisor.snapshot().live),
            "leverage": config.exchange.leverage,
            # 기록이 볼륨에 남는지. False 면 재배포 때마다 성적이 초기화되므로
            # 며칠씩 모아야 하는 이 데이터에서는 치명적이다 — 화면에서 경고한다.
            "persistent": supervisor.store.durable if supervisor.store else False,
            "strategies": [
                {
                    "name": s.name,
                    "summary": s.summary,
                    "category": s.category,
                    "started_at": s.started_at,
                    "return_pct": s.return_pct,
                    "net_pnl": s.net_pnl,
                    "realized_pnl": s.realized_pnl,
                    "equity": s.equity,
                    "start_equity": s.start_equity,
                    "unrealized": s.unrealized,
                    "open_positions": s.open_positions,
                    "trade_count": s.trade_count,
                    "wins": s.wins,
                    "losses": s.losses,
                    "win_rate": s.win_rate,
                    "stop_outs": s.stop_outs,
                    "stop_out_rate": s.stop_out_rate,
                    "max_drawdown_pct": s.max_drawdown_pct,
                    "liquidation_risk_pct": s.liquidation_risk_pct,
                    "long_orders": s.long_orders,
                    "short_orders": s.short_orders,
                    "long_avg_price": s.long_avg_price,
                    "short_avg_price": s.short_avg_price,
                    "long_notional": s.long_notional,
                    "short_notional": s.short_notional,
                    "required_equity": s.required_equity,
                    "position_side": s.position_side,
                    "position_amount": s.position_amount,
                    "position_entry": s.position_entry,
                    "position_notional": s.position_notional,
                    "market_return_pct": s.market_return_pct,
                    "vs_market_pct": s.vs_market_pct,
                    "leverage": s.leverage,
                    "bankrupt": s.bankrupt,
                    "total_fee": s.total_fee,
                    "total_funding": s.total_funding,
                    "best_pnl": s.best_pnl,
                    "worst_pnl": s.worst_pnl,
                    "error": s.error,
                }
                for s in rows
            ],
        }

    @app.get("/api/settings/order-notional")
    def get_order_notional(_: str = Depends(require_auth)) -> dict:
        return {"value": supervisor.order_notional()}

    @app.post("/api/settings/order-notional")
    def set_order_notional(body: OrderNotionalRequest, request: Request,
                           _: str = Depends(require_auth)) -> dict:
        """회당 주문 금액 변경. 실행 중인 봇과 모의매매에 즉시 반영된다."""
        if not (1.0 <= body.value <= 100_000.0):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="주문 금액은 1 ~ 100,000 USDT 사이여야 합니다",
            )
        supervisor.set_order_notional(body.value)
        log.info("회당 주문 금액 변경: %.2f USDT — ip=%s", body.value, client_ip(request))
        return {"value": supervisor.order_notional()}

    # ------------------------------------------------------------------
    # AI 경쟁 매매 — 클로드/GPT/그록의 판단을 웹으로 전달받아 체결한다.
    @app.get("/api/ai/state")
    def ai_state(_: str = Depends(require_auth)) -> dict:
        return {
            "traders": supervisor.ai_state(),
            "running": supervisor.running,
            # 0 = 아직 시작 전 (첫 봇 스펙이 적용되는 순간 4주 카운트 시작)
            "competition_end_ms": supervisor.ai_competition_end_ms(),
        }

    @app.post("/api/ai/order")
    def ai_order(body: AIOrderRequest, request: Request,
                 _: str = Depends(require_auth)) -> dict:
        """AI 의 답을 주문 큐에 넣는다. 다음 봇 주기(15초)에 체결된다."""
        if body.action not in ("long", "short", "close"):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="동작은 long / short / close 중 하나여야 합니다",
            )
        if body.action != "close" and not (1.0 <= body.notional <= 5_000.0):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="주문 금액은 1 ~ 5,000 USDT 사이여야 합니다 (회당 한도)",
            )
        try:
            order = supervisor.submit_ai_order(body.trader, body.action, body.notional)
        except (SupervisorError, ValueError) as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
            ) from exc
        log.info(
            "AI 매매 주문 입력: %s %s %.2f — ip=%s",
            body.trader, body.action, body.notional, client_ip(request),
        )
        return {"ok": True, "order": order, "running": supervisor.running}

    @app.get("/api/ai/handoff")
    def ai_handoff(trader: str, request: Request, _: str = Depends(require_auth)) -> dict:
        """AI 에게 건네는 지시서(MD) — 대회 규칙·전용 토큰·API 사용법·스펙 형식."""
        traders = {t["name"]: t["label"] for t in supervisor.ai_state()}
        if trader not in traders:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"알 수 없는 AI 트레이더 '{trader}'",
            )
        try:
            token = supervisor.ai_token(trader)
        except SupervisorError as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
            ) from exc
        symbol = config.trading.symbols[0] if config.trading.symbols else "BTC/USDT:USDT"
        log.warning("AI 지시서(토큰 포함) 발급 — %s, ip=%s", trader, client_ip(request))
        return {
            "trader": trader,
            "token": token,
            "markdown": _ai_handoff_md(
                label=traders[trader], token=token,
                base_url=str(request.base_url), symbol=symbol,
            ),
        }

    @app.post("/api/ai/spec")
    def ai_spec_via_dashboard(body: AISpecRequest, request: Request,
                              _: str = Depends(require_auth)) -> dict:
        """AI 가 답으로 준 봇 스펙(JSON)을 대신 붙여넣는 경로."""
        try:
            errors = supervisor.ai_set_spec(body.trader, body.spec)
        except SupervisorError as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
            ) from exc
        if errors:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="스펙 오류: " + " / ".join(errors),
            )
        log.info("AI 봇 스펙 교체 — %s, ip=%s", body.trader, client_ip(request))
        return {"ok": True, "spec": supervisor.ai_traders[body.trader].spec}

    # --- AI 가 전용 토큰으로 직접 부르는 엔드포인트 -----------------------
    def require_ai_token(request: Request):
        token = request.headers.get("x-ai-token") or request.query_params.get("token")
        trader = supervisor.ai_trader_by_token(token)
        if trader is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="유효하지 않은 AI 토큰입니다",
            )
        return trader

    @app.get("/api/aibot/state")
    def aibot_state(request: Request) -> dict:
        """토큰의 주인에게: 시세·자기 계좌·현재 스펙. 다른 참가자 정보는 없다."""
        trader = require_ai_token(request)
        symbol = config.trading.symbols[0] if config.trading.symbols else None
        candles = supervisor.candles(symbol) if symbol else []
        row = next((s for s in supervisor.leaderboard() if s.name == trader.name), None)
        account = None
        if row is not None:
            account = {
                "equity": row.equity,
                "start_equity": row.start_equity,
                "unrealized": row.unrealized,
                "return_pct": row.return_pct,
                "market_return_pct": row.market_return_pct,
                "vs_market_pct": row.vs_market_pct,
                "position": (
                    {
                        "side": row.position_side,
                        "notional": row.position_notional,
                        "entry_price": row.position_entry,
                        "amount": row.position_amount,
                    }
                    if row.position_amount > 0 else None
                ),
                "trade_count": row.trade_count,
                "total_fee": row.total_fee,
                "total_funding": row.total_funding,
            }
        return {
            "trader": trader.name,
            "label": trader.label,
            "market": _market_snapshot(candles, config.trading.timeframe),
            "account": account,
            "spec": trader.spec,
            "pending_orders": trader.pending(),
            "bot_running": supervisor.running,
            # 대회 진행 정보 — 다음 스펙 교체 가능 시각과 대회 종료 시각(ms).
            "spec_next_allowed_ms": supervisor.ai_spec_next_allowed_ms(trader.name),
            "competition_end_ms": supervisor.ai_competition_end_ms(),
        }

    @app.post("/api/aibot/spec")
    async def aibot_spec(request: Request) -> dict:
        """토큰의 주인이 자기 봇 스펙을 교체한다."""
        trader = require_ai_token(request)
        try:
            raw = await request.json()
        except Exception as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="본문이 올바른 JSON 이 아닙니다",
            ) from exc
        errors = supervisor.ai_set_spec(trader.name, raw)
        if errors:
            return JSONResponse(
                status_code=status.HTTP_400_BAD_REQUEST,
                content={"ok": False, "errors": errors},
            )
        log.info("AI 봇 스펙 교체(토큰) — %s", trader.name)
        return {"ok": True, "errors": [], "spec": trader.spec}

    @app.get("/api/aibot/spec/set")
    def aibot_spec_via_url(request: Request, spec: str = "") -> dict:
        """URL 하나 여는 것으로 스펙을 제출한다 — 웹 자동화(루틴) AI 용.

        ChatGPT Tasks, Gemini 예약 작업 같은 웹 자동화 기능은 대부분 헤더를
        붙인 POST 를 못 하고 'URL 열기'만 할 수 있다. 그래서 GET + 쿼리로도
        제출을 받는다: /api/aibot/spec/set?token=...&spec=<URL인코딩된 JSON>.
        토큰이 곧 권한이고 모의매매 한정이라 GET 제출의 위험은 없다.
        """
        trader = require_ai_token(request)
        if not spec:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="spec 쿼리 파라미터에 URL 인코딩된 JSON 을 넣으세요",
            )
        try:
            import json as _json
            raw = _json.loads(spec)
        except ValueError as exc:
            return JSONResponse(
                status_code=status.HTTP_400_BAD_REQUEST,
                content={"ok": False,
                         "errors": [f"spec 이 올바른 JSON 이 아닙니다: {exc}"]},
            )
        errors = supervisor.ai_set_spec(trader.name, raw)
        if errors:
            return JSONResponse(
                status_code=status.HTTP_400_BAD_REQUEST,
                content={"ok": False, "errors": errors},
            )
        log.info("AI 봇 스펙 교체(URL) — %s", trader.name)
        return {"ok": True, "errors": [], "spec": trader.spec}

    @app.post("/api/leaderboard/reset")
    def reset_leaderboard(body: CloseAllRequest, request: Request,
                          _: str = Depends(require_auth)) -> dict:
        """모의매매 기록을 지우고 처음부터 다시 비교한다."""
        if body.confirm != "RESET":
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="모의매매 기록을 지우려면 확인 문구 'RESET' 이 필요합니다",
            )
        log.warning("모의매매 기록 초기화 — ip=%s", client_ip(request))
        supervisor.reset_paper()
        return {"ok": True}

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
            "persistent": bool(supervisor.store and supervisor.store.durable),
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
            # 자산 파일명에는 내용 해시가 들어 있어 내용이 바뀌면 이름도 바뀐다.
            # 그래서 오래 캐시해도 안전하다.
            app.mount("/assets", StaticFiles(directory=assets), name="assets")

        index = static_dir / "index.html"

        @app.get("/")
        def serve_index() -> FileResponse:
            # index.html 은 절대 캐시하면 안 된다. 이 파일이 낡으면 브라우저가
            # 예전 자산을 계속 불러와, 재배포해도 옛 화면이 그대로 뜬다.
            return FileResponse(
                index,
                headers={"Cache-Control": "no-cache, must-revalidate", "Pragma": "no-cache"},
            )
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
