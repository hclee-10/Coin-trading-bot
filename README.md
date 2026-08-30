# Coin Trading Bot

Bitget / OKX **USDT 무기한 선물** 자동매매 봇. 거래소는 `ccxt` 로 추상화되어
있어 설정 한 줄로 갈아탈 수 있고, 매매 전략은 플러그인으로 끼워 넣는다.

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

두 거래소 모두 **API 키 + 시크릿 + 패스프레이즈** 세 가지를 준다. 발급할 때:

- **권한은 "거래(Trade)"만 켠다. "출금(Withdraw)"은 절대 켜지 않는다.**
- 가능하면 **IP 화이트리스트**를 건다 (봇을 돌릴 서버의 고정 IP).
- 자금을 **선물(스왑) 계좌**로 이체해 둔다. 현물 계좌 잔고는 보이지 않는다.
- 포지션 모드를 **단방향(one-way)** 으로 설정한다. 이 봇은 헤지 모드를
  지원하지 않으며, 설정에서도 막아 둔다.

| 거래소 | 발급 위치 |
|---|---|
| OKX | 계정 → API → V5 API 키 생성 (passphrase 직접 지정) |
| Bitget | 계정 → API 관리 → 새 키 생성 (passphrase 직접 지정) |

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
| `exchange.id` | `okx` 또는 `bitget` |
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

## 5. 리스크 관리 — 수량이 정해지는 방식

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

## 6. 전략 추가하기

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

## 7. 구조

```
bot/
  models.py            공유 데이터 모델 (Candle, Position, Order, Signal …)
  config.py            YAML 설정 + 환경변수 시크릿, 검증
  exchanges/
    base.py            FuturesExchange 인터페이스 (+ 계약 수량 환산 기본 구현)
    ccxt_futures.py    Bitget/OKX 공용 ccxt 어댑터 — 거래소 차이는 전부 여기
    factory.py         설정 → 어댑터
  strategies/
    base.py            Strategy 인터페이스, StrategyContext, 레지스트리
    hold.py            아무것도 하지 않는 기본 전략
    template.py        새 전략용 스켈레톤
  risk.py              사이징, 한도, 일일 손실 킬스위치
  execution.py         신호 → 주문 (주문이 나가는 유일한 지점)
  engine.py            메인 루프, 오류 격리, 그레이스풀 종료
  cli.py               커맨드라인
tests/                 네트워크 없이 도는 단위 테스트 (가짜 거래소 사용)
```

거래소별로 다른 것은 `ccxt_futures.py` 안에서만 다룬다:

- **수량 단위** — OKX 스왑은 주문 수량이 *계약 수*이고 1계약 = `contractSize`
  베이스 코인이다(예: BTC-USDT-SWAP 은 0.01 BTC). Bitget 스왑은 베이스 코인
  단위다. 상위 계층은 항상 베이스 코인으로 생각하고, `base_to_contracts()` 가
  환산한다. **이 환산을 빠뜨리면 OKX에서 주문 수량이 100배로 나간다.**
- **레버리지/마진 모드** — 파라미터 이름이 다르고, Bitget 격리 마진은 롱/숏에
  각각 걸어야 한다. 포지션이 열려 있으면 변경이 거부되는데, 그 경우 기존 설정을
  쓰는 게 맞으므로 경고만 남기고 진행한다.
- **조건부 주문** — 손절/익절은 ccxt 통합 파라미터로 보내고, 취소는 일반 주문과
  트리거 주문을 각각 훑는다(거래소마다 대량 취소의 트리거 주문 포함 여부가 다름).

## 8. 테스트

```bash
pip install -r requirements-dev.txt
pytest
```

가짜 거래소(`tests/fakes.py`)를 쓰므로 네트워크도 API 키도 필요 없다.

## 9. 알려진 한계

- **단방향(one-way) 모드만 지원.** 헤지 모드는 설정 단계에서 막는다.
- **백테스트 없음.** 과거 데이터로 전략을 검증하는 기능은 포함되어 있지 않다.
- **불타기/물타기 없음.** 같은 방향 신호가 또 와도 추가 진입하지 않는다.
- **주문 체결 확인 없음.** 시장가 주문이 즉시 체결된다고 가정한다. 유동성이
  얕은 심볼에서는 부분 체결이 그대로 남을 수 있다.
- **상태 저장 없음.** 봇을 재시작하면 거래소의 실제 포지션에서 다시 시작한다
  (전략 내부 상태는 초기화된다).
- **일일 손실 킬스위치는 자기자본 스냅샷 기준이다.** 봇이 꺼져 있는 동안의
  변동은 반영되지 않는다.

## 10. 실거래 전 체크리스트

- [ ] API 키에 출금 권한이 꺼져 있는가
- [ ] `python -m bot check` 가 정상 잔고를 보여주는가
- [ ] `python -m bot run` (DRY-RUN)으로 로그를 충분히 관찰했는가
- [ ] `risk.risk_per_trade_pct` 와 `max_daily_loss_pct` 가 감당 가능한 값인가
- [ ] 첫 실거래는 최소 금액으로, 심볼 하나로 시작하는가
