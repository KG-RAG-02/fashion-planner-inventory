"""H&M 원본(Kaggle CSV 또는 Parquet) → data/stock.duckdb

03이 쓰는 articles, transactions만 올린다. customers는 쓰지 않아 올리지 않는다.
입력은 STOCK_DATA_DIR(기본 data/) 아래 Kaggle 원본 이름 그대로 둔다. 같은 이름의 .csv가 있으면 CSV를, 없으면 .parquet를 읽는다.
  articles.csv 또는 articles.parquet
  transactions_train.csv 또는 transactions_train.parquet
실행: python scripts/build_db.py   (이후 build_attributes.py, build_virtual_stock.py)
"""

import os
from pathlib import Path

import duckdb

DATA_DIR = os.getenv("STOCK_DATA_DIR", "data")
DB_PATH = os.getenv("STOCK_DB_PATH", "data/stock.duckdb")


def source(name: str) -> str:
    """Kaggle 원본 파일 하나를 읽는 DuckDB 함수 호출. CSV가 있으면 CSV, 없으면 Parquet."""
    for ext, reader in ((".csv", "read_csv"), (".parquet", "read_parquet")):
        path = Path(DATA_DIR) / f"{name}{ext}"
        if path.is_file():
            return f"{reader}('{path.as_posix()}')"
    raise SystemExit(f"{DATA_DIR}/{name}.csv 또는 .parquet가 없습니다. docs/data.md의 내려받기 절차를 보세요.")


ARTICLES, TRANSACTIONS = source("articles"), source("transactions_train")   # 입력부터 확인(없으면 DB 파일을 만들지 않음)
con = duckdb.connect(DB_PATH)  # 적재할 때만 쓰기 모드

con.execute(f"""
CREATE OR REPLACE TABLE articles AS
SELECT * REPLACE (lpad(CAST(article_id AS VARCHAR), 10, '0') AS article_id)
FROM {ARTICLES};
""")

con.execute("DROP TABLE IF EXISTS customers")   # 이 에이전트는 customers 표를 쓰지 않음

con.execute(f"""
CREATE OR REPLACE TABLE transactions AS
SELECT
    CAST(t_dat AS DATE) AS t_dat,
    customer_id,
    lpad(CAST(article_id AS VARCHAR), 10, '0') AS article_id,
    price,
    sales_channel_id
FROM {TRANSACTIONS}
WHERE CAST(t_dat AS DATE) <= DATE '2020-09-22'   -- 기준일 고정
ORDER BY t_dat;                                   -- 날짜 범위 조회가 빨라짐
""")

for t in ["articles", "transactions"]:
    print(t, con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0])
con.close()
