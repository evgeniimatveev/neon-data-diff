#!/usr/bin/env python3
"""Rebuild the "Findings so far" block of the README from public GitHub data.

Stdlib only. Idempotent: the README is rewritten only when the block changes.

Sources (all public):
* pull-request flow: PRs of the client repos that carry the bot's sticky comment
  (found by its hidden marker); the comment text is parsed, nothing is invented;
* daily flow: runs of the daily pipeline workflow and the conclusion of its
  "Data drift check" step, plus the "Data drift" issues it opens.

Set GITHUB_TOKEN to avoid the unauthenticated rate limit.
"""
from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from pathlib import Path

README = Path(os.getenv("FINDINGS_README", "README.md"))
MARKER = "<!-- neon-data-diff -->"
PR_REPOS = ["evgeniimatveev/uber-driver-analytics"]
DAILY_REPO = "evgeniimatveev/job-market-pulse"
DAILY_WORKFLOW = "pipeline.yml"
DAILY_STEP = "Data drift check (neon-data-diff)"
MAX_DAILY_ROWS = 14
START, END = "<!-- FINDINGS:START -->", "<!-- FINDINGS:END -->"

CLEAN = re.compile(r"No differences across (\d+) table")
CHANGED = re.compile(r"(\d+) of (\d+) table\(s\) changed")
ROW = re.compile(
    r"^\| `(?P<table>[^`]+)` \| (?P<rows>[^|]+) \| (?P<added>[^|]+) \| (?P<removed>[^|]+) \| (?P<changed>[^|]+) \| (?P<touched>[^|]+) \|\s*$",
    re.M,
)


# ------------------------------------------------------------------ GitHub API ---
def api(path: str):
    req = urllib.request.Request(
        "https://api.github.com" + path,
        headers={"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"},
    )
    token = os.getenv("GITHUB_TOKEN")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise


def paged(path: str, pages: int = 3) -> list:
    out: list = []
    sep = "&" if "?" in path else "?"
    for page in range(1, pages + 1):
        chunk = api(f"{path}{sep}per_page=100&page={page}")
        if not chunk:
            break
        if isinstance(chunk, dict):  # e.g. workflow runs / jobs wrappers
            chunk = next((v for v in chunk.values() if isinstance(v, list)), [])
        out += chunk
        if len(chunk) < 100:
            break
    return out


# ---------------------------------------------------------------------- parsing ---
def parse_comment(body: str) -> dict:
    """Turn the bot's markdown comment into a small record. Never guesses: unknown stays unknown."""
    m = CLEAN.search(body)
    if m:
        return {"verdict": "clean", "tables": int(m[1]), "changed_tables": 0, "rows": [], "max_touched": 0.0}
    m = CHANGED.search(body)
    if not m:
        return {"verdict": "unknown", "tables": None, "changed_tables": 0, "rows": [], "max_touched": 0.0}
    rows = [{k: v.strip() for k, v in r.groupdict().items()} for r in ROW.finditer(body)]
    touched = []
    for r in rows:
        try:
            touched.append(float(r["touched"].rstrip("%")))
        except ValueError:
            touched.append(100.0 if "table" in r["rows"] else 0.0)
    return {
        "verdict": "changed",
        "tables": int(m[2]),
        "changed_tables": int(m[1]),
        "rows": rows,
        "max_touched": max(touched, default=0.0),
    }


def _esc(text: str) -> str:
    return text.replace("|", "\\|").replace("\n", " ")


def _short(text: str, n: int = 52) -> str:
    return text if len(text) <= n else text[: n - 1].rstrip() + "…"


def describe_rows(rows: list[dict]) -> str:
    parts = []
    for r in rows:
        if "new table" in r["rows"] or "removed" in r["rows"]:
            parts.append(f"`{r['table']}` {r['rows']}")
        else:
            parts.append(f"`{r['table']}` {r['rows'].replace('->', '→')} (+{r['added']} −{r['removed']} ~{r['changed']})")
    return "<br>".join(parts) if parts else "-"


# ------------------------------------------------------------------ collection ---
def collect_pr_findings() -> list[dict]:
    found = []
    for repo in PR_REPOS:
        for pr in paged(f"/repos/{repo}/pulls?state=all"):
            comments = paged(f"/repos/{repo}/issues/{pr['number']}/comments")
            bot = [c for c in comments if MARKER in (c.get("body") or "")]
            if not bot:
                continue
            c = bot[-1]  # the sticky comment is updated in place; take the latest
            rec = parse_comment(c["body"])
            rec.update(
                repo=repo, number=pr["number"], title=pr["title"], url=pr["html_url"],
                state="merged" if pr.get("merged_at") else pr["state"],
                date=(c.get("updated_at") or c["created_at"])[:10],
            )
            found.append(rec)
    return sorted(found, key=lambda r: (r["date"], r["number"]), reverse=True)


def collect_daily_findings() -> dict:
    runs = (api(f"/repos/{DAILY_REPO}/actions/workflows/{DAILY_WORKFLOW}/runs?per_page=30&status=completed") or {}).get("workflow_runs", [])
    issues = [i for i in paged(f"/repos/{DAILY_REPO}/issues?state=all") if "pull_request" not in i and i["title"].startswith("Data drift")]
    rows = []
    for run in runs:
        jobs = (api(f"/repos/{DAILY_REPO}/actions/runs/{run['id']}/jobs") or {}).get("jobs", [])
        step = next((s for j in jobs for s in j.get("steps", []) if s["name"] == DAILY_STEP), None)
        if step is None:
            continue  # runs from before the drift check existed
        rows.append({"date": run["created_at"][:10], "url": run["html_url"], "conclusion": step["conclusion"], "event": run["event"]})
        if len(rows) >= MAX_DAILY_ROWS:
            break
    return {"rows": rows, "issues": issues}


# -------------------------------------------------------------------- rendering ---
def render(pr_findings: list[dict], daily: dict) -> str:
    out = [START, "<details>", "<summary><b>📈 Findings so far</b> <em>(auto-updated from real runs · click to expand)</em></summary>", ""]

    out.append("#### Pull-request flow")
    if pr_findings:
        changed = sum(1 for f in pr_findings if f["verdict"] == "changed")
        repos = ", ".join(sorted({f["repo"].split("/")[1] for f in pr_findings}))
        out += [
            f"{len(pr_findings)} pull request(s) checked in {repos}: **{changed} with data changes**, {len(pr_findings) - changed} clean.",
            "",
            "| Pull request | Result | What moved | Most touched table | Checked |",
            "|---|---|---|---|---|",
        ]
        for f in pr_findings:
            if f["verdict"] == "clean":
                result, moved, touched = "✅ no differences", f"{f['tables']} table(s) identical", "0%"
            elif f["verdict"] == "changed":
                result = f"⚠️ {f['changed_tables']} of {f['tables']} table(s) changed"
                moved, touched = describe_rows(f["rows"]), f"{f['max_touched']:.1f}%"
            else:
                result, moved, touched = "❔ unreadable report", "-", "-"
            out.append(
                f"| [#{f['number']}]({f['url']}) {_esc(_short(f['title']))} | {result} | {moved} | {touched} | {f['state']} · {f['date']} |"
            )
    else:
        out.append("No pull request with a data-diff comment found yet.")

    out += ["", "#### Daily flow (drift watch)"]
    rows, issues = daily["rows"], daily["issues"]
    if rows:
        ok = sum(1 for r in rows if r["conclusion"] == "success")
        out += [
            f"Last {len(rows)} daily run(s) with the drift check: **{ok} without a flagged rewrite**, {len(rows) - ok} flagged or failed. "
            f"Drift issues opened so far: **{len(issues)}**.",
            "",
            "| Date | Run | Drift check |",
            "|---|---|---|",
        ]
        for r in rows:
            if r["conclusion"] == "success":
                cell = "✅ passed (growth only, or skipped on a first run)"
            elif r["conclusion"] == "failure":
                same_day = [i for i in issues if i["title"].endswith(r["date"])]
                cell = f"⚠️ history rewritten, issue [#{same_day[0]['number']}]({same_day[0]['html_url']})" if same_day else "⚠️ check failed, see the run"
            else:
                cell = str(r["conclusion"])
            out.append(f"| {r['date']} | [{r['event']}]({r['url']}) | {cell} |")
    else:
        out.append(f"No daily run with the drift check yet. The first one is the next scheduled run of `{DAILY_REPO.split('/')[1]}`; this table fills in on its own.")

    out += [
        "",
        f"<sub>Built by <code>scripts/build_findings.py</code> from public GitHub data (the bot's sticky comments and the daily workflow's runs). Nothing here is typed by hand.</sub>",
        "",
        "</details>",
        END,
    ]
    return "\n".join(out)


def main() -> None:
    text = README.read_text(encoding="utf-8")
    if START not in text or END not in text:
        raise SystemExit(f"README is missing the {START} / {END} markers")
    block = render(collect_pr_findings(), collect_daily_findings())
    new = re.sub(re.escape(START) + r".*?" + re.escape(END), lambda _: block, text, count=1, flags=re.S)
    if new != text:
        README.write_text(new, encoding="utf-8", newline="\n")
        print("[findings] README updated")
    else:
        print("[findings] no change")


if __name__ == "__main__":
    main()
