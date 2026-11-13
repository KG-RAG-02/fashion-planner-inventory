# stock-agent (과잉재고 에이전트) HTTP API 이미지
# DB와 이미지는 Cloudflare R2에서 런타임에 읽으며 이미지에 포함하지 않는다.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    STOCK_CLIP_INDEX_PATH=/data/clip_index.npz

WORKDIR /app
COPY requirements.txt .
# torch CPU-only 먼저 설치 → open-clip-torch가 CUDA 버전을 당기지 않도록
RUN pip install --no-cache-dir torch --index-url https://download.pytorch.org/whl/cpu && \
    pip install --no-cache-dir -r requirements.txt

COPY stock_agent ./stock_agent

RUN useradd --create-home app
USER app

EXPOSE 8002
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://localhost:8002/health').status==200 else 1)"

CMD ["uvicorn", "stock_agent.api:app", "--host", "0.0.0.0", "--port", "8002"]
