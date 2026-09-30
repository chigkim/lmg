import csv
import json
import time

from llm_gateway import usage


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

    def agy(argv, timeout, cwd):
        open(argv[argv.index("--log-file") + 1], "w", encoding="utf-8").write(chr(10).join(log))
        return 0, ""
    monkeypatch.setattr(usage, "cli", lambda name: name)
    monkeypatch.setattr(usage, "ran", agy)
    group, five, week = usage.agy()
    assert group.strip() == "Gemini Models"
    assert (five.left, week.left) == (0, 25)


def test_a_claude_window_this_does_not_name_is_left_out_not_read_as_a_group(monkeypatch):
    event = {"type": "rate_limit_event", "rate_limit_info": {"unifiedWindows": {
        "seven_day_opus": {"utilization": 0.5, "resetsAt": 4102444800},
        "five_hour": {"utilization": 0.2, "resetsAt": 4102444800},
        "seven_day": {"utilization": 0.1, "resetsAt": 4102444800}}}}
    monkeypatch.setattr(usage, "cli", lambda name: name)
    monkeypatch.setattr(usage, "ran", lambda argv, timeout, cwd: (0, json.dumps(event)))
    lines = usage._claude_by_call()
    assert [line.split(":")[0].strip() for line in lines] == ["5h limit", "Weekly limit"]


def test_every_window_is_one_row_of_one_table_with_a_signed_spare():
    soon = time.time() + usage.FIVE_HOURS * 0.5
    report = ["Now 6:00 AM on 30 Sep", "Codex", usage.line("5h limit:", 70, soon),
              "agy", usage.Group("  Gemini Models"), usage.line("5h limit:", 20, soon, indent="    "),
              usage.Group("  Claude and GPT models"), usage.line("5h limit:", 0, soon, indent="    "),
              "Claude", "  could not ask: no answer"]
    lines = usage.shown(report)
    assert lines[:2] == ["Now 6:00 AM on 30 Sep", ""]
    assert [cell.strip() for cell in lines[2].split("|")[1:-1]] == ["window", "left", "spare", "resets"]
    body = {line.split("|")[1].strip(): [cell.strip() for cell in line.split("|")[2:4]] for line in lines[4:7]}
    # Half the window to run: 30% left is 20% behind, 80% left is 30% ahead, and agy's two groups are told apart.
    assert body == {"Codex 5h": ["30%", "-20%"], "agy Gemini 5h": ["80%", "+30%"],
                    "agy Claude and GPT 5h": ["100%", "+50%"]}
    assert lines[-1] == "Claude: could not ask: no answer"
