"""무신사 스냅 착샷 → 함께 입은 옷의 소재·핏 → 재고 단계(A/B/C) (2026-11-09 결정)

새 상품(예: 시폰 블라우스)과 짝이 될 재고(하의·아우터)를 고를 때, 새 상품 자신의 소재가 아니라
"트렌드 아이템과 비슷한 무신사 스냅 속 모델이 그 옷과 함께 입은 하의·아우터"의 소재·핏을 기준으로 삼는다.
02는 같은 스냅 속 다른 옷의 속성을 주지 않으므로, 03 LLM이 착샷 이미지를 보고 뽑는다.

착샷 고르기 (pick_snap)
    사용자 카테고리 아이템의 similar_products 중 kind=snap_garment이고 image_url이 있는 것 → score가 가장 높은 1장.
    사용자 카테고리가 없으면 트렌드 아이템 전체에서 고른다.
    catalog_item(판매 상품)만 있으면 착샷 반영 없이 진행한다 (소재·핏 조건 없음).

착샷 분석 (OUTFIT_SYSTEM, parse_outfit)
    LLM이 영역(상의·하의·아우터·원피스)마다 보이는지·소재·핏을 JSON으로 답한다.
    소재는 vocabulary_v1 26개, 핏은 영역별 허용 값만 쓴다 (하의: 스키니·노멀·와이드·루즈·벨보텀,
    그 밖: 타이트·노멀·루즈·오버사이즈). 슬림은 하의면 스키니, 그 밖이면 타이트. 세이프는 뽑지 않는다.
    착장 전체 사진이 아니면(아이템만 잘라낸 사진) 분석을 쓰지 않는다.

짝 영역 (visible_areas)
    착샷에 보이지 않는 짝 영역은 뺀다 (예: 아우터 없음 → 하의만).
    짝 영역이 하나도 보이지 않으면 원래 짝 영역을 그대로 쓰고 소재·핏 조건은 없다.

재고 단계 (assign_tiers, pick_pool)
    A  착샷의 소재·핏이 모두 맞는 재고 (착샷에서 정해진 조건만 본다)
    B  소재가 맞는 재고 (핏을 푼 것)
    C  나머지 전체: 카테고리·색만 맞는 재고. 정보가 없는 재고(미확인)와 다른 소재는 같게 보고 과잉재고 순위로 고른다
    A가 있으면 A에서, 없으면 B, 그것도 없으면 C에서 순위를 매긴다.
    데님은 상품 소재 정보가 없어도 카테고리가 청바지면 맞는 것으로 본다.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

import pandas as pd

from stock_agent.trend_input import MATERIALS, item_area

AREAS = ("상의", "하의", "아우터", "원피스")
FIT_BOTTOM = ("스키니", "노멀", "와이드", "루즈", "벨보텀")
FIT_OTHER = ("타이트", "노멀", "루즈", "오버사이즈")
FIT_BY_AREA = {"상의": FIT_OTHER, "하의": FIT_BOTTOM, "아우터": FIT_OTHER, "원피스": FIT_OTHER}
SLIM = {"하의": "스키니"}           # 그 밖의 영역은 타이트 (vocabulary_v1 근사 매핑과 같음)
DENIM_CATEGORY = "청바지"

SKIP_REASONS = ("no_snap", "no_vision", "not_full_outfit", "image_error")
SKIP_NOTES = {
    "no_snap": "트렌드 아이템의 유사 상품에 무신사 스냅 착샷이 없어(판매 상품만 있음) 소재·핏 조건 없이 찾았습니다.",
    "no_vision": "LLM이 이미지를 볼 수 없어 착샷 소재·핏 조건 없이 찾았습니다.",
    "not_full_outfit": "착샷이 착장 전체 사진이 아니라(아이템만 보임) 소재·핏 조건 없이 찾았습니다.",
    "image_error": "착샷 이미지를 열 수 없어 소재·핏 조건 없이 찾았습니다.",
}

OUTFIT_SYSTEM = """당신은 패션 MD를 돕는 이미지 분석가입니다. 무신사 스냅 사진 속 모델이 입은 옷을 영역별로 봅니다.
반드시 JSON 객체 하나로만 답합니다. 형식:
{"full_outfit": true 또는 false,
 "garments": {"상의": {"visible": true/false, "materials": [...], "fit": 값 또는 null},
              "하의": {...}, "아우터": {...}, "원피스": {...}},
 "note": "짧은 설명"}

규칙
- full_outfit: 모델이 옷을 입은 착장 사진이고 상의와 함께 입은 하의(또는 원피스)가 보이면 true.
  옷 한 벌만 잘라낸 사진, 상품만 찍은 사진, 하반신이 안 보이는 사진이면 false.
- visible: 그 영역의 옷이 사진에 보이면 true. 아우터를 입지 않았으면 아우터는 false.
- materials: 보이는 옷의 소재. 아래 소재 어휘에서만 고릅니다. 확실하지 않으면 [].
- fit: 아래 영역별 핏 어휘에서 하나만 고릅니다. 확실하지 않으면 null. 슬림한 바지는 스키니입니다.
- 세이프(스트레이트·부츠컷·A라인 등)는 답하지 않습니다."""


def outfit_prompt() -> str:
    return json.dumps({"소재 어휘": MATERIALS,
                       "핏 어휘": {a: list(v) for a, v in FIT_BY_AREA.items()}},
                      ensure_ascii=False)


# ---------------------------------------------------------------- 착샷 고르기

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
            if not isinstance(p, dict) or p.get("kind") != "snap_garment":
                continue
            url = p.get("image_url")
            if not isinstance(url, str) or not url.strip():
                continue
            if best is None or _score(p) > best["score"]:
                best = {"product_id": p.get("product_id"), "image_url": url.strip(), "score": _score(p),
                        "source": p.get("source"), "item_id": it.get("id"), "item_name": it.get("name"),
                        "item_category": it.get("category_minor") or it.get("category_major"),
                        # CLIP 쿼리용 텍스트 필드 (ProductMatch에서)
                        "category_minor": p.get("category_minor"),
                        "fit": p.get("fit"),
                        "length_body": p.get("length_body"),
                        "patterns": p.get("patterns"),
                        "materials": p.get("materials"),
                        "details": p.get("details")}
    return best


# ---------------------------------------------------------------- 착샷 분석 결과

@dataclass
class Outfit:
    status: str                                 # used / skipped / failed(분석 중 LLM 실패, 그 트렌드는 오류)
    skip_reason: str | None = None              # SKIP_REASONS
    ref: dict | None = None                     # 고른 착샷 (pick_snap)
    garments: dict = field(default_factory=dict)   # {영역: {"visible", "materials", "fit"}}
    dropped: list[str] = field(default_factory=list)   # 어휘 밖이라 버린 값
    note: str | None = None                     # LLM 설명

    @property
    def used(self) -> bool:
        return self.status == "used"

    def spec(self, area: str) -> dict | None:
        """그 영역의 소재·핏 조건. 쓰지 않거나 보이지 않거나 조건이 없으면 None."""
        if not self.used:
            return None
        g = self.garments.get(area)
        if not g or not g["visible"] or not (g["materials"] or g["fit"]):
            return None
        return g

    def to_dict(self) -> dict:
        return {"status": self.status, "skip_reason": self.skip_reason,
                **({k: self.ref[k] for k in ("product_id", "image_url", "score", "item_id", "item_category")}
                   if self.ref else {"product_id": None, "image_url": None, "score": None,
                                     "item_id": None, "item_category": None}),
                "garments": self.garments, "dropped": self.dropped, "note": self.note,
                "attributes_source": "llm_vision" if self.used else None}


def skipped(reason: str, ref: dict | None = None) -> Outfit:
    return Outfit("skipped", reason, ref)


def _fit(v, area: str, dropped: list[str]) -> str | None:
    if v is None:
        return None
    if isinstance(v, list):
        v = v[0] if v else None
        if v is None:
            return None
    if not isinstance(v, str) or not v.strip():
        return None
    v = v.strip()
    if v == "슬림":
        v = SLIM.get(area, "타이트")
    if v not in FIT_BY_AREA[area]:
        dropped.append(f"{area} 핏 {v}")
        return None
    return v


def _materials(v, area: str, dropped: list[str]) -> list[str]:
    vals = [v] if isinstance(v, str) else v if isinstance(v, list) else []
    out = []
    for m in vals:
        if not isinstance(m, str) or not m.strip():
            continue
        m = m.strip()
        if m not in MATERIALS:
            dropped.append(f"{area} 소재 {m}")
        elif m not in out:
            out.append(m)
    return out


def parse_outfit(raw, ref: dict | None) -> Outfit:
    """LLM 답(dict) → Outfit. 형식이 크게 틀리면 ValueError (agent가 invalid_response로)."""
    if not isinstance(raw, dict) or not isinstance(raw.get("garments"), dict):
        raise ValueError(f"착샷 분석 답에 garments 객체가 없습니다: {str(raw)[:200]}")
    note = raw.get("note") if isinstance(raw.get("note"), str) else None
    if raw.get("full_outfit") is not True:
        o = skipped("not_full_outfit", ref)
        o.note = note
        return o
    dropped: list[str] = []
    garments = {}
    for area in AREAS:
        g = raw["garments"].get(area)
        g = g if isinstance(g, dict) else {}
        visible = g.get("visible") is True
        garments[area] = {"visible": visible,
                          "materials": _materials(g.get("materials"), area, dropped) if visible else [],
                          "fit": _fit(g.get("fit"), area, dropped) if visible else None}
    return Outfit("used", None, ref, garments, dropped, note)


# ---------------------------------------------------------------- 짝 영역

def visible_areas(areas: list[str], outfit: Outfit) -> tuple[list[str], list[str]]:
    """(쓸 짝 영역, 착샷에 없어 뺀 영역). 보이는 짝 영역이 없으면 원래 영역 그대로, 뺀 영역 []."""
    if not outfit.used or not areas:
        return list(areas), []
    vis = [a for a in areas if outfit.garments.get(a, {}).get("visible")]
    if not vis:
        return list(areas), []
    return vis, [a for a in areas if a not in vis]


# ---------------------------------------------------------------- 재고 단계

def material_match(materials, category, wanted: list[str]) -> bool | None:
    """상품 소재가 착샷 소재와 맞나. 착샷 소재가 없으면 None(비교 안 함), 상품 정보가 없으면 None(미확인).
    데님은 카테고리 청바지도 맞는 것으로 본다."""
    if not wanted:
        return None
    have = [m for m in (list(materials) if materials is not None else []) if isinstance(m, str)]
    if "데님" in wanted and category == DENIM_CATEGORY:
        return True
    if not have:
        return None
    return any(m in wanted for m in have)


def fit_match(fit, wanted: str | None) -> bool | None:
    if not wanted:
        return None
    if not isinstance(fit, str) or not fit:
        return None
    return fit == wanted


def tier_of(row: dict, outfit: Outfit) -> str:
    spec = outfit.spec(row.get("kf_area"))
    if spec is None:
        return "C"
    m = material_match(row.get("materials"), row.get("kf_category"), spec["materials"])
    f = fit_match(row.get("fit"), spec["fit"])
    has_m, has_f = bool(spec["materials"]), bool(spec["fit"])
    if (not has_m or m is True) and (not has_f or f is True):
        return "A"
    if has_m and m is True:
        return "B"
    return "C"


def assign_tiers(cand: pd.DataFrame, outfit: Outfit) -> pd.DataFrame:
    out = cand.copy()
    out["tier"] = [tier_of(r, outfit) for r in out.to_dict("records")] if len(out) else pd.Series(dtype=object)
    return out


def pick_pool(cand: pd.DataFrame) -> tuple[pd.DataFrame, str, dict]:
    """(순위를 매길 후보, 고른 단계, 단계별 수). A가 있으면 A, 없으면 B, 없으면 전체(C)."""
    counts = {t: int((cand["tier"] == t).sum()) for t in ("A", "B", "C")}
    for t in ("A", "B"):
        if counts[t]:
            return cand[cand["tier"] == t].reset_index(drop=True), t, counts
    return cand.reset_index(drop=True), "C", counts


def relaxed_conditions(outfit: Outfit, tier: str, areas: list[str]) -> list[str]:
    """고른 단계에서 푼 조건 (fit / materials)."""
    specs = [outfit.spec(a) for a in areas]
    has_f = any(s and s["fit"] for s in specs)
    has_m = any(s and s["materials"] for s in specs)
    out = []
    if tier in ("B", "C") and has_f:
        out.append("fit")
    if tier == "C" and has_m:
        out.append("materials")
    return out


def describe(outfit: Outfit, areas: list[str]) -> str:
    """'하의 데님·스키니, 아우터 없음' 같은 짧은 설명."""
    parts = []
    for a in areas:
        g = outfit.garments.get(a)
        if not g or not g["visible"]:
            parts.append(f"{a} 없음")
            continue
        vals = [*g["materials"], *([g["fit"]] if g["fit"] else [])]
        parts.append(f"{a} {'·'.join(vals) if vals else '소재·핏 판단 못함'}")
    return ", ".join(parts)
