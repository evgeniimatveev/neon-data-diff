# 🔍 neon-data-diff

**Tests say your dbt data is valid. This tells you what changed.** A data diff for pull requests: it runs your dbt project twice on a throwaway Neon branch (base code vs PR code), compares the two result sets in SQL, and comments the difference on the PR.

![Python](https://img.shields.io/badge/Python-3.10%E2%80%933.13-3776AB?logo=python&logoColor=white)
![PostgreSQL](https://img.shields.io/badge/PostgreSQL-16%20%7C%2017%20%7C%2018-4169E1?logo=postgresql&logoColor=white)
![Neon](https://img.shields.io/badge/Neon-branching-00E599?logo=neon&logoColor=white)
![dbt](https://img.shields.io/badge/dbt-1.12-FF694B?logo=dbt&logoColor=white)
![GitHub Actions](https://img.shields.io/badge/GitHub_Action-composite-2088FF?logo=githubactions&logoColor=white)
[![CI](https://github.com/evgeniimatveev/neon-data-diff/actions/workflows/ci.yml/badge.svg)](https://github.com/evgeniimatveev/neon-data-diff/actions/workflows/ci.yml)
[![Release](https://img.shields.io/github/v/release/evgeniimatveev/neon-data-diff?color=00E599)](https://github.com/evgeniimatveev/neon-data-diff/releases)
![License](https://img.shields.io/badge/License-MIT-lightgrey)

![neon-data-diff banner](assets/banner.svg)

**[Interactive demo site →](https://evgeniimatveev.github.io/neon-data-diff/)** (flip one line of code and watch the diff react) · **[Real pull request →](https://github.com/evgeniimatveev/uber-driver-analytics/pull/12)** (intentionally not merged)

---

## The problem

A passing test suite proves the data is still *valid*. It does not prove the data is still *the same*. A refactor can keep every test green and quietly move your numbers.

On my own Uber driver data I changed one line in a staging model: the trip timestamp converted to a date in Los Angeles time became a date in UTC. All 20 dbt tests still passed. But trips near midnight slid into neighbouring months, 13 of 27 monthly rows changed, and one month disappeared. Nothing in a test suite flags that. A diff does:

![The bot comment on the pull request](assets/screenshots/pr-comment.png)

## See it in action

This is the real pull request: the change, the commit, and the comment the Action posted by itself.

![The pull request conversation](assets/screenshots/pr-conversation.png)

Every number in that comment comes from SQL running inside the database, so no table data leaves it. Only counts come back. Sample rows are opt-in, because in a public repo a comment would publish your data.

## Findings so far

The tool leaves its own track record here, rebuilt daily from real runs (not typed by hand).

<!-- FINDINGS:START -->
<details>
<summary><b>📈 Findings so far</b> <em>(auto-updated from real runs · click to expand)</em></summary>

#### Pull-request flow
2 pull request(s) checked in uber-driver-analytics: **1 with data changes**, 1 clean.

| Pull request | Result | What moved | Most touched table | Checked |
|---|---|---|---|---|
| [#13](https://github.com/evgeniimatveev/uber-driver-analytics/pull/13) Run neon-data-diff on every dbt PR | ✅ no differences | 2 table(s) identical | 0% | merged · 2026-11-24 |
| [#12](https://github.com/evgeniimatveev/uber-driver-analytics/pull/12) Demo: UTC date + neon-data-diff PR check | ⚠️ 1 of 2 table(s) changed | `mart_monthly_trips` 27 → 26 (+0 −1 ~13) | 51.9% | closed · 2026-11-23 |

#### Daily flow (drift watch)
No daily run with the drift check yet. The first one is the next scheduled run of `job-market-pulse`; this table fills in on its own.

<sub>Built by <code>scripts/build_findings.py</code> from public GitHub data (the bot's sticky comments and the daily workflow's runs). Nothing here is typed by hand.</sub>

</details>
<!-- FINDINGS:END -->

## How it works

```mermaid
flowchart LR
    PR(["pull request<br/>touches dbt/"]) --> A["neon-data-diff<br/>GitHub Action"]
    A -->|"create branch"| N[("Neon branch<br/>copy-on-write of prod")]
    A -->|"dbt run, main code<br/>DBT_SCHEMA=base"| B["base_marts"]
    A -->|"dbt run, PR code<br/>DBT_SCHEMA=head"| H["head_marts"]
    N --- B
    N --- H
    B --> D{{"neon-data-diff<br/>SQL diff"}}
    H --> D
    D -->|"sticky comment"| C(["PR comment"])
    D -.->|"threshold breached"| F(["check fails"])
    A -->|"always: delete branch"| X(["branch gone"])
```

### Step by step

The log lines below are real, copied from the Action run of the demo pull request.

**1. A pull request touches `dbt/`.** The workflow's `paths` filter means unrelated PRs cost nothing.

**2. Branch the database.** The Action creates a Neon branch of production. A branch is a copy-on-write clone: it appeared in about 0.6 seconds in the run below and takes no extra storage until data on it changes.

```
Branch ndd/pr-45288459016-2 created successfully
```

**3. `dbt run` with the base code.** The target branch is checked out into a separate worktree and built into schema `base_*` (`DBT_SCHEMA=base`). The profile comes from the PR head, so both runs share one connection setup.

```
1 of 5 OK created sql view model base_staging.stg_payments
3 of 5 OK created sql view model base_staging.stg_trips
5 of 5 OK created sql table model base_marts.mart_monthly_trips ........ [SELECT 27]
Done. PASS=5 WARN=0 ERROR=0 SKIP=0 NO-OP=0 REUSED=0 TOTAL=5
```

**4. `dbt run` with the PR code** into schema `head_*` of the *same* branch. Same project, same data, different code, so anything that differs afterwards is caused by the pull request and not by fresher data.

```
3 of 5 OK created sql view model head_staging.stg_trips
5 of 5 OK created sql table model head_marts.mart_monthly_trips ........ [SELECT 26]
Done. PASS=5 WARN=0 ERROR=0 SKIP=0 NO-OP=0 REUSED=0 TOTAL=5
```

**5. Diff, in SQL.** For each table the engine takes the key from your dbt `unique` tests (via the manifest) and runs queries like these inside Postgres:

```sql
-- removed rows: in base, not in head
select count(*) from base_marts.mart_monthly_trips b
left join head_marts.mart_monthly_trips h on b.month = h.month
where h.month is null;                                   -- 1

-- changed rows, plus a count per column (same pattern for each column)
select count(*) filter (where
         (b.gross_fare_usd is distinct from h.gross_fare_usd
          and not abs(b.gross_fare_usd - h.gross_fare_usd) <= 1e-09) or ...),
       count(*) filter (where ... completed_trips ...), ...
from base_marts.mart_monthly_trips b
join head_marts.mart_monthly_trips h on b.month = h.month;   -- 13
```

**6. Comment and clean up.** One comment is created, found later by a hidden marker (`<!-- neon-data-diff -->`) and updated in place on every push, never duplicated. The branch is deleted in a step that always runs, even when the check fails.

```
PR comment updated
```

## Design decisions

- **One branch, two schemas.** Separate branches have separate endpoints and cannot be joined, so base and head are built into two schemas of one branch and compared with plain SQL joins.
- **Comparison runs in the database.** No table data is pulled into Python or into logs. Only counts come back, which is also what makes it safe on private data.
- **Keys come from your tests.** A `unique` (or `dbt_utils.unique_combination_of_columns`) test is already a statement of what identifies a row, so the manifest is the source of truth. No key, or a key that is not actually unique, falls back to a set comparison (`EXCEPT ALL`), and the report says so.
- **`NULL` to value is a change.** The check uses `IS DISTINCT FROM`, not `<>`, which would silently skip `NULL`s.
- **Float noise is not a change.** Numeric columns use a tolerance (default `1e-9`) so `0.1 + 0.2` vs `0.3` is not reported.
- **Opt-in for anything that could leak or mislead.** Sample rows are off by default (public repos). `sum()` shifts are off by default (summing averages or percentages is meaningless).
- **Base and head use the same profile**, taken from the PR head, so a change to the connection setup cannot make base and head differ.
- **`dbt run`, not `dbt build`.** The Action compares data, it does not replace your test job.

## Infrastructure

![Infrastructure: pull request, Action, Neon branch with two dbt runs, SQL diff engine, PR comment](assets/infrastructure.svg)

| Layer | Tool | Role |
|---|---|---|
| Database | Neon (Postgres 18) | copy-on-write branch per PR, deleted afterwards |
| Transform | dbt-core 1.12 + dbt-postgres | builds the models twice, base and head |
| Diff engine | Python + psycopg 3 / DuckDB | generates the comparison SQL, runs it in the database (Postgres schemas or two DuckDB files) |
| Delivery | GitHub composite Action | create branch, run dbt, diff, comment, cleanup |
| Comment | GitHub REST API | one comment found by a hidden marker, updated in place |

### CI/CD

| Workflow | Trigger | What it runs | Status |
|---|---|---|---|
| [CI](.github/workflows/ci.yml) | push to `main`, every PR | pytest on Python 3.10 / 3.12 / 3.13 x Postgres 16 / 17 (6 jobs, DuckDB tests included), plus a check that `action.yml` parses | [![CI](https://github.com/evgeniimatveev/neon-data-diff/actions/workflows/ci.yml/badge.svg)](https://github.com/evgeniimatveev/neon-data-diff/actions/workflows/ci.yml) |
| [Findings](.github/workflows/findings.yml) | daily + manual | rebuilds the "Findings so far" block above from public GitHub data, commits only when it changed | [![Findings](https://github.com/evgeniimatveev/neon-data-diff/actions/workflows/findings.yml/badge.svg)](https://github.com/evgeniimatveev/neon-data-diff/actions/workflows/findings.yml) |

44 tests: the diff engine against a real Postgres (added / removed / changed rows, tolerance, NULLs, schema changes, unkeyed fallback, composite keys), the same engine on two DuckDB files (growth vs rewritten history), the CLI (exit codes, thresholds, missing config), the renderer, the manifest key reader, the sticky-comment logic against a fake GitHub API, and the findings-block builder.

## Quick start (GitHub Action)

```yaml
permissions:
  contents: read
  pull-requests: write          # for the sticky comment

steps:
  - uses: actions/checkout@v7
  - uses: evgeniimatveev/neon-data-diff@v0
    with:
      neon-api-key: ${{ secrets.NEON_API_KEY }}
      neon-project-id: ${{ vars.NEON_PROJECT_ID }}
```

Full workflow: [`examples/neon-data-diff.yml`](examples/neon-data-diff.yml).

**Contract with your dbt project.** The profile (taken from the PR head for both runs) must read its connection from environment variables:

```yaml
host: "{{ env_var('DBT_HOST') }}"
user: "{{ env_var('DBT_USER') }}"
password: "{{ env_var('DBT_PASSWORD') }}"
dbname: "{{ env_var('DBT_DBNAME') }}"
schema: "{{ env_var('DBT_SCHEMA') }}"
```

With a custom schema `marts`, dbt writes `base_marts` and `head_marts`, which are the schemas compared (`schema-suffix` input).

| Input | Default | Meaning |
|---|---|---|
| `neon-api-key` / `neon-project-id` | required | Neon credentials (secret and variable) |
| `dbt-dir` | `dbt` | dbt project directory |
| `install-command` | `pip install -r requirements.txt` | run inside `dbt-dir` |
| `schema-suffix` | `marts` | which schemas to compare |
| `max-change-pct` | none | fail the check if a table has more than N% of rows touched |
| `samples` | `0` | sample changed rows per table (keep 0 on public repos) |
| `database` / `role` / `base-ref` / `python-version` / `github-token` | sensible defaults | see `action.yml` |

## Drift watch (DuckDB, daily pipelines)

The same engine compares **two DuckDB files**: yesterday's database and today's. Both are attached into one session, so tables are joined directly. This catches a pipeline that silently rewrites history: for append-only data, new rows are normal, but changed or removed old rows are not.

```bash
pip install "neon-data-diff[duckdb] @ git+https://github.com/evgeniimatveev/neon-data-diff"

neon-data-diff   --duckdb-base yesterday.duckdb --duckdb-head today.duckdb   --key events=event_id --key daily_stats=day,city   --base-label yesterday --head-label today --title "Data drift"   --max-rewrite-pct 0          # growth is fine, any rewritten row fails
```

No extra storage is needed: your pipeline already downloads the database before it runs, so copy that file first and diff after. A full workflow that does this and opens an issue when history changes: [`examples/drift-watch.yml`](examples/drift-watch.yml).

```
## Data drift
`yesterday` (base) vs `today` (head)

**History rewritten** (removed + changed rows above 0% of base): `job_market_history` 1 row(s)

| Table | Rows base -> head | Added | Removed | Changed | Touched |
| `job_market_history` | 9,240 -> 9,350 | 110 | 0 | 1 | 1.2% |
```

The output above is from a simulation on the real schema of a daily job-market pipeline: 110 appended rows (normal) and one silently edited historical row (flagged).

## What it compares

- **Schema:** added, removed and retyped columns; added and removed tables.
- **Keyed tables:** anti-joins on the key for added and removed rows, and a per-column count of changed rows.
- **Unkeyed tables** (no key, or key not unique): `EXCEPT ALL` in both directions. A changed row then shows up as one removed plus one added, and the report says so.

## CLI

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
| `--manifest` | dbt `manifest.json`, source of keys |
| `--key TABLE=COL[,COL]` | explicit key, repeatable (composite keys supported) |
| `--tolerance` | numeric tolerance, default `1e-9` |
| `--samples N` | include up to N changed rows per table (off by default) |
| `--sums` | also report `sum()` shifts (off by default) |
| `--only TABLE` | limit to some tables |
| `--fail-on-change` | exit 1 if anything differs |
| `--max-change-pct N` | exit 1 if added+removed+changed rows exceed N% of base rows in any table |
| `--max-rewrite-pct N` | exit 1 if removed+changed rows exceed N% of base rows (growth by added rows is allowed; 0 = any rewrite fails) |
| `--duckdb-base` / `--duckdb-head` / `--duckdb-schema` | DuckDB mode: compare two database files (default schema `main`) |
| `--base-label` / `--head-label` / `--title` | names and heading used in the report |
| `--post-comment` | create or update the sticky PR comment (`GITHUB_TOKEN`, `GITHUB_REPOSITORY`, PR from `--pr` or `GITHUB_REF`) |

## FAQ

**Does it touch my production database?** No. Production is only the source the branch is cloned from. Every write (both dbt runs) happens on the clone, which is deleted at the end.

**What does it cost?** Storage only for data that changes on the branch, plus the compute time of a run that lasts about a minute. The workflow itself is two `dbt run`s plus a few queries. Check your own Neon plan for exact limits.

**Can it run on pull requests from forks?** No: forks get no secrets and a read-only token. For a personal or internal repo this does not matter.

**Why not just compare `dbt test` results?** Tests assert rules you already thought of. A diff shows what you did not think of.

**Is it Neon-only?** No. The diff engine works on any Postgres with two schemas, and on two DuckDB files (drift watch). The pull-request Action is built around Neon branches.

## Limitations

- Postgres mode: both schemas must live in the same database. DuckDB mode: two files, attached read-only.
- Non-deterministic models (e.g. `current_timestamp`) always show as changed.
- The Action is verified end to end on one project (the demo above), not yet on a wide range of dbt setups.
- Similar tools exist (Datafold data-diff, dbt-audit-helper, Recce). This one is a small, Neon-branch-native alternative with no extra infrastructure.

## Development

```bash
# needs a throwaway Postgres; the tests create and drop the schemas "base" and "head"
TEST_DATABASE_URL=postgresql://user@localhost:5432/scratch uv run --with-editable ".[dev]" pytest

# rebuild the demo site (docs/index.html) after editing docs/template.html or docs/data.json
python docs/build.py
```

Do not point `TEST_DATABASE_URL` at a database you care about.

---

*Built by Evgenii Matveev · MIT licensed*
