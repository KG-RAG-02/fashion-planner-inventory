"""테스트 공용 fixture: 작은 DuckDB (duckdb가 없으면 해당 테스트는 건너뜀)"""

import pytest


@pytest.fixture(autouse=True)
def _no_real_clip_index(monkeypatch, tmp_path):
    """테스트가 로컬의 실제 CLIP 인덱스(data/clip_index.npz)·R2 인덱스 설정을 읽지 않게 한다 (모델을 불러오지 않음)."""
    monkeypatch.setenv("STOCK_CLIP_INDEX_PATH", str(tmp_path / "no-clip-index.npz"))
    monkeypatch.delenv("R2_CLIP_INDEX_KEY", raising=False)
    monkeypatch.delenv("R2_SNAP_IMAGE_PREFIX", raising=False)
    from fashion_planner_inventory import clip_matcher
    monkeypatch.setattr(clip_matcher, "_snap_source", None)


@pytest.fixture
def con():
    duckdb = pytest.importorskip("duckdb")
    from fashion_planner_inventory.fixture_db import create_base_tables
    c = duckdb.connect()
    # 표 내용(a1~a6)은 fashion_planner_inventory/fixture_db.py에 있다 — scripts/build_fixture_db.py의 stub DB도 같은 표에서 시작한다
    create_base_tables(c)
    return c
