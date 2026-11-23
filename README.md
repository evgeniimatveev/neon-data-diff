# neon-data-diff

A data diff for dbt models on Postgres/Neon. It compares two schemas inside one database and tells you what a code change did to your data: schema changes, added/removed/changed rows, and which columns moved.

The comparison runs entirely in SQL, so no table data leaves the database. Only counts (and optional sample rows) come back.

## Why

Passing dbt tests tell you the data is still *valid*. They do not tell you the data *changed*. A refactor can keep every test green while shifting numbers.

Example from my own Uber driver data: changing one line in a staging model from Los Angeles time to UTC keeps all 20 tests passing, but the diff shows it:

```
| Table              | Rows base -> head | Added | Removed | Changed |
| `mart_monthly_trips` | 27 -> 26        | 0     | 1       | 13      |

- `completed_trips`: 11 row(s) changed
- `gross_fare_usd`: 13 row(s) changed
```

Trips near midnight moved into neighbouring months, and one month disappeared.

## How it fits with Neon

Neon branches are copy-on-write, so a CI job can branch production, run dbt twice into two schemas of that one branch (base = code from `main`, head = code from the PR), diff the two, and delete the branch. Production is never written to. Cross-branch joins are impossible (separate endpoints), which is why both runs share one branch.

```
production --branch--> pr-branch
                         |- base_marts  (dbt build, main code)
                         |- head_marts  (dbt build, PR code)
                       neon-data-diff base_marts head_marts --> PR comment
```

## Usage

```bash
pip install git+https://github.com/evgeniimatveev/neon-data-diff   # not on PyPI

export DATABASE_URL=postgresql://...
neon-data-diff \
  --base-schema base_marts --head-schema head_marts \
  --manifest dbt/target/manifest.json \
  --markdown diff.md
```

| Option | Meaning |
|---|---|
| `--manifest` | dbt `manifest.json`; single-column keys come from `unique` tests |
| `--key TABLE=COL[,COL]` | explicit key, repeatable (composite keys supported here) |
| `--tolerance` | numeric tolerance, default `1e-9`, ignores float noise |
| `--samples N` | include up to N changed rows per table. Off by default: in a public repo a PR comment would publish your data |
| `--sums` | also report `sum()` shifts (meaningless for averages and percentages, so opt-in) |
| `--only TABLE` | limit to some tables |
| `--fail-on-change` | exit 1 if anything differs |

## What it compares

- **Schema:** added, removed and retyped columns; added and removed tables.
- **Keyed tables:** anti-joins on the key for added/removed rows, and a per-column count of changed rows. `NULL` to value counts as a change.
- **Unkeyed tables** (no key, or key not unique): falls back to `EXCEPT ALL` in both directions. A changed row shows up as one removed plus one added, and the report says so.

## Limitations

- Both schemas must be in the same database.
- Non-deterministic models (e.g. `current_timestamp`) will always show as changed.
- Only single-column keys are read from the manifest.
- Similar tools exist (Datafold data-diff, dbt-audit-helper, Recce). This one is a small, Neon-branch-native alternative with no extra infrastructure.

## Development

```bash
uv run --with-editable ".[dev]" pytest      # needs a throwaway Postgres
TEST_DATABASE_URL=postgresql://user@localhost:5432/scratch uv run --with-editable ".[dev]" pytest
```

Tests create and drop the schemas `base` and `head`. Do not point them at a database you care about.
