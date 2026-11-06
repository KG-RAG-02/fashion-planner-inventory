"""추세 규칙 비교: 예전(절대 추세) vs 지금(또래 대비 + 판매 적으면 중립)

검증용 10개 요청으로 두 규칙의 순위를 나란히 계산해 무엇이 달라지는지 본다.
사진 유무와 상관없이 순수 순위만 비교한다.

실행: python scripts/compare_trend.py
"""

import os
import sys
from pathlib import Path

import duckdb
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from stock_agent.overstock import (  # noqa: E402
    get_inventory_status, get_matching_categories, get_matching_colors, get_sales_trend,
    peer_relative_trend, rank_overstock,
)
from stock_agent.season import map_season  # noqa: E402


# validate_requests.py와 같은 10개 요청
REQUESTS = [
    ("Ladieswear", "2027-SS", "상의", ["라이트핑크", "화이트"]),
    ("Ladieswear", "2027-FW", "원피스", ["버건디"]),
    ("Divided", "2027 여름", "하의", ["스카이블루"]),
    ("Divided", "2027 가을", "상의", ["카키"]),
    ("Menswear", "2027-FW", "상의", ["네이비"]),
    ("Menswear", "2027 봄", "하의", ["베이지", "그레이"]),
    ("Baby/Children", "2027-SS", "상의", ["옐로우"]),
    ("Baby/Children", "2027 겨울", "아우터", ["레드"]),
    ("Sport", "2027-SS", "하의", ["블랙"]),
    ("Sport", "2027-FW", "아우터", ["화이트", "핑크", "그레이"]),   # 트렌드 색 3개 → 다수결
]

con = duckdb.connect(os.environ.get("STOCK_DB_PATH", "data/stock.duckdb"), read_only=True)


def zero_zero(df):
    return int(((df.recent_sold == 0) & (df.prev_sold == 0)).sum())


rows = []
for i, (segment, season, target, colors) in enumerate(REQUESTS, 1):
    period = map_season(season)
    _, areas = get_matching_categories(target)
    cand = get_inventory_status(con, areas, get_matching_colors(colors).colors, period, segment=segment)
    if cand.empty:
        rows.append({"#": i, "고객군": segment, "시즌": season, "카테고리": target, "비고": "후보 없음"})
        continue
    t = get_sales_trend(con, cand.article_id.tolist())
    cand = cand.merge(t[["article_id", "recent_sold", "prev_sold"]], on="article_id", how="left")
    cand = peer_relative_trend(cand)

    old = rank_overstock(cand.assign(trend=cand.trend_raw))       # 예전: 절대 추세, 0·0은 -1
    new = rank_overstock(cand)                                    # 지금: 또래 대비, 판매 적으면 0
    o1, n1 = old.iloc[0], new.iloc[0]
    rows.append({
        "#": i, "고객군": segment, "시즌": season, "카테고리": target,
        "예전 1위": f"{o1.article_id} {o1.kf_category}({o1.kf_color}) 판매 {o1.recent_sold}/{o1.prev_sold}",
        "지금 1위": f"{n1.article_id} {n1.kf_category}({n1.kf_color}) 판매 {n1.recent_sold}/{n1.prev_sold}",
        "1위 같음": o1.article_id == n1.article_id,
        "상위50 겹침": len(set(old.article_id) & set(new.article_id)),
        "8주 판매0 (예전→지금)": f"{zero_zero(old)}→{zero_zero(new)}",
        "또래 추세": round(float(new.peer_trend.iloc[0]), 2),
    })

pd.set_option("display.width", 250)
pd.set_option("display.max_colwidth", 60)
print(pd.DataFrame(rows).to_string(index=False))
print("\n판매 a/b = 최근 4주 / 직전 4주. 또래 추세 = 지금 1위가 속한 영역 후보 전체의 변화율(음수면 다 같이 줄어드는 중)")
con.close()
