"""Hand one job to agy, Claude Code or Codex, in a folder of your own, with a usage reading either side.

    lmg ask agy|claude|codex read|edit FOLDER [ID]   # FOLDER/task.md as a job, or a follow-up to the one ID names
    lmg ask review SHA FOLDER                        # codex exec review of one commit, its answer in FOLDER

    --model NAME   the harness's model, instead of DEFAULTS
    --timeout T    agy's print timeout, 3600s unless given
    --force        an edit job in a repository's main checkout, which is refused without it

A mode is what the agent may do. `read` is read and search files, nothing else. `edit` bypasses every permission
prompt and sandbox, so it may write, edit and run anything the machine lets it:

    agy     read: no bypass flag, so headless agy denies every write and command; a denial ends the run unanswered
    claude  read: only Read, Grep and Glob, and no MCP servers
    codex   read: the read-only sandbox, where it still runs commands to read with

The job is FOLDER/task.md, or TASK.md. The harness runs at the top of the git repository FOLDER is in, or in FOLDER
itself if it is in none, so a folder inside a worktree you made gets that worktree to work in. No worktree is made
here: an edit job changes the files where it runs, so one in a repository's main checkout is refused unless --force.

Run N copies task.md to FOLDER/taskN.md and tells the agent to read that copy, so task.md can be edited for the next
run while this one goes. The harness's final message is the answer, FOLDER/responseN.md, written here rather than by
the agent, which in read mode cannot write; its raw output is responseN.jsonl, and codex's log responseN.log. The raw
output is streamed, so a run stopped part way still holds the id to follow up with, which is printed. A follow-up is
the same FOLDER, task.md edited, plus that id, and may change mode. A review numbers its files in FOLDER the same way.

A reading of each plan's windows goes to `usage-ledger.csv` as `before: HARNESS NAME [pid N]` and `after: ...`, NAME
being FOLDER's name, so the harness is what `lmg usage` costs it under; a review's note is `codex review SHA`. One row
a job goes to `token-ledger.csv`: the repository, the uncached input, cache reads, cache writes and output, which add
up to what the model read and wrote, and what they would cost on the public API at the rates in `prices.csv`. The
two ledgers are in ~/.lmg; `prices.csv` sits beside this file.
"""
import argparse
import json
import os
import pathlib
import re
import subprocess
import sys
import time
from datetime import datetime

from . import calls, usage
from .home import HOME

HARNESSES = ("agy", "claude", "codex")
DEFAULTS = {"agy": "gemini-3.8-flash-medium", "claude": "claude-opus-5-5", "codex": "gpt-6.1-sol"}
TOKENS = HOME / "token-ledger.csv"
TOKEN_FIELDS = ["time", "note", "repo", "model", "input", "cache_read", "cache_write", "cache_write_1h", "output", "usd"]

# Codex reads by running commands, which its sandbox allows, so it is told only not to write; the other two read with
# tools, and a command refused ends an agy run with no answer.
READ_ONLY = {
    "codex": " The sandbox is read-only: read and search as you like, but do not try to write, edit or create a file.",
    "claude": " You may read and search files, search and fetch the web, and use your MCP tools: do not write, edit"
              " or create a file, and do not run any command.",
    "agy": " You may only read and search files: do not write, edit or create one, and do not run any command,"
           " which is refused and ends the job.",
}


class Problem(Exception):
    """A job that cannot be started: said in one line, exit 2."""


ASK = "Read {task} and do what it says."
# A follow-up told only to read its task again answered the old one from memory (measured); the copy it is pointed
# at is a file it has not read.
AGAIN = "{task} holds a new task, replacing the one before: read it and do what it says now."
# The answer is the final message, which in read mode the agent could not write to a file anyway.
SAVED = " Your final message is saved as {answer} for you, so put your full answer in it."


def ask_text(harness, mode, task, answer, resume=None):
    text = (AGAIN if resume else ASK).format(task=task) + SAVED.format(answer=pathlib.Path(answer).name)
    if mode == "edit":
        return text
    return text + READ_ONLY[harness]


READ_TOOLS = "Read,Grep,Glob,WebSearch,WebFetch,ToolSearch"


def flags(harness, mode):
    """The harness's permission flags for a mode, for a fresh run."""
    if harness == "codex":
        return ["-s", "read-only"] if mode == "read" else ["--dangerously-bypass-approvals-and-sandbox"]
    if harness == "claude":
        # --tools leaves only these built-ins, so nothing can write a file or run a command; MCP servers are not
        # built-ins and stay, and the bypass lets them and the web run unprompted. ToolSearch finds deferred MCP tools.
        return (["--tools", READ_TOOLS, "--permission-mode", "bypassPermissions"] if mode == "read"
                else ["--dangerously-skip-permissions"])
    return [] if mode == "read" else ["--dangerously-skip-permissions"]


def command(harness, mode, model, cwd, task, answer, resume=None, timeout="3600s"):
    """The argv that runs one job in CWD. A codex resume keeps neither sandbox nor folder, so the sandbox is set again."""
    ask = ask_text(harness, mode, task, answer, resume)
    if harness == "codex":
        if resume:
            again = ["-c", 'sandbox_mode="read-only"'] if mode == "read" else flags(harness, mode)
            return ["codex", "exec", "resume", resume, "--json", "-m", model, *again, "-o", str(answer), ask]
        return ["codex", "exec", "--json", "-m", model, *flags(harness, mode), "-C", str(cwd), "-o", str(answer), ask]
    # The prompt goes straight after -p: --tools takes several values and would swallow a prompt that followed it.
    if harness == "claude":
        return ["claude", "-p", ask, "--output-format", "stream-json", "--verbose", "--model", model,
                *flags(harness, mode), *(["--resume", resume] if resume else [])]
    return ["agy", *flags(harness, mode), "--model", model, "--print-timeout", timeout, "--add-dir", str(cwd),
            "--output-format", "stream-json", *(["--conversation", resume] if resume else []), "-p", ask]


def json_in(text):
    """The first JSON object in TEXT, which may follow lines of other output, as agy's does; {} if none."""
    start = text.find("{")
    while start >= 0:
        try:
            value = json.JSONDecoder().raw_decode(text[start:])[0]
            return value if isinstance(value, dict) else {}
        except ValueError:
            start = text.find("{", start + 1)
    return {}


def counts(input=0, cache_read=0, cache_write=0, cache_write_1h=0, output=0):
    return {"input": input, "cache_read": cache_read, "cache_write": cache_write, "cache_write_1h": cache_write_1h,
            "output": output}


def codex_counts(usage_):
    """A codex usage object -> counts. Its input includes the cached and cache-written part, which come out of it."""
    cached, wrote = usage_.get("cached_input_tokens", 0), usage_.get("cache_write_input_tokens", 0)
    return counts(usage_.get("input_tokens", 0) - cached - wrote, cached, wrote, 0, usage_.get("output_tokens", 0))


def codex_usage(events):
    """codex --json events -> (thread id, the last turn.completed usage object, or None)."""
    thread, usage_ = "", None
    for line in events.splitlines():
        event = json_in(line)
        if event.get("type") == "thread.started" and not thread:
            thread = event.get("thread_id", "")
        elif event.get("type") == "turn.completed" and isinstance(event.get("usage"), dict):
            usage_ = event["usage"]
    return thread, usage_


def read_codex(events, before=""):
    """codex --json events -> (thread id, counts). Its usage is a running total over the session (measured: a
    follow-up's input held the first run's), so a follow-up's is its own less BEFORE, the previous run's events."""
    thread, now = codex_usage(events)
    if now is None:
        return thread, None
    was = codex_usage(before)[1] or {}
    return thread, codex_counts({key: now.get(key, 0) - was.get(key, 0) for key in now})


def events(text):
    """The JSON objects of a stream, one a line; a line that is not one is passed over."""
    for line in text.splitlines():
        if line.lstrip().startswith("{"):
            event = json_in(line)
            if event:
                yield event


def final(harness, text):
    """The result a claude or agy stream ends with, or {} for a run that stopped before it."""
    for event in events(text):
        if harness == "claude" and event.get("type") == "result":
            return event
        if harness == "agy" and event.get("event") == "result" and isinstance(event.get("result"), dict):
            return event["result"]
    return {}


def session(harness, text):
    """The id to follow up with, from the first event that carries one, so a run stopped part way still has it."""
    key = {"agy": "conversation_id", "claude": "session_id", "codex": "thread_id"}[harness]
    for event in events(text):
        nested = event.get("result")
        found = event.get(key) or (nested.get(key) if isinstance(nested, dict) else "")
        if found:
            return found
    return ""


def read_claude(text):
    """A claude stream -> (answer, session id, counts, trouble). Its usage is the run's total; cache writes are kept
    an hour. TROUBLE is why the run did not finish, or ''."""
    out = final("claude", text)
    spent = out.get("usage") or {}
    hour = (spent.get("cache_creation") or {}).get("ephemeral_1h_input_tokens", 0)
    trouble = "" if out else "ended early: no result, so no answer; follow up with the id"
    if out.get("is_error"):
        trouble = f"failed: {out.get('subtype', 'error')}"
    # A run stopped before its result reports no usage, which is not a run that used none.
    return out.get("result") or "", session("claude", text), counts(
        spent.get("input_tokens", 0), spent.get("cache_read_input_tokens", 0),
        spent.get("cache_creation_input_tokens", 0), hour, spent.get("output_tokens", 0)) if out else None, trouble


def read_agy(text, before=""):
    """An agy stream -> (answer, conversation id, counts, trouble). Its usage is a running total over the
    conversation, so a follow-up's is its own less BEFORE, the previous run's stream. Its output includes thinking; it
    reports no cache writes. TROUBLE is why the run did not finish, or ''."""
    out, was = final("agy", text), (final("agy", before).get("usage") or {})
    now = out.get("usage") or {}

    def spent(key):
        return now.get(key, 0) - was.get(key, 0)
    trouble = ""
    # agy says SUCCESS and exits 0 when --print-timeout cuts a turn off.
    if any("print timeout after" in line for line in text.splitlines() if not line.lstrip().startswith("{")):
        trouble = "timed out: the answer and the work are partial; follow up with the id"
    elif out.get("denied_actions"):
        denied = ", ".join(d.get("display_name") or d.get("action", "?") for d in out["denied_actions"])
        trouble = f"denied {denied}: read mode refused it and the run ended there"
    elif not out:
        trouble = "ended early: no result, so no answer; follow up with the id"
    # A run stopped before its result has no total, and less the last one it would book a negative count.
    return out.get("response") or "", session("agy", text), counts(
        spent("input_tokens"), spent("cache_read_tokens"), 0, 0, spent("output_tokens")) if now else None, trouble


def cost(model, spent, table):
    """What SPENT would cost on the public API, or None for a model with no price. The long-context rate is per call
    and cannot be told from a job's totals, so it is not applied."""
    rate = table.get(calls.base(model))
    if rate is None:
        return None
    return (spent["input"] * rate["input"] + spent["cache_read"] * rate["cache_read"]
            + (spent["cache_write"] - spent["cache_write_1h"]) * rate["cache_write"]
            + spent["cache_write_1h"] * rate["cache_write_1h"] + spent["output"] * rate["output"]) / 1e6


def book(note, repo, model, spent, path=None):
    """One row of the token ledger, TOKENS unless PATH; returns the line that says it."""
    path = path or TOKENS
    usd = cost(model, spent, calls.prices())
    usage.append(path, TOKEN_FIELDS, {"time": datetime.now().strftime(usage.STAMP), "note": note, "repo": repo,
                                      "model": model, **spent, "usd": "" if usd is None else f"{usd:.2f}"})
    price = f"${usd:.2f}" if usd is not None else f"unknown ({model} has no price in {calls.PRICES})"
    return (f"tokens: input {spent['input']}, cache read {spent['cache_read']}, cache write {spent['cache_write']},"
            f" output {spent['output']}; API equivalent {price}")


def booked(note, repo, model, spent):
    """`book`, or the line that says it could not be: a token ledger open elsewhere, or a price that is not a number,
    loses the booking, not the job's result printed after it."""
    try:
        return book(note, repo, model, spent)
    except (OSError, ValueError) as problem:
        return f"tokens not booked: {problem}"


def git(*args, cwd):
    # Git for Windows prints paths in UTF-8, which the ANSI code page would mangle.
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, encoding="utf-8",
                          errors="replace")


def toplevel(folder):
    found = git("rev-parse", "--show-toplevel", cwd=folder)
    if found.returncode:
        raise Problem(f"{folder} is not in a git repository")
    return pathlib.Path(found.stdout.strip()).resolve()


def workplace(folder):
    """Where a job in FOLDER runs: the top of the git repository FOLDER is in, else FOLDER itself."""
    found = git("rev-parse", "--show-toplevel", cwd=folder)
    return pathlib.Path(found.stdout.strip()).resolve() if found.returncode == 0 else folder


def main_checkout(folder):
    """Whether FOLDER is a repository's main working tree, not a worktree of it: its git folder is the shared one."""
    found = git("rev-parse", "--path-format=absolute", "--git-dir", "--git-common-dir", cwd=folder)
    if found.returncode:
        return False
    own, shared = (pathlib.Path(line).resolve() for line in found.stdout.splitlines()[:2])
    return own == shared


def task_of(folder):
    """FOLDER's task.md, by whatever case its name is in."""
    for path in sorted(folder.iterdir()):
        if path.name.lower() == "task.md" and path.is_file():
            return path
    raise Problem(f"no task.md in {folder}")


def claimed(folder):
    """The first FOLDER/responseN.jsonl free, made empty in one exclusive create so a run started beside it cannot
    take the same N; the N. Every run writes its raw output there, a review's and one stopped part way included."""
    n = 1
    while True:
        try:
            (folder / f"response{n}.jsonl").open("xb").close()
            return n
        except FileExistsError:
            n += 1


def paired():
    """What makes a job's two readings a pair: this process's pid, which no job running beside it shares, so two
    jobs of one name at once, in two repositories, are not paired with each other's readings."""
    return f" [pid {os.getpid()}]"


def reading(stage, note):
    """An lmg usage reading into its ledger, which lmg usage tasks pairs; a plan that cannot be asked is said and skipped."""
    report = usage.asked()
    try:
        usage.record(f"{stage}: {note}", report)
    except OSError as problem:
        raise Problem(f"could not write the {stage} reading to {usage.LEDGER}: {problem}")
    return report


def bound():
    """Make every process this one starts die with it, however it dies, so a killed job leaves no harness running.

    On Windows this process joins a job object that kills all its members when its last handle closes, which is
    when this process exits; whatever the harness starts inherits the job. Elsewhere `run` gives the harness its
    own process group and kills the group on the way out, and SIGTERM and SIGHUP are made to take that way out.
    """
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        class Basic(ctypes.Structure):
            _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                        ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                        ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                        ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD),
                        ("SchedulingClass", wintypes.DWORD)]

        class Extended(ctypes.Structure):
            _fields_ = [("Basic", Basic), ("Io", ctypes.c_uint64 * 6), ("ProcessMemoryLimit", ctypes.c_size_t),
                        ("JobMemoryLimit", ctypes.c_size_t), ("PeakProcessMemoryUsed", ctypes.c_size_t),
                        ("PeakJobMemoryUsed", ctypes.c_size_t)]
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateJobObjectW.restype = wintypes.HANDLE
        kernel32.GetCurrentProcess.restype = wintypes.HANDLE
        job = kernel32.CreateJobObjectW(None, None)
        limits = Extended()
        limits.Basic.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        ok = job and kernel32.SetInformationJobObject(wintypes.HANDLE(job), 9, ctypes.byref(limits),
                                                      ctypes.sizeof(limits))  # 9: JobObjectExtendedLimitInformation
        if not (ok and kernel32.AssignProcessToJobObject(wintypes.HANDLE(job),
                                                         wintypes.HANDLE(kernel32.GetCurrentProcess()))):
            print(f"warning: no job object (error {ctypes.get_last_error()}), so killing lmg may leave the"
                  " harness running", file=sys.stderr)
        return job  # the handle is never closed: the job ends with this process
    import signal
    for sig in (signal.SIGTERM, signal.SIGHUP):
        signal.signal(sig, lambda number, frame: sys.exit(128 + number))
    return None


def run(argv, cwd, out, err=None):
    """Run a harness with its output to OUT (and ERR, or OUT), returning its exit status."""
    argv = [usage.cli(argv[0]), *argv[1:]]
    with open(out, "w", encoding="utf-8") as stdout:
        errs = open(err, "w", encoding="utf-8") if err else None
        child = subprocess.Popen(argv, cwd=cwd, stdout=stdout, stderr=errs or subprocess.STDOUT,
                                 stdin=subprocess.DEVNULL, start_new_session=os.name != "nt")
        try:
            return child.wait()
        except KeyboardInterrupt:
            # Stopped by hand: what it streamed is read as a run stopped part way, and the job still says its id.
            # The whole tree, since on Windows the harness is a .cmd shim over the process doing the work.
            usage.kill(child)
            child.wait()
            return 130
        finally:
            # Whether or not the harness is still running: what it left in the background is in its group too.
            if os.name != "nt":
                import signal
                try:
                    os.killpg(child.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            if errs:
                errs.close()


def installed(harness):
    """Refuse, before anything is made or booked, a harness that is not on PATH."""
    try:
        usage.cli(harness)
    except RuntimeError as missing:
        raise Problem(str(missing))


def totalled(harness, text):
    """Whether a codex or agy stream ends with its session's running total, which a run stopped part way has not."""
    return (codex_usage(text)[1] if harness == "codex" else final(harness, text).get("usage")) is not None


def last_of(harness, folder, n, resume):
    """The stream of the latest run before N of the session RESUME that has a usage total, or '' if none is kept.
    A run stopped before its total is passed over, so its tokens are booked by the next run that has one."""
    for earlier in range(n - 1, 0, -1):
        path = folder / f"response{earlier}.jsonl"
        text = path.read_text(encoding="utf-8", errors="replace") if path.exists() else ""
        if text and session(harness, text) == resume and totalled(harness, text):
            return text
    return ""


def folder_of(folder):
    folder = pathlib.Path(folder).resolve()
    if not folder.is_dir():
        raise Problem(f"no folder {folder}")
    return folder


def job(harness, mode, folder, resume, model, timeout, force=False):
    folder = folder_of(folder)
    task = task_of(folder)
    installed(harness)
    cwd = workplace(folder)
    if mode == "edit" and not force and main_checkout(cwd):
        raise Problem(f"{cwd} is a repository's main checkout, whose files an edit job would change: make a worktree"
                      " and put the folder in it, or give --force")
    note = f"{harness} {folder.name}" + (" (resumed)" if resume else "") + paired()
    # After the checks, which may refuse the job, and before anything is written, so a reading that fails (a ledger
    # open in Excel) leaves no run half made.
    reading("before", note)
    n = claimed(folder)
    copy = folder / f"task{n}.md"
    copy.write_bytes(task.read_bytes())
    answer, raw, log = (folder / f"response{n}.{kind}" for kind in ("md", "jsonl", "log"))
    print(f"{harness} {mode}, {model}, in {cwd}", flush=True)
    argv = command(harness, mode, model, cwd, copy, answer, resume, timeout)
    lines = []
    if harness == "codex":
        status = run(argv, cwd, raw, log)
        sid, spent = read_codex(raw.read_text(encoding="utf-8", errors="replace"),
                                last_of(harness, folder, n, resume) if resume else "")
        lines.append(f"session id: {sid}")
    else:
        status = run(argv, cwd, raw)
        text = raw.read_text(encoding="utf-8", errors="replace")
        if harness == "claude":
            said, sid, spent, trouble = read_claude(text)
            lines.append(f"session id: {sid}")
        else:
            said, sid, spent, trouble = read_agy(text, last_of(harness, folder, n, resume) if resume else "")
            lines.append(f"conversation id: {sid}")
        if trouble:
            lines.append(f"{harness} {trouble}")
            status = status or (124 if trouble.startswith("timed") else 1)
        answer.write_text(said, encoding="utf-8")
    if spent:
        lines.append(booked(note, cwd.name, model, spent))
    lines.append(f"answer: {answer}")
    if sid:
        lines.append(f"follow up: edit {task.name}, then lmg ask {harness} read|edit {folder} {sid}")
    print("\n".join(lines), flush=True)
    return status, note


def review(sha, folder, model):
    folder = folder_of(folder)
    repo = toplevel(folder)
    found = git("rev-parse", "-q", "--verify", f"{sha}^{{commit}}", cwd=repo)
    if found.returncode:
        raise Problem(f"no commit {sha} in {repo}")
    installed("codex")
    # Named after the commit's own hash in the note, so `v1.2` or `origin/main` reads the same as its hash.
    sha = found.stdout.strip()[:12]
    note = f"codex review {sha}" + paired()
    reading("before", note)
    # By the raw output, which every run writes; codex writes the answer only when it finishes.
    n = claimed(folder)
    answer, raw, log = (folder / f"response{n}.{kind}" for kind in ("md", "jsonl", "log"))
    started = time.time()
    status = run(["codex", "exec", "-m", model, "-s", "read-only", "-o", str(answer), "review",
                  "--json", "--commit", sha], repo, raw, log)
    thread, _ = read_codex(raw.read_text(encoding="utf-8", errors="replace"))
    print(f"session id: {thread}")
    # A review's own events count no tokens (measured): the work is a child session, whose rollout names it as parent.
    spent = child_usage(thread, started)
    if spent:
        print(booked(note, repo.name, model, spent))
    print(f"answer: {answer}")
    return status, note


def child_usage(thread, since, sessions=None):
    """The counts of the codex session whose parent is THREAD, from its rollout written since SINCE; None if none."""
    sessions = sessions or calls.codex_home() / "sessions"
    if not thread or not sessions.exists():
        return None
    for rollout in sessions.rglob("rollout-*.jsonl"):
        if rollout.stat().st_mtime < since:
            continue
        text = rollout.read_text(encoding="utf-8", errors="replace")
        if f'"parent_thread_id":"{thread}"' not in text:
            continue
        total = None
        for line in text.splitlines():
            found = re.search(r'"total_token_usage":(\{[^}]*\})', line)
            if found:
                total = json.loads(found.group(1))
        return codex_counts(total) if total else None
    return None


def parse(argv):
    parser = argparse.ArgumentParser(prog="lmg ask", description=__doc__.split("\n\n")[0],
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("words", nargs="+", help="agy|claude|codex read|edit FOLDER [ID], or review SHA FOLDER")
    parser.add_argument("--model", help="the harness's model instead of its default")
    parser.add_argument("--timeout", default="3600s", help="agy's print timeout (default 3600s)")
    parser.add_argument("--force", action="store_true", help="run an edit job in a repository's main checkout")
    return parser.parse_args(argv)


def main(argv, command_line=False):
    # Only the real command binds its children to it; a test calling main is not to join a job object.
    args = parse(argv)
    words = args.words
    if command_line:
        bound()
    try:
        what = words[0]
        if what in HARNESSES:
            if len(words) not in (3, 4) or words[1] not in ("read", "edit"):
                raise Problem(f"lmg ask {what} read|edit FOLDER [ID]")
            status, note = job(what, words[1], words[2], words[3] if len(words) == 4 else None,
                               args.model or DEFAULTS[what], args.timeout, args.force)
        elif what == "review":
            if len(words) != 3:
                raise Problem("lmg ask review SHA FOLDER")
            status, note = review(words[1], words[2], args.model or DEFAULTS["codex"])
        else:
            raise Problem(f"unknown {what}: agy, claude, codex or review")
    except Problem as problem:
        print(problem, file=sys.stderr)
        return 2
    print(f"exit {status}")
    try:
        after = reading("after", note)
        print("\n".join(usage.shown(after, usage.costed(after))))
    except (Exception, SystemExit) as problem:
        # The job has run; a reading that fails loses the accounting, not the answer.
        print(f"the after reading failed, so this job is not in the ledger's costs: {problem}", file=sys.stderr)
    return status
