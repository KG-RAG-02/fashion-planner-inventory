"""무신사 스냅 착샷(HF disl/musinsa-snap-new) parquet → 행 번호별 JPEG 파일

02 트렌드 결과의 착샷(kind=snap_garment)은 image_url 없이 image_ref만 준다:
  hf://datasets/disl/musinsa-snap-new@<revision>#row=<row>
03은 이 참조를 R2 객체 <R2_SNAP_IMAGE_PREFIX>/<revision>/<row>.jpg 로 읽는다.
이 스크립트는 그 객체들을 로컬에 만든다. 원본 바이트 그대로 저장한다(크롭·재인코딩 없음).

준비: parquet를 받는다 (공개 데이터셋, 로그인 불필요, 약 171MB)
  curl -L -o data/raw/musinsa-snap-new/train-00000-of-00001.parquet \
    https://huggingface.co/datasets/disl/musinsa-snap-new/resolve/<revision>/data/train-00000-of-00001.parquet

실행 (레포 맨 위 폴더에서)
  python scripts/extract_snap_images.py
  → data/snap_images/<revision>/<row>.jpg  (Git에 올리지 않음)
그다음 data/snap_images/ 아래를 R2 <R2_SNAP_IMAGE_PREFIX>/ 아래로 그대로 올린다.
"""

import argparse
import sys
from pathlib import Path

import duckdb

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from stock_agent.snap_image import SNAP_REVISION  # noqa: E402

ap = argparse.ArgumentParser()
ap.add_argument("--parquet", default="data/raw/musinsa-snap-new/train-00000-of-00001.parquet")
ap.add_argument("--out", default="data/snap_images")
ap.add_argument("--revision", default=SNAP_REVISION, help="parquet를 받은 HF 데이터셋 버전(커밋 해시)")
args = ap.parse_args()

out = Path(args.out) / args.revision
out.mkdir(parents=True, exist_ok=True)
con = duckdb.connect()
rows = con.execute("SELECT file_row_number, image.bytes FROM read_parquet(?, file_row_number=true) "
                   "ORDER BY file_row_number", [args.parquet]).fetchall()
n = bad = 0
for row, data in rows:
    if not data or bytes(data[:3]) != b"\xff\xd8\xff":
        bad += 1
        continue
    (out / f"{row}.jpg").write_bytes(bytes(data))
    n += 1
print(f"저장 {n:,}장, 건너뜀 {bad:,}장 (JPEG 아님/빈 값) → {out}/")
