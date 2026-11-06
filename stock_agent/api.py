"""stock-agent HTTP API

  POST /v1/inventory-products   요청 1건 → 공통 envelope + InventoryProductResult
                                trends 목록이 있으면 트렌드마다 1위 1개(총 n개, stock_agent/multi.py)
  GET  /health                  DB 연결·필수 테이블·이미지 폴더 확인
  GET  /docs                    설명서 (요청 예시 3개, 응답 200·400·500 형식)

실행 (로컬):   uvicorn stock_agent.api:app --port 8002
실행 (도커):   README 참고. DB 경로는 환경변수 STOCK_DB_PATH (기본 data/stock.duckdb)
포트·경로는 팀 공통 OpenAPI(contracts/openapi/agent-api.yaml)가 정해지면 맞춘다.
"""

from __future__ import annotations

import json
import os
from contextlib import asynccontextmanager

import duckdb
from fastapi import Body, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.openapi.utils import get_openapi
from fastapi.responses import JSONResponse

from stock_agent.agent import AGENT_ID, SCHEMA_VERSION
from stock_agent.multi import run_request
from stock_agent.images import DEFAULT_IMAGE_DIR
from stock_agent.schemas import REQUEST_EXAMPLES, RESPONSES, Health, InventoryRequest

DB_PATH = os.environ.get("STOCK_DB_PATH", "data/stock.duckdb")
IMAGE_DIR = DEFAULT_IMAGE_DIR   # 환경변수 STOCK_IMAGE_DIR (기본 data/images)
REQUIRED_TABLES = {"articles", "transactions", "article_attrs", "stock_v1"}
CLIENT_ERRORS = {"missing_fields", "invalid_input", "schema_version_mismatch"}

state: dict = {}


@asynccontextmanager
async def lifespan(app: FastAPI):
    state["con"] = duckdb.connect(DB_PATH, read_only=True)
    yield
    state.pop("con").close()


app = FastAPI(
    title="stock-agent",
    version=SCHEMA_VERSION,
    description="VOGUE 04 상품 재고 단계. 짝 카테고리에서 트렌드 색과 어울리는 과잉재고를 순위로 매겨 "
                "사진이 있는 최상위 1개와 추천 이유를 돌려준다. 재고·판매가는 가상 데이터(sample).",
    lifespan=lifespan,
)


def _json(resp: dict, status_code: int = 200) -> JSONResponse:
    # numpy 숫자·날짜 등이 섞여도 JSON으로 나가도록
    # charset을 명시해야 Windows PowerShell 등에서 한글이 깨지지 않는다
    return JSONResponse(json.loads(json.dumps(resp, ensure_ascii=False, default=str)), status_code=status_code,
                        media_type="application/json; charset=utf-8")


def _openapi():
    """설명서에서 FastAPI 기본 422를 뺀다. 04는 형식 오류도 같은 envelope의 400으로 돌려준다."""
    if app.openapi_schema:
        return app.openapi_schema
    schema = get_openapi(title=app.title, version=app.version, description=app.description, routes=app.routes)
    for ops in schema.get("paths", {}).values():
        for op in ops.values():
            op.get("responses", {}).pop("422", None)
    for name in ("HTTPValidationError", "ValidationError"):
        schema.get("components", {}).get("schemas", {}).pop(name, None)
    app.openapi_schema = schema
    return schema


app.openapi = _openapi


def _error_body(request_id, code: str, message: str) -> dict:
    return {"schema_version": SCHEMA_VERSION, "request_id": request_id, "agent_id": AGENT_ID,
            "status": "error", "summary": message, "products": [], "findings": [], "evidence": [],
            "limitations": [], "errors": [{"code": code, "message": message}]}


@app.exception_handler(RequestValidationError)
async def _bad_body(request: Request, exc: RequestValidationError):
    # 본문이 JSON 객체가 아니거나 형식이 크게 다를 때도 FastAPI 기본 422 대신 같은 envelope의 400으로
    first = exc.errors()[0] if exc.errors() else {}
    where = ".".join(str(x) for x in first.get("loc", [])[1:]) or "body"
    return _json(_error_body(None, "invalid_input", f"요청 형식 오류: {where} ({first.get('msg', '')})"), 400)


@app.get("/health", response_model=Health, responses={503: {"description": "DB 연결 실패 또는 필수 테이블 없음"}})
def health():
    try:
        con = state["con"].cursor()
        tables = {r[0] for r in con.execute("SHOW TABLES").fetchall()}
        missing = sorted(REQUIRED_TABLES - tables)
        ok = not missing
        # 이미지 폴더가 없어도 동작은 하지만(사진 없이 응답) 여기서 바로 알 수 있게 표시
        return _json({"status": "ok" if ok else "error", "agent_id": AGENT_ID, "db": DB_PATH,
                      "missing_tables": missing, "images": IMAGE_DIR,
                      "images_found": os.path.isdir(IMAGE_DIR)}, 200 if ok else 503)
    except Exception as e:
        return _json({"status": "error", "agent_id": AGENT_ID, "db": DB_PATH, "message": str(e)}, 503)


@app.post("/v1/inventory-products", responses=RESPONSES, summary="과잉재고 1위 상품 (사진·추천 이유 포함). trends가 있으면 트렌드마다 1개")
def inventory_products(body: InventoryRequest = Body(..., openapi_examples=REQUEST_EXAMPLES)):
    request = body.model_dump(exclude_unset=True)   # 받은 값 그대로(빠진 값은 run이 400으로 알려 줌)
    con = state["con"].cursor()   # 요청마다 cursor (DuckDB 연결을 스레드 간에 그대로 공유하지 않음)
    try:
        resp = run_request(request, con, image_dir=IMAGE_DIR)   # trends 있으면 트렌드별
    except Exception as e:  # 예상하지 못한 실패: 안전한 오류와 request_id만 반환
        body = _error_body(request.get("request_id"), "INTERNAL_ERROR", type(e).__name__)
        return _json({**body, "summary": "내부 오류"}, 500)
    finally:
        con.close()
    code = 200
    if resp["status"] == "error" and resp["errors"] and resp["errors"][0]["code"] in CLIENT_ERRORS:
        code = 400
    return _json(resp, code)
