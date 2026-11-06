# stock-agent (과잉재고 에이전트) HTTP API 이미지
# 데이터(stock.duckdb, images/)는 도커 이미지에 넣지 않고 실행할 때 /data에 읽기 전용으로 연결한다.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    STOCK_DB_PATH=/data/stock.duckdb \
    STOCK_IMAGE_DIR=/data/images

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY stock_agent ./stock_agent

RUN useradd --create-home app
USER app

EXPOSE 8002
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://localhost:8002/health').status==200 else 1)"

CMD ["uvicorn", "stock_agent.api:app", "--host", "0.0.0.0", "--port", "8002"]
