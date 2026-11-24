"""Compare two namespaces of one database, entirely in SQL.

Two backends share one engine:

* Postgres (e.g. a Neon branch): base and head are two *schemas* of one database.
* DuckDB: base and head are two *files*, attached into one session as catalogs
  ``base`` and ``head`` (so they can be joined directly).

A namespace is ``schema`` (Postgres) or ``catalog.schema`` (DuckDB). Nothing is pulled
into Python except counts and optional sample rows.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

# run(sql) -> (rows, column_names)
Runner = Callable[[str], "tuple[list[tuple], list[str]]"]

_NUMERIC = {
    "smallint", "integer", "int", "bigint", "hugeint", "tinyint", "utinyint", "usmallint",
    "uinteger", "ubigint", "real", "float", "float4", "float8", "double", "double precision",
}


def is_numeric(dtype: str) -> bool:
    d = dtype.lower()
    return d in _NUMERIC or d.startswith(("numeric", "decimal"))


def qi(name: str) -> str:
    """Quote an identifier."""
    return '"' + name.replace('"', '""') + '"'


def ql(value: object) -> str:
    """Quote a string literal."""
    return "'" + str(value).replace("'", "''") + "'"


def rel(namespace: str, table: str) -> str:
    return ".".join(qi(p) for p in namespace.split(".")) + "." + qi(table)


def _ns_filter(namespace: str) -> str:
    parts = namespace.split(".")
    if len(parts) == 2:  # catalog.schema (DuckDB)
        return f"table_catalog = {ql(parts[0])} and table_schema = {ql(parts[1])}"
    return f"table_schema = {ql(namespace)}"


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
    def rewrite_pct(self) -> float:
        """Share of base rows that were removed or changed (history rewritten). Growth (added rows) is not counted.

        A removed table counts as 100, a new table as 0.
        """
        if self.status == "removed":
            return 100.0
        if self.status == "added":
            return 0.0
        return 100.0 * (self.rows_removed + (self.rows_changed or 0)) / max(self.base_rows, 1)

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


def rewrite_breaches(diffs: list[TableDiff], max_rewrite_pct: float) -> list[TableDiff]:
    return [d for d in diffs if d.rewrite_pct > max_rewrite_pct]


# ------------------------------------------------------------------ runners ---
def pg_runner(conn) -> Runner:
    def run(sql: str):
        with conn.cursor() as cur:
            cur.execute(sql)
            if cur.description is None:
                return [], []
            return cur.fetchall(), [d.name for d in cur.description]

    return run


def duckdb_runner(con) -> Runner:
    def run(sql: str):
        cur = con.execute(sql)
        return cur.fetchall(), [d[0] for d in cur.description]

    return run


# ---------------------------------------------------------------- the engine ---
def _one(run: Runner, sql: str):
    return run(sql)[0][0][0]


def _relations(run: Runner, namespace: str) -> set[str]:
    rows, _ = run(f"select table_name from information_schema.tables where {_ns_filter(namespace)}")
    return {r[0] for r in rows}


def _columns(run: Runner, namespace: str, table: str) -> dict[str, str]:
    rows, _ = run(
        "select column_name, data_type from information_schema.columns "
        f"where {_ns_filter(namespace)} and table_name = {ql(table)} order by ordinal_position"
    )
    return {c: t for c, t in rows}


def _count(run: Runner, namespace: str, table: str) -> int:
    return _one(run, f"select count(*) from {rel(namespace, table)}")


def _is_unique(run: Runner, namespace: str, table: str, key: list[str]) -> bool:
    cols = ", ".join(qi(k) for k in key)
    r = rel(namespace, table)
    return bool(_one(run, f"select count(*) = (select count(*) from (select distinct {cols} from {r}) d) from {r}"))


def _differs(col: str, dtype: str, tol: float) -> str:
    b, h = f"b.{qi(col)}", f"h.{qi(col)}"
    if is_numeric(dtype):
        return f"({b} is distinct from {h} and not ({b} is not null and {h} is not null and abs({b} - {h}) <= {float(tol)!r}))"
    return f"({b} is distinct from {h})"


def diff_table(
    run: Runner,
    base: str,
    head: str,
    table: str,
    key: list[str] | None,
    tolerance: float = 1e-9,
    samples: int = 0,
    sums: bool = False,
) -> TableDiff:
    bcols, hcols = _columns(run, base, table), _columns(run, head, table)
    d = TableDiff(table=table)
    d.columns_added = [c for c in hcols if c not in bcols]
    d.columns_removed = [c for c in bcols if c not in hcols]
    d.columns_retyped = [(c, bcols[c], hcols[c]) for c in hcols if c in bcols and bcols[c] != hcols[c]]
    d.base_rows, d.head_rows = _count(run, base, table), _count(run, head, table)

    common = [c for c in hcols if c in bcols]
    if not common:
        d.notes.append("no common columns; row comparison skipped")
        return d

    use_key = None
    if key:
        if not all(k in bcols and k in hcols for k in key):
            d.notes.append(f"key {key} missing in one schema; fell back to unkeyed")
        elif not (_is_unique(run, base, table, key) and _is_unique(run, head, table, key)):
            d.notes.append(f"key {key} is not unique; fell back to unkeyed")
        else:
            use_key = key

    b_rel, h_rel = rel(base, table), rel(head, table)
    if use_key:
        d.key, d.mode = use_key, "keyed"
        on = " and ".join(f"b.{qi(k)} = h.{qi(k)}" for k in use_key)
        first = qi(use_key[0])
        d.rows_added = _one(run, f"select count(*) from {h_rel} h left join {b_rel} b on {on} where b.{first} is null")
        d.rows_removed = _one(run, f"select count(*) from {b_rel} b left join {h_rel} h on {on} where h.{first} is null")

        value_cols = [c for c in common if c not in use_key]
        if value_cols:
            checks = {c: _differs(c, hcols[c], tolerance) for c in value_cols}
            any_diff = " or ".join(checks.values())
            per_col = ", ".join(f"count(*) filter (where {checks[c]})" for c in value_cols)
            rows, _ = run(f"select count(*) filter (where {any_diff}), {per_col} from {b_rel} b join {h_rel} h on {on}")
            row = rows[0]
            d.rows_changed = row[0]
            d.changed_by_column = {c: n for c, n in zip(value_cols, row[1:]) if n}
            if samples and d.rows_changed:
                sel = ", ".join(
                    [f"b.{qi(k)} as {qi(k)}" for k in use_key]
                    + [f"b.{qi(c)} as {qi(c + '__base')}, h.{qi(c)} as {qi(c + '__head')}" for c in d.changed_by_column]
                )
                srows, names = run(f"select {sel} from {b_rel} b join {h_rel} h on {on} where {any_diff} limit {int(samples)}")
                d.samples = [dict(zip(names, r)) for r in srows]
        else:
            d.rows_changed = 0
    else:
        d.mode = "unkeyed"
        cols = ", ".join(qi(c) for c in common)
        d.rows_added = _one(run, f"select count(*) from (select {cols} from {h_rel} except all select {cols} from {b_rel}) x")
        d.rows_removed = _one(run, f"select count(*) from (select {cols} from {b_rel} except all select {cols} from {h_rel}) x")
        d.rows_changed = None
        d.notes.append("no unique key: changed rows show up as removed + added")

    if sums:
        for c in common:
            if is_numeric(hcols[c]) and is_numeric(bcols[c]) and (not use_key or c not in use_key):
                sb = _one(run, f"select sum({qi(c)}) from {b_rel}")
                sh = _one(run, f"select sum({qi(c)}) from {h_rel}")
                if sb != sh:
                    d.sum_shift[c] = (None if sb is None else float(sb), None if sh is None else float(sh))
    return d


def diff_namespaces(
    run: Runner,
    base: str,
    head: str,
    keys: dict[str, list[str]] | None = None,
    tolerance: float = 1e-9,
    samples: int = 0,
    only: set[str] | None = None,
    sums: bool = False,
) -> list[TableDiff]:
    keys = keys or {}
    out: list[TableDiff] = []
    base_t, head_t = _relations(run, base), _relations(run, head)
    for t in sorted(base_t | head_t):
        if only and t not in only:
            continue
        if t not in base_t:
            out.append(TableDiff(table=t, status="added", head_rows=_count(run, head, t)))
        elif t not in head_t:
            out.append(TableDiff(table=t, status="removed", base_rows=_count(run, base, t)))
        else:
            out.append(diff_table(run, base, head, t, keys.get(t), tolerance, samples, sums))
    return out


# ---------------------------------------------------------- backend entry points ---
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
    """Postgres: compare two schemas of one database."""
    import psycopg

    with psycopg.connect(dsn, autocommit=True) as conn:
        return diff_namespaces(pg_runner(conn), base_schema, head_schema, keys, tolerance, samples, only, sums)


def diff_duckdb(
    base_path: str,
    head_path: str,
    schema: str = "main",
    keys: dict[str, list[str]] | None = None,
    tolerance: float = 1e-9,
    samples: int = 0,
    only: set[str] | None = None,
    sums: bool = False,
) -> list[TableDiff]:
    """DuckDB: attach two database files read-only and compare them (base = older, head = newer)."""
    try:
        import duckdb
    except ImportError as e:  # pragma: no cover
        raise SystemExit("DuckDB support needs the extra: pip install 'neon-data-diff[duckdb]'") from e

    con = duckdb.connect(":memory:")
    try:
        con.execute(f"attach {ql(base_path)} as base (read_only)")
        con.execute(f"attach {ql(head_path)} as head (read_only)")
        return diff_namespaces(duckdb_runner(con), f"base.{schema}", f"head.{schema}", keys, tolerance, samples, only, sums)
    finally:
        con.close()
