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
        self.history, self.calls = None, []
        self.lock, self.closed, self.handed, self.reply = asyncio.Lock(), False, [], reply
        FakeSession.made.append(self)

    async def turn(self, emitter, send=None, results=None):
        await asyncio.sleep(0)  # as the real one waits for its child to be ready
        self.handed.append(results if results is not None else send)
        for call_id in list(results or {}):
            gateway.ClaudeSession.paused.pop(call_id, None)
        for e in emitter.text(self.reply) if self.reply else []:
            yield e
        for call_id, name, arguments in self.calls:
            gateway.ClaudeSession.paused[call_id] = self
            for e in emitter.call(call_id, name, arguments, False):
                yield e
        if not self.calls:
            gateway.ClaudeSession.idle[(self.key, self.said, gateway.hashlib.sha256(self.reply.encode()).hexdigest())] = self
        for e in emitter.finish({}):
            yield e

    def close(self):
        self.closed = True
        for table in (gateway.ClaudeSession.paused, gateway.ClaudeSession.idle):
            for key in [key for key, session in table.items() if session is self]:
                del table[key]


@pytest.fixture
def fake(monkeypatch):
    monkeypatch.setattr(gateway, "ClaudeSession", FakeSession)
    monkeypatch.setattr(gateway.ClaudeSession, "paused", {})
    monkeypatch.setattr(gateway.ClaudeSession, "idle", {})
    monkeypatch.setattr(gateway, "SESSIONS", set())
    monkeypatch.setattr(gateway, "CLIENTS", {})
    monkeypatch.setattr(gateway, "log", lambda *parts: None)
    FakeSession.made = []
    return FakeSession


async def drained(body):
    return [e async for e in gateway.claude(body["model"].split("/", 1)[1], body)]


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
    session.history = gateway.history_hash(gateway.entries(body)[1][:-1])

    async def both():
        return await asyncio.gather(drained(body), drained(body), return_exceptions=True)
    outcomes = asyncio.run(both())
    refused = [o for o in outcomes if isinstance(o, gateway.Refused)]
    assert len(refused) == 1 and refused[0].status == 409 and len(session.handed) == 1


@pytest.mark.parametrize("changed", [None, "compaction", "assistant", "result", "system", "tools", "model"])
@pytest.mark.parametrize("parallel", [False, True])
def test_paused_session_resumes_only_unchanged_history(fake, changed, parallel):
    async def conversation():
        await drained(said_to("go"))
        session = fake.made[0]
        session.reply, session.calls = "", [("c1", "f", '{ "x": 1 }')]
        if parallel:
            session.calls.append(("c2", "g", "{}"))
        await drained(said_to("go", "ok", "next"))
        calls = [{"type": "function_call", "call_id": call_id, "name": name,
                  "arguments": arguments.replace(" ", "")} for call_id, name, arguments in session.calls]
        history = said_to("go", "ok", "next")["input"] + calls
        if changed == "compaction":
            history = [{"role": "user", "content": "Compacted summary"}] + calls
        elif changed == "assistant":
            history[1]["content"] = "edited reply"
        elif changed == "result":
            history.insert(1, {"type": "function_call_output", "call_id": "old", "output": "edited result"})
        body = {"model": "anthropic/sonnet", "input": history + [
            {"type": "function_call_output", "call_id": call["call_id"], "output": "done"} for call in calls]}
        if changed == "system":
            body["instructions"] = "new system"
        elif changed == "tools":
            body["tools"] = [{"type": "function", "name": "f", "description": "new description"}]
        elif changed == "model":
            body["model"] = "anthropic/opus"
        session.calls = []
        await drained(body)
        return session, body

    session, body = asyncio.run(conversation())
    if changed is None:
        assert len(fake.made) == 1 and not session.closed
        call_ids = ["c1", "c2"] if parallel else ["c1"]
        assert session.handed[-1] == {call_id: [{"type": "text", "text": "done"}] for call_id in call_ids}
    else:
        assert len(fake.made) == 2 and session.closed
        assert session not in gateway.SESSIONS and not gateway.ClaudeSession.paused
        assert len(session.handed) == 2
        assert fake.made[-1].handed == [gateway.transcript(gateway.entries(body)[1])]


def test_a_message_typed_while_a_tool_ran_goes_back_with_its_result(fake):
    async def conversation():
        await drained(said_to("go"))
        session = fake.made[0]
        session.reply, session.calls = "", [("c1", "f", "{}")]
        await drained(said_to("go", "ok", "next"))
        session.calls = []
        await drained({"model": "anthropic/sonnet", "input": said_to("go", "ok", "next")["input"] + [
            {"type": "function_call", "call_id": "c1", "name": "f", "arguments": "{}"},
            {"type": "function_call_output", "call_id": "c1", "output": "done"},
            {"type": "message", "role": "user", "content": "also this"}]})
        return session

    session = asyncio.run(conversation())
    assert len(fake.made) == 1 and not session.closed
    assert session.handed[-1] == {"c1": [{"type": "text", "text": "done"},
                                          {"type": "text", "text": "The user sent this message while the tool ran:"},
                                          {"type": "text", "text": "also this"}]}


def test_editing_an_earlier_assistant_reply_starts_a_fresh_idle_session(fake):
    asyncio.run(drained(said_to("hello")))
    asyncio.run(drained(said_to("hello", "ok", "next")))
    body = said_to("hello", "edited reply", "next", "ok", "continue")
    asyncio.run(drained(body))
    assert len(fake.made) == 2 and fake.made[0].closed
    assert fake.made[1].handed == [gateway.transcript(gateway.entries(body)[1])]


def of_client(body, client="pi-session", tools=True):
    body = {**body, "prompt_cache_key": client}
    if tools:
        body["tools"] = [{"type": "function", "name": "read", "description": ""}]
    return body


def test_a_compacted_conversation_closes_its_earlier_session(fake):
    asyncio.run(drained(of_client(said_to("hello"))))
    asyncio.run(drained(of_client(said_to("hello", "ok", "next"))))
    asyncio.run(drained(of_client(said_to("Summary of the conversation", "ok", "continue"))))
    old, new = fake.made
    assert old.closed and old not in gateway.SESSIONS and not new.closed


def test_a_compaction_that_rewrites_the_system_still_closes_the_earlier_session(fake):
    asyncio.run(drained(of_client({**said_to("hello"), "instructions": "system with MCP servers"})))
    asyncio.run(drained(of_client({**said_to("Summary", "ok", "continue"), "instructions": "shorter system"})))
    old, new = fake.made
    assert old.closed and old not in gateway.SESSIONS and not new.closed


def test_a_side_request_leaves_the_conversation_alone_and_is_closed_after(fake):
    asyncio.run(drained(of_client(said_to("hello"))))
    asyncio.run(drained(of_client(said_to("Summarize: hello ok"), tools=False)))
    main, summary = fake.made
    assert not main.closed and not summary.closed
    asyncio.run(drained(of_client(said_to("Summary", "ok", "continue"))))
    assert main.closed and summary.closed and not fake.made[2].closed


def test_other_clients_and_unnamed_conversations_keep_their_sessions(fake):
    asyncio.run(drained(of_client(said_to("hello"), client="one")))
    asyncio.run(drained(of_client(said_to("goodbye"), client="two")))
    asyncio.run(drained(said_to("hi")))
    asyncio.run(drained(said_to("hey")))
    assert len(fake.made) == 4 and not any(s.closed for s in fake.made)


def test_the_client_may_name_its_conversation_in_a_header(fake):
    async def named(body):
        gateway.CLIENT_HEADER.set("pi-session")
        return await drained(body)
    tools = [{"type": "function", "name": "read", "description": ""}]
    asyncio.run(named({**said_to("hello"), "tools": tools}))
    asyncio.run(named({**said_to("Summary", "ok", "continue"), "tools": tools}))
    assert fake.made[0].closed and not fake.made[1].closed


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


@pytest.mark.parametrize("stop", ["end_turn", "tool_use"])
@pytest.mark.parametrize("repeated_input", [False, True])
def test_claude_usage_updates_replace_cumulative_counts(monkeypatch, stop, repeated_input):
    monkeypatch.setattr(gateway.ClaudeSession, "idle", {})
    monkeypatch.setattr(gateway, "sent_request", lambda *args: None)

    async def turns():
        session = gateway.ClaudeSession.__new__(gateway.ClaudeSession)
        session.ready, session.closed, session.events = asyncio.Event(), False, asyncio.Queue()
        session.ready.set()
        session.futures, session.sent_system, session.sent_tools = {}, "", []
        session.key, session.said = "key", "said"
        responses = []
        # The first input snapshot comes from the vibegames compaction log: 67,007, not 134,014.
        for counts, output in [({"input_tokens": 2, "cache_creation_input_tokens": 43207,
                                 "cache_read_input_tokens": 23798}, 191), ({"input_tokens": 10}, 0)]:
            raw = [{"type": "message_start", "message": {"usage": {**counts, "output_tokens": 8}}}]
            for total in (output // 2, output):
                raw.append({"type": "message_delta", "delta": {"stop_reason": stop},
                            "usage": {**(counts if repeated_input else {}), "output_tokens": total}})
            raw.append({"type": "message_stop"})
            for event in raw:
                session._take(gateway.StreamEvent(uuid="event", session_id="session", event=event))
            if stop == "end_turn":
                session.events.put_nowait(("done",))
            responses.append([e async for e in session.turn(gateway.Emitter("anthropic/opus"), results={})])
        return responses

    first, second = asyncio.run(turns())
    assert first[-1]["response"]["usage"] == {
        "input_tokens": 67007, "input_tokens_details": {"cached_tokens": 23798},
        "output_tokens": 191, "total_tokens": 67198}
    assert second[-1]["response"]["usage"] == {
        "input_tokens": 10, "input_tokens_details": {"cached_tokens": 0},
        "output_tokens": 0, "total_tokens": 10}
    writer = gateway.ChatWriter("anthropic/opus")
    for event in first:
        writer.take(event)
    assert writer.usage["prompt_tokens"] == 67007
    assert writer.usage["completion_tokens"] == 191
    assert writer.usage["total_tokens"] == 67198


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


def test_a_wildcard_host_is_reached_on_loopback():
    assert daemon.url("0.0.0.0", 8797) == "http://127.0.0.1:8797/v1"
    assert daemon.url("::", 8797) == "http://[::1]:8797/v1"
    assert daemon.url("192.168.1.9", 8797) == "http://192.168.1.9:8797/v1"
    assert [daemon.loopback(h) for h in ("127.0.0.1", "::1", "localhost", "0.0.0.0", "box.lan")] == [True] * 3 + [False] * 2


def test_off_loopback_the_default_key_is_refused(tmp_path, monkeypatch):
    monkeypatch.delenv("GATEWAY_API_KEY", raising=False)
    monkeypatch.setattr(gateway, "WORKDIR", str(tmp_path))
    monkeypatch.setattr(gateway, "api_key", lambda path: gateway.DEFAULT_KEY)
    monkeypatch.setattr(gateway, "configured", lambda path: (None, 0, None))
    for name in ("LOG", "LEVEL", "SOURCE", "PROMPTS"):  # serve sets them; put them back after
        monkeypatch.setattr(gateway, name, getattr(gateway, name))
    with pytest.raises(SystemExit, match="default API key"):
        gateway.serve("0.0.0.0", 1, str(tmp_path / "config.toml"))


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


def test_a_malformed_chat_message_is_refused_as_a_bad_request(monkeypatch):
    async def never(model, body):
        raise AssertionError("a malformed request reached the provider")
        yield
    monkeypatch.setitem(gateway.PROVIDERS, "openai", never)
    monkeypatch.setattr(gateway, "log", lambda *parts: None)
    monkeypatch.setattr(gateway, "record", lambda *a, **k: None)

    async def asked(messages):
        app = web.Application()
        app.add_routes([web.post("/v1/chat/completions", gateway.chat)])
        async with test_utils.TestClient(test_utils.TestServer(app)) as client:
            reply = await client.post("/v1/chat/completions", json={"model": "openai/m", "messages": messages})
            return reply.status, await reply.json()
    for messages in ([{"role": "assistant", "tool_calls": [{"function": {"name": "f"}}]}],
                     [{"role": "tool", "content": "x"}]):
        status, answer = asyncio.run(asked(messages))
        assert status == 400 and "message" in answer["error"]


def test_the_system_prompt_reaches_claude_in_a_file_that_lasts_as_long_as_the_session(monkeypatch, tmp_path):
    seen = {}  # the prompt file each session's child was given -> what it held when the child started

    class Client:
        def __init__(self, options):
            self.path = options.system_prompt["path"]

        async def __aenter__(self):
            seen[self.path] = open(self.path, encoding="utf-8", newline="").read()
            return self

        async def __aexit__(self, *exc):
            return False

        async def receive_messages(self):
            return
            yield

    monkeypatch.setattr(gateway, "ClaudeSDKClient", Client)
    monkeypatch.setattr(gateway, "PROMPTS", tmp_path)
    monkeypatch.setattr(gateway, "log", lambda *parts: None)
    long = "x" * 40000 + "\r\nend"  # past Windows' command-line limit, and its line ending kept

    async def sessions():
        a, b = gateway.ClaudeSession("sonnet", long, [], None), gateway.ClaudeSession("sonnet", "other", [], None)
        await asyncio.gather(a.task, b.task)
        return a, b

    a, b = asyncio.run(sessions())
    assert seen == {str(a.prompt): long, str(b.prompt): "other"}  # two at once, a file each
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("url", ["file:///etc/passwd", "http://127.0.0.1/a.png", "http://10.0.0.5/a.png",
                                 "http://[::1]/a.png", "http://169.254.169.254/latest/meta-data"])
def test_an_image_url_off_the_public_internet_is_refused_before_it_is_fetched(url):
    with pytest.raises(gateway.Refused) as refusal:
        asyncio.run(gateway.downloaded(url))
    assert refusal.value.status == 400


def test_a_name_resolves_to_its_public_addresses_only(monkeypatch):
    found = {"private": ["127.0.0.1", "192.168.1.9"], "mixed": ["10.1.1.1", "93.184.216.34"]}

    async def resolved(self, host, port=0, family=0):
        return [{"hostname": host, "host": a, "port": port, "family": family, "proto": 0, "flags": 0}
                for a in found[host]]
    monkeypatch.setattr(aiohttp.ThreadedResolver, "resolve", resolved)
    resolver = gateway.PublicResolver.__new__(gateway.PublicResolver)
    assert [r["host"] for r in asyncio.run(resolver.resolve("mixed"))] == ["93.184.216.34"]
    with pytest.raises(OSError):
        asyncio.run(resolver.resolve("private"))


def test_an_image_download_is_capped_and_its_redirects_are_checked(monkeypatch):
    monkeypatch.setattr(gateway, "IMAGE_BYTES", 10)
    def image(body):
        async def handler(request):
            return web.Response(body=body, content_type="image/png")
        return handler

    def moved(to):
        async def handler(request):
            raise web.HTTPFound(to)
        return handler

    app = web.Application()
    app.add_routes([web.get("/small", image(b"12345")), web.get("/big", image(b"x" * 11)),
                    web.get("/hop", moved("/small")), web.get("/loop", moved("/loop")),
                    web.get("/inside", moved("http://127.0.0.1/secret"))])

    async def fetch(*paths):
        async with test_utils.TestServer(app) as server:
            # By name, which the checks pass and an ordinary session resolves to the test server.
            gateway.IMAGES = aiohttp.ClientSession()
            try:
                out = []
                for path in paths:
                    try:
                        out.append(await gateway.downloaded(f"http://localhost:{server.port}{path}"))
                    except gateway.Refused as e:
                        out.append(str(e))
                return out
            finally:
                await gateway.IMAGES.close()

    small, big, hop, loop, inside = asyncio.run(fetch("/small", "/big", "/hop", "/loop", "/inside"))
    assert small == hop == ("image/png", "MTIzNDU=")
    assert "over" in big and "redirected more than" in loop and "not on a public address" in inside


def test_a_tool_result_whose_image_fails_leaves_the_session_paused_for_a_retry(monkeypatch):
    async def failing(parts):
        raise gateway.Refused(400, "an image at example.com answered 404")
    monkeypatch.setattr(gateway, "fetched", failing)
    monkeypatch.setattr(gateway.ClaudeSession, "paused", {})

    async def retried():
        session = gateway.ClaudeSession.__new__(gateway.ClaudeSession)
        session.ready, session.closed, session.events = asyncio.Event(), False, asyncio.Queue()
        session.ready.set()
        future = asyncio.get_running_loop().create_future()
        session.futures = {"call_1": future}
        gateway.ClaudeSession.paused["call_1"] = session
        with pytest.raises(gateway.Refused):
            async for _ in session.turn(gateway.Emitter("anthropic/sonnet"), results={"call_1": []}):
                pass
        return session, future

    session, future = asyncio.run(retried())
    assert gateway.ClaudeSession.paused == {"call_1": session} and not future.done()


def test_a_parallel_call_whose_handler_starts_after_the_results_still_gets_its_own(monkeypatch):
    monkeypatch.setattr(gateway.ClaudeSession, "paused", {})
    monkeypatch.setattr(gateway, "sent_request", lambda *args: None)

    async def handled():
        session = gateway.ClaudeSession.__new__(gateway.ClaudeSession)
        session.ready, session.closed, session.events = asyncio.Event(), False, asyncio.Queue()
        session.ready.set()
        session.futures, session.waiting, session.blocks, session.custom = {}, {}, {}, set()
        session.sent_system, session.sent_tools, session.stop, session.names = "", [], None, {"bash"}
        for index, call_id in enumerate(("call_a", "call_b")):
            for event in ({"type": "content_block_start", "index": index,
                           "content_block": {"type": "tool_use", "id": call_id, "name": gateway.PREFIX + "bash"}},
                          {"type": "content_block_stop", "index": index}):
                session._take(gateway.StreamEvent(uuid="event", session_id="session", event=event))
        session.events.put_nowait(("pause",))
        handler = session._tool("bash", "", {}).handler
        # Claude Code runs a reply's calls one at a time: the first handler waits, the second has not started.
        first = asyncio.create_task(handler({}))
        await asyncio.sleep(0)
        results = {call_id: [{"type": "text", "text": call_id}] for call_id in ("call_a", "call_b")}
        async for _ in session.turn(gateway.Emitter("anthropic/opus"), results=results):
            pass
        return await first, await handler({})

    # The second handler used to look its call up in session.futures after turn() had emptied it: KeyError('call_b').
    first, second = asyncio.run(handled())
    assert first == {"content": [{"type": "text", "text": "call_a"}]}
    assert second == {"content": [{"type": "text", "text": "call_b"}]}


@pytest.mark.parametrize("refused", ["bare name", "not JSON"])
def test_a_call_claude_code_refuses_is_not_passed_on_or_given_the_next_calls_result(monkeypatch, refused):
    monkeypatch.setattr(gateway.ClaudeSession, "paused", {})
    monkeypatch.setattr(gateway, "sent_request", lambda *args: None)
    monkeypatch.setattr(gateway, "log", lambda *parts: None)

    async def handled():
        session = gateway.ClaudeSession.__new__(gateway.ClaudeSession)
        session.ready, session.closed, session.events = asyncio.Event(), False, asyncio.Queue()
        session.ready.set()
        session.futures, session.waiting, session.blocks, session.custom = {}, {}, {}, set()
        session.sent_system, session.sent_tools, session.names = "", [], {"bash"}
        # Claude Code answers the refused call itself ("No such tool available", or InputValidationError for
        # arguments that are not JSON), and Claude asks again.
        first = {"bare name": ("bash", "{}"), "not JSON": (gateway.PREFIX + "bash", '{"command": "echo \\`"}')}[refused]
        for call_id, (name, args) in (("call_refused", first), ("call_good", (gateway.PREFIX + "bash", "{}"))):
            for event in ({"type": "message_start", "message": {"usage": {}}},
                          {"type": "content_block_start", "index": 0,
                           "content_block": {"type": "tool_use", "id": call_id, "name": name}},
                          {"type": "content_block_delta", "index": 0,
                           "delta": {"type": "input_json_delta", "partial_json": args}},
                          {"type": "content_block_stop", "index": 0},
                          {"type": "message_delta", "delta": {"stop_reason": "tool_use"}, "usage": {}},
                          {"type": "message_stop"}):
                session._take(gateway.StreamEvent(uuid="event", session_id="session", event=event))
        queued = [session.events.get_nowait()[0:2] for _ in range(session.events.qsize())]
        session.events.put_nowait(("pause",))
        handler = asyncio.create_task(session._tool("bash", "", {}).handler({}))
        await asyncio.sleep(0)
        async for _ in session.turn(gateway.Emitter("anthropic/opus"),
                                    results={"call_good": [{"type": "text", "text": "good"}]}):
            pass
        return queued, await handler

    queued, result = asyncio.run(handled())
    # The refused call reaches neither the client nor a pause; the good one is the only call, and gets its own result.
    assert [e for e in queued if e[0] in ("call", "pause")] == [("call", "call_good"), ("pause",)]
    assert result == {"content": [{"type": "text", "text": "good"}]}


def test_a_transcript_names_each_call_as_claude_must_call_it():
    convo = [("user", [{"type": "text", "text": "go"}]), ("call", "c1", "bash", "{}"),
             ("result", "c1", [{"type": "text", "text": "done"}])]
    text = gateway.transcript(convo)[0]["text"]
    assert f"[assistant called the tool {gateway.PREFIX}bash, call c1]" in text


def test_two_conversations_alike_but_for_an_image_are_two_conversations():
    def with_image(url):
        return [("user", [{"type": "text", "text": "what is this?"}, {"type": "image", "url": url}])]
    assert gateway.said(with_image("data:image/png;base64,AAA")) != gateway.said(with_image("data:image/png;base64,BBB"))


def asked_for_models(monkeypatch, **listers):
    """What GET /v1/models answers, with these providers' listers and nothing cached."""
    monkeypatch.setattr(gateway, "log", lambda *parts: None)
    monkeypatch.setattr(gateway, "LISTED", {})
    monkeypatch.setattr(gateway, "LISTERS", listers)

    async def asked():
        app = web.Application()
        app.add_routes([web.get("/v1/models", gateway.models)])
        async with test_utils.TestClient(test_utils.TestServer(app)) as client:
            reply = await client.get("/v1/models")
            return reply.status, await reply.json()
    return asyncio.run(asked())


def test_the_models_are_what_each_harness_lists(monkeypatch):
    async def anthropic():
        return ["sonnet", "claude-sonnet-5-5"]

    async def openai():
        return ["gpt-6-luna"]
    status, answer = asked_for_models(monkeypatch, anthropic=anthropic, openai=openai)
    assert status == 200
    assert [(m["id"], m["owned_by"]) for m in answer["data"]] == [
        ("anthropic/sonnet", "anthropic"), ("anthropic/claude-sonnet-5-5", "anthropic"), ("openai/gpt-6-luna", "openai")]


def test_a_harness_that_cannot_list_leaves_the_others_models(monkeypatch):
    async def anthropic():
        raise gateway.Refused(502, "no claude")

    async def openai():
        return ["gpt-6-luna"]
    status, answer = asked_for_models(monkeypatch, anthropic=anthropic, openai=openai)
    assert status == 200 and [m["id"] for m in answer["data"]] == ["openai/gpt-6-luna"]


def test_no_models_from_any_harness_is_a_502(monkeypatch):
    async def anthropic():
        raise gateway.Refused(502, "no claude")
    status, answer = asked_for_models(monkeypatch, anthropic=anthropic)
    assert status == 502 and "list its models" in answer["error"]["message"]


def test_a_list_is_kept_until_it_is_old_and_then_outlives_a_failure(monkeypatch):
    calls = []

    async def anthropic():
        calls.append(1)
        if len(calls) > 1:
            raise gateway.Refused(502, "gone")
        return ["sonnet"]
    monkeypatch.setattr(gateway, "log", lambda *parts: None)
    monkeypatch.setattr(gateway, "LISTED", {})
    monkeypatch.setattr(gateway, "LISTERS", {"anthropic": anthropic})
    assert asyncio.run(gateway.listed("anthropic")) == ["sonnet"]
    assert asyncio.run(gateway.listed("anthropic")) == ["sonnet"] and len(calls) == 1
    gateway.LISTED["anthropic"] = (1.0, ["sonnet"])  # asked long ago
    assert asyncio.run(gateway.listed("anthropic")) == ["sonnet"] and len(calls) == 2


def test_proxy_models_prints_each_harness_model_and_exits_1_when_one_cannot_answer(monkeypatch, capsys):
    async def anthropic():
        return ["sonnet"]

    async def openai():
        raise gateway.Refused(401, "no login")
    monkeypatch.setattr(gateway, "LISTERS", {"anthropic": anthropic, "openai": openai})
    assert daemon.models(None) == 1
    seen = capsys.readouterr()
    assert seen.out == "anthropic/sonnet\n" and "openai: Refused: no login" in seen.err
    monkeypatch.setattr(gateway, "LISTERS", {"openai": openai})
    assert daemon.models(None) == 3
