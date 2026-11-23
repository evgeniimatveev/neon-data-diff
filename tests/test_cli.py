"""CLI tests. Need a throwaway Postgres: set TEST_DATABASE_URL."""
import os

import psycopg
import pytest

from neon_data_diff.cli import main

DSN = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not DSN, reason="TEST_DATABASE_URL not set")


@pytest.fixture()
def db(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", DSN)
    with psycopg.connect(DSN, autocommit=True) as c:
        for s in ("base", "head"):
            c.execute(f"drop schema if exists {s} cascade")
            c.execute(f"create schema {s}")
        c.execute("create table base.t as select g as id, g as v from generate_series(1,10) g")
        c.execute("create table head.t as select g as id, g as v from generate_series(1,10) g")
        yield c
        for s in ("base", "head"):
            c.execute(f"drop schema if exists {s} cascade")


ARGS = ["--base-schema", "base", "--head-schema", "head", "--key", "t=id"]


def test_identical_exits_zero_even_with_fail_on_change(db, capsys):
    assert main(ARGS + ["--fail-on-change"]) == 0
    assert "No differences" in capsys.readouterr().out


def test_fail_on_change_exits_one(db):
    db.execute("update head.t set v = 0 where id = 1")
    assert main(ARGS) == 0
    assert main(ARGS + ["--fail-on-change"]) == 1


def test_max_change_pct_threshold(db):
    db.execute("update head.t set v = 0 where id <= 2")  # 2 of 10 rows = 20%
    assert main(ARGS + ["--max-change-pct", "50"]) == 0
    assert main(ARGS + ["--max-change-pct", "10"]) == 1


def test_markdown_file_written(db, tmp_path):
    out = tmp_path / "diff.md"
    main(ARGS + ["--markdown", str(out)])
    assert out.read_text(encoding="utf-8").startswith("<!-- neon-data-diff -->")


def test_missing_dsn_env_exits_two(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    assert main(["--base-schema", "a", "--head-schema", "b"]) == 2


def test_post_comment_without_token_is_skipped_not_fatal(db, monkeypatch, capsys):
    for k in ("GITHUB_TOKEN", "GITHUB_REPOSITORY", "GITHUB_REF"):
        monkeypatch.delenv(k, raising=False)
    assert main(ARGS + ["--post-comment"]) == 0
    assert "skipped" in capsys.readouterr().err


def test_composite_key_via_cli(db):
    db.execute("create table base.c as select g as a, g % 2 as b, g as v from generate_series(1,6) g")
    db.execute("create table head.c as select g as a, g % 2 as b, g as v from generate_series(1,6) g")
    db.execute("update head.c set v = 99 where a = 3")
    assert main(["--base-schema", "base", "--head-schema", "head", "--only", "c", "--key", "c=a,b", "--fail-on-change"]) == 1
