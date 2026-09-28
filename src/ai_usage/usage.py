"""How much of the Claude, Codex and agy plan windows is left.

    ai-usage                      # print it
    ai-usage "before: some task"  # print it and add a row to the ledger

Codex and agy are read without running a model: `codex app-server` answers
`account/rateLimits/read`, and `agy models` logs its quota. Claude is read
from the endpoint its own `/usage` reads, with the CLI's login token held in
memory for the one request and never printed or stored; a tiny Haiku call is
the fallback when that token has expired. The three run at once.

The ledger is `usage-ledger.csv` beside this file, so the tool is installed
editable (`uv tool install -e`) and the ledger stays in the checkout. It is
shared by every project that spends the same plans, since a plan's limit
counts all of them.
"""

import csv
import json
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

PROMPT = "Reply with the word ok and nothing else."


def cli(name):
    found = shutil.which(name)
    if not found:
        raise RuntimeError(f"{name} is not on PATH")
    return found


def when(epoch):
    moment = datetime.fromtimestamp(epoch)
    if moment.date() == datetime.now().date():
        return moment.strftime("%I:%M %p").lstrip("0")
    return moment.strftime("%I:%M %p on %d %b").lstrip("0")


def table(headers, rows, text=(0,), notes=None):
    """A Markdown table as lines, padded to line up in a terminal. The columns numbered in `text` align left;
    the rest are figures and align right. `notes`, {header: what it means}, is listed under the table."""
    cells = [[str(c) for c in headers]] + [[str(c) for c in row] for row in rows]
    widths = [max(len(row[i]) for row in cells) for i in range(len(headers))]

    def fit(row):
        return "| " + " | ".join(c.ljust(w) if i in text else c.rjust(w)
                                  for i, (c, w) in enumerate(zip(row, widths))) + " |"

    rule = "| " + " | ".join("-" * (w - 1) + ("-" if i in text else ":") for i, w in enumerate(widths)) + " |"
    legend = [""] + [f"- {header}: {meaning}" for header, meaning in notes.items()] if notes else []
    return [fit(cells[0]), rule] + [fit(row) for row in cells[1:]] + legend


def span(seconds):
    """A length of time in its two largest units: 40m, 2h 42m, 3d 4h."""
    minutes = max(round(seconds / 60), 0)
    days, hours, minutes = minutes // 1440, minutes // 60 % 24, minutes % 60
    parts = [f"{n}{unit}" for n, unit in ((days, "d"), (hours, "h"), (minutes, "m")) if n]
    return " ".join(parts[:2]) or "0m"


WEEK = 7 * 24 * 3600
FIVE_HOURS = 5 * 3600


class Window(str):
    """A printed window line that keeps its figures for the ledger."""


def line(label, used, resets, indent="  "):
    """One window, and how it stands against an even pace -- what would be
    left now if the window were spent evenly -- since a window used up early
    is time out of commission, and a high % left early in it is not room to
    spare. For a five-hour window that pace is 10% a half hour.
    """
    left = max(0, 100 - round(used))
    text = f"{indent}{label:<14}{left:>3}% left (resets {when(resets)}, in {span(resets - time.time())})"
    length = WEEK if label.startswith("Weekly") else FIVE_HOURS
    even = round(100 * min(max(resets - time.time(), 0), length) / length)
    margin = left - even
    text += (f"; even pace {even}%, " + (
        f"{margin}% to spare" if margin >= 0 else f"{-margin}% over pace"))
    window = Window(text)
    window.left, window.resets = left, resets
    window.kind = "week" if label.startswith("Weekly") else "5h"
    return window


CREDENTIALS = pathlib.Path.home() / ".claude" / ".credentials.json"
USAGE = "https://api.anthropic.com/api/oauth/usage"


def claude():
    """Claude's limits from the endpoint `/usage` reads, which is free;
    the Haiku call is kept for when the login token has expired.

    The token is read into one local, put in one header and dropped. It is
    never printed, logged or written, and nothing that fails here carries
    it: a failure is answered with its status code alone.
    """
    try:
        login = json.loads(CREDENTIALS.read_text(encoding="utf-8"))
        login = login["claudeAiOauth"]
        if login["expiresAt"] / 1000 <= time.time():
            raise LookupError("expired")
        request = urllib.request.Request(USAGE, headers={
            "Authorization": "Bearer " + login["accessToken"],
            "anthropic-beta": "oauth-2025-04-20"})
        del login
        with urllib.request.urlopen(request, timeout=30) as reply:
            windows = json.load(reply)
        del request
    except (OSError, LookupError, ValueError):
        # The token, the file, or the endpoint is unavailable; asking with a
        # call refreshes the login as a side effect.
        return _claude_by_call()
    names = {"five_hour": "5h limit:", "seven_day": "Weekly limit:"}
    return [line(label, windows[name]["utilization"],
                 datetime.fromisoformat(
                     windows[name]["resets_at"]).timestamp())
            for name, label in names.items() if windows.get(name)]


def _claude_by_call():
    run = subprocess.run(
        [cli("claude"), "-p", PROMPT, "--model", "claude-haiku-4-5-20251001",
         "--output-format", "stream-json", "--verbose", "--max-turns", "1"],
        capture_output=True, text=True, encoding="utf-8", timeout=180,
        cwd=pathlib.Path.home())
    windows = {}
    for raw in run.stdout.splitlines():
        if '"rate_limit_event"' not in raw:
            continue
        info = json.loads(raw)["rate_limit_info"]
        for name, window in info.get("unifiedWindows", {}).items():
            windows[name] = window
    if not windows:
        return ["  no rate_limit_event in the reply "
                f"(exit {run.returncode})"]
    names = {"five_hour": "5h limit:", "seven_day": "Weekly limit:"}
    return [line(names.get(name, name + ":"), window["utilization"] * 100,
                 window["resetsAt"])
            for name, window in windows.items()]


def codex():
    """Codex's limits from `codex app-server`, which reads them from the
    account without running a model, so asking costs nothing."""
    server = subprocess.Popen(
        [cli("codex"), "app-server"], stdin=subprocess.PIPE,
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
        encoding="utf-8", cwd=pathlib.Path.home())
    try:
        def send(message):
            server.stdin.write(json.dumps(message) + "\n")
            server.stdin.flush()

        def answer(number):
            for raw in server.stdout:
                message = json.loads(raw)
                if message.get("id") == number:
                    return message
            raise RuntimeError("codex app-server closed without answering")

        send({"id": 1, "method": "initialize",
              "params": {"clientInfo": {"name": "usage", "version": "0"}}})
        answer(1)
        send({"method": "initialized"})
        send({"id": 2, "method": "account/rateLimits/read",
              "params": {"excludeResetCreditDetails": True}})
        reply = answer(2)
    finally:
        server.kill()
    if "error" in reply:
        return [f"  codex refused: {reply['error'].get('message')}"]
    limits = reply["result"]["rateLimits"]
    out = []
    for key, label in (("primary", "5h limit:"),
                       ("secondary", "Weekly limit:")):
        window = limits.get(key)
        if window:
            out.append(line(label, window["usedPercent"],
                            window["resetsAt"]))
    return out


def agy():
    """The worker's quota, out of the verbose log of `agy models`.

    Listing models runs no model, so this one costs nothing. At `-v=3` the
    log carries each model group's limits: a run of `... Limit Remaining`
    entries, each with its fraction and reset, closed by the group's own
    `displayName`.
    """
    # `agy models` can exit before its quota request is answered, which
    # leaves the log without it; asking costs nothing, so ask again.
    for _ in range(3):
        folder = pathlib.Path(tempfile.mkdtemp())
        log = folder / "agy.log"
        subprocess.run([cli("agy"), "-v=3", "--log-file", str(log),
                        "models"],
                       capture_output=True, text=True, encoding="utf-8",
                       timeout=120, cwd=pathlib.Path.home())
        text = (log.read_text(encoding="utf-8", errors="replace")
                if log.exists() else "")
        shutil.rmtree(folder, ignore_errors=True)
        if "Limit Remaining" in text:
            break
    field = re.compile(r'"(displayName|resetTime|remainingFraction)": '
                       r'"?([^",\r\n]+)"?')
    # The quota is fetched more than once a run, so a later reading of a
    # group replaces the earlier one.
    groups, limits, current = {}, [], {}
    for name, value in field.findall(text):
        if name == "displayName" and value.endswith("Limit Remaining"):
            current = {"label": value}
            limits.append(current)
        elif name == "displayName" and limits:
            shown = []
            for limit in sorted(limits, key=lambda l: l["label"] != "Five "
                                "Hour Limit Remaining"):
                if "fraction" not in limit or "reset" not in limit:
                    continue
                label = ("5h limit:" if limit["label"].startswith("Five")
                         else "Weekly limit:")
                shown.append(line(label, 100 - limit["fraction"] * 100,
                                  limit["reset"], indent="    "))
            groups[value] = shown
            limits, current = [], {}
        elif name == "remainingFraction" and current:
            current["fraction"] = float(value)
        elif name == "resetTime" and current:
            current["reset"] = datetime.fromisoformat(
                value.replace("Z", "+00:00")).timestamp()
    out = []
    for group, shown in groups.items():
        out += [f"  {group}"] + shown
    return out or ["  no quota in the agy log"]


DATA = pathlib.Path(__file__).parent
LEDGER = DATA / "usage-ledger.csv"
# Each window's percent left and its reset, `YYYY-MM-DD HH:MM`; a blank is
# a window that gave no reading.
WINDOWS = [f"{plan}_{kind}" for plan in ("claude", "codex", "agy")
           for kind in ("5h", "week")]
FIELDS = ["time", "note"] + [f"{window}_{part}" for window in WINDOWS
                             for part in ("left", "reset")]


def main():
    """Print what is left; with a note, also append a row to the ledger.

        ai-usage "before: session mutation sweep"

    A reading just before a task and one just after it is what the task
    cost, whatever else was spending the same plans in between; `ai-pace`
    pairs them.
    """
    note = " ".join(sys.argv[1:])
    report = asked()
    print("\n".join(report))
    if note:
        record(note, report)


def record(note, report):
    """Append one row to the ledger: the report's readings under a note."""
    row = {"time": datetime.now().strftime("%Y-%m-%d %H:%M"), "note": note}
    for key, window in windows(report).items():
        row[f"{key}_left"] = window.left
        row[f"{key}_reset"] = datetime.fromtimestamp(
            window.resets).strftime("%Y-%m-%d %H:%M")
    fresh = not LEDGER.exists()
    with LEDGER.open("a", encoding="utf-8", newline="") as book:
        writer = csv.DictWriter(book, FIELDS, lineterminator=chr(10))
        if fresh:
            writer.writeheader()
        writer.writerow(row)


def asked():
    """The printed report: the three plans asked at once."""
    with ThreadPoolExecutor(3) as pool:
        futures = {"Claude": pool.submit(claude), "Codex": pool.submit(codex),
                   "agy": pool.submit(agy)}
        report = [datetime.now().strftime("Now %I:%M %p on %d %b")
                  .replace(" 0", " ")]
        for name, future in futures.items():
            report.append(name)
            try:
                report += future.result()
            except Exception as problem:
                report.append(f"  could not ask: {problem}")
    return report


def windows(report):
    """The report's readings, `claude_5h` and the like, to their Window."""
    return {f"{name.lower()}_{window.kind}": window
            for name, lines in _sections(report[1:]) for window in lines
            if isinstance(window, Window)}


def _sections(report):
    """(provider, its limit lines), skipping agy's non-Gemini group."""
    name, lines, skip = None, [], False
    for row in report:
        if not row.startswith(" "):
            if name:
                yield name, lines
            name, lines, skip = row, [], False
        elif "limit:" not in row:
            skip = "Gemini" not in row
        elif not skip:
            lines.append(row)
    if name:
        yield name, lines


if __name__ == "__main__":
    main()
