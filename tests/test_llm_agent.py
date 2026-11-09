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
    assert TOOL_NAMES == ["find_categories", "find_colors", "get_inventory", "rank_candidates", "finish"]


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
    ({"colors": ["무지개"]}, "색"),                                    # 21색이 아님
    ({"areas": ["하의"]}, "좁힐 수 없습니다"),                         # 짝 영역 일부만 (2026-11-09 좁히기 금지)
    ({"colors": ["스카이블루"]}, "좁힐 수 없습니다"),                  # 찾을 색 일부만
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
             T("get_inventory"),
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


# ---- 트렌드 아이템 자신의 소재·핏은 재고를 거르는 데 쓰지 않음 (2026-11-09 착샷 기준으로 변경)
def test_item_materials_not_used_to_filter(con, tmp_path):
    item = {**BLOUSE, "materials": ["린넨"], "fit": "루즈"}
    r = run_agent({**BASE, "trend": trend(item)}, con, llm=PolicyLLM(), image_dir=str(tmp_path))
    assert r["status"] == "ok" and r["product"]["sku_ids"] == ["a1"]
    s = r["trend_result"]["search"]
    assert s["attribute_tier"] is None and s["outfit_reference"]["skip_reason"] == "no_snap"


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
               T("get_inventory"),
               T("rank_candidates", {"top_k": 5})]     # 하의·아우터 Ladieswear SS 후보 a1, a2 (a3 아우터는 FW만)


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


# ---- 무신사 스냅 착샷 → 함께 입은 옷의 소재·핏 → 재고 단계 A/B/C (2026-11-09 결정)
SNAP = {"product_id": "musinsa_snap:1", "image_url": "https://img.example/snap1.jpg", "kind": "snap_garment",
        "source": "musinsa_snap", "score": 0.8}
SNAP_LOW = {**SNAP, "product_id": "musinsa_snap:2", "image_url": "https://img.example/snap2.jpg", "score": 0.5}
CATALOG = {"product_id": "otpensource:9", "image_url": "https://img.example/cat.jpg", "kind": "catalog_item",
           "source": "otpensource", "score": 0.95}
SNAP_BLOUSE = {**BLOUSE, "id": "item_1", "similar_products": [CATALOG, SNAP_LOW, SNAP]}


def outfit(bottom=None, outer=None, full=True):
    """착샷 분석 답. bottom/outer: (소재 목록, 핏) 또는 None(안 보임)."""
    g = lambda v: ({"visible": True, "materials": v[0], "fit": v[1]} if v else  # noqa: E731
                   {"visible": False, "materials": [], "fit": None})
    return {"full_outfit": full, "note": "테스트",
            "garments": {"상의": g((["시폰"], "루즈")), "하의": g(bottom), "아우터": g(outer), "원피스": g(None)}}


def run_snap(con, tmp_path, vision, steps=None, item=SNAP_BLOUSE):
    llm = ScriptedLLM(steps or (ALL_BOTTOMS + [pick_first]), vision=vision)
    return run_agent({**BASE, "trend": trend(item)}, con, llm=llm, image_dir=str(tmp_path)), llm


def test_snap_with_highest_score_is_analyzed(con, tmp_path):
    r, llm = run_snap(con, tmp_path, [outfit((["데님"], "와이드"))])
    assert [x["image_url"] for x in llm.seen] == [SNAP["image_url"]]      # catalog_item(0.95)이 아니라 스냅 중 최고
    o = r["trend_result"]["search"]["outfit_reference"]
    assert o["status"] == "used" and o["product_id"] == "musinsa_snap:1" and o["score"] == 0.8
    assert o["garments"]["하의"] == {"visible": True, "materials": ["데님"], "fit": "와이드"}


def test_tier_a_material_and_fit(con, tmp_path):
    # a1 청바지 데님·와이드 → A, a2 소재·핏 없음 → C
    r, _ = run_snap(con, tmp_path, [outfit((["데님"], "와이드"))])
    t = r["trend_result"]["search"]["attribute_tier"]
    assert t == {"tier": "A", "counts": {"A": 1, "B": 0, "C": 1}, "relaxed": []}
    assert r["product"]["sku_ids"] == ["a1"]
    at = r["trend_result"]["reason"]["attributes"]
    assert at["tier"] == "A" and at["materials_match"] is True and at["fit_match"] is True
    assert at["outfit_materials"] == ["데님"] and at["outfit_fit"] == "와이드"


def test_tier_b_relaxes_fit(con, tmp_path):
    # 착샷 하의 데님·스키니 → a1 데님이지만 와이드 → B (핏을 풂)
    r, _ = run_snap(con, tmp_path, [outfit((["데님"], "스키니"))])
    t = r["trend_result"]["search"]["attribute_tier"]
    assert t["tier"] == "B" and t["relaxed"] == ["fit"] and r["product"]["sku_ids"] == ["a1"]
    assert r["trend_result"]["reason"]["attributes"]["fit_match"] is False
    assert any("핏 조건을 풀고" in x for x in r["trend_result"]["limitations"])


def test_tier_c_unknown_and_mismatch_ranked_together(con, tmp_path):
    # 착샷 하의 면 → a1 데님(다른 소재)·a2 소재 없음(미확인) 모두 C, 과잉재고 순위로
    r, _ = run_snap(con, tmp_path, [outfit((["면"], None))])
    t = r["trend_result"]["search"]["attribute_tier"]
    assert t == {"tier": "C", "counts": {"A": 0, "B": 0, "C": 2}, "relaxed": ["materials"]}
    assert r["trend_result"]["search"]["steps"][2]["n_candidates"] == 2
    assert any("소재·핏 조건을 풀고" in x for x in r["trend_result"]["limitations"])


def test_outer_not_in_snap_is_removed(con, tmp_path):
    r, _ = run_snap(con, tmp_path, [outfit((["데님"], "와이드"), outer=None)])
    s = r["trend_result"]["search"]
    assert s["areas"] == ["하의"] and s["outfit_reference"]["areas_removed"] == ["아우터"]
    assert s["steps"][0]["areas"] == ["하의"]
    assert any("착샷에 아우터이(가) 없어" in x for x in r["trend_result"]["limitations"])


def test_outer_in_snap_is_kept(con, tmp_path):
    r, _ = run_snap(con, tmp_path, [outfit((["데님"], "와이드"), outer=(["가죽"], None))])
    assert r["trend_result"]["search"]["areas"] == ["하의", "아우터"]


def test_snap_evidence(con, tmp_path):
    r, _ = run_snap(con, tmp_path, [outfit((["데님"], "와이드"))])
    ev = {e["evidence_id"]: e for e in r["evidence"]}
    assert ev["stock-agent:E-006"]["locator"] == SNAP["image_url"]
    assert ev["stock-agent:E-006"]["source_id"] == "musinsa_snap"


@pytest.mark.parametrize("item, vision, reason", [
    ({**BLOUSE, "similar_products": [CATALOG]}, [], "no_snap"),                 # 판매 상품만
    (SNAP_BLOUSE, [outfit((["데님"], "와이드"), full=False)], "not_full_outfit"),   # 아이템만 잘라낸 사진
    (SNAP_BLOUSE, [__import__("stock_agent.llm", fromlist=["ImageUnavailable"]).ImageUnavailable("404")],
     "image_error"),
    (SNAP_BLOUSE, None, "no_vision"),                                           # 이미지를 못 보는 LLM
])
def test_snap_skipped_goes_on_without_materials(con, tmp_path, item, vision, reason):
    r, llm = run_snap(con, tmp_path, vision, item=item)
    assert r["status"] == "ok", r["trend_result"]["errors"]
    s = r["trend_result"]["search"]
    assert s["outfit_reference"]["status"] == "skipped" and s["outfit_reference"]["skip_reason"] == reason
    assert s["attribute_tier"] is None and s["areas"] == ["하의", "아우터"]
    assert "stock-agent:E-006" not in {e["evidence_id"] for e in r["evidence"]}
    assert any("소재·핏 조건 없이" in x for x in r["trend_result"]["limitations"])
    if reason == "no_snap":
        assert llm.seen == []


@pytest.mark.parametrize("answer, reason", [
    (TimeoutError("30s"), "timeout"),
    (LLMError("bad"), "invalid_response"),
    ({"full_outfit": True}, "invalid_response"),          # garments 없음
])
def test_snap_llm_failure_is_trend_error(con, tmp_path, answer, reason):
    r, _ = run_snap(con, tmp_path, [answer])
    assert err(r) == ("llm_failed", reason) and r["product"] is None
    o = r["trend_result"]["search"]["outfit_reference"]
    assert o["product_id"] == "musinsa_snap:1" and o["status"] == "failed"


def test_snap_without_user_category_uses_all_items(con, tmp_path):
    skirt = {"category_major": "하의", "category_minor": "스커트", "colors": ["화이트"],
             "similar_products": [{**SNAP, "product_id": "musinsa_snap:skirt", "score": 0.9}]}
    req = {k: v for k, v in BASE.items() if k != "target_category"}
    llm = ScriptedLLM([lambda m: LLMError("stop")], vision=[outfit((["데님"], "와이드"))])
    r = run_agent({**req, "trend": trend(SNAP_BLOUSE, skirt)}, con, llm=llm, image_dir=str(tmp_path))
    assert r["trend_result"]["search"]["outfit_reference"]["product_id"] == "musinsa_snap:skirt"


def test_policy_llm_with_snap(con, tmp_path):
    llm = PolicyLLM(vision=[outfit((["데님"], "스키니"))])
    r = run_agent({**BASE, "trend": trend(SNAP_BLOUSE)}, con, llm=llm, image_dir=str(tmp_path))
    assert r["status"] == "ok" and r["product"]["sku_ids"] == ["a1"]
    assert r["trend_result"]["search"]["attribute_tier"]["tier"] == "B"


# ---- 규칙 위반: LLM에게 알려 주고 다시 시도 (2026-11-09 결정)
def test_violation_is_fed_back_and_retried(con, tmp_path):
    # 4번째 호출이 규칙 위반(find_colors 결과에 없는 색) → 오류를 돌려받고 올바르게 다시 부름
    bad = lambda m: T("get_inventory", {"colors": ["무지개"]})      # noqa: E731
    steps = first_steps()[:2] + [bad, lambda m: T("get_inventory", {"colors": _colors(m)}),
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


def test_tone_on_tone_colors_included(con, tmp_path):
    # 기준 색(핑크·화이트) 자체도 find_colors 결과에 들어감 (톤온톤)
    r = run_agent(REQ, con, llm=ScriptedLLM(first_steps() + [T("rank_candidates"), pick_first]), image_dir=str(tmp_path))
    s = r["trend_result"]["search"]["steps"]
    assert "화이트" in s[1]["colors"] and "스카이블루" in s[1]["colors"] and "error" not in s[2]


# ---- get_inventory에서 영역·색 좁히기 금지 (2026-11-09 결정: 어울리는 색 + 톤온톤을 함께 봄)
def test_tone_on_tone_only_narrowing_blocked(con, tmp_path):
    # 어울리는 색을 버리고 톤온톤(핑크·화이트)만 조회 → 실행하지 않고 다시 고르게 함
    steps = first_steps()[:2] + [T("get_inventory", {"colors": ["핑크", "화이트"]}), T("get_inventory"),
                                 T("rank_candidates"), pick_first]
    r = run_agent(REQ, con, llm=ScriptedLLM(steps), image_dir=str(tmp_path))
    s = r["trend_result"]["search"]["steps"]
    assert s[2]["error"]["reason"] == "rule_violation" and "좁힐 수 없습니다" in s[2]["error"]["message"]
    assert "find_colors" in s[2]["error"]["message"]
    assert r["status"] == "ok" and s[3]["n_candidates"] == 1


def test_inventory_args_omitted_use_all(con, tmp_path):
    r = run_agent(REQ, con, llm=ScriptedLLM(first_steps()[:2] + [T("get_inventory"), T("rank_candidates"), pick_first]),
                  image_dir=str(tmp_path))
    s = r["trend_result"]["search"]
    assert s["areas"] == ["하의", "아우터"] and s["colors"] == s["steps"][1]["colors"]
    assert "narrowed" not in s
