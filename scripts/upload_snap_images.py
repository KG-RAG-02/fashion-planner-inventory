"""data/snap_images/ → R2 <버킷>/<R2_SNAP_IMAGE_PREFIX>/ 업로드 (담당자가 직접 실행)

scripts/extract_snap_images.py로 만든 착샷 JPEG를 같은 경로 구조로 R2에 올린다:
    data/snap_images/<revision>/<row>.jpg → <prefix>/<revision>/<row>.jpg
R2에 이미 같은 크기로 있는 파일은 건너뛰므로, 중간에 끊겨도 다시 실행하면 이어서 올린다.

키: 쓰기 권한(Object Read & Write) 키를 R2_UPLOAD_ACCESS_KEY_ID·R2_UPLOAD_SECRET_ACCESS_KEY로 준다.
    없으면 .env의 R2_ACCESS_KEY_ID·R2_SECRET_ACCESS_KEY를 쓴다(읽기 전용 키면 AccessDenied).
    버킷·엔드포인트는 .env의 R2_BUCKET·R2_ENDPOINT(없으면 R2_ACCOUNT_ID로 만든 주소)를 쓴다.
    업로드용 키는 이 실행에만 넣고 서버 .env·secret store에는 넣지 않는다(서버는 읽기 전용 키).

실행 (레포 맨 위 폴더에서)
  python scripts/upload_snap_images.py --dry-run     # 올릴 개수만 확인
  python scripts/upload_snap_images.py               # 업로드
"""

import argparse
import os
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from stock_agent.env import read_dotenv  # noqa: E402
from stock_agent.storage import R2Settings  # noqa: E402

read_dotenv()
ap = argparse.ArgumentParser()
ap.add_argument("--src", default="data/snap_images")
ap.add_argument("--prefix", default=os.environ.get("R2_SNAP_IMAGE_PREFIX", "musinsa-snap"))
ap.add_argument("--workers", type=int, default=32)
ap.add_argument("--dry-run", action="store_true")
args = ap.parse_args()

import boto3  # noqa: E402
from botocore.config import Config  # noqa: E402

s = R2Settings.from_env()
key_id = os.environ.get("R2_UPLOAD_ACCESS_KEY_ID") or s.access_key_id
secret = os.environ.get("R2_UPLOAD_SECRET_ACCESS_KEY") or s.secret_access_key
client = boto3.client("s3", endpoint_url=s.endpoint_url, aws_access_key_id=key_id, aws_secret_access_key=secret,
                      region_name="auto", config=Config(max_pool_connections=args.workers,
                                                        retries={"max_attempts": 5, "mode": "standard"}))
prefix = args.prefix.strip("/")
src = Path(args.src)
files = sorted(p for p in src.rglob("*.jpg"))
if not files:
    sys.exit(f"{src}/ 에 jpg가 없습니다. 먼저 scripts/extract_snap_images.py를 실행하세요.")

existing = {}
for page in client.get_paginator("list_objects_v2").paginate(Bucket=s.bucket, Prefix=prefix + "/"):
    for o in page.get("Contents", []):
        existing[o["Key"]] = o["Size"]

todo = [(p, f"{prefix}/{p.relative_to(src).as_posix()}") for p in files]
todo = [(p, k) for p, k in todo if existing.get(k) != p.stat().st_size]
print(f"버킷 {s.bucket} / {prefix}/  로컬 {len(files):,}장, 이미 있음 {len(files) - len(todo):,}장, "
      f"올릴 것 {len(todo):,}장")
if args.dry_run or not todo:
    sys.exit(0)


def put(item):
    path, key = item
    client.upload_file(str(path), s.bucket, key, ExtraArgs={"ContentType": "image/jpeg"})


failed = []
with ThreadPoolExecutor(args.workers) as ex:
    futs = {ex.submit(put, it): it for it in todo}
    for i, f in enumerate(as_completed(futs), 1):
        if f.exception() is not None:
            failed.append((futs[f][1], f.exception()))
        if i % 500 == 0 or i == len(todo):
            print(f"  {i:,}/{len(todo):,}", flush=True)
if failed:
    print(f"실패 {len(failed):,}장 (다시 실행하면 이어서 올립니다). 첫 오류: {failed[0][0]}: {failed[0][1]}")
    sys.exit(1)
print(f"완료: {len(todo):,}장 업로드")
