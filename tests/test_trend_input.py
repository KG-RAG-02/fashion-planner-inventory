"""trend 결과의 트렌드 1개 → 트렌드 색·카테고리 (2026-11-08 결정)"""

from fashion_planner_inventory.trend_input import item_area, read_trend


def trend(*items, sid="style_007", name="정제된 맥시멀리스트"):
    return {"style": {"id": sid, "name": name, "focus_core": ["color"]}, "items": list(items)}


SHIRT = {"category_major": "상의", "category_minor": "셔츠", "colors": ["화이트"]}
JACKET = {"category_major": "아우터", "category_minor": "재킷", "colors": ["블랙", "레드"]}
SKIRT = {"category_major": "하의", "category_minor": "스커트", "colors": ["블랙"]}


def test_user_category_items_colors_first():
    ti = read_trend(trend(SHIRT, JACKET, SKIRT), "상의")
    assert ti.colors == ["화이트"] and ti.color_basis == "user_category_items" and ti.color_items == ["셔츠"]
    assert ti.style_id == "style_007" and ti.name == "정제된 맥시멀리스트"
    assert ti.categories == ["셔츠", "재킷", "스커트"]


def test_all_item_colors_when_no_user_category():
    ti = read_trend(trend(SHIRT, JACKET, SKIRT), None)
    # 개수 제한 없음, 중복 제거, 나온 순서
    assert ti.colors == ["화이트", "블랙", "레드"] and ti.color_basis == "all_items"


def test_all_item_colors_when_user_category_items_have_no_color():
    shirt_no_color = {**SHIRT, "colors": []}
    ti = read_trend(trend(shirt_no_color, JACKET), "상의")
    assert ti.colors == ["블랙", "레드"] and ti.color_basis == "all_items" and ti.color_items == ["재킷"]


def test_all_item_colors_when_no_item_in_user_category():
    ti = read_trend(trend(JACKET, SKIRT), "원피스")
    assert ti.colors == ["블랙", "레드"] and ti.color_basis == "all_items"


def test_no_colors_anywhere():
    ti = read_trend(trend({"category_major": "아우터", "category_minor": "재킷", "colors": []}), "상의")
    assert ti.colors == [] and ti.color_basis == "none" and ti.categories == ["재킷"]


def test_category_minor_falls_back_to_major_and_area_from_minor():
    items = [{"category_major": "하의", "colors": ["네이비"]},             # minor 없음 → major
             {"category_minor": "블라우스", "colors": ["핑크"]}]            # major 없음 → minor로 영역
    ti = read_trend(trend(*items), "상의")
    assert ti.categories == ["하의", "블라우스"]
    assert ti.colors == ["핑크"] and ti.color_basis == "user_category_items"
    assert item_area({"category_minor": "블라우스"}) == "상의" and item_area({}) is None


def test_missing_style_and_items():
    ti = read_trend({}, None)
    assert ti.style_id is None and ti.colors == [] and ti.categories == [] and ti.color_basis == "none"


# ---- 사용자 색(trend target_colors)이 있으면 사용자 색이 먼저 (2026-11-08 결정)
def test_user_colors_all_in_trend():
    ti = read_trend(trend(SHIRT, JACKET, SKIRT), "상의", ["화이트", "블랙"])
    assert ti.colors == ["화이트", "블랙"] and ti.color_basis == "user_colors_in_trend"
    assert ti.user_colors == ["화이트", "블랙"] and ti.user_colors_excluded == []
    assert ti.color_items == ["셔츠", "재킷", "스커트"]


def test_user_colors_only_overlap_kept():
    ti = read_trend(trend(SHIRT, JACKET), "상의", ["화이트", "그린"])
    assert ti.colors == ["화이트"] and ti.color_basis == "user_colors_in_trend"
    assert ti.user_colors_excluded == ["그린"] and ti.color_items == ["셔츠"]


def test_user_colors_checked_against_all_items_not_only_user_category():
    # 상의 아이템은 화이트뿐이지만 아우터 아이템에 블랙이 있으면 "트렌드에 있음"
    ti = read_trend(trend(SHIRT, JACKET), "상의", ["블랙"])
    assert ti.colors == ["블랙"] and ti.color_basis == "user_colors_in_trend" and ti.color_items == ["재킷"]


def test_user_colors_not_in_trend_used_as_is():
    ti = read_trend(trend(SHIRT, JACKET), "상의", ["그린"])
    assert ti.colors == ["그린"] and ti.color_basis == "user_colors"
    assert ti.item_colors == ["화이트", "블랙", "레드"] and ti.color_items == []


def test_user_colors_when_trend_has_no_colors():
    ti = read_trend(trend({"category_major": "아우터", "colors": []}), "상의", ["화이트"])
    assert ti.colors == ["화이트"] and ti.color_basis == "user_colors" and ti.item_colors == []


def test_user_color_matches_trend_color_written_differently():
    ti = read_trend(trend({"category_major": "상의", "colors": ["라이트핑크"]}), "상의", ["핑크"])
    assert ti.colors == ["핑크"] and ti.color_basis == "user_colors_in_trend"


def test_empty_user_colors_keeps_trend_rule():
    ti = read_trend(trend(SHIRT, JACKET), "상의", [])
    assert ti.colors == ["화이트"] and ti.color_basis == "user_category_items" and ti.user_colors == []


# ---- 아이템 자신의 소재·핏은 짝 재고 검색 조건이 아니다 (README 2026-11-09).
def test_item_materials_and_fit_do_not_change_pair_search():
    original = read_trend(trend(SHIRT, JACKET), "상의")
    changed = read_trend(trend({**SHIRT, "materials": ["시폰"], "fit": "루즈"},
                               {**JACKET, "material": ["울/캐시미어"], "fit": ["오버사이즈"]}), "상의")
    assert changed == original


def test_multi_target_keeps_color_rule_without_item_material_filter():
    ti = read_trend(trend(SHIRT, JACKET), ["상의", "아우터"])
    assert ti.colors == ["화이트", "블랙", "레드"] and ti.color_basis == "user_category_items"
