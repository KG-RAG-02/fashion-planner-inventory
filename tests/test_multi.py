"""multi.run_request: 02 트렌드 결과 → 트렌드마다 agent.run → products n개 + trend_results n개

입구 테스트라 요청은 모두 02 형식(build_trend_result)이다. 사용자 카테고리 상의, 성별 여(→ Ladieswear+Divided).
"""

import pytest

from stock_agent.multi import run_request, validate_response
from stock_agent.trend_request import build_trend_result


def req(*trends, **kw):
    """02 결과. 기본: 2027 SS, 사용자 카테고리 상의, 성별 여."""
    kw = {"include": ["상의"], "gender": "여", **kw}
    return build_trend_result(kw.pop("request_id", "req_multi"), list(trends), **kw)


def trend(sid, name, *items):
    return {"style": {"id": sid, "name": name}, "items": list(items)}


BALLET = trend("style_012", "발레코어",
               {"category_major": "상의", "category_minor": "블라우스", "colors": ["핑크", "화이트"]})   # → a1
NO_COLOR = trend("style_007", "정제된 맥시멀리스트", {"category_major": "아우터", "category_minor": "재킷", "colors": []})
BAD = trend("style_bad", "모르는 색", {"category_major": "상의", "colors": ["무지개"]})


# ---- DB 없이: 입력 검사
def test_empty_trends_is_error():
    r = run_request(req(), con=None)
    assert r["status"] == "error" and r["errors"][0]["code"] == "INVALID_REQUEST" and r["errors"][0]["reason"] == "missing_fields"
    assert "trends" in r["errors"][0]["message"] and r["products"] == [] and r["trend_results"] == []
    assert r["request_id"] == "req_multi"


def test_not_trend_result_is_rejected():
    """02 형식(applied_filters)이 아니면 받지 않는다."""
    flat = {"request_id": "req_flat", "season": "2027-SS", "target_category": "상의", "trends": [BALLET]}
    r = run_request(flat, con=None)
    assert r["status"] == "error" and r["errors"][0]["code"] == "INVALID_REQUEST"
    assert "applied_filters" in r["errors"][0]["message"] and r["request_id"] == "req_flat"


def test_trend_item_must_be_object():
    r = run_request(req("발레코어"), con=None)
    assert r["status"] == "error" and r["errors"][0]["code"] == "INVALID_REQUEST" and r["errors"][0]["reason"] == "invalid_input"


def test_missing_season_checked_once():
    r = run_request(req(BALLET, NO_COLOR, season=None), con=None)
    assert r["status"] == "error" and len(r["errors"]) == 1 and r["trend_results"] == []
    assert "season" in r["errors"][0]["message"] and r["errors"][0]["reason"] == "missing_fields"


def test_all_trends_invalid_is_error():
    r = run_request(req(BAD, {**BAD, "style": {"id": "style_bad2", "name": None}}), con=None)
    assert r["status"] == "error" and r["products"] == [None, None]
    assert [t["status"] for t in r["trend_results"]] == ["error", "error"]
    assert [t["product_index"] for t in r["trend_results"]] == [0, 1]
    assert r["errors"][0]["code"] == "INVALID_REQUEST" and r["errors"][0]["style_ids"] == ["style_bad", "style_bad2"]
    assert "style_bad(모르는 색): 알 수 있는 트렌드 색이 없습니다" in r["errors"][0]["message"]
    assert validate_response(r) == []


# ---- 작은 DB로 실행
@pytest.fixture
def go():
    return run_request


def test_one_result_per_trend_in_order(go, con, tmp_path):
    r = go(req(BALLET, NO_COLOR), con, image_dir=str(tmp_path))
    assert r["status"] == "ok", [t["errors"] for t in r["trend_results"]]
    assert len(r["products"]) == len(r["trend_results"]) == 2
    assert r["products"][0]["sku_ids"] == ["a1"]
    assert "reason" not in r["products"][0] and "rank" not in r["products"][0]          # 상품 정보만
    assert [t["style"]["id"] for t in r["trend_results"]] == ["style_012", "style_007"]
    assert [t["product_index"] for t in r["trend_results"]] == [0, 1]
    # 트렌드별 경고는 trend_results 안에
    assert any("색 조건 없이" in x for x in r["trend_results"][1]["limitations"])
    # 근거: 트렌드마다 E-001~, ID 앞에 style id
    ev = [e["evidence_id"] for e in r["evidence"]]
    assert "stock-agent:style_012:E-001" in ev and "stock-agent:style_007:E-001" in ev and len(ev) == len(set(ev))
    assert {e["style_id"] for e in r["evidence"]} == {"style_012", "style_007"}
    assert "style_012(발레코어)" in r["summary"] and "트렌드 2개" in r["summary"]
    assert r["season_basis"]["source"] == "user"
    assert validate_response(r) == []


def test_duplicate_product_allowed(go, con, tmp_path):
    r = go(req(BALLET, {**BALLET, "style": {"id": "style_013", "name": "또 발레"}}), con, image_dir=str(tmp_path))
    assert [p["sku_ids"][0] for p in r["products"]] == ["a1", "a1"]


def test_same_style_id_twice_keeps_evidence_unique(go, con, tmp_path):
    r = go(req(BALLET, BALLET), con, image_dir=str(tmp_path))
    assert r["status"] == "ok" and validate_response(r) == []
    assert "stock-agent:style_012#2:E-001" in {e["evidence_id"] for e in r["evidence"]}


def test_partial_keeps_null_slot(go, con, tmp_path):
    r = go(req(BAD, BALLET), con, image_dir=str(tmp_path))
    assert r["status"] == "partial"
    assert r["products"][0] is None and r["products"][1]["sku_ids"] == ["a1"]    # 순서 유지
    assert r["trend_results"][0]["status"] == "error" and r["trend_results"][1]["product_index"] == 1
    assert r["errors"] == [] and "style_bad(모르는 색) 입력 오류" in r["summary"]


def test_no_results_when_nothing_found(go, con, tmp_path):
    # 남 → Menswear+Divided: 후보는 a4(하의 그레이)뿐인데 레드와 어울리는 색(블랙·화이트·네이비·베이지)이 아님.
    # 사용자 색(레드)이라 완화하지 않는다
    red = trend("style_red", "레드", {"category_major": "상의", "colors": ["레드"]})
    r = go(req(red, gender="남", colors=["레드"]), con, image_dir=str(tmp_path))
    assert r["status"] == "no_results" and r["products"] == [None]
    assert "조건에 맞는 재고 없음" in r["summary"]


def test_single_trend_same_format(go, con, tmp_path):
    r = go(req(BALLET), con, image_dir=str(tmp_path))
    assert r["status"] == "ok" and len(r["products"]) == 1 and len(r["trend_results"]) == 1


def test_invalid_user_color_checked_once():
    r = run_request(req(BALLET, NO_COLOR, colors=["흰색"]), con=None)
    assert r["status"] == "error" and len(r["errors"]) == 1 and r["errors"][0]["code"] == "INVALID_REQUEST"
    assert "target_colors" in r["errors"][0]["message"]


def test_user_colors_judged_per_trend(go, con, tmp_path):
    # 같은 사용자 색(화이트)이 발레코어에는 있고, 색 없는 트렌드에는 없음 → 트렌드마다 따로 판단
    r = go(req(BALLET, NO_COLOR, colors=["화이트"]), con, image_dir=str(tmp_path))
    b, n = r["trend_results"]
    assert b["search"]["trend_color_basis"] == "user_colors_in_trend" and b["search"]["trend_colors"] == ["화이트"]
    assert n["search"]["trend_color_basis"] == "user_colors" and n["search"]["trend_colors"] == ["화이트"]
    assert any("트렌드 아이템 색이 확인되지 않아(미확인)" in x for x in n["limitations"])
    assert not any("트렌드 아이템 색에 없습니다" in x for x in n["limitations"])
    assert not any("사용자 색" in x for x in b["limitations"])
    assert validate_response(r) == []


def test_gender_and_input_notes_reach_trend_results(go, con, tmp_path):
    r = go(req(BALLET, include=["상의", "모자"]), con, image_dir=str(tmp_path))
    tr = r["trend_results"][0]
    assert tr["search"]["segments"] == ["Ladieswear", "Divided"] and tr["search"]["segment_basis"] == "gender"
    assert any("모자" in x for x in tr["limitations"])
    assert set(tr["evidence_ids"]) <= {e["evidence_id"] for e in r["evidence"]}
