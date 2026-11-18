"""Safe read-only diagnosis; no dotenv loading, DB download, build or upload.

Run from inventory: python scripts/diagnose_readiness.py
Only key presence, aggregate metadata and failure classes are printed.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
from datetime import datetime, timezone

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def main():
    from fashion_planner_inventory.storage import R2Settings, file_sha256
    names = ("FASHION_PLANNER_SERVICE_TOKEN", "R2_ACCOUNT_ID",
             "R2_BUCKET", "R2_DB_KEY", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY", "R2_ENDPOINT",
             "R2_IMAGE_PREFIX", "R2_CLIP_INDEX_KEY", "R2_SNAP_IMAGE_PREFIX", "INVENTORY_CACHE_DIR", "INVENTORY_DB_PATH", "INVENTORY_CLIP_INDEX_PATH",
             "Access Key ID", "Secret Access Key")
    result = {"observed_at": datetime.now(timezone.utc).isoformat(),
              "dotenv_exists": Path(".env").is_file(), "dotenv_loaded_by_probe": False,
              "process_env_present": {key: bool(os.getenv(key)) for key in names},
              "dependencies": {key: bool(importlib.util.find_spec(key)) for key in
                               ("duckdb", "fastapi", "httpx", "jsonschema", "boto3", "uvicorn", "open_clip", "torch")},
              "r2": {"configured": False, "reachable": None, "authenticated": None},
              "database": {"data_verified": False, "as_of": None, "synthetic": None},
              "downloads": 0, "uploads": 0, "db_writes": 0}
    cache = Path(os.environ.get("INVENTORY_CACHE_DIR", Path(tempfile.gettempdir()) / "inventory"))
    result["existing_cache_files"] = len(list(cache.glob("stock-*.duckdb"))) if cache.is_dir() else 0
    try:
        settings = R2Settings.from_env()
        result["r2"]["configured"] = True
        if not result["dependencies"]["boto3"]:
            result["r2"]["reason"] = "boto3_not_installed"
        else:
            from fashion_planner_inventory.storage import _client
            head = _client(settings).head_object(Bucket=settings.bucket, Key=settings.db_key)
            version = str(head.get("VersionId") or str(head.get("ETag", "")).strip('"'))
            result["r2"].update(reachable=True, authenticated=True, version=version,
                                etag=head.get("ETag"), size_bytes=head.get("ContentLength"),
                                last_modified=str(head.get("LastModified")), upstream_checksum_verified=False)
            identity = hashlib.sha256(f"{settings.bucket}/{settings.db_key}/{version}".encode()).hexdigest()
            path = cache / f"inventory-{identity}.duckdb"
            if not path.is_file():
                result["database"]["reason"] = "matching_existing_cache_missing_no_download"
            else:
                checksum = file_sha256(path)
                sidecar = path.with_suffix(".sha256")
                result["database"].update(size_bytes=path.stat().st_size, sha256=checksum,
                                          local_checksum_matches=sidecar.is_file() and sidecar.read_text().strip() == checksum)
                if result["dependencies"]["duckdb"]:
                    import duckdb
                    from fashion_planner_inventory.readiness import inspect_database
                    con = duckdb.connect(str(path), read_only=True)
                    try:
                        inspection = inspect_database(con)
                        result["database"].update(inspection, readonly_opened=True)
                        if inspection["db_ready"]:
                            result["database"]["as_of"] = inspection["snapshot"]["as_of"]
                            result["database"]["synthetic"] = True
                            result["database"]["data_verified"] = bool(result["database"]["local_checksum_matches"])
                    finally:
                        con.close()
    except Exception as exc:
        result["failure_class"] = type(exc).__name__
        result["r2"]["reason"] = type(exc).__name__
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
