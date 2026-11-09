"""stock-agent HTTP API

  POST /v1/inventory-products   02 트렌드 결과(TrendAnalysisResult) 그대로만 → 트렌드마다 상품 1개
                                products[i] ↔ trend_results[i] (stock_agent/multi.py)
                                오류 code는 팀 공통 코드, HTTP 번호는 stock_agent/errors.py
  GET  /health                  DB 연결·필수 테이블·이미지 폴더 확인
  GET  /docs                    설명서 (요청 예시 4개, 응답 200·400·500 형식)

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
from stock_agent.errors import http_status
from stock_agent.multi import error_response, run_request
from stock_agent.images import DEFAULT_IMAGE_DIR
from stock_agent.llm import load_llm
from stock_agent.schemas import REQUEST_EXAMPLES, RESPONSES, Health, InventoryRequest

DB_PATH = os.environ.get("STOCK_DB_PATH", "data/stock.duckdb")
IMAGE_DIR = DEFAULT_IMAGE_DIR   # 환경변수 STOCK_IMAGE_DIR (기본 data/images)
REQUIRED_TABLES = {"articles", "transactions", "article_attrs", "stock_v1"}

state: dict = {}


@asynccontextmanager
async def lifespan(app: FastAPI):
    try:
        state["con"], state["db_error"] = duckdb.connect(DB_PATH, read_only=True), None
    except Exception as e:          # DB 파일이 없어도 서버는 뜨고, 요청에는 SOURCE_UNAVAILABLE(503)
        state["con"], state["db_error"] = None, f"{type(e).__name__}: {e}"
    state["llm"] = load_llm()       # 아직 연결 전이면 None → 트렌드마다 SOURCE_UNAVAILABLE(reason=no_llm)
    yield
    con = state.pop("con", None)
    if con is not None:
        con.close()


app = FastAPI(
    title="stock-agent",
    version=SCHEMA_VERSION,
    description="VOGUE 03 상품 재고 단계. 02 트렌드마다 짝 카테고리에서 트렌드 색과 어울리는 색 중 가장 많이 겹치는 색의 과잉재고를 "
                "순위로 매겨, 사진이 있는 최상위 1개와 추천 이유를 돌려준다. 재고·판매가는 가상 데이터(sample).",
    lifespan=lifespan,
)


def _llm_status(llm) -> dict:
    """health에 보일 LLM 상태. 키 값은 보이지 않는다. 키가 맞는지는 실제 요청에서 알 수 있다."""
    from stock_agent.llm import UnavailableLLM
    if llm is None:
        return {"llm_connected": False, "llm_model": None, "llm_error": "LLM_API_KEY 없음"}
    if isinstance(llm, UnavailableLLM):
        return {"llm_connected": False, "llm_model": None, "llm_error": llm.message}
    return {"llm_connected": True, "llm_model": getattr(llm, "model", None), "llm_error": None}


def _json(resp: dict, status_code: int = 200) -> JSONResponse:
    # numpy 숫자·날짜 등이 섞여도 JSON으로 나가도록
    # charset을 명시해야 Windows PowerShell 등에서 한글이 깨지지 않는다
    return JSONResponse(json.loads(json.dumps(resp, ensure_ascii=False, default=str)), status_code=status_code,
                        media_type="application/json; charset=utf-8")


def _openapi():
    """설명서에서 FastAPI 기본 422를 뺀다. 03은 형식 오류도 같은 envelope의 400으로 돌려준다."""
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


@app.exception_handler(RequestValidationError)
async def _bad_body(request: Request, exc: RequestValidationError):
    # 본문이 JSON 객체가 아니거나 형식이 크게 다를 때도 FastAPI 기본 422 대신 같은 envelope의 400으로
    first = exc.errors()[0] if exc.errors() else {}
    where = ".".join(str(x) for x in first.get("loc", [])[1:]) or "body"
    return _json(error_response({}, "invalid_input", f"요청 형식 오류: {where} ({first.get('msg', '')})"), 400)


@app.get("/health", response_model=Health, responses={503: {"description": "DB 연결 실패 또는 필수 테이블 없음"}})
def health():
    if state.get("con") is None:
        return _json({"status": "error", "agent_id": AGENT_ID, "db": DB_PATH,
                      "message": f"DB 연결 실패: {state.get('db_error')}"}, 503)
    try:
        con = state["con"].cursor()
        tables = {r[0] for r in con.execute("SHOW TABLES").fetchall()}
        missing = sorted(REQUIRED_TABLES - tables)
        ok = not missing
        # 이미지 폴더가 없어도 동작은 하지만(사진 없이 응답) 여기서 바로 알 수 있게 표시
        return _json({"status": "ok" if ok else "error", "agent_id": AGENT_ID, "db": DB_PATH,
                      "missing_tables": missing, "images": IMAGE_DIR,
                      "images_found": os.path.isdir(IMAGE_DIR),
                      **_llm_status(state.get("llm"))}, 200 if ok else 503)
    except Exception as e:
        return _json({"status": "error", "agent_id": AGENT_ID, "db": DB_PATH, "message": str(e)}, 503)


@app.post("/v1/inventory-products", responses=RESPONSES, summary="트렌드마다 과잉재고 1위 상품 1개 (사진·추천 이유 포함)")
def inventory_products(body: InventoryRequest = Body(..., openapi_examples=REQUEST_EXAMPLES)):
    request = body.model_dump(exclude_unset=True)   # 받은 값 그대로(빠진 값은 run이 400으로 알려 줌)
    if state.get("con") is None:
        resp = error_response(request, "db_unavailable", f"재고 DB에 연결하지 못했습니다 ({DB_PATH}).")
        return _json(resp, http_status(resp))
    con = state["con"].cursor()   # 요청마다 cursor (DuckDB 연결을 스레드 간에 그대로 공유하지 않음)
    try:
        resp = run_request(request, con, image_dir=IMAGE_DIR, llm=state.get("llm"))   # 트렌드마다 LLM agent
    except duckdb.Error as e:   # 테이블 없음·파일 손상 등 DB 문제
        resp = error_response(request, "db_unavailable", f"재고 DB 조회 실패: {type(e).__name__}")
    except Exception as e:  # 예상하지 못한 실패: 안전한 오류와 request_id만 반환
        resp = {**error_response(request, "internal", type(e).__name__), "summary": "내부 오류"}
    finally:
        con.close()
    return _json(resp, http_status(resp))
