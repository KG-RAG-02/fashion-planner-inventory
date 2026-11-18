"""가상 재고 생성 → virtual_stock 테이블

먼저 scripts/build_attributes.py로 article_attrs를 만든 뒤 실행한다.
실행: 레포 맨 위 폴더에서  python scripts/build_virtual_stock.py
"""

import os
import sys
from pathlib import Path

import duckdb

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from fashion_planner_inventory.synthetic import AS_OF, SEED, build_stock  # noqa: E402

DB_PATH = os.environ.get("STOCK_DB_PATH", "data/stock.duckdb")
con = duckdb.connect(DB_PATH)

# 의류(kf_area 있음)만 대상. 안 팔린 상품도 포함하려고 LEFT JOIN
sales = con.execute("""
    SELECT t.article_id, t.segment, t.kf_area,
           COUNT(x.article_id)      AS n_sold,
           MAX(x.t_dat)             AS last_sale,
           MAX(x.price)             AS max_price
    FROM article_attrs t
    LEFT JOIN transactions x
      ON x.article_id = t.article_id AND x.t_dat <= ?
    WHERE t.kf_area IS NOT NULL
    GROUP BY ALL
""", [AS_OF]).df()

stock = build_stock(sales, seed=SEED)

con.register("stock_df", stock)
con.execute("CREATE OR REPLACE TABLE virtual_stock AS SELECT * FROM stock_df")
con.unregister("stock_df")
print(f"virtual_stock 저장: {len(stock):,}행 (seed={SEED}, 기준일 {AS_OF})")

# ---- 검사 ----
neg = int((stock.stock_qty < 0).sum())
print(f"재고 음수 상품: {neg}개  {'OK' if neg == 0 else '확인 필요'}")
unsold = int((stock.n_sold == 0).sum())
print(f"한 번도 안 팔린 상품: {unsold:,}개")

print("\n[요약]")
print(stock[["n_sold", "inbound_qty", "stock_qty", "sell_through", "price_krw", "stock_value_krw"]]
      .describe(percentiles=[.1, .5, .9]).round(2).to_string())

print("\n[고객군·영역별 재고]")
summary = con.execute("""
    SELECT a.segment, a.kf_area,
           COUNT(*)                        AS 상품수,
           SUM(s.stock_qty)                AS 재고합,
           ROUND(AVG(s.sell_through), 3)   AS 평균판매율,
           ROUND(SUM(s.stock_value_krw) / 1e8, 1) AS 재고금액_억원
    FROM virtual_stock s JOIN article_attrs a USING (article_id)
    GROUP BY ALL ORDER BY a.segment, a.kf_area
""").df()
print(summary.to_string(index=False))
con.close()
