"""테스트 공용 fixture: 작은 DuckDB (duckdb가 없으면 해당 테스트는 건너뜀)"""

import pytest


@pytest.fixture
def con():
    duckdb = pytest.importorskip("duckdb")
    from stock_agent.fixture_db import create_base_tables
    c = duckdb.connect()
    # 표 내용(a1~a6)은 stock_agent/fixture_db.py에 있다 — scripts/build_fixture_db.py의 stub DB도 같은 표에서 시작한다
    create_base_tables(c)
    return c
