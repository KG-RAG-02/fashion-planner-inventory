"""snap: 무신사 스냅 착샷 고르기·응답 기록 (DB 없이)"""

import pytest

from stock_agent.snap import pick_snap, reference


def snap(pid, score, kind="snap_garment", url="https://img.example/x.jpg", **attrs):
    return {"product_id": pid, "kind": kind, "score": score, "image_url": url, "source": "musinsa_snap", **attrs}


TOP = {"id": "i1", "category_major": "상의", "category_minor": "블라우스",
       "similar_products": [snap("s1", 0.6), snap("c1", 0.99, kind="catalog_item"),
                            snap("s2", 0.7, category_minor="블라우스", fit="루즈", materials=["시폰"])]}
SKIRT = {"id": "i2", "category_major": "하의", "category_minor": "스커트", "similar_products": [snap("s3", 0.9)]}
TREND = {"style": {"id": "t"}, "items": [TOP, SKIRT]}


def test_pick_snap_from_user_category_items():
    p = pick_snap(TREND, ["상의"])
    assert p["product_id"] == "s2" and p["score"] == 0.7 and p["item_id"] == "i1" and p["item_category"] == "블라우스"
    assert (p["category_minor"], p["fit"], p["materials"]) == ("블라우스", "루즈", ["시폰"])   # CLIP 쿼리 텍스트


def test_pick_snap_without_user_category_uses_all_items():
    assert pick_snap(TREND, [])["product_id"] == "s3"


@pytest.mark.parametrize("products", [
    [snap("c1", 0.9, kind="catalog_item")],               # 판매 상품만
    [snap("s1", 0.9, url="")],                            # URL 없음
    [],
])
def test_pick_snap_none(products):
    assert pick_snap({"items": [{**TOP, "similar_products": products}]}, ["상의"]) is None


def test_pick_snap_user_category_without_items():
    assert pick_snap(TREND, ["아우터"]) is None


def test_reference():
    ref = pick_snap(TREND, ["상의"])
    assert reference(None, None) == {"status": "skipped", "skip_reason": "no_snap", "product_id": None,
                                     "image_url": None, "score": None, "source": None, "item_id": None,
                                     "item_category": None}
    used = reference(ref, {"applied": True, "skip_reason": None})
    assert used["status"] == "used" and used["skip_reason"] is None and used["product_id"] == "s2"
    assert set(used) == {"status", "skip_reason", "product_id", "image_url", "score", "source", "item_id",
                         "item_category"}
    assert reference(ref, {"applied": False, "skip_reason": "no_index"})["skip_reason"] == "no_index"
