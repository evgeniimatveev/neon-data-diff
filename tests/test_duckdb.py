"""DuckDB backend: two database files attached into one session. No server needed."""
import pytest

duckdb = pytest.importorskip("duckdb")

from neon_data_diff import diff_duckdb  # noqa: E402
from neon_data_diff.cli import main  # noqa: E402
from neon_data_diff.diff import rewrite_breaches  # noqa: E402


@pytest.fixture()
def files(tmp_path):
    base, head = tmp_path / "yesterday.duckdb", tmp_path / "today.duckdb"

    def make(path, sql):
        con = duckdb.connect(str(path))
        con.execute(sql)
        con.close()

    return base, head, make


def run(base, head, **kw):
    return {d.table: d for d in diff_duckdb(str(base), str(head), **kw)}


def test_identical_files(files):
    base, head, make = files
    for p in (base, head):
        make(p, "create table t as select range as id, range * 1.5 as v from range(5)")
    assert not run(base, head, keys={"t": ["id"]})["t"].has_changes


def test_growth_is_added_rows_not_rewrite(files):
    base, head, make = files
    make(base, "create table t as select range as id, range * 10 as v from range(5)")
    make(head, "create table t as select range as id, range * 10 as v from range(7)")  # two new days appended
    d = run(base, head, keys={"t": ["id"]})["t"]
    assert (d.rows_added, d.rows_removed, d.rows_changed) == (2, 0, 0)
    assert d.rewrite_pct == 0 and rewrite_breaches([d], 0) == []


def test_rewritten_history_is_detected(files):
    base, head, make = files
    make(base, "create table t as select range as id, range * 10 as v from range(10)")
    make(head, "create table t as select range as id, range * 10 as v from range(10)")
    con = duckdb.connect(str(head))
    con.execute("update t set v = -1 where id in (1, 2); delete from t where id = 9")
    con.close()
    d = run(base, head, keys={"t": ["id"]}, samples=5)["t"]
    assert (d.rows_added, d.rows_removed, d.rows_changed) == (0, 1, 2)
    assert d.changed_by_column == {"v": 2}
    assert d.rewrite_pct == pytest.approx(30.0)
    assert rewrite_breaches([d], 10) == [d] and rewrite_breaches([d], 30) == []
    assert {s["v__head"] for s in d.samples} == {-1}


def test_numeric_tolerance_with_decimal_and_double(files):
    base, head, make = files
    make(base, "create table t as select 1 as id, 0.1::double + 0.2::double as f, 1.005::decimal(10,3) as n")
    make(head, "create table t as select 1 as id, 0.3::double as f, 1.005::decimal(10,3) as n")
    assert run(base, head, keys={"t": ["id"]}, tolerance=1e-9)["t"].rows_changed == 0
    assert run(base, head, keys={"t": ["id"]}, tolerance=0)["t"].rows_changed == 1


def test_null_dates_and_strings(files):
    base, head, make = files
    make(base, "create table t as select 1 as id, null::date as d, 'a' as s")
    make(head, "create table t as select 1 as id, date '2026-01-01' as d, 'a' as s")
    d = run(base, head, keys={"t": ["id"]})["t"]
    assert d.rows_changed == 1 and d.changed_by_column == {"d": 1}


def test_schema_and_table_changes(files):
    base, head, make = files
    make(base, "create table t as select 1 as id, 1 as a, 'x' as b; create table old as select 1 as id")
    make(head, "create table t as select 1 as id, 1::bigint as a, 2 as c; create table new as select 1 as id")
    r = run(base, head, keys={"t": ["id"]})
    assert r["t"].columns_added == ["c"] and r["t"].columns_removed == ["b"]
    assert [c for c, *_ in r["t"].columns_retyped] == ["a"]
    assert r["old"].status == "removed" and r["new"].status == "added"


def test_unkeyed_fallback(files):
    base, head, make = files
    make(base, "create table t as select * from (values (1),(1),(2)) v(id)")
    make(head, "create table t as select * from (values (1),(2),(3)) v(id)")
    d = run(base, head, keys={"t": ["id"]})["t"]  # not unique
    assert d.mode == "unkeyed" and (d.rows_added, d.rows_removed) == (1, 1)


def test_custom_schema(files):
    base, head, make = files
    make(base, "create schema s; create table s.t as select 1 as id, 1 as v")
    make(head, "create schema s; create table s.t as select 1 as id, 2 as v")
    assert run(base, head, schema="s", keys={"t": ["id"]})["t"].rows_changed == 1


def test_cli_duckdb_exit_codes(files, capsys):
    base, head, make = files
    make(base, "create table t as select range as id, range as v from range(10)")
    make(head, "create table t as select range as id, range as v from range(12)")  # growth only
    args = ["--duckdb-base", str(base), "--duckdb-head", str(head), "--key", "t=id",
            "--base-label", "yesterday", "--head-label", "today", "--title", "Data drift"]
    assert main(args + ["--max-rewrite-pct", "0"]) == 0  # growth is fine
    out = capsys.readouterr().out
    assert "## Data drift" in out and "`yesterday` (base)" in out
    assert main(args + ["--fail-on-change"]) == 1  # any change fails
    con = duckdb.connect(str(head))
    con.execute("update t set v = -1 where id = 0")
    con.close()
    assert main(args + ["--max-rewrite-pct", "0"]) == 1  # rewrite fails
    assert "History rewritten" in capsys.readouterr().out


def test_cli_argument_validation(files, capsys):
    base, head, _ = files
    assert main(["--duckdb-base", str(base)]) == 2
    assert main([]) == 2
