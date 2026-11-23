from __future__ import annotations

import argparse
import json
import os
import sys

from .diff import diff_schemas
from .render import render_markdown


def keys_from_manifest(path: str) -> dict[str, list[str]]:
    """Single-column keys declared with dbt's `unique` test, by model name."""
    with open(path, encoding="utf-8") as f:
        manifest = json.load(f)
    keys: dict[str, list[str]] = {}
    for node in manifest.get("nodes", {}).values():
        if node.get("resource_type") != "test":
            continue
        meta = node.get("test_metadata") or {}
        if meta.get("name") != "unique":
            continue
        col = (meta.get("kwargs") or {}).get("column_name") or node.get("column_name")
        model = (node.get("attached_node") or "").split(".")[-1]
        if col and model:
            keys.setdefault(model, [col])
    return keys


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="neon-data-diff", description=__doc__)
    p.add_argument("--dsn-env", default="DATABASE_URL", help="env var holding the Postgres connection string")
    p.add_argument("--base-schema", required=True)
    p.add_argument("--head-schema", required=True)
    p.add_argument("--manifest", help="dbt manifest.json; keys are taken from unique tests")
    p.add_argument("--key", action="append", default=[], metavar="TABLE=COL[,COL]", help="explicit key (repeatable)")
    p.add_argument("--tolerance", type=float, default=1e-9, help="numeric tolerance for value changes")
    p.add_argument("--samples", type=int, default=0, help="max sample changed rows per table (0 = none; avoid on public repos)")
    p.add_argument("--sums", action="store_true", help="also report sum() shifts of numeric columns (meaningless for averages/percentages)")
    p.add_argument("--only", action="append", default=[], help="limit to these tables (repeatable)")
    p.add_argument("--markdown", help="write the report to this file")
    p.add_argument("--fail-on-change", action="store_true", help="exit 1 if any table differs")
    a = p.parse_args(argv)

    dsn = os.environ.get(a.dsn_env)
    if not dsn:
        print(f"error: env var {a.dsn_env} is not set", file=sys.stderr)
        return 2

    keys = keys_from_manifest(a.manifest) if a.manifest else {}
    for spec in a.key:
        table, _, cols = spec.partition("=")
        keys[table] = [c for c in cols.split(",") if c]

    diffs = diff_schemas(dsn, a.base_schema, a.head_schema, keys, a.tolerance, a.samples, set(a.only) or None, a.sums)
    md = render_markdown(diffs, a.base_schema, a.head_schema, show_samples=a.samples > 0)
    if a.markdown:
        with open(a.markdown, "w", encoding="utf-8") as f:
            f.write(md)
    print(md)
    return 1 if (a.fail_on_change and any(d.has_changes for d in diffs)) else 0


if __name__ == "__main__":
    raise SystemExit(main())
