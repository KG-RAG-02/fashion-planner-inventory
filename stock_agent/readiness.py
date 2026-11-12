"""Read-only schema and snapshot checks for the historical sample adapter.

This adapter cannot serve arbitrary/current company inventory: its ranking and
serializer are explicitly based on the virtual_stock_v1 snapshot.
"""
from __future__ import annotations

from stock_agent.synthetic import AS_OF, VERSION

REQUIRED_COLUMNS = {
    "articles": {"article_id": "text"},
    "transactions": {"article_id": "text", "t_dat": "date"},
    "article_attrs": {"article_id": "text", "segment": "text", "kf_area": "text",
                      "kf_category": "text", "kf_color": "text", "materials": "list",
                      "fit": "text", "season_fit": "list"},
    "stock_v1": {"article_id": "text", "stock_qty": "integer", "price_krw": "integer",
                 "stock_value_krw": "integer", "sell_through": "number", "n_sold": "integer",
                 "as_of": "date", "version": "text", "data_nature": "text"},
}


def _compatible(actual: str, kind: str) -> bool:
    actual = actual.upper()
    if kind == "text":
        return actual == "VARCHAR"
    if kind == "date":
        return actual == "DATE"
    if kind == "list":
        return actual == "VARCHAR[]"
    if kind == "integer":
        return actual in {"TINYINT", "SMALLINT", "INTEGER", "BIGINT", "HUGEINT",
                          "UTINYINT", "USMALLINT", "UINTEGER", "UBIGINT", "UHUGEINT"}
    return any(actual.startswith(t) for t in (
        "TINYINT", "SMALLINT", "INTEGER", "BIGINT", "HUGEINT", "UTINYINT", "USMALLINT",
        "UINTEGER", "UBIGINT", "UHUGEINT", "FLOAT", "DOUBLE", "DECIMAL"))


def inspect_database(con) -> dict:
    """Aggregate metadata only. Never creates tables, installs extensions or writes."""
    columns = con.execute("""SELECT table_name, column_name, data_type
        FROM information_schema.columns WHERE table_schema = 'main'
        AND table_catalog = current_database()""").fetchall()
    schema: dict[str, dict[str, str]] = {}
    for table, column, dtype in columns:
        schema.setdefault(table, {})[column] = dtype
    missing_tables = sorted(set(REQUIRED_COLUMNS) - set(schema))
    missing_columns, incompatible_columns = {}, {}
    for table, expected in REQUIRED_COLUMNS.items():
        if table not in schema:
            continue
        missing = sorted(set(expected) - set(schema[table]))
        wrong = sorted(c for c, kind in expected.items()
                       if c in schema[table] and not _compatible(schema[table][c], kind))
        if missing:
            missing_columns[table] = missing
        if wrong:
            incompatible_columns[table] = wrong
    result = {"db_ready": False, "missing_tables": missing_tables,
              "missing_columns": missing_columns, "incompatible_columns": incompatible_columns,
              "schema": {t: schema.get(t, {}) for t in REQUIRED_COLUMNS},
              "snapshot": None, "reason": "schema_incompatible"}
    if missing_tables or missing_columns or incompatible_columns:
        return result
    empty = [t for t in REQUIRED_COLUMNS
             if con.execute(f'SELECT 1 FROM "{t}" LIMIT 1').fetchone() is None]
    if empty:
        return {**result, "reason": "empty_tables", "empty_tables": empty}
    profiles = con.execute("SELECT DISTINCT as_of, version, data_nature FROM stock_v1 LIMIT 2").fetchall()
    if profiles != [(AS_OF, VERSION, "sample")]:
        return {**result, "reason": "snapshot_incompatible"}
    invalid = con.execute("""SELECT 1 FROM stock_v1 WHERE stock_qty IS NULL OR price_krw IS NULL
        OR stock_value_krw IS NULL OR sell_through IS NULL OR n_sold IS NULL
        OR stock_qty < 0 OR price_krw < 0 OR n_sold < 0 OR sell_through < 0 OR sell_through > 1
        OR NOT isfinite(sell_through) OR stock_value_krw != stock_qty::HUGEINT * price_krw
        LIMIT 1""").fetchone()
    if invalid:
        return {**result, "reason": "invalid_inventory_values"}
    return {**result, "db_ready": True, "reason": None,
            "snapshot": {"as_of": AS_OF.isoformat(), "version": VERSION, "data_nature": "sample",
                         "synthetic": True, "source": "H&M historical data + virtual_stock_v1",
                         "current_company_inventory_verified": False}}
