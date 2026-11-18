"""contracts/examples/의 응답 예시를 합성 fixture DB로 다시 만든다 (H&M 데이터·네트워크·CLIP 없이).

실행: python scripts/build_contract_examples.py
- 합성 fixture DB(fashion_planner_inventory.fixture_db)를 임시 폴더에 만들고, HTTP 앱을 local-file 모드로 띄워
  canonical 경로(POST /v1/agents/inventory/runs)에 요청 3개를 보낸 뒤 envelope의 result를 저장한다.
    response-ok-synthetic.json           examples/stub/inventory-request.json의 input (trend stub 결과)
    response-no-results-upstream.json    TREND_RESULT_EXAMPLE, status=no_results (DB를 조회하지 않음)
    response-error-invalid-request.json  TREND_RESULT_EXAMPLE, 시즌 없음 → 400 INVALID_REQUEST
- 응답의 fetched_at(실행 시각)과 근거 manifest_id(로컬 DB 파일 해시)는 실행 환경마다 달라진다. 나머지는 같다.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
OUT = ROOT / "contracts" / "examples"
TOKEN = "example-token"


def main() -> int:
    from fashion_planner_inventory.fixture_db import build_fixture_db
    from fashion_planner_inventory.schemas import TREND_RESULT_EXAMPLE

    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "fixture.duckdb"
        build_fixture_db(db)
        os.environ.update({"INVENTORY_DB_PATH": str(db), "FASHION_PLANNER_SERVICE_TOKEN": TOKEN,
                           "INVENTORY_CLIP_INDEX_PATH": str(Path(tmp) / "no-index.npz"),
                           "INVENTORY_IMAGE_DIR": str(Path(tmp) / "no-images")})
        for name in ("R2_CLIP_INDEX_KEY", "R2_SNAP_IMAGE_PREFIX"):
            os.environ.pop(name, None)

        from fastapi.testclient import TestClient
        from fashion_planner_inventory import api

        stub = json.loads((ROOT / "examples/stub/inventory-request.json").read_text(encoding="utf-8"))["input"]
        no_results = {**TREND_RESULT_EXAMPLE, "request_id": "req_example_no_results", "status": "no_results",
                      "trends": [], "limitations": ["2027SS에 해당하는 트렌드가 없습니다."]}
        filters = {**TREND_RESULT_EXAMPLE["applied_filters"], "season": None}
        missing_season = {**TREND_RESULT_EXAMPLE, "request_id": "req_example_missing_season", "applied_filters": filters}
        cases = [("response-ok-synthetic.json", stub, 200),
                 ("response-no-results-upstream.json", no_results, 200),
                 ("response-error-invalid-request.json", missing_season, 400)]

        with TestClient(api.app, headers={"Authorization": f"Bearer {TOKEN}"}) as client:
            for filename, body, expected in cases:
                r = client.post(api.HANDOFF_PATH, json={"contract_version": api.HANDOFF_CONTRACT_VERSION,
                                                        "request_id": body["request_id"], "input": body})
                if r.status_code != expected:
                    raise SystemExit(f"{filename}: HTTP {r.status_code}, 기대 {expected}: {r.text[:300]}")
                (OUT / filename).write_text(json.dumps(r.json()["result"], ensure_ascii=False, indent=2) + "\n",
                                            encoding="utf-8")
                print(f"{filename}: HTTP {r.status_code}, status {r.json()['result']['status']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
