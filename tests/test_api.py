"""HTTP API 테스트 (fastapi·httpx·duckdb가 있을 때만 실행)"""

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")
duckdb = pytest.importorskip("duckdb")

from fastapi.testclient import TestClient  # noqa: E402

REQ = {"schema_version": "1.0", "request_id": "req_api", "season": "2027-SS",
       "target_category": "상의", "trend_colors": ["라이트핑크", "화이트"], "segment": "Ladieswear"}


@pytest.fixture
def client(con, tmp_path, monkeypatch):
    # conftest의 작은 DB를 파일로 저장해 API가 read_only로 열게 한다
    db = tmp_path / "t.duckdb"
    con.execute(f"ATTACH '{db.as_posix()}' AS f")
    for t in ("article_attrs", "stock_v1", "transactions"):
        con.execute(f"CREATE TABLE f.{t} AS SELECT * FROM {t}")
    con.execute("CREATE TABLE f.articles AS SELECT article_id FROM article_attrs")
    con.execute("DETACH f")
    import stock_agent.api as api
    monkeypatch.setattr(api, "DB_PATH", str(db))
    with TestClient(api.app) as c:
        yield c


def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200 and r.json()["status"] == "ok"


def test_post_ok(client):
    r = client.post("/v1/inventory-products", json=REQ)
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok" and body["products"][0]["sku_ids"] == ["a1"]
    assert "charset=utf-8" in r.headers["content-type"]   # 한글 깨짐 방지


def test_post_invalid_is_400(client):
    r = client.post("/v1/inventory-products", json={"request_id": "x"})
    assert r.status_code == 400 and r.json()["errors"][0]["code"] == "missing_fields"


def test_bad_body_is_400_envelope(client):
    r = client.post("/v1/inventory-products", json=["not", "an", "object"])
    assert r.status_code == 400
    body = r.json()
    assert body["status"] == "error" and body["errors"][0]["code"] == "invalid_input" and body["products"] == []


def test_season_object_and_extra_fields_pass_through(client):
    r = client.post("/v1/inventory-products",
                    json={**REQ, "season": {"value": "2027-SS", "source": "inferred"}, "extra_field": 1})
    assert r.status_code == 200 and r.json()["season_basis"]["source"] == "inferred"


def test_docs_show_request_fields_examples_and_400(client):
    spec = client.get("/openapi.json").json()
    op = spec["paths"]["/v1/inventory-products"]["post"]
    assert set(op["responses"]) == {"200", "400", "500"}            # 422 대신 400
    body = op["requestBody"]["content"]["application/json"]
    assert {"user_category", "trend_categories_no_color", "missing_season"} <= set(body["examples"])
    req = spec["components"]["schemas"]["InventoryRequest"]["properties"]
    assert {"season", "target_category", "trend_categories", "trend_colors", "segment"} <= set(req)
    assert "InventoryResponse" in spec["components"]["schemas"]
