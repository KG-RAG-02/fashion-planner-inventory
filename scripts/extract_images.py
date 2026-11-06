"""H&M 이미지 중 04가 쓰는 의류 상품 이미지만 data/images 로 꺼낸다.

Kaggle H&M 데이터는 이미지가 대부분이라 전체를 풀면 수십 GB다.
압축 파일 안에서 article_attrs(의류 79,178개)에 있는 상품 이미지만 골라 푼다.

준비: Kaggle 대회 페이지에서 규정 동의 후 전체 압축 파일을 받는다.
  kaggle competitions download -c h-and-m-personalized-fashion-recommendations -p data/raw
  (또는 대회 Data 탭의 Download All)

실행 예 (레포 맨 위 폴더에서)
  python scripts/extract_images.py --zip data/raw/h-and-m-personalized-fashion-recommendations.zip
  python scripts/extract_images.py --zip <zip> --max-side 512    # 긴 변 512px로 줄여 저장 (Pillow 필요)

결과: data/images/<article_id 앞 3자리>/<article_id>.jpg  (Git에 올리지 않음)
"""

import argparse
import io
import os
import sys
import zipfile
from pathlib import Path

import duckdb

ap = argparse.ArgumentParser()
ap.add_argument("--zip", required=True, help="Kaggle H&M 전체 압축 파일 경로")
ap.add_argument("--out", default=os.environ.get("STOCK_IMAGE_DIR", "data/images"))
ap.add_argument("--db", default=os.environ.get("STOCK_DB_PATH", "data/stock.duckdb"))
ap.add_argument("--max-side", type=int, default=0, help="0이면 원본 그대로, 아니면 긴 변을 이 크기로 줄임")
args = ap.parse_args()

resize = None
if args.max_side:
    try:
        from PIL import Image
    except ImportError:
        sys.exit("--max-side를 쓰려면 Pillow가 필요합니다: pip install pillow")

    def resize(raw: bytes) -> bytes:
        im = Image.open(io.BytesIO(raw)).convert("RGB")
        im.thumbnail((args.max_side, args.max_side))
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=85)
        return buf.getvalue()

con = duckdb.connect(args.db, read_only=True)
wanted = {r[0] for r in con.execute("SELECT article_id FROM article_attrs WHERE kf_area IS NOT NULL").fetchall()}
con.close()
print(f"의류 상품 {len(wanted):,}개의 이미지를 찾습니다")

out = Path(args.out)
done = skipped = 0
with zipfile.ZipFile(args.zip) as zf:
    for info in zf.infolist():
        name = info.filename                       # images/099/0999990001.jpg
        if not (name.startswith("images/") and name.endswith(".jpg")):
            continue
        aid = Path(name).stem
        if aid not in wanted:
            continue
        dest = out / aid[:3] / f"{aid}.jpg"
        if dest.exists():
            skipped += 1
            continue
        dest.parent.mkdir(parents=True, exist_ok=True)
        raw = zf.read(info)
        dest.write_bytes(resize(raw) if resize else raw)
        done += 1
        if done % 5000 == 0:
            print(f"  {done:,}개 저장")

have = done + skipped
print(f"저장 {done:,}개, 이미 있음 {skipped:,}개 → 이미지 있는 의류 {have:,} / {len(wanted):,} "
      f"({have / max(len(wanted), 1):.1%}). 위치: {out}")
