from __future__ import annotations

from .diff import TableDiff, breaches, rewrite_breaches

MARKER = "<!-- neon-data-diff -->"


def _fmt(n: float | None) -> str:
    if n is None:
        return "NULL"
    return f"{n:,.2f}".rstrip("0").rstrip(".") if isinstance(n, float) else f"{n:,}"


def _rewritten(d: TableDiff) -> str:
    if d.status == "removed":
        return f"{d.base_rows:,} (table removed)"
    return f"{d.rows_removed + (d.rows_changed or 0):,}"


def render_markdown(
    diffs: list[TableDiff],
    base: str,
    head: str,
    show_samples: bool = False,
    max_change_pct: float | None = None,
    max_rewrite_pct: float | None = None,
    title: str = "Data diff",
) -> str:
    changed = [d for d in diffs if d.has_changes]
    lines = [MARKER, f"## {title}", f"`{base}` (base) vs `{head}` (head)", ""]
    if not changed:
        lines.append(f"No differences across {len(diffs)} table(s).")
        return "\n".join(lines) + "\n"

    lines += [f"{len(changed)} of {len(diffs)} table(s) changed.", ""]
    if max_change_pct is not None:
        over = breaches(diffs, max_change_pct)
        if over:
            lines += [
                f"**Threshold exceeded** (max {max_change_pct:g}% of rows): "
                + ", ".join(f"`{d.table}` {d.change_pct:.1f}%" for d in over),
                "",
            ]
    if max_rewrite_pct is not None:
        over = rewrite_breaches(diffs, max_rewrite_pct)
        if over:
            lines += [
                f"**History rewritten** (removed + changed rows above {max_rewrite_pct:g}% of base): "
                + ", ".join(f"`{d.table}` {_rewritten(d)} row(s)" for d in over),
                "",
            ]
    lines += [
        "| Table | Rows base -> head | Added | Removed | Changed | Touched |",
        "|---|---|---|---|---|---|",
    ]
    for d in changed:
        if d.status == "added":
            lines.append(f"| `{d.table}` | new table | {d.head_rows:,} | - | - | - |")
        elif d.status == "removed":
            lines.append(f"| `{d.table}` | table removed | - | {d.base_rows:,} | - | - |")
        else:
            ch = "n/a" if d.rows_changed is None else f"{d.rows_changed:,}"
            lines.append(
                f"| `{d.table}` | {d.base_rows:,} -> {d.head_rows:,} | {d.rows_added:,} | "
                f"{d.rows_removed:,} | {ch} | {d.change_pct:.1f}% |"
            )

    for d in changed:
        if d.status != "compared":
            continue
        detail: list[str] = []
        if d.columns_added:
            detail.append("- columns added: " + ", ".join(f"`{c}`" for c in d.columns_added))
        if d.columns_removed:
            detail.append("- columns removed: " + ", ".join(f"`{c}`" for c in d.columns_removed))
        for c, a, b in d.columns_retyped:
            detail.append(f"- `{c}` type changed: {a} -> {b}")
        for c, n in d.changed_by_column.items():
            detail.append(f"- `{c}`: {n:,} row(s) changed")
        for c, (a, b) in d.sum_shift.items():
            detail.append(f"- sum(`{c}`): {_fmt(a)} -> {_fmt(b)}")
        for n in d.notes:
            detail.append(f"- note: {n}")
        if show_samples and d.samples:
            detail.append("- sample changed rows:")
            for s in d.samples:
                detail.append("  - " + ", ".join(f"{k}={v}" for k, v in s.items()))
        if detail:
            lines += ["", f"<details><summary><code>{d.table}</code> details</summary>", ""] + detail + ["", "</details>"]
    return "\n".join(lines) + "\n"
