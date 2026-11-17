"""과잉재고 분석 함수 (#E)

호출 순서 (agent.run_trend가 이 순서로 부른다)
1. resolve_pair_areas       짝 카테고리 (get_matching_categories의 교집합, 없으면 합집합)
2. get_matching_colors      조합 색상표로 어울리는 색 (후보 0개면 any_matching_colors → 21색 전체로 완화)
3. get_inventory_status     후보 상품 + 재고·판매율·시즌 판매 비중 (SQL)
4. get_sales_trend          최근 4주 vs 직전 4주 (SQL)
   (착샷이 있으면 clip_matcher.clip_filter로 후보를 거름)
5. rank_overstock           네 지표 동일 가중 → 순위
6. summarize_attributes     상위 N의 색·카테고리·소재·핏 분포

SQL 값은 모두 파라미터로 넘긴다. DB는 읽기 전용 연결을 받는다.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

import pandas as pd

from stock_agent.attributes import map_color
from stock_agent.season import DATA_END, SeasonPeriod

TOP_N = 50          # 속성 요약을 보는 상위 후보 수 (응답 상품은 사진 있는 최상위 1개)

# ---------------------------------------------------------------- 1. 짝 카테고리

MATCHING_AREAS: dict[str, list[str]] = {
    "상의": ["하의", "아우터"],
    "하의": ["상의", "아우터"],
    "아우터": ["상의", "하의", "원피스"],
    "원피스": ["아우터"],
}

# 사용자 카테고리가 1개일 때의 짝 (2026-11-16 사용자 규칙). 단계별로 찾는다: 앞 단계에 후보가 없을 때만 다음 단계.
#   상의 기획 → 하의
#   하의 기획 → 상의, 상의 후보가 없으면 아우터
# 트렌드 카테고리 교집합(사용자 카테고리 없음)은 위 MATCHING_AREAS를 그대로 쓴다.
AREA_PRIORITY: dict[str, tuple[tuple[str, ...], ...]] = {
    "상의": (("하의",),),
    "하의": (("상의",), ("아우터",)),
}

# 세부 카테고리로 요청이 와도 영역을 찾을 수 있게
CATEGORY_TO_AREA: dict[str, str] = {
    **{c: "상의" for c in ["탑", "블라우스", "티셔츠", "티셔츠·캐주얼상의", "니트웨어", "셔츠", "브라탑", "후드티"]},
    **{c: "하의" for c in ["청바지", "팬츠", "스커트", "레깅스", "조거팬츠"]},
    **{c: "아우터" for c in ["코트", "재킷", "점퍼", "패딩", "베스트", "가디건", "짚업"]},
    **{c: "원피스" for c in ["드레스", "점프수트"]},
}


def get_matching_categories(target: str) -> tuple[str, list[str]]:
    """요청 카테고리(영역 또는 세부 카테고리) → (요청 영역, 짝 영역 목록)."""
    area = target if target in MATCHING_AREAS else CATEGORY_TO_AREA.get(target)
    if area is None:
        raise ValueError(f"짝 카테고리를 정할 수 없는 카테고리: {target!r}")
    return area, MATCHING_AREAS[area]


@dataclass(frozen=True)
class AreaMatch:
    areas: list[str]           # 찾을 짝 영역
    basis: str                 # "user" | "trend" | "all"
    given: list[str]           # 짝을 정하는 데 쓴 영역 (사용자 1개 또는 트렌드 여러 개)
    fallback: bool = False     # 트렌드 카테고리의 공통 짝이 없어 합집합으로 넓혔는지


COMBINES = ("auto", "intersection", "union")


def areas_of(categories) -> list[str]:
    """카테고리 목록(영역 또는 세부) → 영역 목록 (중복 제거, 나온 순서). 모르는 카테고리는 ValueError."""
    if isinstance(categories, str):
        categories = [categories]
    out: list[str] = []
    for c in categories or []:
        a = get_matching_categories(c)[0]
        if a not in out:
            out.append(a)
    return out


def combine_pairs(given: list[str], basis: str, combine: str = "auto") -> AreaMatch:
    """영역 여러 개의 짝 합치기.

    intersection  각 짝의 교집합(받은 영역은 뺌). 비면 빈 목록
    union         각 짝의 합집합. 사용자 카테고리면 받은 영역은 빼고(비면 그대로), 트렌드면 받은 영역도 넣는다
    auto          교집합, 비면 합집합 (fallback=True)
    """
    if combine not in COMBINES:
        raise ValueError(f"combine은 {', '.join(COMBINES)} 중 하나여야 합니다: {combine!r}")
    order = list(MATCHING_AREAS)
    sets = [set(MATCHING_AREAS[a]) for a in given]
    common = set.intersection(*sets) - set(given)
    union = set.union(*sets)
    if basis == "trend":
        union |= set(given)
    elif union - set(given):
        union -= set(given)
    pick = lambda s: [a for a in order if a in s]  # noqa: E731
    if combine == "intersection" or (combine == "auto" and common):
        return AreaMatch(pick(common), basis, given)
    return AreaMatch(pick(union), basis, given, fallback=combine == "auto")


def resolve_pair_areas(target, trend_categories: list[str] | None, combine: str = "auto") -> AreaMatch:
    """짝 영역 정하기 (2026-11-06 팀 합의, 2026-11-08 사용자 카테고리 여러 개).

    1) 사용자 카테고리(02)가 있으면 그 짝. 02는 사용자 카테고리가 든 트렌드만 넘기므로 트렌드 카테고리는 쓰지 않는다.
       여러 개면 각 짝의 교집합, 없으면 합집합
    2) 없고 트렌드 카테고리(02)가 있으면 각 짝의 교집합(받은 카테고리는 뺌).
       공통 짝이 없으면(예: 상의·하의·아우터, 네 영역 전부) 트렌드 카테고리와 그 짝을 모두 후보로 두고
       순위로 하나를 고른다
    3) 둘 다 없으면 전체 영역
    target은 문자열 1개 또는 목록.
    """
    given = areas_of(target)
    if given:
        if len(given) == 1 and combine == "auto":
            tiers = AREA_PRIORITY.get(given[0])
            areas = [a for tier in tiers for a in tier] if tiers else list(MATCHING_AREAS[given[0]])
            return AreaMatch(areas, "user", given)
        return combine_pairs(given, "user", combine)
    if trend_categories:
        return combine_pairs(areas_of(trend_categories), "trend", combine)
    return AreaMatch(list(MATCHING_AREAS), "all", [])


def split_exclude(exclude) -> tuple[list[str], list[str]]:
    """제외 카테고리 → (뺄 영역, 뺄 세부 카테고리). 모르는 카테고리는 ValueError."""
    areas, cats = [], []
    for c in exclude or []:
        if c in MATCHING_AREAS:
            areas.append(c) if c not in areas else None
        elif c in CATEGORY_TO_AREA:
            cats.append(c) if c not in cats else None
        else:
            raise ValueError(f"제외할 수 없는 카테고리: {c!r} (영역 상의·하의·아우터·원피스 또는 세부 카테고리)")
    return areas, cats


# ---------------------------------------------------------------- 2. 어울리는 색

KF_COLORS = (
    "블랙", "화이트", "그레이", "레드", "핑크", "오렌지", "베이지", "브라운", "옐로우", "그린",
    "카키", "민트", "블루", "네이비", "스카이블루", "퍼플", "라벤더", "와인", "네온", "골드", "실버",
)

# 조합 색상표 v1 (수동, 기본 코디 원칙: 무채색·뉴트럴은 대부분과 어울림, 톤 대비)
COLOR_PAIRS: dict[str, list[str]] = {
    "블랙": ["화이트", "그레이", "베이지", "레드", "핑크"],
    "화이트": ["블랙", "네이비", "블루", "스카이블루", "베이지", "그레이"],
    "그레이": ["블랙", "화이트", "네이비", "핑크", "라벤더"],
    "레드": ["블랙", "화이트", "네이비", "베이지"],
    "핑크": ["화이트", "그레이", "스카이블루", "베이지", "네이비"],
    "오렌지": ["화이트", "네이비", "베이지", "브라운"],
    "베이지": ["화이트", "브라운", "네이비", "블랙", "카키"],
    "브라운": ["베이지", "화이트", "카키", "오렌지"],
    "옐로우": ["화이트", "네이비", "그레이", "블루"],
    "그린": ["화이트", "베이지", "브라운", "네이비"],
    "카키": ["베이지", "화이트", "블랙", "브라운"],
    "민트": ["화이트", "그레이", "네이비", "베이지"],
    "블루": ["화이트", "베이지", "그레이", "옐로우"],
    "네이비": ["화이트", "베이지", "그레이", "레드", "스카이블루"],
    "스카이블루": ["화이트", "네이비", "핑크", "베이지", "그레이"],
    "퍼플": ["화이트", "그레이", "블랙"],
    "라벤더": ["화이트", "그레이", "네이비"],
    "와인": ["베이지", "그레이", "블랙", "네이비"],
    "네온": ["블랙", "화이트", "그레이"],
    "골드": ["블랙", "화이트", "네이비"],
    "실버": ["블랙", "화이트", "그레이"],
}

_COLOR_ALIASES = {
    "라이트블루": "스카이블루", "연청": "스카이블루", "하늘색": "스카이블루",
    "다크블루": "네이비", "딥블루": "네이비", "남색": "네이비",
    "아이보리": "화이트", "크림": "화이트", "오프화이트": "화이트",
    "차콜": "그레이", "회색": "그레이", "카멜": "브라운", "버건디": "와인",
    "올리브": "카키", "코랄": "오렌지", "연보라": "라벤더", "라일락": "라벤더",
}
_KO_PREFIXES = ("라이트", "다크", "딥", "파스텔", "페일", "밝은", "어두운", "연", "진")


def normalize_color(text: str) -> str | None:
    """트렌드 색 표기 → K-Fashion 색. 모르면 None."""
    t = text.strip().replace(" ", "")
    if t in KF_COLORS:
        return t
    if t in _COLOR_ALIASES:
        return _COLOR_ALIASES[t]
    en = map_color(text.strip().title())   # "light pink" → H&M 색 이름 규칙으로
    if en:
        return en
    for p in _KO_PREFIXES:
        if t.startswith(p) and t[len(p):] in KF_COLORS:
            return t[len(p):]
    return None


@dataclass(frozen=True)
class ColorMatch:
    colors: list[str]          # 찾을 색 전부 = matching + tone_on_tone
    unknown: list[str]         # K-Fashion 색으로 못 바꾼 트렌드 색
    overlap: int               # 후보 색이 어울리는 트렌드 색 수 (가장 많이 겹치는 수)
    n_trend: int               # 알아본 트렌드 색 수 (중복 제거)
    counts: dict[str, int]     # 색별로 어울리는 트렌드 색 수 (1개 이상인 색 전부)
    matching: list[str] = ()   # 조합표로 어울리는 색 중 가장 많이 겹치는 색
    tone_on_tone: list[str] = ()   # 기준 색 자체 (matching에 이미 있으면 뺌)


def get_matching_colors(trend_colors: list[str]) -> ColorMatch:
    """트렌드 색 → 후보 색 (트렌드 색과 어울리는 색 중 가장 많이 겹치는 색, 2026-11-08 결정).

    - 21색마다 트렌드 색 몇 개와 어울리는지(조합표 v1) 센다.
    - 그 수가 가장 큰 색만 후보로 쓴다. 최대가 같은 색이 여러 개면 모두 쓴다.
      예) 트렌드 색 2개: 둘 다와 어울리는 색이 있으면 그 색만, 없으면 각각과 어울리는 색 전부
    - 최대값은 항상 1 이상이라 찾을 색이 비지 않는다. 재고 후보가 0개일 때 넓히는 단계는 agent.run_trend.
    - 트렌드 색 개수 제한은 없다.
    - 기준 색 자체도 항상 찾는다(톤온톤, 2026-11-09 결정). colors = 어울리는 색 + 기준 색(어울리는 색에 없는 것만).
    순서: 처음 나온 순서 유지, 중복 제거.
    """
    known, unknown = [], []
    for c in trend_colors:
        k = normalize_color(c)
        if k is None:
            unknown.append(c)
        elif k not in known:
            known.append(k)

    counts: dict[str, int] = {}
    for k in known:
        for p in COLOR_PAIRS[k]:
            counts[p] = counts.get(p, 0) + 1

    overlap = max(counts.values()) if counts else 0
    matching = [c for c, n in counts.items() if n == overlap]
    tone = [k for k in known if k not in matching]
    return ColorMatch(matching + tone, unknown, overlap, len(known), counts, matching, tone)



def any_matching_colors(cm: ColorMatch) -> list[str]:
    """완화한 색: 기준 색 하나라도와 어울리는 색 전부 + 기준 색 자체(톤온톤)."""
    return list(cm.counts) + [k for k in cm.tone_on_tone if k not in cm.counts]

# ---------------------------------------------------------------- 3. 후보 재고

SEASON_TO_KF = {
    "spring": ["봄"], "summer": ["여름"], "fall": ["가을"], "winter": ["겨울"],
    "SS": ["봄", "여름"], "FW": ["가을", "겨울"],
}


def inventory_sql(period: SeasonPeriod) -> str:
    season_sql, _ = period.sql_filter("t_dat")
    return f"""
    WITH cand AS (
        SELECT a.article_id, a.segment, a.kf_area, a.kf_category, a.kf_color,
               a.materials, a.fit, a.season_fit,
               s.stock_qty, s.price_krw, s.stock_value_krw, s.sell_through, s.n_sold
        FROM article_attrs a JOIN stock_v1 s USING (article_id)
        WHERE list_contains(?::VARCHAR[], a.kf_area)
          AND list_contains(?::VARCHAR[], a.kf_color)
          AND list_has_any(a.season_fit, ?::VARCHAR[])
          AND s.stock_qty > 0
          AND (?::VARCHAR[] IS NULL OR list_contains(?::VARCHAR[], a.segment))
    ),
    season_sales AS (
        SELECT article_id,
               COUNT(*) FILTER (WHERE {season_sql}) AS season_sold,
               COUNT(*) AS total_sold
        FROM transactions
        WHERE article_id IN (SELECT article_id FROM cand) AND t_dat <= ?
        GROUP BY article_id
    )
    SELECT c.*,
           COALESCE(ss.season_sold, 0) AS season_sold,
           ss.season_sold / NULLIF(ss.total_sold, 0) AS season_share
    FROM cand c LEFT JOIN season_sales ss USING (article_id)
    ORDER BY c.article_id
    """


def get_inventory_status(con, areas: list[str], colors: list[str], period: SeasonPeriod,
                         segment: str | list[str] | tuple[str, ...] | None = None) -> pd.DataFrame:
    """짝 영역·어울리는 색·시즌에 맞고 재고가 있는 상품과 지표.

    segment: 고객군 1개 또는 목록. None·빈 목록이면 전체 고객군.
    """
    _, season_params = period.sql_filter("t_dat")
    kf_seasons = SEASON_TO_KF[period.season]
    segs = [segment] if isinstance(segment, str) else list(segment or [])
    segs = segs or None
    params = [areas, colors, kf_seasons, segs, segs, *season_params, DATA_END]
    return con.execute(inventory_sql(period), params).df()


# ---------------------------------------------------------------- 4. 최근 추세

def trend_ratio(recent: int, prev: int) -> float:
    """최근 판매 ÷ 직전 판매 − 1, -1 ~ +1로 자름. 둘 다 0이면 판매가 멈춘 것으로 보고 -1."""
    if prev == 0:
        return 1.0 if recent > 0 else -1.0
    return max(-1.0, min(1.0, recent / prev - 1))


MIN_TREND_SOLD = 5        # 최근+직전 판매가 이보다 적으면 추세를 판단하지 않음(중립 0)
MIN_GROUP_SOLD = 30       # 영역 묶음 판매가 이보다 적으면 후보 전체를 또래로 씀


def peer_relative_trend(df: pd.DataFrame, group_col: str = "kf_area",
                        min_sold: int = MIN_TREND_SOLD, min_group_sold: int = MIN_GROUP_SOLD) -> pd.DataFrame:
    """또래 대비 추세 (순위에 쓰는 trend).

    데이터가 가을 초입(2020-09-22)에 끝나서, 시즌이 끝난 상품은 모두 같이 판매가 줄어든다.
    그 계절 효과를 빼려고 같은 영역 후보들의 평균 변화와 비교한다.

    - 또래 변화율 = (또래 최근 합 + 1) ÷ (또래 직전 합 + 1)
    - 상품 변화율 = (최근 + 1) ÷ (직전 + 1)
    - trend = 상품 변화율 ÷ 또래 변화율 − 1, -1 ~ +1로 자름 (음수 = 또래보다 빨리 식음)
    - 최근+직전 판매가 min_sold 미만이면 판단하지 않고 0(중립). 0개·0개 상품이 추세만으로 위에 오지 않게
    - 영역 묶음 판매가 min_group_sold 미만이면 후보 전체를 또래로 씀

    입력 컬럼: recent_sold, prev_sold, group_col. 더하는 컬럼: trend, trend_raw, peer_trend, trend_basis
    """
    out = df.copy()
    r = out["recent_sold"].fillna(0).astype(int)
    p = out["prev_sold"].fillna(0).astype(int)
    out["trend_raw"] = [trend_ratio(a, b) for a, b in zip(r, p)]
    tot_r, tot_p = int(r.sum()), int(p.sum())
    grp = out.assign(_r=r, _p=p).groupby(group_col)[["_r", "_p"]].transform("sum")
    small = (grp["_r"] + grp["_p"]) < min_group_sold
    peer_r = grp["_r"].where(~small, tot_r)
    peer_p = grp["_p"].where(~small, tot_p)
    peer_ratio = (peer_r + 1) / (peer_p + 1)
    item_ratio = (r + 1) / (p + 1)
    out["peer_trend"] = (peer_ratio - 1).clip(-1, 1).round(3)
    rel = (item_ratio / peer_ratio - 1).clip(-1, 1)
    few = (r + p) < min_sold
    out["trend"] = rel.where(~few, 0.0)
    out["trend_basis"] = ["too_few_sales" if f else "peer_relative" for f in few]
    return out


def get_sales_trend(con, article_ids: list[str], weeks: int = 4) -> pd.DataFrame:
    """기준일(DATA_END)부터 거꾸로 센 최근 N주 vs 직전 N주."""
    mid = DATA_END - timedelta(weeks=weeks)
    start = DATA_END - timedelta(weeks=2 * weeks)
    ids = pd.DataFrame({"article_id": list(article_ids)})
    con.register("trend_ids", ids)
    try:
        df = con.execute("""
            SELECT i.article_id,
                   COUNT(x.article_id) FILTER (WHERE x.t_dat > ?)  AS recent_sold,
                   COUNT(x.article_id) FILTER (WHERE x.t_dat <= ?) AS prev_sold
            FROM trend_ids i
            LEFT JOIN transactions x
              ON x.article_id = i.article_id AND x.t_dat > ? AND x.t_dat <= ?
            GROUP BY i.article_id
            ORDER BY i.article_id
        """, [mid, mid, start, DATA_END]).df()
    finally:
        con.unregister("trend_ids")
    df.attrs["period"] = {"recent": (mid + timedelta(days=1), DATA_END), "prev": (start + timedelta(days=1), mid)}
    return df


# ---------------------------------------------------------------- 5. 순위

WEIGHTS = {"stock_value": 0.25, "low_sell_through": 0.25, "declining_trend": 0.25, "season_share": 0.25}


def rank_overstock(cand: pd.DataFrame, n: int | None = None, weights: dict | None = None) -> pd.DataFrame:
    """많이 남았는데 잘 안 팔리는 순서.

    cand 컬럼: article_id, stock_value_krw, sell_through, trend, season_share
    각 지표를 후보 안 백분위(0~1)로 바꾼 뒤 가중 평균. 동점은 article_id 순. n=None이면 후보 전체.
    """
    w = weights or WEIGHTS
    if cand.empty:
        out = cand.copy()
        out["score"] = pd.Series(dtype=float)
        out["rank"] = pd.Series(dtype=int)
        return out
    df = cand.copy()
    share = df["season_share"].astype(float)
    df["season_share_filled"] = share.fillna(share.median() if share.notna().any() else 0.0)
    pct = lambda s: s.rank(pct=True, method="average")  # noqa: E731
    df["s_stock_value"] = pct(df["stock_value_krw"])
    df["s_low_sell_through"] = 1 - pct(df["sell_through"]) + 1 / len(df)
    df["s_declining_trend"] = 1 - pct(df["trend"]) + 1 / len(df)
    df["s_season_share"] = pct(df["season_share_filled"])
    df["score"] = sum(w[k] * df[f"s_{k}"] for k in w) / sum(w.values())
    df = df.sort_values(["score", "article_id"], ascending=[False, True])
    df = (df if n is None else df.head(n)).reset_index(drop=True)
    df["rank"] = range(1, len(df) + 1)
    return df


# ---------------------------------------------------------------- 6. 속성 요약

def summarize_attributes(top: pd.DataFrame) -> dict:
    """상위 상품의 분포. 핏·색이 비어 있으면 '정보 없음'."""
    def counts(s: pd.Series) -> dict:
        return {str(k): int(v) for k, v in s.fillna("정보 없음").value_counts().items()}

    mats = top["materials"].explode() if len(top) else pd.Series(dtype=object)
    return {
        "n_items": int(len(top)),
        "total_stock_qty": int(top["stock_qty"].sum()) if len(top) else 0,
        "area": counts(top["kf_area"]),
        "category": counts(top["kf_category"]),
        "color": counts(top["kf_color"]),
        "material": counts(mats.dropna()) if len(mats) else {},
        "fit": counts(top["fit"]),
    }
