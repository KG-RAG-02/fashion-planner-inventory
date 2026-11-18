import pytest

from fashion_planner_inventory.attributes import (
    enrich_row,
    extract_fit,
    extract_materials,
    map_category,
    map_color,
    season_fit,
)


@pytest.mark.parametrize(
    "segment, ptype, name, desc, expected",
    [
        ("Ladieswear", "Blouse", "", "", ("상의", "블라우스")),
        ("Ladieswear", "Trousers", "Jade HW Skinny Denim", "5-pocket jeans in washed denim", ("하의", "청바지")),
        ("Menswear", "Trousers", "Jogger", "Joggers in sweatshirt fabric", ("하의", "조거팬츠")),
        ("Menswear", "Trousers", "Chino", "Chinos in cotton twill", ("하의", "팬츠")),
        ("Divided", "Jacket", "", "Padded jacket with a hood", ("아우터", "패딩")),
        ("Divided", "Jacket", "", "Bomber jacket in satin", ("아우터", "점퍼")),
        ("Ladieswear", "Jacket", "", "Jacket in woven fabric", ("아우터", "재킷")),
        ("Ladieswear", "Blazer", "", "", ("아우터", "재킷")),
        ("Ladieswear", "Cardigan", "", "", ("아우터", "가디건")),
        ("Menswear", "Hoodie", "", "Hooded jacket with a zip down the front", ("아우터", "짚업")),
        ("Menswear", "Hoodie", "", "Top in sweatshirt fabric with a lined hood", ("상의", "후드티")),
        ("Divided", "Sweater", "", "Top in sweatshirt fabric", ("상의", "티셔츠")),
        ("Ladieswear", "Sweater", "", "Jumper in a soft knit", ("상의", "니트웨어")),
        ("Ladieswear", "Dress", "", "", ("원피스", "드레스")),
        ("Ladieswear", "Bikini top", "", "", ("수영복", "수영복")),
        ("Baby/Children", "Garment Set", "", "", ("[확장]", "세트")),
        ("Baby/Children", "Bodysuit", "", "", ("[확장]", "바디수트")),
        ("Ladieswear", "Bodysuit", "", "", ("상의", "탑")),
        ("Sport", "Bra", "", "", ("상의", "브라탑")),
    ],
)
def test_map_category(segment, ptype, name, desc, expected):
    assert map_category(segment, ptype, name, desc) == expected


@pytest.mark.parametrize("ptype", ["Bra", "Sneakers", "Bag", "Socks", "Pyjama set", "Earring", "Unknown", "Costumes"])
def test_non_garments_are_excluded(ptype):
    assert map_category("Ladieswear", ptype) is None


def test_map_color():
    assert map_color("Light Pink") == "핑크"
    assert map_color("Off White") == "화이트"
    assert map_color("Light Blue") == "스카이블루"
    assert map_color("Dark Blue") == "네이비"
    assert map_color("Greenish Khaki") == "카키"
    assert map_color("Other") is None
    assert map_color(None) is None


def test_materials_basic():
    assert extract_materials("Jumper in a soft knit containing some wool") == ["니트", "울/캐시미어"]
    assert extract_materials("5-pocket jeans in washed denim made from cotton blend") == ["면", "데님"]
    assert extract_materials(None) == []


def test_materials_false_positives():
    assert "레이스" not in extract_materials("Boots with lace-up front")
    assert "레이스" in extract_materials("Top in lace")
    mats = extract_materials("Biker jacket in imitation leather")
    assert "가죽" not in mats and "합성섬유" in mats
    assert "스판덱스" not in extract_materials("Leggings in stretch jersey")


def test_fit_uses_area_vocabulary():
    assert extract_fit("Wide trousers with a high waist", "하의") == "와이드"
    assert extract_fit("Skinny-fit jeans", "하의") == "스키니"
    assert extract_fit("Oversized T-shirt", "상의") == "오버사이즈"
    assert extract_fit("Wide-cut top", "상의") is None   # '와이드'는 하의 어휘
    assert extract_fit("Relaxed fit", None) is None


@pytest.mark.parametrize(
    "area, cat, mats, ptype, expected",
    [
        ("수영복", "수영복", [], "Swimsuit", ["여름"]),
        ("하의", "팬츠", ["면"], "Shorts", ["여름"]),
        ("아우터", "패딩", [], "Jacket", ["가을", "겨울"]),
        ("상의", "니트웨어", ["울/캐시미어"], "Sweater", ["가을", "겨울"]),
        ("상의", "블라우스", ["시폰"], "Blouse", ["봄", "여름"]),
        ("하의", "청바지", ["데님", "면"], "Trousers", ["봄", "여름", "가을", "겨울"]),
        (None, None, [], "Sneakers", []),
    ],
)
def test_season_fit(area, cat, mats, ptype, expected):
    assert season_fit(area, cat, mats, ptype) == expected


def test_enrich_row():
    r = enrich_row("Ladieswear", "Trousers", "Wide denim", "Wide jeans in cotton denim", "Light Blue")
    assert r == {
        "kf_area": "하의",
        "kf_category": "청바지",
        "kf_color": "스카이블루",
        "materials": ["면", "데님"],
        "fit": "와이드",
        "season_fit": ["봄", "여름", "가을", "겨울"],
    }


def test_missing_values_from_pandas():
    nan = float("nan")
    assert extract_materials(nan) == []
    assert extract_fit(nan, "상의") is None
    assert map_color(nan) is None
    assert map_category("Ladieswear", "Dress", nan, nan) == ("원피스", "드레스")
    assert enrich_row("Ladieswear", "Blouse", nan, nan, nan)["kf_area"] == "상의"


# 표본 50개 확인에서 나온 수정 (2026-11-05)
def test_sample_review_fixes():
    # 모조 스웨이드 패치는 스웨이드가 아님
    mats = extract_materials("Jacket in woven fabric. Imitation suede elbow patches")
    assert "스웨이드" not in mats and "합성섬유" in mats
    assert "스웨이드" in extract_materials("Short suede skirt")
    # 저지 소재 Sweater는 티셔츠, 니트면 니트웨어 유지
    assert map_category("Ladieswear", "Sweater", "", "Top in supersoft jersey") == ("상의", "티셔츠")
    assert map_category("Ladieswear", "Sweater", "", "Jumper in a knit with jersey trims") == ("상의", "니트웨어")
    # Jacket 타입이지만 트렌치코트
    assert map_category("Divided", "Jacket", "", "Trenchcoat in an airy weave") == ("아우터", "코트")
    # 스포츠 브라렛
    assert map_category("Sport", "Top", "", "Fully lined sports bralette") == ("상의", "브라탑")
    # slim legs → 스키니
    assert extract_fit("5-pocket trousers with slim legs", "하의") == "스키니"
    # airy weave → 봄·여름
    assert season_fit("원피스", "드레스", [], "Dress", "Long dress in an airy weave") == ["봄", "여름"]


# 상위 50개 확인에서 나온 수정 (2026-11-05)
def test_top50_review_fixes():
    assert map_category("Ladieswear", "Shorts", "&DENIM Embrace denim shorts", "Denim shorts") == ("하의", "청바지")
    assert map_category("Ladieswear", "Shorts", "Henry Shorts", "Shorts in a cotton weave") == ("하의", "팬츠")
    assert map_category("Ladieswear", "Trousers", "&DENIM Skirt pencil", "Pencil skirt in denim") == ("하의", "스커트")


def test_mohair_is_hair_knit_and_warm():
    assert extract_materials("Jumper in a fluffy knit containing some mohair") == ["니트", "헤어니트"]


def test_old_names_map_to_vocabulary():
    from fashion_planner_inventory.attributes import to_vocab
    assert to_vocab("티셔츠·캐주얼상의") == "티셔츠" and to_vocab("울·캐시미어") == "울/캐시미어"
    assert to_vocab("청바지") == "청바지" and to_vocab(None) is None
