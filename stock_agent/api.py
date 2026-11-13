"""stock-agent HTTP API

  POST /v1/inventory-products   legacy direct TrendAnalysisResult → 트렌드마다 상품 1개
  POST /v1/agents/stock-agent/runs  vogue-handoff/0.1 envelope + Bearer auth
                                products[i] ↔ trend_results[i] (stock_agent/multi.py)
                                오류 code는 팀 공통 코드, HTTP 번호는 stock_agent/errors.py
  GET  /health                  DB 연결·필수 테이블·이미지 폴더 확인
  GET  /docs                    설명서 (요청 예시 4개, 응답 200·400·500 형식)

실행 (로컬):   uvicorn stock_agent.api:app --port 8002
실행 (도커):   README 참고. DB는 Cloudflare R2에서 내려받아 로컬 read-only 캐시로 연다.
레거시 경로는 기존 클라이언트 호환을 위해 유지한다.
"""

from __future__ import annotations

import json
import os
import asyncio
import hmac
import threading
from contextlib import asynccontextmanager
from typing import Any

import duckdb
from fastapi import Body, FastAPI, Header, Request
from fastapi.exceptions import RequestValidationError
from fastapi.openapi.utils import get_openapi
from fastapi.responses import JSONResponse

from stock_agent.llm import read_dotenv

# Load .env before any module snapshots path or storage defaults.
read_dotenv()

from stock_agent.agent import AGENT_ID, SCHEMA_VERSION
from stock_agent.errors import http_status
from stock_agent.multi import error_response, run_request
from stock_agent.images import DEFAULT_IMAGE_DIR
from stock_agent.llm import load_llm
from stock_agent.llm import UnavailableLLM
from stock_agent.schemas import (REQUEST_EXAMPLES, RESPONSES, Health, InventoryRequest, ErrorResponse,
                                HandoffRequest, HandoffResponse)
from stock_agent.storage import R2ObjectStore, R2Settings

IMAGE_DIR = os.environ.get("STOCK_IMAGE_DIR", DEFAULT_IMAGE_DIR)
HANDOFF_CONTRACT_VERSION = "vogue-handoff/0.1"
HANDOFF_PATH = "/v1/agents/stock-agent/runs"
HANDOFF_TIMEOUT_SECONDS = 60.0
REQUIRED_TABLES = {"articles", "transactions", "article_attrs", "stock_v1"}

state: dict = {}
_RUN_LOCK = threading.RLock()


def prepare_database() -> tuple[str, str, R2ObjectStore]:
    """Fetch one R2 object version into the local runtime cache."""
    store = R2ObjectStore(R2Settings.from_env())
    path, version = store.prepare_database()
    return str(path), version, store


@asynccontextmanager
async def lifespan(app: FastAPI):
    try:
        db_path, db_version, store = prepare_database()
        state["con"] = duckdb.connect(db_path, read_only=True)
        state["db_version"], state["db_error"], state["object_store"] = db_version, None, store
    except Exception as e:          # Keep health available, but never serve requests without a ready DB.
        state["con"], state["db_version"] = None, None
        state["object_store"] = None
        state["db_error"] = str(e) if type(e).__name__ in {
            "StorageConfigurationError", "StorageUnavailable"} else type(e).__name__
    state["llm"] = load_llm()       # 아직 연결 전이면 None → 트렌드마다 SOURCE_UNAVAILABLE(reason=no_llm)
    yield
    con = state.pop("con", None)
    if con is not None:
        con.close()
    state.pop("db_version", None)
    state.pop("db_error", None)
    state.pop("object_store", None)


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
        return {"llm_connected": False, "llm_model": None, "llm_error": "LLM 설정을 사용할 수 없습니다"}
    return {"llm_connected": True, "llm_model": getattr(llm, "model", None), "llm_error": None}


def _json(resp: dict, status_code: int = 200) -> JSONResponse:
    # numpy 숫자·날짜 등이 섞여도 JSON으로 나가도록
    # charset을 명시해야 Windows PowerShell 등에서 한글이 깨지지 않는다
    return JSONResponse(json.loads(json.dumps(resp, ensure_ascii=False, default=str)), status_code=status_code,
                        media_type="application/json; charset=utf-8")


def _openapi():
    """문서에서 FastAPI 422를 제외하고 v0.1 Bearer 보안을 명시한다."""
    if app.openapi_schema:
        return app.openapi_schema
    schema = get_openapi(title=app.title, version=app.version, description=app.description, routes=app.routes)
    handoff = schema.get("paths", {}).get(HANDOFF_PATH, {}).get("post", {})
    if handoff:
        handoff["security"] = [{"BearerAuth": []}]
        schema.setdefault("components", {}).setdefault("securitySchemes", {})["BearerAuth"] = {
            "type": "http", "scheme": "bearer", "bearerFormat": "opaque service token"}
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
    # Both producer routes use the established 400 error payload for malformed JSON.
    first = exc.errors()[0] if exc.errors() else {}
    where = ".".join(str(x) for x in first.get("loc", [])[1:]) or "body"
    request_id = None
    if request.url.path == HANDOFF_PATH:
        try:
            raw = request.scope.get("_handoff_raw_body")
            if raw is None:
                raw = await request.body()
            body = json.loads(raw)
            candidate = body.get("request_id") if isinstance(body, dict) else None
            request_id = candidate if isinstance(candidate, str) and candidate.strip() else None
        except Exception:
            pass
    result = error_response({"request_id": request_id}, "invalid_input", f"요청 형식 오류: {where} ({first.get('msg', '')})")
    if request.url.path == HANDOFF_PATH and request_id:
        return _json(_envelope(request_id, result), 400)
    return _json(result, 400)


def _envelope(request_id: str, result: dict) -> dict:
    return {"contract_version": HANDOFF_CONTRACT_VERSION, "request_id": request_id,
            "producer": {"agent_id": AGENT_ID, "schema_version": result["schema_version"]},
            "status": result["status"], "result": result}


def _run_inventory(request: Any) -> dict:
    """Run the unchanged TrendAnalysisResult through the producer implementation."""
    req = request if isinstance(request, dict) else {}
    if state.get("con") is None:
        return error_response(req, "db_unavailable", "재고 DB를 사용할 수 없습니다.")
    # DuckDB allows separate cursors per thread, but serializing the shared
    # connection keeps concurrent API work safe across supported versions.
    with _RUN_LOCK:
        con = state["con"].cursor()
        try:
            store = state.get("object_store")
            image_source = store if store is not None and store.images_configured else IMAGE_DIR
            return run_request(req, con, image_dir=image_source, llm=state.get("llm"))
        except duckdb.Error as e:
            return error_response(req, "db_unavailable", f"재고 DB 조회 실패: {type(e).__name__}")
        except Exception as e:
            return {**error_response(req, "internal", type(e).__name__), "summary": "내부 오류"}
        finally:
            con.close()


def _handoff_error(request_id: str | None, code: str, message: str, *, wrap: bool = True) -> JSONResponse:
    result = error_response({"request_id": request_id}, code, message)
    return _json(_envelope(request_id, result) if wrap and request_id else result, http_status(result))


@app.middleware("http")
async def _handoff_deadline(request: Request, call_next):
    if request.url.path != HANDOFF_PATH:
        return await call_next(request)
    raw = await request.body()
    request.scope["_handoff_raw_body"] = raw
    sent = False
    async def receive():
        nonlocal sent
        if sent:
            return {"type": "http.request", "body": b"", "more_body": False}
        sent = True
        return {"type": "http.request", "body": raw, "more_body": False}
    request = Request(request.scope, receive)
    loop = asyncio.get_running_loop()
    deadline = loop.time() + HANDOFF_TIMEOUT_SECONDS
    try:
        async with asyncio.timeout_at(deadline):
            return await call_next(request)
    except TimeoutError:
        rid = None
        try:
            body = json.loads(request.scope.get("_handoff_raw_body", b"{}"))
            candidate = body.get("request_id") if isinstance(body, dict) else None
            rid = candidate if isinstance(candidate, str) and candidate.strip() else None
        except Exception:
            pass
        result = error_response({"request_id": rid}, "llm_failed", "HTTP 요청 제한 시간(60초)을 초과했습니다.", reason="timeout")
        return _json(_envelope(rid, result) if rid else result, 504)


@app.get("/health", response_model=Health, responses={503: {"description": "DB 연결 실패 또는 필수 테이블 없음"}})
def health():
    if state.get("con") is None:
        return _json({"status": "error", "agent_id": AGENT_ID, "db": "cloudflare-r2",
                      "message": f"DB 준비 실패: {state.get('db_error', '알 수 없는 오류')}"}, 503)
    try:
        with _RUN_LOCK:
            con = state["con"].cursor()
            try:
                tables = {r[0] for r in con.execute("SHOW TABLES").fetchall()}
            finally:
                con.close()
        missing = sorted(REQUIRED_TABLES - tables)
        ok = not missing
        # R2에서는 prefix 설정만 보고, 개별 object 존재 여부는 이미지 선택 때 지연 확인한다.
        store = state.get("object_store")
        images_remote = bool(store and store.images_configured)
        return _json({"status": "ok" if ok else "error", "agent_id": AGENT_ID, "db": "cloudflare-r2",
                      "db_version": state.get("db_version"),
                      "missing_tables": missing, "images": "cloudflare-r2" if images_remote else "local",
                      "images_found": store.images_available if images_remote else os.path.isdir(IMAGE_DIR),
                      **_llm_status(state.get("llm"))}, 200 if ok else 503)
    except Exception as e:
        return _json({"status": "error", "agent_id": AGENT_ID, "db": "cloudflare-r2",
                      "message": f"DB 상태 확인 실패: {type(e).__name__}"}, 503)


@app.post("/v1/inventory-products", responses=RESPONSES, summary="트렌드마다 과잉재고 1위 상품 1개 (사진·추천 이유 포함)")
def inventory_products(body: InventoryRequest = Body(..., openapi_examples=REQUEST_EXAMPLES)):
    request = body.model_dump(exclude_unset=True)   # 받은 값 그대로(빠진 값은 run이 400으로 알려 줌)
    resp = _run_inventory(request)
    return _json(resp, http_status(resp))


HANDOFF_RESPONSES = {
    200: {"model": HandoffResponse, "description": "v0.1 envelope with Stock producer result (ok, partial, no_results)"},
    400: {"description": "INVALID_REQUEST producer result; wrapped when a non-empty external request_id is available"},
    401: {"model": ErrorResponse, "description": "UNAUTHORIZED: missing or invalid Bearer token"},
    500: {"model": HandoffResponse, "description": "v0.1 envelope with INTERNAL_ERROR producer result"},
    503: {"model": HandoffResponse, "description": "v0.1 envelope with SOURCE_UNAVAILABLE producer result"},
    504: {"model": HandoffResponse, "description": "v0.1 envelope with TIMEOUT producer result"},
}


@app.post(HANDOFF_PATH, responses=HANDOFF_RESPONSES, summary="vogue-handoff/0.1 Stock producer 실행",
          description=("인증된 Project_08 agent route. input은 TrendAnalysisResult 원본 객체이며 "
                       "producer result는 stock-agent payload schema 1.0 그대로 반환됩니다. "
                       "운영에서는 HTTPS와 VOGUE_AGENT_SERVICE_BEARER_TOKEN Bearer 인증이 필요합니다."))
def stock_agent_runs(request: Request, body: HandoffRequest = Body(...),
                     authorization: str | None = Header(default=None)):
    # Validate the bearer header before processing any producer payload.
    token = os.environ.get("VOGUE_AGENT_SERVICE_BEARER_TOKEN", "") or os.environ.get("STOCK_AGENT_SERVICE_TOKEN", "")
    supplied = authorization[7:].strip() if isinstance(authorization, str) and authorization.startswith("Bearer ") else ""
    if not token or not supplied or not hmac.compare_digest(supplied, token):
        result = error_response({}, "UNAUTHORIZED", "Bearer 인증이 필요합니다.")
        return _json(result, 401)

    body = body.model_dump()
    rid = body.get("request_id")
    rid = rid if isinstance(rid, str) and rid.strip() else None
    if body.get("contract_version") != HANDOFF_CONTRACT_VERSION:
        return _handoff_error(rid, "schema_version_mismatch", "지원하지 않는 contract_version입니다.", wrap=bool(rid))
    if rid is None:
        return _handoff_error(None, "missing_fields", "필수 입력이 없습니다: request_id.")
    payload = body.get("input")
    if not isinstance(payload, dict):
        return _handoff_error(rid, "invalid_input", "input은 TrendAnalysisResult 객체여야 합니다.", wrap=bool(rid))
    input_id = payload.get("request_id")
    if not isinstance(input_id, str) or not input_id.strip() or input_id != rid:
        # If the external ID is usable, preserve it in the envelope and the
        # producer error result, even when the inner ID is absent or differs.
        return _handoff_error(rid, "invalid_input", "request_id와 input.request_id가 모두 비어 있지 않고 같아야 합니다.", wrap=bool(rid))

    result = _run_inventory(payload)
    return _json(_envelope(rid, result), http_status(result))


@app.exception_handler(Exception)
async def _unexpected_error(request: Request, exc: Exception):
    if request.url.path != HANDOFF_PATH:
        return _json(error_response({}, "internal", type(exc).__name__), 500)
    try:
        body = await request.json()
    except Exception:
        body = {}
    rid = body.get("request_id") if isinstance(body, dict) else None
    rid = rid if isinstance(rid, str) and rid.strip() else None
    result = error_response({"request_id": rid}, "internal", type(exc).__name__)
    return _json(_envelope(rid, result) if rid else result, 500)
