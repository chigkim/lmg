import csv
import io
import json
import time

from llm_gateway import tasks, usage


def limits():
    return {"five_hour": {"utilization": 99, "resets_at": None}, "seven_day": None,
            "limits": [
                {"kind": "session", "percent": 7, "resets_at": None, "scope": None},
                {"kind": "weekly_scoped", "percent": 0, "resets_at": None,
                 "is_active": False, "scope": {"model": {"id": None, "display_name": "Fable"}}}]}


def test_current_limits_include_inactive_fable_without_duplicate_legacy_windows():
    five, fable = usage.claude_windows(limits())
    assert (five.kind, five.left) == ("5h", 93)
    assert (fable.kind, fable.left) == ("week_fable", 100)
    assert round(fable.resets - time.time()) == usage.WEEK
    assert fable.spare == 0
    report = ["Now", "Claude", five, fable]
    assert list(usage.windows(report)) == ["claude_5h", "claude_week_fable"]
    assert any("Claude Fable week" in row for row in usage.shown(report))


def test_scoped_fable_does_not_replace_the_all_models_week():
    data = limits()
    data["limits"].append({"kind": "weekly", "percent": 20, "resets_at": "2099-01-01T00:00:00Z"})
    data["limits"].append({"kind": "weekly_scoped", "percent": 50, "scope": {"model": {"display_name": "Other"}}})
    windows = usage.windows(["Now", "Claude", *usage.claude_windows(data)])
    assert windows["claude_week"].left == 80
    assert windows["claude_week_fable"].left == 100
    assert len(windows) == 3


def test_endpoint_reads_current_limits_without_a_model_call(tmp_path, monkeypatch):
    path = tmp_path / "credentials.json"
    path.write_text(json.dumps({"claudeAiOauth": {"accessToken": "secret", "expiresAt": 4102444800000}}))
    monkeypatch.setattr(usage, "CREDENTIALS", path)
    monkeypatch.setattr(usage.OPENER, "open", lambda *a, **k: io.BytesIO(json.dumps(limits()).encode()))
    monkeypatch.setattr(usage, "_claude_by_call", lambda: (_ for _ in ()).throw(AssertionError("model call")))
    assert [window.kind for window in usage.claude()] == ["5h", "week_fable"]


def test_record_extends_old_ledger_and_preserves_rows(tmp_path, monkeypatch):
    path = tmp_path / "usage-ledger.csv"
    old_fields = [field for field in usage.FIELDS if not field.startswith("claude_week_fable")]
    with path.open("w", newline="", encoding="utf-8") as book:
        writer = csv.DictWriter(book, old_fields)
        writer.writeheader()
        writer.writerow({"time": "2026-10-01 10:00:00", "note": "before: old task", "claude_week_left": 90})
    monkeypatch.setattr(usage, "LEDGER", path)
    fable = usage.claude_windows(limits())[1]
    usage.record("before: claude new task", ["Now", "Claude", fable])
    fable.left = 98
    usage.record("after: claude new task", ["Now", "Claude", fable])
    rows = tasks.ledger()
    assert len(rows) == 3
    assert rows[0]["claude_week_left"] == "90"
    assert rows[0]["claude_week_fable_left"] == ""
    assert [row["claude_week_fable_left"] for row in rows[1:]] == ["100", "98"]
    assert tasks.paired(rows)[0][2] == {"claude_week_fable": 2}
