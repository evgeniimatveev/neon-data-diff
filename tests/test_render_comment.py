"""Unit tests that need no database."""
import json

from neon_data_diff.cli import keys_from_manifest
from neon_data_diff.comment import pr_number_from_ref, upsert_comment
from neon_data_diff.diff import TableDiff, breaches
from neon_data_diff.render import MARKER, render_markdown


def changed_table(**kw):
    base = dict(table="t", base_rows=100, head_rows=101, rows_added=2, rows_removed=1, rows_changed=7,
                changed_by_column={"amt": 7}, key=["id"])
    base.update(kw)
    return TableDiff(**base)


def test_no_changes_message():
    md = render_markdown([TableDiff(table="t", base_rows=3, head_rows=3, rows_changed=0)], "base", "head")
    assert MARKER in md and "No differences across 1 table(s)." in md


def test_report_lists_counts_and_columns():
    md = render_markdown([changed_table()], "base", "head")
    assert "| `t` | 100 -> 101 | 2 | 1 | 7 | 10.0% |" in md
    assert "`amt`: 7 row(s) changed" in md


def test_samples_hidden_unless_requested():
    d = changed_table(samples=[{"id": 1, "amt__base": 1, "amt__head": 2}])
    assert "sample changed rows" not in render_markdown([d], "b", "h")
    assert "amt__head=2" in render_markdown([d], "b", "h", show_samples=True)


def test_added_and_removed_table_rows():
    md = render_markdown(
        [TableDiff(table="new", status="added", head_rows=5), TableDiff(table="old", status="removed", base_rows=4)],
        "b", "h",
    )
    assert "new table" in md and "table removed" in md


def test_threshold_breaches_and_banner():
    d = changed_table()  # 10% touched
    assert breaches([d], 5) == [d] and breaches([d], 10) == []
    assert "Threshold exceeded" in render_markdown([d], "b", "h", max_change_pct=5)
    assert "Threshold exceeded" not in render_markdown([d], "b", "h", max_change_pct=50)


def test_new_or_removed_table_counts_as_full_change():
    assert TableDiff(table="x", status="added").change_pct == 100.0


def test_manifest_keys_single_and_composite(tmp_path):
    def test_node(model, name, kwargs):
        return {"resource_type": "test", "attached_node": f"model.proj.{model}",
                "test_metadata": {"name": name, "kwargs": kwargs}}

    manifest = {"nodes": {
        "a": test_node("m1", "unique", {"column_name": "id"}),
        "b": test_node("m2", "unique_combination_of_columns", {"combination_of_columns": ["x", "y"]}),
        "c": test_node("m3", "unique_combination_of_columns", {"combination_of_columns": ["p", "q"]}),
        "d": test_node("m3", "unique", {"column_name": "id"}),
        "e": test_node("m1", "not_null", {"column_name": "id"}),
        "f": {"resource_type": "model"},
    }}
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(manifest))
    assert keys_from_manifest(str(path)) == {"m1": ["id"], "m2": ["x", "y"], "m3": ["id"]}


def test_pr_number_from_ref():
    assert pr_number_from_ref("refs/pull/42/merge") == 42
    assert pr_number_from_ref("refs/heads/main") is None and pr_number_from_ref(None) is None


class FakeGitHub:
    def __init__(self, comments):
        self.comments, self.calls = comments, []

    def __call__(self, method, url, token, data=None):
        self.calls.append((method, url, data))
        return self.comments if method == "GET" else {}


def test_upsert_creates_when_no_marker():
    gh = FakeGitHub([{"id": 1, "body": "unrelated"}])
    assert upsert_comment("o/r", 5, f"{MARKER}\nhi", "tok", request=gh) == "created"
    assert gh.calls[-1][0] == "POST" and gh.calls[-1][1].endswith("/issues/5/comments")


def test_upsert_updates_existing_marker_comment():
    gh = FakeGitHub([{"id": 1, "body": "x"}, {"id": 9, "body": f"{MARKER}\nold"}])
    assert upsert_comment("o/r", 5, f"{MARKER}\nnew", "tok", request=gh) == "updated"
    method, url, data = gh.calls[-1]
    assert method == "PATCH" and url.endswith("/issues/comments/9") and "new" in data["body"]
