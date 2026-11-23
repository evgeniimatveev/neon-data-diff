# neon-data-diff

A data diff for dbt models on Postgres/Neon. It compares two schemas inside one database and tells you what a code change did to your data: schema changes, added/removed/changed rows, and which columns moved.

The comparison runs entirely in SQL, so no table data leaves the database. Only counts (and optional sample rows) come back.

## Live demo

[uber-driver-analytics PR #12](https://github.com/evgeniimatveev/uber-driver-analytics/pull/12) switches one staging column from Los Angeles time to UTC. All 20 dbt tests pass, and the bot comment shows what moved. That PR is intentionally not merged.

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

## GitHub Action

```yaml
- uses: actions/checkout@v7
- uses: evgeniimatveev/neon-data-diff@v0
  with:
    neon-api-key: ${{ secrets.NEON_API_KEY }}
    neon-project-id: ${{ vars.NEON_PROJECT_ID }}
```

Full workflow: [`examples/neon-data-diff.yml`](examples/neon-data-diff.yml). The job needs `pull-requests: write` to post the comment.

What it does on each pull request: creates a Neon branch, runs `dbt run` for the base code and for the PR code into `base_*` and `head_*` schemas, diffs them, posts one comment (updated on every push, never duplicated), and deletes the branch.

**Contract with your dbt project.** The profile (taken from the PR head for both runs) must read its connection from environment variables:

```yaml
host: "{{ env_var('DBT_HOST') }}"
user: "{{ env_var('DBT_USER') }}"
password: "{{ env_var('DBT_PASSWORD') }}"
dbname: "{{ env_var('DBT_DBNAME') }}"
schema: "{{ env_var('DBT_SCHEMA') }}"
```

With a custom schema `marts`, dbt writes `base_marts` and `head_marts`, which are the schemas compared (`schema-suffix` input).

Inputs: `dbt-dir` (default `dbt`), `install-command`, `database`, `role`, `schema-suffix`, `base-ref`, `max-change-pct`, `samples`, `python-version`, `github-token`.

Pull requests from forks get no secrets and a read-only token, so the check cannot run there. For a personal or internal repo this does not matter.

---

## Usage (CLI)

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
| `--manifest` | dbt `manifest.json`; keys come from `unique` and dbt_utils `unique_combination_of_columns` tests |
| `--key TABLE=COL[,COL]` | explicit key, repeatable (composite keys supported here) |
| `--tolerance` | numeric tolerance, default `1e-9`, ignores float noise |
| `--samples N` | include up to N changed rows per table. Off by default: in a public repo a PR comment would publish your data |
| `--sums` | also report `sum()` shifts (meaningless for averages and percentages, so opt-in) |
| `--only TABLE` | limit to some tables |
| `--fail-on-change` | exit 1 if anything differs |
| `--max-change-pct N` | exit 1 if added+removed+changed rows exceed N% of base rows in any table |
| `--post-comment` | create or update the sticky PR comment (`GITHUB_TOKEN`, `GITHUB_REPOSITORY`, PR from `--pr` or `GITHUB_REF`) |

## What it compares

- **Schema:** added, removed and retyped columns; added and removed tables.
- **Keyed tables:** anti-joins on the key for added/removed rows, and a per-column count of changed rows. `NULL` to value counts as a change.
- **Unkeyed tables** (no key, or key not unique): falls back to `EXCEPT ALL` in both directions. A changed row shows up as one removed plus one added, and the report says so.

## Limitations

- Both schemas must be in the same database.
- Non-deterministic models (e.g. `current_timestamp`) will always show as changed.
- `dbt run` is used, not `build`: run your tests in your normal CI job.
- Similar tools exist (Datafold data-diff, dbt-audit-helper, Recce). This one is a small, Neon-branch-native alternative with no extra infrastructure.

## Development

```bash
uv run --with-editable ".[dev]" pytest      # needs a throwaway Postgres
TEST_DATABASE_URL=postgresql://user@localhost:5432/scratch uv run --with-editable ".[dev]" pytest
```

Tests create and drop the schemas `base` and `head`. Do not point them at a database you care about.
