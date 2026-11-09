"""contracts/ 스키마·예시가 코드와 맞는지 (DB 없이)"""

import json
from pathlib import Path

import pytest

from stock_agent.multi import run_request, validate_response
from stock_agent.schemas import TREND_RESULT_EXAMPLE

jsonschema = pytest.importorskip("jsonschema")

ROOT = Path(__file__).resolve().parents[1]
C = ROOT / "contracts"
REQ = json.loads((C / "schemas/stock-agent-request.schema.json").read_text(encoding="utf-8"))
RES = json.loads((C / "schemas/stock-agent-result.schema.json").read_text(encoding="utf-8"))


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
    TREND_RESULT_EXAMPLE,                                               # LLM 없음 → SOURCE_UNAVAILABLE
    {**TREND_RESULT_EXAMPLE, "status": "no_results", "trends": []},     # 02 no_results
    {**TREND_RESULT_EXAMPLE, "status": "error", "trends": []},          # 02 error
    {**TREND_RESULT_EXAMPLE, "applied_filters": {**TREND_RESULT_EXAMPLE["applied_filters"], "season": None}},  # 시즌 없음
    {"request_id": "r", "season": "2027-SS", "trends": TREND_RESULT_EXAMPLE["trends"]},  # 02 형식 아님
    {**TREND_RESULT_EXAMPLE, "applied_filters": {**TREND_RESULT_EXAMPLE["applied_filters"], "gender": "x"}},
])
def test_real_responses_match_schema(req):
    assert errors(RES, run_request(req, con=None)) == []


def test_snap_request_and_response_match_schema(con, tmp_path):
    """무신사 스냅 착샷이 있는 02 결과 → 착샷 소재·핏 단계를 쓴 응답도 계약에 맞음 (작은 DB)"""
    from stock_agent.llm import PolicyLLM
    trend = json.loads(json.dumps(TREND_RESULT_EXAMPLE["trends"][0]))
    trend["items"][0]["similar_products"] = [
        {"product_id": "musinsa_snap:1", "name": "스냅", "brand": "Musinsa", "image_url": "https://img.example/s.jpg",
         "source": "musinsa_snap", "kind": "snap_garment", "score": 0.8}]
    req = {**TREND_RESULT_EXAMPLE, "trends": [trend]}
    assert errors(REQ, req) == []
    answer = {"full_outfit": True, "garments": {"하의": {"visible": True, "materials": ["데님"], "fit": "와이드"},
                                                "아우터": {"visible": False}}}
    r = run_request(req, con, image_dir=str(tmp_path), llm=PolicyLLM(vision=[answer]))
    assert errors(RES, r) == [] and r["status"] == "ok"
    s = r["trend_results"][0]["search"]
    assert s["outfit_reference"]["status"] == "used" and s["attribute_tier"]["tier"] == "A"
    assert "stock-agent:style_012:E-006" in r["trend_results"][0]["evidence_ids"]
