"""inventory 요청 1건 실행 → 응답 JSON 출력

요청은 trend 결과(TrendAnalysisResult) 형식이다 (examples/).

실행 예 (레포 맨 위 폴더에서)
  python scripts/run_agent.py                          # 기본 예시 요청 (트렌드 1개)
  python scripts/run_agent.py --request req.json       # 요청 파일 (trend 결과 형식)
  python scripts/run_agent.py --request examples/request-trends-example.json   # 트렌드 여러 개
  python scripts/run_agent.py --out data/resp.json     # 응답을 파일로도 저장

DB는 INVENTORY_DB_PATH(기본 data/stock.duckdb). 착샷 CLIP 필터는 INVENTORY_CLIP_INDEX_PATH(기본 data/clip_index.npz)가
있을 때 걸리고, 없으면 search.clip.skip_reason=no_index로 기록된다.
"""

import argparse
import json
import os
import sys
from pathlib import Path

import duckdb

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from fashion_planner_inventory.multi import run_request  # noqa: E402

EXAMPLE = json.loads((Path(__file__).resolve().parents[1] / "examples/request-example.json").read_text(encoding="utf-8"))

ap = argparse.ArgumentParser()
ap.add_argument("--request")
ap.add_argument("--out")
args = ap.parse_args()

req = json.loads(Path(args.request).read_text(encoding="utf-8")) if args.request else EXAMPLE
con = duckdb.connect(os.environ.get("INVENTORY_DB_PATH", "data/stock.duckdb"), read_only=True)
resp = run_request(req, con)   # 트렌드마다 상품 1개
con.close()

text = json.dumps(resp, ensure_ascii=False, indent=2, default=str)
if args.out:
    Path(args.out).write_text(text, encoding="utf-8")
    print(f"저장: {args.out}")

# 화면에는 사진(base64)을 줄여서 보여준다. 전체는 --out 파일에 저장됨
def _short(p):
    img = p.get("image") if p else None
    if img and len(img.get("data", "")) > 60:
        p = {**p, "image": {**img, "data": img["data"][:60] + f"... ({img['size_bytes']:,} bytes)"}}
    return p


short = {**resp, "products": [_short(p) for p in resp["products"]]}
print(json.dumps(short, ensure_ascii=False, indent=2, default=str))
