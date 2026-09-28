"""Hand one job to agy, Claude Code or Codex, in its own git worktree, with a usage reading either side.

    ask-ai agy|claude|codex read|edit PROMPT [ID]   # a job, or a follow-up to the one ID names
    ask-ai review SHA                               # codex exec review of one commit
    ask-ai clean agy|claude|codex NAME              # remove a job's worktree and branch

    --repo PATH    the repository, instead of the one the current folder is in
    --home PATH    where worktrees/ and runs/ go, instead of the folder the repository sits in
    --model NAME   the harness's model, instead of DEFAULTS
    --timeout T    agy's print timeout, 3600s unless given

A mode is what the agent may do. `read` is read and search files, nothing else. `edit` bypasses every permission
prompt and sandbox, so it may write, edit and run anything the machine lets it:

    agy     read: no bypass flag, so headless agy denies every write and command; a denial ends the run unanswered
    claude  read: only Read, Grep and Glob, and no MCP servers
    codex   read: the read-only sandbox, where it still runs commands to read with

The job is named after the prompt file, less `.md` and a trailing `-prompt`. Its worktree is
HOME/worktrees/HARNESS-NAME, on a branch of that name at HEAD, made on first use and reused by a follow-up. The
prompt is moved in as TASK.md, with a numbered copy kept as HOME/runs/HARNESS/NAME-task-N.md, and the agent is
told only to read TASK.md there. The harness's final message is the answer, HOME/runs/HARNESS/NAME-N.md, beside
its raw output; the id to follow up with is printed. A follow-up is a new prompt file of the same name, plus that
id, and may change mode.

A reading of each plan's windows goes to ai-usage's ledger as `before: HARNESS NAME` and `after: ...`, so the
harness is the kind `ai-pace` costs. One row a job goes to `token-ledger.csv` beside this file: the repository,
the uncached input, cache reads, cache writes and output, which add up to what the model read and wrote, and what
they would cost on the public API at ai-usage's prices.
"""
import argparse
import csv
import json
import os
import pathlib
import re
import subprocess
import sys
import time
from datetime import datetime

from ai_usage import plan, usage

HARNESSES = ("agy", "claude", "codex")
DEFAULTS = {"agy": "gemini-3.8-flash-medium", "claude": "claude-opus-5-5", "codex": "gpt-6-sol"}
TOKENS = pathlib.Path(__file__).parent / "token-ledger.csv"
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


def ask_text(harness, mode):
    if mode == "edit":
        return ASK
    return ASK + READ_ONLY["codex" if harness == "codex" else "other"]


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
    ask = ask_text(harness, mode)
    if harness == "codex":
        if resume:
            again = ["-c", 'sandbox_mode="read-only"'] if mode == "read" else flags(harness, mode)
            return ["codex", "exec", "resume", resume, "--json", "-m", model, *again, "-o", str(answer), ask]
        return ["codex", "exec", "--json", "-m", model, *flags(harness, mode), "-C", str(tree), "-o", str(answer), ask]
    if harness == "claude":
        return ["claude", "-p", "--output-format", "json", "--model", model, *flags(harness, mode),
                *(["--resume", resume] if resume else []), ask]
    return ["agy", *flags(harness, mode), "--model", model, "--print-timeout", timeout, "--add-dir", str(tree),
            "--output-format", "json", *(["--conversation", resume] if resume else []), "-p", ask]


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


def read_codex(events):
    """codex --json events -> (thread id, counts), from thread.started and the last turn.completed."""
    thread, spent = "", None
    for line in events.splitlines():
        event = json_in(line)
        if event.get("type") == "thread.started" and not thread:
            thread = event.get("thread_id", "")
        elif event.get("type") == "turn.completed" and isinstance(event.get("usage"), dict):
            spent = codex_counts(event["usage"])
    return thread, spent


def read_claude(text):
    """claude -p JSON -> (answer, session id, counts). Its usage is the run's total; cache writes are kept an hour."""
    out = json_in(text)
    spent = out.get("usage") or {}
    hour = (spent.get("cache_creation") or {}).get("ephemeral_1h_input_tokens", 0)
    return out.get("result") or "", out.get("session_id", ""), counts(
        spent.get("input_tokens", 0), spent.get("cache_read_input_tokens", 0),
        spent.get("cache_creation_input_tokens", 0), hour, spent.get("output_tokens", 0))


def read_agy(text, before=""):
    """agy JSON -> (answer, conversation id, counts, trouble). Its usage is a running total over the conversation, so
    a follow-up's is its own less BEFORE, the previous run's output. Its output includes thinking; it reports no cache
    writes. TROUBLE is why the run did not finish, or ''."""
    out, was = json_in(text), (json_in(before).get("usage") or {})
    now = out.get("usage") or {}

    def spent(key):
        return now.get(key, 0) - was.get(key, 0)
    trouble = ""
    # agy says SUCCESS and exits 0 when --print-timeout cuts a turn off.
    if "print timeout after" in text:
        trouble = "timed out: the answer and the worktree are partial; follow up with the id"
    elif out.get("denied_actions"):
        denied = ", ".join(d.get("display_name") or d.get("action", "?") for d in out["denied_actions"])
        trouble = f"denied {denied}: read mode refused it and the run ended there"
    return out.get("response") or "", out.get("conversation_id", ""), counts(
        spent("input_tokens"), spent("cache_read_tokens"), 0, 0, spent("output_tokens")), trouble


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
    fresh = not path.exists()
    with path.open("a", encoding="utf-8", newline="") as ledger:
        writer = csv.DictWriter(ledger, TOKEN_FIELDS, lineterminator=chr(10))
        if fresh:
            writer.writeheader()
        writer.writerow({"time": datetime.now().strftime("%Y-%m-%d %H:%M"), "note": note, "repo": repo,
                         "model": model, **spent, "usd": "" if usd is None else f"{usd:.2f}"})
    price = f"${usd:.2f}" if usd is not None else f"unknown ({model} has no price in {plan.PRICES})"
    return (f"tokens: input {spent['input']}, cache read {spent['cache_read']}, cache write {spent['cache_write']},"
            f" output {spent['output']}; API equivalent {price}")


def git(*args, cwd):
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)


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
    """Make TREE a worktree of REPO at HEAD, or check that the one there already is."""
    if tree.exists():
        if common(tree) != common(repo):
            raise Problem(f"{tree} is there but is not a worktree of {repo}")
        return
    if resume:
        raise Problem(f"no worktree {tree} to follow up in")
    made = git("worktree", "add", "-q", str(tree), "-b", tree.name, "HEAD", cwd=repo)
    if made.returncode:
        raise Problem(made.stderr.strip() or f"could not make {tree}")


def numbered(folder, name):
    n = 1
    while (folder / f"{name}-task-{n}.md").exists():
        n += 1
    return n


def reading(stage, note):
    """An ai-usage reading into its ledger, which ai-pace pairs; a plan that cannot be asked is said and skipped."""
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
            print(f"warning: no job object (error {ctypes.get_last_error()}), so killing ask-ai may leave the"
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
            if child.poll() is None and os.name != "nt":
                import signal
                os.killpg(child.pid, signal.SIGKILL)
            if errs:
                errs.close()


def job(harness, mode, prompt, resume, repo, home, model, timeout):
    prompt = pathlib.Path(prompt).resolve()
    if not prompt.is_file():
        raise Problem(f"no prompt file {prompt}")
    name = job_name(prompt)
    tree = home / "worktrees" / f"{harness}-{name}"
    runs = home / "runs" / harness
    worktree(repo, tree, resume)
    runs.mkdir(parents=True, exist_ok=True)
    n = numbered(runs, name)
    (runs / f"{name}-task-{n}.md").write_bytes(prompt.read_bytes())
    prompt.replace(tree / "TASK.md")
    out = runs / f"{name}-{n}"
    note = f"{harness} {name}" + (" (resumed)" if resume else "")
    answer = out.with_suffix(".md")
    reading("before", note)
    print(f"{harness} {mode}, {model}, in {tree}", flush=True)
    argv = command(harness, mode, model, tree, answer, resume, timeout)
    lines = []
    if harness == "codex":
        status = run(argv, tree, out.with_suffix(".jsonl"), out.with_suffix(".log"))
        thread, spent = read_codex(out.with_suffix(".jsonl").read_text(encoding="utf-8", errors="replace"))
        lines.append(f"session id: {thread}")
    else:
        raw = out.with_suffix(".json")
        status = run(argv, tree, raw)
        text = raw.read_text(encoding="utf-8", errors="replace")
        if harness == "claude":
            said, sid, spent = read_claude(text)
            lines.append(f"session id: {sid}")
        else:
            before = runs / f"{name}-{n - 1}.json"
            previous = before.read_text(encoding="utf-8", errors="replace") if resume and before.exists() else ""
            said, sid, spent, trouble = read_agy(text, previous)
            lines.append(f"conversation id: {sid}")
            if trouble:
                lines.append(f"agy {trouble}")
                status = status or (124 if trouble.startswith("timed") else 1)
        answer.write_text(said, encoding="utf-8")
    if spent:
        lines.append(book(note, repo.name, model, spent))
    lines.append(f"answer: {answer}; work: {tree}")
    print("\n".join(lines), flush=True)
    return status, note, (harness, name)


def review(sha, repo, home, model):
    if git("rev-parse", "-q", "--verify", f"{sha}^{{commit}}", cwd=repo).returncode:
        raise Problem(f"no commit {sha} in {repo}")
    runs = home / "runs" / "codex"
    runs.mkdir(parents=True, exist_ok=True)
    out = runs / f"review-{sha}"
    note = f"review {sha}"
    reading("before", note)
    started = time.time()
    status = run(["codex", "exec", "-m", model, "-s", "read-only", "-o", str(out.with_suffix(".md")), "review",
                  "--json", "--commit", sha], repo, out.with_suffix(".jsonl"), out.with_suffix(".log"))
    thread, _ = read_codex(out.with_suffix(".jsonl").read_text(encoding="utf-8", errors="replace"))
    print(f"session id: {thread}")
    # A review's own events count no tokens (measured): the work is a child session, whose rollout names it as parent.
    spent = child_usage(thread, started)
    if spent:
        print(book(note, repo.name, model, spent))
    print(f"answer: {out.with_suffix('.md')}")
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


def clean(harness, name, repo, home):
    tree = home / "worktrees" / f"{harness}-{name}"
    if not tree.exists():
        raise Problem(f"no worktree {tree}")
    if common(tree) != common(repo):
        raise Problem(f"{tree} is not a worktree of {repo}")
    for step in (("worktree", "remove", "--force", str(tree)), ("branch", "-D", "-q", tree.name)):
        done = git(*step, cwd=repo)
        if done.returncode:
            raise Problem(done.stderr.strip())
    print(f"removed {tree} and its branch")


def parse(argv):
    parser = argparse.ArgumentParser(prog="ask-ai", description=__doc__.split("\n\n")[0],
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("words", nargs="+", help="agy|claude|codex read|edit PROMPT [ID], review SHA, or clean ...")
    parser.add_argument("--repo", help="the repository; the one the current folder is in unless given")
    parser.add_argument("--home", help="where worktrees/ and runs/ go; the folder the repository sits in unless given")
    parser.add_argument("--model", help="the harness's model instead of its default")
    parser.add_argument("--timeout", default="3600s", help="agy's print timeout (default 3600s)")
    return parser.parse_args(argv)


def main(argv=None):
    args = parse(sys.argv[1:] if argv is None else argv)
    words = args.words
    if argv is None:
        bound()
    try:
        repo = toplevel(pathlib.Path(args.repo or os.getcwd()).resolve())
        home = pathlib.Path(args.home).resolve() if args.home else repo.parent
        what = words[0]
        if what in HARNESSES:
            if len(words) not in (3, 4) or words[1] not in ("read", "edit"):
                raise Problem(f"ask-ai {what} read|edit PROMPT [ID]")
            status, note, cleanup = job(what, words[1], words[2], words[3] if len(words) == 4 else None, repo, home,
                                        args.model or DEFAULTS[what], args.timeout)
        elif what == "review":
            if len(words) != 2:
                raise Problem("ask-ai review SHA")
            status, note, cleanup = review(words[1], repo, home, args.model or DEFAULTS["codex"])
        elif what == "clean":
            if len(words) != 3 or words[1] not in HARNESSES:
                raise Problem("ask-ai clean agy|claude|codex NAME")
            clean(words[1], words[2], repo, home)
            return 0
        else:
            raise Problem(f"unknown {what}: agy, claude, codex, review or clean")
    except Problem as problem:
        print(problem, file=sys.stderr)
        return 2
    print(f"exit {status}")
    print("\n".join(reading("after", note)))
    print()
    sys.stdout.flush()
    subprocess.run([sys.executable, "-m", "ai_usage.pace", "--brief"], env={**os.environ, "PYTHONIOENCODING": "utf-8"})
    if cleanup:
        harness, name = cleanup
        print(f"\nREMINDER: when the answer is reviewed and anything worth keeping is merged, remove the worktree:\n"
              f"  ask-ai clean {harness} {name}\n"
              f"A follow-up in the same worktree goes first: a new prompt file named {name}-prompt.md plus the id above.")
    return status


if __name__ == "__main__":
    sys.exit(main())
