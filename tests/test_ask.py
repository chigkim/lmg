import csv
import json
import os
import pathlib
import subprocess
import sys
import threading

import pytest

from llm_gateway import ask, calls, cli, daemon, tasks, usage


def test_a_job_is_named_after_its_prompt_file():
    assert ask.job_name("some/where/fix-ledger-prompt.md") == "fix-ledger"
    assert ask.job_name("fix-ledger.md") == "fix-ledger"
    assert ask.job_name("notes") == "notes"


def test_read_mode_gives_each_harness_its_own_limit():
    assert ask.flags("codex", "read") == ["-s", "read-only"]
    assert "--tools" in ask.flags("claude", "read") and "--strict-mcp-config" in ask.flags("claude", "read")
    # agy has no read-only setting: read is its bypass left off.
    assert ask.flags("agy", "read") == []
    for harness in ask.HARNESSES:
        assert any("dangerously" in flag for flag in ask.flags(harness, "edit"))


def test_codex_may_run_commands_to_read_but_the_others_are_told_not_to():
    assert "do not run any command" not in ask.ask_text("codex", "read")
    assert "do not run any command" in ask.ask_text("agy", "read")
    assert ask.ask_text("claude", "edit") == ask.ASK


def test_a_follow_up_is_told_its_task_is_new():
    assert ask.ask_text("claude", "edit", resume="s1") == ask.AGAIN
    argv = ask.command("agy", "read", "m", "tree", "a.md", resume="c1")
    assert argv[argv.index("-p") + 1].startswith(ask.AGAIN)


def test_a_codex_follow_up_sets_its_sandbox_again(tmp_path):
    argv = ask.command("codex", "read", "gpt-6-sol", tmp_path, tmp_path / "a.md", resume="abc")
    assert argv[:4] == ["codex", "exec", "resume", "abc"]
    assert 'sandbox_mode="read-only"' in argv and "-C" not in argv


def test_codex_input_leaves_out_what_was_cached_or_written_to_cache():
    events = "\n".join([json.dumps({"type": "thread.started", "thread_id": "t1"}),
                        json.dumps({"type": "turn.completed", "usage": {"input_tokens": 100, "cached_input_tokens": 60,
                                                                        "output_tokens": 7}}),
                        json.dumps({"type": "turn.completed", "usage": {"input_tokens": 300, "cached_input_tokens": 200,
                                                                        "cache_write_input_tokens": 10,
                                                                        "output_tokens": 9}})])
    thread, spent = ask.read_codex(events)
    assert thread == "t1"
    assert spent == ask.counts(90, 200, 10, 0, 9)


def test_a_codex_follow_up_books_only_its_own_tokens():
    # The totals a live gpt-6-luna follow-up reported: the second run's include the first's.
    first = stream({"type": "thread.started", "thread_id": "t1"},
                   {"type": "turn.completed", "usage": {"input_tokens": 43662, "cached_input_tokens": 39168,
                                                        "output_tokens": 132}})
    second = stream({"type": "thread.started", "thread_id": "t1"},
                    {"type": "turn.completed", "usage": {"input_tokens": 88046, "cached_input_tokens": 78336,
                                                         "output_tokens": 241}})
    assert ask.read_codex(second, first)[1] == ask.counts(44384 - 39168, 39168, 0, 0, 109)


def stream(*events):
    return "\n".join(json.dumps(event) for event in events) + "\n"


def test_claude_cache_writes_kept_an_hour_are_counted_apart():
    text = stream({"type": "system", "subtype": "init", "session_id": "s1"},
                  {"type": "assistant", "session_id": "s1"},
                  {"type": "result", "subtype": "success", "result": "done", "session_id": "s1", "usage": {
                      "input_tokens": 5, "cache_read_input_tokens": 50, "cache_creation_input_tokens": 40,
                      "output_tokens": 3, "cache_creation": {"ephemeral_1h_input_tokens": 40,
                                                             "ephemeral_5m_input_tokens": 0}}})
    assert ask.read_claude("a warning first\n" + text) == ("done", "s1", ask.counts(5, 50, 40, 40, 3), "")


def test_a_run_stopped_part_way_still_gives_its_id():
    claude = stream({"type": "system", "subtype": "init", "session_id": "s1"}, {"type": "assistant"})
    said, sid, spent, trouble = ask.read_claude(claude)
    assert (said, sid, spent) == ("", "s1", None) and trouble.startswith("ended early")
    agy = stream({"event": "init", "conversation_id": "c1", "init": {}}, {"event": "step_update"})
    assert ask.read_agy(agy)[1] == "c1" and ask.read_agy(agy)[3].startswith("ended early")


def agy_result(**result):
    return stream({"event": "init", "conversation_id": "c1", "init": {}}, {"event": "result", "result": result})


def test_an_agy_follow_up_counts_only_its_own_share():
    before = agy_result(usage={"input_tokens": 100, "cache_read_tokens": 10, "output_tokens": 5})
    now = "jetski: note\n" + agy_result(conversation_id="c1", response="ok", usage={
        "input_tokens": 250, "cache_read_tokens": 40, "output_tokens": 12})
    said, cid, spent, trouble = ask.read_agy(now, before)
    assert (said, cid, trouble) == ("ok", "c1", "")
    assert spent == ask.counts(150, 30, 0, 0, 7)


def test_agy_says_why_a_run_ended_unfinished():
    denied = agy_result(response="", denied_actions=[{"action": "command", "display_name": "RunCommand"}])
    assert ask.read_agy(denied)[3].startswith("denied RunCommand")
    assert ask.read_agy("print timeout after 60s\n" + agy_result(response="half"))[3].startswith("timed out")


def test_the_claude_prompt_is_not_taken_as_a_tool_name(tmp_path):
    argv = ask.command("claude", "read", "m", tmp_path, tmp_path / "a.md")
    assert argv[argv.index("-p") + 1] == ask.ask_text("claude", "read")
    assert argv[argv.index("--output-format") + 1] == "stream-json"


def test_cost_prices_hour_long_cache_writes_at_their_own_rate():
    table = {"m": {"input": 1, "cache_read": 0.1, "cache_write": 2, "cache_write_1h": 4, "output": 10}}
    spent = ask.counts(1_000_000, 1_000_000, 1_000_000, 250_000, 1_000_000)
    assert ask.cost("m-medium", spent, table) == pytest.approx(1 + 0.1 + 2 * 0.75 + 4 * 0.25 + 10)
    assert ask.cost("unpriced", spent, table) is None


def test_the_token_ledger_row_carries_the_repo(tmp_path, monkeypatch):
    monkeypatch.setattr(ask.calls, "prices", lambda: {})
    path = tmp_path / "tokens.csv"
    line = ask.book("codex x", "some-repo", "nobody", ask.counts(1, 2, 3, 0, 4), path)
    assert "unknown" in line
    [row] = csv.DictReader(path.open(encoding="utf-8"))
    assert (row["repo"], row["input"], row["output"], row["usd"]) == ("some-repo", "1", "4", "")


def git(*args, cwd):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path):
    folder = tmp_path / "project"
    folder.mkdir()
    git("init", "-q", cwd=folder)
    (folder / "a.txt").write_text("a")
    git("add", ".", cwd=folder)
    git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "a", cwd=folder)
    return ask.toplevel(folder)


def test_a_job_runs_in_its_own_worktree_and_leaves_its_answer_in_runs(repo, tmp_path, monkeypatch):
    monkeypatch.setattr(ask, "reading", lambda stage, note: [])
    monkeypatch.setattr(ask, "installed", lambda harness: None)
    monkeypatch.setattr(ask.calls, "prices", lambda: {})
    monkeypatch.setattr(ask, "TOKENS", tmp_path / "tokens.csv")
    seen = {}

    def harness(argv, cwd, out, err=None):
        seen.update(argv=argv, cwd=cwd)
        out.write_text(stream({"type": "result", "result": "the answer", "session_id": "s9",
                               "usage": {"input_tokens": 1}}))
        return 0
    monkeypatch.setattr(ask, "run", harness)
    prompt = tmp_path / "look-prompt.md"
    prompt.write_text("look around")
    status, note, _ = ask.job("claude", "edit", prompt, None, repo, tmp_path, "claude-opus-5-5", "3600s")
    tree = tmp_path / "worktrees" / "claude-look"
    assert (status, note, seen["cwd"]) == (0, f"claude look [pid {os.getpid()}]", tree)
    assert (tree / "TASK.md").read_text() == "look around" and not prompt.exists()
    assert (tmp_path / "runs" / "claude" / "look-1.task.md").read_text() == "look around"
    assert (tmp_path / "runs" / "claude" / "look-1.md").read_text(encoding="utf-8") == "the answer"
    (tree / "new.txt").write_text("x")
    [row] = ask.jobs(repo, tmp_path)
    assert (row[0], row[1], row[3], row[4], row[5]) == ("claude-look", 1, "yes", 1, "s9")
    with pytest.raises(ask.Problem, match="1 uncommitted"):
        ask.clean("claude", "look", repo, tmp_path)
    ask.clean("claude", "look", repo, tmp_path, force=True)
    assert ask.jobs(repo, tmp_path) == []
    assert not tree.exists()


def test_a_dot_in_a_job_s_name_is_not_a_suffix(repo, tmp_path, monkeypatch):
    fake_harness(monkeypatch, tmp_path)
    for n in (1, 2):
        prompt = tmp_path / f"bump-3.1{n}-prompt.md"
        prompt.write_text("bump")
        ask.job("claude", "read", prompt, None, repo, tmp_path, "m", "3600s")
    runs = tmp_path / "runs" / "claude"
    assert {p.name for p in runs.glob("*.md")} == {"bump-3.11-1.md", "bump-3.11-1.task.md", "bump-3.12-1.md",
                                                   "bump-3.12-1.task.md"}


def test_a_fresh_job_is_not_handed_an_earlier_job_s_worktree(repo, tmp_path, monkeypatch):
    fake_harness(monkeypatch, tmp_path)
    (tmp_path / "look-prompt.md").write_text("look")
    ask.job("claude", "edit", tmp_path / "look-prompt.md", None, repo, tmp_path, "m", "3600s")
    (tmp_path / "look-prompt.md").write_text("look again")
    with pytest.raises(ask.Problem, match="earlier job"):
        ask.job("claude", "edit", tmp_path / "look-prompt.md", None, repo, tmp_path, "m", "3600s")
    ask.job("claude", "edit", tmp_path / "look-prompt.md", "s9", repo, tmp_path, "m", "3600s")
    assert (tmp_path / "worktrees" / "claude-look" / "TASK.md").read_text() == "look again"


def test_an_agy_follow_up_subtracts_the_last_run_of_its_own_conversation(tmp_path):
    (tmp_path / "x-1.jsonl").write_text(agy_result(conversation_id="c1", usage={"input_tokens": 100}))
    (tmp_path / "x-2.jsonl").write_text(agy_result(conversation_id="c2", usage={"input_tokens": 5}))
    assert ask.session("agy", ask.last_of("agy", tmp_path, "x", 3, "c1")) == "c1"
    assert ask.last_of("agy", tmp_path, "x", 3, "gone") == ""


def fake_harness(monkeypatch, tmp_path):
    monkeypatch.setattr(ask, "reading", lambda stage, note: [])
    monkeypatch.setattr(ask, "installed", lambda harness: None)
    monkeypatch.setattr(ask.calls, "prices", lambda: {})
    monkeypatch.setattr(ask, "TOKENS", tmp_path / "tokens.csv")
    monkeypatch.setattr(ask, "run", lambda argv, cwd, out, err=None: out.write_text(stream(
        {"type": "result", "result": "ok", "session_id": "s9", "usage": {}})) and 0)


def test_a_folder_of_another_repository_is_not_taken_for_the_job(repo, tmp_path):
    other = tmp_path / "worktrees" / "codex-look"
    other.mkdir(parents=True)
    git("init", "-q", cwd=other)
    with pytest.raises(ask.Problem, match="not a worktree"):
        ask.worktree(repo, other, None)
    assert ask.worktree(repo, tmp_path / "worktrees" / "codex-none", "id")


def test_usage_mistakes_exit_2_without_running_anything(repo, capsys):
    assert ask.main(["--repo", str(repo), "codex", "write", "x.md"]) == 2
    assert ask.main(["--repo", str(repo), "nope"]) == 2
    assert ask.main(["--repo", str(repo), "clean", "codex", "gone"]) == 2
    assert "no worktree" in capsys.readouterr().err


def test_a_repository_s_home_is_beside_it_unless_homes_toml_names_one(repo, tmp_path):
    homes = tmp_path / "homes.toml"
    assert ask.home_of(repo, homes) == ask.JOBS / repo.name
    assert ask.JOBS == pathlib.Path(ask.__file__).resolve().parents[2] / "jobs"
    homes.write_text(f'[homes]\n"{repo.as_posix()}" = ".."\n', encoding="utf-8")
    assert ask.home_of(repo, homes) == repo.parent


def test_claude_read_mode_is_the_three_tools_and_nothing_else():
    assert ask.flags("claude", "read") == ["--tools", "Read,Grep,Glob", "--allowedTools", "Read,Grep,Glob",
                                           "--permission-mode", "dontAsk", "--strict-mcp-config"]


def test_lmg_hands_each_group_the_rest_of_the_line(monkeypatch):
    seen = []
    monkeypatch.setattr(daemon, "main", lambda argv: seen.append(("proxy", argv)) or 0)
    monkeypatch.setattr(ask, "main", lambda argv, command_line: seen.append(("ask", argv, command_line)) or 0)
    monkeypatch.setattr(usage, "main", lambda argv: seen.append(("usage", argv)) or 0)
    monkeypatch.setattr(calls, "main", lambda argv: seen.append(("calls", argv)) or 0)
    monkeypatch.setattr(tasks, "main", lambda argv: seen.append(("tasks", argv)) or 0)
    monkeypatch.setattr(calls, "worth", lambda argv: seen.append(("worth", argv)) or 0)
    assert cli.main(["proxy", "status", "--port", "9"]) == 0
    assert cli.main(["ask", "list"]) == 0
    assert cli.main(["usage", "before: pi x"]) == 0
    assert cli.main(["usage", "calls", "--hours", "1"]) == 0
    assert cli.main(["usage", "tasks"]) == 0
    assert cli.main(["usage", "worth"]) == 0
    assert seen == [("proxy", ["status", "--port", "9"]), ("ask", ["list"], False), ("usage", ["before: pi x"]),
                    ("calls", ["--hours", "1"]), ("tasks", []), ("worth", [])]
    assert cli.main(["nope"]) == 2


def test_the_proxy_loads_only_its_own_modules():
    code = ("import sys; from llm_gateway import cli; cli.main(['proxy', 'status', '--port', '1']); "
            "print(sorted(m for m in sys.modules if m.startswith('llm_gateway.')))")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True).stdout
    assert "llm_gateway.daemon" in out and "llm_gateway.tasks" not in out and "llm_gateway.ask" not in out
    assert cli.main([]) == 2


def test_clean_keeps_an_ignored_file_the_job_made_but_not_a_copy_the_checkout_has(repo, tmp_path, monkeypatch):
    fake_harness(monkeypatch, tmp_path)
    (repo / ".gitignore").write_text(".venv/\nresults.db\n")
    git("add", ".", cwd=repo)
    git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "ignore", cwd=repo)
    (repo / ".venv").mkdir()
    (tmp_path / "work-prompt.md").write_text("work")
    ask.job("claude", "edit", tmp_path / "work-prompt.md", None, repo, tmp_path, "m", "3600s")
    tree = tmp_path / "worktrees" / "claude-work"
    (tree / ".venv").mkdir()
    (tree / ".venv" / "f").write_text("rebuilt at will")
    (tree / ".venv" / "pyvenv.cfg").write_text("home = x")
    (tree / "results.db").write_text("the job's work")
    with pytest.raises(ask.Problem, match=r"1 ignored files the job made \(results.db\)"):
        ask.clean("claude", "work", repo, tmp_path)
    (tree / "results.db").unlink()
    ask.clean("claude", "work", repo, tmp_path)
    assert not tree.exists()


def test_clean_keeps_an_ignored_file_the_job_changed_though_the_checkout_has_one_of_that_name(repo, tmp_path,
                                                                                              monkeypatch):
    fake_harness(monkeypatch, tmp_path)
    (repo / ".gitignore").write_text("results.db\n")
    git("add", ".", cwd=repo)
    git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "ignore", cwd=repo)
    (repo / "results.db").write_text("the checkout's own")
    (tmp_path / "work-prompt.md").write_text("work")
    ask.job("claude", "edit", tmp_path / "work-prompt.md", None, repo, tmp_path, "m", "3600s")
    tree = tmp_path / "worktrees" / "claude-work"
    (tree / "results.db").write_text("the job's work")
    with pytest.raises(ask.Problem, match=r"1 ignored files the job made \(results.db\)"):
        ask.clean("claude", "work", repo, tmp_path)
    # The same bytes as the checkout's are a copy, and nothing is lost with them.
    (tree / "results.db").write_text("the checkout's own")
    ask.clean("claude", "work", repo, tmp_path)
    assert not tree.exists()


def test_a_task_copy_is_never_another_job_s_answer(repo, tmp_path, monkeypatch):
    fake_harness(monkeypatch, tmp_path)
    for name in ("foo", "foo-task"):
        (tmp_path / f"{name}-prompt.md").write_text(f"the {name} task")
        ask.job("claude", "read", tmp_path / f"{name}-prompt.md", None, repo, tmp_path, "m", "3600s")
    runs = tmp_path / "runs" / "claude"
    assert (runs / "foo-1.task.md").read_text() == "the foo task"
    assert (runs / "foo-task-1.task.md").read_text() == "the foo-task task"


def test_an_agy_run_stopped_before_its_total_books_nothing_and_is_no_baseline(tmp_path):
    stopped = stream({"event": "init", "conversation_id": "c1"})
    said, sid, spent, trouble = ask.read_agy(stopped, agy_result(conversation_id="c1", usage={"input_tokens": 100}))
    assert (sid, spent) == ("c1", None) and trouble.startswith("ended early")
    (tmp_path / "x-1.jsonl").write_text(agy_result(conversation_id="c1", usage={"input_tokens": 100}))
    (tmp_path / "x-2.jsonl").write_text(stopped)
    assert "100" in ask.last_of("agy", tmp_path, "x", 3, "c1")


def test_two_ledgers_made_at_once_have_one_header(tmp_path):
    path = tmp_path / "ledger.csv"
    threads = [threading.Thread(target=usage.append, args=(path, ["time", "note"], {"time": "t", "note": str(n)}))
               for n in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    lines = path.read_text().splitlines()
    assert lines.count("time,note") == 1 and len(lines) == 9
    assert list(tmp_path.iterdir()) == [path]


def test_clean_refuses_a_branch_with_commits_not_in_head(repo, tmp_path, monkeypatch):
    fake_harness(monkeypatch, tmp_path)
    (tmp_path / "work-prompt.md").write_text("work")
    ask.job("claude", "edit", tmp_path / "work-prompt.md", None, repo, tmp_path, "m", "3600s")
    tree = tmp_path / "worktrees" / "claude-work"
    (tree / "b.txt").write_text("b")
    git("add", "b.txt", cwd=tree)
    git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "b", cwd=tree)
    with pytest.raises(ask.Problem, match="1 commits not in"):
        ask.clean("claude", "work", repo, tmp_path)
    assert tree.exists()


def test_an_agy_answer_quoting_the_timeout_notice_is_not_a_timeout():
    text = agy_result(conversation_id="c1", response='it checks for "print timeout after" in the output',
                      usage={"input_tokens": 1})
    assert ask.read_agy(text)[3] == ""
    assert ask.read_agy(text + "print timeout after 3600s\n")[3].startswith("timed out")


def test_a_job_refused_its_worktree_takes_no_reading(repo, tmp_path, monkeypatch):
    fake_harness(monkeypatch, tmp_path)
    (tmp_path / "look-prompt.md").write_text("look")
    ask.job("claude", "edit", tmp_path / "look-prompt.md", None, repo, tmp_path, "m", "3600s")
    readings = []
    monkeypatch.setattr(ask, "reading", lambda stage, note: readings.append(stage) or [])
    (tmp_path / "look-prompt.md").write_text("look again")
    with pytest.raises(ask.Problem, match="earlier job"):
        ask.job("claude", "edit", tmp_path / "look-prompt.md", None, repo, tmp_path, "m", "3600s")
    assert readings == []


def edit_job(repo, tmp_path, monkeypatch):
    fake_harness(monkeypatch, tmp_path)
    (tmp_path / "work-prompt.md").write_text("work")
    ask.job("claude", "edit", tmp_path / "work-prompt.md", None, repo, tmp_path, "m", "3600s")
    return tmp_path / "worktrees" / "claude-work"


def test_clean_refuses_commits_made_on_a_detached_head(repo, tmp_path, monkeypatch):
    tree = edit_job(repo, tmp_path, monkeypatch)
    git("checkout", "-q", "--detach", cwd=tree)
    (tree / "b.txt").write_text("b")
    git("add", "b.txt", cwd=tree)
    git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "b", cwd=tree)
    with pytest.raises(ask.Problem, match="1 commits not in"):
        ask.clean("claude", "work", repo, tmp_path)
    assert tree.exists()


def test_clean_refuses_when_git_cannot_say_what_would_be_lost(repo, tmp_path, monkeypatch):
    tree = edit_job(repo, tmp_path, monkeypatch)
    git("checkout", "-q", "--detach", cwd=tree)
    git("branch", "-q", "-m", "claude-work", "renamed", cwd=repo)
    with pytest.raises(ask.Problem, match="git rev-list"):
        ask.clean("claude", "work", repo, tmp_path)
    assert tree.exists()


def test_clean_keeps_a_file_the_job_wrote_in_an_ignored_folder_the_checkout_has(repo, tmp_path, monkeypatch):
    (repo / ".gitignore").write_text("logs/\n")
    git("add", ".", cwd=repo)
    git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "ignore", cwd=repo)
    (repo / "logs").mkdir()
    (repo / "logs" / "old.jsonl").write_text("the checkout's")
    tree = edit_job(repo, tmp_path, monkeypatch)
    (tree / "logs").mkdir()
    (tree / "logs" / "old.jsonl").write_text("the checkout's")
    (tree / "logs" / "new.jsonl").write_text("the job's work")
    (tree / "pkg" / "__pycache__").mkdir(parents=True)
    (tree / "pkg" / "__pycache__" / "m.pyc").write_text("rebuilt at will")
    (tree / "logs" / "a cache").mkdir()
    (tree / "logs" / "a cache" / "CACHEDIR.TAG").write_text("Signature: 8a477f597d28d172789f06886806bc55")
    (tree / "logs" / "a cache" / "lastfailed").write_text("rebuilt at will")
    (tree / "logs" / "old copy.jsonl").write_text("the checkout's")
    (repo / "logs" / "old copy.jsonl").write_text("the checkout's")
    with pytest.raises(ask.Problem, match=r"1 ignored files the job made \(logs/new.jsonl\)"):
        ask.clean("claude", "work", repo, tmp_path)


def test_a_review_stopped_before_its_answer_keeps_its_number(repo, tmp_path, monkeypatch):
    monkeypatch.setattr(ask, "reading", lambda stage, note: [])
    monkeypatch.setattr(ask, "installed", lambda harness: None)
    monkeypatch.setattr(ask, "child_usage", lambda thread, since: None)
    written = []

    def stopped(argv, cwd, out, err=None):
        written.append(out.name)
        out.write_text(stream({"type": "thread.started", "thread_id": "t1"}))
        return 130
    monkeypatch.setattr(ask, "run", stopped)
    ask.review("HEAD", repo, tmp_path, "m")
    ask.review("HEAD", repo, tmp_path, "m")
    assert [name.rsplit("-", 1)[1] for name in written] == ["1.jsonl", "2.jsonl"]


def test_a_ledger_that_cannot_be_written_is_a_problem_not_a_traceback(monkeypatch):
    monkeypatch.setattr(ask.usage, "asked", lambda: [])

    def locked(note, report):
        raise PermissionError("in use")
    monkeypatch.setattr(ask.usage, "record", locked)
    with pytest.raises(ask.Problem, match="before reading"):
        ask.reading("before", "claude x")


def test_a_booking_that_fails_is_said_and_the_result_is_not_lost(monkeypatch):
    def locked(*args):
        raise PermissionError("in use")
    monkeypatch.setattr(ask, "book", locked)
    assert ask.booked("claude x", "repo", "m", {}) == "tokens not booked: in use"


def test_a_harness_stopped_by_hand_is_a_run_stopped_part_way(tmp_path, monkeypatch):
    class Child:
        pid, waited, killed = 0, 0, False

        def wait(self):
            self.waited += 1
            if self.waited == 1:
                raise KeyboardInterrupt
            return -9

        def kill(self):
            self.killed = True
    child = Child()
    monkeypatch.setattr(ask.usage, "cli", lambda name: name)
    monkeypatch.setattr(ask.subprocess, "Popen", lambda *a, **k: child)
    monkeypatch.setattr(ask.os, "killpg", lambda pid, sig: None, raising=False)
    trees = []
    # The whole tree: on Windows the harness is a .cmd shim over the process doing the work.
    monkeypatch.setattr(ask.subprocess, "run", lambda argv, **k: trees.append(argv))
    assert ask.run(["claude"], tmp_path, tmp_path / "out.jsonl") == 130 and child.killed
    assert trees == ([["taskkill", "/F", "/T", "/PID", "0"]] if ask.os.name == "nt" else [])


def test_list_shows_the_id_an_earlier_run_named_when_the_latest_said_none(tmp_path):
    (tmp_path / "x-1.jsonl").write_text(stream({"type": "system", "session_id": "s1"}))
    (tmp_path / "x-2.jsonl").write_text("")
    assert ask.latest_id("claude", tmp_path, "x", 2) == "s1"


def test_a_read_job_removes_its_worktree_and_a_follow_up_makes_it_again(repo, tmp_path, monkeypatch):
    fake_harness(monkeypatch, tmp_path)
    (tmp_path / "look-prompt.md").write_text("look")
    status, note, cleanup = ask.job("claude", "read", tmp_path / "look-prompt.md", None, repo, tmp_path, "m", "3600s")
    tree = tmp_path / "worktrees" / "claude-look"
    assert cleanup is None and not tree.exists() and ask.jobs(repo, tmp_path) == []
    assert (tmp_path / "runs" / "claude" / "look-1.md").read_text(encoding="utf-8") == "ok"
    (tmp_path / "look-prompt.md").write_text("look again")
    ask.job("claude", "read", tmp_path / "look-prompt.md", "s9", repo, tmp_path, "m", "3600s")
    assert (tmp_path / "runs" / "claude" / "look-2.task.md").read_text() == "look again" and not tree.exists()


def test_a_read_job_that_left_work_keeps_its_worktree(repo, tmp_path, monkeypatch):
    fake_harness(monkeypatch, tmp_path)

    def writes(argv, cwd, out, err=None):
        (cwd / "notes.txt").write_text("written anyway")
        out.write_text(stream({"type": "result", "result": "ok", "session_id": "s9", "usage": {}}))
        return 0
    monkeypatch.setattr(ask, "run", writes)
    (tmp_path / "look-prompt.md").write_text("look")
    _, _, cleanup = ask.job("claude", "read", tmp_path / "look-prompt.md", None, repo, tmp_path, "m", "3600s")
    assert cleanup == ("claude", "look") and (tmp_path / "worktrees" / "claude-look" / "notes.txt").exists()


def test_runs_of_one_job_started_at_once_each_claim_a_number_of_their_own(tmp_path):
    out = []
    threads = [threading.Thread(target=lambda n=n: out.append(ask.reserved(tmp_path, "x", str(n).encode())))
               for n in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sorted(out) == list(range(1, 9))
    assert {ask.task_copy(tmp_path, "x", n).read_text() for n in out} == {str(n) for n in range(8)}
    assert [ask.claimed(tmp_path, "review-s") for _ in range(3)] == [1, 2, 3]


def test_a_repository_that_tracks_a_task_md_is_refused_a_job(repo, tmp_path, monkeypatch):
    fake_harness(monkeypatch, tmp_path)
    (repo / "TASK.md").write_text("ours")
    git("add", "TASK.md", cwd=repo)
    git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "t", cwd=repo)
    (tmp_path / "x-prompt.md").write_text("the task")
    with pytest.raises(ask.Problem, match="tracks a TASK.md"):
        ask.job("claude", "read", tmp_path / "x-prompt.md", None, repo, tmp_path, "m", "3600s")


def test_a_start_and_a_stop_on_one_port_wait_for_each_other(tmp_path, monkeypatch):
    monkeypatch.setattr(daemon, "STATE", tmp_path)
    order = []

    def held(name):
        with daemon.serialized(8000):
            order.append(f"{name} in")
            threading.Event().wait(0.2)
            order.append(f"{name} out")
    threads = [threading.Thread(target=held, args=(n,)) for n in "ab"]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert [o.split()[1] for o in order] == ["in", "out", "in", "out"]
