"""트렌드 여러 개 → 트렌드마다 1위 1개"""

from stock_agent.agent import validate_response
from stock_agent.multi import run_multi, run_request

BASE = {"schema_version": "1.0", "request_id": "req_multi", "season": "2027-SS",
        "target_category": "상의", "segment": "Ladieswear"}


# ---- DB 없이: 입력 검사
def test_empty_trends_is_error():
    r = run_request({**BASE, "trends": []}, con=None)
    assert r["status"] == "error" and r["errors"][0]["code"] == "missing_fields"
    assert "trends" in r["errors"][0]["message"]


def test_trend_item_must_be_object():
    r = run_multi({**BASE, "trends": ["발레코어"]}, con=None)
    assert r["status"] == "error" and r["errors"][0]["code"] == "invalid_input"


def test_missing_season_checked_once():
    req = {k: v for k, v in BASE.items() if k != "season"}
    r = run_multi({**req, "trends": [{"trend_colors": ["화이트"]}, {"trend_colors": ["레드"]}]}, con=None)
    assert r["status"] == "error" and len(r["errors"]) == 1
    assert "받은 필드:" in r["errors"][0]["message"] and "trends" in r["errors"][0]["message"]


def test_all_trends_invalid_is_error_with_trend_ids():
    r = run_multi({**BASE, "trends": [{"trend_id": "a", "trend_colors": ["무지개"]},
                                      {"trend_id": "b", "trend_colors": ["무지개"]}]}, con=None)
    assert r["status"] == "error" and r["products"] == []
    assert r["errors"][0]["code"] == "invalid_input" and r["errors"][0]["trend_ids"] == ["a", "b"]
    assert [t["status"] for t in r["trend_results"]] == ["error", "error"]


# ---- 작은 DB로 실행
TRENDS = [
    {"trend_id": "t1", "name": "발레코어", "trend_colors": ["라이트핑크", "화이트"]},     # → a1 (스카이블루 청바지)
    {"trend_id": "t2", "name": "색 없는 트렌드", "trend_colors": [], "trend_categories": ["재킷"]},   # 색 조건 없이
]


def test_one_product_per_trend(con, tmp_path):
    r = run_multi({**BASE, "trends": TRENDS}, con, image_dir=str(tmp_path))
    assert r["status"] == "ok", r["errors"]
    assert [p["trend_id"] for p in r["products"]] == ["t1", "t2"]
    assert r["products"][0]["trend_name"] == "발레코어" and r["products"][0]["sku_ids"] == ["a1"]
    assert [t["trend_id"] for t in r["trend_results"]] == ["t1", "t2"]
    assert all(t["n_products"] == 1 for t in r["trend_results"])
    # 근거는 트렌드별로 따로, 상품이 가리키는 근거가 모두 있음
    ev = {e["evidence_id"] for e in r["evidence"]}
    assert "stock-agent:t1:E-001" in ev and "stock-agent:t2:E-001" in ev
    assert set(r["products"][1]["evidence_ids"]) <= ev
    # 공통 경고(가상 데이터)는 위에, 트렌드별 경고(색 조건 없음)는 trend_results에
    assert any("sample" in x for x in r["limitations"])
    assert any("색 조건 없이" in x for x in r["trend_results"][1]["limitations"])
    assert "t1(발레코어)" in r["summary"] and "트렌드 2개" in r["summary"]
    assert validate_response(r) == []


def test_duplicate_product_allowed(con, tmp_path):
    same = {"trend_colors": ["라이트핑크", "화이트"]}
    r = run_multi({**BASE, "trends": [{"trend_id": "x", **same}, {"trend_id": "y", **same}]}, con, image_dir=str(tmp_path))
    assert [p["sku_ids"][0] for p in r["products"]] == ["a1", "a1"]


def test_partial_when_one_trend_fails(con, tmp_path):
    r = run_multi({**BASE, "trends": [TRENDS[0], {"trend_id": "bad", "trend_colors": ["무지개"]}]},
                  con, image_dir=str(tmp_path))
    assert r["status"] == "partial" and len(r["products"]) == 1
    assert r["errors"][0]["trend_ids"] == ["bad"]
    assert "bad 입력 오류" in r["summary"]


def test_top_level_trend_fields_ignored_when_trends_given(con, tmp_path):
    r = run_multi({**BASE, "trend_colors": ["무지개"], "trends": [TRENDS[0]]}, con, image_dir=str(tmp_path))
    assert r["status"] == "ok"


def test_without_trends_is_single_format(con, tmp_path):
    r = run_request({**BASE, "trend_colors": ["라이트핑크", "화이트"]}, con, image_dir=str(tmp_path))
    assert "trend_results" not in r and len(r["products"]) == 1
