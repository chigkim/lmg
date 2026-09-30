"""How much of the Claude, Codex and agy plan windows is left.

    lmg usage                      # print it
    lmg usage "before: some task"  # print it and add a row to the ledger

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

import contextlib
import csv
import http.client
import io
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

PROMPT = "Reply with the word ok and nothing else."


def kill(child):
    """Kill CHILD and everything under it. On Windows the CLI is often an npm .cmd shim, so the real process is a
    grandchild that would keep the pipes open after the shim alone was killed."""
    if os.name == "nt":
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(child.pid)], capture_output=True)
    child.kill()


def ran(argv, timeout, cwd):
    """(exit status, stdout) of ARGV, its whole tree killed if it runs past TIMEOUT seconds."""
    child = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL,
                             text=True, encoding="utf-8", errors="replace", cwd=cwd)
    try:
        out, _ = child.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        kill(child)
        child.communicate()
        raise RuntimeError(f"{pathlib.Path(argv[0]).stem} gave no answer in {timeout}s")
    return child.returncode, out


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
    """A length of time in its two largest units: 40m, 2h42m, 3d4h."""
    minutes = max(round(seconds / 60), 0)
    days, hours, minutes = minutes // 1440, minutes // 60 % 24, minutes % 60
    parts = [f"{n}{unit}" for n, unit in ((days, "d"), (hours, "h"), (minutes, "m")) if n]
    return "".join(parts[:2]) or "0m"


WEEK = 7 * 24 * 3600
FIVE_HOURS = 5 * 3600


class Window(str):
    """A printed window line that keeps its figures for the ledger and the table."""


class Group(str):
    """The line naming one of a plan's model groups, as agy has two, above that group's windows."""


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
    window.left, window.resets, window.spare = left, resets, margin
    window.kind = "week" if label.startswith("Weekly") else "5h"
    return window


CREDENTIALS = pathlib.Path.home() / ".claude" / ".credentials.json"
USAGE = "https://api.anthropic.com/api/oauth/usage"


class Unredirected(urllib.request.HTTPRedirectHandler):
    """No redirect is followed: urllib would copy the Authorization header to wherever it pointed."""

    def redirect_request(self, *args):
        return None


# A redirect is then an HTTPError, an OSError, and answered as any failed request is.
OPENER = urllib.request.build_opener(Unredirected)


def claude():
    """Claude's limits from the endpoint `/usage` reads, which is free;
    the Haiku call is kept for when that request cannot be made or fails.

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
        with OPENER.open(request, timeout=30) as reply:
            windows = json.load(reply)
        del request
        names = {"five_hour": "5h limit:", "seven_day": "Weekly limit:"}
        return [line(label, windows[name]["utilization"],
                     unstarted(label, windows[name]["resets_at"]
                               and datetime.fromisoformat(
                                   windows[name]["resets_at"]).timestamp()))
                for name, label in names.items() if windows.get(name)]
    except (OSError, http.client.HTTPException, LookupError, ValueError,
            TypeError, AttributeError):
        # The token, the file, or the endpoint is unavailable, or its reply
        # is not the shape read above; asking with a call refreshes the
        # login as a side effect.
        return _claude_by_call()


def unstarted(label, resets):
    """RESETS, or, for a window that has not started and so has no reset
    (null, as after five idle hours), the one it would get if it started
    now: a whole length from now."""
    if resets:
        return resets
    return time.time() + (WEEK if label.startswith("Weekly") else FIVE_HOURS)


def _claude_by_call():
    status, out = ran(
        [cli("claude"), "-p", PROMPT, "--model", "claude-haiku-4-5-20251001",
         "--output-format", "stream-json", "--verbose", "--max-turns", "1"],
        180, pathlib.Path.home())
    windows = {}
    for raw in out.splitlines():
        if '"rate_limit_event"' not in raw:
            continue
        info = json.loads(raw)["rate_limit_info"]
        for name, window in info.get("unifiedWindows", {}).items():
            windows[name] = window
    if not windows:
        return ["  no rate_limit_event in the reply "
                f"(exit {status})"]
    names = {"five_hour": "5h limit:", "seven_day": "Weekly limit:"}
    # Only the windows it names: another's label, having no `limit:`, would be read as a group header and
    # take the windows after it out of the ledger.
    return [line(names[name], window["utilization"] * 100,
                 unstarted(names[name], window.get("resetsAt")))
            for name, window in windows.items() if name in names]


def codex():
    """Codex's limits from `codex app-server`, which reads them from the
    account without running a model, so asking costs nothing."""
    server = subprocess.Popen(
        [cli("codex"), "app-server"], stdin=subprocess.PIPE,
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
        encoding="utf-8", cwd=pathlib.Path.home())
    # A server that never answers would hold every reading for ever; killed,
    # its output closes and `answer` says it closed without answering.
    guard = threading.Timer(30, kill, [server])
    guard.start()
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
        guard.cancel()
        kill(server)
    if "error" in reply:
        return [f"  codex refused: {reply['error'].get('message')}"]
    limits = reply["result"]["rateLimits"]
    out = []
    for key, label in (("primary", "5h limit:"),
                       ("secondary", "Weekly limit:")):
        window = limits.get(key)
        if window:
            # resetsAt is null for a window not yet started, as Claude's is.
            out.append(line(label, window["usedPercent"],
                            unstarted(label, window["resetsAt"])))
    return out


def agy():
    """agy's quota, out of the verbose log of `agy models`.

    Listing models runs no model, so this one costs nothing. At `-v=3` the
    log carries each model group's limits: a run of `... Limit Remaining`
    entries, each with its fraction and reset, closed by the group's own
    `displayName`.
    """
    # `agy models` can exit before its quota request is answered, which
    # leaves the log without it; asking costs nothing, so ask again.
    # The verbose log is removed however the run ends, a timeout included,
    # since nothing promises a level-3 log holds no header worth keeping.
    for _ in range(3):
        folder = pathlib.Path(tempfile.mkdtemp())
        log = folder / "agy.log"
        try:
            ran([cli("agy"), "-v=3", "--log-file", str(log), "models"],
                120, pathlib.Path.home())
            text = (log.read_text(encoding="utf-8", errors="replace")
                    if log.exists() else "")
        finally:
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
            readings = []
            for limit in sorted(limits, key=lambda l: l["label"] != "Five "
                                "Hour Limit Remaining"):
                if "reset" not in limit:
                    continue
                # A fraction of nought may be left out of the log's JSON,
                # as protobuf leaves out a zero, so a limit with a reset and
                # no fraction is one used up.
                limit.setdefault("fraction", 0.0)
                label = ("5h limit:" if limit["label"].startswith("Five")
                         else "Weekly limit:")
                readings.append(line(label, 100 - limit["fraction"] * 100,
                                  limit["reset"], indent="    "))
            groups[value] = readings
            limits, current = [], {}
        elif name == "remainingFraction" and current:
            current["fraction"] = float(value)
        elif name == "resetTime" and current:
            current["reset"] = datetime.fromisoformat(
                value.replace("Z", "+00:00")).timestamp()
    out = []
    for group, readings in groups.items():
        out += [Group(f"  {group}")] + readings
    return out or ["  no quota in the agy log"]


DATA = pathlib.Path(__file__).parent
LEDGER = DATA / "usage-ledger.csv"
# A ledger row's time. To the second, so a reading just before a task and one just after it never share a time.
STAMP = "%Y-%m-%d %H:%M:%S"
# Each window's percent left and its reset, `YYYY-MM-DD HH:MM`; a blank is
# a window that gave no reading.
WINDOWS = [f"{plan}_{kind}" for plan in ("claude", "codex", "agy")
           for kind in ("5h", "week")]
FIELDS = ["time", "note"] + [f"{window}_{part}" for window in WINDOWS
                             for part in ("left", "reset")]


def main(argv=None):
    """Print what is left; with a note, also append a row to the ledger.

        lmg usage "before: pi mutation sweep"

    A reading just before a task and one just after it is what the task
    cost, whatever else was spending the same plans in between; `lmg usage pace`
    pairs them.

    A note is `word: what`, so a mistyped form -- `lmg usage calss`, or the old `lmg usage plan` -- is refused
    rather than written to the ledger as a note.
    """
    note = " ".join(sys.argv[1:] if argv is None else argv)
    if note in ("-h", "--help"):
        print(HELP)
        return 0
    if note and not NOTE.match(note):
        print(f"lmg usage: {note!r} is not a note or a form of lmg usage. A note is 'word: what',"
              " as 'before: pi x'; the forms are calls, pace and worth.", file=sys.stderr)
        return 2
    report = asked()
    print("\n".join(shown(report)))
    if note:
        record(note, report)


HELP = """\
lmg usage [NOTE]                   every plan's windows: % left, spare against even pace, when each resets
lmg usage calls [--hours N | --since 'YYYY-MM-DD HH:MM']
                                   every call each plan was spent on, and its public-API dollars
lmg usage pace [--tasks]           what can run now: how many more tasks each harness fits
lmg usage worth                    what each window is worth in public-API dollars

A NOTE is 'word: what'. It also adds the reading to the ledger: 'before: NAME' and 'after: NAME' around a task
are what lmg usage pace pairs, and NAME's first word is the harness that ran it. Each form takes --help."""


# A ledger note: a word, a colon, and what it is about.
NOTE = re.compile(r"\w+: \S")


def record(note, report):
    """Append one row to the ledger: the report's readings under a note."""
    row = {"time": datetime.now().strftime(STAMP), "note": note}
    for key, window in windows(report).items():
        row[f"{key}_left"] = window.left
        row[f"{key}_reset"] = datetime.fromtimestamp(
            window.resets).strftime("%Y-%m-%d %H:%M")
    append(LEDGER, FIELDS, row)


def append(path, fields, row):
    """One row onto the CSV at PATH, made with its header if it is empty, under a lock on the file.

    Without the lock two processes could each find the file empty and write a header, and on Windows, where an
    append seeks to the end and then writes, one could write its row over the other's (measured)."""
    text = io.StringIO()
    writer = csv.DictWriter(text, fields, lineterminator=chr(10))
    with path.open("ab") as book, locked(book):
        if book.seek(0, os.SEEK_END) == 0:
            writer.writeheader()
        writer.writerow(row)
        book.write(text.getvalue().encode("utf-8"))


@contextlib.contextmanager
def locked(handle):
    """Hold an exclusive lock on the open file HANDLE, waiting for another process to let go of it."""
    if os.name == "nt":
        import msvcrt
        # Byte 0 stands for the file; locking it past the end is allowed, so an empty file can be locked too.
        handle.seek(0)
        while True:
            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                break
            except OSError:
                time.sleep(0.01)
        try:
            yield
        finally:
            handle.flush()
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            handle.flush()
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


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


def shown(report):
    """The report as its time, one table of every window it read, and anything that could not be read under it.

    A window is named for its plan, its model group if the plan has more than one, and its length: `agy Gemini
    5h`. Spare is signed, + ahead of an even pace and - behind it."""
    rows, plan, group = [], "", ""
    for row in report[1:]:
        if not row.startswith(" "):
            plan, group = row, ""
        elif isinstance(row, Group):
            group = row.strip().removesuffix(" Models").removesuffix(" models")
        elif isinstance(row, Window):
            rows.append([" ".join(filter(None, (plan, group, row.kind))), f"{row.left}%", f"{row.spare:+}%",
                         f"in {span(row.resets - time.time())}, at {when(row.resets)}"])
    said = unread(report)
    table_lines = table(["window", "left", "spare", "resets"], rows, text=(0, 3)) if rows else []
    return [report[0], ""] + table_lines + ([""] + said if said else [])


def unread(report):
    """What the report says instead of a window, each named for its plan: `Codex: could not ask: ...`."""
    said, plan = [], ""
    for row in report[1:]:
        if not row.startswith(" "):
            plan = row
        elif not isinstance(row, (Group, Window)):
            said.append(f"{plan}: {row.strip()}")
    return said


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

