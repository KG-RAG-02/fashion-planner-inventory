"""outfit: 무신사 스냅 착샷 고르기·분석 답 검사·재고 단계 (DB 없이)"""

import pandas as pd
import pytest

from stock_agent import outfit as O


def snap(pid, score, kind="snap_garment", url="https://img.example/x.jpg"):
    return {"product_id": pid, "kind": kind, "score": score, "image_url": url, "source": "musinsa_snap"}


TOP = {"id": "i1", "category_major": "상의", "category_minor": "블라우스",
       "similar_products": [snap("s1", 0.6), snap("c1", 0.99, kind="catalog_item"), snap("s2", 0.7)]}
SKIRT = {"id": "i2", "category_major": "하의", "category_minor": "스커트", "similar_products": [snap("s3", 0.9)]}
TREND = {"style": {"id": "t"}, "items": [TOP, SKIRT]}


# ---------------------------------------------------------------- 착샷 고르기
def test_pick_snap_from_user_category_items():
    p = O.pick_snap(TREND, ["상의"])
    assert p["product_id"] == "s2" and p["score"] == 0.7 and p["item_id"] == "i1" and p["item_category"] == "블라우스"


def test_pick_snap_without_user_category_uses_all_items():
    assert O.pick_snap(TREND, [])["product_id"] == "s3"


@pytest.mark.parametrize("products", [
    [snap("c1", 0.9, kind="catalog_item")],               # 판매 상품만
    [snap("s1", 0.9, url="")],                            # URL 없음
    [],
])
def test_pick_snap_none(products):
    assert O.pick_snap({"items": [{**TOP, "similar_products": products}]}, ["상의"]) is None


def test_pick_snap_user_category_without_items():
    assert O.pick_snap(TREND, ["아우터"]) is None


# ---------------------------------------------------------------- 분석 답 검사
def raw(bottom, outer=None, full=True):
    return {"full_outfit": full,
            "garments": {"하의": bottom, "아우터": outer or {"visible": False}, "상의": {"visible": True}}}


def test_parse_outfit_keeps_vocab_and_maps_slim():
    o = O.parse_outfit(raw({"visible": True, "materials": ["데님", "새틴", "스판덱스"], "fit": "슬림"},
                           {"visible": True, "materials": ["가죽"], "fit": "슬림"}), {"product_id": "s"})
    assert o.used
    assert o.garments["하의"] == {"visible": True, "materials": ["데님", "스판덱스"], "fit": "스키니"}
    assert o.garments["아우터"]["fit"] == "타이트"            # 하의가 아니면 슬림 → 타이트
    assert o.dropped == ["하의 소재 새틴"]
    assert o.garments["원피스"] == {"visible": False, "materials": [], "fit": None}


def test_parse_outfit_fit_must_match_area():
    o = O.parse_outfit(raw({"visible": True, "materials": [], "fit": "오버사이즈"}), None)   # 하의 핏이 아님
    assert o.garments["하의"]["fit"] is None and o.dropped == ["하의 핏 오버사이즈"]


def test_parse_outfit_not_full_is_skipped():
    o = O.parse_outfit(raw({"visible": True, "materials": ["데님"]}, full=False), {"product_id": "s"})
    assert o.status == "skipped" and o.skip_reason == "not_full_outfit" and o.ref == {"product_id": "s"}


@pytest.mark.parametrize("bad", [None, [], {"full_outfit": True}, {"garments": "x"}])
def test_parse_outfit_bad_shape(bad):
    with pytest.raises(ValueError):
        O.parse_outfit(bad, None)


def test_invisible_garment_attributes_ignored():
    o = O.parse_outfit(raw({"visible": False, "materials": ["데님"], "fit": "와이드"}), None)
    assert o.garments["하의"] == {"visible": False, "materials": [], "fit": None} and o.spec("하의") is None


# ---------------------------------------------------------------- 짝 영역
def used(bottom=(["데님"], "스키니"), outer=None):
    g = {"상의": {"visible": True, "materials": [], "fit": None},
         "하의": {"visible": bottom is not None, "materials": bottom[0] if bottom else [], "fit": bottom[1] if bottom else None},
         "아우터": {"visible": outer is not None, "materials": outer[0] if outer else [], "fit": outer[1] if outer else None},
         "원피스": {"visible": False, "materials": [], "fit": None}}
    return O.Outfit("used", garments=g)


def test_visible_areas():
    assert O.visible_areas(["하의", "아우터"], used()) == (["하의"], ["아우터"])
    assert O.visible_areas(["하의", "아우터"], used(outer=([], None))) == (["하의", "아우터"], [])
    assert O.visible_areas(["아우터"], used()) == (["아우터"], [])            # 보이는 짝 영역이 없으면 그대로
    assert O.visible_areas(["하의", "아우터"], O.skipped("no_snap")) == (["하의", "아우터"], [])


# ---------------------------------------------------------------- 재고 단계
@pytest.mark.parametrize("mats, cat, wanted, out", [
    (["데님"], "팬츠", ["데님"], True), (["면"], "팬츠", ["데님"], False), ([], "팬츠", ["데님"], None),
    (None, "팬츠", ["데님"], None), ([], "청바지", ["데님"], True), (["면"], "청바지", ["데님"], True),
    (["데님"], "팬츠", [], None)])
def test_material_match(mats, cat, wanted, out):
    assert O.material_match(mats, cat, wanted) is out


@pytest.mark.parametrize("fit, wanted, out", [
    ("스키니", "스키니", True), ("와이드", "스키니", False), (None, "스키니", None), (float("nan"), "스키니", None),
    ("스키니", None, None)])
def test_fit_match(fit, wanted, out):
    assert O.fit_match(fit, wanted) is out


def cand():
    return pd.DataFrame([
        {"article_id": "p1", "kf_area": "하의", "kf_category": "청바지", "materials": [], "fit": "스키니"},   # 데님(청바지)+스키니 → A
        {"article_id": "p2", "kf_area": "하의", "kf_category": "팬츠", "materials": ["데님"], "fit": "와이드"},  # 데님 → B
        {"article_id": "p3", "kf_area": "하의", "kf_category": "팬츠", "materials": ["면"], "fit": "스키니"},   # 다른 소재 → C
        {"article_id": "p4", "kf_area": "하의", "kf_category": "스커트", "materials": [], "fit": None},       # 미확인 → C
        {"article_id": "p5", "kf_area": "아우터", "kf_category": "재킷", "materials": ["데님"], "fit": None},  # 아우터 조건 없음 → C
    ])


def test_tiers_and_pool_order():
    t = O.assign_tiers(cand(), used())
    assert t["tier"].tolist() == ["A", "B", "C", "C", "C"]
    pool, tier, counts = O.pick_pool(t)
    assert tier == "A" and pool["article_id"].tolist() == ["p1"] and counts == {"A": 1, "B": 1, "C": 3}
    pool, tier, _ = O.pick_pool(t[t["tier"] != "A"])
    assert tier == "B" and pool["article_id"].tolist() == ["p2"]
    pool, tier, _ = O.pick_pool(t[t["tier"] == "C"])
    assert tier == "C" and len(pool) == 3


def test_tier_with_only_fit_or_only_material():
    only_fit = O.assign_tiers(cand(), used(bottom=([], "스키니")))["tier"].tolist()
    assert only_fit == ["A", "C", "A", "C", "C"]
    only_mat = O.assign_tiers(cand(), used(bottom=(["데님"], None)))["tier"].tolist()
    assert only_mat == ["A", "A", "C", "C", "C"]


def test_relaxed_conditions():
    o = used()
    assert O.relaxed_conditions(o, "A", ["하의"]) == []
    assert O.relaxed_conditions(o, "B", ["하의"]) == ["fit"]
    assert O.relaxed_conditions(o, "C", ["하의"]) == ["fit", "materials"]


def test_describe():
    assert O.describe(used(), ["하의", "아우터"]) == "하의 데님·스키니, 아우터 없음"
