import pytest

from ai_usage import plan

RATES = {"gpt-6-sol": {"input": 2.0, "cache_read": 0.2, "cache_write": 2.5, "cache_write_1h": 2.5, "output": 10.0}}


def call(model, input=0, output=0, time=0.0, name="codex"):
    return plan.Call(time, name, "test", model, input, 0, 0, 0, output)


def test_a_plan_name_is_priced_as_its_api_model():
    assert plan.base("claude-opus-5-5[1m]") == "claude-opus-5-5"
    assert plan.base("gemini-3.8-flash-medium") == "gemini-3.8-flash"


def test_a_long_prompt_is_priced_at_the_long_context_rate():
    assert plan.dollars(call("gpt-6-sol", input=100_000, output=1_000), RATES) == pytest.approx(0.21)
    assert plan.dollars(call("gpt-6-sol", input=300_000, output=1_000), RATES) == pytest.approx(1.215)


def test_an_unknown_model_is_unpriced_not_free():
    assert plan.dollars(call("some-model", input=1_000), RATES) is None
    assert plan.summed([call("some-model", input=1_000)], RATES)[6] == {"some-model"}


def test_protobuf_reads_nested_messages():
    # field 1: a message holding field 2 = 150 (varint), then field 3 = 7
    data = bytes([0x0A, 0x03, 0x10, 0x96, 0x01, 0x18, 0x07])
    message = plan.protobuf(data)
    assert plan.field(message, 1, 2) == 150
    assert plan.field(message, 3) == 7
    assert plan.field(message, 9) is None


def test_a_point_is_sized_by_the_calls_inside_one_window():
    ledger = [{"time": "2026-09-27 10:00", "codex_week_left": "80", "codex_week_reset": "2026-10-03 01:00"},
              {"time": "2026-09-27 12:00", "codex_week_left": "78", "codex_week_reset": "2026-10-03 01:00"},
              # a new reset is a new window: its rise is not a fall, and no call after it counts
              {"time": "2026-09-27 13:00", "codex_week_left": "100", "codex_week_reset": "2026-10-10 01:00"}]
    inside = call("gpt-6-sol", input=100_000, time=plan.stamp("2026-09-27 11:00"))
    after = call("gpt-6-sol", input=100_000, time=plan.stamp("2026-09-27 12:30"))
    points, usd = plan.per_point(ledger, [inside, after], "codex", "week", RATES)
    assert (points, usd) == (2, pytest.approx(0.2))
