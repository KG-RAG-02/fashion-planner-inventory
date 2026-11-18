"""trend 결과 입력, 성별 → 고객군, 팀 오류 코드 (2026-11-08 결정)

DB가 필요 없는 테스트는 위쪽, DuckDB가 필요한 테스트(con fixture)는 아래쪽.
"""

import copy
import re

import pytest

from fashion_planner_inventory.agent import Common, check_common, read_segments, segment_note, trend_common
from fashion_planner_inventory.errors import http_status, pick, to_team
from fashion_planner_inventory.multi import run_internal, run_request
from fashion_planner_inventory.schemas import TREND_RESULT_EXAMPLE
from fashion_planner_inventory.trend_input import read_trend
from fashion_planner_inventory.trend_request import from_trend_result, is_trend_result

RES = TREND_RESULT_EXAMPLE


def res(**kw):
    r = copy.deepcopy(RES)
    r.update(kw)
    return r


# ---------------------------------------------------------------- trend 결과 → 내부 요청

def test_detects_trend_result():
    assert is_trend_result(RES)
    assert not is_trend_result({"request_id": "x", "season": "2027-SS", "trends": []})


def test_converts_applied_filters():
    req, status, note = from_trend_result(RES)
    assert status == "ok" and note is None
    assert req["request_id"] == "req_example_010"
    assert req["season"]["year"] == 2027 and req["season"]["origin"] == "user_requested"
    assert req["target_category"] == ["블라우스"]          # minor 우선
    assert "exclude_categories" not in req
    assert req["target_colors"] == ["화이트"]
    assert req["gender"] == "여"
    assert req["trends"] == RES["trends"] and req["trends"] is not RES["trends"]


@pytest.mark.parametrize("version", ["1.1", "", None])
def test_unsupported_trend_version_names_the_supported_one(version):
    """지원하지 않는 trend schema_version이면 지원하는 값(상수)을 알려 준다."""
    from fashion_planner_inventory.trend_request import UPSTREAM_SCHEMA_VERSION, validate_source
    with pytest.raises(ValueError, match=re.escape(UPSTREAM_SCHEMA_VERSION)):
        validate_source(res(schema_version=version))


def test_trend_producer_version_is_not_misread_as_stock_internal_version():
    r = res(schema_version="1.0.0")
    original = copy.deepcopy(r)
    req, status, _ = from_trend_result(r)
    assert status == "ok"
    assert "schema_version" not in req
    assert r == original


def test_category_ref_major_when_no_minor_and_raw_only_dropped():
    r = res()
    r["applied_filters"]["categories"] = {
        "include": [{"raw": "윗옷", "major": "상의", "minor": None}],
        "exclude": [{"raw": "스커트", "major": "하의", "minor": "스커트"}, {"raw": "모자"}]}
    req, _, _ = from_trend_result(r)
    assert req["target_category"] == ["상의"] and req["exclude_categories"] == ["스커트"]
    assert "모자" in req["_input_notes"][0]


def test_non_color_user_attributes_ignored():
    r = res()
    r["applied_filters"]["attributes"] = [{"type": "material", "value": "린넨"}, {"type": "fit", "value": "루즈"}]
    req, _, _ = from_trend_result(r)
    assert "target_colors" not in req and "materials" not in req


def test_upstream_status_handled():
    _, s, note = from_trend_result(res(status="no_results", trends=[], limitations=["라벨 없음"]))
    assert s == "no_results" and "no_results" in note and "라벨 없음" in note
    _, s, note = from_trend_result(res(status="error", trends=[]))
    assert s == "error"
    with pytest.raises(ValueError):
        from_trend_result(res(status="partial"))


def test_run_request_upstream_no_results_skips_db():
    r = run_request(res(status="no_results", trends=[]), con=None)
    assert r["status"] == "no_results" and r["products"] == [] and r["errors"] == []
    assert r["request_id"] == "req_example_010"


def test_run_request_upstream_error_is_invalid_request():
    r = run_request(res(status="error", trends=[]), con=None)
    assert r["status"] == "error" and r["request_id"] == "req_example_010"
    assert r["errors"][0]["code"] == "INVALID_REQUEST" and r["errors"][0]["reason"] == "upstream_error"
    assert http_status(r) == 400


def test_run_request_bad_upstream_status():
    r = run_request(res(status="weird"), con=None)
    assert r["errors"][0]["code"] == "INVALID_REQUEST" and r["errors"][0]["reason"] == "invalid_input"


def test_run_request_ok_but_empty_trends():
    r = run_request(res(trends=[]), con=None)
    assert r["errors"][0]["code"] == "INVALID_REQUEST" and r["errors"][0]["reason"] == "missing_fields"


# ---------------------------------------------------------------- 성별 → 고객군

@pytest.mark.parametrize("segment, gender, expected", [
    (None, "여", (("Ladieswear", "Divided"), "gender")),
    (None, "남", (("Menswear", "Divided"), "gender")),
    (None, "공용", ((), "gender")),
    (None, None, ((), "all")),
    ("Sport", "여", (("Sport",), "segment")),             # segment가 먼저
    (["Ladieswear", "Sport"], None, (("Ladieswear", "Sport"), "segment")),
])
def test_read_segments(segment, gender, expected):
    segs, _, basis = read_segments(segment, gender)
    assert (segs, basis) == expected


@pytest.mark.parametrize("extra", [{"gender": "female"}, {"segment": "Kids"}])
def test_bad_gender_or_segment_is_invalid_input(extra):
    e, _ = check_common({"request_id": "r", "season": "2027-SS", **extra})
    assert e["errors"][0]["code"] == "invalid_input"


def _c(**kw):
    _, c = check_common({"request_id": "r", "season": "2027-SS", **kw})
    return c


@pytest.mark.parametrize("genders, segs, basis", [
    (["여", "여"], ("Ladieswear", "Divided"), "item_gender"),
    (["여", "남"], ("Ladieswear", "Divided", "Menswear"), "item_gender"),
    (["여", "공용"], (), "all"),
    ([None, None], (), "all"),
])
def test_item_gender_when_no_request_gender(genders, segs, basis):
    trend = {"items": [{"category_major": "상의", "gender": g} for g in genders]}
    c = trend_common(_c(), trend)
    assert c.segments == segs and c.segment_basis == basis
    assert segment_note(c)


def test_request_gender_beats_item_gender():
    trend = {"items": [{"gender": "남"}]}
    c = trend_common(_c(gender="여"), trend)
    assert c.segments == ("Ladieswear", "Divided") and c.segment_basis == "gender"
    assert "요청 성별(여)" in segment_note(c)


# ---------------------------------------------------------------- 팀 오류 코드

@pytest.mark.parametrize("err, code, reason", [
    ({"code": "missing_fields"}, "INVALID_REQUEST", "missing_fields"),
    ({"code": "schema_version_mismatch"}, "INVALID_REQUEST", "schema_version_mismatch"),
    ({"code": "timeout"}, "TIMEOUT", "timeout"),
    ({"code": "invalid_response"}, "INTERNAL_ERROR", "invalid_response"),
    ({"code": "trend_failed"}, "INTERNAL_ERROR", "trend_failed"),
    ({"code": "db_unavailable"}, "SOURCE_UNAVAILABLE", "db_unavailable"),
    ({"code": "INVALID_REQUEST", "reason": "x"}, "INVALID_REQUEST", "x"),
])
def test_to_team(err, code, reason):
    e = to_team({**err, "message": "m"})
    assert e["code"] == code and e["reason"] == reason


def test_pick_and_http():
    assert pick(["INVALID_REQUEST", "TIMEOUT"]) == "TIMEOUT"
    assert http_status({"status": "error", "errors": [{"code": "TIMEOUT"}]}) == 504
    assert http_status({"status": "partial", "errors": [{"code": "INTERNAL_ERROR"}]}) == 200


# ---------------------------------------------------------------- DB 필요 (로컬에서 실행)

def test_trend_result_end_to_end(con, tmp_path):
    """trend 결과 그대로 → 트렌드마다 결과, evidence_ids가 evidence를 가리킴, 고객군은 여 → Ladieswear+Divided."""
    r = run_request(res(), con, image_dir=str(tmp_path))
    assert r["request_id"] == "req_example_010" and len(r["trend_results"]) == 1
    tr = r["trend_results"][0]
    assert tr["search"]["segments"] == ["Ladieswear", "Divided"] and tr["search"]["segment_basis"] == "gender"
    ids = {e["evidence_id"] for e in r["evidence"]}
    assert tr["evidence_ids"] and set(tr["evidence_ids"]) <= ids


def test_segment_list_filters(con, tmp_path):
    """(내부 형식) Menswear+Divided면 Ladieswear 상품(a1, a2 …)은 후보에서 빠진다."""
    base = {"request_id": "r", "season": "2027-SS", "target_category": "상의",
            "trends": [{"style": {"id": "s"}, "items": [{"category_major": "상의", "colors": ["그레이"]}]}]}
    r = run_internal({**base, "gender": "남"}, con, image_dir=str(tmp_path))
    p = r["products"][0]
    assert p is None or p["sku_ids"] == ["a4"]
    r = run_internal({**base, "segment": ["Menswear", "Ladieswear"]}, con, image_dir=str(tmp_path))
    assert r["trend_results"][0]["search"]["segments"] == ["Menswear", "Ladieswear"]


# ---------------------------------------------------------------- 사용자 색 안내 문장

def test_user_color_note_unknown_vs_mismatch():
    from fashion_planner_inventory.agent import user_color_notes
    unknown = read_trend({"style": {"id": "s"}, "items": [{"category_major": "상의", "colors": []}]}, ["상의"], ["핑크"])
    assert unknown.color_basis == "user_colors"
    assert user_color_notes(unknown) == ["트렌드 아이템 색이 확인되지 않아(미확인) 사용자 색(핑크)과 어울리는 색으로 찾았습니다."]
    mismatch = read_trend({"style": {"id": "s"}, "items": [{"category_major": "상의", "colors": ["블랙"]}]}, ["상의"], ["핑크"])
    note = user_color_notes(mismatch)[0]
    assert "사용자 색(핑크)이 이 트렌드 아이템 색에 없습니다" in note and "트렌드 아이템 색: 블랙" in note
