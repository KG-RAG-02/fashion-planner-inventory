"""Regression checks for source identity, JSON hashing and readonly readiness."""
import copy

import pytest

from fashion_planner_inventory.multi import run_request
from fashion_planner_inventory.schemas import TREND_RESULT_EXAMPLE
from fashion_planner_inventory.trend_request import canonical_bytes, from_trend_result


@pytest.mark.parametrize("change", [
    {"schema_version": None}, {"schema_version": "9.9"}, {"request_id": " "},
    {"agent_id": "other"}, {"status": "partial"}, {"trends": {}},
    {"applied_filters": {"categories": []}},
    {"applied_filters": {"attributes": "color"}},
])
def test_reject_before_any_db_or_model_access(change):
    result = run_request({**TREND_RESULT_EXAMPLE, **change}, None)
    assert result["status"] == "error"
    assert result["errors"][0]["code"] == "INVALID_REQUEST"


def test_no_source_version_is_rejected():
    source = copy.deepcopy(TREND_RESULT_EXAMPLE)
    del source["schema_version"]
    assert run_request(source, None)["status"] == "error"


def test_derived_input_cannot_mutate_raw_and_no_results_has_no_zero_stock():
    source = copy.deepcopy(TREND_RESULT_EXAMPLE)
    before = canonical_bytes(source)
    internal, _, _ = from_trend_result(source)
    internal["trends"][0]["style"]["name"] = "changed internal copy"
    assert canonical_bytes(source) == before
    result = run_request({**source, "status": "no_results", "trends": []}, None)
    assert result["status"] == "no_results" and result["products"] == []
    assert result["input_provenance"]["source_schema_version"] == "1.0.0"


@pytest.mark.parametrize("source", [{1: "value"}, {"value": float("nan")}, {"value": float("inf")}])
def test_ambiguous_canonical_hash_is_rejected(source):
    with pytest.raises(ValueError):
        canonical_bytes(source)


def test_readonly_schema_and_sample_snapshot(tmp_path, con):
    duckdb = pytest.importorskip("duckdb")
    from fashion_planner_inventory.readiness import inspect_database
    # In-memory synthetic fixture first fails without producer provenance.
    con.execute("CREATE TABLE articles AS SELECT article_id FROM article_attrs")
    assert inspect_database(con)["reason"] == "schema_incompatible"
    con.execute("ALTER TABLE stock_v1 ADD COLUMN as_of DATE DEFAULT DATE '2020-09-22'")
    con.execute("ALTER TABLE stock_v1 ADD COLUMN version VARCHAR DEFAULT 'virtual_stock_v1'")
    con.execute("ALTER TABLE stock_v1 ADD COLUMN data_nature VARCHAR DEFAULT 'sample'")
    assert inspect_database(con)["db_ready"] is True
    con.execute("UPDATE stock_v1 SET as_of = DATE '2026-11-12' WHERE article_id='a1'")
    assert inspect_database(con)["reason"] == "snapshot_incompatible"
    con.execute("UPDATE stock_v1 SET as_of = DATE '2020-09-22'")
    con.execute("UPDATE stock_v1 SET stock_qty = NULL WHERE article_id='a1'")
    assert inspect_database(con)["reason"] == "invalid_inventory_values"
    # Actual read_only file connection; no production DB is modified.
    path = tmp_path / "empty.duckdb"
    duckdb.connect(str(path)).close()
    readonly = duckdb.connect(str(path), read_only=True)
    try:
        assert inspect_database(readonly)["db_ready"] is False
        with pytest.raises(duckdb.Error):
            readonly.execute("CREATE TABLE forbidden(i INTEGER)")
    finally:
        readonly.close()
