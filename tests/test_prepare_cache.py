"""python -m fashion_planner_inventory.server --prepare-cache — 가짜 R2 client로만 (네트워크·실제 R2 없음)."""
import hashlib
import json
from io import BytesIO

import pytest

duckdb = pytest.importorskip("duckdb")

from fashion_planner_inventory import server, storage  # noqa: E402
from test_runtime_modes import ready_db_file  # noqa: E402

SECRETS = ("synthetic-access-key-id", "synthetic-secret-access-key", "synthetic-bucket", "synthetic-account",
           "db/stock.duckdb")


class FakeR2:
    def __init__(self, payload: bytes, etag: str = '"v1"'):
        self.payload, self.etag, self.downloads = payload, etag, 0

    def head_object(self, **kwargs):
        return {"ETag": self.etag, "ContentLength": len(self.payload)}

    def get_object(self, **kwargs):
        self.downloads += 1
        return {"Body": BytesIO(self.payload), "ContentLength": len(self.payload)}


@pytest.fixture
def r2_env(monkeypatch, tmp_path):
    monkeypatch.setattr("fashion_planner_inventory.env.read_dotenv", lambda *args, **kwargs: None)
    for name, value in {"R2_ACCOUNT_ID": "synthetic-account", "R2_BUCKET": "synthetic-bucket",
                        "R2_DB_KEY": "db/stock.duckdb", "R2_ACCESS_KEY_ID": "synthetic-access-key-id",
                        "R2_SECRET_ACCESS_KEY": "synthetic-secret-access-key",
                        "INVENTORY_CACHE_DIR": str(tmp_path / "cache")}.items():
        monkeypatch.setenv(name, value)
    for name in ("R2_ENDPOINT", "R2_IMAGE_PREFIX", "R2_CLIP_INDEX_KEY", "R2_SNAP_IMAGE_PREFIX", "INVENTORY_DB_PATH",
                 "INVENTORY_CLIP_INDEX_PATH"):
        monkeypatch.delenv(name, raising=False)
    return monkeypatch


def _run(capsys):
    code = server.main(["--prepare-cache"])
    out = capsys.readouterr().out
    return code, json.loads(out.strip().splitlines()[-1]), out


def test_prepare_cache_downloads_once_writes_sidecar_and_reports_versions(r2_env, con, tmp_path, capsys):
    payload = ready_db_file(con, tmp_path / "source.duckdb").read_bytes()
    client = FakeR2(payload)
    r2_env.setattr(storage, "_client", lambda settings: client)
    code, report, out = _run(capsys)
    assert code == 0 and report["status"] == "ready" and client.downloads == 1
    identity = hashlib.sha256(b"synthetic-bucket/db/stock.duckdb/v1").hexdigest()
    cache = tmp_path / "cache"
    assert sorted(p.name for p in cache.iterdir()) == [f"inventory-{identity}.duckdb", f"inventory-{identity}.sha256"]
    assert (cache / f"inventory-{identity}.sha256").read_text() == hashlib.sha256(payload).hexdigest() == report["sha256"]
    assert report["sha256_sidecar"] is True and report["db_version"] == "v1" and report["size_bytes"] == len(payload)
    assert report["duckdb_version"] == duckdb.__version__ and report["readiness"]["db_ready"] is True
    assert "storage_version" in report and report["header_version_number"] is not None
    assert not any(secret in out for secret in SECRETS)
    # A second run reuses the verified cache version without downloading again.
    code, report, _ = _run(capsys)
    assert code == 0 and report["status"] == "ready" and client.downloads == 1
    # The prepared cache is what local-file mode opens; its sidecar is verified there too.
    path, version = storage.open_local_database(report["cache_path"])
    assert version == "sha256:" + hashlib.sha256(payload).hexdigest()


@pytest.mark.parametrize("kind", ["empty_duckdb", "not_duckdb"])
def test_prepare_cache_exits_non_zero_unless_the_db_is_ready(r2_env, tmp_path, capsys, kind):
    if kind == "empty_duckdb":                           # opens, but has none of the required tables
        empty = tmp_path / "empty.duckdb"
        duckdb.connect(str(empty)).close()
        payload = empty.read_bytes()
    else:
        payload = b"not a duckdb file " * 64
    r2_env.setattr(storage, "_client", lambda settings: FakeR2(payload))
    code, report, out = _run(capsys)
    assert code == 1
    if kind == "empty_duckdb":
        assert report["status"] == "not_ready" and report["readiness"]["reason"] == "schema_incompatible"
    else:
        assert report["status"] == "error" and report["reason"] == "database_open_failed"
    assert not any(secret in out for secret in SECRETS)


def test_prepare_cache_without_r2_settings_fails_before_any_client(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr("fashion_planner_inventory.env.read_dotenv", lambda *args, **kwargs: None)
    for name in ("R2_ACCOUNT_ID", "R2_BUCKET", "R2_DB_KEY", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY",
                 "Access Key ID", "Secret Access Key"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("INVENTORY_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setattr(storage, "_client", lambda settings: pytest.fail("must not build an R2 client"))
    code, report, _ = _run(capsys)
    assert code == 1 and report["reason"] == "StorageConfigurationError" and report["status"] == "error"


# ---- CLIP 인덱스 (R2_CLIP_INDEX_KEY)
class FakeR2Objects:
    """key별로 다른 객체를 돌려주는 가짜 R2."""

    def __init__(self, objects: dict):
        self.objects, self.downloads = objects, []

    def head_object(self, Bucket, Key):
        return {"ETag": f'"etag-{len(self.objects[Key])}"', "ContentLength": len(self.objects[Key])}

    def get_object(self, Bucket, Key, **kwargs):
        self.downloads.append(Key)
        return {"Body": BytesIO(self.objects[Key]), "ContentLength": len(self.objects[Key])}


def _index_bytes(tmp_path, rule):
    import numpy as np
    from fashion_planner_inventory.clip_matcher import INDEX_RULE
    path = tmp_path / "idx.npz"
    np.savez(path, article_ids=np.array(["a1", "a2"]), embeddings=np.zeros((2, 4), dtype=np.float16),
             rule=np.array(rule or INDEX_RULE))
    return path.read_bytes()


@pytest.mark.parametrize("rule, ok", [(None, True), ("color-image", False)])
def test_prepare_cache_also_fetches_clip_index(r2_env, con, tmp_path, capsys, rule, ok):
    db = ready_db_file(con, tmp_path / "source.duckdb").read_bytes()
    client = FakeR2Objects({"db/stock.duckdb": db, "clip/clip_index.npz": _index_bytes(tmp_path, rule)})
    r2_env.setenv("R2_CLIP_INDEX_KEY", "clip/clip_index.npz")
    r2_env.setattr(storage, "_client", lambda settings: client)
    code, report, out = _run(capsys)
    idx = report["clip_index"]
    assert client.downloads == ["db/stock.duckdb", "clip/clip_index.npz"]
    assert (code == 0) is ok and (report["status"] == "ready") is ok
    if ok:
        assert idx["status"] == "ready" and idx["n_indexed"] == 2 and idx["sha256"]
        assert any(p.name.startswith("clip-index-") and p.suffix == ".npz" for p in (tmp_path / "cache").iterdir())
    else:
        assert idx["reason"] == "clip_index_rule_mismatch" and idx["rule"] == "color-image"
    assert "clip/clip_index.npz" not in out


def test_prepare_cache_without_clip_key_skips_index(r2_env, con, tmp_path, capsys):
    client = FakeR2Objects({"db/stock.duckdb": ready_db_file(con, tmp_path / "source.duckdb").read_bytes()})
    r2_env.delenv("R2_CLIP_INDEX_KEY", raising=False)
    r2_env.setattr(storage, "_client", lambda settings: client)
    code, report, _ = _run(capsys)
    assert code == 0 and "clip_index" not in report and client.downloads == ["db/stock.duckdb"]


# ---- 무신사 스냅 착샷 (R2_SNAP_IMAGE_PREFIX)
def test_fetch_snap_image_reads_revision_row_key(r2_env):
    keys = []

    class Snaps(FakeR2):
        def get_object(self, **kwargs):
            keys.append(kwargs["Key"])
            return super().get_object(**kwargs)
    ref = "hf://datasets/disl/musinsa-snap-new@4335d233ef3b54bc5132a4d572d589042a41219d#row=7"
    r2_env.setattr(storage, "_client", lambda settings: Snaps(b"jpeg"))
    assert storage.R2ObjectStore(storage.R2Settings.from_env()).fetch_snap_image(ref) is None   # prefix 없음
    r2_env.setenv("R2_SNAP_IMAGE_PREFIX", "/musinsa-snap/")
    s = storage.R2ObjectStore(storage.R2Settings.from_env())
    assert s.fetch_snap_image(ref) == b"jpeg" and s.fetch_snap_image("https://x/y.jpg") is None
    assert keys == ["musinsa-snap/4335d233ef3b54bc5132a4d572d589042a41219d/7.jpg"]
