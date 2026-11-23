"""Compare two schemas inside one Postgres database, entirely in SQL.

Both schemas must live in the same database (for Neon: build the base and head
dbt runs into two schemas of one branch). Nothing is pulled into Python except
counts and optional sample rows.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import psycopg
from psycopg import sql

NUMERIC_TYPES = {"smallint", "integer", "bigint", "numeric", "real", "double precision"}


@dataclass
class TableDiff:
    table: str
    status: str = "compared"  # compared | added | removed
    base_rows: int = 0
    head_rows: int = 0
    columns_added: list[str] = field(default_factory=list)
    columns_removed: list[str] = field(default_factory=list)
    columns_retyped: list[tuple[str, str, str]] = field(default_factory=list)
    key: list[str] | None = None
    mode: str = "keyed"  # keyed | unkeyed
    rows_added: int = 0
    rows_removed: int = 0
    rows_changed: int | None = None  # None when unknown (unkeyed)
    changed_by_column: dict[str, int] = field(default_factory=dict)
    sum_shift: dict[str, tuple[float | None, float | None]] = field(default_factory=dict)
    samples: list[dict] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def change_pct(self) -> float:
        """Share of base rows touched (added + removed + changed). New/removed tables count as 100."""
        if self.status != "compared":
            return 100.0
        touched = self.rows_added + self.rows_removed + (self.rows_changed or 0)
        return 100.0 * touched / max(self.base_rows, 1)

    @property
    def has_changes(self) -> bool:
        return bool(
            self.status != "compared"
            or self.columns_added
            or self.columns_removed
            or self.columns_retyped
            or self.rows_added
            or self.rows_removed
            or self.rows_changed
        )


def breaches(diffs: list[TableDiff], max_change_pct: float) -> list[TableDiff]:
    return [d for d in diffs if d.change_pct > max_change_pct]


def _relations(cur, schema: str) -> set[str]:
    cur.execute(
        "select table_name from information_schema.tables where table_schema = %s",
        (schema,),
    )
    return {r[0] for r in cur.fetchall()}


def _columns(cur, schema: str, table: str) -> dict[str, str]:
    cur.execute(
        "select column_name, data_type from information_schema.columns "
        "where table_schema = %s and table_name = %s order by ordinal_position",
        (schema, table),
    )
    return dict(cur.fetchall())


def _rel(schema: str, table: str) -> sql.Composed:
    return sql.SQL("{}.{}").format(sql.Identifier(schema), sql.Identifier(table))


def _count(cur, schema: str, table: str) -> int:
    cur.execute(sql.SQL("select count(*) from {}").format(_rel(schema, table)))
    return cur.fetchone()[0]


def _is_unique(cur, schema: str, table: str, key: list[str]) -> bool:
    cols = sql.SQL(", ").join(sql.Identifier(k) for k in key)
    cur.execute(
        sql.SQL("select count(*) = (select count(*) from (select distinct {c} from {r}) d) from {r}").format(
            c=cols, r=_rel(schema, table)
        )
    )
    return cur.fetchone()[0]


def _differs(col: str, dtype: str, tol: float) -> sql.Composed:
    b = sql.SQL("b.{}").format(sql.Identifier(col))
    h = sql.SQL("h.{}").format(sql.Identifier(col))
    if dtype in NUMERIC_TYPES:
        return sql.SQL(
            "({b} is distinct from {h} and not ({b} is not null and {h} is not null "
            "and abs({b} - {h}) <= {tol}))"
        ).format(b=b, h=h, tol=sql.Literal(tol))
    return sql.SQL("({b} is distinct from {h})").format(b=b, h=h)


def diff_table(
    cur,
    base: str,
    head: str,
    table: str,
    key: list[str] | None,
    tolerance: float = 1e-9,
    samples: int = 0,
    sums: bool = False,
) -> TableDiff:
    bcols, hcols = _columns(cur, base, table), _columns(cur, head, table)
    d = TableDiff(table=table)
    d.columns_added = [c for c in hcols if c not in bcols]
    d.columns_removed = [c for c in bcols if c not in hcols]
    d.columns_retyped = [(c, bcols[c], hcols[c]) for c in hcols if c in bcols and bcols[c] != hcols[c]]
    d.base_rows, d.head_rows = _count(cur, base, table), _count(cur, head, table)

    common = [c for c in hcols if c in bcols]
    if not common:
        d.notes.append("no common columns; row comparison skipped")
        return d

    use_key = None
    if key:
        if not all(k in bcols and k in hcols for k in key):
            d.notes.append(f"key {key} missing in one schema; fell back to unkeyed")
        elif not (_is_unique(cur, base, table, key) and _is_unique(cur, head, table, key)):
            d.notes.append(f"key {key} is not unique; fell back to unkeyed")
        else:
            use_key = key

    b_rel, h_rel = _rel(base, table), _rel(head, table)
    if use_key:
        d.key, d.mode = use_key, "keyed"
        on = sql.SQL(" and ").join(
            sql.SQL("b.{k} = h.{k}").format(k=sql.Identifier(k)) for k in use_key
        )
        first = sql.Identifier(use_key[0])
        cur.execute(
            sql.SQL("select count(*) from {h} h left join {b} b on {on} where b.{f} is null").format(
                h=h_rel, b=b_rel, on=on, f=first
            )
        )
        d.rows_added = cur.fetchone()[0]
        cur.execute(
            sql.SQL("select count(*) from {b} b left join {h} h on {on} where h.{f} is null").format(
                h=h_rel, b=b_rel, on=on, f=first
            )
        )
        d.rows_removed = cur.fetchone()[0]

        value_cols = [c for c in common if c not in use_key]
        if value_cols:
            checks = {c: _differs(c, hcols[c], tolerance) for c in value_cols}
            any_diff = sql.SQL(" or ").join(checks.values())
            per_col = sql.SQL(", ").join(
                sql.SQL("count(*) filter (where {})").format(checks[c]) for c in value_cols
            )
            cur.execute(
                sql.SQL("select count(*) filter (where {any}), {per} from {b} b join {h} h on {on}").format(
                    any=any_diff, per=per_col, b=b_rel, h=h_rel, on=on
                )
            )
            row = cur.fetchone()
            d.rows_changed = row[0]
            d.changed_by_column = {c: n for c, n in zip(value_cols, row[1:]) if n}
            if samples and d.rows_changed:
                sel = sql.SQL(", ").join(
                    [sql.SQL("b.{k} as {k}").format(k=sql.Identifier(k)) for k in use_key]
                    + [
                        sql.SQL("b.{c} as {bn}, h.{c} as {hn}").format(
                            c=sql.Identifier(c),
                            bn=sql.Identifier(f"{c}__base"),
                            hn=sql.Identifier(f"{c}__head"),
                        )
                        for c in d.changed_by_column
                    ]
                )
                cur.execute(
                    sql.SQL("select {sel} from {b} b join {h} h on {on} where {any} limit {n}").format(
                        sel=sel, b=b_rel, h=h_rel, on=on, any=any_diff, n=sql.Literal(samples)
                    )
                )
                names = [x.name for x in cur.description]
                d.samples = [dict(zip(names, r)) for r in cur.fetchall()]
    else:
        d.mode = "unkeyed"
        cols = sql.SQL(", ").join(sql.Identifier(c) for c in common)
        cur.execute(
            sql.SQL("select count(*) from (select {c} from {h} except all select {c} from {b}) x").format(
                c=cols, h=h_rel, b=b_rel
            )
        )
        d.rows_added = cur.fetchone()[0]
        cur.execute(
            sql.SQL("select count(*) from (select {c} from {b} except all select {c} from {h}) x").format(
                c=cols, h=h_rel, b=b_rel
            )
        )
        d.rows_removed = cur.fetchone()[0]
        d.rows_changed = None
        d.notes.append("no unique key: changed rows show up as removed + added")

    for c in (common if sums else []):
        if hcols[c] in NUMERIC_TYPES and bcols[c] in NUMERIC_TYPES and (not use_key or c not in use_key):
            cur.execute(sql.SQL("select sum({c}) from {b}").format(c=sql.Identifier(c), b=b_rel))
            sb = cur.fetchone()[0]
            cur.execute(sql.SQL("select sum({c}) from {h}").format(c=sql.Identifier(c), h=h_rel))
            sh = cur.fetchone()[0]
            if sb != sh:
                d.sum_shift[c] = (None if sb is None else float(sb), None if sh is None else float(sh))
    return d


def diff_schemas(
    dsn: str,
    base_schema: str,
    head_schema: str,
    keys: dict[str, list[str]] | None = None,
    tolerance: float = 1e-9,
    samples: int = 0,
    only: set[str] | None = None,
    sums: bool = False,
) -> list[TableDiff]:
    keys = keys or {}
    out: list[TableDiff] = []
    with psycopg.connect(dsn, autocommit=True) as conn, conn.cursor() as cur:
        base_t, head_t = _relations(cur, base_schema), _relations(cur, head_schema)
        for t in sorted(base_t | head_t):
            if only and t not in only:
                continue
            if t not in base_t:
                out.append(TableDiff(table=t, status="added", head_rows=_count(cur, head_schema, t)))
            elif t not in head_t:
                out.append(TableDiff(table=t, status="removed", base_rows=_count(cur, base_schema, t)))
            else:
                out.append(diff_table(cur, base_schema, head_schema, t, keys.get(t), tolerance, samples, sums))
    return out
