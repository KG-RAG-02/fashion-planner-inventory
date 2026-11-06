from datetime import date

import pandas as pd
import pytest

from stock_agent.overstock import (
    COLOR_PAIRS,
    KF_COLORS,
    get_inventory_status,
    get_matching_categories,
    get_matching_colors,
    get_sales_trend,
    normalize_color,
    rank_overstock,
    summarize_attributes,
    trend_ratio,
)
from stock_agent.season import map_season


# ---- 1. 짝 카테고리
def test_matching_categories():
    assert get_matching_categories("상의") == ("상의", ["하의", "아우터"])
    assert get_matching_categories("블라우스") == ("상의", ["하의", "아우터"])
    assert get_matching_categories("청바지") == ("하의", ["상의", "아우터"])
    assert get_matching_categories("원피스") == ("원피스", ["아우터"])
    assert get_matching_categories("티셔츠") == ("상의", ["하의", "아우터"])   # K-Fashion 이름 그대로 와도 됨
    with pytest.raises(ValueError):
        get_matching_categories("신발")


@pytest.mark.parametrize("target, trend, areas, basis, fallback", [
    ("하의", ["하의", "상의"], ["상의", "아우터"], "user", False),       # 사용자 카테고리 우선
    (None, ["상의", "아우터"], ["하의"], "trend", False),                 # 교집합
    (None, ["재킷", "셔츠"], ["하의"], "trend", False),                   # 세부 카테고리도 영역으로
    (None, ["상의", "하의"], ["아우터"], "trend", False),
    (None, ["상의"], ["하의", "아우터"], "trend", False),
    (None, ["상의", "하의", "아우터"], ["상의", "하의", "아우터", "원피스"], "trend", True),  # 공통 짝 없음 → 전부
    (None, ["상의", "하의", "아우터", "원피스"], ["상의", "하의", "아우터", "원피스"], "trend", True),
    (None, [], ["상의", "하의", "아우터", "원피스"], "all", False),
])
def test_resolve_pair_areas(target, trend, areas, basis, fallback):
    from stock_agent.overstock import resolve_pair_areas
    am = resolve_pair_areas(target, trend)
    assert (am.areas, am.basis, am.fallback) == (areas, basis, fallback)


# ---- 2. 색
def test_color_table_uses_kfashion_vocabulary():
    assert set(COLOR_PAIRS) == set(KF_COLORS)
    for pairs in COLOR_PAIRS.values():
        assert set(pairs) <= set(KF_COLORS)


@pytest.mark.parametrize("text, expected", [
    ("핑크", "핑크"), ("라이트핑크", "핑크"), ("라이트 핑크", "핑크"), ("light pink", "핑크"),
    ("라이트블루", "스카이블루"), ("아이보리", "화이트"), ("버건디", "와인"), ("다크그레이", "그레이"),
    ("무지개", None),
])
def test_normalize_color(text, expected):
    assert normalize_color(text) == expected


def test_matching_colors_architecture_example():
    m = get_matching_colors(["라이트핑크", "화이트"])
    colors, unknown = m.colors, m.unknown
    assert unknown == [] and m.threshold == 1 and not m.fallback
    # 아키텍처 예시: 라이트블루(스카이블루)·그레이·베이지가 들어 있어야 함
    assert {"스카이블루", "그레이", "베이지"} <= set(colors)
    assert len(colors) == len(set(colors))


def test_matching_colors_reports_unknown():
    m = get_matching_colors(["무지개", "블랙"])
    assert m.unknown == ["무지개"] and m.colors == COLOR_PAIRS["블랙"]


def test_matching_colors_majority_for_three():
    m = get_matching_colors(["화이트", "핑크", "그레이"])
    assert m.threshold == 2 and not m.fallback
    assert set(m.colors) == {"네이비", "스카이블루", "베이지", "그레이", "블랙", "화이트"}
    assert m.counts["네이비"] == 3 and m.counts["라벤더"] == 1
    assert "화이트" in m.colors                         # 톤온톤 허용


def test_matching_colors_two_is_union():
    m = get_matching_colors(["핑크", "화이트"])
    assert m.threshold == 1
    assert set(m.colors) == set(COLOR_PAIRS["핑크"]) | set(COLOR_PAIRS["화이트"])


def test_matching_colors_falls_back_to_union(monkeypatch):
    # 지금 조합표로는 3~6색 어떤 조합도 다수결 결과가 남아서, 가짜 표로 확인
    import stock_agent.overstock as ov
    fake = {k: [] for k in ov.KF_COLORS}
    fake.update({"레드": ["블랙"], "그린": ["베이지"], "퍼플": ["그레이"]})
    monkeypatch.setattr(ov, "COLOR_PAIRS", fake)
    m = ov.get_matching_colors(["레드", "그린", "퍼플"])
    assert m.fallback and m.threshold == 2
    assert m.colors == ["블랙", "베이지", "그레이"]


def test_duplicate_trend_colors_count_once():
    m = get_matching_colors(["라이트핑크", "핑크"])   # 둘 다 핑크
    assert m.threshold == 1 and m.colors == COLOR_PAIRS["핑크"]


# ---- 4. 추세
@pytest.mark.parametrize("recent, prev, expected", [
    (50, 100, -0.5), (100, 100, 0.0), (300, 100, 1.0), (5, 0, 1.0), (0, 0, -1.0), (0, 10, -1.0),
])
def test_trend_ratio(recent, prev, expected):
    assert trend_ratio(recent, prev) == expected


# ---- 5. 순위
def _cand():
    return pd.DataFrame({
        "article_id": ["A", "B", "C", "D"],
        "stock_value_krw": [9_000_000, 1_000_000, 9_000_000, 5_000_000],
        "sell_through": [0.35, 0.90, 0.35, 0.60],
        "trend": [-0.8, 0.5, -0.8, 0.0],
        "season_share": [0.7, 0.2, None, 0.5],
    })


def test_rank_overstock_order():
    top = rank_overstock(_cand(), n=3)
    assert list(top["rank"]) == [1, 2, 3]
    assert top.iloc[0]["article_id"] == "A"          # 재고 많고 안 팔리고 하락, 시즌 비중 높음
    assert "B" not in set(top["article_id"])         # 잘 팔리는 상품은 밀림
    assert top["score"].is_monotonic_decreasing


def test_rank_overstock_is_deterministic_and_handles_empty():
    pd.testing.assert_frame_equal(rank_overstock(_cand()), rank_overstock(_cand().iloc[::-1]))
    assert rank_overstock(_cand().iloc[0:0]).empty


def test_rank_weights_change_order():
    only_stock = {"stock_value": 1, "low_sell_through": 0, "declining_trend": 0, "season_share": 0}
    top = rank_overstock(_cand(), n=4, weights=only_stock)
    assert list(top["article_id"][:2]) == ["A", "C"]  # 동점은 article_id 순


# ---- 6. 요약
def test_summarize_attributes():
    top = pd.DataFrame({
        "kf_area": ["하의", "하의", "아우터"],
        "kf_category": ["청바지", "스커트", "재킷"],
        "kf_color": ["스카이블루", "그레이", "베이지"],
        "materials": [["데님", "면"], [], ["면"]],
        "fit": ["와이드", None, None],
        "stock_qty": [100, 50, 30],
    })
    s = summarize_attributes(top)
    assert s["n_items"] == 3 and s["total_stock_qty"] == 180
    assert s["material"] == {"면": 2, "데님": 1}
    assert s["fit"] == {"정보 없음": 2, "와이드": 1}
    assert s["area"] == {"하의": 2, "아우터": 1}


# ---- 3·4 SQL: PC(duckdb 있음)에서만 실행


def test_get_inventory_status_filters(con):
    df = get_inventory_status(con, ["하의", "아우터"], ["스카이블루", "그레이", "베이지"],
                              map_season("2027-SS"), segment="Ladieswear")
    # a2 레드(색 불일치), a3 가을·겨울(시즌), a4 Menswear, a5 상의(영역), a6 재고 0 → 제외
    assert list(df.article_id) == ["a1"]
    # a1 판매 5건 중 SS(2019·2020 3~8월) 안은 2019-04-01, 2019-04-02, 2020-08-01의 3건
    assert df.loc[0, "season_sold"] == 3
    assert df.loc[0, "season_share"] == pytest.approx(3 / 5)


def test_get_sales_trend(con):
    df = get_sales_trend(con, ["a1", "a4", "a5"], weeks=4).set_index("article_id")
    assert df.loc["a1", "recent_sold"] == 1 and df.loc["a1", "prev_sold"] == 1   # 9/20 vs 8/1
    assert df.loc["a4", "recent_sold"] == 0 and df.loc["a4", "prev_sold"] == 1
    assert df.loc["a5", "trend"] == -1.0


# ---- 또래 대비 추세 + 판매 적으면 중립
def test_peer_relative_trend():
    import pandas as pd
    from stock_agent.overstock import peer_relative_trend
    df = pd.DataFrame({
        "article_id": ["x1", "x2", "x3", "x4", "y1"],
        "kf_area":    ["하의", "하의", "하의", "하의", "아우터"],
        "recent_sold": [30, 10, 0, 1, 2],
        "prev_sold":   [100, 10, 0, 2, 1],
    })
    out = peer_relative_trend(df).set_index("article_id")
    # 하의 또래: 최근 41 / 직전 112 → 다 같이 약 63% 감소
    assert out.loc["x1", "peer_trend"] < -0.5
    # x1은 70% 감소지만 또래도 63% 감소 → 또래 대비로는 거의 0
    assert abs(out.loc["x1", "trend"]) < 0.25 and out.loc["x1", "trend_raw"] == -0.7
    # x2는 그대로인데 또래가 줄었으니 상대적으로 좋음(+)
    assert out.loc["x2", "trend"] > 0.5
    # 0·0, 1·2는 판매가 적어 중립: 예전 규칙이면 -1 이었음
    assert out.loc["x3", "trend"] == 0 and out.loc["x3", "trend_raw"] == -1
    assert out.loc["x3", "trend_basis"] == "too_few_sales" and out.loc["x4", "trend"] == 0
    assert out.loc["x1", "trend_basis"] == "peer_relative"
    # 아우터는 묶음 판매가 30개 미만 → 후보 전체를 또래로 씀 (y1도 판매 적어 중립)
    assert out.loc["y1", "peer_trend"] == out.loc["x1", "peer_trend"] or out.loc["y1", "trend"] == 0
