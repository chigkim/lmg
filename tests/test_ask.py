import csv
import json
import subprocess

import pytest

from ask_ai import ask


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


def test_claude_cache_writes_kept_an_hour_are_counted_apart():
    text = json.dumps({"result": "done", "session_id": "s1", "usage": {
        "input_tokens": 5, "cache_read_input_tokens": 50, "cache_creation_input_tokens": 40, "output_tokens": 3,
        "cache_creation": {"ephemeral_1h_input_tokens": 40, "ephemeral_5m_input_tokens": 0}}})
    assert ask.read_claude("a warning first\n" + text) == ("done", "s1", ask.counts(5, 50, 40, 40, 3))


def test_an_agy_follow_up_counts_only_its_own_share():
    before = json.dumps({"usage": {"input_tokens": 100, "cache_read_tokens": 10, "output_tokens": 5}})
    now = "jetski: note\n" + json.dumps({"conversation_id": "c1", "response": "ok", "usage": {
        "input_tokens": 250, "cache_read_tokens": 40, "output_tokens": 12}})
    said, cid, spent, trouble = ask.read_agy(now, before)
    assert (said, cid, trouble) == ("ok", "c1", "")
    assert spent == ask.counts(150, 30, 0, 0, 7)


def test_agy_says_why_a_run_ended_unfinished():
    denied = json.dumps({"response": "", "denied_actions": [{"action": "command", "display_name": "RunCommand"}]})
    assert ask.read_agy(denied)[3].startswith("denied RunCommand")
    assert ask.read_agy('print timeout after 60s\n{"response": "half"}')[3].startswith("timed out")


def test_cost_prices_hour_long_cache_writes_at_their_own_rate():
    table = {"m": {"input": 1, "cache_read": 0.1, "cache_write": 2, "cache_write_1h": 4, "output": 10}}
    spent = ask.counts(1_000_000, 1_000_000, 1_000_000, 250_000, 1_000_000)
    assert ask.cost("m-medium", spent, table) == pytest.approx(1 + 0.1 + 2 * 0.75 + 4 * 0.25 + 10)
    assert ask.cost("unpriced", spent, table) is None


def test_the_token_ledger_row_carries_the_repo(tmp_path, monkeypatch):
    monkeypatch.setattr(ask.plan, "prices", lambda: {})
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
    monkeypatch.setattr(ask.plan, "prices", lambda: {})
    monkeypatch.setattr(ask, "TOKENS", tmp_path / "tokens.csv")
    seen = {}

    def harness(argv, cwd, out, err=None):
        seen.update(argv=argv, cwd=cwd)
        out.write_text(json.dumps({"result": "the answer", "session_id": "s9", "usage": {"input_tokens": 1}}))
        return 0
    monkeypatch.setattr(ask, "run", harness)
    prompt = tmp_path / "look-prompt.md"
    prompt.write_text("look around")
    status, note, _ = ask.job("claude", "read", prompt, None, repo, tmp_path, "claude-opus-5-5", "3600s")
    tree = tmp_path / "worktrees" / "claude-look"
    assert (status, note, seen["cwd"]) == (0, "claude look", tree)
    assert (tree / "TASK.md").read_text() == "look around" and not prompt.exists()
    assert (tmp_path / "runs" / "claude" / "look-task-1.md").read_text() == "look around"
    assert (tmp_path / "runs" / "claude" / "look-1.md").read_text(encoding="utf-8") == "the answer"
    ask.clean("claude", "look", repo, tmp_path)
    assert not tree.exists()


def test_a_folder_of_another_repository_is_not_taken_for_the_job(repo, tmp_path):
    other = tmp_path / "worktrees" / "codex-look"
    other.mkdir(parents=True)
    with pytest.raises(ask.Problem, match="not a worktree"):
        ask.worktree(repo, other, None)
    with pytest.raises(ask.Problem, match="to follow up in"):
        ask.worktree(repo, tmp_path / "worktrees" / "codex-none", "id")


def test_usage_mistakes_exit_2_without_running_anything(repo, capsys):
    assert ask.main(["--repo", str(repo), "codex", "write", "x.md"]) == 2
    assert ask.main(["--repo", str(repo), "nope"]) == 2
    assert ask.main(["--repo", str(repo), "clean", "codex", "gone"]) == 2
    assert "no worktree" in capsys.readouterr().err
