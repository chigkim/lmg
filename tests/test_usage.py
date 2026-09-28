import csv
import time

from ai_usage import usage


def test_a_window_spent_evenly_is_on_pace():
    halfway = time.time() + usage.FIVE_HOURS / 2
    window = usage.line("5h limit:", 50, halfway)
    assert (window.left, window.kind) == (50, "5h")
    assert "even pace 50%, 0% to spare" in window


def test_a_length_of_time_is_its_two_largest_units():
    assert usage.span(8 * 60) == "8m"
    assert usage.span(20 * 3600) == "20h"
    assert usage.span(2 * 3600 + 42 * 60) == "2h 42m"
    assert usage.span(3 * 86400 + 4 * 3600 + 5 * 60) == "3d 4h"
    assert usage.span(10) == "0m"


def test_a_window_spent_early_is_over_pace():
    window = usage.line("Weekly limit:", 60, time.time() + usage.WEEK * 0.9)
    assert window.kind == "week"
    assert "even pace 90%, 50% over pace" in window


def test_the_ledger_keeps_agy_gemini_group_and_skips_the_others():
    soon = time.time() + 3600
    report = ["Now", "Codex", usage.line("5h limit:", 10, soon),
              "agy", "  Claude and GPT", usage.line("5h limit:", 80, soon, indent="    "),
              "  Gemini", usage.line("5h limit:", 30, soon, indent="    ")]
    readings = usage.windows(report)
    assert {key: window.left for key, window in readings.items()} == {"codex_5h": 90, "agy_5h": 70}


def test_a_note_appends_one_row_under_one_header(tmp_path, monkeypatch):
    monkeypatch.setattr(usage, "LEDGER", tmp_path / "usage-ledger.csv")
    report = ["Now", "Codex", usage.line("Weekly limit:", 25, time.time() + 3600)]
    usage.record("before: a task", report)
    usage.record("after: a task", report)
    with usage.LEDGER.open(encoding="utf-8") as book:
        rows = list(csv.DictReader(book))
    assert [row["note"] for row in rows] == ["before: a task", "after: a task"]
    assert rows[0]["codex_week_left"] == "75" and rows[0]["claude_5h_left"] == ""
