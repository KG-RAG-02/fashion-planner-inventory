# Cloudflare R2 data access

## Decision

The API reads the DuckDB file from a private Cloudflare R2 bucket. It checks object metadata at process startup, downloads only when the cached object version is missing or changed, verifies the byte count, atomically promotes the temporary file, then opens the local cache with DuckDB `read_only=True`. Requests do not download the database.

A historical repository note described an approximately 808 MB object; current object size is unverified until a fresh HEAD. DuckDB's `httpfs` extension supports read-only S3 database access with range reads, and its documentation lists Cloudflare R2 as a service that should work ([DuckDB S3 API support](https://duckdb.org/docs/current/core_extensions/httpfs/s3api)). That option avoids a full cold-start download, but it adds extension installation and remote range-read behavior to the live query path. Remote version pinning also depends on version metadata and cache settings. The local snapshot keeps query behavior local and gives each process one stable DB version. For repeated restarts, mount a persistent volume at `STOCK_CACHE_DIR`; the service still checks the remote ETag/version before reusing the cached file.

For version updates, publish a new object version/key and restart or roll out the API. Avoid replacing an object in place while instances are running. Each instance has its own local cache and read-only DuckDB connection.

## Images

When `R2_IMAGE_PREFIX` is set, the API checks product image objects on demand and caches only selected images. The verified key shape is `<prefix>/<article_id first 3 digits>/<article_id>.jpg`; for example, `images/099/0999991001.jpg`. Presence checks are cached in-process for 30 seconds. A missing or unavailable image does not prevent inventory results. When the prefix is unset, `STOCK_IMAGE_DIR` remains the local fallback.

The existing producer response continues to include the selected product image as base64. The raw database and image collection are not copied into Git or the Docker image.

## Configuration and security

The API uses these settings:

- `R2_ACCOUNT_ID` and optionally `R2_ENDPOINT` for the S3-compatible endpoint. `R2_ENDPOINT` is needed for jurisdiction-specific endpoints.
- `R2_BUCKET` and `R2_DB_KEY` for the database object.
- `R2_IMAGE_PREFIX` for the optional image collection.
- `R2_ACCESS_KEY_ID` and `R2_SECRET_ACCESS_KEY` for the S3-compatible API credentials.
- `STOCK_CACHE_DIR` to choose a local persistent cache directory.

The SDK region is `auto`. Scope the R2 token to the required bucket with `Object Read only`; Cloudflare documents that permission and jurisdiction-specific endpoints in its [R2 authentication guide](https://developers.cloudflare.com/r2/api/tokens/). Keep access key values in a deployment secret store; never log credentials, signed URLs, or provider exception messages. `/health` reports the provider and a non-secret version identifier, not bucket/key paths or credentials.

`STOCK_DB_PATH` selects local-file mode: the API skips the R2 settings and HEAD and opens that DuckDB file read-only, verifying a `<name>.sha256` sidecar when one is present; `/health` then reports `db: local-file` and `db_version: sha256:<file hash>`, and images come from `STOCK_IMAGE_DIR`. Local build scripts use the same variable.

## Verification

Historical repository note (not revalidated in the 2026-11-12 environment): the then-configured R2 configuration was checked with read-only requests. The database object was readable and opened successfully as a DuckDB read-only database with the required tables (`articles`, `transactions`, `article_attrs`, `stock_v1`). The configured image prefix returned objects matching the expected key shape, and a sample object downloaded as JPEG. The API `/health` returned 200 with no missing tables. The downloaded verification cache was removed after the check.

R2 account identifiers, bucket names, object keys, and credential values belong in local `.env` or the deployment secret store, not in this document or `.env.example`.

API startup now uses HEAD plus an existing SHA-256-sidecar cache only (`allow_download=False`). `/health/live` is liveness; `/health/ready` is schema/snapshot readiness. The SHA sidecar checks local corruption, not upstream authenticity. Run `python scripts/diagnose_readiness.py` for a value-free current-process report.
