"""분석 함수 6개를 순서대로 실행해 보는 확인용 스크립트 (#E)

실행 예 (레포 맨 위 폴더에서)
  python scripts/try_overstock.py
  python scripts/try_overstock.py --season "2027 FW" --target 하의 --colors 네이비 --segment Menswear
"""

import argparse
import os
import sys
import time
from pathlib import Path

import duckdb
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from stock_agent.overstock import (  # noqa: E402
    get_inventory_status, get_matching_categories, get_matching_colors,
    get_sales_trend, rank_overstock,
)
from stock_agent.season import map_season  # noqa: E402

ap = argparse.ArgumentParser()
ap.add_argument("--season", default="2027-SS")
ap.add_argument("--target", default="상의")
ap.add_argument("--colors", nargs="+", default=["라이트핑크", "화이트"])
ap.add_argument("--segment", default="Ladieswear")
ap.add_argument("--n", type=int, default=50)
args = ap.parse_args()

con = duckdb.connect(os.environ.get("STOCK_DB_PATH", "data/stock.duckdb"), read_only=True)
t0 = time.time()

period = map_season(args.season)
area, areas = get_matching_categories(args.target)
cm = get_matching_colors(args.colors)
colors, unknown = cm.colors, cm.unknown
print(f"1. 요청 영역 {area} → 짝 영역 {areas}")
print(f"2. 트렌드 색 {args.colors} → 어울리는 색 {colors} (트렌드 색 {cm.n_trend}개 중 {cm.overlap}개와 어울림, "
      f"어울리는 색 중 가장 많이 겹치는 색)" + (f"  (알 수 없는 색: {unknown})" if unknown else ""))
print(f"   시즌 기준: {period.basis}")

cand = get_inventory_status(con, areas, colors, period, segment=args.segment)
print(f"3. 후보 상품 {len(cand):,}개")
if cand.empty:
    print("결과 없음 (no_results)")
    sys.exit(0)

trend = get_sales_trend(con, cand["article_id"].tolist())
cand = cand.merge(trend[["article_id", "recent_sold", "prev_sold", "trend"]], on="article_id")
fmt = lambda r: f"{r[0]}~{r[1]}"  # noqa: E731
print(f"4. 최근 추세: 최근 {fmt(trend.attrs['period']['recent'])} vs 직전 {fmt(trend.attrs['period']['prev'])}")

top = rank_overstock(cand, n=args.n)
print(f"5. 상위 {len(top)}개\n")
pd.set_option("display.width", 200)
names = con.execute(
    "SELECT article_id, prod_name FROM articles WHERE list_contains(?::VARCHAR[], article_id)",
    [top["article_id"].tolist()],
).df()
show = top.merge(names, on="article_id")[
    ["rank", "article_id", "prod_name", "kf_area", "kf_category", "kf_color", "stock_qty",
     "stock_value_krw", "sell_through", "trend", "season_share", "score"]
].sort_values("rank")
print(show.head(15).to_string(index=False, float_format=lambda x: f"{x:.2f}"))

print(f"\n소요 시간 {time.time() - t0:.1f}초")
