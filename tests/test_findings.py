"""The README findings block: parsing the bot's comment and rendering. No network."""
import importlib.util
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "build_findings.py"
spec = importlib.util.spec_from_file_location("build_findings", SCRIPT)
bf = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bf)

CHANGED = """<!-- neon-data-diff -->
## Data diff
`base_marts` (base) vs `head_marts` (head)

1 of 2 table(s) changed.

| Table | Rows base -> head | Added | Removed | Changed | Touched |
|---|---|---|---|---|---|
| `mart_monthly_trips` | 27 -> 26 | 0 | 1 | 13 | 51.9% |
"""
CLEAN = "<!-- neon-data-diff -->\n## Data diff\n`a` (base) vs `b` (head)\n\nNo differences across 2 table(s).\n"


def test_parse_clean():
    r = bf.parse_comment(CLEAN)
    assert (r["verdict"], r["tables"], r["changed_tables"]) == ("clean", 2, 0)


def test_parse_changed_with_row():
    r = bf.parse_comment(CHANGED)
    assert (r["verdict"], r["tables"], r["changed_tables"]) == ("changed", 2, 1)
    assert r["rows"][0]["table"] == "mart_monthly_trips" and r["rows"][0]["changed"] == "13"
    assert r["max_touched"] == pytest.approx(51.9)


def test_parse_new_and_removed_tables_count_as_fully_touched():
    body = CHANGED.replace("| `mart_monthly_trips` | 27 -> 26 | 0 | 1 | 13 | 51.9% |", "| `fresh` | new table | 5 | - | - | - |")
    r = bf.parse_comment(body)
    assert r["max_touched"] == 100.0
    assert "new table" in bf.describe_rows(r["rows"])


def test_parse_unknown_is_not_guessed():
    r = bf.parse_comment("some unrelated text")
    assert r["verdict"] == "unknown" and r["tables"] is None


def pr(**kw):
    base = dict(repo="o/client", number=12, title="Demo | pipe", url="https://x/12", state="closed", date="2026-11-23", **bf.parse_comment(CHANGED))
    base.update(kw)
    return base


def test_render_collapsible_with_tables_and_escaping():
    md = bf.render([pr(), pr(number=13, title="Clean one", **bf.parse_comment(CLEAN))], {"rows": [], "issues": []})
    assert md.startswith(bf.START) and md.endswith(bf.END)
    assert "<details>" in md and "</details>" in md and "<summary>" in md
    assert "**1 with data changes**, 1 clean" in md
    assert "`mart_monthly_trips` 27 → 26 (+0 −1 ~13)" in md
    assert "Demo \\| pipe" in md  # a pipe in a title must not break the table
    assert "No daily run with the drift check yet" in md


def test_render_empty_states():
    md = bf.render([], {"rows": [], "issues": []})
    assert "No pull request with a data-diff comment found yet." in md


def test_render_daily_rows_and_issue_link():
    daily = {
        "rows": [
            {"date": "2026-11-25", "url": "https://r/2", "conclusion": "failure", "event": "schedule"},
            {"date": "2026-11-24", "url": "https://r/1", "conclusion": "success", "event": "schedule"},
        ],
        "issues": [{"title": "Data drift on 2026-11-25", "number": 7, "html_url": "https://i/7"}],
    }
    md = bf.render([], daily)
    assert "**1 without a flagged rewrite**, 1 flagged or failed" in md
    assert "history rewritten, issue [#7](https://i/7)" in md
    assert "Drift issues opened so far: **1**" in md


def test_failure_without_issue_is_a_failed_check_not_a_rewrite():
    daily = {"rows": [{"date": "2026-11-25", "url": "https://r/2", "conclusion": "failure", "event": "schedule"}], "issues": []}
    assert "check failed, see the run" in bf.render([], daily)


def test_main_is_idempotent_and_requires_markers(tmp_path, monkeypatch):
    readme = tmp_path / "README.md"
    readme.write_text(f"intro\n{bf.START}\nold\n{bf.END}\noutro\n", encoding="utf-8")
    monkeypatch.setattr(bf, "README", readme)
    monkeypatch.setattr(bf, "collect_pr_findings", lambda: [pr()])
    monkeypatch.setattr(bf, "collect_daily_findings", lambda: {"rows": [], "issues": []})
    bf.main()
    first = readme.read_text(encoding="utf-8")
    assert "old" not in first and first.startswith("intro\n") and first.endswith("outro\n")
    bf.main()
    assert readme.read_text(encoding="utf-8") == first  # second run changes nothing

    bad = tmp_path / "BAD.md"
    bad.write_text("no markers", encoding="utf-8")
    monkeypatch.setattr(bf, "README", bad)
    with pytest.raises(SystemExit):
        bf.main()
