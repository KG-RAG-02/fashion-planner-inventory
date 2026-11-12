"""Runtime modes for the containerized stub profile (fastapi·httpx·duckdb가 있을 때만 실행).

local-file: STOCK_DB_PATH가 있으면 R2 설정·HEAD 없이 그 파일을 read-only로 연다.
"""
import hashlib

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")
duckdb = pytest.importorskip("duckdb")

from fastapi.testclient import TestClient  # noqa: E402

from stock_agent.trend_request import build_trend_result  # noqa: E402

TOKEN = "test-stock-token"
BALLET = {"style": {"id": "style_012", "name": "발레코어"},
          "items": [{"category_major": "상의", "category_minor": "블라우스", "colors": ["핑크", "화이트"]}]}
REQ = build_trend_result("req_modes", [BALLET], include=["상의"], gender="여")
R2_VARS = ("R2_ACCOUNT_ID", "R2_ENDPOINT", "R2_BUCKET", "R2_DB_KEY", "R2_IMAGE_PREFIX", "R2_ACCESS_KEY_ID",
           "R2_SECRET_ACCESS_KEY", "Access Key ID", "Secret Access Key")


def ready_db_file(con, path):
    """conftest의 작은 DB + readiness 열·articles (tests/test_api.py make_client와 같은 모양)."""
    con.execute(f"ATTACH '{path.as_posix()}' AS f")
    for t in ("article_attrs", "stock_v1", "transactions"):
        con.execute(f"CREATE TABLE f.{t} AS SELECT * FROM {t}")
    con.execute("CREATE TABLE f.articles AS SELECT article_id FROM article_attrs")
    con.execute("ALTER TABLE f.stock_v1 ADD COLUMN as_of DATE DEFAULT DATE '2020-09-22'")
    con.execute("ALTER TABLE f.stock_v1 ADD COLUMN version VARCHAR DEFAULT 'virtual_stock_v1'")
    con.execute("ALTER TABLE f.stock_v1 ADD COLUMN data_nature VARCHAR DEFAULT 'sample'")
    con.execute("DETACH f")
    return path


@pytest.fixture
def local_env(monkeypatch):
    """R2 설정이 전혀 없고, R2 객체 저장소를 만들면 실패하는 환경."""
    import stock_agent.api as api
    for name in R2_VARS + ("STOCK_DB_PATH",):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("VOGUE_AGENT_SERVICE_BEARER_TOKEN", TOKEN)

    def no_r2(*args, **kwargs):
        raise AssertionError("local-file mode must not build an R2 object store")

    monkeypatch.setattr(api, "R2ObjectStore", no_r2)
    return monkeypatch


def _client():
    import stock_agent.api as api
    return TestClient(api.app, headers={"Authorization": f"Bearer {TOKEN}"})


def _handoff(body):
    return {"contract_version": "vogue-handoff/0.1", "request_id": body["request_id"], "input": body}


def test_local_file_mode_needs_no_r2_opens_read_only_and_reports_local_file(local_env, con, tmp_path):
    import stock_agent.api as api
    from stock_agent.llm import PolicyLLM
    db = ready_db_file(con, tmp_path / "stock-local.duckdb")
    local_env.setenv("STOCK_DB_PATH", str(db))
    local_env.setattr(api, "load_llm", lambda: PolicyLLM())
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
        r = c.post("/v1/agents/stock-agent/runs", json=_handoff(REQ))
    assert r.status_code == 200 and r.json()["status"] == "ok"
    assert r.json()["result"]["products"][0]["sku_ids"] == ["a1"]


def test_local_file_sidecar_must_match_and_missing_file_fails_closed(local_env, con, tmp_path):
    import stock_agent.api as api
    local_env.setattr(api, "load_llm", lambda: None)
    db = ready_db_file(con, tmp_path / "stock-local.duckdb")
    sidecar = db.with_suffix(".sha256")
    sidecar.write_text("0" * 64)
    local_env.setenv("STOCK_DB_PATH", str(db))
    with _client() as c:
        assert c.get("/health/live").status_code == 200
        ready = c.get("/health/ready")
        assert ready.status_code == 503 and ready.json()["db"] == "local-file"
        assert ready.json()["reason"] == "StorageUnavailable"
        assert c.post("/v1/agents/stock-agent/runs", json=_handoff(REQ)).status_code == 503
    sidecar.write_text(hashlib.sha256(db.read_bytes()).hexdigest() + "\n")
    with _client() as c:
        assert c.get("/health/ready").status_code == 200                   # a matching sidecar is accepted
    local_env.setenv("STOCK_DB_PATH", str(tmp_path / "missing.duckdb"))
    with _client() as c:
        ready = c.get("/health/ready")
        assert ready.status_code == 503 and ready.json()["reason"] == "StorageUnavailable"
        assert "missing.duckdb" not in ready.text


# ---------------------------------------------------------------- STOCK_LLM_PROVIDER

@pytest.fixture
def llm_env(monkeypatch):
    """개발자 .env·키에 영향받지 않는 LLM 설정."""
    import stock_agent.llm as llm
    monkeypatch.setattr(llm, "read_dotenv", lambda *args, **kwargs: None)
    for name in ("STOCK_LLM_PROVIDER", "LLM_API_KEY", "LLM_MODEL", "LLM_TIMEOUT"):
        monkeypatch.delenv(name, raising=False)
    return monkeypatch


def test_policy_provider_needs_no_key_and_never_builds_openai(llm_env):
    import stock_agent.llm as llm
    import stock_agent.llm_openai as llm_openai

    def no_openai(*args, **kwargs):
        raise AssertionError("policy provider must not build the OpenAI client")

    llm_env.setattr(llm_openai, "OpenAILLM", no_openai)
    llm_env.setenv("STOCK_LLM_PROVIDER", "policy")
    assert llm.llm_provider() == "policy" and isinstance(llm.load_llm(), llm.PolicyLLM)
    llm_env.setenv("LLM_API_KEY", "synthetic-key-not-used")
    assert isinstance(llm.load_llm(), llm.PolicyLLM)
    llm_env.setenv("STOCK_LLM_PROVIDER", " Policy ")
    assert isinstance(llm.load_llm(), llm.PolicyLLM)


def test_default_provider_is_openai_and_unknown_provider_fails_closed(llm_env):
    import stock_agent.llm as llm
    assert llm.llm_provider() == "openai" and llm.load_llm() is None       # no key → no_llm, as before
    llm_env.setenv("STOCK_LLM_PROVIDER", "anthropic")
    assert llm.llm_provider() == "invalid" and isinstance(llm.load_llm(), llm.UnavailableLLM)


def test_policy_with_local_file_serves_without_network_and_labels_results(local_env, llm_env, con, tmp_path):
    from stock_agent.llm import POLICY_LIMITATION
    llm_env.setenv("STOCK_DB_PATH", str(ready_db_file(con, tmp_path / "stock-local.duckdb")))
    llm_env.setenv("STOCK_LLM_PROVIDER", "policy")
    with _client() as c:
        ready = c.get("/health/ready").json()
        assert ready["llm_provider"] == "policy" and ready["llm_configured"] is True and ready["db"] == "local-file"
        r = c.post("/v1/agents/stock-agent/runs", json=_handoff(REQ))
    result = r.json()["result"]
    assert r.status_code == 200 and result["status"] == "ok" and result["products"][0] is not None
    assert not [e for tr in result["trend_results"] for e in tr["errors"] if e.get("reason") == "no_llm"]
    assert all(POLICY_LIMITATION in tr["limitations"] for tr in result["trend_results"])
