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


class R2ObjectStore:
    """Small read-only adapter; pass a fake client to exercise it offline."""

    def __init__(self, settings: R2Settings, client=None, cache_dir: Path | None = None):
        self.settings = settings
        self.client = client if client is not None else _client(settings)
        self.cache_dir = Path(cache_dir) if cache_dir is not None else _safe_cache_dir()
        self.cache_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._image_presence: dict[str, tuple[float, bool]] = {}
        self._image_lock = threading.Lock()

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
                # Images are optional; avoid repeated slow HEAD calls after an outage.
                result = False
        with self._image_lock:
            self._image_presence[key] = (time.monotonic() + 30, result)
        return result

    def load_image(self, article_id: str) -> bytes | None:
        key = self.object_key_for_image(article_id)
        cached = self.cache_dir / "images" / (hashlib.sha256(key.encode()).hexdigest() + ".jpg")
        if cached.is_file():
            return cached.read_bytes()
        cached.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd, partial_name = tempfile.mkstemp(prefix=cached.name + ".", suffix=".part", dir=cached.parent)
        os.close(fd)
        partial = Path(partial_name)
        try:
            response = self.client.get_object(Bucket=self.settings.bucket, Key=key)
            body = response["Body"]
            size = _read_stream(body, partial)
            expected = response.get("ContentLength")
            if expected is not None and size != expected:
                raise StorageUnavailable("R2 이미지 다운로드 크기가 일치하지 않습니다")
            os.replace(partial, cached)
            return cached.read_bytes()
        except StorageUnavailable:
            raise
        except Exception:
            raise StorageUnavailable("R2 이미지를 가져오지 못했습니다") from None
        finally:
            partial.unlink(missing_ok=True)

    def prepare_database(self, attempts: int = 3, backoff_seconds: float = 0.25) -> tuple[Path, str]:
        """Download the configured DB once per object version to an atomic cache file."""
        bucket, key = self.settings.bucket, self.settings.db_key
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
                target = self.cache_dir / f"stock-{identity}.duckdb"
                if target.is_file() and (content_length is None or target.stat().st_size == content_length):
                    return target, version_tag
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
                        raise StorageUnavailable("R2 DB 다운로드 크기가 일치하지 않습니다")
                    os.replace(partial, target)
                finally:
                    partial.unlink(missing_ok=True)
                return target, version_tag
            except StorageUnavailable as exc:
                last_error = exc
            except Exception as exc:
                # Keep provider messages out of logs and API responses; they may contain URLs.
                last_error = StorageUnavailable("R2 DB를 가져오지 못했습니다")
            if attempt + 1 < attempts:
                time.sleep(backoff_seconds * (2 ** attempt))
        raise last_error or StorageUnavailable("R2 DB를 가져오지 못했습니다")
