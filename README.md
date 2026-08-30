# Coin Trading Bot

Gate.io / Bitget / OKX **USDT 무기한 선물** 자동매매 봇. 거래소는 `ccxt` 로 추상화되어
있어 설정 한 줄로 갈아탈 수 있고, 매매 전략은 플러그인으로 끼워 넣는다.
터미널에서도, 브라우저 대시보드에서도 돌릴 수 있다.

현재 상태: **골격 완성.** 데이터 수집 → 전략 판단 → 리스크 사이징 → 주문
전송 → 손절/익절 등록까지 전 경로가 동작한다. 매매 전략 자체는 비어 있다
(`hold` 는 아무것도 하지 않고, `template` 은 복사해 쓰는 스켈레톤이다).

> ⚠️ **선물 거래는 원금 전액을 잃을 수 있고, 레버리지 때문에 청산되면 그보다
> 빨리 잃는다.** 이 코드는 있는 그대로 제공되며 수익을 보장하지 않는다.
> 반드시 잃어도 되는 금액으로만, 소액부터 시작할 것.

---

## 1. 설치

```bash
python3 -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

## 2. API 키 발급

발급할 때 지켜야 할 것:

- **권한은 "거래(Trade)"만 켠다. "출금(Withdraw)"은 절대 켜지 않는다.**
- 가능하면 **IP 화이트리스트**를 건다 (봇을 돌릴 서버의 고정 IP).
- 자금을 **선물(스왑) 계좌**로 이체해 둔다. 현물 계좌 잔고는 보이지 않는다.
- 포지션 모드를 **단방향(one-way)** 으로 설정한다. 이 봇은 헤지 모드를
  지원하지 않으며, 설정에서도 막아 둔다.

| 거래소 | 발급 위치 | 필요한 값 |
|---|---|---|
| Gate.io | 계정 → API 키 관리 → APIv4 키 생성 | 키, 시크릿 (**패스프레이즈 없음**) |
| Bitget | 계정 → API 관리 → 새 키 생성 | 키, 시크릿, 패스프레이즈 |
| OKX | 계정 → API → V5 API 키 생성 | 키, 시크릿, 패스프레이즈 |

Gate.io 는 API 패스프레이즈 개념이 없어 키와 시크릿 두 개만 넣으면 된다.
Gate 키를 만들 때는 권한에서 **Perpetual Futures(무기한 선물)** 를 켜야 한다 —
Spot 권한만으로는 선물 주문이 나가지 않는다.

```bash
cp .env.example .env
# .env 를 열어 사용할 거래소 블록의 값을 채운다
```

`.env` 와 `config.yaml` 은 `.gitignore` 에 있어 커밋되지 않는다.

## 3. 설정

```bash
cp config.example.yaml config.yaml
```

주요 항목:

| 항목 | 의미 |
|---|---|
| `exchange.id` | `gate`, `bitget`, `okx` 중 하나 |
| `exchange.leverage` | 레버리지. `risk.max_leverage` 를 넘으면 시작하지 않는다 |
| `trading.symbols` | `"BTC/USDT:USDT"` 형식. `"BTC/USDT"` 는 현물이라 거부된다 |
| `trading.poll_interval_sec` | 판단 주기(초). 최소 1초 (레이트리밋 보호) |
| `trading.allow_reverse` | 반대 신호에서 청산 후 즉시 반대 진입 허용 |
| `strategy.name` | `python -m bot strategies` 로 목록 확인 |
| `risk.risk_per_trade_pct` | **수량은 이 값에서 역산된다** (아래 참조) |
| `risk.max_daily_loss_pct` | 일일 손실이 이 값을 넘으면 신규 진입 차단 |

설정 키에 오타가 있으면 조용히 무시하지 않고 에러로 알려 준다.

## 4. 실행

```bash
python -m bot strategies    # 등록된 전략 목록
python -m bot check         # 연결·잔고·심볼 규격 점검 (주문 없음)
python -m bot positions     # 현재 포지션
python -m bot run           # DRY-RUN — 주문을 전송하지 않는다
python -m bot run --live    # 실거래. LIVE 입력 확인을 거친다
python -m bot close --live  # 보유 포지션 전부 시장가 청산
```

`run` 은 **기본이 DRY-RUN** 이다. `--live` 없이는 사이징과 규격 보정까지 전부
수행하고 주문 전송만 건너뛰므로, 실제 자금 없이 전체 경로를 확인할 수 있다.
무인 실행(systemd 등)에서는 `--live --yes` 로 확인 프롬프트를 건너뛴다.

`Ctrl+C` 또는 `SIGTERM` 을 받으면 현재 주기를 마치고 종료한다.
`trading.close_positions_on_exit: true` 면 종료 시 포지션을 정리한다.

## 5. 웹 대시보드

브라우저에서 봇 상태를 보고 시작·정지·긴급 청산까지 할 수 있다. 봇은 서버에서
돌고 브라우저는 이 서버의 API 만 호출한다 — **API 키는 절대 브라우저로
내려가지 않는다.**

### 준비

```bash
pip install -r requirements-web.txt

# 프론트엔드 빌드 (Node 20+ 필요). 산출물은 bot/web/static/ 에 생성된다.
cd frontend && npm install && npm run build && cd ..

# 대시보드 계정 생성 → 출력된 두 줄을 .env 에 붙여넣기
python -m bot hash-password
```

`WEB_USERNAME` 과 `WEB_PASSWORD_HASH` 가 모두 없으면 서버는 뜨지 않는다.
기본 계정은 존재하지 않으며, 비밀번호가 12자 미만이면 거부된다.

### 실행

```bash
python -m bot web                      # http://127.0.0.1:8000
python -m bot web --port 9000          # 포트 변경
```

기본 바인드 주소는 `127.0.0.1` 이다. 외부에서 접속하려면 `--host 0.0.0.0` 으로
직접 여는 대신 **리버스 프록시 뒤에 두는 구성**을 쓴다 (6절).

프론트엔드를 고치면서 개발할 때는 `python -m bot web` 을 띄워 둔 채로:

```bash
cd frontend && npm run dev    # http://localhost:5173, /api 는 8000 으로 프록시
```

### 화면에서 할 수 있는 것

| 기능 | 설명 |
|---|---|
| 상태 카드 | 자기자본, 오늘 손익(UTC 기준), 보유 포지션 수, 마지막 주기 시각 |
| 포지션 표 | 방향, 수량, 진입가, 명목가, 미실현 손익, 청산가 |
| 실시간 로그 | 봇 로그를 그대로 스트리밍. 위로 스크롤하면 자동 추적이 멈춘다 |
| 봇 제어 | DRY-RUN 시작 / 실거래 시작 / 정지 |
| 긴급 전체 청산 | 봇을 멈추고 보유 포지션을 전부 시장가 청산 |

**킬스위치가 걸리면** 화면 상단에 배너가 뜨고 사유가 표시된다. **실거래 모드로
도는 동안에도** 붉은 배너가 계속 떠 있다 — DRY-RUN 인 줄 알고 방치하는 사고를
막기 위해서다.

### 보안 모델

웹 UI 를 붙이는 순간 내 계좌를 조작할 수 있는 창구가 네트워크에 열린다.
그래서 다음이 기본값이다:

- **아이디와 비밀번호로 로그인한다.** 비밀번호는 scrypt 로 해시해 저장하고
  원문은 어디에도 남지 않는다. 아이디가 틀려도 비밀번호 해시 계산을 건너뛰지
  않아, 응답 시간으로 아이디 존재 여부가 새어 나가지 않는다.
- **실패 메시지는 어느 쪽이 틀렸는지 알려 주지 않는다.**
- **세션 토큰은 서버 메모리에만 있다.** 프로세스를 재시작하면 전부 무효가 되고,
  로그아웃이 즉시 반영된다(JWT 와 달리 폐기가 확실하다). 기본 만료는 12시간.
- **로그인 실패는 IP 단위로 5회까지**, 초과하면 5분 잠금.
- **자금이 움직이는 동작은 확인 문구를 요구한다** — 실거래 시작은 `LIVE`,
  긴급 청산은 `CLOSE`. 프론트엔드와 백엔드가 각각 검사한다.
- **제어 동작은 접속 IP 와 함께 로그에 남는다.** 리버스 프록시 뒤에서는
  `X-Forwarded-For` 를 **오른쪽에서** 읽는다 — 왼쪽 값은 클라이언트가 위조할
  수 있어서, 그쪽을 믿으면 매 시도마다 IP 를 바꿔 시도 제한을 통째로 우회한다.
- **응답에 시크릿이 없다.** `/api/config` 는 거래 파라미터만 돌려준다.
- CSP·X-Frame-Options 등 보안 헤더를 붙이고, 외부 스크립트를 일절 쓰지 않는다.

브라우저가 하는 일은 서버 API 호출뿐이다. 거래소를 직접 부르지 않는다.

## 6. Railway 배포

`Dockerfile` 과 `railway.json` 이 들어 있어 저장소를 연결하면 바로 배포된다.
HTTPS 와 리버스 프록시는 Railway 가 처리한다.

### 1) 계정 만들기

```bash
python -m bot hash-password
```

아이디와 비밀번호를 입력하면 환경변수 두 줄이 나온다. 비밀번호 원문은 어디에도
저장되지 않으니 따로 기억해 둘 것.

### 2) 서비스 생성

Railway 대시보드에서 **New Project → Deploy from GitHub repo** 로 이 저장소를
고른다. `railway.json` 을 읽어 Dockerfile 로 빌드하고 `/healthz` 로 헬스체크한다.

### 3) 환경변수

Variables 탭에서 설정한다. **Railway 는 `PORT` 를 자동으로 주입하므로 직접
넣지 않는다.**

| 변수 | 필수 | 설명 |
|---|---|---|
| `WEB_USERNAME` | ✅ | 대시보드 아이디 |
| `WEB_PASSWORD_HASH` | ✅ | `hash-password` 가 출력한 `scrypt$...` 해시 |
| `GATE_API_KEY` / `GATE_API_SECRET` | ✅ | Gate.io 를 쓸 때 (패스프레이즈 없음) |
| `BITGET_API_KEY` / `BITGET_API_SECRET` / `BITGET_API_PASSPHRASE` | ✅ | Bitget 을 쓸 때 |
| `OKX_API_KEY` / `OKX_API_SECRET` / `OKX_API_PASSPHRASE` | ✅ | OKX 를 쓸 때 |
| `CONFIG_YAML` | ✅ | `config.example.yaml` 내용을 그대로 붙여넣고 수정 |
| `LOG_FILE` | — | 기본 `/data/logs/bot.log` (볼륨 경로) |
| `TRUST_PROXY` | — | Railway 에서는 자동으로 켜진다 |

`CONFIG_YAML` 은 여러 줄을 그대로 붙여넣으면 된다. 설정을 바꾸려면 이 값을
고치고 재배포한다.

### 4) 로그 볼륨

재배포·재시작하면 컨테이너 파일시스템은 초기화된다. 로그를 남기려면 서비스에
**Volume 을 추가하고 마운트 경로를 `/data`** 로 지정한다. 볼륨이 없어도 서버는
정상 동작하고, 로그는 Railway 로그 뷰어와 대시보드 화면에서 볼 수 있다.

### 5) 도메인

Settings → Networking → **Generate Domain** 을 누르면 `*.up.railway.app` 주소와
HTTPS 인증서가 자동으로 붙는다.

### 6) 배포 후 반드시 확인할 것

- [ ] 생성된 도메인으로 접속해 로그인되는가
- [ ] 잘못된 비밀번호를 여러 번 넣으면 잠기는가
- [ ] **Replicas 가 1인가** (아래 참조)
- [ ] 서비스가 잠들지 않도록 설정되어 있는가 (아래 참조)

---

### ⚠️ Railway 에서 특히 주의할 것

**레플리카는 반드시 1개.** `railway.json` 에 `numReplicas: 1` 로 고정해 두었다.
2개 이상이면 **봇이 두 개 돌면서 같은 신호에 주문을 두 번 낸다** — 의도한
포지션의 두 배가 잡힌다. 세션 토큰도 각 인스턴스 메모리에 따로 있어서 로그인이
계속 풀린다. 스케일업하지 말 것.

**서비스가 잠들면 매매가 멈춘다.** Railway 의 앱 슬립(App Sleeping)이 켜져
있으면 트래픽이 없을 때 컨테이너가 정지한다. 봇에게는 치명적이다 — 포지션을
연 채로 멈추면 손절 주문은 거래소에 걸려 있지만 그 뒤의 판단은 아무도 하지
않는다. Settings 에서 **앱 슬립을 반드시 꺼 둔다.**

**거래소 API 키에 IP 화이트리스트를 걸 수 없다.** Railway 의 아웃바운드 IP 는
고정이 아니라서, 화이트리스트를 걸면 어느 순간 주문이 거부되기 시작한다.
IP 제한이라는 방어선 하나를 포기하는 셈이므로, **API 키 권한에서 출금은
반드시 꺼 두는 것**이 그만큼 더 중요해진다.

**재배포하면 봇은 멈춘 상태로 시작한다.** 컨테이너가 새로 뜰 때 자동으로
매매를 시작하지 않는다 — 대시보드에서 직접 눌러야 한다. 의도적인 설계다.
설정을 바꿔 재배포한 뒤에는 봇이 꺼져 있다는 것을 기억할 것.

**아직 2단계 인증이 없다.** 인터넷에 공개된 주소에서 아이디와 비밀번호만으로
계좌 제어권을 지키는 상태다. 비밀번호는 길고 다른 곳에서 쓰지 않는 것으로
정하고, 도메인 주소를 공유하지 말 것. TOTP 2단계 인증 추가를 권한다.

### 로컬에서 이미지 확인하기

```bash
docker build -t coin-trading-bot .
docker run --rm -p 8000:8000 \
  -e WEB_USERNAME=trader \
  -e WEB_PASSWORD_HASH='scrypt$...' \
  -e GATE_API_KEY=... -e GATE_API_SECRET=... \
  -e CONFIG_YAML="$(cat config.yaml)" \
  coin-trading-bot
```

## 7. VPS 배포 (직접 서버를 운영할 때)

`--host 0.0.0.0` 으로 직접 여는 것은 권하지 않는다. HTTPS 없이 열면 비밀번호와
세션 토큰이 평문으로 오간다. 아래 구성을 쓴다:

```
브라우저 ──HTTPS──> nginx (443) ──HTTP──> 봇 서버 (127.0.0.1:8000)
```

### 1) 봇 서버는 localhost 로만

```bash
python -m bot web --host 127.0.0.1 --port 8000
```

### 2) systemd 서비스

`/etc/systemd/system/tradingbot.service`:

```ini
[Unit]
Description=Coin Trading Bot Dashboard
After=network-online.target

[Service]
Type=simple
User=tradingbot
WorkingDirectory=/opt/coin-trading-bot
ExecStart=/opt/coin-trading-bot/.venv/bin/python -m bot web --host 127.0.0.1 --port 8000
Restart=on-failure
RestartSec=10
# .env 는 이 사용자만 읽을 수 있어야 한다 (chmod 600)
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true
ReadWritePaths=/opt/coin-trading-bot/logs

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl enable --now tradingbot
sudo journalctl -u tradingbot -f
```

**서버가 재시작되어도 봇은 자동으로 매매를 시작하지 않는다.** 대시보드에서
직접 시작해야 한다 — 의도적인 설계다.

### 3) nginx + HTTPS

```nginx
server {
    listen 443 ssl http2;
    server_name bot.example.com;

    ssl_certificate     /etc/letsencrypt/live/bot.example.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/bot.example.com/privkey.pem;

    # 가능하면 접속 IP 를 제한한다 — 가장 효과적인 한 줄이다
    # allow 203.0.113.7;
    # deny all;

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}

server {
    listen 80;
    server_name bot.example.com;
    return 301 https://$host$request_uri;
}
```

인증서는 `certbot --nginx -d bot.example.com` 으로 발급한다.

### 4) 방화벽

```bash
sudo ufw allow 22/tcp
sudo ufw allow 443/tcp
sudo ufw enable          # 8000 은 열지 않는다 — nginx 만 접근하면 된다
```

### 5) 배포 후 확인

- [ ] `https://` 로 접속되고 인증서 경고가 없는가
- [ ] `http://<서버IP>:8000` 이 **밖에서 접속되지 않는가**
- [ ] 틀린 비밀번호 6회 시도 후 잠기는가
- [ ] 거래소 API 키에 이 VPS 의 IP 화이트리스트가 걸려 있는가
- [ ] `.env` 파일 권한이 `600` 인가

> 접속 IP 제한(`allow`/`deny`)을 걸 수 있다면 반드시 걸 것. 비밀번호 하나만으로
> 자금 제어권을 지키는 것보다 훨씬 안전하다.

## 8. 리스크 관리 — 수량이 정해지는 방식

전략은 **방향만** 정하고, **얼마나 걸지는 `RiskManager` 가 정한다.** 전략을
갈아 끼워도 계좌 보호 규칙이 그대로 남게 하려는 의도적인 분리다.

```
위험금액 = 자기자본 × risk_per_trade_pct% × 신호강도
수량     = 위험금액 ÷ |진입가 − 손절가|
```

즉 **손절까지 갔을 때 잃는 금액이 먼저 고정되고, 거기서 수량이 역산된다.**
손절이 타이트할수록 수량이 커지고, 넓을수록 작아진다. 그 다음 두 상한이
적용된다:

- `max_position_notional_pct` — 자기자본 대비 포지션 명목가 상한
- `max_leverage` — 레버리지가 허용하는 명목가 상한

계산된 명목가가 `min_order_notional` 이나 거래소 최소 주문금액보다 작으면
주문을 내지 않고 사유를 로그에 남긴다.

**손절 없는 진입은 불가능하다.** 전략이 손절가를 주지 않으면
`default_stop_loss_pct` 로 채워진다 — 사이징 자체가 손절폭에 기반하기 때문이다.

**킬스위치**: UTC 일자 시작 자기자본 대비 `max_daily_loss_pct` 이상 손실이 나면
신규 진입이 차단된다. 청산 신호는 계속 처리된다(빠져나올 길은 막지 않는다).
다음 UTC 일자에 자동 해제된다.

## 9. 전략 추가하기

```bash
cp bot/strategies/template.py bot/strategies/my_strategy.py
```

1. `@register_strategy("my_strategy")` 로 이름을 바꾸고 클래스명을 고친다.
2. `generate(ctx)` 에 진입/청산 조건을 작성하고 `Signal` 을 반환한다.
3. `bot/strategies/__init__.py` 의 import 줄에 모듈을 추가한다.
4. `config.yaml` 의 `strategy.name` 을 그 이름으로 바꾼다.

`generate()` 가 받는 `ctx` 에는 캔들, 티커, 현재 포지션, 자기자본이 들어 있다.
지표를 계산할 때는 `ctx.closed_candles` 를 쓰는 편이 좋다 — `ctx.candles` 의
마지막 원소는 아직 진행 중인 캔들이라 값이 계속 바뀌어서 신호가 흔들린다.

지켜야 할 계약:

- `generate()` 는 부작용이 없어야 한다. 주문을 직접 보내지 않는다.
- 수량을 반환하지 않는다. `strength`(0~1)로 확신도만 표현하면 사이징에 배수로
  반영된다.
- `warmup_candles` 를 선언하면 엔진이 캔들이 충분히 쌓일 때까지 호출을 미룬다.

## 10. 구조

```
bot/
  models.py            공유 데이터 모델 (Candle, Position, Order, Signal …)
  config.py            YAML 설정 + 환경변수 시크릿, 검증
  exchanges/
    base.py            FuturesExchange 인터페이스 (+ 계약 수량 환산 기본 구현)
    ccxt_futures.py    Gate/Bitget/OKX 공용 ccxt 어댑터 — 거래소 차이는 전부 여기
    factory.py         설정 → 어댑터
  strategies/
    base.py            Strategy 인터페이스, StrategyContext, 레지스트리
    hold.py            아무것도 하지 않는 기본 전략
    template.py        새 전략용 스켈레톤
  risk.py              사이징, 한도, 일일 손실 킬스위치
  execution.py         신호 → 주문 (주문이 나가는 유일한 지점)
  engine.py            메인 루프, 오류 격리, 그레이스풀 종료
  cli.py               커맨드라인
  web/
    auth.py            scrypt 비밀번호, 세션 토큰, 로그인 시도 제한
    supervisor.py      봇 스레드 생명주기 + 거래소 동시 접근 차단
    app.py             FastAPI 라우트, 보안 헤더, 확인 문구 검사
    static/            프론트엔드 빌드 산출물 (npm run build 로 생성)
frontend/              React 대시보드 소스 (Vite)
tests/                 네트워크 없이 도는 단위 테스트 (가짜 거래소 사용)
```

웹 계층에서 지킨 동시성 원칙 하나: **거래소 객체는 한 번에 한 스레드만
만진다.** ccxt 의 동기 클라이언트는 `requests.Session` 을 재사용해 스레드
안전하지 않다. 그래서 봇이 도는 동안 대시보드는 봇 루프가 이미 받아 둔
결과만 읽고, 봇이 멈춰 있을 때만 요청 스레드가 짧게 거래소를 조회한다
(그마저도 5초 캐시를 거친다 — 대시보드 폴링과 열린 탭 수가 레이트리밋을
태우지 않도록). 긴급 청산은 봇을 먼저 멈춘 뒤 실행한다.

거래소별로 다른 것은 `ccxt_futures.py` 안에서만 다룬다:

- **수량 단위** — OKX 와 Gate 스왑은 주문 수량이 *계약 수*이고 1계약 =
  `contractSize` 베이스 코인이다(OKX BTC-USDT-SWAP 은 0.01 BTC, Gate BTC_USDT
  는 0.0001 BTC). Bitget 스왑은 베이스 코인 단위다. 상위 계층은 항상 베이스
  코인으로 생각하고, `base_to_contracts()` 가 환산한다. **이 환산을 빠뜨리면
  Gate 에서 주문 수량이 1만 배로 나간다.**
- **자격증명 구성** — Gate 는 패스프레이즈가 없고 Bitget·OKX 는 필수다. 어느
  쪽인지는 ccxt 가 선언한 값을 그대로 읽어 판단한다.
- **레버리지/마진 모드** — 셋이 전부 다르다. Bitget 격리는 롱/숏에 각각 걸어야
  하고, Gate 는 마진 모드 전용 API 가 없어 클라이언트 옵션으로 지정하며(파라미터로
  넘기면 요청에 그대로 실려 거부될 수 있다), OKX 는 통합 파라미터를 받는다.
  포지션이 열려 있으면 변경이 거부되는데, 그 경우 기존 설정을 쓰는 게 맞으므로
  경고만 남기고 진행한다.
- **조건부 주문** — 손절/익절은 ccxt 통합 파라미터로 보내고, 취소는 일반 주문과
  트리거 주문을 각각 훑는다(거래소마다 대량 취소의 트리거 주문 포함 여부가 다름).

## 11. 테스트

```bash
pip install -r requirements-dev.txt
pytest
```

가짜 거래소(`tests/fakes.py`)를 쓰므로 네트워크도 API 키도 필요 없다. 웹 API
테스트는 FastAPI 의 `TestClient` 로 실제 라우트를 통과시킨다 — 인증 우회,
확인 문구 없는 주문, 시크릿 노출을 각각 검증한다.

## 12. 알려진 한계

- **단방향(one-way) 모드만 지원.** 헤지 모드는 설정 단계에서 막는다.
- **백테스트 없음.** 과거 데이터로 전략을 검증하는 기능은 포함되어 있지 않다.
- **불타기/물타기 없음.** 같은 방향 신호가 또 와도 추가 진입하지 않는다.
- **주문 체결 확인 없음.** 시장가 주문이 즉시 체결된다고 가정한다. 유동성이
  얕은 심볼에서는 부분 체결이 그대로 남을 수 있다.
- **상태 저장 없음.** 봇을 재시작하면 거래소의 실제 포지션에서 다시 시작한다
  (전략 내부 상태는 초기화된다).
- **일일 손실 킬스위치는 자기자본 스냅샷 기준이다.** 봇이 꺼져 있는 동안의
  변동은 반영되지 않는다.
- **대시보드는 폴링 방식이다** (2초 간격). WebSocket 실시간 푸시가 아니라
  최대 2초의 지연이 있다.
- **대시보드에서 설정을 바꿀 수 없다.** 심볼·레버리지·리스크 파라미터는
  `config.yaml` 을 고치고 봇을 재시작해야 반영된다.
- **사용자는 한 명뿐이다.** 아이디/비밀번호 한 쌍을 쓰는 단일 계정 모델이라
  누가 버튼을 눌렀는지는 IP 로만 구분된다.
- **2단계 인증이 없다.** 비밀번호가 유출되면 그대로 계좌 제어권이 넘어간다.
  인터넷에 공개된 주소로 운영한다면 TOTP 추가를 권한다.
- **인스턴스는 하나만 띄울 수 있다.** 봇 상태와 세션 토큰이 프로세스 메모리에
  있어서, 두 개 이상 돌리면 주문이 중복되고 로그인이 계속 풀린다.
- **로그 버퍼는 메모리에 최근 1000줄만 둔다.** 그 이상은 로그 파일을 본다.

## 13. 실거래 전 체크리스트

- [ ] API 키에 출금 권한이 꺼져 있는가
- [ ] `python -m bot check` 가 정상 잔고를 보여주는가
- [ ] `python -m bot run` (DRY-RUN)으로 로그를 충분히 관찰했는가
- [ ] `risk.risk_per_trade_pct` 와 `max_daily_loss_pct` 가 감당 가능한 값인가
- [ ] 첫 실거래는 최소 금액으로, 심볼 하나로 시작하는가
- [ ] (배포 시) 인스턴스가 1개인가 — 2개면 주문이 두 배로 나간다
- [ ] (배포 시) 서비스가 잠들지 않도록 설정했는가
