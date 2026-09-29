# llm-gateway

A small LLM gateway on loopback. It speaks OpenAI's Responses and Chat Completions APIs, and answers them from a Claude or ChatGPT subscription instead of an API key.

## Run it

```bash
uv sync
cp .env.example .env         # then set GATEWAY_API_KEY in it
uv run llm-gateway            # listens on http://127.0.0.1:8797/v1
uv run llm-gateway --port 9000
```

Point any OpenAI-compatible client at `http://127.0.0.1:8797/v1`, with `GATEWAY_API_KEY` as its API key. It serves `/v1/responses`, `/v1/chat/completions` and `/v1/models`.

## API key

Every request needs `Authorization: Bearer KEY`, which is what an OpenAI client sends for its API key. The gateway reads `GATEWAY_API_KEY` from the environment first, then from the first `.env` found from the working directory upward, and refuses to start without one. `.env` is gitignored. The key is compared in constant time and never logged.

Make one with `uv run python -c "import secrets; print(secrets.token_urlsafe(32))"`.

The key is what stops other processes on this machine, or a web page in your browser, from spending your plans: a browser cannot send an `Authorization` header to another site without a CORS preflight, which the gateway never answers.

## Models

A model is named `provider/model`, as in LiteLLM.

| Model | Goes to | Needs |
| --- | --- | --- |
| `anthropic/MODEL` | Claude, through the Claude Agent SDK | `claude` installed and logged in. `MODEL` is anything `claude --model` takes: `sonnet`, `opus`, `haiku`, `claude-opus-5-5`. |
| `openai/MODEL` | OpenAI, through the ChatGPT Codex backend | A Codex login in `~/.codex/auth.json` (or `$CODEX_HOME`). |

## Tools

The client runs its own tools. A tool call comes back to the client as a `function_call` (Responses) or `tool_calls` (Chat Completions), and the client sends the result back in its next request.

On the Claude side the Agent SDK only accepts tools as MCP tools, so the gateway registers the client's tools as an in-process MCP server whose handlers run nothing: a call ends the HTTP response, the handler waits, and the next request carrying that call's output hands it over. Claude Code's own tools, settings and skills are all turned off.

## Images

Both APIs accept images, as data URLs or remote URLs. The Claude side fetches a remote image and sends it as base64.

## Credentials

- The Codex token is read from `auth.json` on every request and never refreshed, written, logged or printed. When it expires, run any `codex` command and retry.
- Claude's login is never touched: the SDK's `claude` child uses its own. `ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_TOKEN` and `ANTHROPIC_BASE_URL` are dropped from the environment at startup, so the Claude side is the subscription or nothing.
- On Windows the SDK will not run npm's `claude.cmd` shim, so the gateway uses the `claude.exe` inside the npm package. `CLAUDE_CLI` overrides the path.

## Limits

- It binds to `127.0.0.1` only. Anything on this machine that can read `.env` can use it.
- Anthropic bills requests it identifies as a third-party app to Extra Usage instead of plan limits. A full agent harness sending its own system prompt, such as Pi, is refused with `Third-party apps now draw from extra usage` unless Extra Usage is on.
- A conversation with no live Claude session (after a restart, or a client that edited its history) is replayed to a fresh session as a transcript.
- Claude's thinking is not passed back. The Codex backend drops fields it does not accept, such as `temperature`.

## Request log

`config.toml` (read from the working directory, or `--config PATH`) turns it on:

```toml
[log]
file = "log.jsonl"   # relative to config.toml; comment out to log nothing
```

Each request is one JSON line: `time`, `api` (`responses` or `chat`), `model`, `stream`, `seconds`, `input` (the last message the client sent, as it sent it: a user message or a tool result), `output` (the Responses output items, or the Chat Completions assistant message with any tool calls), `usage`, and `error` when a request was refused or failed. Image data URLs are cut to their type and size. The key is never in it, but prompts and replies are, in full.

## Debugging

`GATEWAY_DUMP=path.jsonl` appends every request and every event to that file. It holds every prompt and image a client sent, so delete it when done; `*.jsonl` is gitignored.

## Smoke test

With the gateway running:

```bash
uv run tools/smoke.py anthropic/haiku [image.jpg]
uv run tools/smoke.py openai/gpt-6-luna [image.jpg]
```

It checks a client-run tool round trip, a follow-up and streaming on both APIs, and an image if one is given.
