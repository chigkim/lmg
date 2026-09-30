import pytest
import csv
import json
import http.client
import io
import urllib.error
import urllib.request
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
    assert usage.span(2 * 3600 + 42 * 60) == "2h42m"
    assert usage.span(3 * 86400 + 4 * 3600 + 5 * 60) == "3d4h"
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


def test_a_word_that_is_not_a_note_is_refused_before_any_plan_is_asked(monkeypatch):
    monkeypatch.setattr(usage, "asked", lambda: pytest.fail("asked"))
    monkeypatch.setattr(usage, "record", lambda note, report: pytest.fail("recorded"))
    assert usage.main(["plan"]) == 2
    assert usage.main(["weekly", "pace", "added"]) == 2
    assert usage.NOTE.match("before: session x") and usage.NOTE.match("reading: pace")


class Server:
    """A `codex app-server` that answers from a script."""

    def __init__(self, replies):
        self.stdin = self
        self.stdout = iter(json.dumps(reply) + "\n" for reply in replies)

    def write(self, text):
        pass

    def flush(self):
        pass


def test_a_codex_window_not_yet_started_reads_as_one_starting_now(monkeypatch):
    limits = {"primary": {"usedPercent": 0, "resetsAt": None},
              "secondary": {"usedPercent": 40, "resetsAt": 4102444800}}
    monkeypatch.setattr(usage, "cli", lambda name: name)
    monkeypatch.setattr(usage, "kill", lambda server: None)
    monkeypatch.setattr(usage.subprocess, "Popen", lambda *a, **k: Server(
        [{"id": 1, "result": {}}, {"id": 2, "result": {"rateLimits": limits}}]))
    five, week = usage.codex()
    assert (five.left, round(five.resets - time.time())) == (100, usage.FIVE_HOURS)
    assert (week.left, week.resets) == (60, 4102444800)


def claude_endpoint(monkeypatch, tmp_path, opened):
    login = {"claudeAiOauth": {"accessToken": "sk-secret", "expiresAt": 4102444800000}}
    (tmp_path / "login.json").write_text(json.dumps(login), encoding="utf-8")
    monkeypatch.setattr(usage, "CREDENTIALS", tmp_path / "login.json")
    monkeypatch.setattr(usage.OPENER, "open", opened)
    monkeypatch.setattr(usage, "_claude_by_call", lambda: ["  by call"])


@pytest.mark.parametrize("problem", [http.client.IncompleteRead(b""),
                                     urllib.error.HTTPError(usage.USAGE, 302, "Found", {}, None)])
def test_a_claude_request_that_fails_falls_back_to_the_call(monkeypatch, tmp_path, problem):
    def opened(request, timeout):
        raise problem
    claude_endpoint(monkeypatch, tmp_path, opened)
    assert usage.claude() == ["  by call"]


@pytest.mark.parametrize("body", ['{"five_hour": {"utilization": null, "resets_at": null}}', '[1]'])
def test_a_claude_reply_of_another_shape_falls_back_to_the_call(monkeypatch, tmp_path, body):
    claude_endpoint(monkeypatch, tmp_path, lambda request, timeout: io.BytesIO(body.encode()))
    assert usage.claude() == ["  by call"]


def test_the_claude_token_is_in_the_header_and_in_no_line(monkeypatch, tmp_path):
    sent = []

    def opened(request, timeout):
        sent.append(request.get_header("Authorization"))
        return io.BytesIO(b'{"five_hour": {"utilization": 20, "resets_at": "2099-01-01T00:00:00+00:00"}}')
    claude_endpoint(monkeypatch, tmp_path, opened)
    [five] = usage.claude()
    assert sent == ["Bearer sk-secret"] and five.left == 80 and "sk-secret" not in str(five)


def test_the_claude_opener_follows_no_redirect():
    [handler] = [h for h in usage.OPENER.handlers if isinstance(h, urllib.request.HTTPRedirectHandler)]
    assert handler.redirect_request(None, None, 302, "Found", {}, "https://elsewhere") is None


def test_what_could_not_be_read_is_named_for_its_plan():
    report = ["Now", "Claude", usage.line("5h limit:", 10, time.time() + 60), "Codex", "  could not ask: gone"]
    assert usage.unread(report) == ["Codex: could not ask: gone"]
