"""Received versus provider-facing logs, without contacting providers."""
import asyncio
import json
import time

import pytest

from llm_gateway import gateway


@pytest.fixture
def logs(monkeypatch):
    lines = []
    monkeypatch.setattr(gateway, "LOG", "unused")
    monkeypatch.setattr(gateway, "LEVEL", gateway.LEVELS.index("debug"))
    monkeypatch.setattr(gateway, "SOURCE", "received")
    monkeypatch.setattr(gateway, "logged", lines.append)
    return lines


def test_received_system_is_separate(logs):
    body = {"model": "anthropic/sonnet", "messages": [
        {"role": "system", "content": "original"}, {"role": "user", "content": "hello"}]}
    gateway.record("chat", body, time.monotonic(), output="reply")
    assert [line["kind"] for line in logs] == ["system", "request"]
    assert logs[0]["system"] == "original"
    assert logs[1]["input"] == body["messages"][-1]
    assert "system" not in logs[1]


def test_sent_uses_adapted_system_and_input(logs, monkeypatch):
    monkeypatch.setattr(gateway, "SOURCE", "sent")
    body = {"model": "anthropic/sonnet", "instructions": "original", "input": "hello"}
    sent = {"system": {"type": "preset", "preset": "claude_code", "append": "adapted"},
            "input": {"role": "user", "content": [{"type": "text", "text": "hello"}]}}
    gateway.record("responses", body, time.monotonic(), sent=sent)
    assert logs[0]["system"] == sent["system"]
    assert logs[1]["input"] == sent["input"]
    assert "original" not in json.dumps(logs)
    assert "system" not in logs[1]


def test_sent_refusal_does_not_fall_back_to_received(logs, monkeypatch):
    monkeypatch.setattr(gateway, "SOURCE", "sent")
    gateway.record("responses", {"instructions": "original", "input": "hello"}, time.monotonic(),
                   sent={}, error="refused")
    assert len(logs) == 1 and logs[0]["input"] is None
    assert "original" not in json.dumps(logs)


def test_long_conversation_has_no_system_entry(logs, monkeypatch):
    monkeypatch.setattr(gateway, "SOURCE", "sent")
    body = {"input": [{"role": "user", "content": "one"}, {"role": "assistant", "content": "two"}]}
    gateway.record("responses", body, time.monotonic(), sent={"system": "custom", "input": "transcript"})
    assert len(logs) == 1 and logs[0]["input"] == "transcript"


def test_received_tools_join_the_system_entry(logs):
    tools = [{"type": "function", "function": {"name": "read", "parameters": {"type": "object"}}}]
    body = {"model": "anthropic/sonnet", "tools": tools, "messages": [
        {"role": "system", "content": "original"}, {"role": "user", "content": "hello"}]}
    gateway.record("chat", body, time.monotonic(), output="reply")
    assert [line["kind"] for line in logs] == ["system", "request"]
    assert logs[0]["tools"] == tools
    assert "tools" not in logs[1]


def test_tools_without_a_system_prompt_still_get_an_entry(logs):
    tools = [{"type": "function", "name": "read"}]
    gateway.record("responses", {"tools": tools, "input": "hello"}, time.monotonic())
    assert logs[0]["kind"] == "system" and logs[0]["system"] is None and logs[0]["tools"] == tools


def test_sent_logs_provider_tools_not_received_ones(logs, monkeypatch):
    monkeypatch.setattr(gateway, "SOURCE", "sent")
    sent_tools = [{"name": "mcp__c__read", "description": "", "input_schema": {"type": "object"}}]
    body = {"instructions": "original", "input": "hello", "tools": [{"type": "function", "name": "read"}]}
    gateway.record("responses", body, time.monotonic(), sent={"system": "s", "input": "i", "tools": sent_tools})
    assert logs[0]["tools"] == sent_tools


def test_long_conversation_has_no_tools_entry(logs):
    body = {"tools": [{"type": "function", "name": "read"}],
            "input": [{"role": "user", "content": "one"}, {"role": "assistant", "content": "two"}]}
    gateway.record("responses", body, time.monotonic())
    assert [line["kind"] for line in logs] == ["request"]


@pytest.mark.parametrize("source", ["received", "sent"])
def test_config_source(tmp_path, source):
    path = tmp_path / "config.toml"
    path.write_text(f'[log]\nfile = "log.jsonl"\nlevel = "debug"\nsource = "{source}"\n')
    assert gateway.configured(path) == (str(tmp_path / "log.jsonl"), 3, source)


def test_config_defaults_and_invalid_source(tmp_path):
    path = tmp_path / "config.toml"
    assert gateway.configured(path) == (None, 0, "received")
    path.write_text('[log]\nfile = "log.jsonl"\n')
    assert gateway.configured(path)[2] == "received"
    path.write_text('[log]\nsource = "invalid"\n')
    with pytest.raises(SystemExit, match="source"):
        gateway.configured(path)


def test_sent_capture_is_request_local_and_traces_only_provider_body(logs, monkeypatch):
    monkeypatch.setattr(gateway, "SOURCE", "sent")
    monkeypatch.setattr(gateway, "LEVEL", 4)

    async def provider(text):
        await asyncio.sleep(0)
        gateway.sent_request({"instructions": text, "input": text}, text, text)
        yield {"type": "response.completed"}

    async def run():
        a, b = {}, {}
        async def drain(text, state):
            return [e async for e in gateway.guarded(provider(text), state)]
        await asyncio.gather(drain("a", a), drain("b", b))
        assert gateway.SENT.get() is None
        return a, b

    a, b = asyncio.run(run())
    assert a == {"input": "a", "system": "a", "tools": None}
    assert b == {"input": "b", "system": "b", "tools": None}
    assert [line["kind"] for line in logs] == ["sent", "sent"]


def test_claude_turn_captures_the_blocks_it_queries(logs, monkeypatch):
    monkeypatch.setattr(gateway, "SOURCE", "sent")
    monkeypatch.setattr(gateway.ClaudeSession, "idle", {})
    seen = []

    class Client:
        async def query(self, messages):
            seen.extend([message async for message in messages])

    async def run():
        session = object.__new__(gateway.ClaudeSession)
        session.ready = asyncio.Event()
        session.ready.set()
        session.closed = False
        session.sent_system = {"preset": "claude_code", "append": "adapted"}
        session.sent_tools = [{"name": "mcp__c__read", "description": "Read", "input_schema": {"type": "object"}}]
        session.client = Client()
        session.events = asyncio.Queue()
        session.events.put_nowait(("done",))
        session.key, session.said = "key", "said"
        state = {}
        blocks = [{"type": "text", "text": "actual transcript"}]
        stream = session.turn(gateway.Emitter("anthropic/sonnet"), send=blocks)
        result = [e async for e in gateway.guarded(stream, state)]
        assert result[-1]["type"] == "response.completed"
        assert state["input"] == seen[0]["message"]
        assert state["system"] == session.sent_system
        assert state["tools"] == session.sent_tools

    asyncio.run(run())


def test_codex_captures_normalized_payload(logs, monkeypatch):
    monkeypatch.setattr(gateway, "SOURCE", "sent")
    monkeypatch.setattr(gateway, "LEVEL", 4)
    monkeypatch.setattr(gateway, "codex_login", lambda: ("secret", "account"))
    monkeypatch.setattr(gateway, "log", lambda *args: None)
    posted = []

    class Reply:
        status = 200

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        @property
        def content(self):
            async def chunks():
                yield b'data: {"type": "response.completed"}\n'
                yield b'\n'
            return chunks()

    class HTTP:
        def post(self, url, json, headers):
            posted.append(json)
            return Reply()

    monkeypatch.setattr(gateway, "HTTP", HTTP())

    async def run():
        state = {}
        tools = [{"type": "function", "name": "read", "parameters": {"type": "object"}}]
        body = {"model": "openai/m", "input": "hello", "temperature": 0.5, "tools": tools}
        stream = gateway.openai("m", body)
        result = [e async for e in gateway.guarded(stream, state)]
        assert result[-1]["type"] == "response.completed"
        assert state["input"] == posted[0]["input"][-1]
        assert state["system"] == "You are a helpful assistant."
        assert state["tools"] == posted[0]["tools"] == tools

    asyncio.run(run())
    assert logs[0]["body"] == posted[0]
    assert "temperature" not in logs[0]["body"]
    assert "secret" not in json.dumps(logs)


def test_fetched_image_data_is_shrunk():
    assert gateway.shrunk({"type": "image", "source": {
        "type": "base64", "media_type": "image/png", "data": "abcdef"}})["source"]["data"] == "<6 chars>"
