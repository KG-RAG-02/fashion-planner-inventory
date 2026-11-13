"""결정적 합성 fixture DuckDB를 만든다 — stub 프로필의 STOCK_DB_PATH (H&M 원본이 아닌 합성 데이터).

실행: python scripts/build_fixture_db.py <out.duckdb> [--force] [--storage-version v1.0.0]
      컨테이너 안: python -m stock_agent.fixture_db <out.duckdb>
- 데이터: stock_agent/fixture_db.py — tests/conftest.py의 작은 표(a1~a6) + trend-analysis-agent stub 결과
  (examples/stub/stock-request.json의 input, 발레코어·상의·핑크)와 맞춘 상품 3개 + 합성 표지 표 stock_fixture.
- 출력: 경로, 표별 행 수, 내용 해시(content_sha256: 같은 코드면 같은 값), readiness, DuckDB·storage version.
- 있는 파일은 --force 없이 덮어쓰지 않는다 (exit 2). readiness가 준비되지 않으면 exit 1.
사용: STOCK_DB_PATH=<out.duckdb> STOCK_LLM_PROVIDER=policy python -m stock_agent.server
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from stock_agent.fixture_db import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
