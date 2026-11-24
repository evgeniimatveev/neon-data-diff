from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error

from .comment import pr_number_from_ref, upsert_comment
from .diff import breaches, diff_duckdb, diff_schemas, rewrite_breaches
from .render import render_markdown


def keys_from_manifest(path: str) -> dict[str, list[str]]:
    """Keys declared in dbt tests, by model name.

    Reads `unique` (single column) and dbt_utils `unique_combination_of_columns` (composite).
    A single-column `unique` wins over a composite one for the same model.
    """
    with open(path, encoding="utf-8") as f:
        manifest = json.load(f)
    single: dict[str, list[str]] = {}
    composite: dict[str, list[str]] = {}
    for node in manifest.get("nodes", {}).values():
        if node.get("resource_type") != "test":
            continue
        meta = node.get("test_metadata") or {}
        kwargs = meta.get("kwargs") or {}
        model = (node.get("attached_node") or "").split(".")[-1]
        if not model:
            continue
        if meta.get("name") == "unique":
            col = kwargs.get("column_name") or node.get("column_name")
            if col:
                single.setdefault(model, [col])
        elif meta.get("name") == "unique_combination_of_columns":
            cols = kwargs.get("combination_of_columns")
            if cols:
                composite.setdefault(model, list(cols))
    return {**composite, **single}


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="neon-data-diff",
        description="Diff two schemas of one Postgres database (e.g. dbt base vs head runs on a Neon branch).",
    )
    p.add_argument("--dsn-env", default="DATABASE_URL", help="env var holding the Postgres connection string")
    p.add_argument("--base-schema", help="Postgres: base schema")
    p.add_argument("--head-schema", help="Postgres: head schema")
    p.add_argument("--duckdb-base", metavar="FILE", help="DuckDB: older database file (base)")
    p.add_argument("--duckdb-head", metavar="FILE", help="DuckDB: newer database file (head)")
    p.add_argument("--duckdb-schema", default="main", help="DuckDB: schema to compare in both files (default: main)")
    p.add_argument("--base-label", help="name of the base side in the report")
    p.add_argument("--head-label", help="name of the head side in the report")
    p.add_argument("--title", default="Data diff", help="report heading")
    p.add_argument("--manifest", help="dbt manifest.json; keys come from unique / unique_combination_of_columns tests")
    p.add_argument("--key", action="append", default=[], metavar="TABLE=COL[,COL]", help="explicit key (repeatable)")
    p.add_argument("--tolerance", type=float, default=1e-9, help="numeric tolerance for value changes")
    p.add_argument("--samples", type=int, default=0, help="max sample changed rows per table (0 = none; avoid on public repos)")
    p.add_argument("--sums", action="store_true", help="also report sum() shifts of numeric columns (meaningless for averages/percentages)")
    p.add_argument("--only", action="append", default=[], help="limit to these tables (repeatable)")
    p.add_argument("--markdown", help="write the report to this file")
    p.add_argument("--fail-on-change", action="store_true", help="exit 1 if any table differs")
    p.add_argument("--max-change-pct", type=float, help="exit 1 if added+removed+changed rows exceed this %% of base rows in any table")
    p.add_argument("--max-rewrite-pct", type=float, help="exit 1 if removed+changed rows exceed this %% of base rows in any table (growth by added rows is allowed; 0 = any rewrite fails)")
    p.add_argument("--post-comment", action="store_true", help="create/update a sticky PR comment (needs GITHUB_TOKEN, GITHUB_REPOSITORY, PR from --pr or GITHUB_REF)")
    p.add_argument("--pr", type=int, help="pull request number for --post-comment")
    a = p.parse_args(argv)

    duck = bool(a.duckdb_base or a.duckdb_head)
    if duck and not (a.duckdb_base and a.duckdb_head):
        print("error: --duckdb-base and --duckdb-head go together", file=sys.stderr)
        return 2
    if not duck and not (a.base_schema and a.head_schema):
        print("error: give --base-schema and --head-schema (Postgres) or --duckdb-base and --duckdb-head", file=sys.stderr)
        return 2
    dsn = None
    if not duck:
        dsn = os.environ.get(a.dsn_env)
        if not dsn:
            print(f"error: env var {a.dsn_env} is not set", file=sys.stderr)
            return 2

    keys = keys_from_manifest(a.manifest) if a.manifest else {}
    for spec in a.key:
        table, _, cols = spec.partition("=")
        keys[table] = [c for c in cols.split(",") if c]

    only = set(a.only) or None
    if duck:
        diffs = diff_duckdb(a.duckdb_base, a.duckdb_head, a.duckdb_schema, keys, a.tolerance, a.samples, only, a.sums)
        base_label, head_label = a.base_label or a.duckdb_base, a.head_label or a.duckdb_head
    else:
        diffs = diff_schemas(dsn, a.base_schema, a.head_schema, keys, a.tolerance, a.samples, only, a.sums)
        base_label, head_label = a.base_label or a.base_schema, a.head_label or a.head_schema
    md = render_markdown(
        diffs, base_label, head_label, show_samples=a.samples > 0,
        max_change_pct=a.max_change_pct, max_rewrite_pct=a.max_rewrite_pct, title=a.title,
    )
    if a.markdown:
        with open(a.markdown, "w", encoding="utf-8") as f:
            f.write(md)
    print(md)

    if a.post_comment:
        token, repo = os.environ.get("GITHUB_TOKEN"), os.environ.get("GITHUB_REPOSITORY")
        pr = a.pr or pr_number_from_ref(os.environ.get("GITHUB_REF"))
        if not (token and repo and pr):
            print("warning: --post-comment needs GITHUB_TOKEN, GITHUB_REPOSITORY and a PR number; skipped", file=sys.stderr)
        else:
            try:
                print(f"PR comment {upsert_comment(repo, pr, md, token)}", file=sys.stderr)
            except urllib.error.HTTPError as e:  # e.g. 403 on fork PRs: do not fail the build for that
                print(f"warning: could not post PR comment ({e.code} {e.reason})", file=sys.stderr)

    failed = (
        (a.fail_on_change and any(d.has_changes for d in diffs))
        or (a.max_change_pct is not None and bool(breaches(diffs, a.max_change_pct)))
        or (a.max_rewrite_pct is not None and bool(rewrite_breaches(diffs, a.max_rewrite_pct)))
    )
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
