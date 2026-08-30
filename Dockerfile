# syntax=docker/dockerfile:1

# ---------- 1단계: 프론트엔드 빌드 ----------
FROM node:22-slim AS frontend

WORKDIR /app/frontend

# 의존성 설치를 소스 복사와 분리해 두면 소스만 바뀔 때 npm ci 를 건너뛴다
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci

COPY frontend/ ./
# vite 의 outDir 이 ../bot/web/static 이라 /app/bot/web/static 에 생성된다
RUN npm run build


# ---------- 2단계: 런타임 ----------
FROM python:3.11-slim AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    # Railway 볼륨을 /data 에 마운트하면 재배포 후에도 로그가 남는다.
    # 볼륨이 없거나 쓸 수 없으면 경고만 남기고 콘솔 로그로 계속 동작한다.
    LOG_FILE=/data/logs/bot.log \
    # 로컬 `docker run` 기본 포트. Railway 는 자체 PORT 를 주입해 덮어쓴다.
    PORT=8000

WORKDIR /app

COPY requirements.txt requirements-web.txt ./
RUN pip install --no-cache-dir -r requirements-web.txt

COPY bot/ ./bot/
COPY --from=frontend /app/bot/web/static ./bot/web/static

EXPOSE 8000

# 설정은 CONFIG_YAML 환경변수로 넣는다. 없으면 기동 시 명확한 오류를 남기고
# 멈춘다 — 예시 기본값으로 실거래를 시작하는 일은 없어야 한다.
CMD ["python", "-m", "bot", "web"]
