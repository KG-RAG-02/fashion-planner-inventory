"""02 트렌드 1개 → 03이 찾을 트렌드 색·카테고리 (2026-11-08 결정)

02 트렌드 분석 결과(trend_analysis_result_v1)의 트렌드 하나를 그대로 받는다.
    {"style": {"id": "style_007", "name": "정제된 맥시멀리스트", ...},
     "items": [{"category_major": "상의", "category_minor": "셔츠", "colors": ["화이트"], ...}, ...]}

트렌드 카테고리
    아이템마다 category_minor, 없으면 category_major (중복 제거, 나온 순서)

트렌드 색 (새로 기획할 상품의 색이라서 사용자 카테고리 아이템을 먼저 본다)
    1. 사용자 카테고리가 있고, 그 영역 아이템에 색이 있으면 → 그 아이템들의 색 전부   (user_category_items)
    2. 아니면 → 아이템 전체의 색 전부                                              (all_items)
    3. 색이 하나도 없으면 → []  (색 조건 없이 찾음)                                (none)
    개수 제한은 없다. 아이템 안의 색 순서는 의미가 없어 쓰지 않는다.

사용자 색(02 target_colors)이 있으면 위 규칙 대신 이것을 쓴다 (사용자 쿼리가 가장 중요, 2026-11-08 결정)
    트렌드 아이템 전체의 색과 비교해서
    a. 겹치는 사용자 색이 있으면 → 겹치는 색만                                      (user_colors_in_trend)
       트렌드 아이템에 없는 사용자 색은 빼고, agent가 limitations에 적는다 (user_colors_excluded)
    b. 하나도 안 겹치면 → 사용자 색 전부. agent가 limitations에 "트렌드 아이템에 없음"을 적는다 (user_colors)
    어울리는 색은 overstock.get_matching_colors가 "트렌드 색과 어울리는 색 중 가장 많이 겹치는 색"으로 고른다.

트렌드 소재·핏 (LLM agent가 재고를 거를 때 비교 기준, 2026-11-08)
    아이템의 materials(또는 material)·fit 값을 모은다. 사용자 카테고리 아이템을 먼저 보고, 없으면 아이템 전체.
    하나도 없으면 [] → agent가 트렌드 설명을 보고 LLM이 추론한다 (search.trend_attributes_source = llm_inferred)
    vocabulary_v1에 없는 값과 02가 근사 매핑으로 표시한 값(approx: "material:플리스", "fit:타이트")은 쓰지 않고
    attr_notes에 적는다 (agent가 limitations에 넣음). 02도 근사 값은 1차 검색에 쓰지 않는다.

LLM에게 보내는 트렌드 (llm_trend)
    02 트렌드에서 similar_products(무신사 유사 상품)·image_urls·evidence_ids·request_match·점수를 빼고,
    스타일 정의·설명과 아이템 속성만 남긴다. 소재·핏은 위 규칙으로 정리한 값.

style.focus_core·focus_supporting은 속성 이름만 있고 값이 없어서 쓰지 않는다.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from stock_agent.attributes import _FIT_BOTTOM, _FIT_TOP, _MATERIALS
from stock_agent.overstock import CATEGORY_TO_AREA, MATCHING_AREAS, normalize_color

MATERIALS = [m for m, _ in _MATERIALS]                                     # vocabulary_v1 소재 26
FITS = list(dict.fromkeys(f for f, _ in _FIT_TOP + _FIT_BOTTOM))         # vocabulary_v1 핏 7

COLOR_BASES = ("user_colors_in_trend", "user_colors", "user_category_items", "all_items", "none")
USER_COLOR_BASES = ("user_colors_in_trend", "user_colors")


@dataclass(frozen=True)
class TrendInput:
    style_id: str | None
    name: str | None
    colors: list[str]                 # 트렌드 색 (중복 제거)
    color_basis: str                  # COLOR_BASES 중 하나
    categories: list[str]             # 트렌드 카테고리
    color_items: list[str] = field(default_factory=list)   # 색을 가져온 아이템의 카테고리 (설명용)
    user_colors: list[str] = field(default_factory=list)   # 02 사용자 색 (받은 그대로)
    user_colors_excluded: list[str] = field(default_factory=list)   # 트렌드 아이템에 없어 뺀 사용자 색 (a일 때)
    item_colors: list[str] = field(default_factory=list)   # 트렌드 아이템 전체의 색 (사용자 색 비교용)
    materials: list[str] = field(default_factory=list)     # 트렌드 아이템의 소재 (없으면 [])
    fits: list[str] = field(default_factory=list)          # 트렌드 아이템의 핏 (없으면 [])
    attr_notes: list[str] = field(default_factory=list)    # 쓰지 않은 소재·핏 설명 (limitations)


def _as_list(v) -> list:
    if v is None:
        return []
    return [v] if isinstance(v, str) else list(v) if isinstance(v, (list, tuple)) else [v]


def _text(v):
    """소재·핏 값: 문자열 또는 {"value": ...}."""
    if isinstance(v, dict):
        v = v.get("value")
    return v.strip() if isinstance(v, str) and v.strip() else None


def _approx(item: dict, kind: str) -> set[str]:
    """02 approx 목록에서 "material:플리스" → {"플리스"}."""
    out = set()
    for a in _as_list(item.get("approx")):
        if isinstance(a, str) and ":" in a:
            k, v = a.split(":", 1)
            if k.strip() == kind:
                out.add(v.strip())
    return out


def clean_item_attrs(item: dict) -> tuple[list[str], list[str], dict]:
    """아이템 1개 → (쓸 소재, 쓸 핏, 뺀 값 {"not_vocab": [...], "approx": [...]})."""
    dropped = {"not_vocab": [], "approx": []}
    out = {}
    for kind, raw, vocab in (("material", item.get("materials", item.get("material")), MATERIALS),
                             ("fit", item.get("fit"), FITS)):
        approx, keep = _approx(item, kind), []
        for v in map(_text, _as_list(raw)):
            if v is None:
                continue
            if v in approx:
                dropped["approx"].append(v)
            elif v not in vocab:
                dropped["not_vocab"].append(v)
            elif v not in keep:
                keep.append(v)
        out[kind] = keep
    return out["material"], out["fit"], dropped


def llm_trend(trend: dict) -> dict:
    """LLM에게 보낼 트렌드: 스타일 정의·설명 + 아이템 속성 (유사 상품·이미지·근거 ID 뺌)."""
    style = trend.get("style") if isinstance(trend.get("style"), dict) else {}
    desc = style.get("description")
    keep_style = {k: style[k] for k in ("id", "name", "seasons", "definition", "focus_core", "focus_supporting")
                  if k in style}
    if desc is not None:
        keep_style["description"] = [d.get("text") if isinstance(d, dict) else d for d in _as_list(desc)]
    items = []
    for it in _as_list(trend.get("items")):
        if not isinstance(it, dict):
            continue
        mats, fits = clean_item_attrs(it)[:2]
        one = {k: it[k] for k in ("name", "category_major", "category_minor", "colors", "patterns", "details",
                                  "shape", "length_body", "length_sleeve", "styling_raw", "gender") if k in it}
        one["materials"], one["fit"] = mats, (fits[0] if len(fits) == 1 else fits or None)
        items.append(one)
    return {"style": keep_style, "items": items}


def item_area(item: dict) -> str | None:
    """아이템의 영역(상의·하의·아우터·원피스). category_major가 없으면 category_minor로 찾는다."""
    major = item.get("category_major")
    if major in MATCHING_AREAS:
        return major
    return CATEGORY_TO_AREA.get(item.get("category_minor"))


def _unique(xs) -> list:
    out = []
    for x in xs:
        if x and x not in out:
            out.append(x)
    return out


def _attrs(items: list[dict]) -> tuple[list[str], list[str]]:
    cleaned = [clean_item_attrs(it) for it in items]
    return _unique(m for ms, _, _ in cleaned for m in ms), _unique(f for _, fs, _ in cleaned for f in fs)


def _attr_notes(items: list[dict]) -> list[str]:
    nv, ap = [], []
    for it in items:
        d = clean_item_attrs(it)[2]
        nv += d["not_vocab"]
        ap += d["approx"]
    out = []
    if nv:
        out.append(f"트렌드 아이템의 소재·핏 중 표준 어휘(vocabulary_v1)에 없는 값은 쓰지 않았습니다: {', '.join(_unique(nv))}")
    if ap:
        out.append(f"트렌드 아이템의 소재·핏 중 근사 매핑(approx) 값은 쓰지 않았습니다: {', '.join(_unique(ap))}")
    return out


def read_trend(trend: dict, target_area, user_colors: list[str] | None = None) -> TrendInput:
    """02 트렌드 1개 → TrendInput. target_area는 사용자 카테고리의 영역 1개 또는 목록(없으면 None),
    user_colors는 02 사용자 색(21색 이름, agent.check_common이 검사함)."""
    style = trend.get("style") or {}
    items = [it for it in _as_list(trend.get("items")) if isinstance(it, dict)]
    targets = set(_as_list(target_area))

    categories = _unique(it.get("category_minor") or it.get("category_major") for it in items)

    own = [it for it in items if targets and item_area(it) in targets]
    own_mats, own_fits = _attrs(own)
    all_mats, all_fits = _attrs(items)
    mats = own_mats or all_mats
    fits = own_fits or all_fits
    own_colors = _unique(c for it in own for c in _as_list(it.get("colors")))
    if own_colors:
        colors, basis, src = own_colors, "user_category_items", own
    else:
        colors = _unique(c for it in items for c in _as_list(it.get("colors")))
        basis, src = ("all_items", items) if colors else ("none", [])
    color_items = _unique(it.get("category_minor") or it.get("category_major") for it in src
                          if _as_list(it.get("colors")))
    name, sid = style.get("name"), style.get("id")
    notes = _attr_notes(items)

    user = _unique(user_colors or [])
    if not user:
        return TrendInput(sid, name, colors, basis, categories, color_items, materials=mats, fits=fits,
                          attr_notes=notes)

    item_colors = _unique(c for it in items for c in _as_list(it.get("colors")))
    in_trend = {normalize_color(c) or c for c in item_colors}     # 표기가 달라도 같은 21색이면 같은 색
    hit = [c for c in user if c in in_trend]
    hit_items = lambda cs: _unique(it.get("category_minor") or it.get("category_major") for it in items  # noqa: E731
                                   if any((normalize_color(c) or c) in cs for c in _as_list(it.get("colors"))))
    if hit:
        return TrendInput(sid, name, hit, "user_colors_in_trend", categories, hit_items(hit),
                          user, [c for c in user if c not in hit], item_colors, mats, fits, notes)
    return TrendInput(sid, name, user, "user_colors", categories, [], user, [], item_colors, mats, fits, notes)
