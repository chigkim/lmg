import json

import pytest

from llm_gateway import calls

RATES = {"gpt-6-sol": {"input": 2.0, "cache_read": 0.2, "cache_write": 2.5, "cache_write_1h": 2.5, "output": 10.0}}


def call(model, input=0, output=0, time=0.0, name="codex"):
    return calls.Call(time, name, "test", model, input, 0, 0, 0, output)


def test_a_plan_name_is_priced_as_its_api_model():
    assert calls.base("claude-opus-5-5[1m]") == "claude-opus-5-5"
    assert calls.base("gemini-3.8-flash-medium") == "gemini-3.8-flash"


def test_a_long_prompt_is_priced_at_the_long_context_rate():
    assert calls.dollars(call("gpt-6-sol", input=100_000, output=1_000), RATES) == pytest.approx(0.21)
    assert calls.dollars(call("gpt-6-sol", input=300_000, output=1_000), RATES) == pytest.approx(1.215)


def test_an_unknown_model_is_unpriced_not_free():
    assert calls.dollars(call("some-model", input=1_000), RATES) is None
    assert calls.summed([call("some-model", input=1_000)], RATES)[6] == {"some-model"}


def test_protobuf_reads_nested_messages():
    # field 1: a message holding field 2 = 150 (varint), then field 3 = 7
    data = bytes([0x0A, 0x03, 0x10, 0x96, 0x01, 0x18, 0x07])
    message = calls.protobuf(data)
    assert calls.field(message, 1, 2) == 150
    assert calls.field(message, 3) == 7
    assert calls.field(message, 9) is None


def test_a_point_is_sized_by_the_calls_inside_one_window():
    ledger = [{"time": "2026-09-27 10:00", "codex_week_left": "80", "codex_week_reset": "2026-10-03 01:00"},
              {"time": "2026-09-27 12:00", "codex_week_left": "78", "codex_week_reset": "2026-10-03 01:00"},
              # a new reset is a new window: its rise is not a fall, and no call after it counts
              {"time": "2026-09-27 13:00", "codex_week_left": "100", "codex_week_reset": "2026-10-10 01:00"}]
    inside = call("gpt-6-sol", input=100_000, time=calls.stamp("2026-09-27 11:00"))
    after = call("gpt-6-sol", input=100_000, time=calls.stamp("2026-09-27 12:30"))
    points, usd = calls.per_point(ledger, [inside, after], "codex", "week", RATES)
    assert (points, usd) == (2, pytest.approx(0.2))


def test_an_unpriced_call_leaves_a_window_unsized():
    ledger = [{"time": "2026-09-27 10:00", "codex_week_left": "80", "codex_week_reset": "2026-10-03 01:00"},
              {"time": "2026-09-27 12:00", "codex_week_left": "79", "codex_week_reset": "2026-10-03 01:00"}]
    unknown = call("some-model", input=100_000, time=calls.stamp("2026-09-27 11:00"))
    assert calls.per_point(ledger, [unknown], "codex", "week", RATES) == (1, None)


def test_codex_input_is_less_its_cached_reads_and_cache_writes(tmp_path, monkeypatch):
    folder = tmp_path / ".codex/sessions"
    folder.mkdir(parents=True)
    usage = {"input_tokens": 100, "cached_input_tokens": 30, "cache_write_input_tokens": 20, "output_tokens": 5}
    (folder / "rollout-a.jsonl").write_text("\n".join(json.dumps(r) for r in [
        None,
        {"type": "session_meta", "payload": {"model_provider": "openai"}},
        {"type": "turn_context", "payload": {"model": "gpt-6-sol"}},
        {"timestamp": "2026-09-27T10:00:00Z",
         "payload": {"type": "token_count", "info": {"total_token_usage": usage}}}]), encoding="utf-8")
    monkeypatch.setattr(calls, "HOME", tmp_path)
    [c] = calls.codex(0)
    assert (c.input, c.cache_read, c.cache_write, c.output) == (50, 30, 20, 5)


def test_a_pi_provider_on_an_api_key_is_no_plan(tmp_path, monkeypatch):
    (tmp_path / ".pi/agent").mkdir(parents=True)
    (tmp_path / ".pi/agent/auth.json").write_text(json.dumps(
        {"anthropic": {"type": "api_key"}, "openai-codex": {"type": "oauth"}}), encoding="utf-8")
    monkeypatch.setattr(calls, "HOME", tmp_path)
    assert calls.subscribed() == {"openai-codex"}
