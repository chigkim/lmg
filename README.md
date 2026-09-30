# llm-gateway

One command, `lmg`, for three jobs: a small LLM gateway on loopback, jobs handed to agy, Claude Code or Codex in their own git worktree, and what each subscription plan has left.

| Command | What it does |
| --- | --- |
| `lmg proxy [run\|start\|stop\|restart\|status]` | The gateway. It speaks OpenAI's Responses and Chat Completions APIs, and answers them from a Claude or ChatGPT subscription instead of an API key. |
| `lmg ask HARNESS MODE PROMPT [ID]` | Runs one job, or follows up the one `ID` names, with a usage reading either side, and prints the `lmg usage` table when it ends. `lmg ask review SHA`, `lmg ask clean HARNESS NAME` and `lmg ask list` are its other forms. |
| `lmg usage [NOTE]` | One table of every plan's 5-hour and weekly windows, agy's two model groups each on their own rows: percent left, the spare against an even pace (+ ahead, - behind), and when each resets. With a note, `word: what` such as `"before: session x"`, it also adds a row to the ledger, which keeps agy's Gemini group only. Any other word is refused, so a mistyped form writes nothing. |
| `lmg usage calls [--hours N \| --since "YYYY-MM-DD HH:MM"]` | Every call each plan was spent on, read from the clients' own records, grouped by who made it, with tokens and what they would cost on the public API. |
| `lmg usage pace [--fresh] [--brief]` | What each kind of task costs a window, from paired `before:` and `after:` rows in the ledger, and how many more fit, paced on the ledger's latest reading. The windows themselves are `lmg usage`'s. |
| `lmg usage worth` | What each window is worth in dollars, sized from the ledger and the clients' records, and how much of it is spent. It takes no options, and it is slower than the rest (about 9 seconds), since it reads every record back to a week before the ledger's first row. |

Three things run a model: `lmg proxy`, every form of `lmg ask` but `clean` and `list` (`review` included), and the Haiku fallback of `lmg usage` described below. `lmg usage` asks `codex app-server` for Codex's limits, reads agy's quota out of the log `agy models` writes, and asks the endpoint Claude Code's own `/usage` reads, with the CLI's login token held in memory for that one request and never printed or stored; a redirect is refused rather than followed, because following it would send the token on. When that request cannot be made or fails -- an expired token, no credentials file, no network, or an error status -- it falls back to a one-word Haiku call, which costs a little and refreshes the login.

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
      --config PATH:                 # default config.toml
  ask:                               # a job in its own git worktree
    agy|claude|codex:
      read|edit:
        PROMPT:                      # the prompt file; the job is named after it
          ID:                        # optional: follow up that session in the same worktree
    review:
      SHA:                           # codex exec review of one commit
    clean:
      agy|claude|codex:
        NAME:                        # remove the job's worktree and branch
    list:                            # every open job: its runs, answer, changes and id
    options:
      --repo PATH:                   # default: the repository the current folder is in
      --home PATH:                   # where worktrees/ and runs/ go; default homes.toml's, else PARENT/.lmg/REPO
      --model NAME:                  # default gemini-3.8-flash-medium, claude-opus-5-5 or gpt-6-sol
      --timeout T:                   # agy's print timeout; default 3600s
      --force:                       # clean only: remove it even with unmerged work
  usage:                             # every window in one table: percent left, spare, resets
    NOTE:                            # optional: also add a ledger row; "word: what", e.g. "before: session x"
    calls:                           # every call each plan was spent on, and its API cost
      --hours N:                     # default 5
      --since "YYYY-MM-DD HH:MM":    # local time; overrides --hours
    pace:                            # what each kind of task costs, and how many more fit
      --fresh:                       # take a reading first
      --brief:                       # costs and room, no task table
    worth:                           # each window's size and use, in dollars
```

## Install

Python 3.14. Install it editable, because the ledgers are written beside the scripts and a normal install would put them inside uv's tool folder, where the next reinstall deletes them:

```bash
uv tool install -e .
```

`lmg` then runs from any folder. Edits to the checkout take effect at once; a change to `pyproject.toml` needs `uv tool install -e --reinstall .`.

A plan whose client is not on `PATH` (`claude`, `codex`, `agy`) is reported as one that could not be asked, and the others are still read. `lmg ask` needs the harness it runs on `PATH`, and `git`.

## The proxy

```bash
uv sync
uv run lmg proxy start      # in the background, on http://127.0.0.1:8797/v1
uv run lmg proxy status
uv run lmg proxy restart    # after changing config.toml
uv run lmg proxy stop
uv run lmg proxy run        # or in this console, until Ctrl-C
```

Every command takes `--port N` (default 8797) and `--config PATH` (default `config.toml`). Installed as a tool, `lmg proxy` runs without `uv run`; it still reads `config.toml` and `.env` from the folder you run `start` in.

The daemon is known by its port. Its pid and console output are in `logs/PORT.pid` and `logs/PORT.log` in this project, found from the script's own location, so `stop` and `status` work from any folder. `start` returns once the port accepts connections, or reports the exit and points at the log. `stop` kills the whole process tree, Claude children included. `status` exits 0 when running and 3 when not.

Point any OpenAI-compatible client at `http://127.0.0.1:8797/v1`, with `GATEWAY_API_KEY` as its API key. It serves `/v1/responses`, `/v1/chat/completions` and `/v1/models`.

### API key

Every request needs `Authorization: Bearer KEY`, which is what an OpenAI client sends for its API key. The gateway reads `GATEWAY_API_KEY` from the environment first, then from the first `.env` found from the working directory upward. With neither, the first run generates a random key, writes it to `.env` in the working directory (appending if that file exists without one) and says so, without printing the key. Give a client the value from that file. `.env` is gitignored. The key is compared in constant time and never logged.

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

Both APIs accept images, as data URLs or remote URLs. The Claude side fetches a remote image and sends it as base64.

### Credentials

- The Codex token is read from `auth.json` on every request and never refreshed, written, logged or printed. When it expires, run any `codex` command and retry.
- Claude's login is never touched: the SDK's `claude` child uses its own. `ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_TOKEN` and `ANTHROPIC_BASE_URL` are dropped from the environment at startup, so the Claude side is the subscription or nothing.
- On Windows the SDK will not run npm's `claude.cmd` shim, so the gateway uses the `claude.exe` inside the npm package. `CLAUDE_CLI` overrides the path.

### Limits

- It binds to `127.0.0.1` only. Anything on this machine that can read `.env` can use it.
- Anthropic bills requests it identifies as a third-party app to Extra Usage instead of plan limits. A full agent harness sending its own system prompt, such as Pi, is refused with `Third-party apps now draw from extra usage` unless Extra Usage is on.
- A conversation with no live Claude session (after a restart, or a client that edited its history) is replayed to a fresh session as a transcript.
- Claude's thinking is not passed back. The Codex backend drops fields it does not accept, such as `temperature`.

### Request log

`config.toml` (read from the working directory, or `--config PATH`) sets it up:

```toml
[log]
file = "logs/log.jsonl"   # relative to config.toml; leave out to log nothing
level = "debug"
```

Each level holds everything below it:

| Level | What each line holds |
| --- | --- |
| `off` | Nothing is written. |
| `error` | A refused or failed request: `api`, `model`, `stream`, `seconds`, `usage` and the `error`. |
| `info` | Every request, with the same fields. The default when `level` is left out. |
| `debug` | Also `input`, the last message the client sent (a user message or a tool result), and `output`, the reply: the Responses output items, or the Chat Completions assistant message with any tool calls. |
| `trace` | Also one line for every whole request body (`kind` `responses` or `chat`) and every event the model sent (`kind` `event`). |

A request's line has `kind` `request`. Image data URLs are cut to their type and size at every level. The key is never logged, but from `debug` up prompts and replies are, in full; `*.jsonl` is gitignored. The level is read at startup, so a change needs a restart.

### Smoke test

With the gateway running:

```bash
uv run tools/smoke.py anthropic/haiku [image.jpg]
uv run tools/smoke.py openai/gpt-6-luna [image.jpg]
```

It checks a client-run tool round trip, a follow-up and streaming on both APIs, and an image if one is given.

## Jobs

```bash
lmg ask agy|claude|codex read|edit PROMPT [ID]   # a job, or a follow-up to the one ID names
lmg ask review SHA                               # codex exec review of one commit
lmg ask clean agy|claude|codex NAME [--force]    # remove a job's worktree and branch, if nothing is lost
lmg ask list                                     # every open job: its runs, answer, changes and id
```

| Option | What it does |
| --- | --- |
| `--repo PATH` | The repository, instead of the one the current folder is in. |
| `--home PATH` | Where `worktrees/` and `runs/` go, instead of the repository's home. |
| `--model NAME` | The harness's model, instead of its default: `gemini-3.8-flash-medium`, `claude-opus-5-5`, `gpt-6-sol`. |
| `--timeout T` | agy's print timeout, `3600s` unless given. |
| `--force` | `clean` only: remove the worktree even though it holds uncommitted files or unmerged commits. |

A repository's home is `PARENT/.lmg/REPO`, a folder beside it, so repositories that share a parent folder do not share jobs. `src/llm_gateway/homes.toml`, gitignored, names another for a repository; a relative home is taken from the repository:

```toml
[homes]
"D:/code/game" = ".."    # D:/code/worktrees and D:/code/runs
```

A job is named after its prompt file, less `.md` and a trailing `-prompt`: `fix-ledger-prompt.md` is the job `fix-ledger`.

1. `lmg ask` makes `HOME/worktrees/HARNESS-NAME`, a worktree on a branch of that name at `HEAD`, or reuses it for a follow-up. A new job whose worktree is still there from an earlier one is refused, rather than handed that job's changes.
2. It moves the prompt in as `TASK.md` and keeps a numbered copy as `HOME/runs/HARNESS/NAME-N.task.md`.
3. It takes a reading as `before: HARNESS NAME`, runs the harness in the worktree told only to read `TASK.md`, and takes the `after:` reading.
4. The harness's last message is the answer, `HOME/runs/HARNESS/NAME-N.md`, beside its raw output, `NAME-N.jsonl`. The session id to follow up with is printed. The raw output is streamed as the job runs, so a job stopped part way still has its id, and `lmg ask list` shows it.
5. One row goes to `src/llm_gateway/token-ledger.csv`: time, job, repository, model, uncached input, cache reads, cache writes, output, and the public-API cost.

Edit work is left as an uncommitted diff in the worktree for you to review, test and merge. `lmg ask clean HARNESS NAME` removes the worktree and its branch, and refuses while it holds uncommitted files (`TASK.md` aside), commits that `HEAD` has not got, or gitignored files the job made, unless given `--force`. Git deletes a worktree's ignored files with it, so an ignored file is left out of the count only where the main checkout has the same path, a `.venv` or a `__pycache__`: a local copy of something rebuilt at will.

A follow-up is a new prompt file of the same name plus the id: `lmg ask codex edit fix-ledger-prompt.md <id>`. It runs in the same worktree and may change mode. The agent is told that `TASK.md` now holds a new task, because a session told only to read it again answered the old one from memory. An agy follow-up books only its own tokens: agy's count runs over the whole conversation, so the last earlier run of the same conversation is taken off it.

### Modes

`read` lets the agent read and search files, and nothing else. `edit` bypasses every permission prompt and sandbox.

| Harness | `read` | `edit` |
| --- | --- | --- |
| agy | No bypass flag, so headless agy denies every write and command. A denial ends the run with no answer, and `lmg ask` says so. | `--dangerously-skip-permissions` |
| claude | Only `Read`, `Grep` and `Glob`, and no MCP servers. Hooks in your settings or the repository's still run. | `--dangerously-skip-permissions` |
| codex | The read-only sandbox, where it may still run commands to read with. | `--dangerously-bypass-approvals-and-sandbox` |

A worktree is where a job starts, not a fence. An edit job can reach anything your account can, and a read job can read files outside the worktree.

A new worktree holds what git tracks and nothing else: no `.venv`, no `node_modules`, no gitignored config or keys. An edit job sets up what it needs, such as the `.venv` a first `uv run` makes from `uv.lock`. A read job cannot, so it cannot run the tests.

### Stopping a job

On Windows every process `lmg ask` starts dies with it, however it is stopped: it puts itself in a job object that kills its members when it exits. Elsewhere the harness runs in its own process group, which is killed when the harness ends and when `lmg ask` exits on a signal it can catch; a `kill -9` of `lmg ask` leaves the group running. A plan reading that runs past its timeout has its whole process tree killed, since on Windows an npm-installed CLI is a `.cmd` shim whose real process is a grandchild.

## Reading the windows

```
$ lmg usage
Now 6:17 AM on 30 Sep

| window                  | left | spare | resets                          |
| ----------------------- | ---: | ----: | ------------------------------- |
| Claude 5h               |  93% |  +22% | in 3h32m, at 9:50 AM            |
| Codex week              |  59% |  +12% | in 3d6h, at 1:15 PM on 03 Oct   |
| agy Gemini 5h           |  95% |  +30% | in 3h16m, at 9:33 AM            |
| agy Claude and GPT week |  68% |  -15% | in 5d19h, at 2:11 AM on 06 Oct  |
```

Spare is what is left less what would be left had the window been spent evenly over its length: +12% is that much ahead of pace, -15% that much behind. A reset today gives only its time; a later one gives its date too. A plan that could not be read is named under the table with the reason. A window its plan does not report is not listed: Claude's endpoint does not always give a weekly figure.

## Measuring a task

`lmg ask` takes its own readings. For a task of your own, take a reading before it and one after, each under the task's name:

```bash
lmg usage "before: session design review"
# ... the task ...
lmg usage "after: session design review"
```

A note is a word, a colon and what it is about; `before:` and `after:` are the two `lmg usage pace` pairs, and `lmg usage pace --fresh` writes `reading: pace`. Both rows go to `src/llm_gateway/usage-ledger.csv`. A task's cost is only what its windows lost between its own two readings, so other spending before or after it does not count. A window that went up was reset during the task, so it is left out rather than counted as a gain.

`lmg usage pace` groups tasks by the first word of their name, so an `lmg ask` job is of the kind `agy`, `claude` or `codex`. `src/llm_gateway/pace.toml` says which window each kind is paced on, and the plan in that window's name is the one the kind spends:

```toml
[paced]
session = "claude_5h"
agy = "agy_week"
claude = "claude_5h"
codex = "codex_week"
review = "codex_week"
```

A window is `claude`, `codex` or `agy`, then `_5h` or `_week`. Without the file, `lmg usage pace` still lists every task but costs and paces none.

The two ledgers and `pace.toml` are gitignored: they are your records, not the tool's.

## What `lmg usage worth` measures

Between two ledger rows where a window's figure fell, the calls made are what that many points cost, so a point's price is the calls made during every fall the ledger saw divided by the points they took, and a hundred points are the window. One gap is rough, because the ledger keeps whole percents; the sum over many is fair. It prints that price, the whole window, and what has gone since the window opened two ways: the calls since then, priced, and the points lost times the price. The two differ by what the price misjudges, or by calls these records do not hold. A window with no fall to measure, or a call in one from a model with no price, is `-`.

## Where `lmg usage calls` and `lmg usage worth` read

A plan's limit counts every call made on it, by whatever made it, so both read every record the clients leave:

- Claude: `~/.claude/projects`, Claude Code's transcripts, including calls made through Pi's claude-bridge.
- Codex: `~/.codex/sessions`, and Pi's `openai-codex` provider in `~/.pi/agent/sessions`.
- agy: `~/.gemini/antigravity-cli/conversations`.
- Pi's compactions and pi-hermes-memory's reviews, which are estimated where nothing records them and marked `(est.)`.

Prices are in `src/llm_gateway/prices.csv`, per million tokens, with the page each came from and the date it was checked. A model not in it is listed as unpriced rather than counted as free.

## Tests

```bash
uv run python -m pytest -q
```

The tests run no harness and ask no plan. The job test replaces the harness with a stub and uses a throwaway repository.
