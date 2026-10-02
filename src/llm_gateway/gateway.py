"""A small LLM gateway on loopback: OpenAI's Responses and Chat Completions APIs in, subscription plans out.

    uv run llm-gateway [--port 8797]

A model is named provider/model, as in LiteLLM:

    anthropic/MODEL   Claude, through the Claude Agent SDK on the Claude Code login (`claude` must be logged in).
                      MODEL is anything `claude --model` takes: sonnet, opus, haiku, claude-opus-5-5, ...
    openai/MODEL      OpenAI, through the ChatGPT Codex backend on the Codex login in ~/.codex/auth.json.

Both front doors speak one internal language, the Responses API: a Chat Completions request is translated into a
Responses request, and the Responses events that come back are translated into chat chunks.

The Claude Agent SDK runs its own agent loop and expects to run the tools itself, where these APIs hand a tool call back
to the client. So the client's tools are registered as an in-process MCP server whose handlers do not run anything:
a tool call ends the HTTP response with a function_call, the handler waits, and the request that brings back a
function_call_output for that call id hands the output to it and lets Claude carry on. A conversation the gateway has
no live session for (a restart, a client that edited its history) is replayed to a fresh session as a transcript.

Clients authenticate with `Authorization: Bearer KEY`, KEY being GATEWAY_API_KEY from the environment or from a .env
file found from the working directory upward. With neither, it writes a new random key to .env in the working directory
and uses that: a keyless gateway would let any process on the machine, or a web page in its browser, spend both plans.

Credentials: the Codex token is read from auth.json on every request and never refreshed, written, logged or printed.
Codex rotates its refresh token, so a second refresher could log Codex out; when the token expires, run any `codex`
command and retry. Claude's login is never touched at all: the SDK's `claude` child uses its own.
"""
import asyncio
import base64
import contextvars
import functools
import hashlib
import hmac
import ipaddress
import json
import os
import pathlib
import re
import secrets
import shutil
import socket
import sys
import tempfile
import time
import tomllib
import traceback
import urllib.parse
import uuid

import aiohttp
from aiohttp import web
from dotenv import dotenv_values, find_dotenv
from claude_agent_sdk import (ClaudeAgentOptions, ClaudeSDKClient, ResultMessage, SdkMcpTool, StreamEvent,
                              create_sdk_mcp_server)

from .pi_prompt import adapt_pi_prompt

SERVER = "c"                 # the SDK names a tool mcp__SERVER__NAME
PREFIX = f"mcp__{SERVER}__"
IDLE = 30 * 60               # seconds a Claude session is kept with nobody using it
CODEX_URL = "https://chatgpt.com/backend-api/codex/responses"
CODEX_MODELS_URL = "https://chatgpt.com/backend-api/codex/models"
LISTED_FOR = 5 * 60          # seconds a provider's list of models is kept before it is asked again
IMAGE_BYTES = 20 * 1024 * 1024  # the most an image URL may download
REDIRECTS = 5                   # how many an image URL may take before it is refused
# Fields the Codex backend accepts; it refuses others (temperature, max_output_tokens, ...).
CODEX_FIELDS = {"model", "instructions", "input", "tools", "tool_choice", "parallel_tool_calls", "reasoning", "text",
                "include", "prompt_cache_key"}
# How much config.toml's [log] file gets, each level holding everything below it:
#   error  a request that was refused or failed, and why
#   info   every request: model, timing, usage
#   debug  and its input and reply, plus a separate system and tools entry for a lone message
#   trace  and the whole request body and every event the model sent
LEVELS = ("off", "error", "info", "debug", "trace")
LOG, LEVEL, SOURCE = None, 0, "received"  # configured at startup
SENT = contextvars.ContextVar("sent_request", default=None)
LOG_CONTEXT = contextvars.ContextVar("conversation_log", default=None)
LOG_FILES, LOG_HISTORY = {}, {}  # bounded, process-local conversation lookup


class Refused(Exception):
    """A request the gateway answers with an HTTP error and a plain message."""

    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


def log(*parts):
    print(time.strftime("%H:%M:%S"), *parts, file=sys.stderr, flush=True)


def bounded_put(cache, key, value):
    cache[key] = value
    if len(cache) > 4096:
        del cache[next(iter(cache))]


def conversation_log(handler):
    """Keep concurrent HTTP requests' log destinations separate, including trace lines."""
    @functools.wraps(handler)
    async def wrapped(request):
        if not LOG or "{session}" not in LOG:
            return await handler(request)
        client_id = request.headers.get("X-Session-ID") or request.headers.get("Session-ID")
        state = {"client_id": client_id, "session": None, "pending": []}
        token = LOG_CONTEXT.set(state)
        try:
            return await handler(request)
        finally:
            try:
                bind_log()
            finally:
                LOG_CONTEXT.reset(token)
    return wrapped


def bind_log(session_id=None):
    """Select a conversation file and flush any trace received before routing was known."""
    state = LOG_CONTEXT.get()
    if state is None or state["session"] is not None:
        return
    session_id = str(state.get("client_id") or state.get("inferred") or session_id or new_id("session"))
    if session_id not in LOG_FILES:
        safe = re.sub(r"[^a-zA-Z0-9_.-]", "_", session_id)[:80]
        if safe != session_id:
            safe += "-" + hashlib.sha256(session_id.encode()).hexdigest()[:12]
        bounded_put(LOG_FILES, session_id, time.strftime("%y-%m-%d %H-%M-%S") + " " + safe)
    state["session"] = session_id
    state["file"] = LOG.replace("{session}", LOG_FILES[session_id])
    pending, state["pending"] = state["pending"], []
    for line in pending:
        logged(line)


def log_history_key(scope, convo):
    return hashlib.sha256(json.dumps([scope, convo], sort_keys=True).encode()).hexdigest()


def identify_log(body):
    """Prefer a client session ID; otherwise match a known conversation-history prefix."""
    state = LOG_CONTEXT.get()
    if state is None:
        return
    metadata = body.get("metadata")
    client_id = metadata.get("session_id") if isinstance(metadata, dict) else None
    state["client_id"] = state["client_id"] or client_id
    try:
        system, convo = entries(body)
    except (KeyError, TypeError, AttributeError, ValueError):
        return  # Logging must not change how the provider handles malformed input.
    scope = fingerprint(body.get("model"), system, body.get("tools"))
    state.update(scope=scope, convo=convo)
    if not state["client_id"]:
        for n in range(len(convo) - 1, 0, -1):
            known = LOG_HISTORY.get(log_history_key(scope, convo[:n]))
            if known:
                state["inferred"] = known
                break


def remember_log(api, output):
    state = LOG_CONTEXT.get()
    if state is None or "convo" not in state or not output:
        return
    bind_log()
    try:
        if api == "chat":
            output = chat_to_responses({"messages": [output]}).get("input", [])
        _, reply = entries({"input": output})
    except (Refused, KeyError, TypeError, AttributeError, ValueError):
        return
    key = log_history_key(state["scope"], state["convo"] + reply)
    session = state["session"]
    # Indistinguishable histories must not silently merge two independent conversations.
    if key in LOG_HISTORY and LOG_HISTORY[key] != session:
        session = None
    bounded_put(LOG_HISTORY, key, session)


def logged(line):
    state = LOG_CONTEXT.get()
    if state is not None and state["session"] is None:
        state["pending"].append(dict(time=time.strftime("%Y-%m-%dT%H:%M:%S%z"), **line))
        return
    file = state["file"] if state is not None else LOG
    if "{session}" in file:
        file = file.replace("{session}", time.strftime("%y-%m-%d %H-%M-%S") + " " + new_id("session"))
    line = dict(line)
    line.setdefault("time", time.strftime("%Y-%m-%dT%H:%M:%S%z"))
    if state is not None:
        line["session_id"] = state["session"]
    with open(file, "a", encoding="utf-8") as f:
        f.write(json.dumps(shrunk(line), ensure_ascii=False) + "\n")


def trace(kind, obj):
    """At trace, one line for a request body or an event, as it came."""
    if LOG and LEVEL >= LEVELS.index("trace"):
        logged({"kind": kind, "body": obj})


def shrunk(obj):
    """obj with every data URL cut to its type and size, so an image does not fill the log."""
    if isinstance(obj, dict):
        return {k: f"<{len(v)} chars>" if k == "data" and isinstance(v, str)
                and obj.get("type") in ("base64", "image") else shrunk(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [shrunk(v) for v in obj]
    if isinstance(obj, str) and obj.startswith("data:") and "," in obj:
        head, data = obj.split(",", 1)
        return f"{head},<{len(data)} chars>"
    return obj


def last_input(body, api):
    """The request's last input message, as the client sent it."""
    items = body.get("messages") if api == "chat" else body.get("input")
    return items[-1] if isinstance(items, list) and items else items


def lone_system(body, api):
    """The system prompt of a request whose conversation is a single message, or None for a longer one."""
    items = body.get("messages") if api == "chat" else body.get("input")
    if isinstance(items, str):
        items = [{"role": "user", "content": items}]
    items = [i for i in items or [] if isinstance(i, dict)]
    system = [i for i in items if i.get("role") in ("system", "developer")]
    if len(items) - len(system) != 1:
        return None
    texts = [body.get("instructions")] if api == "responses" else []
    texts += [plain(parts_of(i.get("content"))) for i in system]
    return "\n\n".join(t for t in texts if t) or None


def sent_request(body, input, system=None, tools=None):
    """Capture provider-facing content, never credentials or temporary prompt paths."""
    state = SENT.get()
    if state is not None and SOURCE == "sent":
        state.update(input=input, system=system, tools=tools)
        trace("sent", body)


def record(api, body, started, output=None, usage=None, error=None, sent=None):
    """One line for a finished request, holding as much as the level asks for."""
    if not LOG or LEVEL < LEVELS.index("info" if error is None else "error"):
        return
    bind_log()
    remember_log(api, output)
    line = {"kind": "request", "api": api, "model": body.get("model"), "stream": bool(body.get("stream")),
            "seconds": round(time.monotonic() - started, 2), "usage": usage}
    if error:
        line["error"] = error
    if LEVEL >= LEVELS.index("debug"):
        items = body.get("messages") if api == "chat" else body.get("input")
        if isinstance(items, str):
            items = [{"role": "user", "content": items}]
        conversation = [i for i in items or [] if isinstance(i, dict)
                        and i.get("role") not in ("system", "developer")]
        lone = len(conversation) == 1
        if SOURCE == "sent":
            line["source"] = "sent"
            line["input"] = (sent or {}).get("input")
            system = (sent or {}).get("system") if lone else None
            tools = (sent or {}).get("tools") if lone else None
        else:
            system = lone_system(body, api)
            tools = body.get("tools") if lone else None
            line["input"] = last_input(body, api)
        if system or tools:
            entry = {"kind": "system", "api": api, "model": body.get("model"), "source": SOURCE, "system": system}
            if tools:
                entry["tools"] = tools
            logged(entry)
        line["output"] = output
    logged(line)


def configured(path):
    """Return the log file, level, and received/sent source from config.toml."""
    path = pathlib.Path(path)
    if not path.exists():
        return None, 0, "received"
    section = tomllib.loads(path.read_text(encoding="utf-8")).get("log", {})
    level = section.get("level", "info")
    if level not in LEVELS:
        sys.exit(f"{path}: [log] level is {level!r}; use one of {', '.join(LEVELS)}")
    source = section.get("source", "received")
    if source not in ("received", "sent"):
        sys.exit(f"{path}: [log] source is {source!r}; use received or sent")
    name = section.get("file")
    if not name or level == "off":
        return None, 0, source
    file = path.parent / name
    file.parent.mkdir(parents=True, exist_ok=True)
    return str(file), LEVELS.index(level), source


def new_id(prefix):
    return f"{prefix}_{uuid.uuid4().hex[:24]}"


# ---------------------------------------------------------------- Responses events, built by the Claude side

class Emitter:
    """Builds the Responses stream events for one response, and the response object they add up to."""

    def __init__(self, model):
        self.id, self.model, self.created = new_id("resp"), model, int(time.time())
        self.output, self.open = [], None  # `open` is the message item whose text is still streaming

    def response(self, status, usage=None):
        return {"id": self.id, "object": "response", "created_at": self.created, "status": status,
                "model": self.model, "output": self.output, "usage": usage}

    def start(self):
        return [{"type": "response.created", "response": self.response("in_progress")}]

    def text(self, delta):
        events = []
        if self.open is None:
            self.open = {"type": "message", "id": new_id("msg"), "status": "in_progress", "role": "assistant",
                         "content": [{"type": "output_text", "text": "", "annotations": []}]}
            index = len(self.output)
            events += [{"type": "response.output_item.added", "output_index": index, "item": dict(self.open, content=[])},
                       {"type": "response.content_part.added", "item_id": self.open["id"], "output_index": index,
                        "content_index": 0, "part": {"type": "output_text", "text": "", "annotations": []}}]
        self.open["content"][0]["text"] += delta
        events.append({"type": "response.output_text.delta", "item_id": self.open["id"],
                       "output_index": len(self.output), "content_index": 0, "delta": delta})
        return events

    def close(self):
        if self.open is None:
            return []
        item, index, self.open = self.open, len(self.output), None
        item["status"] = "completed"
        part = item["content"][0]
        self.output.append(item)
        return [{"type": "response.output_text.done", "item_id": item["id"], "output_index": index, "content_index": 0,
                 "text": part["text"]},
                {"type": "response.content_part.done", "item_id": item["id"], "output_index": index,
                 "content_index": 0, "part": part},
                {"type": "response.output_item.done", "output_index": index, "item": item}]

    def call(self, call_id, name, arguments, custom):
        events, index = self.close(), len(self.output)
        if custom:
            item = {"type": "custom_tool_call", "id": new_id("ctc"), "status": "completed", "call_id": call_id,
                    "name": name, "input": arguments}
            events += [{"type": "response.output_item.added", "output_index": index, "item": dict(item, input="")},
                       {"type": "response.custom_tool_call_input.delta", "item_id": item["id"], "output_index": index,
                        "delta": arguments},
                       {"type": "response.custom_tool_call_input.done", "item_id": item["id"], "output_index": index,
                        "input": arguments}]
        else:
            item = {"type": "function_call", "id": new_id("fc"), "status": "completed", "call_id": call_id,
                    "name": name, "arguments": arguments}
            events += [{"type": "response.output_item.added", "output_index": index, "item": dict(item, arguments="")},
                       {"type": "response.function_call_arguments.delta", "item_id": item["id"],
                        "output_index": index, "delta": arguments},
                       {"type": "response.function_call_arguments.done", "item_id": item["id"],
                        "output_index": index, "arguments": arguments}]
        self.output.append(item)
        return events + [{"type": "response.output_item.done", "output_index": index, "item": item}]

    def finish(self, usage, status="completed"):
        return self.close() + [{"type": f"response.{status}", "response": self.response(status, usage)}]


# ---------------------------------------------------------------- reading a Responses request

def text_of(content):
    """The text of a message's content, a string or a list of parts."""
    if isinstance(content, str):
        return content
    return "\n".join(p["text"] for p in content or [] if isinstance(p.get("text"), str))


def parts_of(content):
    """A message's content as Anthropic blocks: text, and images still named by their URL."""
    if isinstance(content, str):
        return [{"type": "text", "text": content}] if content else []
    out = []
    for part in content or []:
        if isinstance(part.get("text"), str):
            out.append({"type": "text", "text": part["text"]})
        elif part.get("type") == "input_image":
            url = part.get("image_url")
            url = url.get("url") if isinstance(url, dict) else url
            if url:
                out.append({"type": "image", "url": url})
            else:
                out.append({"type": "text", "text": "[an image sent by file id, which this gateway cannot fetch]"})
    return out


def plain(parts):
    return "\n".join(p["text"] for p in parts if p["type"] == "text")


async def fetched(parts):
    """The same blocks with every image as base64 data, which is what Claude Code passes on."""
    out = []
    for p in parts:
        if p["type"] != "image" or "source" in p:
            out.append(p)
            continue
        url = p["url"]
        if url.startswith("data:"):
            head, _, data = url.partition(",")
            media = head[5:].split(";")[0] or "image/png"
        else:
            media, data = await downloaded(url)
        out.append({"type": "image", "source": {"type": "base64", "media_type": media, "data": data}})
    return out


async def downloaded(url):
    """The media type and base64 data of the image at URL, from a public address and no larger than IMAGE_BYTES.

    The gateway fetches on behalf of whatever the caller was told, so a URL is held to the public internet: a name
    resolves only to public addresses (PublicResolver), a literal address is checked here because aiohttp does not
    resolve one, and each redirect is checked again rather than followed blind."""
    for _ in range(REDIRECTS + 1):
        parts = urllib.parse.urlsplit(url)
        # The host alone in every refusal: a signed URL carries its credential in the path or the query.
        host = parts.hostname
        if parts.scheme not in ("http", "https") or not host:
            raise Refused(400, f"an image URL must be http or https, not {parts.scheme or 'none'}")
        try:
            literal = ipaddress.ip_address(host)
        except ValueError:
            literal = None
        if literal is not None and not literal.is_global:
            raise Refused(400, f"an image at {host} is not on a public address")
        try:
            async with IMAGES.get(url, allow_redirects=False) as reply:
                if reply.status in (301, 302, 303, 307, 308) and reply.headers.get("Location"):
                    url = urllib.parse.urljoin(url, reply.headers["Location"])
                    continue
                if reply.status != 200:
                    raise Refused(400, f"an image at {host} answered {reply.status}")
                if (reply.content_length or 0) > IMAGE_BYTES:
                    raise Refused(400, f"an image at {host} is over {IMAGE_BYTES // 2**20} MB")
                body = bytearray()
                async for chunk in reply.content.iter_chunked(64 * 1024):
                    body += chunk
                    if len(body) > IMAGE_BYTES:
                        raise Refused(400, f"an image at {host} is over {IMAGE_BYTES // 2**20} MB")
                return reply.content_type or "image/png", base64.b64encode(body).decode()
        except (aiohttp.ClientError, TimeoutError) as e:
            raise Refused(400, f"an image at {host} could not be fetched: {type(e).__name__}")
    raise Refused(400, f"an image at {host} redirected more than {REDIRECTS} times")


class PublicResolver(aiohttp.ThreadedResolver):
    """A resolver that keeps a name's public addresses only, so an image URL cannot reach this machine or its network."""

    async def resolve(self, host, port=0, family=socket.AF_INET):
        found = [r for r in await super().resolve(host, port, family) if ipaddress.ip_address(r["host"]).is_global]
        if not found:
            raise OSError(f"{host} has no public address")
        return found


def entries(body):
    """A Responses request's system text and its conversation, as (kind, ...) tuples."""
    system = [body["instructions"]] if body.get("instructions") else []
    items = body.get("input") or []
    if isinstance(items, str):
        items = [{"type": "message", "role": "user", "content": items}]
    convo = []
    for item in items:
        kind = item.get("type") or "message"
        if kind == "message":
            role, parts = item.get("role"), parts_of(item.get("content"))
            if role in ("system", "developer"):
                system.append(plain(parts))
            elif parts:
                convo.append(("assistant" if role == "assistant" else "user", parts))
        elif kind == "function_call":
            convo.append(("call", item["call_id"], item["name"], item.get("arguments") or "{}"))
        elif kind == "custom_tool_call":
            convo.append(("call", item["call_id"], item["name"], item.get("input") or ""))
        elif kind in ("function_call_output", "custom_tool_call_output"):
            convo.append(("result", item["call_id"], parts_of(item.get("output"))))
        # reasoning and the rest carry nothing a transcript can use
    return "\n\n".join(s for s in system if s), convo


def tools_of(body):
    """(name, description, schema, custom) for each tool Claude can be given; the rest are dropped with a note."""
    out = []
    for t in body.get("tools") or []:
        if t.get("type") == "function":
            schema = dict(t.get("parameters") or {})
            schema.setdefault("type", "object")
            schema.setdefault("properties", {})
            out.append((t["name"], t.get("description") or "", schema, False))
        elif t.get("type") == "custom":
            fmt = t.get("format") or {}
            about = t.get("description") or ""
            if fmt.get("definition"):
                about += f"\n\nThe input must follow this {fmt.get('syntax', '')} grammar:\n{fmt['definition']}"
            out.append((t["name"], about, {"type": "object", "properties": {"input": {"type": "string"}},
                                           "required": ["input"]}, True))
        else:
            log(f"  dropped a {t.get('type')} tool; only function and custom tools reach Claude")
    return out


def transcript(convo):
    """A conversation replayed as one prompt's blocks, for a session that did not live through it."""
    if len(convo) == 1 and convo[0][0] == "user":
        return convo[0][1]
    last = "the last tool result" if convo[-1][0] == "result" else "the last message"
    blocks = [("This conversation began before you joined it. Here is all of it so far, as a transcript. Continue it "
               f"as the assistant from {last}, calling tools the same way, and do not repeat or mention the transcript."
               "\n\n<transcript>")]
    for e in convo:
        if e[0] == "call":
            blocks.append(f"\n\n[assistant called the tool {e[2]}, call {e[1]}]\n{e[3]}")
            continue
        blocks.append(f"\n\n[tool result for call {e[1]}]\n" if e[0] == "result" else f"\n\n[{e[0]}]\n")
        for p in e[-1]:
            blocks.append(p["text"] if p["type"] == "text" else p)
    blocks.append("\n</transcript>")
    out = []
    for b in blocks:  # adjacent strings become one text block
        if isinstance(b, str) and out and out[-1]["type"] == "text":
            out[-1]["text"] += b
        else:
            out.append({"type": "text", "text": b} if isinstance(b, str) else b)
    return out


async def one_message(blocks):
    yield {"type": "user", "message": {"role": "user", "content": blocks}, "parent_tool_use_id": None}


def mcp_content(blocks):
    """Anthropic blocks as an MCP tool result's content."""
    out = [{"type": "text", "text": b["text"]} if b["type"] == "text" else
           {"type": "image", "data": b["source"]["data"], "mimeType": b["source"]["media_type"]} for b in blocks]
    return out or [{"type": "text", "text": "(empty)"}]


def fingerprint(model, system, tools):
    return hashlib.sha256(json.dumps([model, system, tools], sort_keys=True).encode()).hexdigest()


def said(convo):
    """A hash of everything the user said in a conversation, images included, which the client sends again word for
    word: two conversations alike in their text but not their images are two conversations."""
    return hashlib.sha256(json.dumps([e[1] for e in convo if e[0] == "user"], sort_keys=True).encode()).hexdigest()


# ---------------------------------------------------------------- the Claude side

class ClaudeSession:
    """One `claude` child and the conversation it holds, paused whenever Claude calls a client tool."""

    paused = {}  # call id -> the session waiting on it
    idle = {}    # (fingerprint, said(), hash of the last reply) -> a session that finished its reply

    def __init__(self, model, system, tools, effort):
        self.model, self.key, self.said = model, fingerprint(model, system, tools), None
        self.custom = {name for name, _, _, custom in tools if custom}
        self.events, self.futures, self.waiting = asyncio.Queue(), {}, {}
        self.blocks, self.stop, self.used, self.closed = {}, None, time.monotonic(), False
        self.failure = "the Claude session could not start; is `claude` installed and logged in?"
        self.lock = asyncio.Lock()
        server = create_sdk_mcp_server(SERVER, tools=[self._tool(n, d, s) for n, d, s, _ in tools])
        # The prompt goes in a file: on the command line, one over 32,767 characters (OpenCode's is 51,000)
        # cannot start the child on Windows, and the SDK reports that as the CLI not being found.
        self.prompt = PROMPTS / f"system-{uuid.uuid4().hex}.md"
        pi_append = adapt_pi_prompt(system)
        prompt_text = pi_append if pi_append is not None else system or "You are a helpful assistant."
        self.sent_system = ({"type": "preset", "preset": "claude_code", "append": prompt_text}
                            if pi_append is not None else prompt_text)
        self.sent_tools = [{"name": PREFIX + n, "description": d, "input_schema": s} for n, d, s, _ in tools]
        self.prompt.write_text(prompt_text, encoding="utf-8", newline="")
        system_prompt = ({"type": "preset", "preset": "claude_code"} if pi_append is not None
                         else {"type": "file", "path": str(self.prompt)})
        # Keep both custom prompts and Pi's preset append off Windows' command line. Session matching still uses the original client prompt above.
        extra_args = {"append-system-prompt-file": str(self.prompt)} if pi_append is not None else {}
        options = ClaudeAgentOptions(
            model=model, system_prompt=system_prompt, extra_args=extra_args, tools=[],
            mcp_servers={SERVER: server}, strict_mcp_config=True, allowed_tools=[PREFIX + n for n, *_ in tools],
            setting_sources=[], skills=[], include_partial_messages=True, cwd=WORKDIR, effort=effort, cli_path=CLAUDE,
            env={"MCP_TOOL_TIMEOUT": str(24 * 3600 * 1000), "MAX_MCP_OUTPUT_TOKENS": "200000",
                 "ENABLE_TOOL_SEARCH": "false"})
        self.ready = asyncio.Event()
        self.task = asyncio.create_task(self._run(options))

    def _tool(self, name, description, schema):
        async def handler(args):
            call_id = await self.waiting.setdefault(name, asyncio.Queue()).get()
            return {"content": mcp_content(await self.futures[call_id])}
        return SdkMcpTool(name=name, description=description, input_schema=schema, handler=handler)

    async def _run(self, options):
        try:
            async with ClaudeSDKClient(options) as client:
                self.client = client
                self.ready.set()
                async for message in client.receive_messages():
                    self._take(message)
        except Exception as e:
            self.failure = f"the Claude session failed: {type(e).__name__}: {e}"
            log("  " + self.failure)
            self.events.put_nowait(("error", self.failure))
        finally:
            self.prompt.unlink(missing_ok=True)
            self.closed = True
            self.ready.set()
            self.events.put_nowait(("error", "the Claude session ended"))

    def _take(self, message):
        """Turn what the SDK says into ("text" | "call" | "usage" | "pause" | "done" | "error", ...) events."""
        if isinstance(message, ResultMessage):
            self.events.put_nowait(("error", message.result or message.subtype) if message.is_error else ("done",))
            return
        if not isinstance(message, StreamEvent) or message.parent_tool_use_id:
            return
        ev, kind = message.event, message.event.get("type")
        if kind == "message_start":
            self.stop = None
            self.events.put_nowait(("usage", ev["message"].get("usage") or {}))
        elif kind == "content_block_start":
            block = ev["content_block"]
            if block.get("type") == "tool_use":
                self.blocks[ev["index"]] = {"id": block["id"], "name": block["name"], "args": ""}
        elif kind == "content_block_delta":
            delta = ev["delta"]
            if delta.get("type") == "text_delta":
                self.events.put_nowait(("text", delta["text"]))
            elif delta.get("type") == "input_json_delta" and ev["index"] in self.blocks:
                self.blocks[ev["index"]]["args"] += delta.get("partial_json", "")
        elif kind == "content_block_stop" and ev["index"] in self.blocks:
            block = self.blocks.pop(ev["index"])
            name = block["name"].removeprefix(PREFIX)
            self.futures[block["id"]] = asyncio.get_running_loop().create_future()
            self.waiting.setdefault(name, asyncio.Queue()).put_nowait(block["id"])
            ClaudeSession.paused[block["id"]] = self
            args = block["args"] or "{}"
            if name in self.custom:
                args = json.loads(args).get("input", "")
            self.events.put_nowait(("call", block["id"], name, args, name in self.custom))
        elif kind == "message_delta":
            self.stop = (ev.get("delta") or {}).get("stop_reason")
            self.events.put_nowait(("usage", ev.get("usage") or {}))
        elif kind == "message_stop" and self.stop == "tool_use":
            self.events.put_nowait(("pause",))

    async def turn(self, emitter, send=None, results=None):
        """Send a prompt or hand back tool results, and yield Responses events until Claude pauses or finishes."""
        await self.ready.wait()
        if self.closed:
            raise Refused(502, self.failure)
        self.used = time.monotonic()
        if results is not None:
            missing = [{"type": "text", "text": "(the client returned no output for this call)"}]
            # Every result fetched before any call is let go: an image that fails leaves the session paused on the
            # same calls, so the client can send the results again.
            ready = {call_id: await fetched(results.get(call_id, missing))
                     for call_id, future in self.futures.items() if not future.done()}
            sent_request({"system": self.sent_system, "tools": self.sent_tools, "tool_results": ready}, ready,
                         self.sent_system, self.sent_tools)
            for call_id, future in list(self.futures.items()):
                ClaudeSession.paused.pop(call_id, None)
                del self.futures[call_id]
                if not future.done():
                    future.set_result(ready[call_id])
        else:
            blocks = await fetched(send)
            message = {"role": "user", "content": blocks}
            sent_request({"system": self.sent_system, "tools": self.sent_tools, "input": message}, message,
                         self.sent_system, self.sent_tools)
            await self.client.query(one_message(blocks))
        usage, reply = {}, ""
        while True:
            event = await self.events.get()
            if event[0] == "text":
                reply += event[1]
                for e in emitter.text(event[1]):
                    yield e
            elif event[0] == "call":
                for e in emitter.call(*event[1:]):
                    yield e
            elif event[0] == "usage":
                for k, v in event[1].items():
                    if isinstance(v, int):
                        usage[k] = usage.get(k, 0) + v
            elif event[0] == "error":
                raise Refused(502, event[1])
            elif event[0] in ("pause", "done"):
                break
        if event[0] == "done":
            ClaudeSession.idle[(self.key, self.said, hashlib.sha256(reply.strip().encode()).hexdigest())] = self
        self.used = time.monotonic()
        cached = usage.get("cache_read_input_tokens", 0)
        prompt = usage.get("input_tokens", 0) + cached + usage.get("cache_creation_input_tokens", 0)
        out = usage.get("output_tokens", 0)
        for e in emitter.finish({"input_tokens": prompt, "input_tokens_details": {"cached_tokens": cached},
                                 "output_tokens": out, "total_tokens": prompt + out}):
            yield e

    def close(self):
        self.task.cancel()
        for table in (ClaudeSession.paused, ClaudeSession.idle):
            for k in [k for k, s in table.items() if s is self]:
                del table[k]


def effort_of(model, body):
    effort = (body.get("reasoning") or {}).get("effort")
    if not effort or "haiku" in model:
        return None
    return {"none": "low", "minimal": "low"}.get(effort, effort)


async def claude(model, body):
    """Responses events for a request to anthropic/MODEL."""
    system, convo = entries(body)
    tools = tools_of(body)
    if not convo:
        raise Refused(400, "the request has no conversation in it")
    emitter = Emitter(f"anthropic/{model}")
    tail = []
    for e in reversed(convo):
        if e[0] != "result":
            break
        tail.append(e)
    owners = {ClaudeSession.paused.get(e[1]) for e in tail}
    if tail and len(owners) == 1 and None not in owners:
        session, route, run = owners.pop(), "resumed", {"results": {e[1]: e[2] for e in tail}}
    else:
        news = []
        for e in reversed(convo):
            if e[0] != "user":
                break
            news[:0] = e[1]
        tail_users = next((i for i, e in enumerate(reversed(convo)) if e[0] != "user"), len(convo))
        before = convo[-tail_users - 1] if news and tail_users < len(convo) else None
        key = None
        if before and before[0] == "assistant":
            # The user's side too, so two conversations that were given the same reply keep their own sessions.
            key = (fingerprint(model, system, tools), said(convo[:len(convo) - tail_users]),
                   hashlib.sha256(plain(before[1]).strip().encode()).hexdigest())
        session = ClaudeSession.idle.pop(key, None) if key else None
        if session and not session.closed:
            route, run = "continued", {"send": news}
        else:
            session = ClaudeSession(model, system, tools, effort_of(model, body))
            SESSIONS.add(session)
            route, run = f"fresh, {len(convo)} entries", {"send": transcript(convo)}
    if not hasattr(session, "log_id"):
        session.log_id = new_id("session")
    bind_log(session.log_id)
    log(f"  anthropic/{model}: {route}")
    async with session.lock:
        # Two requests carrying the same results can both find the paused session; only the first hands them back.
        if route == "resumed" and any(ClaudeSession.paused.get(e[1]) is not session for e in tail):
            raise Refused(409, "those tool results were already handed back by another request")
        session.said = said(convo)
        async for e in session.turn(emitter, **run):
            yield e


async def reap():
    while True:
        await asyncio.sleep(60)
        for s in [s for s in SESSIONS if s.closed or time.monotonic() - s.used > IDLE]:
            SESSIONS.discard(s)
            s.close()


# ---------------------------------------------------------------- the OpenAI side

def codex_login():
    home = pathlib.Path(os.environ.get("CODEX_HOME") or pathlib.Path.home() / ".codex")
    try:
        tokens = json.loads((home / "auth.json").read_text(encoding="utf-8")).get("tokens") or {}
    except (OSError, ValueError):
        raise Refused(401, f"no Codex login at {home / 'auth.json'}; run `codex login`")
    access, account = tokens.get("access_token"), tokens.get("account_id")
    if not access or not account:
        raise Refused(401, "the Codex login is not a ChatGPT login; run `codex login`")
    try:
        payload = access.split(".")[1]
        expires = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4))).get("exp", 0)
    except (IndexError, ValueError):
        expires = 0
    if expires < time.time() + 30:
        raise Refused(401, "the Codex login has expired; run any `codex` command to refresh it, then retry")
    return access, account


async def openai(model, body):
    """Responses events for a request to openai/MODEL, from the Codex backend."""
    bind_log()
    access, account = codex_login()
    sent = {k: v for k, v in body.items() if k in CODEX_FIELDS}
    sent.update(model=model, store=False, stream=True)
    sent.setdefault("instructions", "You are a helpful assistant.")
    if isinstance(sent.get("input"), str):
        sent["input"] = [{"type": "message", "role": "user", "content": sent["input"]}]
    dropped = sorted(set(body) - CODEX_FIELDS - {"model", "stream", "store"})
    log(f"  openai/{model}" + (f": dropped {', '.join(dropped)}" if dropped else ""))
    headers = {"Authorization": f"Bearer {access}", "chatgpt-account-id": account, "OpenAI-Beta": "responses=experimental",
               "originator": "llm-gateway", "accept": "text/event-stream", "content-type": "application/json"}
    sent_request(sent, last_input(sent, "responses"), lone_system(sent, "responses"), sent.get("tools"))
    async with HTTP.post(CODEX_URL, json=sent, headers=headers) as reply:
        if reply.status != 200:
            text = (await reply.text())[:500]
            raise Refused(reply.status, f"the Codex backend answered {reply.status}: {text}")
        data = []
        async for raw in reply.content:
            line = raw.decode("utf-8").rstrip("\r\n")
            if line.startswith("data:"):
                data.append(line[5:].strip())
            elif not line and data:
                chunk = "\n".join(data)
                data = []
                if chunk != "[DONE]":
                    yield json.loads(chunk)


async def claude_models():
    """The names `claude --model` takes: each alias the harness offers, and the model it resolves to."""
    options = ClaudeAgentOptions(setting_sources=[], skills=[], cwd=WORKDIR, cli_path=CLAUDE)
    async with asyncio.timeout(60):
        async with ClaudeSDKClient(options) as client:
            info = await client.get_server_info() or {}
    names = [n for m in info.get("models") or [] for n in (m.get("value"), m.get("resolvedModel")) if n]
    return list(dict.fromkeys(n for n in names if n != "default"))


async def codex_version():
    """The installed Codex's version, which its backend wants to know before it says which models it has."""
    try:
        child = await asyncio.create_subprocess_exec(shutil.which("codex") or "codex", "--version",
                                                     stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        out, _ = await asyncio.wait_for(child.communicate(), 30)
    except (OSError, TimeoutError):
        raise Refused(502, "codex --version could not be run")
    found = re.search(rb"\d+\.\d+\.\d+\S*", out)
    if not found:
        raise Refused(502, "codex --version gave no version")
    return found.group().decode()


async def codex_models():
    """The models the Codex backend lists for this login: the ones its own `/model` picker shows."""
    access, account = codex_login()
    headers = {"Authorization": f"Bearer {access}", "chatgpt-account-id": account, "originator": "llm-gateway"}
    async with HTTP.get(CODEX_MODELS_URL, params={"client_version": await codex_version()}, headers=headers) as reply:
        if reply.status != 200:
            raise Refused(reply.status, f"the Codex backend answered {reply.status}: {(await reply.text())[:500]}")
        found = await reply.json()
    return [m["slug"] for m in found.get("models") or [] if m.get("visibility") == "list" and m.get("supported_in_api", True)]


PROVIDERS = {"anthropic": claude, "openai": openai}
LISTERS = {"anthropic": claude_models, "openai": codex_models}
LISTED = {}                                      # provider -> (when it was asked, its models)


# ---------------------------------------------------------------- Chat Completions, translated

def chat_parts(content):
    """Chat content parts as Responses input parts; a string stays a string."""
    if not isinstance(content, list):
        return content or ""
    return [{"type": "input_text", "text": p["text"]} if p.get("type") == "text" else
            {"type": "input_image", "image_url": (p.get("image_url") or {}).get("url")}
            for p in content if p.get("type") in ("text", "image_url")]


def chat_to_responses(body):
    """A Chat Completions request as a Responses request; one missing a field it needs, a tool call with no id
    say, is refused as a bad request rather than failing as the gateway's own error."""
    try:
        return _chat_to_responses(body)
    except (KeyError, TypeError, AttributeError) as e:
        raise Refused(400, f"malformed chat request: {type(e).__name__} {e}") from None


def _chat_to_responses(body):
    system, items = [], []
    for m in body.get("messages") or []:
        role = m.get("role")
        if role in ("system", "developer"):
            system.append(text_of(m.get("content")))
        elif role == "user":
            items.append({"type": "message", "role": "user", "content": chat_parts(m.get("content"))})
        elif role == "assistant":
            if text_of(m.get("content")):
                items.append({"type": "message", "role": "assistant",
                              "content": [{"type": "output_text", "text": text_of(m.get("content"))}]})
            for c in m.get("tool_calls") or []:
                items.append({"type": "function_call", "call_id": c["id"], "name": c["function"]["name"],
                              "arguments": c["function"].get("arguments") or "{}"})
        elif role == "tool":
            items.append({"type": "function_call_output", "call_id": m["tool_call_id"],
                          "output": chat_parts(m.get("content"))})
    out = {"model": body.get("model"), "input": items, "stream": bool(body.get("stream"))}
    if body.get("metadata"):
        out["metadata"] = body["metadata"]
    if system:
        out["instructions"] = "\n\n".join(system)
    tools = [{"type": "function", "name": t["function"]["name"], "description": t["function"].get("description", ""),
              "parameters": t["function"].get("parameters") or {"type": "object", "properties": {}}}
             for t in body.get("tools") or [] if t.get("type") == "function"]
    if tools:
        out["tools"] = tools
    choice = body.get("tool_choice")
    if isinstance(choice, dict):
        choice = {"type": "function", "name": choice["function"]["name"]}
    if choice:
        out["tool_choice"] = choice
    if body.get("reasoning_effort"):
        out["reasoning"] = {"effort": body["reasoning_effort"]}
    return out


class ChatWriter:
    """Responses events in, Chat Completions chunks out."""

    def __init__(self, model):
        self.id, self.model, self.created = new_id("chatcmpl"), model, int(time.time())
        self.calls, self.text, self.reasoning, self.usage, self.finish = [], "", "", None, "stop"
        self.slots = {}  # a call's item id -> its index in calls, as parallel calls' deltas may interleave

    def chunk(self, delta, finish=None):
        return {"id": self.id, "object": "chat.completion.chunk", "created": self.created, "model": self.model,
                "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}

    def take(self, e):
        kind = e.get("type")
        if kind == "response.output_text.delta":
            self.text += e["delta"]
            return [self.chunk({"content": e["delta"]})]
        if kind == "response.reasoning_summary_text.delta":
            self.reasoning += e["delta"]
            return [self.chunk({"reasoning_content": e["delta"]})]
        if kind == "response.output_item.added" and e["item"].get("type") == "function_call":
            item = e["item"]
            self.slots[item.get("id")] = len(self.calls)
            self.calls.append({"id": item["call_id"], "type": "function",
                               "function": {"name": item["name"], "arguments": ""}})
            return [self.chunk({"tool_calls": [{"index": len(self.calls) - 1, "id": item["call_id"], "type": "function",
                                                "function": {"name": item["name"], "arguments": ""}}]})]
        if kind == "response.function_call_arguments.delta" and self.calls:
            slot = self.slots.get(e.get("item_id"), len(self.calls) - 1)
            self.calls[slot]["function"]["arguments"] += e["delta"]
            return [self.chunk({"tool_calls": [{"index": slot, "function": {"arguments": e["delta"]}}]})]
        if kind in ("response.completed", "response.incomplete"):
            u = e["response"].get("usage") or {}
            self.usage = {"prompt_tokens": u.get("input_tokens", 0), "completion_tokens": u.get("output_tokens", 0),
                          "total_tokens": u.get("total_tokens", 0),
                          "prompt_tokens_details": {"cached_tokens": (u.get("input_tokens_details") or {})
                                                    .get("cached_tokens", 0)}}
            self.finish = "tool_calls" if self.calls else "length" if kind == "response.incomplete" else "stop"
            return [self.chunk({}, self.finish)]
        if kind == "response.failed":
            raise Refused(502, failure_of(e))
        return []

    def whole(self):
        message = {"role": "assistant", "content": self.text or None}
        if self.calls:
            message["tool_calls"] = self.calls
        if self.reasoning:
            message["reasoning_content"] = self.reasoning
        return {"id": self.id, "object": "chat.completion", "created": self.created, "model": self.model,
                "choices": [{"index": 0, "message": message, "finish_reason": self.finish}], "usage": self.usage}


# ---------------------------------------------------------------- HTTP

def backend(body, sent=None):
    named = body.get("model")
    provider, _, model = (named if isinstance(named, str) else "").partition("/")
    if provider not in PROVIDERS or not model:
        raise Refused(400, f"the model {named!r} names no provider; use "
                           + " or ".join(f"{p}/MODEL" for p in PROVIDERS))
    identify_log(body)
    return guarded(PROVIDERS[provider](model, body), sent)


async def guarded(events, sent=None):
    """EVENTS, with any failure but a refusal made one: a provider that cannot be reached is a 502, and anything
    else -- a request shaped as no client should send, or a fault here -- a 500 with its traceback in the log, so the
    client gets an error it can read, or a response.failed event mid-stream, rather than a dropped connection."""
    try:
        while True:
            token = SENT.set(sent)
            try:
                e = await anext(events)
            except StopAsyncIteration:
                return
            finally:
                SENT.reset(token)
            yield e
    except Refused:
        raise
    except (aiohttp.ClientError, TimeoutError) as e:
        raise Refused(502, f"the provider could not be reached: {type(e).__name__}")
    except Exception as e:
        log("  " + traceback.format_exc().rstrip().replace("\n", "\n  "))
        raise Refused(500, f"the gateway could not handle this request ({type(e).__name__}); see its log")


def failure_of(e):
    """What a response.failed event says went wrong."""
    return ((e["response"].get("error") or {}).get("message")) or "the model failed"


async def first(events):
    """The first event, so a refusal is still an HTTP error rather than a broken stream."""
    try:
        return await events.__anext__()
    except StopAsyncIteration:
        raise Refused(502, "the model sent nothing")


def refusal(status, message):
    error = {"message": message, "type": "gateway_error"}
    return web.json_response({"error": error}, status=status)


def api_key():
    """GATEWAY_API_KEY from the environment, else from .env; with neither, a new one written to ./.env."""
    key = os.environ.get("GATEWAY_API_KEY") or dotenv_values(find_dotenv(usecwd=True)).get("GATEWAY_API_KEY")
    if key and key.strip():
        return key.strip()
    key, path = secrets.token_urlsafe(32), pathlib.Path(".env").resolve()
    lead = "\n" if path.exists() and not path.read_bytes().endswith(b"\n") and path.stat().st_size else ""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(fd, "a", encoding="utf-8", newline="\n") as f:
        f.write(f"{lead}GATEWAY_API_KEY={key}\n")
    log(f"no GATEWAY_API_KEY: generated one in {path}")
    return key


def keyed(key):
    """Middleware refusing any request without `Authorization: Bearer KEY`. The key is never logged."""
    wanted = f"Bearer {key}".encode()

    @web.middleware
    async def check(request, handler):
        given = request.headers.get("Authorization", "").encode()
        if not hmac.compare_digest(given, wanted):
            log(f"{request.method} {request.path} refused 401: no valid API key")
            return refusal(401, "a valid API key is required: Authorization: Bearer GATEWAY_API_KEY")
        return await handler(request)
    return check


async def json_body(request):
    """The request's JSON object, or a 400 for a body that is not one."""
    try:
        body = await request.json()
    except ValueError:
        raise Refused(400, "the request body is not JSON")
    if not isinstance(body, dict):
        raise Refused(400, "the request body is not a JSON object")
    return body


async def sse(request):
    reply = web.StreamResponse(headers={"Content-Type": "text/event-stream", "Cache-Control": "no-cache"})
    await reply.prepare(request)
    return reply


@conversation_log
async def responses(request):
    started = time.monotonic()
    try:
        body = await json_body(request)
    except Refused as e:
        log(f"POST {request.path} refused {e.status}: {e}")
        return refusal(e.status, str(e))
    sent = {}
    if SOURCE == "received":
        trace("responses", body)
    log(f"POST /v1/responses {body.get('model')} stream={bool(body.get('stream'))}")
    try:
        events = backend(body, sent)
        head = await first(events)
    except Refused as e:
        log(f"  refused {e.status}: {e}")
        record("responses", body, started, sent=sent, error=f"{e.status}: {e}")
        return refusal(e.status, str(e))
    reply, n, last, done, failure = None, 0, None, [], None
    try:
        if body.get("stream"):
            reply = await sse(request)
        async for e in _chain(head, events):
            trace("event", e)
            if e.get("type") == "response.failed" and not reply:
                raise Refused(502, failure_of(e))  # a whole reply that failed is an HTTP error, not a 200
            if e.get("type") == "response.output_item.done":
                done.append(e["item"])
            if e.get("type", "").startswith("response.") and "response" in e:
                last = e["response"]
                # The Codex backend ends with an empty output and sends the items only one by one.
                if e["type"] in ("response.completed", "response.incomplete") and not last.get("output"):
                    last["output"] = done
            if reply:
                e["sequence_number"] = n
                n += 1
                await reply.write(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n".encode())
    except Refused as e:
        log(f"  failed mid-reply: {e}")
        failure = f"{e.status}: {e}"
        if not reply:
            record("responses", body, started, sent=sent, output=done, error=failure)
            return refusal(e.status, str(e))
        failed = {"type": "response.failed", "sequence_number": n,
                  "response": {"status": "failed", "error": {"code": "gateway_error", "message": str(e)}}}
        await reply.write(f"event: response.failed\ndata: {json.dumps(failed)}\n\n".encode())
    log(f"  {time.monotonic() - started:.1f}s, usage {(last or {}).get('usage')}")
    record("responses", body, started, sent=sent, output=(last or {}).get("output") or done,
           usage=(last or {}).get("usage"), error=failure)
    if reply:
        await reply.write_eof()
        return reply
    if last is not None and not last.get("output"):
        last["output"] = done
    return web.json_response(last)


@conversation_log
async def chat(request):
    started = time.monotonic()
    try:
        body = await json_body(request)
    except Refused as e:
        log(f"POST {request.path} refused {e.status}: {e}")
        return refusal(e.status, str(e))
    sent = {}
    if SOURCE == "received":
        trace("chat", body)
    log(f"POST /v1/chat/completions {body.get('model')} stream={bool(body.get('stream'))}")
    writer = ChatWriter(body.get("model"))
    try:
        events = backend(chat_to_responses(body), sent)
        head = await first(events)
    except Refused as e:
        log(f"  refused {e.status}: {e}")
        record("chat", body, started, sent=sent, error=f"{e.status}: {e}")
        return refusal(e.status, str(e))
    stream, reply, failure = bool(body.get("stream")), None, None
    try:
        if stream:
            reply = await sse(request)
            await reply.write(f"data: {json.dumps(writer.chunk({'role': 'assistant', 'content': ''}))}\n\n".encode())
        async for e in _chain(head, events):
            trace("event", e)
            for c in writer.take(e):
                if reply:
                    await reply.write(f"data: {json.dumps(c)}\n\n".encode())
    except Refused as e:
        log(f"  failed mid-reply: {e}")
        failure = f"{e.status}: {e}"
        if not reply:
            record("chat", body, started, sent=sent, output=writer.whole()["choices"][0]["message"], error=failure)
            return refusal(e.status, str(e))
        await reply.write(f"data: {json.dumps({'error': {'message': str(e)}})}\n\n".encode())
    log(f"  {time.monotonic() - started:.1f}s, usage {writer.usage}")
    record("chat", body, started, sent=sent, output=writer.whole()["choices"][0]["message"], usage=writer.usage, error=failure)
    if not reply:
        return web.json_response(writer.whole())
    if (body.get("stream_options") or {}).get("include_usage"):
        await reply.write(f"data: {json.dumps(dict(writer.chunk({}), choices=[], usage=writer.usage))}\n\n".encode())
    await reply.write(b"data: [DONE]\n\n")
    await reply.write_eof()
    return reply


TERMINAL = ("response.completed", "response.incomplete", "response.failed")


async def _chain(head, rest):
    """HEAD, then the rest; a stream that ends without saying it finished is a failure, not a short answer."""
    yield head
    ended = head.get("type") in TERMINAL
    async for e in rest:
        ended = ended or e.get("type") in TERMINAL
        yield e
    if not ended:
        raise Refused(502, "the model's stream ended before the response was finished")


async def listed(provider):
    """PROVIDER's models, asked of its harness at most every LISTED_FOR seconds; the last answer if it cannot be
    asked now, none if it never could."""
    when, names = LISTED.get(provider, (0, []))
    if when and time.monotonic() - when < LISTED_FOR:
        return names
    try:
        names = await LISTERS[provider]()
    except Exception as e:
        log(f"  {provider} models: {type(e).__name__}: {e}")
        return names
    LISTED[provider] = (time.monotonic(), names)
    return names


async def models(request):
    found = await asyncio.gather(*(listed(p) for p in LISTERS))
    data = [{"id": f"{p}/{n}", "object": "model", "owned_by": p} for p, names in zip(LISTERS, found) for n in names]
    if not data:
        return refusal(502, "no provider could list its models; see the log")
    return web.json_response({"object": "list", "data": data})


async def startup(app):
    global HTTP, IMAGES
    HTTP = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=None, sock_read=600))
    IMAGES = aiohttp.ClientSession(connector=aiohttp.TCPConnector(resolver=PublicResolver()),
                                   timeout=aiohttp.ClientTimeout(total=120))
    app["reaper"] = asyncio.create_task(reap())


async def cleanup(app):
    app["reaper"].cancel()
    for s in list(SESSIONS):
        s.close()
    await HTTP.close()
    await IMAGES.close()


def claude_exe():
    """A native `claude`: the SDK will not run npm's claude.cmd shim, but the npm package holds claude.exe."""
    if os.environ.get("CLAUDE_CLI"):
        return os.environ["CLAUDE_CLI"]
    found = shutil.which("claude")
    if found and found.lower().endswith((".cmd", ".bat")):
        native = pathlib.Path(found).parent / "node_modules" / "@anthropic-ai" / "claude-code" / "bin" / "claude.exe"
        return str(native) if native.exists() else None
    return found


SESSIONS, HTTP, IMAGES, CLAUDE = set(), None, None, claude_exe()
WORKDIR = str(pathlib.Path(tempfile.gettempdir()) / "llm-gateway")
PROMPTS = pathlib.Path(WORKDIR) / "prompts"  # one folder a port once serving, so no gateway sweeps another's


def serve(port, config):
    """Serve on 127.0.0.1:port until stopped, with the settings in the config file."""
    global LOG, LEVEL, SOURCE, PROMPTS
    LOG, LEVEL, SOURCE = configured(config)
    PROMPTS = pathlib.Path(WORKDIR) / f"prompts-{port}"
    key = api_key()
    os.makedirs(PROMPTS, exist_ok=True)
    for left in PROMPTS.glob("system-*.md"):  # left by this port's last gateway, which `stop` kills outright
        left.unlink(missing_ok=True)
    # The Claude side is the subscription or nothing: a key or a base URL in the environment would reroute the child.
    for name in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL"):
        if os.environ.pop(name, None) is not None:
            log(f"ignoring {name} from the environment")
    app = web.Application(client_max_size=64 * 1024 * 1024, middlewares=[keyed(key)])
    app.add_routes([web.post("/v1/responses", responses), web.post("/v1/chat/completions", chat),
                    web.get("/v1/models", models)])
    app.on_startup.append(startup)
    app.on_cleanup.append(cleanup)
    log(f"claude: {CLAUDE or 'the SDK default'}")
    log(f"log: {f'{LOG} at {LEVELS[LEVEL]}' if LOG else 'off'}")
    log(f"listening on http://127.0.0.1:{port}/v1")
    web.run_app(app, host="127.0.0.1", port=port, print=None)
