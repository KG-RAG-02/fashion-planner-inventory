# stock-agent (과잉재고 에이전트) HTTP API 이미지
# DB와 이미지는 이미지에 넣지 않는다. R2 모드는 STOCK_CACHE_DIR(/cache)의 검증된 cache를, local-file 모드는
# STOCK_DB_PATH 파일을 read-only로 연다. uid/gid 10001로 실행되고 /app에 쓰지 않으므로 read-only root filesystem
# 에서는 /tmp(tmpfs)와 /cache(volume 또는 tmpfs)만 쓰기 가능하게 주면 된다.
# CLIP 유사도 필터는 재고 CLIP 인덱스가 있을 때만 동작하고, 없으면 건너뛴다. 인덱스는 R2(R2_CLIP_INDEX_KEY)에서
# --prepare-cache가 DB와 함께 /cache에 받아 두거나, STOCK_CLIP_INDEX_PATH로 붙인 파일을 쓴다.
# 인덱스가 있으면 서버 시작 때 CLIP 모델(ViT-B-32)을 미리 불러오므로 healthcheck start-period를 길게 둔다.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app
COPY requirements.txt .
# torch CPU-only 먼저 설치 → open-clip-torch가 CUDA 버전을 당기지 않도록
RUN pip install --no-cache-dir torch --index-url https://download.pytorch.org/whl/cpu \
 && pip install --no-cache-dir -r requirements.txt \
 && groupadd --system --gid 10001 app \
 && useradd --system --uid 10001 --gid 10001 --home-dir /tmp --no-create-home --shell /usr/sbin/nologin app \
 && install -d -o 10001 -g 10001 -m 0700 /cache

COPY stock_agent ./stock_agent

ENV HOME=/tmp \
    STOCK_CACHE_DIR=/cache
USER 10001:10001

EXPOSE 8002
# Readiness (GET /health/ready, no external calls): healthy only when the DB is open and passes the checks.
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
  CMD ["python", "-m", "stock_agent.server", "--check-ready"]

CMD ["python", "-m", "stock_agent.server", "--host", "0.0.0.0"]
