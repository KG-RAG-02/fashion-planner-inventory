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
    # conftest의 작은 DB를 파일로 저장해 API가 read_only로 열게 한다
    db = tmp_path / "t.duckdb"
    con.execute(f"ATTACH '{db.as_posix()}' AS f")
    for t in ("article_attrs", "stock_v1", "transactions"):
        con.execute(f"CREATE TABLE f.{t} AS SELECT * FROM {t}")
    con.execute("CREATE TABLE f.articles AS SELECT article_id FROM article_attrs")
    con.execute("DETACH f")
    import stock_agent.api as api
    from stock_agent.llm import PolicyLLM
    monkeypatch.setattr(api, "DB_PATH", str(db))

    def make(llm):
        monkeypatch.setattr(api, "load_llm", lambda: llm)
        return TestClient(api.app)
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
    assert set(op["responses"]) == {"200", "400", "500", "503", "504"}     # 422 대신 400
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
    import stock_agent.api as api
    monkeypatch.setattr(api, "DB_PATH", str(tmp_path / "없음.duckdb"))
    with TestClient(api.app) as c:
        assert c.get("/health").status_code == 503
        r = c.post("/v1/inventory-products", json=REQ)
    assert r.status_code == 503
    assert r.json()["errors"][0]["code"] == "SOURCE_UNAVAILABLE" and r.json()["errors"][0]["reason"] == "db_unavailable"
