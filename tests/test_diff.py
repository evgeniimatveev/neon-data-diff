"""Integration tests. Need a throwaway Postgres: set TEST_DATABASE_URL."""
import os

import psycopg
import pytest

from neon_data_diff import diff_schemas
from neon_data_diff.render import render_markdown

DSN = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not DSN, reason="TEST_DATABASE_URL not set")


@pytest.fixture()
def db():
    with psycopg.connect(DSN, autocommit=True) as c:
        for s in ("base", "head"):
            c.execute(f"drop schema if exists {s} cascade")
            c.execute(f"create schema {s}")
        yield c
        for s in ("base", "head"):
            c.execute(f"drop schema if exists {s} cascade")


def run(**kw):
    return {d.table: d for d in diff_schemas(DSN, "base", "head", **kw)}


def test_identical_schemas_have_no_changes(db):
    for s in ("base", "head"):
        db.execute(f"create table {s}.t as select g as id, g * 1.5 as v from generate_series(1,5) g")
    d = run(keys={"t": ["id"]})["t"]
    assert not d.has_changes
    assert "No differences" in render_markdown(list(run(keys={"t": ["id"]}).values()), "base", "head")


def test_keyed_added_removed_changed(db):
    db.execute("create table base.t as select g as id, g * 10 as v from generate_series(1,5) g")
    db.execute("create table head.t as select g as id, g * 10 as v from generate_series(2,6) g")
    db.execute("update head.t set v = 999 where id = 3")
    d = run(keys={"t": ["id"]}, samples=5)["t"]
    assert (d.rows_added, d.rows_removed, d.rows_changed) == (1, 1, 1)
    assert d.changed_by_column == {"v": 1}
    assert d.samples[0]["v__base"] == 30 and d.samples[0]["v__head"] == 999


def test_numeric_tolerance_ignores_float_noise(db):
    db.execute("create table base.t as select 1 as id, 0.1::float8 + 0.2::float8 as v")
    db.execute("create table head.t as select 1 as id, 0.3::float8 as v")
    assert run(keys={"t": ["id"]}, tolerance=1e-9)["t"].rows_changed == 0
    assert run(keys={"t": ["id"]}, tolerance=0)["t"].rows_changed == 1


def test_null_to_value_counts_as_change(db):
    db.execute("create table base.t as select 1 as id, null::int as v")
    db.execute("create table head.t as select 1 as id, 5 as v")
    assert run(keys={"t": ["id"]})["t"].rows_changed == 1


def test_schema_changes(db):
    db.execute("create table base.t as select 1 as id, 1 as a, 'x' as b")
    db.execute("create table head.t as select 1 as id, 1::bigint as a, 1 as c")
    d = run(keys={"t": ["id"]})["t"]
    assert d.columns_added == ["c"] and d.columns_removed == ["b"]
    assert d.columns_retyped == [("a", "integer", "bigint")]


def test_added_and_removed_tables(db):
    db.execute("create table base.old as select 1 as id")
    db.execute("create table head.new as select 1 as id, 2 as x")
    r = run()
    assert r["old"].status == "removed" and r["new"].status == "added"


def test_unkeyed_fallback_and_non_unique_key(db):
    db.execute("create table base.t as select * from (values (1),(1),(2)) v(id)")
    db.execute("create table head.t as select * from (values (1),(2),(3)) v(id)")
    d = run(keys={"t": ["id"]})["t"]  # key not unique -> unkeyed
    assert d.mode == "unkeyed" and d.rows_changed is None
    assert (d.rows_added, d.rows_removed) == (1, 1)
    assert any("not unique" in n for n in d.notes)


def test_sum_shift(db):
    db.execute("create table base.t as select 1 as id, 10 as amt")
    db.execute("create table head.t as select 1 as id, 12 as amt")
    assert run(keys={"t": ["id"]}, sums=True)["t"].sum_shift == {"amt": (10.0, 12.0)}
    assert run(keys={"t": ["id"]})["t"].sum_shift == {}
