"""상품 속성 보강

H&M articles의 값을 K-Fashion 어휘로 바꾼다.
- 카테고리: product_type_name (+ 상품명·설명 키워드) → (영역, 세부 카테고리). 이름은 trend 표준 어휘 (예: 티셔츠)
- 색: colour_group_name → K-Fashion 21색
- 소재: detail_desc 키워드 → K-Fashion 소재 26종 (trend 표준 어휘 표기, 여러 이름은 '/')
- 핏: detail_desc 키워드 → K-Fashion 핏
- 시즌 적합성: 카테고리·소재 규칙 → 입을 수 있는 계절 목록

영역(area)은 상의/하의/아우터/원피스/수영복/[확장] 중 하나다.
짝 카테고리는 상의·하의·아우터·원피스 안에서만 고른다.
의류가 아닌 상품(신발·잡화·속옷·잠옷 등)은 None을 돌려주고 과잉재고 분석에서 뺀다.
"""

from __future__ import annotations

import re

SEASONS = ("봄", "여름", "가을", "겨울")


def _s(x) -> str:
    """문자열이 아니면(None, pandas NaN 등) 빈 문자열."""
    return x if isinstance(x, str) else ""


# ---------------------------------------------------------------- 카테고리

# product_type_name → (영역, 카테고리). 키워드로 다시 나누는 타입은 _refine에서 처리.
_TYPE_MAP: dict[str, tuple[str, str]] = {
    # 상의
    "T-shirt": ("상의", "티셔츠"),
    "Polo shirt": ("상의", "티셔츠"),
    "Top": ("상의", "탑"),
    "Vest top": ("상의", "탑"),
    "Bodysuit": ("상의", "탑"),            # 아동은 _refine에서 [확장]
    "Blouse": ("상의", "블라우스"),
    "Shirt": ("상의", "셔츠"),
    "Sweater": ("상의", "니트웨어"),        # sweatshirt는 _refine
    "Hoodie": ("상의", "후드티"),           # 지퍼형은 _refine에서 아우터 짚업
    # 아우터
    "Jacket": ("아우터", "재킷"),           # 패딩·점퍼는 _refine
    "Blazer": ("아우터", "재킷"),
    "Coat": ("아우터", "코트"),             # 패딩은 _refine
    "Cardigan": ("아우터", "가디건"),
    "Outdoor Waistcoat": ("아우터", "베스트"),
    "Tailored Waistcoat": ("아우터", "베스트"),
    # 하의
    "Trousers": ("하의", "팬츠"),           # 청바지·조거는 _refine
    "Outdoor trousers": ("하의", "팬츠"),
    "Shorts": ("하의", "팬츠"),
    "Skirt": ("하의", "스커트"),
    "Leggings/Tights": ("하의", "레깅스"),
    # 원피스
    "Dress": ("원피스", "드레스"),
    "Jumpsuit/Playsuit": ("원피스", "점프수트"),
    "Dungarees": ("원피스", "점프수트"),
    # 수영복
    "Swimsuit": ("수영복", "수영복"),
    "Bikini top": ("수영복", "수영복"),
    "Swimwear bottom": ("수영복", "수영복"),
    "Swimwear set": ("수영복", "수영복"),
    "Swimwear top": ("수영복", "수영복"),
    "Sarong": ("수영복", "수영복"),
    # K-Fashion에 없는 의류 → 확장 카테고리
    "Garment Set": ("[확장]", "세트"),
    "Outdoor overall": ("[확장]", "아웃도어 오버롤"),
}

_PADDED = re.compile(r"\b(padded|puffer|down[- ]filled|quilted)\b")
_JUMPER = re.compile(r"\b(bomber|windbreaker|anorak|parka|shell jacket)\b")
_ZIP = re.compile(r"\b(zip[- ]through|full[- ]zip|zip(?:per)? (?:down|at) the front)\b")
_SWEATSHIRT = re.compile(r"\bsweatshirt")
_JERSEY = re.compile(r"\bjersey\b")
_KNIT = re.compile(r"\bknit")
_COAT = re.compile(r"\b(trench ?coat|coat)\b")
_BRALETTE = re.compile(r"\b(bralette|sports bra)\b")
_DENIM = re.compile(r"\b(denim|jeans?)\b")
_JOGGER = re.compile(r"\bjoggers?\b")


def map_category(segment: str, product_type: str, prod_name: str = "", desc: str = "") -> tuple[str, str] | None:
    """(영역, 카테고리). 의류가 아니면 None."""
    text = f"{_s(prod_name)} {_s(desc)}".lower()

    # 스포츠 브라는 겉옷으로 입으므로 상의 브라탑, 그 밖의 Bra는 속옷이라 제외
    product_type = _s(product_type)
    if product_type == "Bra":
        return ("상의", "브라탑") if segment == "Sport" else None
    # 아기 바디수트는 전신 의류(우주복)
    if product_type == "Bodysuit" and segment == "Baby/Children":
        return ("[확장]", "바디수트")

    base = _TYPE_MAP.get(product_type)
    if base is None:
        return None
    area, cat = base

    if product_type in ("Jacket", "Coat") and _PADDED.search(text):
        return ("아우터", "패딩")
    if product_type == "Jacket" and _COAT.search(text):
        return ("아우터", "코트")
    if product_type == "Jacket" and _JUMPER.search(text):
        return ("아우터", "점퍼")
    if product_type == "Hoodie" and _ZIP.search(text):
        return ("아우터", "짚업")
    if product_type == "Sweater" and (
        _SWEATSHIRT.search(text) or (_JERSEY.search(text) and not _KNIT.search(text))
    ):
        return ("상의", "티셔츠")
    if product_type == "Top" and segment == "Sport" and _BRALETTE.search(text):
        return ("상의", "브라탑")
    if product_type == "Trousers" and re.search(r"\bskirt\b", _s(prod_name).lower()):
        return ("하의", "스커트")
    if product_type == "Shorts" and _DENIM.search(text):
        return ("하의", "청바지")
    if product_type == "Trousers":
        if _DENIM.search(text):
            return ("하의", "청바지")
        if _JOGGER.search(text):
            return ("하의", "조거팬츠")
    return area, cat


# ---------------------------------------------------------------- 색

# colour_group_name → K-Fashion 21색
_COLOR_MAP: dict[str, str] = {
    "Black": "블랙",
    "White": "화이트", "Off White": "화이트", "Transparent": "화이트",
    "Grey": "그레이", "Light Grey": "그레이", "Dark Grey": "그레이",
    "Red": "레드", "Light Red": "레드", "Dark Red": "와인", "Other Red": "레드",
    "Pink": "핑크", "Light Pink": "핑크", "Dark Pink": "핑크", "Other Pink": "핑크",
    "Orange": "오렌지", "Light Orange": "오렌지", "Dark Orange": "오렌지", "Other Orange": "오렌지",
    "Beige": "베이지", "Light Beige": "베이지", "Dark Beige": "베이지", "Greyish Beige": "베이지",
    "Brown": "브라운", "Yellowish Brown": "브라운", "Mole": "브라운", "Bronze/Copper": "브라운",
    "Yellow": "옐로우", "Light Yellow": "옐로우", "Dark Yellow": "옐로우", "Other Yellow": "옐로우",
    "Green": "그린", "Light Green": "그린", "Dark Green": "그린", "Other Green": "그린",
    "Yellowish Green": "그린",
    "Greenish Khaki": "카키",
    "Turquoise": "민트", "Light Turquoise": "민트", "Dark Turquoise": "민트", "Other Turquoise": "민트",
    "Blue": "블루", "Other Blue": "블루",
    "Light Blue": "스카이블루",
    "Dark Blue": "네이비",
    "Purple": "퍼플", "Dark Purple": "퍼플", "Other Purple": "퍼플",
    "Light Purple": "라벤더", "Lilac Purple": "라벤더",
    "Gold": "골드",
    "Silver": "실버", "Metal": "실버",
}


def map_color(colour_group: str | None) -> str | None:
    """K-Fashion 컬러. 대응이 없으면(Other, Unknown 등) None."""
    return _COLOR_MAP.get(_s(colour_group))


# ---------------------------------------------------------------- 소재

# 순서대로 검사. (K-Fashion 소재, 정규식)
_MATERIALS: list[tuple[str, re.Pattern]] = [
    ("면", re.compile(r"\bcotton\b")),
    ("저지", re.compile(r"\bjersey\b")),
    ("니트", re.compile(r"\bknit(?:ted)?\b")),
    ("합성섬유", re.compile(r"\b(polyester|synthetic|nylon|viscose|(?:imitation|faux) (?:leather|suede))\b")),
    ("스판덱스", re.compile(r"\b(spandex|elastane)\b")),   # 'stretch'는 신축성 설명일 수 있어 제외
    ("데님", re.compile(r"\bdenim\b")),
    ("레이스", re.compile(r"\blace\b")),                  # lace-up은 아래에서 먼저 지움
    ("가죽", re.compile(r"\bleather\b")),                 # imitation/faux leather는 아래에서 먼저 지움
    ("울/캐시미어", re.compile(r"\b(wool|cashmere)\b")),
    ("메시", re.compile(r"\bmesh\b")),
    ("시퀸/글리터", re.compile(r"\b(sequins?|glitter)\b")),
    ("스웨이드", re.compile(r"\bsuede\b")),                 # imitation/faux suede는 아래에서 먼저 지움
    ("자카드", re.compile(r"\bjacquard\b")),
    ("린넨", re.compile(r"\blinen\b")),
    ("퍼", re.compile(r"\b(faux )?fur\b")),
    ("플리스", re.compile(r"\bfleece\b")),
    ("시폰", re.compile(r"\bchiffon\b")),
    ("벨벳", re.compile(r"\b(velvet|velour)\b")),
    ("실크", re.compile(r"\bsilk\b")),
    ("코듀로이", re.compile(r"\bcorduroy\b")),
    ("무스탕", re.compile(r"\b(mouton|shearling)\b")),
    ("비닐/PVC", re.compile(r"\b(vinyl|pvc)\b")),
    ("네오프렌", re.compile(r"\b(neoprene|scuba)\b")),
    ("앙고라", re.compile(r"\bangora\b")),
    ("헤어니트", re.compile(r"\bmohair\b")),               # 털이 긴 니트. H&M 설명에는 mohair로만 나옴
    ("트위드", re.compile(r"\btweed\b")),
]

_NOT_LACE = re.compile(r"\blace[- ]?up\b|\blacing\b")
_NOT_LEATHER = re.compile(r"\b(imitation|faux) leather\b")
_NOT_SUEDE = re.compile(r"\b(imitation|faux) suede\b")


def extract_materials(desc: str | None) -> list[str]:
    """detail_desc에서 K-Fashion 소재 목록. 없으면 빈 목록."""
    text = _s(desc).lower()
    found = []
    for name, pat in _MATERIALS:
        t = text
        if name == "레이스":
            t = _NOT_LACE.sub(" ", t)
        elif name == "가죽":
            t = _NOT_LEATHER.sub(" ", t)
        elif name == "스웨이드":
            t = _NOT_SUEDE.sub(" ", t)
        if pat.search(t):
            found.append(name)
    return found


# ---------------------------------------------------------------- 핏

# 영역별로 K-Fashion 핏 어휘가 다르다. 먼저 맞는 규칙을 쓴다.
_FIT_BOTTOM = [
    ("스키니", re.compile(r"\b(skinny|slim(?:-fit)? legs?|slim fit)\b")),
    ("벨보텀", re.compile(r"\b(flared|bootcut|boot-cut|bell-bottom)")),
    ("와이드", re.compile(r"\bwide\b")),
    ("루즈", re.compile(r"\b(relaxed|loose|baggy)\b")),
    ("노멀", re.compile(r"\b(regular fit|straight)\b")),
]
_FIT_TOP = [
    ("오버사이즈", re.compile(r"\boversized?\b")),
    ("루즈", re.compile(r"\b(relaxed|loose)\b")),
    ("타이트", re.compile(r"\b(fitted|slim fit|tight|close-fitting)\b")),
    ("노멀", re.compile(r"\bregular fit\b")),
]


def extract_fit(desc: str | None, area: str | None) -> str | None:
    if area not in ("상의", "하의", "아우터", "원피스"):
        return None
    rules = _FIT_BOTTOM if area == "하의" else _FIT_TOP
    text = _s(desc).lower()
    for name, pat in rules:
        if pat.search(text):
            return name
    return None


# ---------------------------------------------------------------- 디테일 (CLIP 인덱스용)

# detail_desc 키워드 → trend 표준 DetailName(40개). article_attrs에는 넣지 않고 CLIP 인덱스를 만들 때만 쓴다.
_DETAILS = [
    ("비즈", re.compile(r"\b(beads?|beaded|beading)\b")),
    ("단추", re.compile(r"\bbuttons?\b")),
    ("니트꽈배기", re.compile(r"\bcable[- ]knit")),
    ("체인", re.compile(r"\bchains?\b")),
    ("컷오프", re.compile(r"\b(raw[- ]edged?|cut[- ]off)\b")),
    ("더블브레스티드", re.compile(r"\bdouble[- ]breasted\b")),
    ("드롭숄더", re.compile(r"\b(dropped|drop) shoulders?\b")),
    ("자수", re.compile(r"\bembroider")),
    ("프릴", re.compile(r"\bfrill")),
    ("프린지", re.compile(r"\bfringe")),
    ("플레어", re.compile(r"\bflared?\b")),
    ("퀼팅", re.compile(r"\bquilt")),
    ("리본", re.compile(r"\b(bows?|ribbons?)\b")),
    ("롤업", re.compile(r"\b(roll[- ]?up|turn[- ]?ups?|turned[- ]up)\b")),
    ("러플", re.compile(r"\b(ruffles?|ruffled|flounces?|flounced)\b")),
    ("셔링", re.compile(r"\b(shirring|smock(ed|ing)|gathers|gathered)\b")),
    ("슬릿", re.compile(r"\bslits?\b")),
    ("스팽글", re.compile(r"\b(sequins?|sequinned|spangles?)\b")),
    ("스티치", re.compile(r"\b(top-?stitching|decorative stitching|contrasting stitching|visible stitching)\b")),
    ("스터드", re.compile(r"(?<!press-)(?<!press )\bstuds?\b|\bstudded\b")),   # press-stud(똑딱단추)는 제외
    ("퍼트리밍", re.compile(r"\b(fur|pile)[- ]trim")),
    ("글리터", re.compile(r"\b(glitter|glittery)\b")),
    ("후드", re.compile(r"\b(hood|hooded)\b")),
    ("레이스업", re.compile(r"\b(lace[- ]?up|lacing)\b")),
    ("패딩", re.compile(r"\bpadded\b")),
    ("패치워크", re.compile(r"\bpatchwork\b")),
    ("페플럼", re.compile(r"\bpeplum\b")),
    ("플리츠", re.compile(r"\bpleat")),
    ("포켓", re.compile(r"\bpockets?\b")),
    ("폼폼", re.compile(r"\bpom-?poms?\b")),
    ("퍼프", re.compile(r"\bpuff(ed|y)? sleeves?\b")),
    ("태슬", re.compile(r"\btassels?\b")),
    ("집업", re.compile(r"\b(zip[- ]through|zip[- ]up|full[- ]zip)\b")),
    ("띠", re.compile(r"\b(tie belt|tie-belt|belt|ties at the waist|waist tie)\b")),
    ("디스트로이드", re.compile(r"\b(distressed|ripped|rips|destroyed|trashed|(hard-)?worn details)\b")),
    ("드롭웨이스트", re.compile(r"\b(dropped|drop) waist\b")),
    ("버클", re.compile(r"\bbuckles?\b")),
    ("컷아웃", re.compile(r"\bcut[- ]?outs?\b")),
    ("X스트랩", re.compile(r"\b(criss-?cross|crossover|crossed) (back )?straps?\b")),
    ("비대칭", re.compile(r"\basymmetric")),
]


def extract_details(desc: str | None) -> list[str]:
    """detail_desc에서 trend 표준 DetailName 목록 (표준 순서). 없으면 빈 목록."""
    text = _s(desc).lower()
    return [name for name, pat in _DETAILS if pat.search(text)]


# ---------------------------------------------------------------- 비표준 표기 → trend 표준 어휘

# DB 값이 다른 표기(·)여도 응답은 trend 표준 어휘 표기(/)로 낸다
VOCAB_ALIASES = {
    "티셔츠·캐주얼상의": "티셔츠",
    "울·캐시미어": "울/캐시미어",
    "시퀸·글리터": "시퀸/글리터",
    "비닐·PVC": "비닐/PVC",
}


def to_vocab(name):
    """비표준 카테고리·소재 표기 → trend 표준 어휘 이름. 그 밖은 그대로."""
    return VOCAB_ALIASES.get(name, name) if isinstance(name, str) else name


# ---------------------------------------------------------------- 시즌 적합성

_WARM_MATERIALS = {"울/캐시미어", "퍼", "무스탕", "플리스", "코듀로이", "벨벳", "트위드", "앙고라", "헤어니트"}
_COOL_MATERIALS = {"린넨", "시폰", "메시"}
_WINTER_CATS = {"패딩", "코트"}
_SUMMER_TYPES = {"Shorts", "Vest top", "Sarong"}


def season_fit(area: str | None, category: str | None, materials: list[str], product_type: str,
               desc: str = "") -> list[str]:
    """입을 수 있는 계절 목록. 규칙이 없으면 사계절.

    규칙 (위에서부터 먼저 맞는 것)
    1. 수영복, 반바지·민소매·사롱 → 여름
    2. 패딩·코트 → 가을·겨울
    3. 따뜻한 소재(울·퍼·플리스·코듀로이·벨벳 등) → 가을·겨울
    4. 얇은 소재(린넨·시폰·메시) 또는 설명에 airy가 있음 → 봄·여름
    5. 그 밖 → 사계절
    """
    if area is None:
        return []
    if area == "수영복" or product_type in _SUMMER_TYPES:
        return ["여름"]
    if category in _WINTER_CATS:
        return ["가을", "겨울"]
    mats = set(materials)
    airy = bool(re.search(r"\bairy\b", _s(desc).lower()))
    if mats & _WARM_MATERIALS:
        return ["가을", "겨울"]
    if mats & _COOL_MATERIALS or airy:
        return ["봄", "여름"]
    return list(SEASONS)


# ---------------------------------------------------------------- 한 행 처리

def enrich_row(segment, product_type, prod_name, desc, colour_group) -> dict:
    cat = map_category(segment, product_type, prod_name, desc)
    area, category = cat if cat else (None, None)
    materials = extract_materials(desc) if cat else []
    return {
        "kf_area": area,
        "kf_category": category,
        "kf_color": map_color(colour_group),
        "materials": materials,
        "fit": extract_fit(desc, area),
        "season_fit": season_fit(area, category, materials, _s(product_type), desc),
    }
