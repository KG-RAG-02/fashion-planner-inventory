import pandas as pd
import pytest

from stock_agent.agent import (
    TemplateSummarizer,
    allowed_numbers,
    build_products,
    numbers_in,
    run,
    safe_summary,
    validate_response,
)
from stock_agent.season import map_season

FACTS = {
    "season_label": "2027 SS(봄·여름)", "target": "상의", "areas": ["하의", "아우터"],
    "trend_colors": ["라이트핑크", "화이트"], "n_candidates": 5199, "n_items": 50,
    "total_stock_qty": 11928, "top_colors": ["베이지", "블랙", "화이트"],
    "top1_rank": 1, "top1_category": "팬츠", "top1_color": "베이지", "top1_stock_qty": 149,
}


# ---- summary
def test_template_summary_numbers_come_from_facts():
    text = TemplateSummarizer().summarize(FACTS)
    assert "5,199개" in text and "149개" in text and "1위 팬츠(베이지)" in text
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
            return f"과잉재고 {facts['n_items']}개, 합계 {facts['total_stock_qty']:,}개."
    text, warn = safe_summary(Good(), FACTS)
    assert text == "과잉재고 50개, 합계 11,928개." and warn == []


# ---- 입력 검증 (DB 없이)
@pytest.mark.parametrize("req, code", [
    ({"request_id": "r1", "target_category": "상의"}, "missing_fields"),     # 시즌 없음
    ({"request_id": "r1", "season": "2027-SS"}, "missing_fields"),          # 색도 카테고리도 없음
    ({"request_id": "r1", "season": "2027-SS", "trend_categories": ["가방"]}, "invalid_input"),
    ({"request_id": "r1", "season": "2027", "target_category": "상의", "trend_colors": ["핑크"]}, "invalid_input"),
    ({"request_id": "r1", "season": "2027-SS", "target_category": "신발", "trend_colors": ["핑크"]}, "invalid_input"),
    ({"request_id": "r1", "season": "2027-SS", "target_category": "상의", "trend_colors": ["무지개"]}, "invalid_input"),
    ({"schema_version": "9.9", "request_id": "r1", "season": "2027-SS", "target_category": "상의",
      "trend_colors": ["핑크"]}, "schema_version_mismatch"),
])
def test_invalid_requests_return_error(req, code):
    resp = run(req, con=None)
    assert resp["status"] == "error"
    assert resp["errors"][0]["code"] == code
    assert resp["products"] == []
    assert validate_response(resp) == []


# ---- 응답 조립
def test_build_products_contract_fields():
    top = pd.DataFrame({
        "article_id": ["0999992001"], "kf_area": ["하의"], "kf_category": ["청바지"], "kf_color": ["스카이블루"],
        "materials": [["데님", "면"]], "fit": ["스키니"], "stock_qty": [3200], "rank": [1],
        "stock_value_krw": [20_200_000], "sell_through": [0.3412], "season_share": [1.0],
        "recent_sold": [0], "prev_sold": [0], "trend": [-1.0], "score": [0.9123],
    })
    [p] = build_products(top, map_season("2027-SS"), ["라이트핑크", "화이트"], "상의")
    assert p["style_id"] == "0999992" and p["sku_ids"] == ["0999992001"]
    assert p["season"] == "2027SS"
    assert p["inventory"]["data_nature"] == "sample" and p["inventory"]["quantity"] == 3200
    assert p["matched_trend_color"] == "라이트핑크"     # 핑크의 조합 색에 스카이블루가 있음
    rs = p["reason"]
    assert rs["pairing"] == {"basis": "user", "target_category": "상의", "trend_categories": [],
                             "product_area": "하의"}
    assert rs["sell_through"] == 0.341 and rs["score"] == 0.912
    assert rs["data_nature"]["sell_through"] == "sample" and rs["data_nature"]["season_share"] == "fact"
    assert rs["text"] == ("상의 기획용 짝 카테고리(하의), 라이트핑크와 어울리는 스카이블루. "
                          "재고 금액 2,020만 원(가상), 판매율 34%(가상), 최근 8주 판매 없음, "
                          "판매의 100%가 SS(봄·여름) 기간.")


def _reason_row(**kw):
    base = {"article_id": ["a"], "kf_area": ["아우터"], "kf_category": ["코트"], "kf_color": ["블랙"],
            "materials": [[]], "fit": [None], "stock_qty": [10], "rank": [1],
            "stock_value_krw": [17_620_000], "sell_through": [0.38], "season_share": [0.97],
            "recent_sold": [3], "prev_sold": [12], "trend": [-0.75], "score": [0.8]}
    base.update({k: [v] for k, v in kw.items()})
    return pd.DataFrame(base)


def test_reason_text_with_recent_sales():
    [p] = build_products(_reason_row(), map_season("2027-SS"), ["화이트"], "상의")
    assert p["reason"]["text"] == ("상의 기획용 짝 카테고리(아우터), 화이트와 어울리는 블랙. "
                                   "재고 금액 1,762만 원(가상), 판매율 38%(가상), "
                                   "최근 4주 3개 / 직전 4주 12개 판매, 판매의 97%가 SS(봄·여름) 기간.")


def test_reason_text_no_category_and_never_sold():
    [p] = build_products(_reason_row(season_share=None, recent_sold=0, prev_sold=0),
                         map_season("2027-SS"), ["화이트"], None)
    t = p["reason"]["text"]
    assert t.startswith("전체 영역 검색(아우터)") and t.endswith("판매 이력 없음.")
    assert p["reason"]["season_share"] is None


def test_reason_when_trend_columns_missing():
    top = _reason_row().drop(columns=["recent_sold", "prev_sold"])
    [p] = build_products(top, map_season("2027-SS"), ["화이트"], "상의")
    assert p["reason"]["recent_trend"]["recent_sold"] is None
    assert "주" not in p["reason"]["text"]              # 추세 문장은 빠짐


# ---- 전체 실행 (PC에서 duckdb로)
REQ = {"schema_version": "1.0", "request_id": "req_test", "season": "2027-SS",
       "target_category": "상의", "trend_colors": ["라이트핑크", "화이트"], "segment": "Ladieswear"}


def test_run_ok(con):
    resp = run(REQ, con)
    assert resp["status"] == "ok", resp["errors"]
    assert [p["sku_ids"][0] for p in resp["products"]] == ["a1"]
    assert resp["season_basis"]["n_periods"] == 2
    assert resp["attribute_summary"]["n_items"] == 1
    rs = resp["products"][0]["reason"]
    assert rs["pairing"]["target_category"] == "상의" and rs["recent_trend"]["recent_sold"] is not None
    assert rs["text"].startswith("상의 기획용 짝 카테고리(")
    assert any("sample" in x for x in resp["limitations"])
    assert validate_response(resp) == []


def test_run_no_results(con):
    resp = run({**REQ, "segment": "Sport"}, con)
    assert resp["status"] == "no_results"
    assert resp["products"] == [] and validate_response(resp) == []


def test_run_partial_when_trend_fails(con, monkeypatch):
    import stock_agent.agent as agent

    def boom(*a, **k):
        raise RuntimeError("timeout")
    monkeypatch.setattr(agent, "get_sales_trend", boom)
    resp = run(REQ, con)
    assert resp["status"] == "partial"
    assert resp["errors"][0]["code"] == "trend_failed"
    assert len(resp["products"]) == 1


# ---- 시즌·카테고리 입력 처리 (2026-11-05 결정: 시즌은 02가 정해서 줌)
from stock_agent.agent import read_season  # noqa: E402


def test_read_season():
    assert read_season("2027-SS") == ("2027-SS", "unknown")
    assert read_season({"value": "2026 겨울", "source": "inferred"}) == ("2026 겨울", "inferred")
    assert read_season({"value": "2027 봄", "source": "user"}) == ("2027 봄", "user")
    assert read_season({"value": "2027 봄", "source": "이상한값"}) == ("2027 봄", "unknown")


@pytest.mark.parametrize("season", [None, "", {"value": None, "source": "inferred"}])
def test_missing_season_is_error(season):
    req = {**REQ, "season": season}
    resp = run(req, con=None)
    assert resp["status"] == "error" and "쿼리 전처리" in resp["errors"][0]["message"]


def test_no_colors_and_no_category_is_error():
    resp = run({"request_id": "r", "season": "2027-SS", "trend_colors": [], "trend_categories": []}, con=None)
    assert resp["status"] == "error" and resp["errors"][0]["code"] == "missing_fields"
    assert "카테고리와 trend_colors가 모두 없습니다" in resp["errors"][0]["message"]


def test_colors_always_used_within_pair_categories(con):
    # 색이 있으면 짝 카테고리 안에서 조합색으로만 매치: 레드 스커트(a2)는 빠짐
    resp = run(REQ, con)
    assert resp["attribute_summary"]["n_items"] == 1 and set(resp["attribute_summary"]["color"]) == {"스카이블루"}


# ---- 2026-11-06 팀 합의: 03이 색을 21색으로 정제, 색이 없을 땐 트렌드 카테고리를 줌
def test_no_trend_colors_searches_all_colors(con):
    req = {k: v for k, v in REQ.items() if k != "trend_colors"}
    resp = run(req, con)
    assert resp["status"] == "ok"
    # 색 조건이 없으니 레드 스커트(a2)도 후보. 응답은 1위 1개, 요약은 후보 전체
    assert len(resp["products"]) == 1
    assert resp["attribute_summary"]["n_items"] == 2 and set(resp["attribute_summary"]["color"]) == {"스카이블루", "레드"}
    assert all(p["matched_trend_color"] is None for p in resp["products"])
    assert any("색 조건 없이" in x for x in resp["limitations"])
    assert "색 조건 없이" in resp["summary"]
    assert validate_response(resp) == []


def test_trend_categories_used_when_no_user_category(con):
    req = {k: v for k, v in REQ.items() if k not in ("target_category", "trend_colors")}
    resp = run({**req, "trend_categories": ["상의", "아우터"]}, con)
    assert resp["status"] == "ok"
    assert {p["reason"]["pairing"]["product_area"] for p in resp["products"]} == {"하의"}
    rs = resp["products"][0]["reason"]
    assert rs["pairing"]["basis"] == "trend" and rs["pairing"]["trend_categories"] == ["상의", "아우터"]
    assert rs["text"].startswith("트렌드 카테고리(상의, 아우터)의 짝 카테고리(하의)")
    assert "트렌드 카테고리(상의, 아우터)의 짝 카테고리(하의)" in resp["summary"]


def test_user_category_wins_over_trend_categories(con):
    resp = run({**REQ, "trend_categories": ["하의", "원피스"]}, con)
    assert resp["products"][0]["reason"]["pairing"]["basis"] == "user"
    assert not any("트렌드 카테고리" in x for x in resp["limitations"])


def test_missing_category_searches_all_areas(con):
    req = {k: v for k, v in REQ.items() if k != "target_category"}
    resp = run(req, con)
    assert resp["status"] == "ok"
    # 상의(a5)도 후보에 들어옴: 짝 카테고리 대신 전체 영역. 응답은 1개
    assert len(resp["products"]) == 1
    assert set(resp["attribute_summary"]["area"]) == {"하의", "상의"}
    assert any("카테고리 입력이 없어" in x for x in resp["limitations"])
    assert "카테고리 미지정" in resp["summary"]


def test_inferred_season_from_preprocessing_is_passed_through(con):
    req = {**REQ, "season": {"value": "2026 여름", "source": "inferred"}}
    resp = run(req, con)
    assert resp["status"] == "ok"
    sb = resp["season_basis"]
    assert sb["season"] == "2026 여름" and sb["source"] == "inferred"
    assert sb["ranges"][0] == {"start": "2019-06-01", "end": "2019-08-31"}
    assert any("쿼리 전처리 단계가 추정한 값" in x for x in resp["limitations"])


def test_user_season_marked_as_user(con):
    req = {**REQ, "season": {"value": "2027-SS", "source": "user"}}
    assert run(req, con)["season_basis"]["source"] == "user"


def test_output_is_single_top1(con):
    resp = run({**REQ, "trend_colors": [], "target_category": None, "trend_categories": ["상의", "하의", "아우터", "원피스"]}, con)
    # 네 영역 전부 → 오류 없이 전체 후보에서 1위 1개
    assert resp["status"] == "ok" and len(resp["products"]) == 1
    assert resp["products"][0]["rank"] == 1
    assert resp["attribute_summary"]["scope"].startswith("상위 ")
    assert any("공통 짝이 없어" in x for x in resp["limitations"])


def test_reason_text_peer_relative_and_too_few():
    from stock_agent.agent import reason_text
    base = {"pairing": {"basis": "user", "target_category": "상의", "trend_categories": [], "product_area": "하의"},
            "color": {"product_color": "베이지", "matched_trend_color": "화이트"},
            "stock_value_krw": None, "sell_through": None, "season_share": None, "season_name": "SS(봄·여름)"}
    t = lambda **k: {"weeks": 4, "recent_sold": 3, "prev_sold": 12, **k}  # noqa: E731
    assert "같은 영역 평균보다 빠르게 감소" in reason_text({**base, "recent_trend": t(basis="peer_relative", value=-0.4)})
    assert "같은 영역 평균과 비슷한 흐름" in reason_text({**base, "recent_trend": t(basis="peer_relative", value=0.02)})
    txt = reason_text({**base, "recent_trend": t(basis="too_few_sales", value=0.0, recent_sold=0, prev_sold=0)})
    assert "최근 8주 판매 없음(판매가 적어 추세는 순위에 반영 안 함)" in txt



# ---- 필수 값이 없을 때: 원인을 단정하지 않고 받은 필드를 보여 준다
def test_missing_season_message_shows_received_fields():
    resp = run({"request_id": "r", "Season": "2027-SS", "target_category": "상의"}, con=None)
    msg = resp["errors"][0]["message"]
    assert "요청에 없는 필드: season" in msg
    assert "받은 필드: Season, request_id, target_category" in msg      # 필드 이름이 달라 못 받은 경우가 보임
    assert "요청을 만드는 쪽" in msg and "필드 이름이 season인지" in msg


def test_empty_season_message():
    msg = run({"request_id": "r", "season": "", "trend_colors": ["화이트"]}, con=None)["errors"][0]["message"]
    assert "값이 비어 있는 필드: season=''" in msg
    msg = run({"request_id": "r", "season": {"value": None, "source": "inferred"}, "trend_colors": ["화이트"]},
              con=None)["errors"][0]["message"]
    assert "value가 비어 있습니다" in msg
