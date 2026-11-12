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


def _tag_map(value) -> dict:
    """DuckDB MAP → dict (older Python clients return {"key": [...], "value": [...]})."""
    if isinstance(value, dict) and set(value) == {"key", "value"} and isinstance(value["key"], list):
        return dict(zip(value["key"], value["value"]))
    return value if isinstance(value, dict) else {}


def database_storage_info(con=None, path=None) -> dict:
    """DuckDB file-format facts, read-only and best effort.

    storage_version: DuckDB's tag for the open file (``duckdb_databases().tags``; DuckDB >= 1.2).
    header_version_number / created_by_library: the file's main header (format number and the
    DuckDB release that created it). A DuckDB older than the file's storage version cannot open it.
    """
    info = {"storage_version": None, "header_version_number": None, "created_by_library": None}
    if con is not None:
        try:
            row = con.execute("SELECT tags FROM duckdb_databases() "
                              "WHERE database_name = current_database()").fetchone()
            info["storage_version"] = _tag_map(row[0] if row else None).get("storage_version")
        except Exception:
            pass
    if path is not None:
        try:
            with open(path, "rb") as source:
                head = source.read(116)
        except OSError:
            head = b""
        if len(head) == 116 and head[8:12] == b"DUCK":
            info["header_version_number"] = int.from_bytes(head[12:20], "little")
            info["created_by_library"] = head[52:84].split(b"\0", 1)[0].decode("ascii", "replace") or None
    return info


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
