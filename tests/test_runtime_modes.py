"""Runtime modes for the containerized stub profile (fastapi·httpx·duckdb가 있을 때만 실행).

local-file: STOCK_DB_PATH가 있으면 R2 설정·HEAD 없이 그 파일을 read-only로 연다.
"""
import hashlib

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")
duckdb = pytest.importorskip("duckdb")

from fastapi.testclient import TestClient  # noqa: E402

from fashion_planner_inventory.trend_request import build_trend_result  # noqa: E402

TOKEN = "test-stock-token"
BALLET = {"style": {"id": "style_012", "name": "발레코어"},
          "items": [{"category_major": "상의", "category_minor": "블라우스", "colors": ["핑크", "화이트"]}]}
REQ = build_trend_result("req_modes", [BALLET], include=["상의"], gender="여")
R2_VARS = ("R2_ACCOUNT_ID", "R2_ENDPOINT", "R2_BUCKET", "R2_DB_KEY", "R2_IMAGE_PREFIX", "R2_ACCESS_KEY_ID",
           "R2_SECRET_ACCESS_KEY", "Access Key ID", "Secret Access Key", "R2_CLIP_INDEX_KEY",
           "R2_SNAP_IMAGE_PREFIX")


def ready_db_file(con, path, numeric_ids=False):
    """conftest의 작은 DB + readiness 열·articles (tests/test_api.py make_client와 같은 모양).

    numeric_ids: a1 → 0000000001처럼 H&M 형식 id로 바꾼다 (R2 이미지 key는 숫자 id만 받는다).
    """
    con.execute(f"ATTACH '{path.as_posix()}' AS f")
    for t in ("article_attrs", "stock_v1", "transactions"):
        con.execute(f"CREATE TABLE f.{t} AS SELECT * FROM {t}")
        if numeric_ids:
            con.execute(f"UPDATE f.{t} SET article_id = lpad(substr(article_id, 2), 10, '0')")
    con.execute("CREATE TABLE f.articles AS SELECT article_id FROM article_attrs")
    con.execute("ALTER TABLE f.stock_v1 ADD COLUMN as_of DATE DEFAULT DATE '2020-09-22'")
    con.execute("ALTER TABLE f.stock_v1 ADD COLUMN version VARCHAR DEFAULT 'virtual_stock_v1'")
    con.execute("ALTER TABLE f.stock_v1 ADD COLUMN data_nature VARCHAR DEFAULT 'sample'")
    con.execute("DETACH f")
    return path


@pytest.fixture
def local_env(monkeypatch):
    """R2 설정이 전혀 없고, R2 객체 저장소를 만들면 실패하는 환경."""
    import fashion_planner_inventory.api as api
    for name in R2_VARS + ("STOCK_DB_PATH",):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("VOGUE_AGENT_SERVICE_BEARER_TOKEN", TOKEN)

    def no_r2(*args, **kwargs):
        raise AssertionError("local-file mode must not build an R2 object store")

    monkeypatch.setattr(api, "R2ObjectStore", no_r2)
    monkeypatch.setattr(api, "warm_up_clip", lambda: {"clip_index_loaded": False, "clip_n_indexed": 0})
    return monkeypatch


def _client():
    import fashion_planner_inventory.api as api
    return TestClient(api.app, headers={"Authorization": f"Bearer {TOKEN}"})


def _handoff(body):
    return {"contract_version": "inventory-handoff", "request_id": body["request_id"], "input": body}


def test_local_file_mode_needs_no_r2_opens_read_only_and_reports_local_file(local_env, con, tmp_path):
    import fashion_planner_inventory.api as api
    db = ready_db_file(con, tmp_path / "stock-local.duckdb")
    local_env.setenv("STOCK_DB_PATH", str(db))
    with _client() as c:
        ready = c.get("/health/ready")
        body = ready.json()
        assert ready.status_code == 200 and body["status"] == "ok"
        assert body["db"] == "local-file" and body["db_ready"] is True
        assert body["db_version"] == "sha256:" + hashlib.sha256(db.read_bytes()).hexdigest()
        assert body["snapshot"]["version"] == "virtual_stock_v1"          # inspect_database still ran
        assert str(tmp_path) not in ready.text and "stock-local" not in ready.text
        with pytest.raises(duckdb.Error):
            api.state["con"].execute("CREATE TABLE forbidden(i INTEGER)")  # opened read-only
        r = c.post("/v1/agents/inventory/runs", json=_handoff(REQ))
    assert r.status_code == 200 and r.json()["status"] == "ok"
    assert r.json()["result"]["products"][0]["sku_ids"] == ["a1"]


def test_local_file_sidecar_must_match_and_missing_file_fails_closed(local_env, con, tmp_path):
    db = ready_db_file(con, tmp_path / "stock-local.duckdb")
    sidecar = db.with_suffix(".sha256")
    sidecar.write_text("0" * 64)
    local_env.setenv("STOCK_DB_PATH", str(db))
    with _client() as c:
        assert c.get("/health/live").status_code == 200
        ready = c.get("/health/ready")
        assert ready.status_code == 503 and ready.json()["db"] == "local-file"
        assert ready.json()["reason"] == "StorageUnavailable"
        assert c.post("/v1/agents/inventory/runs", json=_handoff(REQ)).status_code == 503
    sidecar.write_text(hashlib.sha256(db.read_bytes()).hexdigest() + "\n")
    with _client() as c:
        assert c.get("/health/ready").status_code == 200                   # a matching sidecar is accepted
    local_env.setenv("STOCK_DB_PATH", str(tmp_path / "missing.duckdb"))
    with _client() as c:
        ready = c.get("/health/ready")
        assert ready.status_code == 503 and ready.json()["reason"] == "StorageUnavailable"
        assert "missing.duckdb" not in ready.text


# ---------------------------------------------------------------- image cache (R2 images)

def test_unwritable_image_cache_is_a_limitation_not_a_500(monkeypatch, con, tmp_path):
    """R2 이미지 캐시가 read-only여도 사진은 캐시 없이 응답에 넣고 limitations에 적는다."""
    import io
    import fashion_planner_inventory.api as api
    from fashion_planner_inventory.storage import R2ObjectStore, R2Settings

    class ImageObjects:                          # R2 image objects (fake client, no network)
        downloads = 0

        def head_object(self, **kwargs):
            return {"ContentLength": 4}

        def get_object(self, **kwargs):
            ImageObjects.downloads += 1
            return {"Body": io.BytesIO(b"jpeg"), "ContentLength": 4}

    monkeypatch.delenv("STOCK_DB_PATH", raising=False)
    monkeypatch.setenv("VOGUE_AGENT_SERVICE_BEARER_TOKEN", TOKEN)
    db = ready_db_file(con, tmp_path / "stock.duckdb", numeric_ids=True)   # R2 image keys need digit ids
    blocker = tmp_path / "read-only-cache"
    blocker.write_text("x")
    store = R2ObjectStore(R2Settings("account", "bucket", "db.duckdb", "id", "secret", image_prefix="images"),
                          client=ImageObjects(), cache_dir=tmp_path / "cache", image_cache_dir=blocker / "images")
    monkeypatch.setattr(api, "prepare_database", lambda: (str(db), "test-version", store))
    monkeypatch.setattr(api, "warm_up_clip", lambda: {"clip_index_loaded": False, "clip_n_indexed": 0})
    with _client() as c:
        r = c.post("/v1/agents/inventory/runs", json=_handoff(REQ))
    result = r.json()["result"]
    assert r.status_code == 200 and result["status"] == "ok" and ImageObjects.downloads == 1
    assert result["products"][0]["image"]["size_bytes"] == 4
    assert R2ObjectStore.IMAGE_CACHE_NOTE in result["trend_results"][0]["limitations"]
