"""HTTP API 테스트 (fastapi·httpx·duckdb가 있을 때만 실행)"""

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")
duckdb = pytest.importorskip("duckdb")

from fastapi.testclient import TestClient  # noqa: E402

from fashion_planner_inventory.trend_request import build_trend_result  # noqa: E402

BALLET = {"style": {"id": "style_012", "name": "발레코어"},
          "items": [{"category_major": "상의", "category_minor": "블라우스", "colors": ["핑크", "화이트"]}]}
REQ = build_trend_result("req_api", [BALLET], include=["상의"], gender="여")      # trend 결과 그대로


@pytest.fixture
def make_client(con, tmp_path, monkeypatch):
    monkeypatch.setenv("FASHION_PLANNER_SERVICE_TOKEN", "test-stock-token")
    monkeypatch.delenv("INVENTORY_DB_PATH", raising=False)   # R2 mode (INVENTORY_DB_PATH would select local-file)
    # conftest의 작은 DB를 파일로 저장해 API가 read_only로 열게 한다
    db = tmp_path / "t.duckdb"
    con.execute(f"ATTACH '{db.as_posix()}' AS f")
    for t in ("article_attrs", "virtual_stock", "transactions"):
        con.execute(f"CREATE TABLE f.{t} AS SELECT * FROM {t}")
    con.execute("CREATE TABLE f.articles AS SELECT article_id FROM article_attrs")
    con.execute("ALTER TABLE f.virtual_stock ADD COLUMN as_of DATE DEFAULT DATE '2020-09-22'")
    con.execute("ALTER TABLE f.virtual_stock ADD COLUMN version VARCHAR DEFAULT 'virtual_stock_from_hm_sales'")
    con.execute("ALTER TABLE f.virtual_stock ADD COLUMN data_nature VARCHAR DEFAULT 'sample'")
    con.execute("DETACH f")
    import fashion_planner_inventory.api as api
    monkeypatch.setattr(api, "prepare_database", lambda: (str(db), "test-version", None))
    monkeypatch.setattr(api, "warm_up_clip", lambda: {"clip_index_loaded": False, "clip_n_indexed": 0})

    def make():
        return TestClient(api.app, headers={"Authorization": "Bearer test-stock-token"})
    return make


@pytest.fixture
def client(make_client):
    with make_client() as c:
        yield c


def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200 and r.json()["status"] == "ok"
    assert r.json()["db"] == "cloudflare-r2" and r.json()["db_version"] == "test-version"
    assert r.json()["clip_index_loaded"] is False and r.json()["clip_n_indexed"] == 0
    assert not [k for k in r.json() if k.startswith("llm")]
    assert "tmp" not in r.text and "stock.duckdb" not in r.text


def test_health_reports_clip_index(make_client, monkeypatch):
    import fashion_planner_inventory.api as api
    monkeypatch.setattr(api, "warm_up_clip", lambda: {"clip_index_loaded": True, "clip_n_indexed": 6})
    with make_client() as c:
        body = c.get("/health").json()
    assert body["clip_index_loaded"] is True and body["clip_n_indexed"] == 6


def test_clip_index_path_order(monkeypatch, tmp_path):
    """INVENTORY_CLIP_INDEX_PATH → R2 캐시(R2_CLIP_INDEX_KEY, 서버는 받지 않음) → 기본 data/clip_index.npz."""
    import fashion_planner_inventory.api as api
    import fashion_planner_inventory.clip_matcher as clip
    from fashion_planner_inventory.storage import StorageUnavailable

    class Store:
        def __init__(self, key, cached):
            self.settings = type("S", (), {"clip_index_key": key})()
            self.cached, self.calls = cached, []

        def prepare_clip_index(self, allow_download):
            self.calls.append(allow_download)
            if self.cached is None:
                raise StorageUnavailable("no cache")
            return self.cached, "v1"

    monkeypatch.setenv("INVENTORY_CLIP_INDEX_PATH", str(tmp_path / "x.npz"))
    assert api.clip_index_path() == str(tmp_path / "x.npz")
    monkeypatch.delenv("INVENTORY_CLIP_INDEX_PATH")
    monkeypatch.setitem(api.state, "object_store", Store("clip/i.npz", tmp_path / "cached.npz"))
    assert api.clip_index_path() == tmp_path / "cached.npz" and api.state["object_store"].calls == [False]
    monkeypatch.setitem(api.state, "object_store", Store("clip/i.npz", None))
    assert api.clip_index_path() is None                                  # 캐시 없음 → 인덱스 없음
    monkeypatch.setitem(api.state, "object_store", Store("", None))
    assert api.clip_index_path() == clip.DEFAULT_INDEX_PATH


@pytest.mark.parametrize("prefix, connected", [("musinsa-snap", True), ("", False)])
def test_warm_up_clip_connects_snap_store(monkeypatch, prefix, connected):
    """R2_SNAP_IMAGE_PREFIX가 있으면 착샷 이미지를 R2 저장소에서 읽도록 연결한다."""
    import fashion_planner_inventory.api as api
    import fashion_planner_inventory.clip_matcher as clip
    store = type("Store", (), {"settings": type("S", (), {"snap_image_prefix": prefix, "clip_index_key": ""})()})()
    monkeypatch.setitem(api.state, "object_store", store)
    monkeypatch.setattr(clip, "warm_up", lambda path=None: {"clip_index_loaded": False, "clip_n_indexed": 0})
    api.warm_up_clip()
    assert (clip._snap_source is store) is connected


def test_clip_warm_up_failure_keeps_server_up(monkeypatch):
    import fashion_planner_inventory.clip_matcher as clip
    import fashion_planner_inventory.api as api
    monkeypatch.setattr(clip, "warm_up", lambda: (_ for _ in ()).throw(RuntimeError("no open_clip")))
    assert api.warm_up_clip() == {"clip_index_loaded": False, "clip_n_indexed": 0}


def _run(client, body):
    """canonical 경로로 trend 결과를 보내고 (HTTP 상태, producer 결과)를 돌려준다."""
    r = client.post("/v1/agents/inventory/runs", json={
        "contract_version": "inventory-handoff", "request_id": body["request_id"], "input": body})
    return r, (r.json()["result"] if "result" in r.json() else r.json())


def test_post_ok(client):
    r, body = _run(client, REQ)
    assert r.status_code == 200
    assert body["status"] == "ok" and body["products"][0]["sku_ids"] == ["a1"]
    assert body["trend_results"][0]["style"]["id"] == "style_012" and body["trend_results"][0]["product_index"] == 0
    assert "charset=utf-8" in r.headers["content-type"]   # 한글 깨짐 방지


def test_post_invalid_is_400(client):
    r, body = _run(client, {"request_id": "x"})
    assert r.status_code == 400 and body["errors"][0]["code"] == "INVALID_REQUEST"
    r, body = _run(client, {**REQ, "trends": []})
    assert r.status_code == 400 and body["errors"][0]["reason"] == "missing_fields"


def test_season_origin_and_extra_fields_pass_through(client):
    req = build_trend_result("req_fw", [BALLET], year=2026, season="FW", origin="default_next", include=["상의"])
    r, body = _run(client, {**req, "extra_field": 1})
    assert r.status_code == 200 and body["season_basis"]["source"] == "inferred"
    # trend 결과에 inventory가 안 쓰는 필드가 있어도 그대로 받음
    trend = {**BALLET, "style": {**BALLET["style"], "focus_core": ["color"]},
             "items": [{**BALLET["items"][0], "material": ["면"], "fit": "루즈"}]}
    assert _run(client, {**REQ, "trends": [trend]})[0].status_code == 200


def test_docs_show_envelope_examples_and_400(client):
    spec = client.get("/openapi.json").json()
    assert set(spec["paths"]) == {"/health/live", "/health/ready", "/health", "/v1/agents/inventory/runs"}
    op = spec["paths"]["/v1/agents/inventory/runs"]["post"]
    assert set(op["responses"]) == {"200", "400", "401", "500", "503", "504"}     # 422 대신 400
    examples = op["requestBody"]["content"]["application/json"]["examples"]
    assert {"one_trend", "multi_trends", "no_user_category", "upstream_no_results", "missing_season"} == set(examples)
    for ex in examples.values():
        value = ex["value"]
        assert value["contract_version"] == "inventory-handoff" and value["request_id"] == value["input"]["request_id"]
    assert "InventoryResponse" in spec["components"]["schemas"]


def test_post_trends_returns_one_per_trend(client):
    other = {"style": {"id": "style_007", "name": "색 없음"},
             "items": [{"category_major": "아우터", "category_minor": "재킷", "colors": []}]}
    r, body = _run(client, {**REQ, "trends": [BALLET, other]})
    assert r.status_code == 200
    assert len(body["products"]) == len(body["trend_results"]) == 2
    assert [t["style"]["id"] for t in body["trend_results"]] == ["style_012", "style_007"]


def test_post_trends_all_invalid_is_400(client):
    bad = {"style": {"id": "style_bad"}, "items": [{"category_major": "상의", "colors": ["무지개"]}]}
    r, body = _run(client, {**REQ, "trends": [bad]})
    assert r.status_code == 400 and body["status"] == "error"
    assert body["trend_results"][0]["status"] == "error"


def test_internal_format_is_400(client):
    """trend 형식이 아니면(applied_filters 없음) 받지 않는다."""
    flat = {"request_id": "req_flat", "season": "2027-SS", "target_category": "상의", "trends": [BALLET]}
    r, body = _run(client, flat)
    assert r.status_code == 400 and "applied_filters" in body["errors"][0]["message"]


def test_trend_result_request(client):
    """trend 결과 그대로 보내도 된다. 근거 ID가 evidence를 가리킨다."""
    from fashion_planner_inventory.schemas import TREND_RESULT_EXAMPLE
    r, body = _run(client, TREND_RESULT_EXAMPLE)
    assert r.status_code == 200 and body["request_id"] == TREND_RESULT_EXAMPLE["request_id"]
    tr = body["trend_results"][0]
    assert tr["search"]["segments"] == ["Ladieswear", "Divided"]
    assert set(tr["evidence_ids"]) <= {e["evidence_id"] for e in body["evidence"]}


def test_db_missing_is_503(tmp_path, monkeypatch):
    monkeypatch.setenv("FASHION_PLANNER_SERVICE_TOKEN", "test-stock-token")
    import fashion_planner_inventory.api as api
    monkeypatch.setattr(api, "prepare_database", lambda: (str(tmp_path / "missing.duckdb"), "test-version", None))
    with TestClient(api.app, headers={"Authorization": "Bearer test-stock-token"}) as c:
        assert c.get("/health").status_code == 503
        r, body = _run(c, REQ)
    assert r.status_code == 503
    assert body["errors"][0]["code"] == "SOURCE_UNAVAILABLE" and body["errors"][0]["reason"] == "db_unavailable"


def test_missing_r2_location_is_unhealthy_without_exposing_credentials(monkeypatch):
    import fashion_planner_inventory.api as api
    monkeypatch.setattr(api, "warm_up_clip", lambda: {"clip_index_loaded": False, "clip_n_indexed": 0})
    for name in ("R2_ACCOUNT_ID", "R2_BUCKET", "R2_DB_KEY", "INVENTORY_DB_PATH"):
        monkeypatch.delenv(name, raising=False)
    with TestClient(api.app, headers={"Authorization": "Bearer test-stock-token"}) as c:
        response = c.get("/health")
    assert response.status_code == 503
    body = response.json()
    assert body["db"] == "cloudflare-r2"
    assert body["reason"] == "StorageConfigurationError"
    assert "Secret Access Key" not in response.text


@pytest.fixture
def handoff_client(make_client, monkeypatch):
    monkeypatch.setenv("FASHION_PLANNER_SERVICE_TOKEN", "test-stock-token")
    with make_client() as c:
        yield c


def _handoff(body=REQ, request_id=None, version="inventory-handoff"):
    return {"contract_version": version, "request_id": request_id or body["request_id"], "input": body}


def _post_handoff(client, body=None, token="test-stock-token"):
    headers = {"Authorization": f"Bearer {token}"} if token is not None else {"Authorization": ""}
    return client.post("/v1/agents/inventory/runs", json=body or _handoff(), headers=headers)


def test_handoff_accepts_trend_11_and_records_adapter_provenance(handoff_client):
    trend = {**REQ, "schema_version": "1.0.0"}
    body = _handoff({**trend, "status": "no_results", "trends": []})
    response = _post_handoff(handoff_client, body=body)
    assert response.status_code == 200
    envelope = response.json()
    assert envelope["status"] == envelope["result"]["status"] == "no_results"
    assert envelope["result"]["input_provenance"] == {
        "source_agent_id": "trend",
        "source_schema_version": "1.0.0",
        "internal_schema_version": None,
        "adapter_version": "inventory-trend-input-adapter",
        "transformed": True,
        "source_payload_sha256": envelope["result"]["input_provenance"]["source_payload_sha256"],
    }
    assert len(envelope["result"]["input_provenance"]["source_payload_sha256"]) == 64


def test_handoff_envelope_preserves_result_and_metadata(handoff_client):
    r = _post_handoff(handoff_client)
    assert r.status_code == 200
    env = r.json()
    assert env["contract_version"] == "inventory-handoff"
    assert env["request_id"] == env["result"]["request_id"] == REQ["request_id"]
    assert env["producer"] == {"agent_id": "inventory", "schema_version": "1.0.0"}
    assert env["status"] == env["result"]["status"] == "ok"
    assert list(env["result"]) == ["schema_version", "request_id", "agent_id", "status", "summary", "products",
                                    "trend_results", "season_basis", "evidence", "errors", "input_provenance"]
    assert env["result"]["products"][0]["sku_ids"] == ["a1"]


@pytest.mark.parametrize("payload,version", [(REQ, "trend-handoff"), (REQ, ""), ([], "inventory-handoff")])
def test_handoff_bad_version_or_input_is_400_envelope(handoff_client, payload, version):
    env_req = {"contract_version": version, "request_id": REQ["request_id"], "input": payload}
    r = _post_handoff(handoff_client, env_req)
    assert r.status_code == 400 and r.status_code != 422
    result = r.json()["result"] if "result" in r.json() else r.json()
    # Missing/wrong field types hit FastAPI's common 400 handler. It preserves
    # a valid outer request ID but uses the standard producer error body.
    assert result["request_id"] == REQ["request_id"]
    assert result["status"] == "error"
    assert result["errors"][0]["reason"] == "invalid_input"


@pytest.mark.parametrize("outer,inner", [("different", REQ["request_id"]), (REQ["request_id"], ""), (REQ["request_id"], None)])
def test_handoff_request_id_mismatch_uses_outer_id(handoff_client, outer, inner):
    payload = {**REQ, "request_id": inner}
    r = _post_handoff(handoff_client, _handoff(payload, request_id=outer))
    assert r.status_code == 400
    env = r.json()
    assert env["request_id"] == env["result"]["request_id"] == outer
    assert env["result"]["errors"][0]["reason"] == "invalid_input"


def test_handoff_missing_external_id_is_400_error_payload(handoff_client):
    r = handoff_client.post("/v1/agents/inventory/runs", json={
        "contract_version": "inventory-handoff", "input": REQ},
        headers={"Authorization": "Bearer test-stock-token"})
    assert r.status_code == 400 and "contract_version" not in r.json()
    assert r.json()["request_id"] is None
    assert r.json()["errors"][0]["reason"] == "invalid_input"


@pytest.mark.parametrize("token", [None, "wrong-token"])
def test_handoff_requires_bearer_token(handoff_client, token):
    r = _post_handoff(handoff_client, token=token)
    assert r.status_code == 401
    assert r.json()["errors"][0]["code"] == "UNAUTHORIZED"
    assert "test-stock-token" not in r.text


def test_handoff_no_results_and_domain_error_status_are_wrapped(handoff_client):
    no_results = {**REQ, "status": "no_results", "trends": []}
    r = _post_handoff(handoff_client, _handoff(no_results))
    assert r.status_code == 200 and r.json()["status"] == r.json()["result"]["status"] == "no_results"
    upstream_error = {**REQ, "status": "error"}
    r = _post_handoff(handoff_client, _handoff(upstream_error))
    assert r.status_code == 400 and r.json()["status"] == r.json()["result"]["status"] == "error"
    assert r.json()["result"]["errors"][0]["reason"] == "upstream_error"


def test_handoff_server_status_mappings_are_enveloped(handoff_client, monkeypatch):
    import fashion_planner_inventory.api as api
    for code, expected in (("db_unavailable", 503), ("timeout", 504), ("internal", 500)):
        monkeypatch.setattr(api, "_run_inventory", lambda payload, code=code: api.error_response(
            payload, code, "controlled"))
        r = _post_handoff(handoff_client)
        assert r.status_code == expected
        assert r.json()["contract_version"] == "inventory-handoff"
        assert r.json()["status"] == r.json()["result"]["status"] == "error"
        assert r.json()["request_id"] == r.json()["result"]["request_id"]


def test_handoff_partial_status_is_copied(handoff_client, monkeypatch):
    import fashion_planner_inventory.api as api
    partial = {"schema_version": "1.0.0", "request_id": REQ["request_id"], "agent_id": "inventory",
               "status": "partial", "summary": "partial", "products": [], "trend_results": [],
               "season_basis": None, "evidence": [], "errors": []}
    monkeypatch.setattr(api, "_run_inventory", lambda payload: partial)
    r = _post_handoff(handoff_client)
    assert r.status_code == 200 and r.json()["status"] == r.json()["result"]["status"] == "partial"


def test_handoff_invalid_json_is_400_not_422(handoff_client):
    r = handoff_client.post("/v1/agents/inventory/runs", data="{bad json", headers={
        "Authorization": "Bearer test-stock-token", "Content-Type": "application/json"})
    assert r.status_code == 400 and r.status_code != 422
    assert r.json()["status"] == "error"


def test_handoff_missing_required_fields_is_400_not_422(handoff_client):
    r = handoff_client.post("/v1/agents/inventory/runs", json={
        "contract_version": "inventory-handoff", "request_id": REQ["request_id"]},
        headers={"Authorization": "Bearer test-stock-token"})
    assert r.status_code == 400 and r.status_code != 422
    assert r.json()["result"]["errors"][0]["reason"] == "invalid_input"


def test_handoff_openapi_declares_bearer_auth(handoff_client):
    spec = handoff_client.get("/openapi.json").json()
    op = spec["paths"]["/v1/agents/inventory/runs"]["post"]
    assert "422" not in op["responses"]
    assert {"200", "400", "500", "503", "504"} <= set(op["responses"])
    assert op["security"] == [{"BearerAuth": []}]
    assert "/v1/inventory-products" not in spec["paths"]
    assert handoff_client.post("/v1/inventory-products", json=REQ).status_code == 404


def test_handoff_deadline_returns_504_envelope(handoff_client, monkeypatch):
    import time
    import fashion_planner_inventory.api as api
    monkeypatch.setattr(api, "HANDOFF_TIMEOUT_SECONDS", 0.01)
    monkeypatch.setattr(api, "_run_inventory", lambda payload: (time.sleep(0.2), api.error_response(payload, "internal", "late"))[1])
    r = _post_handoff(handoff_client)
    assert r.status_code == 504
    result = r.json()["result"] if "result" in r.json() else r.json()
    assert r.json().get("request_id", result["request_id"]) == result["request_id"]
    assert result["errors"][0]["reason"] == "timeout"


def test_handoff_deadline_is_configurable(monkeypatch):
    import fashion_planner_inventory.api as api
    cases = {None: 60.0, "": 60.0, "  ": 60.0, "90": 90.0, " 120.5 ": 120.5, "600": 600.0,
             "0": 60.0, "-5": 60.0, "601": 60.0, "abc": 60.0}
    for raw, expected in cases.items():
        if raw is None:
            monkeypatch.delenv("INVENTORY_HANDOFF_TIMEOUT_SECONDS", raising=False)
        else:
            monkeypatch.setenv("INVENTORY_HANDOFF_TIMEOUT_SECONDS", raw)
        assert api.handoff_timeout_seconds() == expected, raw


def test_handoff_deadline_message_names_the_configured_seconds(handoff_client, monkeypatch):
    import time
    import fashion_planner_inventory.api as api
    monkeypatch.setattr(api, "HANDOFF_TIMEOUT_SECONDS", 0.05)
    monkeypatch.setattr(api, "_run_inventory", lambda payload: (time.sleep(0.2), api.error_response(payload, "internal", "late"))[1])
    r = _post_handoff(handoff_client)
    result = r.json()["result"] if "result" in r.json() else r.json()
    assert r.status_code == 504
    assert result["errors"][0]["message"] == "HTTP 요청 제한 시간(0.05초)을 초과했습니다."


def test_concurrent_requests_keep_duckdb_cursor_isolation(client):
    from concurrent.futures import ThreadPoolExecutor
    from fastapi.testclient import TestClient

    # Separate clients issue simultaneous requests against the app's shared DB
    # connection. Every response must remain valid and retain its own request ID.
    clients = [TestClient(client.app, headers={"Authorization": "Bearer test-stock-token"}) for _ in range(6)]
    def send(i):
        return _run(clients[i], {**REQ, "request_id": f"parallel-{i}"})[0]
    try:
        with ThreadPoolExecutor(max_workers=6) as pool:
            replies = list(pool.map(send, range(6)))
        assert all(r.status_code == 200 for r in replies)
        assert {r.json()["result"]["request_id"] for r in replies} == {f"parallel-{i}" for i in range(6)}
        assert all(r.json()["result"]["status"] == "ok" for r in replies)
    finally:
        for c in clients:
            c.close()


@pytest.mark.parametrize("authorization", ["", "Bearer wrong"])
def test_auth_precedes_even_malformed_json(handoff_client, authorization, monkeypatch):
    import fashion_planner_inventory.api as api
    def forbidden(*args, **kwargs):
        pytest.fail("unauthorized request entered inventory")
    monkeypatch.setattr(api, "_run_inventory", forbidden)
    result = handoff_client.post("/v1/agents/inventory/runs", content=b"{invalid",
                                 headers={"Authorization": authorization})
    assert result.status_code == 401


@pytest.mark.parametrize("raw", [b'{"request_id":"one","request_id":"two"}', b'{"x":NaN}', b'{"x":1e999}'])
def test_ambiguous_json_rejected(handoff_client, raw):
    assert handoff_client.post("/v1/agents/inventory/runs", content=raw).status_code == 400


def test_db_unavailable_is_alive_and_upstream_no_results_skips_database(monkeypatch):
    import fashion_planner_inventory.api as api
    monkeypatch.setenv("FASHION_PLANNER_SERVICE_TOKEN", "test-stock-token")
    def unavailable():
        raise RuntimeError("fixture DB unavailable")
    monkeypatch.setattr(api, "prepare_database", unavailable)
    monkeypatch.setattr(api, "warm_up_clip", lambda: {"clip_index_loaded": False, "clip_n_indexed": 0})
    with TestClient(api.app) as client:
        assert client.get("/health/live").status_code == 200
        assert client.get("/health/ready").status_code == 503
        assert client.get("/health").status_code == 503
        response = _post_handoff(client, _handoff({**REQ, "status": "no_results", "trends": []}))
        assert response.status_code == 200 and response.json()["status"] == "no_results"
        assert response.json()["result"]["products"] == []
        assert _post_handoff(client).status_code == 503


def test_trend_11_example_images_are_accepted_and_covered_by_the_source_hash(handoff_client):
    """Trend 1.1 may carry trends[].style.example_images (schema_version stays 1.1). Stock accepts it,
    and hashes the unchanged object into input_provenance."""
    import copy
    import hashlib
    import json
    from pathlib import Path

    import jsonschema
    from fashion_planner_inventory.trend_request import canonical_bytes

    trend = copy.deepcopy(REQ)
    images = [{"url": "https://img.example.invalid/0.jpg", "basis": "item_link"},
              {"url": "https://img.example.invalid/1.jpg", "basis": "article"}]
    trend["trends"][0]["style"]["example_images"] = images
    assert trend["schema_version"] == "1.0.0"
    schema = json.loads((Path(__file__).resolve().parents[1] / "contracts/schemas/inventory-request.schema.json")
                        .read_text(encoding="utf-8"))
    assert list(jsonschema.Draft202012Validator(schema).iter_errors(trend)) == []

    r = _post_handoff(handoff_client, _handoff(trend))
    assert r.status_code == 200 and r.json()["status"] == "ok"
    provenance = r.json()["result"]["input_provenance"]
    assert provenance["source_schema_version"] == "1.0.0"
    assert provenance["source_payload_sha256"] == hashlib.sha256(canonical_bytes(trend)).hexdigest()
    without = copy.deepcopy(trend)
    del without["trends"][0]["style"]["example_images"]
    assert provenance["source_payload_sha256"] != hashlib.sha256(canonical_bytes(without)).hexdigest()

