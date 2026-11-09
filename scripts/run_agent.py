"""stock-agent 요청 1건 실행 → 응답 JSON 출력

요청은 02 트렌드 결과(TrendAnalysisResult) 형식이다 (examples/).

실행 예 (레포 맨 위 폴더에서)
  python scripts/run_agent.py                          # 기본 예시 요청 (트렌드 1개)
  python scripts/run_agent.py --request req.json       # 요청 파일 (02 트렌드 결과 형식)
  python scripts/run_agent.py --request examples/request-trends-example.json   # 트렌드 여러 개
  python scripts/run_agent.py --out data/resp.json     # 응답을 파일로도 저장
  python scripts/run_agent.py --mode rule              # 규칙 방식 (LLM 없이, 비교 기준선)
  python scripts/run_agent.py --policy                 # LLM agent 구조를 PolicyLLM(가짜 LLM)으로 점검

기본(--mode agent)은 stock_agent.llm.load_llm()의 LLM(OpenAI, 현재 폴더 .env의 LLM_API_KEY)을 쓴다.
키가 없으면 트렌드마다 SOURCE_UNAVAILABLE(reason=no_llm).
"""

import argparse
import json
import os
import sys
from pathlib import Path

import duckdb

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from stock_agent.llm import PolicyLLM, load_llm  # noqa: E402
from stock_agent.multi import run_request  # noqa: E402

EXAMPLE = json.loads((Path(__file__).resolve().parents[1] / "examples/request-example.json").read_text(encoding="utf-8"))

ap = argparse.ArgumentParser()
ap.add_argument("--request")
ap.add_argument("--out")
ap.add_argument("--mode", choices=["agent", "rule"], default="agent")
ap.add_argument("--policy", action="store_true", help="LLM 대신 PolicyLLM(점검용)")
args = ap.parse_args()

req = json.loads(Path(args.request).read_text(encoding="utf-8")) if args.request else EXAMPLE
con = duckdb.connect(os.environ.get("STOCK_DB_PATH", "data/stock.duckdb"), read_only=True)
llm = PolicyLLM() if args.policy else load_llm()
resp = run_request(req, con, llm=llm, mode=args.mode)   # 트렌드마다 상품 1개
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
