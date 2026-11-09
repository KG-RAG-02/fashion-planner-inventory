"""llm_agent: LLM이 도구를 골라 부르는 03 agent (가짜 LLM으로 검사)"""

import pytest

from stock_agent.agent import check_common
from stock_agent.llm import LLMError, LLMReply, PolicyLLM, ScriptedLLM, ToolCall as T, last_result
from stock_agent.llm_agent import MAX_RULE_RETRIES, TOOL_NAMES, run_agent
from stock_agent.multi import run_internal

BASE = {"schema_version": "1.0", "request_id": "req_llm", "season": "2027-SS",
        "target_category": "상의", "segment": "Ladieswear"}


def trend(*items, sid="style_012", name="발레코어"):
    return {"style": {"id": sid, "name": name}, "items": list(items)}


BLOUSE = {"category_major": "상의", "category_minor": "블라우스", "colors": ["라이트핑크", "화이트"]}   # → 하의 a1(스카이블루)
NEON = {"category_major": "상의", "category_minor": "탑", "colors": ["네온"]}        # 어울리는 색 재고 없음
REQ = {**BASE, "trend": trend(BLOUSE)}


def err(resp):
    e = resp["trend_result"]["errors"][0]
    return e["code"], e.get("reason")


def first_steps():
    """find_categories → find_colors(트렌드 기준 색 그대로) → get_inventory (앞 결과 그대로)."""
    return [
        T("find_categories"),
        lambda m: T("find_colors", {"base_colors": __import__("json").loads(m[0]["content"])["base_colors"]}),
        lambda m: T("get_inventory", {"areas": _areas(m), "colors": last_result(m)["colors"]}),
    ]


def _areas(messages):
    import json
    return next(json.loads(x["content"])["areas"] for x in messages
                if x.get("role") == "tool" and x["name"] == "find_categories")


def stubborn(steps):
    """마지막(규칙 위반) 호출을 허용된 다시 시도 횟수만큼 되풀이하는 LLM → 결국 위반으로 실패."""
    steps = list(steps)
    return ScriptedLLM(steps + [steps[-1]] * MAX_RULE_RETRIES)


def pick_first(m):
    return T("finish", {"article_id": last_result(m)["candidates"][0]["article_id"], "reason": "1위 상품"})


# ---- LLM 없음·실패 → 오류 (규칙 방식으로 대신 내지 않음)
def test_no_llm_is_error_not_fallback(con, tmp_path):
    r = run_agent(REQ, con, llm=None, image_dir=str(tmp_path))
    assert r["status"] == "error" and r["product"] is None
    assert err(r) == ("llm_failed", "no_llm") and r["trend_result"]["search"]["mode"] == "llm"


def test_input_errors_come_before_llm():
    r = run_agent({**BASE, "trend": trend({"category_major": "상의", "colors": ["무지개"]})}, con=None, llm=None)
    assert err(r)[0] == "invalid_input"


@pytest.mark.parametrize("step, reason", [
    (TimeoutError("30s"), "timeout"),
    (LLMError("bad json"), "invalid_response"),
    (LLMReply(text="그냥 글로 답함"), "invalid_response"),
    (T("delete_db"), "invalid_response"),
])
def test_llm_failures(con, tmp_path, step, reason):
    r = run_agent(REQ, con, llm=ScriptedLLM([step]), image_dir=str(tmp_path))
    assert err(r) == ("llm_failed", reason) and r["product"] is None


def test_step_limit(con, tmp_path):
    r = run_agent(REQ, con, llm=PolicyLLM(), image_dir=str(tmp_path), max_steps=3)
    assert err(r) == ("llm_failed", "step_limit")
    assert [s["tool"] for s in r["trend_result"]["search"]["steps"]] == ["find_categories", "find_colors", "get_inventory"]


# ---- 정상: 도구 기록과 결정 주체가 응답에 남음
def test_policy_run_records_steps(con, tmp_path):
    r = run_agent(REQ, con, llm=PolicyLLM(), image_dir=str(tmp_path))
    assert r["status"] == "ok" and r["product"]["sku_ids"] == ["a1"]
    s = r["trend_result"]["search"]
    assert s["mode"] == "llm" and s["relaxed"] == []
    assert [x["tool"] for x in s["steps"]] == ["find_categories", "find_colors", "get_inventory", "rank_candidates",
                                               "finish"]
    rs = r["trend_result"]["reason"]
    assert rs["decided_by"] == "llm" and rs["llm_reason"] == "순위 1위 상품입니다."
    assert rs["attributes"]["product_materials"] == ["데님"]


def test_tools_have_schemas():
    assert TOOL_NAMES == ["find_categories", "find_colors", "get_inventory", "filter_attributes", "rank_candidates",
                          "finish"]


# ---- 사용자 조건은 완화하지 않음
def test_cannot_change_user_category(con, tmp_path):
    r = run_agent(REQ, con, llm=stubborn([T("find_categories", {"base_categories": ["하의"]})]),
                  image_dir=str(tmp_path))
    assert err(r) == ("llm_failed", "rule_violation") and "사용자 카테고리" in r["trend_result"]["errors"][0]["message"]


def test_cannot_change_user_colors(con, tmp_path):
    req = {**REQ, "target_colors": ["화이트"]}
    r = run_agent(req, con, llm=stubborn([T("find_categories"), T("find_colors", {"base_colors": []})]),
                  image_dir=str(tmp_path))
    assert err(r) == ("llm_failed", "rule_violation") and "사용자 색" in r["trend_result"]["errors"][0]["message"]


def test_user_colors_not_relaxed_when_empty(con, tmp_path):
    # 사용자 색 네온 → 어울리는 색 재고 없음. PolicyLLM은 사용자 색을 풀지 않고 no_results로 끝냄
    r = run_agent({**BASE, "target_colors": ["네온"], "trend": trend(NEON)}, con, llm=PolicyLLM(), image_dir=str(tmp_path))
    assert r["status"] == "no_results" and r["trend_result"]["search"]["relaxed"] == []


@pytest.mark.parametrize("inv_args, word", [
    ({"areas": ["상의"], "colors": ["스카이블루"]}, "영역"),           # 짝 영역이 아닌 영역
    ({"areas": ["하의"], "colors": ["무지개"]}, "색"),                 # 21색이 아님
])
def test_inventory_args_must_come_from_earlier_tools(con, tmp_path, inv_args, word):
    steps = first_steps()[:2] + [T("get_inventory", inv_args)]
    r = run_agent(REQ, con, llm=stubborn(steps), image_dir=str(tmp_path))
    assert err(r) == ("llm_failed", "rule_violation") and word in r["trend_result"]["errors"][0]["message"]


def test_inventory_before_categories_is_violation(con, tmp_path):
    r = run_agent(REQ, con, llm=stubborn([T("get_inventory", {"areas": ["하의"], "colors": ["화이트"]})]),
                  image_dir=str(tmp_path))
    assert err(r) == ("llm_failed", "rule_violation")


def test_excluded_area_cannot_come_back(con, tmp_path):
    req = {**REQ, "exclude_categories": ["아우터"]}
    steps = first_steps()[:2] + [T("get_inventory", {"areas": ["하의", "아우터"], "colors": ["스카이블루"]})]
    r = run_agent(req, con, llm=stubborn(steps), image_dir=str(tmp_path))
    assert err(r) == ("llm_failed", "rule_violation") and "제외" in r["trend_result"]["errors"][0]["message"]


# ---- finish 검사
def test_finish_must_pick_from_ranking(con, tmp_path):
    steps = first_steps() + [T("rank_candidates"), T("finish", {"article_id": "a4", "reason": "좋아 보임"})]
    r = run_agent(REQ, con, llm=stubborn(steps), image_dir=str(tmp_path))
    assert err(r) == ("llm_failed", "invalid_final")


def test_finish_null_with_candidates_is_invalid(con, tmp_path):
    steps = first_steps() + [T("finish", {"article_id": None, "reason": "없음"})]
    r = run_agent(REQ, con, llm=stubborn(steps), image_dir=str(tmp_path))
    assert err(r) == ("llm_failed", "invalid_final")


def test_finish_reason_numbers_must_come_from_tools(con, tmp_path):
    steps = first_steps() + [T("rank_candidates"),
                             lambda m: T("finish", {"article_id": "a1", "reason": "재고가 98765개로 많음"})]
    r = run_agent(REQ, con, llm=stubborn(steps), image_dir=str(tmp_path))
    assert err(r) == ("llm_failed", "invalid_final") and "98765" in r["trend_result"]["errors"][0]["message"]


def test_llm_can_pick_lower_rank(con, tmp_path):
    # 트렌드 색 중 화이트만 기준으로, 하나라도 어울리는 색까지 넓혀서 순위 마지막 후보를 고름
    steps = [T("find_categories"), T("find_colors", {"base_colors": ["화이트"], "mode": "any", "note": "넓게"}),
             lambda m: T("get_inventory", {"areas": ["하의"], "colors": last_result(m)["colors"]}),
             T("rank_candidates", {"top_k": 5}),
             lambda m: T("finish", {"article_id": last_result(m)["candidates"][-1]["article_id"], "reason": "마지막 후보",
                                    "why_not_higher": "더 높은 순위 상품에 사진이 없음"})]
    r = run_agent(REQ, con, llm=ScriptedLLM(steps), image_dir=str(tmp_path))
    assert r["status"] == "ok", r["trend_result"]["errors"]
    relaxed = {x["condition"] for x in r["trend_result"]["search"]["relaxed"]}
    assert relaxed == {"trend_colors", "color_rule"}
    assert any("완화" in x and "넓게" in x for x in r["trend_result"]["limitations"])


# ---- 트렌드 조건 완화
def test_relax_trend_colors_when_empty(con, tmp_path):
    r = run_agent({**BASE, "trend": trend(NEON)}, con, llm=PolicyLLM(), image_dir=str(tmp_path))
    assert r["status"] == "ok", r["trend_result"]["errors"]
    s = r["trend_result"]["search"]
    assert s["relaxed"][0]["condition"] == "trend_colors" and s["relaxed"][0]["to"] == []
    assert [x["tool"] for x in s["steps"]][:5] == ["find_categories", "find_colors", "get_inventory", "find_colors",
                                                   "get_inventory"]
    assert r["trend_result"]["reason"]["color"]["rule"] == "색 조건 없음"


# ---- 소재·핏
def test_filter_by_trend_item_materials(con, tmp_path):
    item = {**BLOUSE, "colors": ["화이트"], "materials": ["데님"]}
    r = run_agent({**BASE, "trend": trend(item)}, con, llm=PolicyLLM(), image_dir=str(tmp_path))
    s = r["trend_result"]["search"]
    assert r["product"]["sku_ids"] == ["a1"] and s["trend_materials"] == ["데님"]
    assert s["attribute_filter"]["materials_source"] == "trend_item" and s["relaxed"] == []
    assert r["trend_result"]["reason"]["attributes"]["matched_materials"] == ["데님"]


def test_relax_materials_when_filter_empties(con, tmp_path):
    item = {**BLOUSE, "materials": ["린넨"]}
    r = run_agent({**BASE, "trend": trend(item)}, con, llm=PolicyLLM(), image_dir=str(tmp_path))
    assert r["status"] == "ok"
    assert r["trend_result"]["search"]["relaxed"][0]["condition"] == "trend_materials"


def test_trend_materials_cannot_be_replaced(con, tmp_path):
    item = {**BLOUSE, "materials": ["데님"]}
    steps = first_steps() + [T("filter_attributes", {"materials": ["면"]})]
    r = run_agent({**BASE, "trend": trend(item)}, con, llm=stubborn(steps), image_dir=str(tmp_path))
    assert err(r) == ("llm_failed", "rule_violation")


def test_llm_inferred_materials(con, tmp_path):
    steps = first_steps() + [T("filter_attributes", {"materials": ["데님"], "note": "발레코어는 데님과 매치"}),
                             T("rank_candidates"), pick_first]
    r = run_agent(REQ, con, llm=ScriptedLLM(steps), image_dir=str(tmp_path))
    s = r["trend_result"]["search"]
    assert s["trend_attributes_source"] == "llm_inferred" and s["attribute_filter"]["materials"] == ["데님"]
    assert any("LLM이 트렌드 설명을 보고 추론" in x for x in r["trend_result"]["limitations"])


def test_vocab_checked(con, tmp_path):
    steps = first_steps() + [T("filter_attributes", {"fit": ["슈퍼와이드"]})]
    r = run_agent(REQ, con, llm=stubborn(steps), image_dir=str(tmp_path))
    assert err(r) == ("llm_failed", "rule_violation")


# ---- 사용자 카테고리 여러 개·제외
def test_multiple_targets_intersection(con, tmp_path):
    req = {**BASE, "target_category": ["상의", "아우터"], "trend": trend(BLOUSE)}
    r = run_agent(req, con, llm=PolicyLLM(), image_dir=str(tmp_path))
    assert r["trend_result"]["search"]["areas"] == ["하의"]
    assert r["trend_result"]["reason"]["pairing"]["target_categories"] == ["상의", "아우터"]
    assert any("공통 짝" in x for x in r["trend_result"]["limitations"])


def test_llm_can_choose_union(con, tmp_path):
    req = {**BASE, "target_category": ["상의", "아우터"], "trend": trend(BLOUSE)}
    r = run_agent(req, con, llm=ScriptedLLM([T("find_categories", {"combine": "union"}),
                                             lambda m: LLMError("stop")]), image_dir=str(tmp_path))
    steps = r["trend_result"]["search"]["steps"]
    assert steps[0]["areas"] == ["하의", "원피스"]       # 상의 짝 하의·아우터 + 아우터 짝 상의·하의·원피스, 사용자 영역 뺌


def test_exclude_minor_category(con, tmp_path):
    for go in (lambda q: run_internal(q, con, image_dir=str(tmp_path), mode="rule"),
               lambda q: run_internal(q, con, image_dir=str(tmp_path), llm=PolicyLLM())):
        q = {k: v for k, v in BASE.items()}
        r = go({**q, "exclude_categories": ["청바지"], "trends": [trend(BLOUSE)]})
        assert r["products"] == [None] or r["products"][0]["sku_ids"] != ["a1"]
        assert any("제외 카테고리(청바지)" in x for x in r["trend_results"][0]["limitations"])


def test_exclude_all_pair_areas_is_no_results(con, tmp_path):
    q = {**BASE, "target_category": "원피스", "exclude_categories": ["아우터"], "trend": trend(BLOUSE)}
    r = run_agent(q, con, llm=PolicyLLM(), image_dir=str(tmp_path))
    assert r["status"] == "no_results" and any("찾을 짝 영역이 없습니다" in x for x in r["trend_result"]["limitations"])


@pytest.mark.parametrize("extra, word", [
    ({"exclude_categories": ["신발"]}, "제외할 수 없는"),
    ({"exclude_categories": ["상의"]}, "모두 있습니다"),
    ({"target_category": ["상의", "신발"]}, "짝 카테고리"),
])
def test_category_input_errors(extra, word):
    e, _ = check_common({**BASE, **extra})
    assert e["errors"][0]["code"] == "invalid_input" and word in e["errors"][0]["message"]


def test_target_category_list_and_string_same():
    _, a = check_common({**BASE, "target_category": "상의"})
    _, b = check_common({**BASE, "target_category": ["상의"]})
    assert a == b and a.targets == ("상의",)


# ---- multi: 모든 트렌드가 LLM 실패(no_llm)면 전체 오류 코드 SOURCE_UNAVAILABLE
def test_multi_all_llm_failed(con, tmp_path):
    r = run_internal({**BASE, "trends": [trend(BLOUSE), trend(NEON, sid="s2")]}, con, image_dir=str(tmp_path))
    assert r["status"] == "error" and r["errors"][0]["code"] == "SOURCE_UNAVAILABLE"
    assert r["errors"][0]["reason"] == "no_llm"


# ---- 1위가 아닌 상품: why_not_higher 필수 (2026-11-09 결정)
ALL_BOTTOMS = [T("find_categories"), T("find_colors", {"base_colors": [], "note": "색 조건 풂"}),
               lambda m: T("get_inventory", {"areas": ["하의"], "colors": last_result(m)["colors"]}),
               T("rank_candidates", {"top_k": 5})]                      # 하의 Ladieswear 후보 a1, a2


def last_ranking(messages):
    """마지막 rank_candidates 결과 (그 뒤에 막힌 finish의 오류 결과가 있어도)."""
    import json
    return next(json.loads(x["content"]) for x in reversed(messages)
                if x.get("role") == "tool" and x["name"] == "rank_candidates")


def pick_second(why=None):
    def f(m):
        args = {"article_id": last_ranking(m)["candidates"][1]["article_id"], "reason": "2위 상품"}
        if why is not None:
            args["why_not_higher"] = why
        return T("finish", args)
    return f


def test_lower_rank_without_why_is_invalid(con, tmp_path):
    r = run_agent(REQ, con, llm=stubborn(ALL_BOTTOMS + [pick_second()]), image_dir=str(tmp_path))
    assert err(r) == ("llm_failed", "invalid_final") and "why_not_higher" in r["trend_result"]["errors"][0]["message"]
    r = run_agent(REQ, con, llm=stubborn(ALL_BOTTOMS + [pick_second("   ")]), image_dir=str(tmp_path))
    assert err(r) == ("llm_failed", "invalid_final")


def test_lower_rank_with_why_is_kept(con, tmp_path):
    r = run_agent(REQ, con, llm=ScriptedLLM(ALL_BOTTOMS + [pick_second("1위는 트렌드 소재 정보가 미확인")]),
                  image_dir=str(tmp_path))
    assert r["status"] == "ok", r["trend_result"]["errors"]
    assert r["trend_result"]["rank"] == 2
    assert r["trend_result"]["reason"]["llm_why_not_higher"] == "1위는 트렌드 소재 정보가 미확인"


def test_first_rank_has_no_why(con, tmp_path):
    r = run_agent(REQ, con, llm=ScriptedLLM(ALL_BOTTOMS + [pick_first]), image_dir=str(tmp_path))
    assert r["trend_result"]["rank"] == 1 and r["trend_result"]["reason"]["llm_why_not_higher"] is None


def test_why_numbers_must_come_from_tools(con, tmp_path):
    r = run_agent(REQ, con, llm=stubborn(ALL_BOTTOMS + [pick_second("재고 98765개")]), image_dir=str(tmp_path))
    assert err(r) == ("llm_failed", "invalid_final") and "98765" in r["trend_result"]["errors"][0]["message"]


# ---- 소재·핏 거르기: 정보 없는 상품(미확인)은 남김 (2026-11-09 결정)
def _filter_then_first(**f):
    return ALL_BOTTOMS[:3] + [T("filter_attributes", f), lambda m: T("rank_candidates", {"top_k": 5}), pick_first]


def test_filter_keeps_unknown_materials(con, tmp_path):
    # a1 데님(일치), a2 소재 없음(미확인) → 둘 다 남음
    r = run_agent(REQ, con, llm=ScriptedLLM(_filter_then_first(materials=["데님"])), image_dir=str(tmp_path))
    s = r["trend_result"]["search"]
    assert s["steps"][3]["n_candidates"] == 2
    assert s["attribute_filter"]["counts"] == {"materials": {"matched": 1, "unknown": 1}, "fit": None}
    assert any("미확인)은 빼지 않고" in x and "소재: 일치 1개·미확인 1개" in x for x in r["trend_result"]["limitations"])


def test_filter_drops_known_mismatch(con, tmp_path):
    # 면: a1 데님(불일치) 빠짐, a2 미확인 남음
    r = run_agent(REQ, con, llm=ScriptedLLM(_filter_then_first(materials=["면"])), image_dir=str(tmp_path))
    assert r["product"]["sku_ids"] == ["a2"]
    assert r["trend_result"]["reason"]["attributes"]["materials_match"] is None        # a2는 미확인


def test_filter_fit_unknown_kept(con, tmp_path):
    # 루즈: a1 와이드(불일치) 빠짐, a2 핏 없음(미확인) 남음
    r = run_agent(REQ, con, llm=ScriptedLLM(_filter_then_first(fit=["루즈"])), image_dir=str(tmp_path))
    assert r["product"]["sku_ids"] == ["a2"]
    assert r["trend_result"]["search"]["attribute_filter"]["counts"]["fit"] == {"matched": 0, "unknown": 1}


@pytest.mark.parametrize("ms, wanted, state", [
    (["데님"], ["데님"], "matched"), (["면"], ["데님"], "mismatched"), ([], ["데님"], "unknown"),
    (None, ["데님"], "unknown"), (["데님"], [], None)])
def test_material_state(ms, wanted, state):
    from stock_agent.llm_agent import material_state
    assert material_state(ms, wanted) == state


@pytest.mark.parametrize("f, wanted, state", [
    ("루즈", ["루즈"], "matched"), ("와이드", ["루즈"], "mismatched"), (None, ["루즈"], "unknown"),
    (float("nan"), ["루즈"], "unknown"), ("루즈", [], None)])
def test_fit_state(f, wanted, state):
    from stock_agent.llm_agent import fit_state
    assert fit_state(f, wanted) == state


# ---- 규칙 위반: LLM에게 알려 주고 다시 시도 (2026-11-09 결정)
def test_violation_is_fed_back_and_retried(con, tmp_path):
    # 4번째 호출이 규칙 위반(find_colors 결과에 없는 색) → 오류를 돌려받고 올바르게 다시 부름
    bad = lambda m: T("get_inventory", {"areas": ["하의"], "colors": ["무지개"]})      # noqa: E731
    steps = first_steps()[:2] + [bad, lambda m: T("get_inventory", {"areas": ["하의"], "colors": _colors(m)}),
                                 T("rank_candidates"), pick_first]
    llm = ScriptedLLM(steps)
    r = run_agent(REQ, con, llm=llm, image_dir=str(tmp_path))
    assert r["status"] == "ok", r["trend_result"]["errors"]
    s = r["trend_result"]["search"]["steps"]
    assert s[2]["tool"] == "get_inventory" and s[2]["error"]["reason"] == "rule_violation"
    assert "쓸 수 있는 색" in s[2]["error"]["message"] and "error" not in s[3]
    fed_back = [m for m in llm.calls[3] if m.get("role") == "tool"][-1]
    assert '"error": "rule_violation"' in fed_back["content"] and '"retries_left": 2' in fed_back["content"]
    assert any("규칙에 맞지 않는 호출을 해 다시 골랐습니다(1번" in x for x in r["trend_result"]["limitations"])


def test_violation_limit(con, tmp_path):
    # 위반 3번째에서 실패. 막힌 호출 3개가 모두 steps에 남음
    bad = T("get_inventory", {"areas": ["하의"], "colors": ["무지개"]})
    r = run_agent(REQ, con, llm=ScriptedLLM(first_steps()[:2] + [bad] * 3), image_dir=str(tmp_path))
    assert err(r) == ("llm_failed", "rule_violation") and "3번째" in r["trend_result"]["errors"][0]["message"]
    assert [x.get("error", {}).get("reason") for x in r["trend_result"]["search"]["steps"]] == [
        None, None, "rule_violation", "rule_violation", "rule_violation"]


def test_missing_why_can_be_fixed_on_retry(con, tmp_path):
    r = run_agent(REQ, con, llm=ScriptedLLM(ALL_BOTTOMS + [pick_second(), pick_second("1위는 사진이 없음")]),
                  image_dir=str(tmp_path))
    assert r["status"] == "ok" and r["trend_result"]["rank"] == 2
    assert r["trend_result"]["search"]["steps"][-2]["error"]["reason"] == "invalid_final"


def test_non_retryable_failure_is_logged(con, tmp_path):
    # 인자 형식 오류(invalid_response)는 다시 시도하지 않지만 steps에는 남김
    r = run_agent(REQ, con, llm=ScriptedLLM([T("find_categories"), T("find_colors", {"base_colors": 5})]),
                  image_dir=str(tmp_path))
    assert err(r)[1] == "invalid_response"
    assert r["trend_result"]["search"]["steps"][-1]["error"]["reason"] == "invalid_response"


def _colors(messages):
    import json
    return next(json.loads(x["content"])["colors"] for x in reversed(messages)
                if x.get("role") == "tool" and x["name"] == "find_colors")


def test_tone_on_tone_colors_allowed(con, tmp_path):
    # 기준 색(핑크·화이트) 자체도 찾을 수 있음 → 화이트로 조회해도 위반이 아님
    steps = first_steps()[:2] + [T("get_inventory", {"areas": ["하의"], "colors": ["화이트"]}), T("rank_candidates"),
                                 lambda m: T("finish", {"article_id": None, "reason": "후보 없음"})]
    r = run_agent(REQ, con, llm=ScriptedLLM(steps), image_dir=str(tmp_path))
    s = r["trend_result"]["search"]["steps"]
    assert "error" not in s[2] and "화이트" in s[1]["colors"]


# ---- LLM이 앞 도구 결과보다 좁혀 쓴 영역·색은 기록 (2026-11-09 결정)
def test_narrowed_colors_and_areas_recorded(con, tmp_path):
    steps = first_steps()[:2] + [T("get_inventory", {"areas": ["하의"], "colors": ["스카이블루"], "note": "데님 위주"}),
                                 T("rank_candidates"), pick_first]
    r = run_agent(REQ, con, llm=ScriptedLLM(steps), image_dir=str(tmp_path))
    tr = r["trend_result"]
    got = {x["condition"]: x for x in tr["search"]["narrowed"]}
    assert got["areas"]["to"] == ["하의"] and got["colors"]["to"] == ["스카이블루"]
    assert got["colors"]["tone_on_tone_only"] is False and got["colors"]["why"] == "데님 위주"
    assert any("LLM이 찾을 색을 좁혔습니다" in x and "스카이블루만" in x for x in tr["limitations"])
    assert "LLM이 고른 색(스카이블루)" in tr["summary"]


def test_tone_on_tone_only_narrowing(con, tmp_path):
    steps = first_steps()[:2] + [lambda m: T("get_inventory", {"areas": _areas(m), "colors": ["핑크", "화이트"]}),
                                 T("rank_candidates"), lambda m: T("finish", {"article_id": None, "reason": "없음"})]
    r = run_agent(REQ, con, llm=ScriptedLLM(steps), image_dir=str(tmp_path))
    n = r["trend_result"]["search"]["narrowed"]
    assert [x["condition"] for x in n] == ["colors"] and n[0]["tone_on_tone_only"] is True
    assert any("(같은 색, 톤온톤만)" in x for x in r["trend_result"]["limitations"])


def test_full_use_not_narrowed(con, tmp_path):
    r = run_agent(REQ, con, llm=ScriptedLLM(first_steps() + [T("rank_candidates"), pick_first]), image_dir=str(tmp_path))
    assert r["trend_result"]["search"]["narrowed"] == []
