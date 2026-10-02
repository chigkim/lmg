"""Conversation log files for both API front doors, using a fake provider."""
import asyncio
import json
import re

import pytest
from aiohttp import test_utils, web

from llm_gateway import gateway


@pytest.fixture
def conversation_logs(tmp_path, monkeypatch):
    monkeypatch.setattr(gateway, "LOG", str(tmp_path / "{session}.jsonl"))
    monkeypatch.setattr(gateway, "LEVEL", 4)
    monkeypatch.setattr(gateway, "SOURCE", "received")
    monkeypatch.setattr(gateway, "LOG_FILES", {})
    monkeypatch.setattr(gateway, "LOG_HISTORY", {})
    monkeypatch.setattr(gateway, "log", lambda *args: None)

    async def provider(model, body):
        gateway.bind_log()
        await asyncio.sleep(0)
        emitter = gateway.Emitter("openai/m")
        for event in emitter.start() + emitter.text("reply") + emitter.finish({}):
            yield event

    monkeypatch.setitem(gateway.PROVIDERS, "openai", provider)
    return tmp_path


async def with_client(action):
    app = web.Application()
    app.add_routes([web.post("/v1/responses", gateway.responses),
                    web.post("/v1/chat/completions", gateway.chat)])
    async with test_utils.TestClient(test_utils.TestServer(app)) as client:
        await action(client)
    assert gateway.LOG_CONTEXT.get() is None


def lines(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


@pytest.mark.parametrize("api", ["responses", "chat"])
def test_history_continues_one_file_and_new_conversation_gets_another(conversation_logs, api):
    async def ask(client):
        path = "/v1/responses" if api == "responses" else "/v1/chat/completions"
        field = "input" if api == "responses" else "messages"
        history = [{"role": "user", "content": "hello"}]
        for messages in (history, history + [{"role": "assistant", "content": "reply"},
                                           {"role": "user", "content": "next"}],
                         [{"role": "user", "content": "different conversation"}]):
            response = await client.post(path, json={"model": "openai/m", field: messages})
            assert response.status == 200
            await response.json()

    asyncio.run(with_client(ask))
    files = list(conversation_logs.glob("*.jsonl"))
    assert len(files) == 2
    assert sorted(sum(line["kind"] == "request" for line in lines(file)) for file in files) == [1, 2]
    for file in files:
        assert re.match(r"\d{2}-\d{2}-\d{2} \d{2}-\d{2}-\d{2} session_[a-f0-9]+\.jsonl", file.name)
        assert len({line["session_id"] for line in lines(file)}) == 1
        assert any(line["kind"] in ("responses", "chat") for line in lines(file))
        assert any(line["kind"] == "event" for line in lines(file))


def test_explicit_ids_isolate_concurrent_identical_inputs_and_reuse_files(conversation_logs):
    async def ask(client):
        async def send(session):
            response = await client.post("/v1/responses", headers={"X-Session-ID": session},
                                         json={"model": "openai/m", "input": "identical"})
            assert response.status == 200
            await response.json()
        await asyncio.gather(send("one"), send("two"))
        await send("one")

    asyncio.run(with_client(ask))
    files = list(conversation_logs.glob("*.jsonl"))
    assert len(files) == 2
    for file in files:
        entries = lines(file)
        session = entries[0]["session_id"]
        assert {line["session_id"] for line in entries} == {session}
        assert sum(line["kind"] == "request" for line in entries) == (2 if session == "one" else 1)


@pytest.mark.parametrize("api", ["responses", "chat"])
def test_metadata_session_id_is_filename_safe(conversation_logs, api):
    async def ask(client):
        field = "input" if api == "responses" else "messages"
        path = "/v1/responses" if api == "responses" else "/v1/chat/completions"
        body = {"model": "openai/m", field: [{"role": "user", "content": "hello"}],
                "metadata": {"session_id": "../../unsafe:session"}}
        for _ in range(2):
            response = await client.post(path, json=body)
            assert response.status == 200
            await response.json()

    asyncio.run(with_client(ask))
    files = list(conversation_logs.glob("*.jsonl"))
    assert len(files) == 1 and ":" not in files[0].name
    assert sum(line["kind"] == "request" for line in lines(files[0])) == 2


def test_refusal_flushes_received_trace_and_error_to_same_file(conversation_logs):
    async def ask(client):
        response = await client.post("/v1/responses", json={"model": "unknown/m", "input": "hello"})
        assert response.status == 400
        await response.json()

    asyncio.run(with_client(ask))
    files = list(conversation_logs.glob("*.jsonl"))
    assert len(files) == 1
    assert [line["kind"] for line in lines(files[0])] == ["responses", "request"]


def test_ambiguous_identical_histories_do_not_merge(conversation_logs):
    async def ask(client):
        for _ in range(2):
            response = await client.post("/v1/responses", json={"model": "openai/m", "input": "hello"})
            await response.json()
        response = await client.post("/v1/responses", json={"model": "openai/m", "input": [
            {"role": "user", "content": "hello"}, {"role": "assistant", "content": "reply"},
            {"role": "user", "content": "next"}]})
        await response.json()

    asyncio.run(with_client(ask))
    assert len(list(conversation_logs.glob("*.jsonl"))) == 3


def test_fixed_log_filename_keeps_combined_mode(conversation_logs, monkeypatch):
    monkeypatch.setattr(gateway, "LOG", str(conversation_logs / "combined.jsonl"))
    async def ask(client):
        for text in ("one", "two"):
            response = await client.post("/v1/responses", json={"model": "openai/m", "input": text})
            await response.json()
    asyncio.run(with_client(ask))
    files = list(conversation_logs.glob("*.jsonl"))
    assert len(files) == 1 and files[0].name == "combined.jsonl"
    assert sum(line["kind"] == "request" for line in lines(files[0])) == 2
