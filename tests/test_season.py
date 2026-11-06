from datetime import date

import pytest

from stock_agent.season import map_season, parse_season


@pytest.mark.parametrize(
    "text, ranges",
    [
        ("2026 봄", [(date(2019, 3, 1), date(2019, 5, 31)), (date(2020, 3, 1), date(2020, 5, 31))]),
        ("2026 여름", [(date(2019, 6, 1), date(2019, 8, 31)), (date(2020, 6, 1), date(2020, 8, 31))]),
        ("2026 가을", [(date(2019, 9, 1), date(2019, 11, 30))]),
        ("2026 겨울", [(date(2018, 12, 1), date(2019, 2, 28)), (date(2019, 12, 1), date(2020, 2, 29))]),
        ("2026-SS", [(date(2019, 3, 1), date(2019, 8, 31)), (date(2020, 3, 1), date(2020, 8, 31))]),
        ("2026-FW", [(date(2019, 9, 1), date(2020, 2, 29))]),
    ],
)
def test_2026_seasons_use_all_complete_seasons_in_data(text, ranges):
    p = map_season(text)
    assert list(p.ranges) == ranges
    assert p.n_periods == len(ranges)
    assert p.is_substitute
    assert p.warnings


def test_partial_seasons_at_data_edges_are_excluded():
    # 2018 가을은 데이터가 9/20에 시작, 2020 가을은 9/22에 끝나서 제외
    p = map_season("2026 가을")
    assert all(s.year == 2019 for s, _ in p.ranges)


def test_season_inside_data_is_used_as_is():
    p = map_season("2020 봄")
    assert list(p.ranges) == [(date(2020, 3, 1), date(2020, 5, 31))]
    assert not p.is_substitute
    assert p.warnings == []


@pytest.mark.parametrize("text", ["2026SS", "2026 S/S", "2026-ss", "2026 spring", "2026_FW", "2026 F/W"])
def test_aliases(text):
    year, _ = parse_season(text)
    assert year == 2026


@pytest.mark.parametrize("text", ["봄", "2026", "2026 초봄", "SS-2026"])
def test_invalid_input_raises(text):
    with pytest.raises(ValueError):
        map_season(text)


def test_contract_helpers():
    p = map_season("2026 봄")
    assert p.to_period() == {
        "ranges": [
            {"start": "2019-03-01", "end": "2019-05-31"},
            {"start": "2020-03-01", "end": "2020-05-31"},
        ]
    }
    assert p.days == 92 * 2
    assert "2회" in p.basis
    sql, params = p.sql_filter()
    assert sql == "(t_dat BETWEEN ? AND ? OR t_dat BETWEEN ? AND ?)"
    assert params == [date(2019, 3, 1), date(2019, 5, 31), date(2020, 3, 1), date(2020, 5, 31)]
    with pytest.raises(ValueError):
        p.sql_filter("t_dat; DROP TABLE x")


def test_sql_filter_runs_in_duckdb():
    duckdb = pytest.importorskip("duckdb")
    con = duckdb.connect()
    con.execute("CREATE TABLE t AS SELECT * FROM (VALUES (DATE '2019-04-01'), (DATE '2019-07-01'), (DATE '2020-05-31')) v(t_dat)")
    sql, params = map_season("2026 봄").sql_filter()
    assert con.execute(f"SELECT COUNT(*) FROM t WHERE {sql}", params).fetchone()[0] == 2
