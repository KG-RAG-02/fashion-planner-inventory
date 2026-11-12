"""stock-agent HTTP API

  POST /v1/inventory-products   legacy direct TrendAnalysisResult → 트렌드마다 상품 1개
  POST /v1/agents/stock-agent/runs  vogue-handoff/0.1 envelope + Bearer auth
                                products[i] ↔ trend_results[i] (stock_agent/multi.py)
                                오류 code는 팀 공통 코드, HTTP 번호는 stock_agent/errors.py
  GET  /health/live             API 생존
  GET  /health/ready, /health    DB schema·sample snapshot readiness
  GET  /docs                    설명서 (요청 예시 4개, 응답 200·400·500 형식)

실행 (로컬):   uvicorn stock_agent.api:app --port 8002
실행 (도커):   README 참고. DB는 read-only로 연다: STOCK_DB_PATH가 있으면 그 로컬 파일(local-file, R2 설정·HEAD 없음),
              없으면 Cloudflare R2 HEAD로 확인한 기존 검증 cache(시작 시 내려받지 않음).
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
from fastapi import Body, FastAPI, Request
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
from stock_agent.schemas import (REQUEST_EXAMPLES, RESPONSES, InventoryRequest, ErrorResponse,
                                HandoffRequest, HandoffResponse)
from stock_agent.storage import R2ObjectStore, R2Settings, StorageUnavailable, open_local_database
from stock_agent.readiness import inspect_database
from stock_agent.trend_request import from_trend_result
from stock_agent.multi import _record_input_provenance

IMAGE_DIR = os.environ.get("STOCK_IMAGE_DIR", DEFAULT_IMAGE_DIR)
HANDOFF_CONTRACT_VERSION = "vogue-handoff/0.1"
HANDOFF_PATH = "/v1/agents/stock-agent/runs"
INVENTORY_PATH = "/v1/inventory-products"
HANDOFF_TIMEOUT_SECONDS = 60.0

state: dict = {}
_RUN_LOCK = threading.RLock()


def database_mode() -> str:
    """``local-file`` when ``STOCK_DB_PATH`` is set (no R2 settings or HEAD), otherwise ``cloudflare-r2``."""
    return "local-file" if os.environ.get("STOCK_DB_PATH", "").strip() else "cloudflare-r2"


def prepare_database() -> tuple[str, str, R2ObjectStore | None]:
    """Locate the DB to open read-only; never download.

    local-file: the ``STOCK_DB_PATH`` file (and its SHA-256 sidecar when present); images then come
    from ``STOCK_IMAGE_DIR``. cloudflare-r2: HEAD plus one verified existing cache version.
    """
    local = os.environ.get("STOCK_DB_PATH", "").strip()
    if local:
        path, version = open_local_database(local)
        return str(path), version, None
    store = R2ObjectStore(R2Settings.from_env())
    # Startup may inspect HEAD and an existing cache, but never initiates a DB download.
    path, version = store.prepare_database(allow_download=False)
    return str(path), version, store


@asynccontextmanager
async def lifespan(app: FastAPI):
    state.clear()
    state["db_mode"] = database_mode()
    try:
        db_path, db_version, store = prepare_database()
        state["con"] = duckdb.connect(db_path, read_only=True)
        state["db_version"], state["db_error"], state["object_store"] = db_version, None, store
        state["readiness"] = inspect_database(state["con"])
    except Exception as e:          # Keep health available, but never serve requests without a ready DB.
        con = state.pop("con", None)
        if con is not None:
            con.close()
        state["con"], state["db_version"] = None, None
        state["object_store"] = None
        state["db_error"] = type(e).__name__
        state["readiness"] = {"db_ready": False, "reason": type(e).__name__}
    state["llm"] = load_llm()       # 아직 연결 전이면 None → 트렌드마다 SOURCE_UNAVAILABLE(reason=no_llm)
    yield
    con = state.pop("con", None)
    if con is not None:
        con.close()
    state.pop("db_version", None)
    state.pop("db_error", None)
    state.pop("object_store", None)
    state.pop("readiness", None)
    state.pop("db_mode", None)


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
    for path in (HANDOFF_PATH, INVENTORY_PATH):
        operation = schema.get("paths", {}).get(path, {}).get("post", {})
        operation["security"] = [{"BearerAuth": []}]
        operation.setdefault("responses", {})["401"] = {"description": "Missing or invalid Bearer token"}
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
    try:
        _, upstream, _ = from_trend_result(req)
    except (ValueError, TypeError):
        return run_request(req, None)  # Domain validation precedes DB/LLM access.
    if upstream in {"no_results", "error"} or not req.get("trends"):
        return run_request(req, None)
    if state.get("con") is None or not state.get("readiness", {}).get("db_ready"):
        return _record_input_provenance(error_response(req, "db_unavailable", "재고 DB를 사용할 수 없습니다."), req)
    # DuckDB allows separate cursors per thread, but serializing the shared
    # connection keeps concurrent API work safe across supported versions.
    with _RUN_LOCK:
        con = state["con"].cursor()
        try:
            store = state.get("object_store")
            image_source = store if store is not None and store.images_configured else IMAGE_DIR
            return run_request(req, con, image_dir=image_source, llm=state.get("llm"))
        except StorageUnavailable:
            return _record_input_provenance(error_response(req, "SOURCE_UNAVAILABLE", "이미지 자료원을 조회하지 못했습니다.",
                                                          reason="image_source_unavailable"), req)
        except duckdb.Error as e:
            state["readiness"] = {"db_ready": False, "reason": "database_query_failed"}
            return _record_input_provenance(error_response(req, "db_unavailable", "재고 DB 조회 실패"), req)
        except Exception as e:
            return _record_input_provenance(error_response(req, "internal", "내부 오류"), req)
        finally:
            con.close()


def _handoff_error(request_id: str | None, code: str, message: str, *, wrap: bool = True) -> JSONResponse:
    result = error_response({"request_id": request_id}, code, message)
    return _json(_envelope(request_id, result) if wrap and request_id else result, http_status(result))


@app.middleware("http")
async def _handoff_deadline(request: Request, call_next):
    if request.url.path not in {HANDOFF_PATH, INVENTORY_PATH}:
        return await call_next(request)
    token = os.environ.get("VOGUE_AGENT_SERVICE_BEARER_TOKEN", "") or os.environ.get("STOCK_AGENT_SERVICE_TOKEN", "")
    authorization = request.headers.get("authorization", "")
    scheme, _, supplied = authorization.partition(" ")
    if not token or scheme.lower() != "bearer" or not supplied.strip() or not hmac.compare_digest(
            supplied.strip().encode("utf-8"), token.encode("utf-8")):
        return _json(error_response({}, "UNAUTHORIZED", "Bearer 인증이 필요합니다."), 401)
    raw = await request.body()
    request.scope["_handoff_raw_body"] = raw
    # Reject ambiguous JSON before Pydantic can coerce or discard source fields.
    def unique_object(pairs):
        obj = {}
        for key, value in pairs:
            if key in obj:
                raise ValueError("duplicate key")
            obj[key] = value
        return obj
    try:
        decoded = json.loads(raw, object_pairs_hook=unique_object,
                             parse_constant=lambda _: (_ for _ in ()).throw(ValueError("nonfinite")))
        # Also catches overflow to infinity (e.g. 1e999).
        json.dumps(decoded, allow_nan=False)
        request.scope["_stock_source_body"] = decoded
    except (ValueError, UnicodeError):
        return _json(error_response({}, "invalid_input", "유효하고 중복 key가 없는 JSON이 필요합니다."), 400)
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
        return _json(_envelope(rid, result) if request.url.path == HANDOFF_PATH and rid else result, 504)


@app.get("/health/live")
def alive():
    return {"status": "ok", "agent_id": AGENT_ID, "api_alive": True}


@app.get("/health/ready", responses={503: {"description": "DB not ready"}})
@app.get("/health", responses={503: {"description": "Readiness alias"}})
def health():
    ready = state.get("readiness", {"db_ready": False, "reason": "not_initialized"})
    ok = bool(state.get("con") is not None and ready["db_ready"])
    store = state.get("object_store")
    images_remote = bool(store and store.images_configured)
    return _json({"status": "ok" if ok else "error", "api_alive": True, "agent_id": AGENT_ID,
                  "db": state.get("db_mode", "cloudflare-r2"), "db_version": state.get("db_version"),
                  **ready, "images_configured": images_remote or os.path.isdir(IMAGE_DIR),
                  "image_objects_verified": False, "llm_authenticated": None,
                  "llm_configured": state.get("llm") is not None and not isinstance(state.get("llm"), UnavailableLLM),
                  **_llm_status(state.get("llm"))}, 200 if ok else 503)


@app.post("/v1/inventory-products", responses=RESPONSES, summary="트렌드마다 과잉재고 1위 상품 1개 (사진·추천 이유 포함)")
def inventory_products(request: Request, body: InventoryRequest = Body(..., openapi_examples=REQUEST_EXAMPLES)):
    resp = _run_inventory(request.scope["_stock_source_body"])
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
def stock_agent_runs(request: Request, body: HandoffRequest = Body(...)):
    body = request.scope["_stock_source_body"]
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
