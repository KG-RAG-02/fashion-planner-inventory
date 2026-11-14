# Cloudflare R2 data access

## Decision

The API reads the DuckDB file from a private Cloudflare R2 bucket through a local, checksummed cache. At process startup it only checks the object metadata (HEAD) and reuses the cached file when it matches that object version and its SHA-256 sidecar; it never downloads at startup, so a missing or outdated cache keeps `/health/ready` at 503. The separate data job `python -m stock_agent.server --prepare-cache` downloads a missing or changed object version, verifies the byte count, atomically promotes the temporary file and writes the sidecar. The API opens the cache with DuckDB `read_only=True`; requests do not download the database. `STOCK_DB_PATH` (local-file mode) bypasses R2 entirely.

DuckDB's `httpfs` extension supports read-only S3 database access with range reads, and its documentation lists Cloudflare R2 as a service that should work ([DuckDB S3 API support](https://duckdb.org/docs/current/core_extensions/httpfs/s3api)). That option avoids a full cold-start download, but it adds extension installation and remote range-read behavior to the live query path. Remote version pinning also depends on version metadata and cache settings. The local snapshot keeps query behavior local and gives each process one stable DB version. For repeated restarts, mount a persistent volume at `STOCK_CACHE_DIR`; the service still checks the remote ETag/version before reusing the cached file.

For version updates, publish a new object version/key and restart or roll out the API. Avoid replacing an object in place while instances are running. Each instance has its own local cache and read-only DuckDB connection.

## Images

When `R2_IMAGE_PREFIX` is set, the API checks product image objects on demand and caches only selected images. The verified key shape is `<prefix>/<article_id first 3 digits>/<article_id>.jpg`; for example, `images/099/0999991001.jpg`. Presence checks are cached in-process for 30 seconds. Fetched images are cached in `STOCK_IMAGE_CACHE_DIR` (default `$STOCK_CACHE_DIR/images`); when that directory cannot be written, for example because the DB cache is a read-only mount, the image is returned uncached and the trend result's `limitations` say so instead of failing the request. A missing or unavailable image does not prevent inventory results. When the prefix is unset, `STOCK_IMAGE_DIR` is the local fallback.

## CLIP index

`R2_CLIP_INDEX_KEY` names the CLIP index object (npz built by `scripts/build_clip_index.py`, rule `gray-image+material-detail-en/v2`). `--prepare-cache` downloads it once per object version into `STOCK_CACHE_DIR/clip-index-<sha256>.npz` next to the database; the API reads that cache at startup and never downloads it. A missing index or a different rule leaves the server up and skips the CLIP filter (`search.clip.skip_reason: no_index`). `STOCK_CLIP_INDEX_PATH` selects a local index file instead.

## Musinsa snap images

Trend results carry Musinsa snap garments (`kind: snap_garment`) with only an `image_ref` of the form `hf://datasets/disl/musinsa-snap-new@<revision>#row=<n>`. The snap JPEGs of that dataset revision are stored as `<R2_SNAP_IMAGE_PREFIX>/<revision>/<n>.jpg` (built with `scripts/extract_snap_images.py`, uploaded with `scripts/upload_snap_images.py` using a separate write-scoped key passed only for that run). The API reads one snap image per request on demand and does not cache it. A reference to another revision has no object and is treated as text-only CLIP (`search.clip.image_skip: not_found`); an unset prefix gives `no_snap_store`.

The producer response includes the selected product image as base64. The raw database and image collection are not copied into Git or the Docker image.

## Configuration and security

The API uses these settings:

- `R2_ACCOUNT_ID` and optionally `R2_ENDPOINT` for the S3-compatible endpoint. `R2_ENDPOINT` is needed for jurisdiction-specific endpoints.
- `R2_BUCKET` and `R2_DB_KEY` for the database object.
- `R2_IMAGE_PREFIX` for the optional image collection.
- `R2_ACCESS_KEY_ID` and `R2_SECRET_ACCESS_KEY` for the S3-compatible API credentials.
- `STOCK_CACHE_DIR` to choose a local persistent cache directory.
- `STOCK_IMAGE_CACHE_DIR` for fetched product images (default `$STOCK_CACHE_DIR/images`).
- `R2_CLIP_INDEX_KEY` (optional) for the CLIP index object.
- `R2_SNAP_IMAGE_PREFIX` (optional) for Musinsa snap images.

The SDK region is `auto`. Scope the R2 token to the required bucket with `Object Read only`; Cloudflare documents that permission and jurisdiction-specific endpoints in its [R2 authentication guide](https://developers.cloudflare.com/r2/api/tokens/). Keep access key values in a deployment secret store; never log credentials, signed URLs, or provider exception messages. `/health` reports the provider and a non-secret version identifier, not bucket/key paths or credentials.

`STOCK_DB_PATH` selects local-file mode: the API skips the R2 settings and HEAD and opens that DuckDB file read-only, verifying a `<name>.sha256` sidecar when one is present; `/health` then reports `db: local-file` and `db_version: sha256:<file hash>`, and images come from `STOCK_IMAGE_DIR`. Local build scripts use the same variable.

## Verification

Use read-only checks: `python scripts/diagnose_readiness.py` reports configuration presence, R2 HEAD, existing cache and read-only schema without values, and `python -m stock_agent.server --prepare-cache` fills and verifies the cache (database and CLIP index).

R2 account identifiers, bucket names, object keys, and credential values belong in local `.env` or the deployment secret store, not in this document or `.env.example`.

API startup uses HEAD plus an existing SHA-256-sidecar cache only (`allow_download=False`). `/health/live` is liveness; `/health/ready` is schema/snapshot readiness. The SHA sidecar checks local corruption, not upstream authenticity. Run `python scripts/diagnose_readiness.py` for a value-free current-process report. Fill the cache with the separate data job `python -m stock_agent.server --prepare-cache`: it downloads once per object version (`prepare_database(allow_download=True)`), writes the SHA-256 sidecar, opens the file read-only, runs the readiness inspection, prints one JSON line with the DuckDB library version and the file's storage version (no credentials, endpoint, bucket or key), and exits non-zero unless the database is ready. A DuckDB older than the file's storage version cannot open it.
