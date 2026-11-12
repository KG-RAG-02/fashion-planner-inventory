"""Start/check Stock using the packaged service manifest.

  python -m stock_agent.server [--host 0.0.0.0]   run the API (port from service-manifest.json)
  python -m stock_agent.server --check-ready      exit 0 when GET /health/ready answers 200
  python -m stock_agent.server --prepare-cache    data job: download the configured R2 DB into
                                                  STOCK_CACHE_DIR (checksummed), inspect it, report
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from urllib.request import ProxyHandler, build_opener


def manifest():
    return json.loads(Path(__file__).with_name("service-manifest.json").read_text())


def _report(report: dict, code: int) -> int:
    print(json.dumps(report, ensure_ascii=False, default=str))
    return code


def prepare_cache() -> int:
    """Download the configured R2 DB once, then inspect the cache file read-only.

    The API never downloads at startup; this is the separate data job that fills its cache.
    Writes ``stock-<sha256(bucket/key/version)>.duckdb`` plus the ``.sha256`` sidecar into
    ``STOCK_CACHE_DIR`` (an already verified version is reused), prints one JSON report
    (DuckDB library version, the file's storage version, readiness) without credentials,
    endpoint, bucket or key, and returns 0 only when the database is ready.
    """
    import duckdb

    from stock_agent.llm import read_dotenv
    from stock_agent.readiness import database_storage_info, inspect_database
    from stock_agent.storage import R2ObjectStore, R2Settings, StorageConfigurationError, StorageUnavailable

    read_dotenv()      # same configuration source as the API; process env wins
    report: dict = {"command": "prepare-cache", "status": "error", "duckdb_version": duckdb.__version__}
    try:
        store = R2ObjectStore(R2Settings.from_env())
        path, version = store.prepare_database(allow_download=True)
    except (StorageConfigurationError, StorageUnavailable) as exc:
        return _report({**report, "reason": type(exc).__name__, "message": str(exc)}, 1)
    except OSError as exc:   # STOCK_CACHE_DIR cannot be created
        return _report({**report, "reason": type(exc).__name__, "message": "STOCK_CACHE_DIR를 만들거나 쓸 수 없습니다"}, 1)
    report.update(db_version=version, cache_path=str(path), size_bytes=path.stat().st_size,
                  sha256=store.database_metadata.get("sha256"), sha256_sidecar=path.with_suffix(".sha256").is_file())
    try:
        con = duckdb.connect(str(path), read_only=True)
    except duckdb.Error as exc:
        first_line = (str(exc).splitlines() or [""])[0][:300]
        return _report({**report, **database_storage_info(None, path), "reason": "database_open_failed",
                        "message": f"{type(exc).__name__}: {first_line}"}, 1)
    try:
        report.update(database_storage_info(con, path))
        readiness = inspect_database(con)
    finally:
        con.close()
    report["readiness"] = {k: v for k, v in readiness.items() if k != "schema"}
    report["status"] = "ready" if readiness["db_ready"] else "not_ready"
    return _report(report, 0 if readiness["db_ready"] else 1)


def main(argv: list[str] | None = None) -> int:
    config = manifest()
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--host", default=config["host_bind"])
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--check-ready", action="store_true")
    action.add_argument("--prepare-cache", action="store_true",
                        help="download and verify the R2 DB cache (needs R2 settings), then exit")
    args = parser.parse_args(argv)
    if args.prepare_cache:
        return prepare_cache()
    if args.check_ready:
        try:
            with build_opener(ProxyHandler({})).open(
                    f'http://127.0.0.1:{config["port"]}{config["readiness_path"]}', timeout=4) as response:
                return 0 if response.status == 200 else 1
        except Exception:
            return 1
    import uvicorn
    uvicorn.run("stock_agent.api:app", host=args.host, port=config["port"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
