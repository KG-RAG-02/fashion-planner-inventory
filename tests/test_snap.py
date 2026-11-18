"""snap: CLIP 기준 이미지 고르기(착샷 → 상품 이미지)·응답 기록 (DB 없이)"""

import pytest

from fashion_planner_inventory.snap import REFERENCE_KEYS, pick_reference, reference


def prod(pid, score, kind="snap_garment", url="https://img.example/x.jpg", **attrs):
    return {"product_id": pid, "kind": kind, "score": score, "image_url": url, "source": "musinsa_snap", **attrs}


TOP = {"id": "i1", "category_major": "상의", "category_minor": "블라우스",
       "similar_products": [prod("s1", 0.6), prod("c1", 0.99, kind="catalog_item"),
                            prod("s2", 0.7, materials=["시폰"], details=[{"type": "detail", "value": "러플"}])]}
SKIRT = {"id": "i2", "category_major": "하의", "category_minor": "스커트", "similar_products": [prod("s3", 0.9)]}
TREND = {"style": {"id": "t"}, "items": [TOP, SKIRT]}


def test_snap_first_from_user_category_items():
    p = pick_reference(TREND, ["상의"])
    assert p["kind"] == "snap_garment" and p["product_id"] == "s2" and p["score"] == 0.7     # 상품 이미지 0.99보다 착샷 우선
    assert p["item_id"] == "i1" and p["item_category"] == "블라우스"
    assert (p["materials"], p["details"]) == (["시폰"], [{"type": "detail", "value": "러플"}])   # CLIP 텍스트


def test_without_user_category_uses_all_items():
    assert pick_reference(TREND, [])["product_id"] == "s3"


def test_catalog_image_when_no_snap():
    items = [{**TOP, "similar_products": [prod("c1", 1.1, kind="catalog_item"), prod("c2", 1.4, kind="catalog_item"),
                                          prod("c3", 2.0, kind="catalog_item", has_image=False)]}]
    p = pick_reference({"items": items}, ["상의"])
    assert p["kind"] == "catalog_item" and p["product_id"] == "c2"         # has_image=false는 뺌


@pytest.mark.parametrize("products", [
    [prod("s1", 0.9, url="")],                                            # URL 없음
    [prod("c1", 0.9, kind="catalog_item", url=None)],
    [prod("x1", 0.9, kind="other")],
    [],
])
def test_none(products):
    assert pick_reference({"items": [{**TOP, "similar_products": products}]}, ["상의"]) is None


def test_user_category_without_items():
    assert pick_reference(TREND, ["아우터"]) is None


def test_reference():
    ref = pick_reference(TREND, ["상의"])
    assert reference(None, None) == {"status": "skipped", "skip_reason": "no_reference", **{k: None for k in REFERENCE_KEYS}}
    used = reference(ref, {"applied": True, "skip_reason": None})
    assert used["status"] == "used" and used["skip_reason"] is None and used["product_id"] == "s2"
    assert used["kind"] == "snap_garment" and set(used) == {"status", "skip_reason", *REFERENCE_KEYS}
    assert reference(ref, {"applied": False, "skip_reason": "no_index"})["skip_reason"] == "no_index"


# ---- 02 착샷: image_url 없이 image_ref만 (graph_schema §2.3)
REF0 = "hf://datasets/disl/musinsa-snap-new@4335d233ef3b54bc5132a4d572d589042a41219d#row=12"


def test_snap_with_image_ref_only():
    snaps = [prod("musinsa_snap:12:상의", 0.8, url=None, image_ref=REF0, has_image=True),
             prod("c1", 1.5, kind="catalog_item")]
    p = pick_reference({"items": [{**TOP, "similar_products": snaps}]}, ["상의"])
    assert p["kind"] == "snap_garment" and p["image_url"] is None and p["image_ref"] == REF0
    assert reference(p, {"applied": True})["image_ref"] == REF0


def test_multi_wearer_snap_skipped():
    snaps = [prod("s_multi", 0.9, url=None, image_ref=REF0, multi_wearer_suspected=True),
             prod("s_one", 0.5, url=None, image_ref=REF0.replace("row=12", "row=13"))]
    p = pick_reference({"items": [{**TOP, "similar_products": snaps}]}, ["상의"])
    assert p["product_id"] == "s_one"                                    # 점수가 높아도 여러 명 착샷은 뺌
    only_multi = [snaps[0], prod("c1", 1.0, kind="catalog_item")]
    assert pick_reference({"items": [{**TOP, "similar_products": only_multi}]}, ["상의"])["kind"] == "catalog_item"


@pytest.mark.parametrize("ref", ["hf://datasets/other/data@4335d23#row=1", "row=1", "", None])
def test_snap_with_unreadable_ref_is_not_used(ref):
    snaps = [prod("s1", 0.9, url=None, image_ref=ref)]
    assert pick_reference({"items": [{**TOP, "similar_products": snaps}]}, ["상의"]) is None


def test_catalog_http_image_ref_used_as_url():
    c = [prod("c1", 1.0, kind="catalog_item", url=None, image_ref="https://image.msscdn.net/a.jpg")]
    p = pick_reference({"items": [{**TOP, "similar_products": c}]}, ["상의"])
    assert p["image_url"] == "https://image.msscdn.net/a.jpg" and p["image_ref"] is None


def test_snap_object_key():
    from fashion_planner_inventory.snap_image import parse_image_ref, snap_object_key
    assert parse_image_ref(REF0) == ("4335d233ef3b54bc5132a4d572d589042a41219d", 12)
    assert snap_object_key("musinsa-snap/", REF0) == "musinsa-snap/4335d233ef3b54bc5132a4d572d589042a41219d/12.jpg"
    assert snap_object_key("musinsa-snap", "https://x/y.jpg") is None and parse_image_ref(None) is None
