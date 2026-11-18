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

style.focus_core·focus_supporting은 속성 이름만 있고 값이 없어서 쓰지 않는다.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from fashion_planner_inventory.overstock import CATEGORY_TO_AREA, MATCHING_AREAS, normalize_color

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


def _as_list(v) -> list:
    if v is None:
        return []
    return [v] if isinstance(v, str) else list(v) if isinstance(v, (list, tuple)) else [v]


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



def read_trend(trend: dict, target_area, user_colors: list[str] | None = None) -> TrendInput:
    """02 트렌드 1개 → TrendInput. target_area는 사용자 카테고리의 영역 1개 또는 목록(없으면 None),
    user_colors는 02 사용자 색(21색 이름, agent.check_common이 검사함)."""
    style = trend.get("style") or {}
    items = [it for it in _as_list(trend.get("items")) if isinstance(it, dict)]
    targets = set(_as_list(target_area))

    categories = _unique(it.get("category_minor") or it.get("category_major") for it in items)

    own = [it for it in items if targets and item_area(it) in targets]
    own_colors = _unique(c for it in own for c in _as_list(it.get("colors")))
    if own_colors:
        colors, basis, src = own_colors, "user_category_items", own
    else:
        colors = _unique(c for it in items for c in _as_list(it.get("colors")))
        basis, src = ("all_items", items) if colors else ("none", [])
    color_items = _unique(it.get("category_minor") or it.get("category_major") for it in src
                          if _as_list(it.get("colors")))
    name, sid = style.get("name"), style.get("id")

    user = _unique(user_colors or [])
    if not user:
        return TrendInput(sid, name, colors, basis, categories, color_items)

    item_colors = _unique(c for it in items for c in _as_list(it.get("colors")))
    in_trend = {normalize_color(c) or c for c in item_colors}     # 표기가 달라도 같은 21색이면 같은 색
    hit = [c for c in user if c in in_trend]
    hit_items = lambda cs: _unique(it.get("category_minor") or it.get("category_major") for it in items  # noqa: E731
                                   if any((normalize_color(c) or c) in cs for c in _as_list(it.get("colors"))))
    if hit:
        return TrendInput(sid, name, hit, "user_colors_in_trend", categories, hit_items(hit),
                          user, [c for c in user if c not in hit], item_colors)
    return TrendInput(sid, name, user, "user_colors", categories, [], user, [], item_colors)
