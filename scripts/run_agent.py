"""stock-agent 요청 1건 실행 → 응답 JSON 출력

실행 예 (레포 맨 위 폴더에서)
  python scripts/run_agent.py                          # 기본 예시 요청
  python scripts/run_agent.py --request req.json       # 요청 파일
  python scripts/run_agent.py --request examples/request-trends-example.json   # 트렌드 여러 개
  python scripts/run_agent.py --out data/resp.json     # 응답을 파일로도 저장
"""

import argparse
import json
import os
import sys
from pathlib import Path

import duckdb

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from stock_agent.multi import run_request  # noqa: E402

EXAMPLE = {
    "schema_version": "1.0",
    "request_id": "req_example_001",
    "season": {"value": "2027-SS", "source": "user"},
    "target_category": "상의",
    "segment": "Ladieswear",
    "trend_colors": ["라이트핑크", "화이트"],
}

ap = argparse.ArgumentParser()
ap.add_argument("--request")
ap.add_argument("--out")
args = ap.parse_args()

req = json.loads(Path(args.request).read_text(encoding="utf-8")) if args.request else EXAMPLE
con = duckdb.connect(os.environ.get("STOCK_DB_PATH", "data/stock.duckdb"), read_only=True)
resp = run_request(req, con)   # trends가 있으면 트렌드마다 1개
con.close()

text = json.dumps(resp, ensure_ascii=False, indent=2, default=str)
if args.out:
    Path(args.out).write_text(text, encoding="utf-8")
    print(f"저장: {args.out}")

# 화면에는 사진(base64)을 줄여서 보여준다. 전체는 --out 파일에 저장됨
def _short(p):
    img = p.get("image")
    if img and len(img.get("data", "")) > 60:
        p = {**p, "image": {**img, "data": img["data"][:60] + f"... ({img['size_bytes']:,} bytes)"}}
    return p


short = {**resp, "products": [_short(p) for p in resp["products"]]}
print(json.dumps(short, ensure_ascii=False, indent=2, default=str))
