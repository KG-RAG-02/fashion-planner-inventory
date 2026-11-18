"""CLIP 기준 이미지 고르기 — 무신사 스냅 착샷, 없으면 무신사 상품 이미지

trend 결과에서 트렌드 아이템의 similar_products 중 1장을 골라, clip_matcher.clip_filter가 재고 후보와
소재·질감·디테일 유사도를 비교하는 기준(쿼리)으로 쓴다. 기준 이미지가 있으면 CLIP 필터를 항상 건다.

기준 이미지 고르기 (pick_reference)
    찾는 범위: 사용자 카테고리 아이템. 사용자 카테고리가 없으면 트렌드 아이템 전체.
    1. kind=snap_garment(스냅 착샷) → score가 가장 높은 1장
       trend 착샷은 image_url이 없고 image_ref(hf://datasets/disl/musinsa-snap-new@<rev>#row=<n>)만 있다.
       이 참조를 R2에 미리 올린 착샷 JPEG로 읽는다(snap_image, R2_SNAP_IMAGE_PREFIX).
       multi_wearer_suspected=true(여러 명이 찍혔을 수 있는 착장 사진)는 다른 사람 옷이 섞이므로 뺀다.
    2. 착샷이 없으면 kind=catalog_item(판매 상품 이미지) → score 최고 1장
    공통: has_image=false는 뺀다. 이미지 참조(image_url, 또는 http image_ref, 또는 착샷 image_ref)가 있어야 한다.
       상품 이미지는 트렌드 아이템 카테고리(예: 블라우스)라 재고의 짝 카테고리(하의)와 다르지만,
       CLIP은 흑백 이미지와 소재·디테일 텍스트만 비교하므로 카테고리와 무관하게 무드를 본다.
    착샷과 상품 이미지는 score 범위가 달라 서로 섞어 비교하지 않는다.
    CLIP 텍스트에 쓰도록 trend 속성 materials·details를 함께 담는다.
"""

from __future__ import annotations

from fashion_planner_inventory.snap_image import parse_image_ref
from fashion_planner_inventory.trend_input import item_area

SNAP_KIND = "snap_garment"
CATALOG_KIND = "catalog_item"
KINDS = (SNAP_KIND, CATALOG_KIND)          # 우선순위 순
REFERENCE_KEYS = ("kind", "product_id", "image_url", "image_ref", "score", "source", "item_id", "item_category")


def _score(p: dict) -> float:
    s = p.get("score")
    return float(s) if isinstance(s, (int, float)) and not isinstance(s, bool) else 0.0


def _text(v) -> str | None:
    return v.strip() if isinstance(v, str) and v.strip() else None


def image_source(p: dict) -> tuple[str | None, str | None]:
    """상품 → (열 URL, 착샷 image_ref). URL은 image_url, 없으면 http(s) image_ref(trend 상품 이미지는 둘이 같다)."""
    url, ref = _text(p.get("image_url")), _text(p.get("image_ref"))
    if url is None and ref and ref.startswith(("http://", "https://")):
        url = ref
    return url, (ref if ref and parse_image_ref(ref) else None)


def _usable(p: dict, kind: str) -> bool:
    if not isinstance(p, dict) or p.get("kind") != kind or p.get("has_image") is False:
        return False
    if kind == SNAP_KIND and p.get("multi_wearer_suspected") is True:
        return False
    url, ref = image_source(p)
    return url is not None or (kind == SNAP_KIND and ref is not None)


def pick_reference(trend: dict, target_areas) -> dict | None:
    """사용자 카테고리 아이템(없으면 아이템 전체)에서 착샷 → 상품 이미지 순으로 score 최고 1장. 없으면 None."""
    items = [it for it in (trend.get("items") or []) if isinstance(it, dict)]
    targets = set(target_areas or [])
    if targets:
        items = [it for it in items if item_area(it) in targets]
    for kind in KINDS:
        best = None
        for it in items:
            for p in it.get("similar_products") or []:
                if _usable(p, kind) and (best is None or _score(p) > best["score"]):
                    url, ref = image_source(p)
                    best = {"kind": kind, "product_id": p.get("product_id"), "image_url": url,
                            "image_ref": ref if kind == SNAP_KIND else None,
                            "score": _score(p), "source": p.get("source"), "item_id": it.get("id"),
                            "item_name": it.get("name"),
                            "item_category": it.get("category_minor") or it.get("category_major"),
                            "materials": p.get("materials"), "details": p.get("details")}
        if best is not None:
            return best
    return None


def reference(ref: dict | None, clip: dict | None) -> dict:
    """응답 search.outfit_reference: 고른 기준 이미지와 CLIP 기준으로 썼는지.

    status=used: CLIP 필터를 걸었음. skipped: 기준 이미지 없음(no_reference) 또는 CLIP을 걸지 못함(clip.skip_reason)
    """
    if ref is None:
        return {"status": "skipped", "skip_reason": "no_reference", **{k: None for k in REFERENCE_KEYS}}
    used = bool(clip and clip.get("applied"))
    return {"status": "used" if used else "skipped", "skip_reason": None if used else (clip or {}).get("skip_reason"),
            **{k: ref.get(k) for k in REFERENCE_KEYS}}
