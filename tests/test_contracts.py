"""contracts/ 스키마·예시가 코드와 맞는지 (DB 없이)"""

import json
from pathlib import Path

import pytest

from fashion_planner_inventory.multi import run_request, validate_response
from fashion_planner_inventory.schemas import TREND_RESULT_EXAMPLE

jsonschema = pytest.importorskip("jsonschema")

ROOT = Path(__file__).resolve().parents[1]
C = ROOT / "contracts"
REQ = json.loads((C / "schemas/inventory-request.schema.json").read_text(encoding="utf-8"))
RES = json.loads((C / "schemas/inventory-result.schema.json").read_text(encoding="utf-8"))


def errors(schema, obj):
    return [e.message for e in jsonschema.Draft202012Validator(schema).iter_errors(obj)]


def test_schemas_are_valid():
    jsonschema.Draft202012Validator.check_schema(REQ)
    jsonschema.Draft202012Validator.check_schema(RES)


@pytest.mark.parametrize("path", sorted((C / "examples").glob("*.json")) + sorted((ROOT / "examples").glob("*.json")),
                         ids=lambda p: p.name)
def test_examples_match_schema(path):
    obj = json.loads(path.read_text(encoding="utf-8"))
    schema = RES if path.name.startswith("response") else REQ
    assert errors(schema, obj) == []
    if path.name.startswith("response"):
        assert validate_response(obj) == [] or obj["status"] in ("error", "no_results")


def test_request_example_in_docs_is_contract_example():
    assert json.loads((C / "examples/request-trend-result.json").read_text(encoding="utf-8")) == TREND_RESULT_EXAMPLE


@pytest.mark.parametrize("req", [
    {**TREND_RESULT_EXAMPLE, "status": "no_results", "trends": []},     # trend no_results
    {**TREND_RESULT_EXAMPLE, "status": "error", "trends": []},          # trend error
    {**TREND_RESULT_EXAMPLE, "applied_filters": {**TREND_RESULT_EXAMPLE["applied_filters"], "season": None}},  # 시즌 없음
    {"request_id": "r", "season": "2027-SS", "trends": TREND_RESULT_EXAMPLE["trends"]},  # trend 형식 아님
    {**TREND_RESULT_EXAMPLE, "applied_filters": {**TREND_RESULT_EXAMPLE["applied_filters"], "gender": "x"}},
])
def test_real_responses_match_schema(req):
    assert errors(RES, run_request(req, con=None)) == []


def test_real_ok_response_matches_schema(con, tmp_path):
    r = run_request(TREND_RESULT_EXAMPLE, con, image_dir=str(tmp_path))
    assert errors(RES, r) == [] and r["status"] in ("ok", "partial", "no_results")


def test_snap_request_and_response_match_schema(con, tmp_path):
    """무신사 스냅 착샷이 있는 trend 결과 → CLIP 필터를 쓴 응답도 계약에 맞음 (작은 DB, 가짜 CLIP)"""
    from fashion_planner_inventory.clip_matcher import ClipInfo
    trend = json.loads(json.dumps(TREND_RESULT_EXAMPLE["trends"][0]))
    trend["items"][0]["similar_products"] = [
        {"product_id": "musinsa_snap:1", "name": "스냅", "brand": "Musinsa", "image_url": "https://img.example/s.jpg",
         "source": "musinsa_snap", "kind": "snap_garment", "score": 0.8}]
    req = {**TREND_RESULT_EXAMPLE, "trends": [trend]}
    assert errors(REQ, req) == []

    def fake_clip(cand, ref):
        return cand, ClipInfo(True, None, True, len(cand), len(cand), len(cand))
    r = run_request(req, con, image_dir=str(tmp_path), clip_filter=fake_clip)
    assert errors(RES, r) == [] and r["status"] == "ok"
    s = r["trend_results"][0]["search"]
    assert s["outfit_reference"]["status"] == "used" and s["clip"]["applied"] is True
    assert "inventory:style_012:E-006" in r["trend_results"][0]["evidence_ids"]
