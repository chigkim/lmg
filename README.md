# llm-gateway

One command, `lmg`, for three jobs: a small LLM gateway on loopback, jobs handed to agy, Claude Code or Codex in a folder of your own, and what each subscription plan has left.

| Command | What it does |
| --- | --- |
| `lmg proxy [run\|start\|stop\|restart\|status\|models]` | The gateway. It speaks OpenAI's Responses and Chat Completions APIs, and answers them from a Claude or ChatGPT subscription instead of an API key. |
| `lmg ask HARNESS MODE FOLDER [ID]` | Runs the job in `FOLDER/task.md`, or follows up the one `ID` names, with a usage reading either side, and prints the `lmg usage` table when it ends. `lmg ask review SHA FOLDER` is its other form. |
| `lmg usage [NOTE]` | One table of every plan's 5-hour and weekly windows, agy's two model groups each on their own rows: percent left, the spare against an even pace (+ ahead, - behind), what one task of its harness costs it on average, how many more tasks that leaves, and when each resets. With a note, `word: what` such as `"before: pi x"`, it also adds a row to the ledger, which keeps agy's Gemini group only. Any other word is refused, so a mistyped form writes nothing. `lmg usage --help` lists every form, and each form takes `--help` too. |
| `lmg usage calls [--hours N \| --since "YYYY-MM-DD HH:MM"]` | Every call each plan was spent on, read from the clients' own records, grouped by who made it (`claude code`, `claude -p` and `pi bridge` for Claude; `codex` and `codex exec` for Codex), with tokens and what they would cost on the public API. |
| `lmg usage tasks` | Every task in the ledger with what it took from each window, then what a task costs each window and how many tasks that is measured from. A cost from fewer than five tasks is marked `~`, here and in `lmg usage`. |
| `lmg usage worth` | What each window is worth in dollars, sized from the ledger and the clients' records, and how much of it is spent. It takes no options but `--help`, and it is slower than the rest (about 9 seconds), since it reads every record back to a week before the ledger's first row. |

Three things run a model: `lmg proxy`, every form of `lmg ask` (`review` included), and the Haiku fallback of `lmg usage` described below. `lmg usage` asks `codex app-server` for Codex's limits, reads agy's quota out of the log `agy models` writes, and asks the endpoint Claude Code's own `/usage` reads, with the CLI's login token held in memory for that one request and never printed or stored; a redirect is refused rather than followed, because following it would send the token on. When that request cannot be made or fails -- an expired token, no credentials file, no network, an error status, a broken reply or one of another shape -- it falls back to a one-word Haiku call, which costs a little and refreshes the login.

## Every command

```yaml
lmg:
  --help, -h:                        # this list's short form
  proxy:                             # the gateway
    run:                             # in this console until Ctrl-C; the default when no word is given
    start:                           # in the background; returns once it is listening
    stop:                            # stop it and everything it started
    restart:                         # stop, then start
    status:                          # exit 0 when running, 3 when not
    options:
      --port N:                      # default 8797
      --config PATH:                 # default ~/.lmg/config.toml
  ask:                               # a job in a folder of your own
    agy|claude|codex:
      read|edit:
        FOLDER:                      # holds task.md; the job is named after it, and its answers go in it
          ID:                        # optional: follow up that session
    review:
      SHA:
        FOLDER:                      # codex exec review of one commit, its answer in FOLDER
    options:
      --model NAME:                  # default gemini-3.8-flash-medium, claude-opus-5-5 or gpt-6.1-sol
      --timeout T:                   # agy's print timeout; default 3600s
      --force:                       # run an edit job in a repository's main checkout
  usage:                             # every window in one table: percent left, spare, task cost, tasks left, resets
    NOTE:                            # optional: also add a ledger row; "word: what", e.g. "before: pi x"
    calls:                           # every call each plan was spent on, and its API cost
      --hours N:                     # default 5
      --since "YYYY-MM-DD HH:MM":    # local time; overrides --hours
    tasks:                           # every task, then what a task costs each window
    worth:                           # each window's size and use, in dollars
```

## Install

Python 3.14. Install it editable, so edits to the checkout take effect at once:

```bash
uv tool install -e .
```

`lmg` then runs from any folder. A change to `pyproject.toml` needs `uv tool install -e --reinstall .`.

A plan whose client is not on `PATH` (`claude`, `codex`, `agy`) is reported as one that could not be asked, and the others are still read. `lmg ask` needs the harness it runs on `PATH`, and `git`.

## The proxy

```bash
uv sync
uv run lmg proxy start      # in the background, on http://127.0.0.1:8797/v1
uv run lmg proxy status
uv run lmg proxy restart    # after changing ~/.lmg/config.toml
uv run lmg proxy stop
uv run lmg proxy run        # or in this console, until Ctrl-C
```

Every command takes `--port N` (default 8797) and `--config PATH` (default `~/.lmg/config.toml`). Installed as a tool, `lmg proxy` runs without `uv run`.

What lmg keeps for you is in `~/.lmg`, whatever folder you run it from: `.env` (the key), `config.toml` (copy the example in this repository there), `logs/` (conversation logs, relative to `config.toml`), `daemon/` (pid, lock and console log per port), `usage-ledger.csv` and `token-ledger.csv`, plus `memory/` and `prompts-PORT/` for the Claude sessions.

The daemon is known by its port. Its pid and console output are in `~/.lmg/daemon/PORT.pid` and `PORT.log`, so `stop` and `status` work from any folder. `start` returns once the port accepts connections, or reports the exit and points at the log. `stop` kills the whole process tree, Claude children included. `status` exits 0 when running and 3 when not.

Point any OpenAI-compatible client at `http://127.0.0.1:8797/v1`, with `GATEWAY_API_KEY` as its API key. It serves `/v1/responses`, `/v1/chat/completions` and `/v1/models`.

`lmg proxy models` prints the same list, one `provider/NAME` a line, without the gateway running (exit 1 if a harness could not answer, 3 if none did). `/v1/models` is asked of the harnesses, not written down: each alias `claude --model` offers and the model it resolves to, and the models Codex's backend lists for your login. A list is kept for 5 minutes; a harness that cannot answer leaves out its models, or keeps its last list. Any name `claude --model` or Codex accepts works whether it is listed or not.

Codex takes it as a provider in `~/.codex/config.toml`, with the key in an environment variable rather than the file:

```toml
[model_providers.lmg]
name = "lmg proxy"
base_url = "http://127.0.0.1:8797/v1"
env_key = "LMG_KEY"
wire_api = "responses"
```

Then `codex exec -c model_provider=lmg -m anthropic/claude-haiku-4-5 "..."` runs Codex on Claude, its shell tool included, with `LMG_KEY` set to the gateway's key. Codex warns that it has no metadata for a model it does not know and carries on.

### API key

Every request needs `Authorization: Bearer KEY`, which is what an OpenAI client sends for its API key. The gateway reads `GATEWAY_API_KEY` from the environment first, then from `~/.lmg/.env`, never the folder it runs in. With neither, the first run generates a random key, writes it to `~/.lmg/.env` (appending if that file exists without one) and says so, without printing the key. Give a client the value from that file. The key is compared in constant time and never logged.

The key is what stops other processes on this machine, or a web page in your browser, from spending your plans: a browser cannot send an `Authorization` header to another site without a CORS preflight, which the gateway never answers.

### Models

A model is named `provider/model`, as in LiteLLM.

| Model | Goes to | Needs |
| --- | --- | --- |
| `anthropic/MODEL` | Claude, through the Claude Agent SDK | `claude` installed and logged in. `MODEL` is anything `claude --model` takes: `sonnet`, `opus`, `haiku`, `claude-opus-5-5`. |
| `openai/MODEL` | OpenAI, through the ChatGPT Codex backend | A Codex login in `~/.codex/auth.json` (or `$CODEX_HOME`). |

### Tools

The client runs its own tools. A tool call comes back to the client as a `function_call` (Responses) or `tool_calls` (Chat Completions), and the client sends the result back in its next request.

On the Claude side the Agent SDK only accepts tools as MCP tools, so the gateway registers the client's tools as an in-process MCP server whose handlers run nothing: a call ends the HTTP response, the handler waits, and the next request carrying that call's output hands it over. Claude Code's own tools, settings and skills are all turned off.

### Images

Both APIs accept images, as data URLs or remote URLs. The Claude side fetches a remote image and sends it as base64. It fetches only from the public internet: a URL must be `http` or `https`, a host that is or resolves to a loopback, private, link-local or other non-public address is refused, each redirect is checked again up to five, and an image over 20 MB is refused. A tool result whose image cannot be fetched is refused with the session still waiting on that call, so the client can send it again.

### Credentials

- The Codex token is read from `auth.json` on every request and never refreshed, written, logged or printed. When it expires, run any `codex` command and retry.
- Claude's login is never touched: the SDK's `claude` child uses its own. `ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_TOKEN` and `ANTHROPIC_BASE_URL` are dropped from the environment at startup, so the Claude side is the subscription or nothing.
- `claude` runs in `~/.lmg` and keeps its auto memory in `~/.lmg/memory`, set with `--settings {"autoMemoryDirectory": ...}`, so every proxy session shares one memory whatever project the client is in.
- The client's system prompt reaches `claude` as a file, `system-ID.md` in `~/.lmg/prompts-PORT`, under the folder `claude` runs in: one a session, so two at once never share one, deleted when the session ends, and swept when the gateway on that port next starts, since `stop` kills it before it can clean up. On the command line, a prompt over 32,767 characters, such as OpenCode's, could not start the child on Windows. Recognized Pi prompts use that file as an append to Claude Code's preset rather than as a full replacement; other prompts keep the original file mode.
- On Windows the SDK will not run npm's `claude.cmd` shim, so the gateway uses the `claude.exe` inside the npm package. `CLAUDE_CLI` overrides the path.

### Pi prompts

On the Claude route only, the gateway recognizes Pi's standard tagged system prompt by its complete generated preamble followed by the `<tools>`, `<rules>`, and Pi `<docs>` sections in order. It replaces the generated preamble, tool inventory, and documentation routing with Claude Code's preset. Rules remain because they can contain user or tool guidelines; project instructions, skills, working-directory context, custom sections, and text wrapped around the Pi prompt are preserved. Client tools still run through MCP, and Claude Code's native tools, settings, and skills stay disabled.

Unlike the Pi bridge, the gateway receives no structured prompt metadata, so this is a rendered-text adaptation, not an exact copy of the bridge's projection. Custom, older, or rewritten prompts that do not match the standard scaffold pass through unchanged, as do standalone summary prompts and every OpenAI request. Session matching continues to use the original client prompt. Anthropic's acceptance and billing remain determined by its service; adaptation is not a guarantee of subscription eligibility.

### Limits

- It binds to `127.0.0.1` only. Anything on this machine that can read `.env` can use it.
- Anthropic bills requests it identifies as a third-party app to Extra Usage instead of plan limits. An unadapted full agent harness prompt, such as Pi's, can be refused with `Third-party apps now draw from extra usage` unless Extra Usage is on; the recognized Pi scaffold is adapted as described above. The check reads the prompt's content, not the fact that it is another agent's: Pi's prompt is refused for the routing list in its `<docs>` block (extensions, themes, skills, ..., `pi packages`, MCP servers), where no one item fails alone and dropping `custom providers` or `pi packages` from that line alone lets it pass, while its opening line naming Pi passes by itself; Hermes Agent's own 15,000-character prompt, which names Nous Research, passed on Haiku 4.5 and Opus 5.5 in September 2026, as did Codex's. OpenCode's main prompt is refused like Pi's, while its short session-title prompt passes.
- A conversation with no live Claude session (after a restart, or a client that edited its history) is replayed to a fresh session as a transcript.
- Claude's thinking is not passed back. The Codex backend drops fields it does not accept, such as `temperature`.

### Request log

`~/.lmg/config.toml` (or `--config PATH`) sets it up:

```toml
[log]
file = "logs/{session}.jsonl"  # relative to config.toml; leave out to log nothing
level = "debug"
source = "sent"         # received (default) or sent, after provider adaptation
```

`{session}` expands to `yy-mm-dd hh-mm-ss session-id`, for example `26-10-03 14-30-05 session-a1b2c3.jsonl`. Each conversation's system, request, and trace entries share one file; later turns append to it. A fixed filename such as `logs/log.jsonl` keeps the old combined log. Existing logs are not moved or split.

Send a stable `X-Session-ID` or `Session-ID` header, or `metadata.session_id`, for reliable conversation grouping. IDs are sanitized for filenames. Without an explicit ID, the gateway matches previously logged conversation-history prefixes and reuses live Claude sessions' generated IDs. Two independent conversations with identical histories cannot be distinguished reliably; ambiguous history matches start a separate file rather than merge them. Conversation lookup is process-local and bounded to 4,096 entries, so edited or truncated histories or evicted entries may start a new file. A gateway restart starts new timestamped files, even for explicit IDs. An explicit ID takes priority over inferred history and provider sessions.

Each level holds everything below it:

| Level | What each line holds |
| --- | --- |
| `off` | Nothing is written. |
| `error` | A refused or failed request: `api`, `model`, `stream`, `seconds`, `usage` and the `error`. |
| `info` | Every request, with the same fields. The default when `level` is left out. |
| `debug` | Also `input` and `output`, the reply: the Responses output items, or the Chat Completions assistant message with any tool calls. When the received conversation has only one non-system message, the system prompt and the available tools are written separately with `kind = "system"`, never inside the request entry. The entry is written when either is present; `tools` is left out when there are none. |
| `trace` | Also one line for every whole request body (`kind` `responses` or `chat` for received content, `sent` for provider-facing content) and every event the model sent (`kind` `event`). |

`source = "received"` preserves the client's last input message and original system prompt. `source = "sent"` logs the actual provider-facing input: Claude's fetched message blocks or resumed tool results, including transcript replay, or Codex's normalized last input. Claude system entries contain the exact custom prompt text, or an object naming the `claude_code` preset and its exact adapted `append` text. Received `tools` are the client's tool list as sent. Sent `tools` are what the provider got: for Claude, the MCP tools as `name` (with the `mcp__c__` prefix), `description`, and `input_schema`, with any dropped tool types left out; for Codex, the `tools` field of the posted body. The SDK's built-in preset text is not available to the gateway; this logs what the gateway supplies, not an internal model request reconstructed from the SDK. Requests refused before anything is sent have a null input and no system entry in sent mode.

A request's line has `kind` `request`. Image data URLs are cut to their type and size at every level. The key is never logged, but from `debug` up prompts and replies are, in full; `*.jsonl` is gitignored. Settings are read at startup, so a change needs a restart.

### Smoke test

With the gateway running:

```bash
uv run tools/smoke.py anthropic/haiku [image.jpg]
uv run tools/smoke.py openai/gpt-6-luna [image.jpg]
```

It checks a client-run tool round trip, a follow-up and streaming on both APIs, and an image if one is given.

## Jobs

```bash
lmg ask agy|claude|codex read|edit FOLDER [ID]   # FOLDER/task.md as a job, or a follow-up to the one ID names
lmg ask review SHA FOLDER                        # codex exec review of one commit, its answer in FOLDER
```

| Option | What it does |
| --- | --- |
| `--model NAME` | The harness's model, instead of its default: `gemini-3.8-flash-medium`, `claude-opus-5-5`, `gpt-6.1-sol`. |
| `--timeout T` | agy's print timeout, `3600s` unless given. |
| `--force` | Run an edit job in a repository's main checkout, which is refused without it. |

A job is a folder of your own with a `task.md` in it (`TASK.md` will do). `lmg ask` makes no worktree. If you want one, make it first and put the folder inside it:

```bash
git worktree add ../wt-fix -b fix
mkdir ../wt-fix/jobs/fix-ledger        # then write ../wt-fix/jobs/fix-ledger/task.md
lmg ask codex edit ../wt-fix/jobs/fix-ledger
```

1. `lmg ask` finds the folder's `task.md` and checks the harness. The harness runs at the top of the git repository the folder is in, or in the folder itself if it is in none, so a folder inside a worktree gets that whole worktree to work in. An edit job whose folder is in a repository's main checkout is refused unless given `--force`, since it would change your real files.
2. It takes a reading as `before: HARNESS NAME [pid N]`, NAME being the folder's name, with `(resumed)` after it on a follow-up, before anything is written, so a reading that cannot be written leaves no run half made. The pid pairs it with this job's own `after:` reading.
3. Run N claims its number by making `responseN.jsonl` (two runs started at once each get a number of their own), copies `task.md` to `taskN.md`, and runs the harness told to read `taskN.md` by its full path and that its final message is saved as `responseN.md`. `task.md` is left in place, so you can edit it for the next run while this one goes. Stopped with Ctrl-C, the harness is killed and the job reads what it streamed as a run stopped part way.
4. The harness's final message is the answer, `responseN.md`, written by `lmg ask` rather than the agent, which in read mode cannot write. Its raw output is `responseN.jsonl`, and Codex's log `responseN.log`. The raw output is streamed as the job runs, so a run stopped part way still has the session id, which is printed with the follow-up command.
5. It takes the `after:` reading, and one row goes to `~/.lmg/token-ledger.csv`: time, job, repository, model, uncached input, cache reads, cache writes, output, and the public-API cost.

A folder after a job and one follow-up:

```
task.md          # yours to edit
task1.md         # what run 1 was told
response1.md
response1.jsonl
task2.md
response2.md
response2.jsonl
```

A follow-up is the same folder, with `task.md` edited, plus the id: `lmg ask codex edit ../wt-fix/jobs/fix-ledger <id>`. It may change mode. The agent is told that `taskN.md` holds a new task, a file it has not read, because a session told to read the same file again answered the old task from memory. An agy or Codex follow-up books only its own tokens: their counts run over the whole session, so the last earlier run of the same session in the folder is taken off it.

A review needs a folder in the repository to review, and numbers its `responseN` files in it the same way.

A folder inside a repository shows up there as untracked files. Ignore it in that repository's `.gitignore`, or keep the folder in a worktree you throw away.

### Modes

`read` lets the agent read and search files, and nothing else. `edit` bypasses every permission prompt and sandbox.

| Harness | `read` | `edit` |
| --- | --- | --- |
| agy | No bypass flag, so headless agy denies every write and command. A denial ends the run with no answer, and `lmg ask` says so. | `--dangerously-skip-permissions` |
| claude | Only `Read`, `Grep` and `Glob`, and no MCP servers. Hooks in your settings or the repository's still run. | `--dangerously-skip-permissions` |
| codex | The read-only sandbox, where it may still run commands to read with. | `--dangerously-bypass-approvals-and-sandbox` |

Where a job runs is where it starts, not a fence. An edit job can reach anything your account can, and a read job can read files outside it.

A new worktree holds what git tracks and nothing else: no `.venv`, no `node_modules`, no gitignored config or keys. An edit job sets up what it needs, such as the `.venv` a first `uv run` makes from `uv.lock`. A read job cannot, so it cannot run the tests.

### Stopping a job

On Windows every process `lmg ask` starts dies with it, however it is stopped: it puts itself in a job object that kills its members when it exits. Elsewhere the harness runs in its own process group, which is killed when the harness ends and when `lmg ask` exits on a signal it can catch; a `kill -9` of `lmg ask` leaves the group running. A plan reading that runs past its timeout has its whole process tree killed, since on Windows an npm-installed CLI is a `.cmd` shim whose real process is a grandchild.

## Reading the windows

```
$ lmg usage
Now 6:17 AM on 30 Sep

| window                  | left | spare | task cost | tasks left | resets                          |
| ----------------------- | ---: | ----: | --------: | ---------: | ------------------------------- |
| Claude 5h               |  93% |  +22% |      0.4% |        232 | in 3h32m, at 9:50 AM            |
| Codex week              |  59% |  +12% |      0.4% |        147 | in 3d6h, at 1:15 PM on 03 Oct   |
| agy Gemini 5h           |  95% |  +30% |        5% |         19 | in 3h16m, at 9:33 AM            |
| agy Claude and GPT week |  68% |  -15% |         - |          - | in 5d19h, at 2:11 AM on 06 Oct  |
```

Spare is what is left less what would be left had the window been spent evenly over its length: +12% is that much ahead of pace, -15% that much behind. Task cost is what one task of that plan's harness has taken from the window on average, and tasks left is what is left over it; `lmg usage tasks` shows what the cost is measured from. agy's Claude and GPT group shows `-`, since the ledger keeps agy's Gemini group only. A 5-hour window that runs out may reset before the weekly one does, so which one limits you is read off when each resets. A reset today gives only its time; a later one gives its date too. A plan that could not be read is named under the table with the reason. A window its plan does not report is not listed: Claude's endpoint does not always give a weekly figure.

Claude's model-specific weekly Fable limit is listed as `Claude Fable week`, separately from the all-models `Claude week` limit when reported. It is also recorded for `lmg usage tasks`; writing a reading extends older ledger headers while preserving their rows. Fable is not included in `lmg usage worth`, since its model-to-call mapping is not verified.

## Measuring a task

`lmg ask` takes its own readings. For a task of your own, take a reading before it and one after, each under the task's name:

```bash
lmg usage "before: pi design review"
# ... the task ...
lmg usage "after: pi design review"
```

A note is a word, a colon and what it is about; `before:` and `after:` are the two a task is paired from. Both rows go to `~/.lmg/usage-ledger.csv`. A task's cost is only what its windows lost between its own two readings, so other spending before or after it does not count. A window that went up was reset during the task, so it is left out rather than counted as a gain.

Tasks are grouped by the harness that ran them, the first word of their name. An `lmg ask` job is `agy`, `claude` or `codex`, and a review is `codex review SHA`, so it counts as Codex. Each spends the plan of its own name. A task of your own may be named anything, such as `pi x`: it has no cost column of its own, but `lmg usage tasks` lists it. It is taken to spend every plan whose window fell while it ran, and a harness task it overlaps on one of those plans is left out of that harness's cost, since the two cannot be told apart. In the task list an `agy`, `claude` or `codex` task shows only its own plan's windows, since a fall on another plan is somebody else's spending.

The two ledgers are in `~/.lmg`, outside the checkout: they are your records, not the tool's.

## What `lmg usage worth` measures

Between two ledger rows where a window's figure fell, the calls made are what that many points cost, so a point's price is the calls made during every fall the ledger saw divided by the points they took, and a hundred points are the window. One gap is rough, because the ledger keeps whole percents; the sum over many is fair. It prints that price, the whole window, and what has gone since the window opened two ways: the calls since then, priced, and the points lost times the price. The two differ by what the price misjudges, or by calls these records do not hold. A window with no fall to measure, or a call in one from a model with no price, is `-`.

## Where `lmg usage calls` and `lmg usage worth` read

A plan's limit counts every call made on it, by whatever made it, so both read every record the clients leave:

- Claude: `~/.claude/projects`, Claude Code's transcripts, including calls made through Pi's claude-bridge.
- Codex: `~/.codex/sessions` (or `$CODEX_HOME/sessions`), and Pi's `openai-codex` provider in `~/.pi/agent/sessions`.
- agy: `~/.gemini/antigravity-cli/conversations`.
- Pi's compactions and pi-hermes-memory's reviews, which are estimated where nothing records them and marked `(est.)`.

Prices are in `src/llm_gateway/prices.csv`, per million tokens, with the page each came from and the date it was checked. A model not in it is listed as unpriced rather than counted as free.

## Tests

```bash
uv run python -m pytest -q
```

The tests run no harness and ask no plan. The job test replaces the harness with a stub and uses a throwaway repository.
