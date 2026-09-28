import csv
import time

from askai import usage


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


def test_a_table_is_markdown_padded_to_line_up():
    assert usage.table(["kind", "cost"], [["critic", "3%"], ["worker", "12.5%"]]) == [
        "| kind   |  cost |",
        "| ------ | ----: |",
        "| critic |    3% |",
        "| worker | 12.5% |"]


def test_a_table_lists_what_its_headers_mean_under_it():
    lines = usage.table(["kind", "cost"], [["critic", "3%"]], notes={"cost": "% of the window"})
    assert lines[-2:] == ["", "- cost: % of the window"]


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


def test_a_window_not_yet_started_reads_as_one_starting_now():
    window = usage.line("5h limit:", 0, usage.unstarted("5h limit:", None))
    assert (window.left, round(window.resets - time.time())) == (100, usage.FIVE_HOURS)
    assert usage.unstarted("Weekly limit:", 123.0) == 123.0


def test_an_agy_limit_logged_without_a_fraction_is_used_up(monkeypatch):
    log = ['"displayName": "Five Hour Limit Remaining"', '"resetTime": "2099-01-01T00:00:00Z"',
           '"displayName": "Weekly Limit Remaining"', '"resetTime": "2099-01-02T00:00:00Z"',
           '"remainingFraction": 0.25', '"displayName": "Gemini Models"']

    def agy(argv, **kwargs):
        open(argv[argv.index("--log-file") + 1], "w", encoding="utf-8").write(chr(10).join(log))
    monkeypatch.setattr(usage, "cli", lambda name: name)
    monkeypatch.setattr(usage.subprocess, "run", agy)
    group, five, week = usage.agy()
    assert group.strip() == "Gemini Models"
    assert (five.left, week.left) == (0, 25)
