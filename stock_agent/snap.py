"""무신사 스냅 착샷 고르기 → CLIP 유사도 필터의 기준 이미지

02 트렌드 아이템의 similar_products 중 무신사 스냅 착샷(kind=snap_garment) 1장을 골라,
clip_matcher.clip_filter가 재고 후보와 시각·스타일 유사도를 비교하는 기준(쿼리)으로 쓴다.
착샷이 있으면 CLIP 필터를 항상 건다. 착샷이 없으면(판매 상품 catalog_item만 있음) 필터 없이 찾는다.

착샷 고르기 (pick_snap)
    사용자 카테고리 아이템의 similar_products 중 kind=snap_garment이고 image_url이 있는 것 → score가 가장 높은 1장.
    사용자 카테고리가 없으면 트렌드 아이템 전체에서 고른다.
    CLIP 쿼리 텍스트에 쓰도록 착샷의 02 속성(category_minor, fit, length_body, patterns, materials, details)을 함께 담는다.
"""

from __future__ import annotations

from stock_agent.trend_input import item_area

SNAP_KIND = "snap_garment"
REFERENCE_KEYS = ("product_id", "image_url", "score", "source", "item_id", "item_category")


def _score(p: dict) -> float:
    s = p.get("score")
    return float(s) if isinstance(s, (int, float)) and not isinstance(s, bool) else 0.0


def pick_snap(trend: dict, target_areas) -> dict | None:
    """사용자 카테고리 아이템(없으면 아이템 전체)의 snap_garment 중 score 최고 1장. 없으면 None."""
    items = [it for it in (trend.get("items") or []) if isinstance(it, dict)]
    targets = set(target_areas or [])
    if targets:
        items = [it for it in items if item_area(it) in targets]
    best = None
    for it in items:
        for p in it.get("similar_products") or []:
            if not isinstance(p, dict) or p.get("kind") != SNAP_KIND:
                continue
            url = p.get("image_url")
            if not isinstance(url, str) or not url.strip():
                continue
            if best is None or _score(p) > best["score"]:
                best = {"product_id": p.get("product_id"), "image_url": url.strip(), "score": _score(p),
                        "source": p.get("source"), "item_id": it.get("id"), "item_name": it.get("name"),
                        "item_category": it.get("category_minor") or it.get("category_major"),
                        # CLIP 쿼리 텍스트 (02 ProductMatch 속성)
                        "category_minor": p.get("category_minor"),
                        "fit": p.get("fit"),
                        "length_body": p.get("length_body"),
                        "patterns": p.get("patterns"),
                        "materials": p.get("materials"),
                        "details": p.get("details")}
    return best


def reference(ref: dict | None, clip: dict | None) -> dict:
    """응답 search.outfit_reference: 고른 착샷과 CLIP 기준으로 썼는지.

    status=used: CLIP 필터를 걸었음. skipped: 착샷 없음(no_snap) 또는 CLIP을 걸지 못함(clip.skip_reason)
    """
    if ref is None:
        return {"status": "skipped", "skip_reason": "no_snap", **{k: None for k in REFERENCE_KEYS}}
    used = bool(clip and clip.get("applied"))
    return {"status": "used" if used else "skipped", "skip_reason": None if used else (clip or {}).get("skip_reason"),
            **{k: ref.get(k) for k in REFERENCE_KEYS}}
