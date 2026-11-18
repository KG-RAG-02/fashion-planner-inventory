"""Offline tests for R2 config parsing and atomic versioned object caching."""

from io import BytesIO

import pytest

from fashion_planner_inventory.storage import R2ObjectStore, R2Settings, StorageConfigurationError, StorageUnavailable


class FakeR2:
    def __init__(self, payload=b"duckdb-image", etag='"v1"'):
        self.payload = payload
        self.etag = etag
        self.downloads = 0

    def head_object(self, **kwargs):
        if kwargs["Key"].endswith("0000000000.jpg"):
            raise FakeNotFound()
        return {"ETag": self.etag, "ContentLength": len(self.payload)}

    def get_object(self, **kwargs):
        self.downloads += 1
        return {"Body": BytesIO(self.payload), "ContentLength": len(self.payload)}


class FakeNotFound(Exception):
    response = {"ResponseMetadata": {"HTTPStatusCode": 404}, "Error": {"Code": "NoSuchKey"}}


@pytest.fixture
def settings():
    return R2Settings("account", "stock", "db/stock.duckdb", "id-secret", "key-secret", "images")


def test_settings_requires_r2_location_and_credentials(monkeypatch):
    monkeypatch.setenv("R2_ENDPOINT", "")
    monkeypatch.setenv("R2_IMAGE_PREFIX", "")
    monkeypatch.setenv("R2_ACCOUNT_ID", "account")
    monkeypatch.setenv("R2_BUCKET", "stock")
    monkeypatch.setenv("R2_DB_KEY", "db/stock.duckdb")
    monkeypatch.setenv("R2_ACCESS_KEY_ID", "key-id")
    monkeypatch.setenv("R2_SECRET_ACCESS_KEY", "secret")
    assert R2Settings.from_env().endpoint_url == "https://account.r2.cloudflarestorage.com"
    monkeypatch.delenv("R2_BUCKET")
    with pytest.raises(StorageConfigurationError, match="R2_BUCKET"):
        R2Settings.from_env()


def test_settings_repr_does_not_include_credentials(settings):
    assert "id-secret" not in repr(settings)
    assert "key-secret" not in repr(settings)


def test_database_download_is_cached_by_etag_and_atomically_complete(tmp_path, settings):
    client = FakeR2()
    store = R2ObjectStore(settings, client=client, cache_dir=tmp_path)
    path, version = store.prepare_database()
    assert path.read_bytes() == client.payload
    assert version == "v1" and client.downloads == 1
    assert not list(tmp_path.glob("*.part"))
    again, same_version = store.prepare_database()
    assert again == path and same_version == version and client.downloads == 1


def test_database_changed_etag_creates_new_cache_version(tmp_path, settings):
    client = FakeR2(payload=b"new-db", etag='"v2"')
    store = R2ObjectStore(settings, client=client, cache_dir=tmp_path)
    path, version = store.prepare_database()
    assert path.read_bytes() == b"new-db" and version == "v2"
    assert client.downloads == 1


def test_image_objects_are_checked_and_cached_lazily(tmp_path, settings):
    client = FakeR2(payload=b"jpeg")
    store = R2ObjectStore(settings, client=client, cache_dir=tmp_path)
    assert store.images_available
    assert store.object_key_for_image("0999991001") == "images/099/0999991001.jpg"
    assert store.image_exists("0999990001")
    assert store.load_image("0999990001") == b"jpeg"
    assert store.load_image("0999990001") == b"jpeg"
    assert client.downloads == 1
    assert not store.image_exists("0000000000")


def test_image_key_rejects_non_numeric_article_ids(tmp_path, settings):
    store = R2ObjectStore(settings, client=FakeR2(), cache_dir=tmp_path)
    with pytest.raises(ValueError):
        store.object_key_for_image("../secret")


def test_bad_download_size_does_not_leave_partial_file(tmp_path, settings):
    class BadSize(FakeR2):
        def get_object(self, **kwargs):
            self.downloads += 1
            return {"Body": BytesIO(b"short"), "ContentLength": 99}

    client = BadSize()
    store = R2ObjectStore(settings, client=client, cache_dir=tmp_path)
    with pytest.raises(StorageUnavailable):
        store.prepare_database(attempts=1)
    assert not list(tmp_path.glob("*.duckdb"))
    assert not list(tmp_path.glob("*.part"))


def test_cache_only_never_downloads_and_detects_same_size_corruption(tmp_path, settings):
    client = FakeR2()
    store = R2ObjectStore(settings, client=client, cache_dir=tmp_path)
    with pytest.raises(StorageUnavailable):
        store.prepare_database(attempts=1, allow_download=False)
    assert client.downloads == 0
    path, _ = store.prepare_database()  # Fake bytes only.
    assert store.database_metadata["sha256"]
    store.prepare_database(attempts=1, allow_download=False)
    path.write_bytes(b"x" * len(client.payload))
    with pytest.raises(StorageUnavailable):
        store.prepare_database(attempts=1, allow_download=False)
    assert client.downloads == 1


def test_image_denied_is_not_missing(tmp_path, settings):
    class Denied(Exception):
        response = {"ResponseMetadata": {"HTTPStatusCode": 403}}
    class DeniedR2(FakeR2):
        def head_object(self, **kwargs):
            raise Denied()
    store = R2ObjectStore(settings, client=DeniedR2(), cache_dir=tmp_path)
    with pytest.raises(StorageUnavailable):
        store.image_exists("0999990001")


def test_image_cache_dir_defaults_under_cache_dir_and_follows_env(tmp_path, settings, monkeypatch):
    monkeypatch.delenv("STOCK_IMAGE_CACHE_DIR", raising=False)
    assert R2ObjectStore(settings, client=FakeR2(), cache_dir=tmp_path / "cache").image_cache_dir == tmp_path / "cache" / "images"
    monkeypatch.setenv("STOCK_IMAGE_CACHE_DIR", str(tmp_path / "image-cache"))
    client = FakeR2(payload=b"jpeg")
    store = R2ObjectStore(settings, client=client, cache_dir=tmp_path / "cache")
    assert store.load_image("0999990001") == b"jpeg" and store.load_image("0999990001") == b"jpeg"
    assert client.downloads == 1 and len(list((tmp_path / "image-cache").glob("*.jpg"))) == 1
    assert store.take_image_notes() == []


def test_unwritable_image_cache_serves_uncached_bytes_with_a_note(tmp_path, settings):
    blocker = tmp_path / "not-a-directory"
    blocker.write_text("x")                      # mkdir under a regular file fails even for root
    client = FakeR2(payload=b"jpeg")
    store = R2ObjectStore(settings, client=client, cache_dir=tmp_path / "cache", image_cache_dir=blocker / "images")
    assert store.load_image("0999990001") == b"jpeg"
    assert store.load_image("0999990001") == b"jpeg"
    assert client.downloads == 2                 # nothing could be cached, nothing raised
    assert store.take_image_notes() == [R2ObjectStore.IMAGE_CACHE_NOTE]
    assert store.take_image_notes() == []
    assert not list(tmp_path.rglob("*.part"))
