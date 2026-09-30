"""The gateway's translation and session routing, with fake sessions and fake events: no provider is asked."""
import asyncio
import os
import urllib.error
import urllib.request

import aiohttp
import pytest
from aiohttp import test_utils, web

from llm_gateway import daemon, gateway, usage


def said_to(*turns):
    """A Responses body whose input alternates user and assistant messages, starting with the user."""
    roles = ("user", "assistant")
    return {"model": "anthropic/sonnet",
            "input": [{"type": "message", "role": roles[i % 2], "content": t} for i, t in enumerate(turns)]}


class FakeSession(gateway.ClaudeSession):
    """A ClaudeSession with no `claude` child: each turn answers `reply` and records what it was handed."""
    made = []

    def __init__(self, model, system, tools, effort, reply="ok"):
        self.model, self.key, self.said = model, gateway.fingerprint(model, system, tools), None
        self.lock, self.closed, self.handed, self.reply = asyncio.Lock(), False, [], reply
        FakeSession.made.append(self)

    async def turn(self, emitter, send=None, results=None):
        await asyncio.sleep(0)  # as the real one waits for its child to be ready
        self.handed.append(results if results is not None else send)
        for call_id in list(results or {}):
            gateway.ClaudeSession.paused.pop(call_id, None)
        for e in emitter.text(self.reply):
            yield e
        gateway.ClaudeSession.idle[(self.key, self.said, gateway.hashlib.sha256(self.reply.encode()).hexdigest())] = self
        for e in emitter.finish({}):
            yield e


@pytest.fixture
def fake(monkeypatch):
    monkeypatch.setattr(gateway, "ClaudeSession", FakeSession)
    monkeypatch.setattr(gateway.ClaudeSession, "paused", {})
    monkeypatch.setattr(gateway.ClaudeSession, "idle", {})
    monkeypatch.setattr(gateway, "SESSIONS", set())
    monkeypatch.setattr(gateway, "log", lambda *parts: None)
    FakeSession.made = []
    return FakeSession


async def drained(body):
    return [e async for e in gateway.claude("sonnet", body)]


def test_a_conversation_continues_its_own_session(fake):
    asyncio.run(drained(said_to("hello")))
    asyncio.run(drained(said_to("hello", "ok", "and then?")))
    assert len(fake.made) == 1


def test_two_conversations_given_the_same_reply_keep_their_own_sessions(fake):
    asyncio.run(drained(said_to("hello")))
    asyncio.run(drained(said_to("goodbye")))
    asyncio.run(drained(said_to("hello", "ok", "and then?")))
    first, second = fake.made[:2]
    assert len(fake.made) == 2 and len(first.handed) == 2 and len(second.handed) == 1


def test_tool_results_are_handed_back_once(fake):
    session = FakeSession("sonnet", "", [], None)
    gateway.ClaudeSession.paused["c1"] = session
    body = {"model": "anthropic/sonnet", "input": [
        {"type": "message", "role": "user", "content": "go"},
        {"type": "function_call", "call_id": "c1", "name": "f", "arguments": "{}"},
        {"type": "function_call_output", "call_id": "c1", "output": "done"}]}

    async def both():
        return await asyncio.gather(drained(body), drained(body), return_exceptions=True)
    outcomes = asyncio.run(both())
    refused = [o for o in outcomes if isinstance(o, gateway.Refused)]
    assert len(refused) == 1 and refused[0].status == 409 and len(session.handed) == 1


def test_interleaved_tool_call_arguments_stay_with_their_call():
    writer = gateway.ChatWriter("m")
    for n in ("a", "b"):
        writer.take({"type": "response.output_item.added",
                     "item": {"type": "function_call", "id": f"fc_{n}", "call_id": f"call_{n}", "name": n}})
    chunks = []
    for item, delta in (("fc_a", '{"x":'), ("fc_b", '{"y":'), ("fc_a", "1}"), ("fc_b", "2}")):
        chunks += writer.take({"type": "response.function_call_arguments.delta", "item_id": item, "delta": delta})
    assert [c["function"]["arguments"] for c in writer.calls] == ['{"x":1}', '{"y":2}']
    assert [c["choices"][0]["delta"]["tool_calls"][0]["index"] for c in chunks] == [0, 1, 0, 1]


class Body:
    def __init__(self, parsed):
        self.parsed = parsed

    async def json(self):
        if isinstance(self.parsed, Exception):
            raise self.parsed
        return self.parsed


@pytest.mark.parametrize("parsed, said", [(ValueError("bad"), "not JSON"), ([1, 2], "not a JSON object")])
def test_a_body_that_is_not_a_json_object_is_a_400(parsed, said):
    with pytest.raises(gateway.Refused) as refusal:
        asyncio.run(gateway.json_body(Body(parsed)))
    assert refusal.value.status == 400 and said in str(refusal.value)


async def events(*kinds):
    for kind in kinds:
        yield {"type": kind}


async def chained(*kinds):
    stream = events(*kinds)
    return [e["type"] async for e in gateway._chain(await stream.__anext__(), stream)]


def test_a_stream_that_says_it_finished_is_passed_through():
    assert asyncio.run(chained("response.created", "response.completed")) == ["response.created", "response.completed"]


def test_a_stream_that_stops_before_it_finished_is_a_502():
    with pytest.raises(gateway.Refused) as refusal:
        asyncio.run(chained("response.created", "response.output_text.delta"))
    assert refusal.value.status == 502


def test_the_usage_request_follows_no_redirect(monkeypatch):
    # The redirect handler is asked for the next request and must give none, so the Authorization header goes nowhere.
    handler = next(h for h in usage.OPENER.handlers if isinstance(h, urllib.request.HTTPRedirectHandler))
    request = urllib.request.Request(usage.USAGE, headers={"Authorization": "Bearer secret"})
    assert handler.redirect_request(request, None, 302, "Found", {}, "https://elsewhere.example/") is None


def test_a_pid_file_whose_pid_started_later_is_stale(tmp_path, monkeypatch):
    monkeypatch.setattr(daemon, "STATE", tmp_path)
    daemon.pid_file(1).write_text(f"{os.getpid()} {daemon.born(os.getpid())}")
    assert daemon.running(1) == os.getpid()
    daemon.pid_file(1).write_text(f"{os.getpid()} 1")
    assert daemon.running(1) is None and not daemon.pid_file(1).exists()


def test_a_start_time_that_cannot_be_read_is_no_match(tmp_path, monkeypatch):
    monkeypatch.setattr(daemon, "STATE", tmp_path)
    monkeypatch.setattr(daemon, "born", lambda pid: None)
    daemon.pid_file(1).write_text(f"{os.getpid()} None")
    assert daemon.running(1) is None


async def raising(error):
    raise error
    yield


@pytest.mark.parametrize("error, status", [(aiohttp.ClientConnectionError(), 502), (AttributeError("x"), 500),
                                           (gateway.Refused(409, "taken"), 409)])
def test_a_provider_failure_is_a_refusal(monkeypatch, error, status):
    monkeypatch.setattr(gateway, "log", lambda *parts: None)
    with pytest.raises(gateway.Refused) as refusal:
        asyncio.run(gateway.first(gateway.guarded(raising(error))))
    assert refusal.value.status == status


def test_a_model_that_is_not_a_string_is_a_400():
    with pytest.raises(gateway.Refused) as refusal:
        gateway.backend({"model": 7})
    assert refusal.value.status == 400


def test_a_failed_reply_asked_for_whole_is_an_http_error(monkeypatch):
    async def failing(model, body):
        yield {"type": "response.created", "response": {"status": "in_progress"}}
        yield {"type": "response.failed", "response": {"status": "failed", "error": {"message": "over quota"}}}
    monkeypatch.setitem(gateway.PROVIDERS, "openai", failing)
    monkeypatch.setattr(gateway, "log", lambda *parts: None)

    async def asked():
        app = web.Application()
        app.add_routes([web.post("/v1/responses", gateway.responses)])
        async with test_utils.TestClient(test_utils.TestServer(app)) as client:
            reply = await client.post("/v1/responses", json={"model": "openai/m", "input": "hi"})
            return reply.status, await reply.json()
    status, answer = asyncio.run(asked())
    assert status == 502 and answer["error"]["message"] == "over quota"
