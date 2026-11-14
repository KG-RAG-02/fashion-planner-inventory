"""CLIP 텍스트 규칙 — 소재(Material 26) + 디테일(DetailName 40)을 영어로

CLIP(ViT-B-32, OpenAI)은 영어로 학습되어 한글 단어의 뜻을 구분하지 못한다(시폰 러플 ↔ 데님 디스트로이드 cosine 0.96).
그래서 CLIP에 넣는 텍스트만 02 표준 어휘(vocabulary_v1)의 영어 대응어로 바꾼다. 응답·데이터는 한글 그대로다.

색은 넣지 않는다 (색은 조합 색상표가 정하고, CLIP은 소재·질감·디테일만 본다).
카테고리·핏·기장도 넣지 않는다 (기준 이미지는 트렌드 아이템 카테고리이고, 재고는 짝 카테고리라 서로 다르다).

기준 쪽(02 착샷·상품 이미지의 materials·details)과 재고 쪽(인덱스: article_attrs.materials + 상품 설명에서 뽑은 디테일)이
이 모듈의 clip_text 하나를 같이 쓴다. 텍스트가 비면 ""를 돌려주고, 그때는 이미지 임베딩만 쓴다.
"""

from __future__ import annotations

from stock_agent.attributes import to_vocab

MATERIAL_EN: dict[str, str] = {
    "퍼": "fur",
    "무스탕": "shearling",
    "스웨이드": "suede",
    "앙고라": "angora",
    "코듀로이": "corduroy",
    "시퀸/글리터": "sequin glitter",
    "데님": "denim",
    "저지": "jersey",
    "트위드": "tweed",
    "벨벳": "velvet",
    "비닐/PVC": "vinyl PVC",
    "울/캐시미어": "wool cashmere",
    "합성섬유": "synthetic fabric",
    "니트": "knit",
    "레이스": "lace",
    "린넨": "linen",
    "메시": "mesh",
    "플리스": "fleece",
    "네오프렌": "neoprene",
    "실크": "silk",
    "스판덱스": "spandex",
    "자카드": "jacquard",
    "가죽": "leather",
    "면": "cotton",
    "시폰": "chiffon",
    "헤어니트": "fuzzy hair knit",
}

DETAIL_EN: dict[str, str] = {
    "비즈": "beads",
    "단추": "buttons",
    "니트꽈배기": "cable knit",
    "체인": "chain",
    "컷오프": "cut-off raw hem",
    "더블브레스티드": "double-breasted",
    "드롭숄더": "dropped shoulders",
    "자수": "embroidery",
    "프릴": "frill",
    "프린지": "fringe",
    "플레어": "flared",
    "퀼팅": "quilted",
    "리본": "bow ribbon",
    "롤업": "rolled-up cuffs",
    "러플": "ruffle",
    "셔링": "shirring gathers",
    "슬릿": "slit",
    "스팽글": "sequins",
    "스티치": "contrast stitching",
    "스터드": "studs",
    "퍼트리밍": "fur trim",
    "글리터": "glitter",
    "후드": "hood",
    "레이스업": "lace-up",
    "패딩": "padded",
    "패치워크": "patchwork",
    "페플럼": "peplum",
    "플리츠": "pleats",
    "포켓": "pockets",
    "폼폼": "pompom",
    "퍼프": "puff sleeves",
    "태슬": "tassels",
    "집업": "zip-up",
    "띠": "tie belt",
    "디스트로이드": "distressed ripped",
    "드롭웨이스트": "drop waist",
    "버클": "buckle",
    "컷아웃": "cut-out",
    "X스트랩": "criss-cross straps",
    "비대칭": "asymmetric",
}


def _values(items) -> list[str]:
    """02 목록 값 → 문자열. 문자열 또는 {"value"|"name": ...} dict를 받는다."""
    out = []
    for x in items or []:
        if isinstance(x, str):
            out.append(x)
        elif isinstance(x, dict):
            v = x.get("value") or x.get("name")
            if isinstance(v, str):
                out.append(v)
    return out


def detail_names(details) -> list[str]:
    """02 details 중 DetailName(type=detail 또는 type 없음)만. 넥라인·칼라는 뺀다."""
    keep = []
    for x in details or []:
        if isinstance(x, dict) and x.get("type") not in (None, "detail"):
            continue
        keep.append(x)
    return [v for v in _values(keep) if v in DETAIL_EN]


def clip_text(materials, details) -> str:
    """소재 + 디테일 → 영어 CLIP 텍스트. 둘 다 없으면 "" (이미지만 쓴다)."""
    words: list[str] = []
    for m in _values(materials):
        en = MATERIAL_EN.get(to_vocab(m))
        if en and en not in words:
            words.append(en)
    for d in detail_names(details):
        en = DETAIL_EN[d]
        if en not in words:
            words.append(en)
    return ", ".join(words)
