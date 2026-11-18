"""Start/check the inventory service using the packaged service manifest.

  python -m fashion_planner_inventory.server [--host 0.0.0.0]   run the API (port from service-manifest.json)
  python -m fashion_planner_inventory.server --check-ready      exit 0 when GET /health/ready answers 200
  python -m fashion_planner_inventory.server --prepare-cache    data job: download the configured R2 DB (and the CLIP
                                                  index when R2_CLIP_INDEX_KEY is set) into
                                                  INVENTORY_CACHE_DIR (checksummed), inspect it, report
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
    """Download the configured R2 DB (and CLIP index) once, then inspect the cache files read-only.

    The API never downloads at startup; this is the separate data job that fills its cache.
    Writes ``inventory-<sha256(bucket/key/version)>.duckdb`` plus the ``.sha256`` sidecar into
    ``INVENTORY_CACHE_DIR`` (an already verified version is reused), prints one JSON report
    (DuckDB library version, the file's storage version, readiness) without credentials,
    endpoint, bucket or key, and returns 0 only when the database is ready.
    With ``R2_CLIP_INDEX_KEY`` it also caches ``clip-index-<sha256>.npz`` and reports ``clip_index``
    (version, size, loaded, n_indexed); a missing or unreadable index fails the job.
    """
    import duckdb

    from fashion_planner_inventory.env import read_dotenv
    from fashion_planner_inventory.readiness import database_storage_info, inspect_database
    from fashion_planner_inventory.storage import R2ObjectStore, R2Settings, StorageConfigurationError, StorageUnavailable

    read_dotenv()      # same configuration source as the API; process env wins
    report: dict = {"command": "prepare-cache", "status": "error", "duckdb_version": duckdb.__version__}
    try:
        store = R2ObjectStore(R2Settings.from_env())
        path, version = store.prepare_database(allow_download=True)
    except (StorageConfigurationError, StorageUnavailable) as exc:
        return _report({**report, "reason": type(exc).__name__, "message": str(exc)}, 1)
    except OSError as exc:   # INVENTORY_CACHE_DIR cannot be created
        return _report({**report, "reason": type(exc).__name__, "message": "INVENTORY_CACHE_DIR를 만들거나 쓸 수 없습니다"}, 1)
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
    clip_ok = True
    if store.settings.clip_index_key:
        report["clip_index"], clip_ok = _prepare_clip_index(store)
    ready = readiness["db_ready"] and clip_ok
    report["status"] = "ready" if ready else "not_ready"
    return _report(report, 0 if ready else 1)


def _prepare_clip_index(store) -> tuple[dict, bool]:
    """R2 CLIP 인덱스를 캐시에 받고 규칙이 맞는지 열어 본다."""
    from fashion_planner_inventory.storage import StorageUnavailable
    try:
        path, version = store.prepare_clip_index(allow_download=True)
    except (StorageUnavailable, OSError) as exc:
        return {"status": "error", "reason": type(exc).__name__, "message": str(exc)}, False
    info = {"version": version, "size_bytes": path.stat().st_size, "sha256": store.clip_index_metadata.get("sha256")}
    try:
        import numpy as np
        from fashion_planner_inventory.clip_matcher import INDEX_RULE
        with np.load(path, allow_pickle=False) as data:
            rule = str(data["rule"]) if "rule" in data.files else None
            n = int(len(data["article_ids"]))
    except Exception as exc:
        return {**info, "status": "error", "reason": "clip_index_open_failed", "message": type(exc).__name__}, False
    if rule != INDEX_RULE:
        return {**info, "status": "error", "reason": "clip_index_rule_mismatch", "rule": rule,
                "expected_rule": INDEX_RULE}, False
    return {**info, "status": "ready", "rule": rule, "n_indexed": n}, True


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
    uvicorn.run("fashion_planner_inventory.api:app", host=args.host, port=config["port"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
