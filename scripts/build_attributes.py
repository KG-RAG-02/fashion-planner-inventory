"""articles에 K-Fashion 속성을 붙여 article_attrs 테이블로 저장한다 (#C).

실행: 레포 맨 위 폴더에서  python scripts/build_attributes.py
DB를 쓰기 모드로 여는 건 이 스크립트뿐이다. 에이전트는 read_only로 연다.
"""

import os
import sys
from pathlib import Path

import duckdb
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from fashion_planner_inventory.attributes import enrich_row  # noqa: E402

DB_PATH = os.environ.get("STOCK_DB_PATH", "data/stock.duckdb")

con = duckdb.connect(DB_PATH)
arts = con.execute("""
    SELECT article_id, index_group_name, product_type_name, prod_name,
           detail_desc, colour_group_name
    FROM articles
""").df()

rows = [
    enrich_row(r.index_group_name, r.product_type_name, r.prod_name, r.detail_desc, r.colour_group_name)
    for r in arts.itertuples(index=False)
]
attrs = pd.concat([arts[["article_id", "index_group_name"]], pd.DataFrame(rows)], axis=1)
attrs = attrs.rename(columns={"index_group_name": "segment"})

con.register("attrs_df", attrs)
con.execute("CREATE OR REPLACE TABLE article_attrs AS SELECT * FROM attrs_df")
con.unregister("attrs_df")
print(f"article_attrs 저장: {len(attrs):,}행")

# ---- 보고 ----
garment = attrs[attrs.kf_area.notna()]
print(f"\n의류로 분류: {len(garment):,} / {len(attrs):,} ({len(garment)/len(attrs):.1%})")

print("\n[영역별 상품 수]")
print(garment.groupby(["segment", "kf_area"]).size().unstack(fill_value=0).to_string())

print("\n[카테고리별 상품 수]")
print(garment.groupby(["kf_area", "kf_category"]).size().to_string())

print("\n[고객군별 커버율 (의류만)]")
cov = garment.groupby("segment").agg(
    상품수=("article_id", "size"),
    색=("kf_color", lambda s: s.notna().mean()),
    소재=("materials", lambda s: s.map(len).gt(0).mean()),
    핏=("fit", lambda s: s.notna().mean()),
)
print(cov.to_string(float_format=lambda x: f"{x:.1%}"))

print("\n[K-Fashion 색으로 못 바꾼 colour_group_name]")
unmapped = con.execute("""
    SELECT a.colour_group_name, COUNT(*) n
    FROM articles a JOIN article_attrs t USING (article_id)
    WHERE t.kf_area IS NOT NULL AND t.kf_color IS NULL
    GROUP BY 1 ORDER BY n DESC
""").df()
print(unmapped.to_string(index=False) if len(unmapped) else "(없음)")

print("\n[시즌 적합성 분포 (의류만)]")
print(garment.season_fit.map(lambda s: "·".join(s)).value_counts().to_string())

# 수동 확인용 무작위 50개
sample = con.execute("""
    SELECT t.segment, a.product_type_name, a.prod_name, t.kf_area, t.kf_category,
           a.colour_group_name, t.kf_color, t.materials, t.fit, t.season_fit, a.detail_desc
    FROM article_attrs t JOIN articles a USING (article_id)
    WHERE t.kf_area IS NOT NULL
""").df().sample(n=50, random_state=42)
sample.to_csv("data/attrs_sample50.csv", index=False, encoding="utf-8-sig")
print("\n수동 확인용 표본 50개: data/attrs_sample50.csv")
con.close()
