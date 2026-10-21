import csv
import json
import os
import pathlib
import subprocess
import sys
import threading

import pytest

from llm_gateway import ask, calls, cli, daemon, tasks, usage


def test_read_mode_gives_each_harness_its_own_limit():
    assert ask.flags("codex", "read") == ["-s", "read-only"]
    assert "--tools" in ask.flags("claude", "read") and "--strict-mcp-config" not in ask.flags("claude", "read")
    # agy has no read-only setting: read is its bypass left off.
    assert ask.flags("agy", "read") == []
    for harness in ask.HARNESSES:
        assert any("dangerously" in flag for flag in ask.flags(harness, "edit"))


def test_codex_may_run_commands_to_read_but_the_others_are_told_not_to():
    assert "do not run any command" not in ask.ask_text("codex", "read", "t1.md", "r1.md")
    assert "do not run any command" in ask.ask_text("agy", "read", "t1.md", "r1.md")
    assert "do not run any command" in ask.ask_text("claude", "read", "t1.md", "r1.md")
    assert "the web" in ask.ask_text("claude", "read", "t1.md", "r1.md")
    assert ask.ask_text("claude", "edit", "t1.md", "r1.md") == "Read t1.md and do what it says." + ask.SAVED.format(
        answer="r1.md")


def test_a_follow_up_is_told_its_task_is_new(tmp_path):
    assert ask.ask_text("claude", "edit", "t2.md", tmp_path / "r2.md", resume="s1").startswith(
        "t2.md holds a new task")
    argv = ask.command("agy", "read", "m", "tree", "t2.md", tmp_path / "r2.md", resume="c1")
    assert argv[argv.index("-p") + 1].startswith("t2.md holds a new task")
    assert "saved as r2.md" in argv[argv.index("-p") + 1]


def test_a_codex_follow_up_sets_its_sandbox_again(tmp_path):
    argv = ask.command("codex", "read", "gpt-6-sol", tmp_path, "t.md", tmp_path / "a.md", resume="abc")
    assert argv[:4] == ["codex", "exec", "resume", "abc"]
    assert 'sandbox_mode="read-only"' in argv and "-C" not in argv


def test_reasoning_is_each_harness_own_effort_flag_on_every_run(tmp_path):
    answer = tmp_path / "a.md"
    for resume in (None, "abc"):
        argv = ask.command("codex", "read", "m", tmp_path, "t.md", answer, resume=resume, reasoning="high")
        assert argv[argv.index('model_reasoning_effort="high"') - 1] == "-c" and argv[-1].startswith(("Read", "t.md"))
        for harness in ("claude", "agy"):
            argv = ask.command(harness, "read", "m", tmp_path, "t.md", answer, resume=resume, reasoning="xhigh")
            assert argv[argv.index("--effort") + 1] == "xhigh"
    assert not any("effort" in word for harness in ask.HARNESSES
                   for word in ask.command(harness, "read", "m", tmp_path, "t.md", answer))
    with pytest.raises(ask.Problem, match="no reasoning level"):
        ask.command("claude", "read", "m", tmp_path, "t.md", answer, reasoning="minimal")


def test_a_reasoning_level_the_harness_lacks_exits_2_before_anything_is_written(tmp_path, monkeypatch):
    monkeypatch.setattr(ask, "installed", lambda harness: None)
    (tmp_path / "task.md").write_text("think")
    assert ask.main(["claude", "read", str(tmp_path), "--reasoning", "minimal"]) == 2
    assert sorted(p.name for p in tmp_path.iterdir()) == ["task.md"]


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
    argv = ask.command("claude", "read", "m", tmp_path, "t.md", tmp_path / "a.md")
    assert argv[argv.index("-p") + 1] == ask.ask_text("claude", "read", "t.md", "a.md")
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


def test_an_agy_follow_up_subtracts_the_last_run_of_its_own_conversation(tmp_path):
    (tmp_path / "response1.jsonl").write_text(agy_result(conversation_id="c1", usage={"input_tokens": 100}))
    (tmp_path / "response2.jsonl").write_text(agy_result(conversation_id="c2", usage={"input_tokens": 5}))
    assert ask.session("agy", ask.last_of("agy", tmp_path, 3, "c1")) == "c1"
    assert ask.last_of("agy", tmp_path, 3, "gone") == ""


def fake_harness(monkeypatch, tmp_path):
    monkeypatch.setattr(ask, "reading", lambda stage, note: [])
    monkeypatch.setattr(ask, "installed", lambda harness: None)
    monkeypatch.setattr(ask.calls, "prices", lambda: {})
    monkeypatch.setattr(ask, "TOKENS", tmp_path / "tokens.csv")
    monkeypatch.setattr(ask, "run", lambda argv, cwd, out, err=None: out.write_text(stream(
        {"type": "result", "result": "ok", "session_id": "s9", "usage": {}})) and 0)


def recorded(monkeypatch, tmp_path):
    """fake_harness, keeping each run's argv and folder."""
    fake_harness(monkeypatch, tmp_path)
    seen = []

    def harness(argv, cwd, out, err=None):
        seen.append((argv, cwd))
        out.write_text(stream({"type": "result", "result": "the answer", "session_id": "s9",
                               "usage": {"input_tokens": 1}}))
        return 0
    monkeypatch.setattr(ask, "run", harness)
    return seen


def asked(argv):
    return argv[argv.index("-p") + 1]


def test_a_job_runs_in_the_repository_its_folder_is_in_and_answers_beside_its_task(repo, tmp_path, monkeypatch):
    seen = recorded(monkeypatch, tmp_path)
    folder = repo / "questions" / "why"
    folder.mkdir(parents=True)
    (folder / "task.md").write_text("look around")
    status, note = ask.job("claude", "read", folder, None, "m", "3600s")
    [(argv, cwd)] = seen
    assert (status, note, cwd) == (0, f"claude why [pid {os.getpid()}]", repo)
    assert asked(argv).startswith(f"Read {folder / 'task1.md'} and do what it says.")
    assert "saved as response1.md" in asked(argv)
    assert (folder / "task.md").read_text() == (folder / "task1.md").read_text() == "look around"
    assert (folder / "response1.md").read_text(encoding="utf-8") == "the answer"
    [row] = csv.DictReader((tmp_path / "tokens.csv").open(encoding="utf-8"))
    assert row["repo"] == "project"
    (folder / "task.md").write_text("look again")
    ask.job("claude", "read", folder, "s9", "m", "3600s")
    argv = seen[1][0]
    assert asked(argv).startswith(f"{folder / 'task2.md'} holds a new task") and argv[-2:] == ["--resume", "s9"]
    assert (folder / "task2.md").read_text() == "look again"
    assert (folder / "response2.md").exists() and (folder / "task1.md").read_text() == "look around"


def test_a_folder_in_no_repository_is_where_its_job_runs(tmp_path, monkeypatch):
    seen = recorded(monkeypatch, tmp_path)
    folder = tmp_path / "loose"
    folder.mkdir()
    (folder / "TASK.md").write_text("fix notes.txt")
    assert ask.task_of(folder).name == "TASK.md"
    ask.job("codex", "edit", folder, None, "m", "3600s")
    [(argv, cwd)] = seen
    assert cwd == folder and argv[argv.index("-C") + 1] == str(folder)


def test_an_edit_job_in_a_main_checkout_is_refused_unless_forced_but_not_in_a_worktree(repo, tmp_path, monkeypatch):
    seen = recorded(monkeypatch, tmp_path)
    readings = []
    monkeypatch.setattr(ask, "reading", lambda stage, note: readings.append(stage) or [])
    folder = repo / "q"
    folder.mkdir()
    (folder / "task.md").write_text("change a.txt")
    with pytest.raises(ask.Problem, match="main checkout"):
        ask.job("claude", "edit", folder, None, "m", "3600s")
    assert readings == [] and sorted(p.name for p in folder.iterdir()) == ["task.md"]
    ask.job("claude", "edit", folder, None, "m", "3600s", force=True)
    git("worktree", "add", "-q", str(tmp_path / "wt"), "-b", "wt", cwd=repo)
    other = tmp_path / "wt" / "q"
    other.mkdir()
    (other / "task.md").write_text("change a.txt")
    ask.job("claude", "edit", other, None, "m", "3600s")
    assert [cwd for _, cwd in seen] == [repo, (tmp_path / "wt").resolve()]


def test_lmg_ask_alone_prints_the_guide(capsys):
    assert ask.main([]) == 0
    assert "task.md" in capsys.readouterr().out


def test_usage_mistakes_exit_2_without_running_anything(tmp_path, capsys):
    assert ask.main(["codex", "write", str(tmp_path)]) == 2
    assert ask.main(["nope"]) == 2
    assert ask.main(["review", "HEAD"]) == 2
    assert ask.main(["claude", "read", str(tmp_path / "gone")]) == 2
    assert "no folder" in capsys.readouterr().err
    assert ask.main(["claude", "read", str(tmp_path)]) == 2
    assert "no task.md" in capsys.readouterr().err
    assert list(tmp_path.iterdir()) == []


def test_claude_read_mode_is_reading_the_web_and_mcp_and_no_writing_tool():
    assert ask.flags("claude", "read") == ["--tools", "Read,Grep,Glob,WebSearch,WebFetch,ToolSearch",
                                           "--permission-mode", "bypassPermissions"]


def test_lmg_hands_each_group_the_rest_of_the_line(monkeypatch):
    seen = []
    monkeypatch.setattr(daemon, "main", lambda argv: seen.append(("proxy", argv)) or 0)
    monkeypatch.setattr(ask, "main", lambda argv, command_line: seen.append(("ask", argv, command_line)) or 0)
    monkeypatch.setattr(usage, "main", lambda argv: seen.append(("usage", argv)) or 0)
    monkeypatch.setattr(calls, "main", lambda argv: seen.append(("calls", argv)) or 0)
    monkeypatch.setattr(tasks, "main", lambda argv: seen.append(("tasks", argv)) or 0)
    monkeypatch.setattr(calls, "worth", lambda argv: seen.append(("worth", argv)) or 0)
    assert cli.main(["proxy", "status", "--port", "9"]) == 0
    assert cli.main(["ask", "claude", "read", "q"]) == 0
    assert cli.main(["usage", "before: pi x"]) == 0
    assert cli.main(["usage", "calls", "--hours", "1"]) == 0
    assert cli.main(["usage", "tasks"]) == 0
    assert cli.main(["usage", "worth"]) == 0
    assert seen == [("proxy", ["status", "--port", "9"]), ("ask", ["claude", "read", "q"], False), ("usage", ["before: pi x"]),
                    ("calls", ["--hours", "1"]), ("tasks", []), ("worth", [])]
    assert cli.main(["nope"]) == 2


def test_the_proxy_loads_only_its_own_modules():
    code = ("import sys; from llm_gateway import cli; cli.main(['proxy', 'status', '--port', '1']); "
            "print(sorted(m for m in sys.modules if m.startswith('llm_gateway.')))")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True).stdout
    assert "llm_gateway.daemon" in out and "llm_gateway.tasks" not in out and "llm_gateway.ask" not in out
    assert cli.main([]) == 2


def test_an_agy_run_stopped_before_its_total_books_nothing_and_is_no_baseline(tmp_path):
    stopped = stream({"event": "init", "conversation_id": "c1"})
    said, sid, spent, trouble = ask.read_agy(stopped, agy_result(conversation_id="c1", usage={"input_tokens": 100}))
    assert (sid, spent) == ("c1", None) and trouble.startswith("ended early")
    (tmp_path / "response1.jsonl").write_text(agy_result(conversation_id="c1", usage={"input_tokens": 100}))
    (tmp_path / "response2.jsonl").write_text(stopped)
    assert "100" in ask.last_of("agy", tmp_path, 3, "c1")


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


def test_an_agy_answer_quoting_the_timeout_notice_is_not_a_timeout():
    text = agy_result(conversation_id="c1", response='it checks for "print timeout after" in the output',
                      usage={"input_tokens": 1})
    assert ask.read_agy(text)[3] == ""
    assert ask.read_agy(text + "print timeout after 3600s\n")[3].startswith("timed out")


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
    ask.review("HEAD", repo, "m")
    ask.review("HEAD", repo, "m")
    assert written == ["response1.jsonl", "response2.jsonl"]


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


def test_runs_of_one_job_started_at_once_each_claim_a_number_of_their_own(tmp_path):
    out = []
    threads = [threading.Thread(target=lambda: out.append(ask.claimed(tmp_path))) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sorted(out) == list(range(1, 9))


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
