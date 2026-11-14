"""agent.run: 트렌드 1개 → product 1개 + trend_result 1개"""

import pandas as pd
import pytest

from stock_agent.agent import (
    TemplateSummarizer,
    allowed_numbers,
    build_product,
    build_reason,
    numbers_in,
    read_season,
    reason_detail,
    run,
    safe_summary,
    validate_output,
)
from stock_agent.overstock import resolve_pair_areas
from stock_agent.season import map_season
from stock_agent.trend_input import TrendInput

FACTS = {
    "season_label": "2027 SS(봄·여름)", "target": "상의", "area_basis": "user", "given_areas": ["상의"],
    "areas": ["하의", "아우터"], "trend_colors": ["라이트핑크", "화이트"], "n_candidates": 5199, "n_items": 1,
    "top1_rank": 1, "top1_category": "팬츠", "top1_color": "베이지", "top1_stock_qty": 149,
    "reason_detail": "재고 금액 447만 원(가상), 판매율 62%(가상).",
}


def trend(*items, sid="style_012", name="발레코어"):
    return {"style": {"id": sid, "name": name}, "items": list(items)}


BLOUSE = {"category_major": "상의", "category_minor": "블라우스", "colors": ["라이트핑크", "화이트"]}
REQ = {"schema_version": "1.0", "request_id": "req_test", "season": "2027-SS",
       "target_category": "상의", "segment": "Ladieswear", "trend": trend(BLOUSE)}


# ---- summary
def test_template_summary_numbers_come_from_facts():
    text = TemplateSummarizer().summarize(FACTS)
    assert "5,199개" in text and "149개" in text and "1위 팬츠(베이지)" in text
    assert "어울리는 색 중 가장 많이 겹치는 색" in text and text.endswith("재고 금액 447만 원(가상), 판매율 62%(가상).")
    assert numbers_in(text) <= allowed_numbers(FACTS)


def test_safe_summary_rejects_made_up_numbers():
    class Liar:
        def summarize(self, facts):
            return "재고가 99,999개로 매우 많습니다."
    text, warn = safe_summary(Liar(), FACTS)
    assert "99,999" not in text and warn


def test_safe_summary_keeps_good_custom_summary():
    class Good:
        def summarize(self, facts):
            return f"후보 {facts['n_candidates']:,}개 중 재고 {facts['top1_stock_qty']}개."
    text, warn = safe_summary(Good(), FACTS)
    assert text == "후보 5,199개 중 재고 149개." and warn == []


# ---- 공통 입력 오류 (DB 없이): product·trend_result 없음
@pytest.mark.parametrize("req, code", [
    ({"request_id": "r1", "target_category": "상의", "trend": trend(BLOUSE)}, "missing_fields"),   # 시즌 없음
    ({"request_id": "r1", "season": "2027-SS"}, "missing_fields"),                                 # 트렌드 없음
    ({"request_id": "r1", "season": "2027", "trend": trend(BLOUSE)}, "invalid_input"),
    ({"request_id": "r1", "season": "2027-SS", "target_category": "신발", "trend": trend(BLOUSE)}, "invalid_input"),
    ({"schema_version": "9.9", "request_id": "r1", "season": "2027-SS", "trend": trend(BLOUSE)},
     "schema_version_mismatch"),
    ({"request_id": "r1", "season": "2027-SS", "target_colors": ["흰색"], "trend": trend(BLOUSE)}, "invalid_input"),
    ({"request_id": "r1", "season": "2027-SS", "target_colors": ["화이트", 3], "trend": trend(BLOUSE)}, "invalid_input"),
])
def test_common_input_errors(req, code):
    resp = run(req, con=None)
    assert resp["status"] == "error" and resp["errors"][0]["code"] == code
    assert resp["product"] is None and resp["trend_result"] is None
    assert validate_output(resp) == []


# ---- 트렌드 입력 오류 (DB 없이): trend_result.status = error
@pytest.mark.parametrize("items, target, code", [
    ([{"category_major": "아우터", "colors": ["무지개"]}], "상의", "invalid_input"),   # 모르는 색만
    ([{"colors": []}], None, "missing_fields"),                                        # 카테고리·색 모두 없음
    ([], None, "missing_fields"),
])
def test_trend_errors(items, target, code):
    resp = run({**REQ, "target_category": target, "trend": trend(*items)}, con=None)
    tr = resp["trend_result"]
    assert resp["status"] == "error" and resp["product"] is None and resp["errors"] == []
    assert tr["status"] == "error" and tr["errors"][0]["code"] == code
    assert tr["style"] == {"id": "style_012", "name": "발레코어"}
    assert validate_output(resp) == []


# ---- 시즌 입력 (02)
def test_read_season():
    assert read_season("2027-SS") == ("2027-SS", "unknown")
    assert read_season({"value": "2026 겨울", "source": "inferred"}) == ("2026 겨울", "inferred")
    assert read_season({"value": "2027 봄", "source": "이상한값"}) == ("2027 봄", "unknown")
    # 02 SeasonRequest 모양
    assert read_season({"year": 2027, "season": "SS", "raw": "내년 봄여름", "origin": "user_requested"}) \
        == ("2027-SS", "user")
    assert read_season({"year": 2026, "season": "FW", "origin": "default_next"}) == ("2026-FW", "inferred")
    assert read_season({"year": 2026, "season": None, "origin": "default_next"}) == (None, "inferred")


@pytest.mark.parametrize("season", [None, "", {"value": None, "source": "inferred"}, {"year": 2027, "origin": "user_requested"}])
def test_missing_season_is_error(season):
    resp = run({**REQ, "season": season}, con=None)
    assert resp["status"] == "error" and "쿼리 전처리" in resp["errors"][0]["message"]


def test_missing_season_message_shows_received_fields():
    resp = run({"request_id": "r", "Season": "2027-SS", "target_category": "상의"}, con=None)
    msg = resp["errors"][0]["message"]
    assert "요청에 없는 필드: season" in msg
    assert "받은 필드: Season, request_id, target_category" in msg      # 필드 이름이 달라 못 받은 경우가 보임
    assert "applied_filters.season" in msg and "필드 이름이 season인지" in msg


def test_empty_season_message():
    msg = run({**REQ, "season": ""}, con=None)["errors"][0]["message"]
    assert "값이 비어 있는 필드: season=''" in msg
    msg = run({**REQ, "season": {"value": None, "source": "inferred"}}, con=None)["errors"][0]["message"]
    assert "시즌 값(year·season 또는 value)이 비어 있습니다" in msg


# ---- 응답 조립 (DB 없이)
def _row(**kw):
    base = {"article_id": "0999992001", "kf_area": "하의", "kf_category": "청바지", "kf_color": "스카이블루",
            "materials": ["데님", "울/캐시미어"], "fit": "스키니", "stock_qty": 3200, "rank": 1,
            "stock_value_krw": 20_200_000, "sell_through": 0.3412, "season_share": 1.0,
            "recent_sold": 0, "prev_sold": 0, "trend": -1.0, "score": 0.9123, "trend_basis": "too_few_sales"}
    base.update(kw)
    return next(pd.DataFrame([base]).itertuples(index=False))


TI = TrendInput("style_012", "발레코어", ["라이트핑크", "화이트"], "user_category_items", ["블라우스"], ["블라우스"])


def test_build_product_has_product_info_only():
    p = build_product(_row(kf_category="티셔츠·캐주얼상의", materials=["울·캐시미어"]), map_season("2027-SS"))
    assert set(p) == {"style_id", "sku_ids", "category", "season", "attributes", "inventory", "image"}
    assert p["style_id"] == "0999992" and p["sku_ids"] == ["0999992001"] and p["season"] == "2027SS"
    assert p["category"] == "티셔츠" and p["attributes"]["material"] == ["울/캐시미어"]   # 예전 DB 이름도 새 이름으로
    assert p["inventory"]["data_nature"] == "sample" and p["inventory"]["quantity"] == 3200


def test_build_reason_fields_and_detail():
    rs = build_reason(_row(), resolve_pair_areas("상의", None), "상의", TI, "SS(봄·여름)")
    assert "text" not in rs
    assert rs["pairing"] == {"basis": "user", "target_categories": ["상의"], "trend_categories": [],
                             "product_area": "하의"}
    # 스카이블루는 핑크·화이트 둘 다와 어울림
    assert rs["color"] == {"product_color": "스카이블루", "matched_trend_colors": ["라이트핑크", "화이트"],
                           "tone_on_tone": [], "trend_color_basis": "user_category_items",
                           "rule": "조합표 v1, 트렌드 색과 어울리는 색 중 가장 많이 겹치는 색 + 트렌드 색 자체(톤온톤)"}
    assert rs["sell_through"] == 0.341 and rs["score"] == 0.912
    assert rs["data_nature"]["sell_through"] == "sample" and rs["data_nature"]["season_share"] == "fact"
    assert reason_detail(rs) == ("라이트핑크·화이트와 어울리는 스카이블루. 재고 금액 2,020만 원(가상), 판매율 34%(가상), "
                                 "최근 8주 판매 없음(판매가 적어 추세는 순위에 반영 안 함), "
                                 "판매의 100%가 SS(봄·여름) 기간.")


def test_reason_detail_tone_on_tone():
    # 상품 색이 기준 색과 같으면 톤온톤으로 적는다
    rs = build_reason(_row(kf_color="화이트"), resolve_pair_areas("상의", None), "상의", TI, "SS(봄·여름)")
    assert rs["color"]["tone_on_tone"] == ["화이트"]
    detail = reason_detail(rs)
    assert "화이트와 같은 색(톤온톤)" in detail


def test_reason_detail_recent_sales_and_peer():
    rs = build_reason(_row(kf_color="블랙", recent_sold=3, prev_sold=12, trend=-0.4, trend_basis="peer_relative",
                           season_share=None),
                      resolve_pair_areas("상의", None), "상의",
                      TrendInput("s", None, [], "none", [], []), "SS(봄·여름)")
    assert rs["color"]["matched_trend_colors"] == [] and rs["color"]["rule"] == "색 조건 없음"
    assert reason_detail(rs).endswith("최근 4주 3개 / 직전 4주 12개 판매, 같은 영역 평균보다 빠르게 감소, 판매 이력 없음.")


def test_reason_when_trend_columns_missing():
    row = pd.DataFrame([{k: v for k, v in _row()._asdict().items() if k not in ("recent_sold", "prev_sold")}])
    rs = build_reason(next(row.itertuples(index=False)), resolve_pair_areas("상의", None), "상의", TI, "SS")
    assert rs["recent_trend"]["recent_sold"] is None and "주" not in reason_detail(rs)


# ---- 전체 실행 (PC에서 duckdb로)
def test_run_ok(con):
    resp = run(REQ, con)
    tr, p = resp["trend_result"], resp["product"]
    assert resp["status"] == "ok" and tr["status"] == "ok", tr["errors"]
    assert p["sku_ids"] == ["a1"] and tr["rank"] == 1 and tr["product_index"] == 0
    assert tr["style"] == {"id": "style_012", "name": "발레코어"}
    assert resp["season_basis"]["n_periods"] == 2
    assert tr["search"]["color_attempts"][-1]["n_candidates"] == 1
    assert tr["reason"]["color"]["matched_trend_colors"] == ["라이트핑크", "화이트"]
    assert tr["search"]["trend_colors"] == ["라이트핑크", "화이트"] and tr["search"]["color_overlap"] == 2
    assert tr["summary"].startswith("2027 SS(봄·여름) 상의 기획용 짝 카테고리(") and "재고 금액" in tr["summary"]
    assert any("sample" in x for x in tr["limitations"])
    assert [e["evidence_id"] for e in resp["evidence"]][:4] == [f"stock-agent:E-00{i}" for i in range(1, 5)]
    assert validate_output(resp) == []


def test_run_no_results(con):
    resp = run({**REQ, "segment": "Sport"}, con)
    assert resp["status"] == "no_results" and resp["product"] is None
    assert resp["trend_result"]["status"] == "no_results" and resp["trend_result"]["rank"] is None
    assert validate_output(resp) == []


def test_run_partial_when_trend_fails(con, monkeypatch):
    import stock_agent.agent as agent

    def boom(*a, **k):
        raise RuntimeError("timeout")
    monkeypatch.setattr(agent, "get_sales_trend", boom)
    resp = run(REQ, con)
    assert resp["status"] == "partial" and resp["product"] is not None
    assert resp["trend_result"]["errors"][0]["code"] == "trend_failed"


def test_no_trend_colors_searches_all_colors(con):
    resp = run({**REQ, "trend": trend({**BLOUSE, "colors": []})}, con)
    tr = resp["trend_result"]
    assert resp["status"] == "ok"
    # 색 조건이 없으니 레드 스커트(a2)도 후보
    assert tr["search"]["color_attempts"][-1]["n_candidates"] == 2
    assert tr["search"]["trend_color_basis"] == "none" and tr["search"]["colors"] and len(tr["search"]["colors"]) == 21
    assert any("색 조건 없이" in x for x in tr["limitations"]) and "색 조건 없이" in tr["summary"]


def test_colors_used_within_pair_categories(con):
    # 색이 있으면 짝 카테고리 안에서 조합색으로만 매치: 레드 스커트(a2)는 빠짐
    resp = run(REQ, con)
    tr = resp["trend_result"]
    assert tr["search"]["color_attempts"][-1]["n_candidates"] == 1 and resp["product"]["attributes"]["color"] == "스카이블루"


def test_user_category_item_without_color_uses_all_items(con):
    items = [{**BLOUSE, "colors": []}, {"category_major": "하의", "category_minor": "스커트", "colors": ["화이트"]}]
    tr = run({**REQ, "trend": trend(*items)}, con)["trend_result"]
    assert tr["search"]["trend_colors"] == ["화이트"] and tr["search"]["trend_color_basis"] == "all_items"
    assert any("사용자 카테고리(상의) 아이템에 색이 없어" in x for x in tr["limitations"])


def test_trend_categories_used_when_no_user_category(con):
    items = [{"category_major": "상의", "colors": []}, {"category_major": "아우터", "colors": []}]
    resp = run({**REQ, "target_category": None, "trend": trend(*items)}, con)
    tr = resp["trend_result"]
    assert resp["status"] == "ok" and tr["reason"]["pairing"]["product_area"] == "하의"
    assert tr["reason"]["pairing"]["basis"] == "trend" and tr["reason"]["pairing"]["trend_categories"] == ["상의", "아우터"]
    assert "트렌드 카테고리(상의, 아우터)의 짝 카테고리(하의)" in tr["summary"]


def test_user_category_wins_over_trend_categories(con):
    items = [BLOUSE, {"category_major": "원피스", "category_minor": "드레스", "colors": ["화이트"]}]
    tr = run({**REQ, "trend": trend(*items)}, con)["trend_result"]
    assert tr["reason"]["pairing"]["basis"] == "user"
    assert not any("트렌드 카테고리" in x for x in tr["limitations"])


def test_missing_category_searches_all_areas(con):
    resp = run({**REQ, "target_category": None, "trend": trend({"colors": ["라이트핑크", "화이트"]})}, con)
    tr = resp["trend_result"]
    assert resp["status"] == "ok"
    # 상의(a5)도 후보: 짝 카테고리 대신 전체 영역
    assert tr["search"]["areas"] == ["상의", "하의", "아우터", "원피스"] and tr["search"]["color_attempts"][-1]["n_candidates"] == 2
    assert any("카테고리 입력이 없어" in x for x in tr["limitations"]) and "카테고리 미지정" in tr["summary"]


def test_season_from_02_shape(con):
    resp = run({**REQ, "season": {"year": 2026, "season": "SS", "origin": "default_next"}}, con)
    sb = resp["season_basis"]
    assert resp["status"] == "ok" and sb["season"] == "2026 SS(봄·여름)" and sb["source"] == "inferred"
    assert any("쿼리 전처리 단계가 정한 값" in x for x in resp["trend_result"]["limitations"])
    resp = run({**REQ, "season": {"year": 2027, "season": "SS", "origin": "user_requested"}}, con)
    assert resp["season_basis"]["source"] == "user"


def test_inferred_season_value_shape(con):
    sb = run({**REQ, "season": {"value": "2026 여름", "source": "inferred"}}, con)["season_basis"]
    assert sb["season"] == "2026 여름" and sb["source"] == "inferred"
    assert sb["ranges"][0] == {"start": "2019-06-01", "end": "2019-08-31"}


def test_all_four_areas_still_picks_one(con):
    items = [{"category_major": a, "colors": []} for a in ("상의", "하의", "아우터", "원피스")]
    resp = run({**REQ, "target_category": None, "trend": trend(*items)}, con)
    tr = resp["trend_result"]
    assert resp["status"] == "ok" and tr["rank"] == 1
    assert any("공통 짝이 없어" in x for x in tr["limitations"])


# ---- 사용자 색(02 target_colors)
def test_read_user_colors():
    from stock_agent.agent import read_user_colors
    assert read_user_colors(None) == ([], [])
    assert read_user_colors("화이트") == (["화이트"], [])
    assert read_user_colors(["화이트", " 화이트", "", None]) == (["화이트"], [])
    assert read_user_colors(["화이트", "흰색"]) == (["화이트"], ["흰색"])


def test_invalid_user_color_message_lists_21_colors():
    resp = run({**REQ, "target_colors": ["아이보리"]}, con=None)
    msg = resp["errors"][0]["message"]
    assert "'아이보리'" in msg and "화이트" in msg and "21색" in msg


def test_user_color_in_trend_used_and_missing_one_noted(con):
    # 사용자 화이트·블랙, 트렌드(블라우스)에는 화이트만 → 화이트만 기준 색, 블랙은 limitations
    tr = run({**REQ, "target_colors": ["화이트", "블랙"]}, con)["trend_result"]
    assert tr["search"]["trend_colors"] == ["화이트"] and tr["search"]["trend_color_basis"] == "user_colors_in_trend"
    assert tr["search"]["user_colors"] == ["화이트", "블랙"] and tr["search"]["user_colors_excluded"] == ["블랙"]
    assert any("사용자 색 중 블랙" in x for x in tr["limitations"])
    assert tr["reason"]["color"]["rule"] == "조합표 v1, 사용자 색과 어울리는 색 중 가장 많이 겹치는 색 + 사용자 색 자체(톤온톤)"
    assert "사용자 색(화이트)과 어울리는 색 중 가장 많이 겹치는 색" in tr["summary"]
    assert tr["search"]["color_attempts"][-1]["n_candidates"] == 1     # 화이트의 조합색 스카이블루(a1)만, 레드는 아님


def test_user_color_not_in_trend_still_used(con):
    # 사용자 블랙은 트렌드 아이템에 없음 → 그래도 블랙 기준 (블랙의 조합색 레드 스커트가 후보)
    resp = run({**REQ, "target_colors": ["블랙"]}, con)
    tr = resp["trend_result"]
    assert resp["status"] == "ok"
    assert tr["search"]["trend_colors"] == ["블랙"] and tr["search"]["trend_color_basis"] == "user_colors"
    assert "레드" in tr["search"]["colors"] and resp["product"]["attributes"]["color"] == "레드"
    assert any("사용자 색(블랙)이 이 트렌드 아이템 색에 없습니다" in x and "라이트핑크, 화이트" in x
               for x in tr["limitations"])
    assert validate_output(resp) == []


def test_no_user_colors_keeps_trend_colors(con):
    tr = run({**REQ, "target_colors": []}, con)["trend_result"]
    assert tr["search"]["trend_color_basis"] == "user_category_items" and tr["search"]["user_colors"] == []
    assert tr["reason"]["color"]["rule"] == "조합표 v1, 트렌드 색과 어울리는 색 중 가장 많이 겹치는 색 + 트렌드 색 자체(톤온톤)"


# ---- 재고 후보가 0개일 때 트렌드 색 완화 (사용자 색·카테고리는 완화하지 않음)
NEON = {"category_major": "상의", "category_minor": "탑", "colors": ["네온"]}               # 어울리는 색 재고 없음
BLACK_GRAY = {"category_major": "상의", "category_minor": "셔츠", "colors": ["블랙", "그레이"]}  # 하나라도 → 레드 a2


def test_no_relax_when_candidates_found(con):
    s = run(REQ, con)["trend_result"]["search"]
    assert s["color_mode"] == "max_overlap" and s["relaxed"] == []
    assert [a["mode"] for a in s["color_attempts"]] == ["max_overlap"]


def test_relax_to_any_matching_color(con):
    resp = run({**REQ, "trend": trend(BLACK_GRAY)}, con)
    tr = resp["trend_result"]
    assert resp["status"] == "ok" and resp["product"]["sku_ids"] == ["a2"]          # 레드 스커트
    s = tr["search"]
    assert [(a["mode"], a["n_candidates"]) for a in s["color_attempts"]] == [("max_overlap", 0), ("any", 1)]
    assert s["color_mode"] == "any" and "레드" in s["colors"]
    assert s["relaxed"] == [{"condition": "color_rule", "from": "max_overlap", "to": "any"}]
    assert tr["reason"]["color"]["rule"].startswith("조합표 v1, 트렌드 색과 하나라도 어울리는 색")
    assert "하나라도 어울리는 색" in tr["summary"]
    assert any("트렌드 색(블랙, 그레이) 조건을 완화했습니다: 가장 많이 겹치는 색 → 하나라도 어울리는 색" in x
               for x in tr["limitations"])
    assert not any("모두와 어울리는 색이 없어" in x for x in tr["limitations"])
    assert validate_output(resp) == []


def test_relax_to_all_colors_skips_identical_any(con):
    # 네온 1색: 하나라도 어울리는 색 = 가장 많이 겹치는 색이라 any 단계는 건너뛴다
    resp = run({**REQ, "trend": trend(NEON)}, con)
    tr = resp["trend_result"]
    assert resp["status"] == "ok"
    s = tr["search"]
    assert [a["mode"] for a in s["color_attempts"]] == ["max_overlap", "none"]
    assert s["color_mode"] == "none" and len(s["colors"]) == 21
    assert s["relaxed"] == [{"condition": "trend_colors", "from": ["네온"], "to": []}]
    assert tr["reason"]["color"]["rule"] == "색 조건 없음" and tr["reason"]["color"]["matched_trend_colors"] == []
    assert "트렌드 색 조건을 풀어 색 조건 없이" in tr["summary"]
    assert validate_output(resp) == []


def test_user_colors_are_not_relaxed(con):
    resp = run({**REQ, "target_colors": ["네온"], "trend": trend(NEON)}, con)
    s = resp["trend_result"]["search"]
    assert resp["status"] == "no_results" and s["trend_color_basis"] == "user_colors_in_trend"
    assert [a["mode"] for a in s["color_attempts"]] == ["max_overlap"] and s["relaxed"] == []


def test_relax_still_empty_is_no_results(con):
    resp = run({**REQ, "segment": "Sport", "trend": trend(BLACK_GRAY)}, con)       # Sport 재고 없음
    s = resp["trend_result"]["search"]
    assert resp["status"] == "no_results"
    assert [a["n_candidates"] for a in s["color_attempts"]] == [0, 0, 0]
    assert any("완화한 뒤에도 후보가 없습니다" in x for x in resp["trend_result"]["limitations"])


def test_user_category_is_not_relaxed(con):
    # 원피스의 짝은 아우터뿐이고 SS 아우터 재고(a3 패딩은 가을·겨울)가 없다: 색은 풀어도 영역은 넓히지 않는다
    resp = run({**REQ, "target_category": "원피스", "trend": trend({**NEON, "category_major": "원피스"})}, con)
    s = resp["trend_result"]["search"]
    assert resp["status"] == "no_results" and s["areas"] == ["아우터"]
    assert [(a["mode"], a["n_candidates"]) for a in s["color_attempts"]] == [("max_overlap", 0), ("none", 0)]


# ---- 무신사 스냅 착샷 → CLIP 유사도 필터 (착샷이 있으면 항상)
SNAP = {"product_id": "musinsa_snap:1", "image_url": "https://img.example/snap1.jpg", "kind": "snap_garment",
        "source": "musinsa_snap", "score": 0.8, "category_minor": "블라우스", "materials": ["시폰"]}
CATALOG = {"product_id": "otpensource:9", "image_url": "https://img.example/cat.jpg", "kind": "catalog_item",
           "source": "otpensource", "score": 0.95}
SNAP_BLOUSE = {**BLOUSE, "id": "item_1", "similar_products": [CATALOG, SNAP]}
NO_COLOR_BLOUSE = {**SNAP_BLOUSE, "colors": []}        # 색 조건 없음 → 하의 a1·a2 둘 다 후보


class FakeClip:
    """clip_filter 대역: keep에 있는 상품만 남긴다 (None이면 skip_reason으로 건너뜀)."""

    def __init__(self, keep=None, skip_reason=None, image_used=True, fallback=False):
        self.keep, self.skip_reason, self.image_used, self.fallback = keep, skip_reason, image_used, fallback
        self.calls = []

    def __call__(self, cand, ref):
        from stock_agent.clip_matcher import ClipInfo
        self.calls.append((list(cand["article_id"]), ref))
        n = len(cand)
        if self.skip_reason:
            return cand, ClipInfo(False, self.skip_reason, n_before=n, n_after=n)
        out = cand[cand["article_id"].isin(self.keep)].reset_index(drop=True)
        return out, ClipInfo(True, None, self.image_used, n, len(out), n, 0.6, self.fallback)


def test_snap_applies_clip_and_records_it(con):
    clip = FakeClip(keep=["a2"])
    resp = run({**REQ, "trend": trend(NO_COLOR_BLOUSE)}, con, clip_filter=clip)
    tr = resp["trend_result"]
    assert resp["status"] == "ok" and resp["product"]["sku_ids"] == ["a2"]     # CLIP이 a1을 걸러냄
    assert sorted(clip.calls[0][0]) == ["a1", "a2"] and clip.calls[0][1]["image_url"] == SNAP["image_url"]
    s = tr["search"]
    assert s["clip"] == {"applied": True, "skip_reason": None, "image_used": True, "n_before": 2, "n_after": 1,
                         "n_indexed": 2, "threshold": 0.6, "fallback": False}
    o = s["outfit_reference"]
    assert o["status"] == "used" and o["product_id"] == "musinsa_snap:1" and o["score"] == 0.8   # 스냅 중 최고
    assert any("시각·스타일 유사도(CLIP)로 재고 후보 2개 중 1개를 남겼습니다" in x for x in tr["limitations"])
    ev = {e["evidence_id"]: e for e in resp["evidence"]}
    assert ev["stock-agent:E-006"]["locator"] == SNAP["image_url"] and ev["stock-agent:E-006"]["source_id"] == "musinsa_snap"
    assert validate_output(resp) == []


def test_clip_fallback_and_text_only_notes(con):
    resp = run({**REQ, "trend": trend(NO_COLOR_BLOUSE)}, con, clip_filter=FakeClip(["a1", "a2"], image_used=False,
                                                                                  fallback=True))
    lim = resp["trend_result"]["limitations"]
    assert any("유사도 0.60 이상인 후보가 없어" in x for x in lim)
    assert any("텍스트 속성만으로 유사도를 계산했습니다" in x for x in lim)


def test_no_snap_skips_clip(con):
    clip = FakeClip(keep=[])
    resp = run({**REQ, "trend": trend({**BLOUSE, "similar_products": [CATALOG]})}, con, clip_filter=clip)
    s = resp["trend_result"]["search"]
    assert resp["status"] == "ok" and clip.calls == [] and s["clip"] is None
    assert s["outfit_reference"]["status"] == "skipped" and s["outfit_reference"]["skip_reason"] == "no_snap"
    assert "stock-agent:E-006" not in {e["evidence_id"] for e in resp["evidence"]}
    assert any("무신사 스냅 착샷이 없어" in x for x in resp["trend_result"]["limitations"])


@pytest.mark.parametrize("why, note", [("no_index", "CLIP 인덱스가 없어"), ("no_match", "CLIP 인덱스에 없어")])
def test_clip_skipped_is_recorded_honestly(con, why, note):
    resp = run({**REQ, "trend": trend(SNAP_BLOUSE)}, con, clip_filter=FakeClip(skip_reason=why))
    s = resp["trend_result"]["search"]
    assert resp["status"] == "ok" and s["clip"]["applied"] is False and s["clip"]["skip_reason"] == why
    assert s["outfit_reference"]["status"] == "skipped" and s["outfit_reference"]["skip_reason"] == why
    assert "stock-agent:E-006" not in {e["evidence_id"] for e in resp["evidence"]}
    lim = resp["trend_result"]["limitations"]
    assert any(note in x for x in lim) and not any("남겼습니다" in x for x in lim)


def test_clip_runs_after_color_relaxation(con):
    clip = FakeClip(keep=["a2"])
    resp = run({**REQ, "trend": trend({**BLACK_GRAY, "similar_products": [SNAP]})}, con, clip_filter=clip)
    assert clip.calls[0][0] == ["a2"] and resp["trend_result"]["search"]["color_mode"] == "any"


def test_snap_without_user_category_uses_all_items(con):
    skirt = {"category_major": "하의", "category_minor": "스커트", "colors": ["화이트"],
             "similar_products": [{**SNAP, "product_id": "musinsa_snap:skirt", "score": 0.9}]}
    clip = FakeClip(keep=["a1", "a2", "a5"])
    req = {k: v for k, v in REQ.items() if k != "target_category"}
    resp = run({**req, "trend": trend(SNAP_BLOUSE, skirt)}, con, clip_filter=clip)
    assert resp["trend_result"]["search"]["outfit_reference"]["product_id"] == "musinsa_snap:skirt"
