"""결정적 합성 fixture DuckDB — 테스트와 오프라인 stub 프로필(`STOCK_DB_PATH`)이 같이 쓴다. H&M 원본 데이터가 아니다.

- create_base_tables(con): tests/conftest.py가 써 온 작은 표(a1~a6) 그대로.
- add_stub_rows(con): trend-analysis-agent stub 결과(examples/stub/inventory-request.json의 input: 발레코어, 2027 SS,
  사용자 카테고리 상의, 사용자 색 핑크)와 맞춘 상품 3개 — 짝 영역 하의·아우터, 핑크와 어울리는 색, 봄·여름.
- finalize(con): articles 표, stock_v1 snapshot 열(2020-09-22 · virtual_stock_v1 · sample), 표지 표 `stock_fixture`.
  readiness는 `stock_fixture`가 있으면 synthetic_fixture=true를 내고, API는 trend 결과마다 합성 DB임을 limitations에 적는다.
- build_fixture_db(path): 위 순서로 새 파일을 만든다. 기본 storage version v1.0.0 (DuckDB 1.x·2.x 모두 읽는다).
실행 진입점: scripts/build_fixture_db.py, 또는 `python -m fashion_planner_inventory.fixture_db <out.duckdb>` (이미지 안에서도 된다).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

from fashion_planner_inventory.readiness import FIXTURE_TABLE  # marker table readiness looks for

DEFAULT_STORAGE_VERSION = "v1.0.0"

BASE_TABLES = (
    """CREATE TABLE article_attrs AS SELECT * FROM (VALUES
        ('a1','Ladieswear','하의','청바지','스카이블루',['데님'],'와이드',['봄','여름','가을','겨울']),
        ('a2','Ladieswear','하의','스커트','레드',[]::VARCHAR[],NULL,['봄','여름','가을','겨울']),
        ('a3','Ladieswear','아우터','패딩','베이지',[]::VARCHAR[],NULL,['가을','겨울']),
        ('a4','Menswear','하의','팬츠','그레이',['면'],NULL,['봄','여름','가을','겨울']),
        ('a5','Ladieswear','상의','탑','그레이',[]::VARCHAR[],NULL,['봄','여름','가을','겨울']),
        ('a6','Ladieswear','하의','팬츠','베이지',[]::VARCHAR[],NULL,['봄','여름','가을','겨울'])
      ) v(article_id,segment,kf_area,kf_category,kf_color,materials,fit,season_fit)""",
    """CREATE TABLE stock_v1 AS SELECT * FROM (VALUES
        ('a1',100,30000,3000000,0.4,200),('a2',100,30000,3000000,0.4,200),
        ('a3',100,30000,3000000,0.4,200),('a4',100,30000,3000000,0.4,200),
        ('a5',100,30000,3000000,0.4,200),('a6',0,30000,0,0.9,200)
      ) v(article_id,stock_qty,price_krw,stock_value_krw,sell_through,n_sold)""",
    """CREATE TABLE transactions AS SELECT * FROM (VALUES
        (DATE '2019-04-01','a1'),(DATE '2019-04-02','a1'),(DATE '2019-10-01','a1'),
        (DATE '2020-09-20','a1'),(DATE '2020-08-01','a1'),(DATE '2020-08-02','a4')
      ) v(t_dat, article_id)""",
)

# trend stub(발레코어, 상의 + 핑크)의 짝 영역(하의·아우터) · 핑크와 어울리는 색 · SS(봄·여름)에 맞춘 합성 상품
STUB_ROWS = (
    """INSERT INTO article_attrs VALUES
        ('0910001001','Ladieswear','하의','스커트','화이트',['면'],NULL,['봄','여름']),
        ('0910002001','Divided','아우터','재킷','베이지',['면'],'오버사이즈',['봄','가을']),
        ('0910003001','Ladieswear','하의','팬츠','네이비',['린넨'],'와이드',['봄','여름'])""",
    """INSERT INTO stock_v1 VALUES
        ('0910001001',120,39000,4680000,0.35,65),
        ('0910002001',80,59000,4720000,0.45,65),
        ('0910003001',60,45000,2700000,0.62,98)""",
    """INSERT INTO transactions VALUES
        (DATE '2019-04-10','0910001001'),(DATE '2019-06-15','0910001001'),(DATE '2020-05-20','0910001001'),
        (DATE '2020-09-01','0910001001'),(DATE '2019-10-01','0910002001'),(DATE '2020-03-15','0910002001'),
        (DATE '2020-08-30','0910003001'),(DATE '2020-09-10','0910003001'),(DATE '2020-09-15','0910003001')""",
)


def create_base_tables(con) -> None:
    for sql in BASE_TABLES:
        con.execute(sql)


def add_stub_rows(con) -> None:
    for sql in STUB_ROWS:
        con.execute(sql)


def finalize(con) -> None:
    """readiness가 요구하는 articles·snapshot 열과 합성 표지 표 (tests/test_api.py make_client와 같은 열)."""
    con.execute("CREATE TABLE articles AS SELECT article_id FROM article_attrs ORDER BY article_id")
    con.execute("ALTER TABLE stock_v1 ADD COLUMN as_of DATE DEFAULT DATE '2020-09-22'")
    con.execute("ALTER TABLE stock_v1 ADD COLUMN version VARCHAR DEFAULT 'virtual_stock_v1'")
    con.execute("ALTER TABLE stock_v1 ADD COLUMN data_nature VARCHAR DEFAULT 'sample'")
    con.execute(f"CREATE TABLE {FIXTURE_TABLE} (key VARCHAR, value VARCHAR)")
    con.execute(f"INSERT INTO {FIXTURE_TABLE} VALUES ('data_nature', 'synthetic_fixture'), "
                "('builder', 'fashion_planner_inventory.fixture_db'), "
                "('aligned_with', 'trend-analysis-agent examples/stub (request_id stub-trend-001)')")


def content_digest(con) -> str:
    """표 이름·행 내용의 SHA-256 (파일 바이트가 아니라 내용이 같은지 보는 값)."""
    digest = hashlib.sha256()
    tables = con.execute("SELECT table_name FROM information_schema.tables WHERE table_schema = 'main' "
                         "AND table_catalog = current_database() ORDER BY table_name").fetchall()
    for (table,) in tables:
        digest.update(table.encode())
        for row in con.execute(f'SELECT * FROM "{table}" ORDER BY ALL').fetchall():
            digest.update(repr(row).encode())
    return digest.hexdigest()


def describe(path: str | Path) -> dict:
    """만든 파일을 read-only로 열어: 표별 행 수, 내용 해시, readiness, storage version."""
    import duckdb

    from fashion_planner_inventory.readiness import database_storage_info, inspect_database

    con = duckdb.connect(str(path), read_only=True)
    try:
        tables = [t for (t,) in con.execute("SELECT table_name FROM information_schema.tables WHERE table_schema = "
                                            "'main' AND table_catalog = current_database() ORDER BY 1").fetchall()]
        readiness = inspect_database(con)
        return {"path": str(path), "tables": {t: con.execute(f'SELECT count(*) FROM "{t}"').fetchone()[0] for t in tables},
                "content_sha256": content_digest(con), "db_ready": readiness["db_ready"],
                "reason": readiness["reason"], "synthetic_fixture": readiness.get("synthetic_fixture"),
                "duckdb_version": duckdb.__version__, **database_storage_info(con, path)}
    finally:
        con.close()


def build_fixture_db(path: str | Path, *, storage_version: str | None = DEFAULT_STORAGE_VERSION,
                     overwrite: bool = False) -> dict:
    """새 fixture DB 파일을 만들고 describe() 결과를 돌려준다. 있는 파일은 overwrite 없이 거부(FileExistsError)."""
    import duckdb

    path = Path(path)
    if path.exists():
        if not overwrite:
            raise FileExistsError(str(path))
        path.unlink()
    Path(f"{path}.wal").unlink(missing_ok=True)
    quoted = str(path).replace("'", "''")
    con = duckdb.connect()
    try:
        try:
            con.execute(f"ATTACH '{quoted}' AS fixture"
                        + (f" (STORAGE_VERSION '{storage_version}')" if storage_version else ""))
        except duckdb.Error:
            if not storage_version:
                raise
            # DuckDB < 1.2 has no STORAGE_VERSION option; its default format is already v1.0-compatible.
            path.unlink(missing_ok=True)
            con.execute(f"ATTACH '{quoted}' AS fixture")
        con.execute("USE fixture")
        create_base_tables(con)
        add_stub_rows(con)
        finalize(con)
        con.execute("USE memory")
        con.execute("DETACH fixture")
    finally:
        con.close()
    return describe(path)


def summarize_result(result: dict) -> dict:
    """Stock producer result → 비교용 요약 (시각·이미지 바이트 없이; examples/stub/inventory-response-summary.json)."""
    return {
        "status": result.get("status"),
        "products": [None if p is None else {
            "sku_ids": p["sku_ids"], "category": p["category"], "color": p["attributes"]["color"],
            "quantity": p["inventory"]["quantity"], "data_nature": p["inventory"]["data_nature"],
            "has_image": p["image"] is not None} for p in result.get("products", [])],
        "trend_results": [{
            "style": tr["style"], "status": tr["status"], "rank": tr["rank"],
            "areas": tr["search"].get("areas"), "colors": tr["search"].get("colors"),
            "segments": tr["search"].get("segments"), "error_codes": [e["code"] for e in tr["errors"]],
        } for tr in result.get("trend_results", [])],
        "evidence_ids": [e["evidence_id"] for e in result.get("evidence", [])],
        "input_provenance": result.get("input_provenance"),
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Build the deterministic synthetic Stock fixture DuckDB.")
    ap.add_argument("out", help="output .duckdb path (refused if it exists unless --force)")
    ap.add_argument("--force", action="store_true", help="replace an existing file")
    ap.add_argument("--storage-version", default=DEFAULT_STORAGE_VERSION,
                    help="DuckDB storage version to write (default v1.0.0; '' = DuckDB default)")
    args = ap.parse_args(argv)
    try:
        report = build_fixture_db(args.out, storage_version=args.storage_version or None, overwrite=args.force)
    except FileExistsError:
        print(f"거부: {args.out}가 이미 있습니다 (--force로 바꿉니다).", file=sys.stderr)
        return 2
    print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
    return 0 if report["db_ready"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
