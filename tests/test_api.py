"""HTTP API 테스트 (fastapi·httpx·duckdb가 있을 때만 실행)"""

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")
duckdb = pytest.importorskip("duckdb")

from fastapi.testclient import TestClient  # noqa: E402

from stock_agent.trend_request import build_trend_result  # noqa: E402

BALLET = {"style": {"id": "style_012", "name": "발레코어"},
          "items": [{"category_major": "상의", "category_minor": "블라우스", "colors": ["핑크", "화이트"]}]}
REQ = build_trend_result("req_api", [BALLET], include=["상의"], gender="여")      # 02 결과 그대로


@pytest.fixture
def make_client(con, tmp_path, monkeypatch):
    monkeypatch.setenv("VOGUE_AGENT_SERVICE_BEARER_TOKEN", "test-stock-token")
    monkeypatch.delenv("STOCK_DB_PATH", raising=False)   # R2 mode (STOCK_DB_PATH would select local-file)
    # conftest의 작은 DB를 파일로 저장해 API가 read_only로 열게 한다
    db = tmp_path / "t.duckdb"
    con.execute(f"ATTACH '{db.as_posix()}' AS f")
    for t in ("article_attrs", "stock_v1", "transactions"):
        con.execute(f"CREATE TABLE f.{t} AS SELECT * FROM {t}")
    con.execute("CREATE TABLE f.articles AS SELECT article_id FROM article_attrs")
    con.execute("ALTER TABLE f.stock_v1 ADD COLUMN as_of DATE DEFAULT DATE '2020-09-22'")
    con.execute("ALTER TABLE f.stock_v1 ADD COLUMN version VARCHAR DEFAULT 'virtual_stock_v1'")
    con.execute("ALTER TABLE f.stock_v1 ADD COLUMN data_nature VARCHAR DEFAULT 'sample'")
    con.execute("DETACH f")
    import stock_agent.api as api
    from stock_agent.llm import PolicyLLM
    monkeypatch.setattr(api, "prepare_database", lambda: (str(db), "test-version", None))

    def make(llm):
        monkeypatch.setattr(api, "load_llm", lambda: llm)
        return TestClient(api.app, headers={"Authorization": "Bearer test-stock-token"})
    return make


@pytest.fixture
def client(make_client):
    # 실제 LLM 대신 정해진 순서로 도구를 부르는 PolicyLLM
    from stock_agent.llm import PolicyLLM
    with make_client(PolicyLLM()) as c:
        yield c


def test_no_llm_is_503(make_client):
    with make_client(None) as c:
        assert c.get("/health").json()["llm_connected"] is False
        r = c.post("/v1/inventory-products", json=REQ)
    assert r.status_code == 503 and r.json()["errors"][0]["code"] == "SOURCE_UNAVAILABLE"
    assert r.json()["errors"][0]["reason"] == "no_llm"
    assert r.json()["trend_results"][0]["errors"][0]["reason"] == "no_llm"


def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200 and r.json()["status"] == "ok"
    assert r.json()["db"] == "cloudflare-r2" and r.json()["db_version"] == "test-version"
    assert "tmp" not in r.text and "stock.duckdb" not in r.text


def test_post_ok(client):
    r = client.post("/v1/inventory-products", json=REQ)
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok" and body["products"][0]["sku_ids"] == ["a1"]
    assert body["trend_results"][0]["style"]["id"] == "style_012" and body["trend_results"][0]["product_index"] == 0
    assert "charset=utf-8" in r.headers["content-type"]   # 한글 깨짐 방지


def test_post_invalid_is_400(client):
    r = client.post("/v1/inventory-products", json={"request_id": "x"})
    assert r.status_code == 400 and r.json()["errors"][0]["code"] == "INVALID_REQUEST"
    r = client.post("/v1/inventory-products", json={**REQ, "trends": []})
    assert r.status_code == 400 and r.json()["errors"][0]["reason"] == "missing_fields"


def test_bad_body_is_400_envelope(client):
    r = client.post("/v1/inventory-products", json=["not", "an", "object"])
    assert r.status_code == 400
    body = r.json()
    assert body["status"] == "error" and body["errors"][0]["code"] == "INVALID_REQUEST" and body["products"] == []
    assert body["trend_results"] == []


def test_season_origin_and_extra_fields_pass_through(client):
    req = build_trend_result("req_fw", [BALLET], year=2026, season="FW", origin="default_next", include=["상의"])
    r = client.post("/v1/inventory-products", json={**req, "extra_field": 1})
    assert r.status_code == 200 and r.json()["season_basis"]["source"] == "inferred"
    # 02 결과에 03이 안 쓰는 필드가 있어도 그대로 받음
    trend = {**BALLET, "style": {**BALLET["style"], "focus_core": ["color"]},
             "items": [{**BALLET["items"][0], "material": ["면"], "fit": "루즈"}]}
    assert client.post("/v1/inventory-products", json={**REQ, "trends": [trend]}).status_code == 200


def test_docs_show_request_fields_examples_and_400(client):
    spec = client.get("/openapi.json").json()
    op = spec["paths"]["/v1/inventory-products"]["post"]
    assert set(op["responses"]) == {"200", "400", "401", "500", "503", "504"}     # 422 대신 400
    body = op["requestBody"]["content"]["application/json"]
    assert {"one_trend", "multi_trends", "no_user_category", "upstream_no_results", "missing_season"} == set(body["examples"])
    req = spec["components"]["schemas"]["InventoryRequest"]["properties"]
    assert {"request_id", "status", "applied_filters", "trends"} <= set(req)
    assert not {"season", "target_category", "segment"} & set(req)        # 내부 형식 필드는 설명서에 없음
    assert "InventoryResponse" in spec["components"]["schemas"]


def test_post_trends_returns_one_per_trend(client):
    other = {"style": {"id": "style_007", "name": "색 없음"},
             "items": [{"category_major": "아우터", "category_minor": "재킷", "colors": []}]}
    r = client.post("/v1/inventory-products", json={**REQ, "trends": [BALLET, other]})
    assert r.status_code == 200
    body = r.json()
    assert len(body["products"]) == len(body["trend_results"]) == 2
    assert [t["style"]["id"] for t in body["trend_results"]] == ["style_012", "style_007"]


def test_post_trends_all_invalid_is_400(client):
    bad = {"style": {"id": "style_bad"}, "items": [{"category_major": "상의", "colors": ["무지개"]}]}
    r = client.post("/v1/inventory-products", json={**REQ, "trends": [bad]})
    assert r.status_code == 400 and r.json()["status"] == "error"
    assert r.json()["trend_results"][0]["status"] == "error"


def test_internal_format_is_400(client):
    """02 형식이 아니면(applied_filters 없음) 받지 않는다."""
    flat = {"request_id": "req_flat", "season": "2027-SS", "target_category": "상의", "trends": [BALLET]}
    r = client.post("/v1/inventory-products", json=flat)
    assert r.status_code == 400 and "applied_filters" in r.json()["errors"][0]["message"]


def test_trend_result_request(client):
    """02 트렌드 결과 그대로 보내도 된다. 근거 ID가 evidence를 가리킨다."""
    from stock_agent.schemas import TREND_RESULT_EXAMPLE
    r = client.post("/v1/inventory-products", json=TREND_RESULT_EXAMPLE)
    body = r.json()
    assert r.status_code == 200 and body["request_id"] == TREND_RESULT_EXAMPLE["request_id"]
    tr = body["trend_results"][0]
    assert tr["search"]["segments"] == ["Ladieswear", "Divided"]
    assert set(tr["evidence_ids"]) <= {e["evidence_id"] for e in body["evidence"]}


def test_trend_result_no_results_is_200(client):
    from stock_agent.schemas import TREND_RESULT_EXAMPLE
    r = client.post("/v1/inventory-products", json={**TREND_RESULT_EXAMPLE, "status": "no_results", "trends": []})
    assert r.status_code == 200 and r.json()["status"] == "no_results"


def test_db_missing_is_503(tmp_path, monkeypatch):
    monkeypatch.setenv("VOGUE_AGENT_SERVICE_BEARER_TOKEN", "test-stock-token")
    import stock_agent.api as api
    monkeypatch.setattr(api, "prepare_database", lambda: (str(tmp_path / "missing.duckdb"), "test-version", None))
    with TestClient(api.app, headers={"Authorization": "Bearer test-stock-token"}) as c:
        assert c.get("/health").status_code == 503
        r = c.post("/v1/inventory-products", json=REQ)
    assert r.status_code == 503
    assert r.json()["errors"][0]["code"] == "SOURCE_UNAVAILABLE" and r.json()["errors"][0]["reason"] == "db_unavailable"


def test_missing_r2_location_is_unhealthy_without_exposing_credentials(monkeypatch):
    import stock_agent.api as api
    monkeypatch.setattr(api, "load_llm", lambda: None)
    for name in ("R2_ACCOUNT_ID", "R2_BUCKET", "R2_DB_KEY", "STOCK_DB_PATH"):
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
    monkeypatch.setenv("VOGUE_AGENT_SERVICE_BEARER_TOKEN", "test-stock-token")
    with make_client(__import__("stock_agent.llm", fromlist=["PolicyLLM"]).PolicyLLM()) as c:
        yield c


def _handoff(body=REQ, request_id=None, version="vogue-handoff/0.1"):
    return {"contract_version": version, "request_id": request_id or body["request_id"], "input": body}


def _post_handoff(client, body=None, token="test-stock-token"):
    headers = {"Authorization": f"Bearer {token}"} if token is not None else {"Authorization": ""}
    return client.post("/v1/agents/stock-agent/runs", json=body or _handoff(), headers=headers)


def test_handoff_accepts_trend_11_and_records_adapter_provenance(handoff_client):
    trend = {**REQ, "schema_version": "1.1"}
    body = _handoff({**trend, "status": "no_results", "trends": []})
    response = _post_handoff(handoff_client, body=body)
    assert response.status_code == 200
    envelope = response.json()
    assert envelope["status"] == envelope["result"]["status"] == "no_results"
    assert envelope["result"]["input_provenance"] == {
        "source_agent_id": "trend-analysis-agent",
        "source_schema_version": "1.1",
        "internal_schema_version": None,
        "adapter_version": "stock-trend-input-adapter/1.0",
        "transformed": True,
        "source_payload_sha256": envelope["result"]["input_provenance"]["source_payload_sha256"],
    }
    assert len(envelope["result"]["input_provenance"]["source_payload_sha256"]) == 64


def test_handoff_envelope_preserves_result_and_metadata(handoff_client):
    r = _post_handoff(handoff_client)
    assert r.status_code == 200
    env = r.json()
    assert env["contract_version"] == "vogue-handoff/0.1"
    assert env["request_id"] == env["result"]["request_id"] == REQ["request_id"]
    assert env["producer"] == {"agent_id": "stock-agent", "schema_version": "1.0"}
    assert env["status"] == env["result"]["status"] == "ok"
    assert list(env["result"]) == ["schema_version", "request_id", "agent_id", "status", "summary", "products",
                                    "trend_results", "season_basis", "evidence", "errors", "input_provenance"]
    assert env["result"]["products"][0]["sku_ids"] == ["a1"]


@pytest.mark.parametrize("payload,version", [(REQ, "vogue-handoff/9"), (REQ, ""), ([], "vogue-handoff/0.1")])
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
    r = handoff_client.post("/v1/agents/stock-agent/runs", json={
        "contract_version": "vogue-handoff/0.1", "input": REQ},
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
    import stock_agent.api as api
    for code, expected in (("no_llm", 503), ("timeout", 504), ("internal", 500)):
        monkeypatch.setattr(api, "_run_inventory", lambda payload, code=code: api.error_response(
            payload, "llm_failed" if code != "internal" else code,
            "controlled", **({"reason": code} if code != "internal" else {})))
        r = _post_handoff(handoff_client)
        assert r.status_code == expected
        assert r.json()["contract_version"] == "vogue-handoff/0.1"
        assert r.json()["status"] == r.json()["result"]["status"] == "error"
        assert r.json()["request_id"] == r.json()["result"]["request_id"]


def test_handoff_partial_status_is_copied(handoff_client, monkeypatch):
    import stock_agent.api as api
    partial = {"schema_version": "1.0", "request_id": REQ["request_id"], "agent_id": "stock-agent",
               "status": "partial", "summary": "partial", "products": [], "trend_results": [],
               "season_basis": None, "evidence": [], "errors": []}
    monkeypatch.setattr(api, "_run_inventory", lambda payload: partial)
    r = _post_handoff(handoff_client)
    assert r.status_code == 200 and r.json()["status"] == r.json()["result"]["status"] == "partial"


def test_handoff_invalid_json_is_400_not_422(handoff_client):
    r = handoff_client.post("/v1/agents/stock-agent/runs", data="{bad json", headers={
        "Authorization": "Bearer test-stock-token", "Content-Type": "application/json"})
    assert r.status_code == 400 and r.status_code != 422
    assert r.json()["status"] == "error"


def test_handoff_missing_required_fields_is_400_not_422(handoff_client):
    r = handoff_client.post("/v1/agents/stock-agent/runs", json={
        "contract_version": "vogue-handoff/0.1", "request_id": REQ["request_id"]},
        headers={"Authorization": "Bearer test-stock-token"})
    assert r.status_code == 400 and r.status_code != 422
    assert r.json()["result"]["errors"][0]["reason"] == "invalid_input"


def test_handoff_openapi_and_legacy_shares_auth(handoff_client):
    spec = handoff_client.get("/openapi.json").json()
    op = spec["paths"]["/v1/agents/stock-agent/runs"]["post"]
    assert "422" not in op["responses"]
    assert {"200", "400", "500", "503", "504"} <= set(op["responses"])
    assert op["security"] == [{"BearerAuth": []}]
    # Both producer routes share the same fail-closed policy.
    r = handoff_client.post("/v1/inventory-products", json=REQ, headers={"Authorization": ""})
    assert r.status_code == 401
    assert spec["paths"]["/v1/inventory-products"]["post"]["security"] == [{"BearerAuth": []}]


def test_handoff_deadline_returns_504_envelope(handoff_client, monkeypatch):
    import time
    import stock_agent.api as api
    monkeypatch.setattr(api, "HANDOFF_TIMEOUT_SECONDS", 0.01)
    monkeypatch.setattr(api, "_run_inventory", lambda payload: (time.sleep(0.2), api.run_request(payload, None))[1])
    r = _post_handoff(handoff_client)
    assert r.status_code == 504
    result = r.json()["result"] if "result" in r.json() else r.json()
    assert r.json().get("request_id", result["request_id"]) == result["request_id"]
    assert result["errors"][0]["reason"] == "timeout"


def test_handoff_deadline_is_configurable(monkeypatch):
    import stock_agent.api as api
    cases = {None: 60.0, "": 60.0, "  ": 60.0, "90": 90.0, " 120.5 ": 120.5, "600": 600.0,
             "0": 60.0, "-5": 60.0, "601": 60.0, "abc": 60.0}
    for raw, expected in cases.items():
        if raw is None:
            monkeypatch.delenv("STOCK_HANDOFF_TIMEOUT_SECONDS", raising=False)
        else:
            monkeypatch.setenv("STOCK_HANDOFF_TIMEOUT_SECONDS", raw)
        assert api.handoff_timeout_seconds() == expected, raw


def test_handoff_deadline_message_names_the_configured_seconds(handoff_client, monkeypatch):
    import time
    import stock_agent.api as api
    monkeypatch.setattr(api, "HANDOFF_TIMEOUT_SECONDS", 0.05)
    monkeypatch.setattr(api, "_run_inventory", lambda payload: (time.sleep(0.2), api.run_request(payload, None))[1])
    r = _post_handoff(handoff_client)
    result = r.json()["result"] if "result" in r.json() else r.json()
    assert r.status_code == 504
    assert result["errors"][0]["message"] == "HTTP 요청 제한 시간(0.05초)을 초과했습니다."


def test_concurrent_legacy_requests_keep_duckdb_cursor_isolation(client):
    from concurrent.futures import ThreadPoolExecutor
    from fastapi.testclient import TestClient

    # Separate clients issue simultaneous requests against the app's shared DB
    # connection. Every response must remain valid and retain its own request ID.
    clients = [TestClient(client.app, headers={"Authorization": "Bearer test-stock-token"}) for _ in range(6)]
    def send(i):
        return clients[i].post("/v1/inventory-products", json={**REQ, "request_id": f"parallel-{i}"})
    try:
        with ThreadPoolExecutor(max_workers=6) as pool:
            replies = list(pool.map(send, range(6)))
        assert all(r.status_code == 200 for r in replies)
        assert {r.json()["request_id"] for r in replies} == {f"parallel-{i}" for i in range(6)}
        assert all(r.json()["status"] == "ok" for r in replies)
    finally:
        for c in clients:
            c.close()


def test_project08_http_adapter_loopback_integration(handoff_client, monkeypatch):
    """Exercise Project_08's real adapter and envelope validator over loopback."""
    import sys
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from pathlib import Path
    import pytest

    p08_src = Path(__file__).resolve().parents[2] / "vogue-orchestrator" / "src"
    if not p08_src.is_dir():
        pytest.skip("vogue-orchestrator source checkout not available")
    sys.path.insert(0, str(p08_src))
    from vogue_orchestrator.connectors import agent_http_v01_adapter
    from vogue_orchestrator.core import _unpack_handoff_response

    class Bridge(BaseHTTPRequestHandler):
        def do_POST(self):
            raw = self.rfile.read(int(self.headers["Content-Length"]))
            upstream = handoff_client.post(self.path, content=raw, headers={
                "Authorization": self.headers.get("Authorization", ""),
                "Content-Type": "application/json"})
            self.send_response(upstream.status_code)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(upstream.content)
        def log_message(self, fmt, *args):
            return

    try:
        server = ThreadingHTTPServer(("127.0.0.1", 0), Bridge)
    except PermissionError:
        pytest.skip("sandbox does not permit loopback socket binding")
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        adapter = agent_http_v01_adapter(
            f"http://127.0.0.1:{server.server_address[1]}", "stock-agent",
            bearer_token="test-stock-token", dev_mode=True)
        response = adapter({"request_id": REQ["request_id"], **REQ})
        result, agent, version, status = _unpack_handoff_response(
            response, REQ["request_id"], expected_agent="stock-agent")
        assert agent == "stock-agent" and version == "1.0" and status == result["status"] == "ok"
        assert result["request_id"] == REQ["request_id"]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_project08_adapter_request_and_core_validation_in_process(handoff_client, monkeypatch):
    """Exercise the real P08 adapter builder and core validator without sockets."""
    import sys
    from pathlib import Path
    import pytest

    p08_src = Path(__file__).resolve().parents[2] / "vogue-orchestrator" / "src"
    if not p08_src.is_dir():
        pytest.skip("vogue-orchestrator source checkout not available")
    sys.path.insert(0, str(p08_src))
    import vogue_orchestrator.connectors as connectors
    from vogue_orchestrator.core import _unpack_handoff_response

    sent = {}
    def post(url, payload, *, bearer_token=None):
        sent.update(url=url, payload=payload, bearer_token=bearer_token)
        env = _post_handoff(handoff_client, payload,
                            token=bearer_token).json()
        return env
    monkeypatch.setattr(connectors, "_post_json", post)
    adapter = connectors.agent_http_v01_adapter(
        "http://localhost:8002", "stock-agent", bearer_token="test-stock-token", dev_mode=True)
    envelope = adapter({"request_id": REQ["request_id"], **REQ})
    result, producer, version, status = _unpack_handoff_response(
        envelope, REQ["request_id"], expected_agent="stock-agent")
    assert sent["payload"]["contract_version"] == "vogue-handoff/0.1"
    assert sent["payload"]["input"] == REQ
    assert sent["bearer_token"] == "test-stock-token"
    assert (producer, version, status) == ("stock-agent", "1.0", "ok")
    assert result["request_id"] == REQ["request_id"]


@pytest.mark.parametrize("path", ["/v1/inventory-products", "/v1/agents/stock-agent/runs"])
@pytest.mark.parametrize("authorization", ["", "Bearer wrong"])
def test_auth_precedes_even_malformed_json(handoff_client, path, authorization, monkeypatch):
    import stock_agent.api as api
    def forbidden(*args, **kwargs):
        pytest.fail("unauthorized request entered inventory")
    monkeypatch.setattr(api, "_run_inventory", forbidden)
    result = handoff_client.post(path, content=b"{invalid", headers={"Authorization": authorization})
    assert result.status_code == 401


@pytest.mark.parametrize("path", ["/v1/inventory-products", "/v1/agents/stock-agent/runs"])
@pytest.mark.parametrize("raw", [b'{"request_id":"one","request_id":"two"}', b'{"x":NaN}', b'{"x":1e999}'])
def test_ambiguous_json_rejected(handoff_client, path, raw):
    assert handoff_client.post(path, content=raw).status_code == 400


def test_db_unavailable_is_alive_and_upstream_no_results_skips_database(monkeypatch):
    import stock_agent.api as api
    monkeypatch.setenv("VOGUE_AGENT_SERVICE_BEARER_TOKEN", "test-stock-token")
    def unavailable():
        raise RuntimeError("fixture DB unavailable")
    monkeypatch.setattr(api, "prepare_database", unavailable)
    monkeypatch.setattr(api, "load_llm", lambda: None)
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
    hashes the unchanged object into input_provenance, and never forwards the image links to the LLM."""
    import copy
    import hashlib
    import json
    from pathlib import Path

    import jsonschema
    from stock_agent.trend_input import llm_trend
    from stock_agent.trend_request import canonical_bytes

    trend = copy.deepcopy(REQ)
    images = [{"url": "https://img.example.invalid/0.jpg", "basis": "item_link"},
              {"url": "https://img.example.invalid/1.jpg", "basis": "article"}]
    trend["trends"][0]["style"]["example_images"] = images
    assert trend["schema_version"] == "1.1"
    schema = json.loads((Path(__file__).resolve().parents[1] / "contracts/schemas/stock-agent-request.schema.json")
                        .read_text(encoding="utf-8"))
    assert list(jsonschema.Draft202012Validator(schema).iter_errors(trend)) == []

    r = _post_handoff(handoff_client, _handoff(trend))
    assert r.status_code == 200 and r.json()["status"] == "ok"
    provenance = r.json()["result"]["input_provenance"]
    assert provenance["source_schema_version"] == "1.1"
    assert provenance["source_payload_sha256"] == hashlib.sha256(canonical_bytes(trend)).hexdigest()
    without = copy.deepcopy(trend)
    del without["trends"][0]["style"]["example_images"]
    assert provenance["source_payload_sha256"] != hashlib.sha256(canonical_bytes(without)).hexdigest()

    legacy = handoff_client.post("/v1/inventory-products", json=trend)
    assert legacy.status_code == 200
    assert legacy.json()["input_provenance"]["source_payload_sha256"] == provenance["source_payload_sha256"]
    assert "example_images" not in llm_trend(trend["trends"][0])["style"]   # links only; never sent onward
