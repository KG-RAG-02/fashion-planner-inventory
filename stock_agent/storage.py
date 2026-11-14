"""Cloudflare R2 object access for the stock database and product images.

R2 uses its S3-compatible API. Credentials are read from environment variables;
the historical ``Access Key ID`` / ``Secret Access Key`` dotenv names are
accepted temporarily so existing local setups can be migrated without copying
secret values into source files.
"""

from __future__ import annotations

import hashlib
import os
import tempfile
import time
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit


class StorageConfigurationError(RuntimeError):
    """Required non-secret R2 configuration is missing or invalid."""


class StorageUnavailable(RuntimeError):
    """R2 could not provide a complete object."""


@dataclass(frozen=True)
class R2Settings:
    account_id: str
    bucket: str
    db_key: str
    access_key_id: str = field(repr=False)
    secret_access_key: str = field(repr=False)
    image_prefix: str = ""
    endpoint: str = ""
    clip_index_key: str = ""          # 선택: 재고 CLIP 인덱스(npz) 객체 key
    snap_image_prefix: str = ""       # 선택: 무신사 스냅 착샷 JPEG prefix (<prefix>/<revision>/<row>.jpg)

    @property
    def endpoint_url(self) -> str:
        return self.endpoint or f"https://{self.account_id}.r2.cloudflarestorage.com"

    @classmethod
    def from_env(cls) -> "R2Settings":
        values = {
            "R2_ACCOUNT_ID": os.environ.get("R2_ACCOUNT_ID", "").strip(),
            "R2_BUCKET": os.environ.get("R2_BUCKET", "").strip(),
            "R2_DB_KEY": os.environ.get("R2_DB_KEY", "").strip(),
            "R2_ACCESS_KEY_ID": (os.environ.get("R2_ACCESS_KEY_ID", "")
                                 or os.environ.get("Access Key ID", "")).strip(),
            "R2_SECRET_ACCESS_KEY": (os.environ.get("R2_SECRET_ACCESS_KEY", "")
                                     or os.environ.get("Secret Access Key", "")).strip(),
        }
        missing = [name for name, value in values.items() if not value]
        if missing:
            raise StorageConfigurationError("필수 R2 설정 누락: " + ", ".join(missing))
        account_id = values["R2_ACCOUNT_ID"]
        # Keep endpoint construction predictable and prevent URL injection.
        if any(ch in account_id for ch in "/:@?#"):
            raise StorageConfigurationError("R2_ACCOUNT_ID 형식이 올바르지 않습니다")
        endpoint = os.environ.get("R2_ENDPOINT", "").strip().rstrip("/")
        if endpoint:
            parsed = urlsplit(endpoint)
            if parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.password or parsed.query or parsed.fragment:
                raise StorageConfigurationError("R2_ENDPOINT는 인증 정보 없이 HTTPS endpoint로 설정해야 합니다")
        return cls(
            account_id=values["R2_ACCOUNT_ID"],
            bucket=values["R2_BUCKET"],
            db_key=values["R2_DB_KEY"],
            access_key_id=values["R2_ACCESS_KEY_ID"],
            secret_access_key=values["R2_SECRET_ACCESS_KEY"],
            image_prefix=os.environ.get("R2_IMAGE_PREFIX", "").strip().strip("/"),
            endpoint=endpoint,
            clip_index_key=os.environ.get("R2_CLIP_INDEX_KEY", "").strip().lstrip("/"),
            snap_image_prefix=os.environ.get("R2_SNAP_IMAGE_PREFIX", "").strip().strip("/"),
        )


def _client(settings: R2Settings):
    try:
        import boto3
        from botocore.config import Config
    except ImportError as exc:  # pragma: no cover - depends on installation
        raise StorageConfigurationError("R2 연동 라이브러리가 설치되지 않았습니다") from exc
    return boto3.client(
        "s3",
        endpoint_url=settings.endpoint_url,
        region_name="auto",
        aws_access_key_id=settings.access_key_id,
        aws_secret_access_key=settings.secret_access_key,
        config=Config(connect_timeout=5, read_timeout=30,
                      retries={"max_attempts": 3, "mode": "standard"}),
    )


def _safe_cache_dir() -> Path:
    root = Path(os.environ.get("STOCK_CACHE_DIR", Path(tempfile.gettempdir()) / "stock-agent"))
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        root.chmod(0o700)
    except OSError:
        pass
    return root


def _image_cache_dir(cache_dir: Path) -> Path:
    """Where fetched R2 images are cached: ``STOCK_IMAGE_CACHE_DIR``, default ``<STOCK_CACHE_DIR>/images``."""
    configured = os.environ.get("STOCK_IMAGE_CACHE_DIR", "").strip()
    return Path(configured) if configured else Path(cache_dir) / "images"


def _read_stream(body: Any, target: Path) -> int:
    size = 0
    try:
        with target.open("wb") as out:
            while True:
                chunk = body.read(1024 * 1024)
                if not chunk:
                    break
                out.write(chunk)
                size += len(chunk)
            out.flush()
            os.fsync(out.fileno())
    finally:
        close = getattr(body, "close", None)
        if callable(close):
            close()
    return size


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def open_local_database(path_value: str | os.PathLike) -> tuple[Path, str]:
    """Local-file mode (``STOCK_DB_PATH``): no R2 settings, HEAD or download.

    The file must exist. If a ``<name>.sha256`` sidecar sits next to it (as written by
    ``python -m stock_agent.server --prepare-cache``), the file must match it. The version
    identifier is the file's SHA-256. The caller opens the file read-only.
    """
    path = Path(path_value).expanduser()
    if not path.is_file():
        raise StorageUnavailable("STOCK_DB_PATH가 가리키는 DB 파일이 없습니다")
    checksum = file_sha256(path)
    sidecar = path.with_suffix(".sha256")
    if sidecar.is_file() and sidecar.read_text().strip() != checksum:
        raise StorageUnavailable("STOCK_DB_PATH DB 파일이 SHA-256 sidecar와 일치하지 않습니다")
    return path, f"sha256:{checksum}"


def _write_checksum(target: Path) -> str:
    checksum = file_sha256(target)
    fd, name = tempfile.mkstemp(prefix=target.name, suffix=".part", dir=target.parent)
    try:
        with os.fdopen(fd, "w") as out:
            out.write(checksum)
            out.flush()
            os.fsync(out.fileno())
        os.replace(name, target.with_suffix(".sha256"))
    finally:
        Path(name).unlink(missing_ok=True)
    return checksum


class R2ObjectStore:
    """Small read-only adapter; pass a fake client to exercise it offline."""

    IMAGE_CACHE_NOTE = ("이미지 캐시 디렉터리(STOCK_IMAGE_CACHE_DIR, 기본 STOCK_CACHE_DIR/images)에 쓸 수 없어 "
                        "상품 사진을 캐시하지 않고 R2에서 받은 그대로 응답에 넣었습니다.")

    def __init__(self, settings: R2Settings, client=None, cache_dir: Path | None = None,
                 image_cache_dir: Path | None = None):
        self.settings = settings
        self.client = client if client is not None else _client(settings)
        self.cache_dir = Path(cache_dir) if cache_dir is not None else _safe_cache_dir()
        self.cache_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        # Created lazily: a read-only image cache only disables caching (see load_image).
        self.image_cache_dir = Path(image_cache_dir) if image_cache_dir is not None else _image_cache_dir(self.cache_dir)
        self._image_presence: dict[str, tuple[float, bool]] = {}
        self._image_lock = threading.Lock()
        self._image_notes: list[str] = []
        self.database_metadata: dict = {}
        self.clip_index_metadata: dict = {}

    @property
    def images_configured(self) -> bool:
        return bool(self.settings.image_prefix)

    @property
    def images_available(self) -> bool:
        # This is configuration readiness only; object availability is checked lazily.
        return self.images_configured

    def object_key_for_image(self, article_id: str) -> str:
        if not article_id.isdigit():
            raise ValueError("article_id 형식이 올바르지 않습니다")
        suffix = f"{article_id[:3]}/{article_id}.jpg"
        return f"{self.settings.image_prefix}/{suffix}" if self.settings.image_prefix else suffix

    def image_exists(self, article_id: str) -> bool:
        key = self.object_key_for_image(article_id)
        now = time.monotonic()
        with self._image_lock:
            cached = self._image_presence.get(key)
            if cached and cached[0] > now:
                return cached[1]
        try:
            self.client.head_object(Bucket=self.settings.bucket,
                                    Key=key)
            result = True
        except Exception as exc:
            response = getattr(exc, "response", {})
            code = response.get("ResponseMetadata", {}).get("HTTPStatusCode")
            error_code = response.get("Error", {}).get("Code")
            if code == 404 or error_code in {"404", "NoSuchKey", "NotFound"}:
                result = False
            else:
                # A denied/unreachable source is not proof that the image is absent.
                raise StorageUnavailable("R2 이미지 존재 여부를 확인하지 못했습니다") from None
        with self._image_lock:
            self._image_presence[key] = (time.monotonic() + 30, result)
        return result

    def take_image_notes(self) -> list[str]:
        """Limitation sentences left by image loading since the last call (e.g. caching disabled)."""
        with self._image_lock:
            notes, self._image_notes = self._image_notes, []
        return notes

    def load_image(self, article_id: str) -> bytes | None:
        """Image bytes from the local cache or R2 (None when the object does not exist).

        Caching is best effort: when the image cache directory is not writable (read-only mount,
        full disk) the fetched bytes are still returned and a note is left for take_image_notes().
        """
        key = self.object_key_for_image(article_id)
        cached = self.image_cache_dir / (hashlib.sha256(key.encode()).hexdigest() + ".jpg")
        try:
            if cached.is_file():
                return cached.read_bytes()
        except OSError:
            pass  # unreadable cache entry: fetch again
        raw = self._fetch_image(key)
        if raw is not None and not self._cache_image(cached, raw):
            with self._image_lock:
                if self.IMAGE_CACHE_NOTE not in self._image_notes:
                    self._image_notes.append(self.IMAGE_CACHE_NOTE)
        return raw

    def fetch_image(self, article_id: str) -> bytes | None:
        """R2에서 이미지 1장을 받는다(캐시에 저장하지 않음). 없으면 None. CLIP 인덱스 만들기용."""
        return self._fetch_image(self.object_key_for_image(article_id))

    def fetch_snap_image(self, image_ref: str) -> bytes | None:
        """무신사 스냅 착샷 image_ref(hf://…musinsa-snap-new@<rev>#row=<n>) → R2 <snap prefix>/<rev>/<n>.jpg.

        prefix가 없거나 참조를 해석할 수 없거나 객체가 없으면(다른 버전 등) None. 캐시에 저장하지 않는다.
        """
        from stock_agent.snap_image import snap_object_key
        if not self.settings.snap_image_prefix:
            return None
        key = snap_object_key(self.settings.snap_image_prefix, image_ref)
        return None if key is None else self._fetch_image(key)

    def _fetch_image(self, key: str) -> bytes | None:
        try:
            response = self.client.get_object(Bucket=self.settings.bucket, Key=key)
            body = response["Body"]
            try:
                raw = body.read()
            finally:
                close = getattr(body, "close", None)
                if callable(close):
                    close()
            expected = response.get("ContentLength")
            if expected is not None and len(raw) != expected:
                raise StorageUnavailable("R2 이미지 다운로드 크기가 일치하지 않습니다")
            return raw
        except StorageUnavailable:
            raise
        except Exception as exc:
            response = getattr(exc, "response", {})
            if (response.get("ResponseMetadata", {}).get("HTTPStatusCode") == 404
                    or response.get("Error", {}).get("Code") in {"404", "NoSuchKey", "NotFound"}):
                return None
            raise StorageUnavailable("R2 이미지를 가져오지 못했습니다") from None

    @staticmethod
    def _cache_image(cached: Path, raw: bytes) -> bool:
        """Atomically store one image; False when the cache directory cannot be written."""
        partial = None
        try:
            cached.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            fd, name = tempfile.mkstemp(prefix=cached.name + ".", suffix=".part", dir=cached.parent)
            partial = Path(name)
            with open(fd, "wb") as out:
                out.write(raw)
                out.flush()
                os.fsync(out.fileno())
            os.replace(partial, cached)
            partial = None
            return True
        except OSError:
            return False
        finally:
            if partial is not None:
                try:
                    partial.unlink(missing_ok=True)
                except OSError:
                    pass

    def prepare_database(self, attempts: int = 3, backoff_seconds: float = 0.25,
                         *, allow_download: bool = True) -> tuple[Path, str]:
        """Download the configured DB once per object version to an atomic cache file."""
        path, version, meta = self._prepare_object(self.settings.db_key, "stock", ".duckdb", "R2 DB",
                                                   attempts, backoff_seconds, allow_download)
        self.database_metadata = meta
        return path, version

    def prepare_clip_index(self, attempts: int = 3, backoff_seconds: float = 0.25,
                           *, allow_download: bool = True) -> tuple[Path, str]:
        """재고 CLIP 인덱스(R2_CLIP_INDEX_KEY)를 DB와 같은 방식으로 캐시에 받는다 (객체 버전마다 한 번)."""
        if not self.settings.clip_index_key:
            raise StorageConfigurationError("R2_CLIP_INDEX_KEY가 설정되지 않았습니다")
        path, version, meta = self._prepare_object(self.settings.clip_index_key, "clip-index", ".npz",
                                                   "R2 CLIP 인덱스", attempts, backoff_seconds, allow_download)
        self.clip_index_metadata = meta
        return path, version

    def _prepare_object(self, key: str, stem: str, suffix: str, label: str, attempts: int,
                        backoff_seconds: float, allow_download: bool) -> tuple[Path, str, dict]:
        """객체 1개를 버전마다 한 번만 받아 cache_dir/<stem>-<sha256(bucket/key/version)><suffix>에 둔다."""
        bucket = self.settings.bucket
        last_error = None
        for attempt in range(attempts):
            try:
                head = self.client.head_object(Bucket=bucket, Key=key)
                etag_header = str(head.get("ETag", ""))
                etag = etag_header.strip('"')
                version = str(head.get("VersionId", ""))
                content_length = head.get("ContentLength")
                if not etag and not version:
                    raise StorageUnavailable("R2 객체 버전 정보를 확인할 수 없습니다")
                version_tag = version or etag
                identity = hashlib.sha256(f"{bucket}/{key}/{version_tag}".encode()).hexdigest()
                target = self.cache_dir / f"{stem}-{identity}{suffix}"
                meta = {"version": version_tag, "etag": etag, "size_bytes": content_length, "sha256": None,
                        "upstream_checksum_verified": False}
                if target.is_file() and (content_length is None or target.stat().st_size == content_length):
                    sidecar = target.with_suffix(".sha256")
                    actual = file_sha256(target)
                    if sidecar.is_file() and sidecar.read_text().strip() == actual:
                        meta["sha256"] = actual
                        return target, version_tag, meta
                if not allow_download:
                    raise StorageUnavailable(f"검증된 기존 {label} cache가 없습니다. 자동 다운로드는 비활성화되어 있습니다")
                fd, partial_name = tempfile.mkstemp(prefix=target.name + ".", suffix=".part",
                                                    dir=target.parent)
                os.close(fd)
                partial = Path(partial_name)
                try:
                    get_args = {"Bucket": bucket, "Key": key}
                    if version:
                        get_args["VersionId"] = version
                    elif etag_header:
                        get_args["IfMatch"] = etag_header
                    response = self.client.get_object(**get_args)
                    size = _read_stream(response["Body"], partial)
                    expected = response.get("ContentLength", content_length)
                    if expected is not None and size != expected:
                        raise StorageUnavailable(f"{label} 다운로드 크기가 일치하지 않습니다")
                    os.replace(partial, target)
                    meta["sha256"] = _write_checksum(target)
                finally:
                    partial.unlink(missing_ok=True)
                return target, version_tag, meta
            except StorageUnavailable as exc:
                last_error = exc
            except Exception as exc:
                # Keep provider messages out of logs and API responses; they may contain URLs.
                last_error = StorageUnavailable(f"{label}를 가져오지 못했습니다")
            if attempt + 1 < attempts:
                time.sleep(backoff_seconds * (2 ** attempt))
        raise last_error or StorageUnavailable(f"{label}를 가져오지 못했습니다")
