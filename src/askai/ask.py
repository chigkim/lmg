"""Hand one job to agy, Claude Code or Codex, in its own git worktree, with a usage reading either side.

    askai agy|claude|codex read|edit PROMPT [ID]   # a job, or a follow-up to the one ID names
    askai review SHA                               # codex exec review of one commit
    askai clean agy|claude|codex NAME [--force]    # remove a job's worktree and branch, if nothing is lost
    askai list                                     # every open job: its runs, answer, changes and id
    askai usage|plan|pace ...                      # read the plans: usage.py, plan.py and pace.py, which see

    --repo PATH    the repository, instead of the one the current folder is in
    --home PATH    where worktrees/ and runs/ go, instead of the repository's home
    --model NAME   the harness's model, instead of DEFAULTS
    --timeout T    agy's print timeout, 3600s unless given

A mode is what the agent may do. `read` is read and search files, nothing else. `edit` bypasses every permission
prompt and sandbox, so it may write, edit and run anything the machine lets it:

    agy     read: no bypass flag, so headless agy denies every write and command; a denial ends the run unanswered
    claude  read: only Read, Grep and Glob, and no MCP servers
    codex   read: the read-only sandbox, where it still runs commands to read with

A repository's home is PARENT/.askai/REPO, beside it, unless `homes.toml` beside this file names another:

    [homes]
    "D:/code/game" = "D:/code"      # a relative home is taken from the repository

The job is named after the prompt file, less `.md` and a trailing `-prompt`. Its worktree is
HOME/worktrees/HARNESS-NAME, on a branch of that name at HEAD, made on first use and reused by a follow-up. The
prompt is moved in as TASK.md, with a numbered copy kept as HOME/runs/HARNESS/NAME-N.task.md, and the agent is
told only to read TASK.md there. The harness's final message is the answer, HOME/runs/HARNESS/NAME-N.md, beside
its raw output; the id to follow up with is printed. The raw output is streamed, so a run stopped part way still
holds the id, and `list` finds it there. A follow-up is a new prompt file of the same name, plus that
id, and may change mode.

A reading of each plan's windows goes to `usage-ledger.csv` as `before: HARNESS NAME` and `after: ...`, so the
harness is the kind `askai pace` costs. One row a job goes to `token-ledger.csv`: the repository, the uncached input,
cache reads, cache writes and output, which add up to what the model read and wrote, and what they would cost on
the public API at the rates in `prices.csv`. All three sit beside this file.
"""
import argparse
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime

from askai import pace, plan, usage

# The three commands that only read the plans, each its own module's main, handed the rest of the command line.
READERS = {"usage": usage.main, "plan": plan.main, "pace": pace.main}

HARNESSES = ("agy", "claude", "codex")
DEFAULTS = {"agy": "gemini-3.8-flash-medium", "claude": "claude-opus-5-5", "codex": "gpt-6-sol"}
TOKENS = pathlib.Path(__file__).parent / "token-ledger.csv"
HOMES = pathlib.Path(__file__).parent / "homes.toml"
TOKEN_FIELDS = ["time", "note", "repo", "model", "input", "cache_read", "cache_write", "cache_write_1h", "output", "usd"]

ASK = "Read TASK.md in this folder and do what it says."
# Codex reads by running commands, which its sandbox allows, so it is told only not to write; the other two read with
# tools, and a command refused ends an agy run with no answer.
READ_ONLY = {
    "codex": " The sandbox is read-only: read and search as you like, but do not try to write, edit or create a file.",
    "other": " You may only read and search files: do not write, edit or create one, and do not run any command,"
             " which is refused and ends the job.",
}


class Problem(Exception):
    """A job that cannot be started: said in one line, exit 2."""


def job_name(prompt):
    name = pathlib.Path(prompt).name
    name = name[:-3] if name.endswith(".md") else name
    return name[:-len("-prompt")] if name.endswith("-prompt") else name


# A follow-up that said ASK again was answered from the old TASK.md the session had already read (measured).
AGAIN = "TASK.md now holds a new task, replacing the one before: read it again and do what it says now."


def ask_text(harness, mode, resume=None):
    text = AGAIN if resume else ASK
    if mode == "edit":
        return text
    return text + READ_ONLY["codex" if harness == "codex" else "other"]


def flags(harness, mode):
    """The harness's permission flags for a mode, for a fresh run."""
    if harness == "codex":
        return ["-s", "read-only"] if mode == "read" else ["--dangerously-bypass-approvals-and-sandbox"]
    if harness == "claude":
        # --tools leaves only the three; dontAsk denies whatever is not allowed, so they are allowed by name.
        return (["--tools", "Read,Grep,Glob", "--allowedTools", "Read,Grep,Glob", "--permission-mode", "dontAsk",
                 "--strict-mcp-config"] if mode == "read" else ["--dangerously-skip-permissions"])
    return [] if mode == "read" else ["--dangerously-skip-permissions"]


def command(harness, mode, model, tree, answer, resume=None, timeout="3600s"):
    """The argv that runs one job in TREE. A codex resume keeps neither sandbox nor folder, so the sandbox is set again."""
    ask = ask_text(harness, mode, resume)
    if harness == "codex":
        if resume:
            again = ["-c", 'sandbox_mode="read-only"'] if mode == "read" else flags(harness, mode)
            return ["codex", "exec", "resume", resume, "--json", "-m", model, *again, "-o", str(answer), ask]
        return ["codex", "exec", "--json", "-m", model, *flags(harness, mode), "-C", str(tree), "-o", str(answer), ask]
    # The prompt goes straight after -p: --tools takes several values and would swallow a prompt that followed it.
    if harness == "claude":
        return ["claude", "-p", ask, "--output-format", "stream-json", "--verbose", "--model", model,
                *flags(harness, mode), *(["--resume", resume] if resume else [])]
    return ["agy", *flags(harness, mode), "--model", model, "--print-timeout", timeout, "--add-dir", str(tree),
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
        trouble = "timed out: the answer and the worktree are partial; follow up with the id"
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
    rate = table.get(plan.base(model))
    if rate is None:
        return None
    return (spent["input"] * rate["input"] + spent["cache_read"] * rate["cache_read"]
            + (spent["cache_write"] - spent["cache_write_1h"]) * rate["cache_write"]
            + spent["cache_write_1h"] * rate["cache_write_1h"] + spent["output"] * rate["output"]) / 1e6


def book(note, repo, model, spent, path=None):
    """One row of the token ledger, TOKENS unless PATH; returns the line that says it."""
    path = path or TOKENS
    usd = cost(model, spent, plan.prices())
    usage.append(path, TOKEN_FIELDS, {"time": datetime.now().strftime(usage.STAMP), "note": note, "repo": repo,
                                      "model": model, **spent, "usd": "" if usd is None else f"{usd:.2f}"})
    price = f"${usd:.2f}" if usd is not None else f"unknown ({model} has no price in {plan.PRICES})"
    return (f"tokens: input {spent['input']}, cache read {spent['cache_read']}, cache write {spent['cache_write']},"
            f" output {spent['output']}; API equivalent {price}")


def git(*args, cwd):
    # Git for Windows prints paths in UTF-8, which the ANSI code page would mangle.
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, encoding="utf-8",
                          errors="replace")


def toplevel(folder):
    found = git("rev-parse", "--show-toplevel", cwd=folder)
    if found.returncode:
        raise Problem(f"{folder} is not in a git repository")
    return pathlib.Path(found.stdout.strip()).resolve()


def common(folder):
    """The shared .git folder of the repository FOLDER is a worktree of."""
    found = git("rev-parse", "--path-format=absolute", "--git-common-dir", cwd=folder)
    return pathlib.Path(found.stdout.strip()).resolve() if found.returncode == 0 else None


def worktree(repo, tree, resume):
    """Whether TREE is still to be made, having checked it: a follow-up needs the worktree there already, and a
    fresh job is refused one left by an earlier job, which would hand it that job's changes as its own."""
    if tree.exists():
        if common(tree) != common(repo):
            raise Problem(f"{tree} is there but is not a worktree of {repo}")
        if not resume:
            raise Problem(f"{tree} is left from an earlier job: follow it up with its id (`askai list`), or clean it")
        return False
    if resume:
        raise Problem(f"no worktree {tree} to follow up in")
    return True


def make(repo, tree):
    """Make TREE a worktree of REPO at HEAD, on a branch of its name."""
    made = git("worktree", "add", "-q", str(tree), "-b", tree.name, "HEAD", cwd=repo)
    if made.returncode:
        raise Problem(made.stderr.strip() or f"could not make {tree}")


def task_copy(runs, name, n):
    """Where run N of job NAME keeps its prompt. NAME-N.task.md, not NAME-task-N.md, which a job named NAME-task
    would write its answer over: an answer ends in its run number and this in .task, so no name makes them meet."""
    return runs / f"{name}-{n}.task.md"


def numbered(folder, name):
    n = 1
    while task_copy(folder, name, n).exists():
        n += 1
    return n


def reading(stage, note):
    """An askai usage reading into its ledger, which askai pace pairs; a plan that cannot be asked is said and skipped."""
    report = usage.asked()
    usage.record(f"{stage}: {note}", report)
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
            print(f"warning: no job object (error {ctypes.get_last_error()}), so killing askai may leave the"
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


def last_of(harness, runs, name, n, resume):
    """The stream of the latest run before N of the session RESUME that has a usage total, or '' if none is kept.
    A run stopped before its total is passed over, so its tokens are booked by the next run that has one."""
    for earlier in range(n - 1, 0, -1):
        path = runs / f"{name}-{earlier}.jsonl"
        text = path.read_text(encoding="utf-8", errors="replace") if path.exists() else ""
        if text and session(harness, text) == resume and totalled(harness, text):
            return text
    return ""


def job(harness, mode, prompt, resume, repo, home, model, timeout):
    prompt = pathlib.Path(prompt).resolve()
    if not prompt.is_file():
        raise Problem(f"no prompt file {prompt}")
    installed(harness)
    name = job_name(prompt)
    tree = home / "worktrees" / f"{harness}-{name}"
    runs = home / "runs" / harness
    note = f"{harness} {name}" + (" (resumed)" if resume else "")
    fresh = worktree(repo, tree, resume)
    # After the checks, which may refuse the job, and before anything is made or moved, so a reading that fails
    # (a ledger open in Excel) leaves no job half made.
    reading("before", note)
    if fresh:
        make(repo, tree)
    runs.mkdir(parents=True, exist_ok=True)
    n = numbered(runs, name)
    task_copy(runs, name, n).write_bytes(prompt.read_bytes())
    shutil.move(prompt, tree / "TASK.md")  # os.replace cannot cross drives
    # Each path is named whole: with_suffix would take a dot in the job's name for the start of a suffix.
    answer, raw, log = (runs / f"{name}-{n}.{kind}" for kind in ("md", "jsonl", "log"))
    print(f"{harness} {mode}, {model}, in {tree}", flush=True)
    argv = command(harness, mode, model, tree, answer, resume, timeout)
    lines = []
    if harness == "codex":
        status = run(argv, tree, raw, log)
        thread, spent = read_codex(raw.read_text(encoding="utf-8", errors="replace"),
                                   last_of(harness, runs, name, n, resume) if resume else "")
        lines.append(f"session id: {thread}")
    else:
        status = run(argv, tree, raw)
        text = raw.read_text(encoding="utf-8", errors="replace")
        if harness == "claude":
            said, sid, spent, trouble = read_claude(text)
            lines.append(f"session id: {sid}")
        else:
            said, sid, spent, trouble = read_agy(text, last_of(harness, runs, name, n, resume) if resume else "")
            lines.append(f"conversation id: {sid}")
        if trouble:
            lines.append(f"{harness} {trouble}")
            status = status or (124 if trouble.startswith("timed") else 1)
        answer.write_text(said, encoding="utf-8")
    if spent:
        lines.append(book(note, repo.name, model, spent))
    lines.append(f"answer: {answer}; work: {tree}")
    print("\n".join(lines), flush=True)
    return status, note, (harness, name)


def review(sha, repo, home, model):
    found = git("rev-parse", "-q", "--verify", f"{sha}^{{commit}}", cwd=repo)
    if found.returncode:
        raise Problem(f"no commit {sha} in {repo}")
    installed("codex")
    # Named after the commit's own hash, so `v1.2` or `origin/main` is neither a suffix nor a folder.
    sha = found.stdout.strip()[:12]
    runs = home / "runs" / "codex"
    runs.mkdir(parents=True, exist_ok=True)
    n = 1
    while (runs / f"review-{sha}-{n}.md").exists():
        n += 1
    answer, raw, log = (runs / f"review-{sha}-{n}.{kind}" for kind in ("md", "jsonl", "log"))
    note = f"review {sha}"
    reading("before", note)
    started = time.time()
    status = run(["codex", "exec", "-m", model, "-s", "read-only", "-o", str(answer), "review",
                  "--json", "--commit", sha], repo, raw, log)
    thread, _ = read_codex(raw.read_text(encoding="utf-8", errors="replace"))
    print(f"session id: {thread}")
    # A review's own events count no tokens (measured): the work is a child session, whose rollout names it as parent.
    spent = child_usage(thread, started)
    if spent:
        print(book(note, repo.name, model, spent))
    print(f"answer: {answer}")
    return status, note, None


def child_usage(thread, since, sessions=pathlib.Path.home() / ".codex" / "sessions"):
    """The counts of the codex session whose parent is THREAD, from its rollout written since SINCE; None if none."""
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


def changes(tree):
    """TREE's uncommitted changes, TASK.md left out, as `git status --porcelain` lines."""
    return [line for line in git("status", "--porcelain", cwd=tree).stdout.splitlines()
            if line[3:] != "TASK.md"]


def made(tree, repo):
    """What git ignores in TREE that REPO's own checkout has no path for, so the job made it: a results file, a
    config it wrote. `git worktree remove` deletes ignored files, with or without its --force (measured). A path the
    checkout has too, a .venv or a __pycache__, is a local copy of something rebuilt at will, and is left out."""
    entries = git("status", "--porcelain", "--ignored", cwd=tree).stdout.splitlines()
    return [line[3:] for line in entries if line.startswith("!! ") and not (repo / line[3:].rstrip("/")).exists()]


def clean(harness, name, repo, home, force=False):
    """Remove a job's worktree and branch; unless FORCE, not while it holds work that would be lost with them."""
    tree = home / "worktrees" / f"{harness}-{name}"
    if not tree.exists():
        raise Problem(f"no worktree {tree}")
    if common(tree) != common(repo):
        raise Problem(f"{tree} is not a worktree of {repo}")
    if not force:
        unmerged = git("rev-list", "--count", "HEAD.." + tree.name, cwd=repo).stdout.strip()
        changed, new = changes(tree), made(tree, repo)
        if changed or new or unmerged not in ("", "0"):
            raise Problem(f"{tree} has {len(changed)} uncommitted files, {len(new)} ignored files the job made"
                          f"{' (' + ', '.join(new) + ')' if new else ''} and {unmerged or 0} commits not in"
                          f" {repo.name}'s HEAD: merge what is worth keeping, or clean with --force")
    for step in (("worktree", "remove", "--force", str(tree)), ("branch", "-D", "-q", tree.name)):
        done = git(*step, cwd=repo)
        if done.returncode:
            raise Problem(done.stderr.strip())
    print(f"removed {tree} and its branch")


def home_of(repo, homes=None):
    """Where REPO's worktrees/ and runs/ go: what HOMES (homes.toml) names for it, else PARENT/.askai/REPO."""
    import tomllib
    path = homes or HOMES
    named = {}
    if path.exists():
        with path.open("rb") as handle:
            named = tomllib.load(handle).get("homes", {})
    for key, value in named.items():
        if pathlib.Path(key).resolve() == repo:
            return (repo / value).resolve()
    return repo.parent / ".askai" / repo.name


def jobs(repo, home):
    """One row a worktree of REPO under HOME: job, runs, age, answer, changes, id."""
    rows = []
    folder = home / "worktrees"
    for tree in sorted(folder.iterdir()) if folder.exists() else ():
        harness, _, name = tree.name.partition("-")
        if harness not in HARNESSES or not tree.is_dir() or common(tree) != common(repo):
            continue
        runs = home / "runs" / harness
        n = numbered(runs, name) - 1
        last = runs / f"{name}-{n}.jsonl"
        text = last.read_text(encoding="utf-8", errors="replace") if last.exists() else ""
        answer = runs / f"{name}-{n}.md"
        said = "yes" if answer.exists() and answer.stat().st_size else "no"
        changed = changes(tree)
        age = usage.span(time.time() - (last if last.exists() else tree).stat().st_mtime)
        rows.append([tree.name, n, age, said, len(changed), session(harness, text) or "-"])
    return rows


def listing(repo, home):
    rows = jobs(repo, home)
    if not rows:
        print(f"no open jobs for {repo} in {home / 'worktrees'}")
        return
    print("\n".join(usage.table(["job", "runs", "last run", "answer", "changed files", "id"], rows,
                                text=(0, 3, 5), notes={
                                    "last run": "how long ago its latest output was written",
                                    "changed files": "uncommitted, TASK.md left out",
                                    "id": "what a follow-up passes; - if no run got far enough to say"})))


def parse(argv):
    parser = argparse.ArgumentParser(prog="askai", description=__doc__.split("\n\n")[0],
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("words", nargs="+", help="agy|claude|codex read|edit PROMPT [ID], review SHA, clean ..., list")
    parser.add_argument("--repo", help="the repository; the one the current folder is in unless given")
    parser.add_argument("--home", help="where worktrees/ and runs/ go; homes.toml's or PARENT/.askai/REPO unless given")
    parser.add_argument("--model", help="the harness's model instead of its default")
    parser.add_argument("--timeout", default="3600s", help="agy's print timeout (default 3600s)")
    parser.add_argument("--force", action="store_true", help="clean a worktree even though it holds unmerged work")
    return parser.parse_args(argv)


def main(argv=None):
    # Only the real command binds its children to it; a test calling main is not to join a job object.
    command_line, argv = argv is None, sys.argv[1:] if argv is None else argv
    if argv and argv[0] in READERS:
        return READERS[argv[0]](argv[1:])
    args = parse(argv)
    words = args.words
    if command_line:
        bound()
    try:
        repo = toplevel(pathlib.Path(args.repo or os.getcwd()).resolve())
        home = pathlib.Path(args.home).resolve() if args.home else home_of(repo)
        what = words[0]
        if what in HARNESSES:
            if len(words) not in (3, 4) or words[1] not in ("read", "edit"):
                raise Problem(f"askai {what} read|edit PROMPT [ID]")
            status, note, cleanup = job(what, words[1], words[2], words[3] if len(words) == 4 else None, repo, home,
                                        args.model or DEFAULTS[what], args.timeout)
        elif what == "review":
            if len(words) != 2:
                raise Problem("askai review SHA")
            status, note, cleanup = review(words[1], repo, home, args.model or DEFAULTS["codex"])
        elif what == "clean":
            if len(words) != 3 or words[1] not in HARNESSES:
                raise Problem("askai clean agy|claude|codex NAME")
            clean(words[1], words[2], repo, home, args.force)
            return 0
        elif what == "list":
            if len(words) != 1:
                raise Problem("askai list")
            listing(repo, home)
            return 0
        else:
            raise Problem(f"unknown {what}: agy, claude, codex, review, clean or list")
    except Problem as problem:
        print(problem, file=sys.stderr)
        return 2
    print(f"exit {status}")
    print("\n".join(reading("after", note)))
    print()
    sys.stdout.flush()
    pace.main(["--brief"])
    if cleanup:
        harness, name = cleanup
        print(f"\nREMINDER: when the answer is reviewed and anything worth keeping is merged, remove the worktree:\n"
              f"  askai clean {harness} {name}\n"
              f"A follow-up in the same worktree goes first: a new prompt file named {name}-prompt.md plus the id above.")
    return status


if __name__ == "__main__":
    sys.exit(main())
