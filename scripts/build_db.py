"""H&M 원본(Parquet) → data/stock.duckdb

03이 쓰는 articles, transactions만 올린다. customers는 쓰지 않아 올리지 않는다.
실행: python scripts/build_db.py   (이후 build_attributes.py, build_stock_v1.py)
"""

import os

import duckdb

DATA_DIR = os.getenv("STOCK_DATA_DIR", "data")
DB_PATH = os.getenv("STOCK_DB_PATH", "data/stock.duckdb")

con = duckdb.connect(DB_PATH)  # 적재할 때만 쓰기 모드

con.execute(f"""
CREATE OR REPLACE TABLE articles AS
SELECT * REPLACE (lpad(CAST(article_id AS VARCHAR), 10, '0') AS article_id)
FROM read_parquet('{DATA_DIR}/articles.parquet');
""")

con.execute("DROP TABLE IF EXISTS customers")   # 예전에 올린 customers가 남아 있으면 지움

con.execute(f"""
CREATE OR REPLACE TABLE transactions AS
SELECT
    CAST(t_dat AS DATE) AS t_dat,
    customer_id,
    lpad(CAST(article_id AS VARCHAR), 10, '0') AS article_id,
    price,
    sales_channel_id
FROM read_parquet('{DATA_DIR}/transactions_train.parquet')
WHERE CAST(t_dat AS DATE) <= DATE '2020-09-22'   -- 기준일 고정
ORDER BY t_dat;                                   -- 날짜 범위 조회가 빨라짐
""")

for t in ["articles", "transactions"]:
    print(t, con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0])
con.close()