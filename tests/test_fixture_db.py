"""Synthetic fixture DB (fashion_planner_inventory.fixture_db, scripts/build_fixture_db.py) and the cross-repo stub run.

examples/stub/inventory-request.json carries the trend-analysis-agent stub result unchanged (inventory-handoff);
with STOCK_DB_PATH=<fixture DB> it must give ok/partial with a product.
"""
import hashlib
import json
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")
duckdb = pytest.importorskip("duckdb")
jsonschema = pytest.importorskip("jsonschema")

from fastapi.testclient import TestClient  # noqa: E402

from fashion_planner_inventory import fixture_db  # noqa: E402
from fashion_planner_inventory.trend_request import canonical_bytes  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
STUB = ROOT / "examples" / "stub"
TOKEN = "test-stock-token"
R2_VARS = ("R2_ACCOUNT_ID", "R2_ENDPOINT", "R2_BUCKET", "R2_DB_KEY", "R2_IMAGE_PREFIX", "R2_ACCESS_KEY_ID",
           "R2_SECRET_ACCESS_KEY", "Access Key ID", "Secret Access Key", "R2_CLIP_INDEX_KEY",
           "R2_SNAP_IMAGE_PREFIX")


def _json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def _schema(name):
    return jsonschema.Draft202012Validator(_json(ROOT / "contracts" / "schemas" / name))


def test_fixture_db_is_deterministic_ready_and_marked_synthetic(tmp_path):
    first = fixture_db.build_fixture_db(tmp_path / "a.duckdb")
    second = fixture_db.build_fixture_db(tmp_path / "b.duckdb")
    assert first["content_sha256"] == second["content_sha256"]
    assert first["db_ready"] is True and first["reason"] is None and first["synthetic_fixture"] is True
    assert first["tables"] == {"article_attrs": 9, "articles": 9, "stock_fixture": 3, "stock_v1": 9, "transactions": 15}
    assert first["storage_version"] in (None, "v1.0.0+")          # v1.0.0 format: readable by DuckDB 1.x and 2.x
    with pytest.raises(FileExistsError):
        fixture_db.build_fixture_db(tmp_path / "a.duckdb")
    assert fixture_db.build_fixture_db(tmp_path / "a.duckdb", overwrite=True)["content_sha256"] == first["content_sha256"]


def test_build_script_refuses_to_overwrite(tmp_path, capsys):
    out = tmp_path / "fixture.duckdb"
    assert fixture_db.main([str(out)]) == 0 and out.is_file()
    assert json.loads(capsys.readouterr().out)["db_ready"] is True
    assert fixture_db.main([str(out)]) == 2 and "--force" in capsys.readouterr().err


def test_conftest_tables_are_the_fixture_base_tables(con):
    assert [r[0] for r in con.execute("SELECT article_id FROM article_attrs ORDER BY 1").fetchall()] == [
        "a1", "a2", "a3", "a4", "a5", "a6"]


def test_stub_request_is_an_unchanged_trend_11_result():
    req = _json(STUB / "inventory-request.json")
    assert req["contract_version"] == "inventory-handoff" and req["request_id"] == req["input"]["request_id"]
    source = req["input"]
    assert source["schema_version"] == "1.1" and source["status"] == "ok" and source["trends"]
    assert source["trends"][0]["style"]["example_images"]
    assert list(_schema("inventory-request.schema.json").iter_errors(source)) == []


@pytest.fixture
def stub_env(monkeypatch, tmp_path):
    """local-file: R2 설정·키 없이, R2 객체 저장소를 만들면 실패."""
    import fashion_planner_inventory.api as api
    for name in R2_VARS + ("STOCK_IMAGE_CACHE_DIR",):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(api, "R2ObjectStore", lambda *a, **k: pytest.fail("stub mode must not build an R2 store"))
    monkeypatch.setattr(api, "warm_up_clip", lambda: {"clip_index_loaded": False, "clip_n_indexed": 0})
    db = tmp_path / "stock-fixture.duckdb"
    fixture_db.build_fixture_db(db)
    monkeypatch.setenv("STOCK_DB_PATH", str(db))
    monkeypatch.setenv("VOGUE_AGENT_SERVICE_BEARER_TOKEN", TOKEN)
    return db


def test_trend_stub_result_runs_ok_in_local_file_mode(stub_env):
    import fashion_planner_inventory.api as api
    from fashion_planner_inventory.readiness import FIXTURE_LIMITATION
    req = _json(STUB / "inventory-request.json")
    expected = _json(STUB / "inventory-response-summary.json")
    with TestClient(api.app) as c:
        ready = c.get("/health/ready")
        assert ready.status_code == 200
        assert {k: ready.json()[k] for k in ("db", "synthetic_fixture")} == {
            k: expected["health_ready"][k] for k in ("db", "synthetic_fixture")}
        assert ready.json()["db_version"] == "sha256:" + hashlib.sha256(stub_env.read_bytes()).hexdigest()
        unauth = c.post("/v1/agents/inventory/runs", json=req)
        r = c.post("/v1/agents/inventory/runs", json=req, headers={"Authorization": f"Bearer {TOKEN}"})
    assert unauth.status_code == expected["without_bearer"]["http_status"] == 401
    assert r.status_code == expected["http_status"] == 200
    body = r.json()
    assert (body["contract_version"], body["request_id"], body["producer"], body["status"]) == (
        expected["contract_version"], expected["request_id"], expected["producer"], expected["status"])
    result = body["result"]
    assert result["status"] in ("ok", "partial") and any(p is not None for p in result["products"])
    assert fixture_db.summarize_result(result) == expected["result"]
    assert result["input_provenance"]["source_payload_sha256"] == hashlib.sha256(canonical_bytes(req["input"])).hexdigest()
    assert all(FIXTURE_LIMITATION in tr["limitations"] for tr in result["trend_results"])
    assert list(_schema("inventory-result.schema.json").iter_errors(result)) == []
