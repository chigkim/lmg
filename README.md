# llm-gateway

One command, `lmg`, for three jobs: a small LLM gateway on loopback, jobs handed to agy, Claude Code or Codex in their own git worktree, and what each subscription plan has left.

| Command | What it does |
| --- | --- |
| `lmg proxy [run\|start\|stop\|restart\|status\|models]` | The gateway. It speaks OpenAI's Responses and Chat Completions APIs, and answers them from a Claude or ChatGPT subscription instead of an API key. |
| `lmg ask HARNESS MODE PROMPT [ID]` | Runs one job, or follows up the one `ID` names, with a usage reading either side, and prints the `lmg usage` table when it ends. `lmg ask review SHA`, `lmg ask clean HARNESS NAME` and `lmg ask list` are its other forms. |
| `lmg usage [NOTE]` | One table of every plan's 5-hour and weekly windows, agy's two model groups each on their own rows: percent left, the spare against an even pace (+ ahead, - behind), what one task of its harness costs it on average, how many more tasks that leaves, and when each resets. With a note, `word: what` such as `"before: pi x"`, it also adds a row to the ledger, which keeps agy's Gemini group only. Any other word is refused, so a mistyped form writes nothing. `lmg usage --help` lists every form, and each form takes `--help` too. |
| `lmg usage calls [--hours N \| --since "YYYY-MM-DD HH:MM"]` | Every call each plan was spent on, read from the clients' own records, grouped by who made it (`claude code`, `claude -p` and `pi bridge` for Claude; `codex` and `codex exec` for Codex), with tokens and what they would cost on the public API. |
| `lmg usage tasks` | Every task in the ledger with what it took from each window, then what a task costs each window and how many tasks that is measured from. A cost from fewer than five tasks is marked `~`, here and in `lmg usage`. |
| `lmg usage worth` | What each window is worth in dollars, sized from the ledger and the clients' records, and how much of it is spent. It takes no options but `--help`, and it is slower than the rest (about 9 seconds), since it reads every record back to a week before the ledger's first row. |

Three things run a model: `lmg proxy`, every form of `lmg ask` but `clean` and `list` (`review` included), and the Haiku fallback of `lmg usage` described below. `lmg usage` asks `codex app-server` for Codex's limits, reads agy's quota out of the log `agy models` writes, and asks the endpoint Claude Code's own `/usage` reads, with the CLI's login token held in memory for that one request and never printed or stored; a redirect is refused rather than followed, because following it would send the token on. When that request cannot be made or fails -- an expired token, no credentials file, no network, an error status, a broken reply or one of another shape -- it falls back to a one-word Haiku call, which costs a little and refreshes the login.

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
      --home PATH:                   # where worktrees/ and runs/ go; default homes.toml's, else jobs/REPO here
      --model NAME:                  # default gemini-3.8-flash-medium, claude-opus-5-5 or gpt-6.1-sol
      --timeout T:                   # agy's print timeout; default 3600s
      --force:                       # clean only: remove it even with unmerged work
  usage:                             # every window in one table: percent left, spare, task cost, tasks left, resets
    NOTE:                            # optional: also add a ledger row; "word: what", e.g. "before: pi x"
    calls:                           # every call each plan was spent on, and its API cost
      --hours N:                     # default 5
      --since "YYYY-MM-DD HH:MM":    # local time; overrides --hours
    tasks:                           # every task, then what a task costs each window
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

Both APIs accept images, as data URLs or remote URLs. The Claude side fetches a remote image and sends it as base64. It fetches only from the public internet: a URL must be `http` or `https`, a host that is or resolves to a loopback, private, link-local or other non-public address is refused, each redirect is checked again up to five, and an image over 20 MB is refused. A tool result whose image cannot be fetched is refused with the session still waiting on that call, so the client can send it again.

### Credentials

- The Codex token is read from `auth.json` on every request and never refreshed, written, logged or printed. When it expires, run any `codex` command and retry.
- Claude's login is never touched: the SDK's `claude` child uses its own. `ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_TOKEN` and `ANTHROPIC_BASE_URL` are dropped from the environment at startup, so the Claude side is the subscription or nothing.
- The client's system prompt reaches `claude` as a file, `system-ID.md` in `llm-gateway/prompts-PORT` under the temp folder: one a session, so two at once never share one, deleted when the session ends, and swept when the gateway on that port next starts, since `stop` kills it before it can clean up. On the command line, a prompt over 32,767 characters, such as OpenCode's, could not start the child on Windows.
- On Windows the SDK will not run npm's `claude.cmd` shim, so the gateway uses the `claude.exe` inside the npm package. `CLAUDE_CLI` overrides the path.

### Limits

- It binds to `127.0.0.1` only. Anything on this machine that can read `.env` can use it.
- Anthropic bills requests it identifies as a third-party app to Extra Usage instead of plan limits. A full agent harness sending its own system prompt, such as Pi, is refused with `Third-party apps now draw from extra usage` unless Extra Usage is on. The check reads the prompt's content, not the fact that it is another agent's: Pi's prompt is refused for the routing list in its `<docs>` block (extensions, themes, skills, ..., `pi packages`, MCP servers), where no one item fails alone and dropping `custom providers` or `pi packages` from that line alone lets it pass, while its opening line naming Pi passes by itself; Hermes Agent's own 15,000-character prompt, which names Nous Research, passed on Haiku 4.5 and Opus 5.5 in September 2026, as did Codex's. OpenCode's main prompt is refused like Pi's, while its short session-title prompt passes.
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
| `--model NAME` | The harness's model, instead of its default: `gemini-3.8-flash-medium`, `claude-opus-5-5`, `gpt-6.1-sol`. |
| `--timeout T` | agy's print timeout, `3600s` unless given. |
| `--force` | `clean` only: remove the worktree even though it holds uncommitted files or unmerged commits. |

A repository's home is `jobs/REPO` in this checkout, gitignored, so every repository's jobs are in one place beside the tool, and `pytest` here collects only `tests/`. Two repositories of the same folder name would share a home; `src/llm_gateway/homes.toml`, gitignored, names another for a repository, and a relative home is taken from the repository:

```toml
[homes]
"D:/code/game" = ".."    # D:/code/worktrees and D:/code/runs
```

A job is named after its prompt file, less `.md` and a trailing `-prompt`: `fix-ledger-prompt.md` is the job `fix-ledger`.

1. `lmg ask` checks the harness and the worktree: `HOME/worktrees/HARNESS-NAME`, a worktree on a branch of that name at `HEAD`, reused by a follow-up. A new job whose worktree is still there from an earlier one is refused, rather than handed that job's changes.
2. It takes a reading as `before: HARNESS NAME [pid N]`, with `(resumed)` after the name on a follow-up, before anything is made, so a reading that cannot be written leaves no job half made. The pid pairs it with this job's own `after:` reading.
3. It makes the worktree if it is new, moves the prompt in as `TASK.md`, keeps a numbered copy as `HOME/runs/HARNESS/NAME-N.task.md` (two runs started at once each claim a number of their own), runs the harness in the worktree told only to read `TASK.md`, and takes the `after:` reading. A repository that tracks a `TASK.md` of its own is refused a job, since the prompt would be written over it. Stopped with Ctrl-C, the harness is killed and the job reads what it streamed as a run stopped part way.
4. The harness's last message is the answer, `HOME/runs/HARNESS/NAME-N.md`, beside its raw output, `NAME-N.jsonl`. The session id to follow up with is printed. The raw output is streamed as the job runs, so a job stopped part way still has its id, and `lmg ask list` shows it, or the one an earlier run named when the latest said none.
5. One row goes to `src/llm_gateway/token-ledger.csv`: time, job, repository, model, uncached input, cache reads, cache writes, output, and the public-API cost.

A read job leaves nothing to merge, so when it ends its worktree and branch are removed, unless `clean` would refuse to, and then it says why and keeps them. A follow-up to a read job makes the worktree again at `HEAD`; the harness resumes the session by its id, not by the folder. Edit work is left as an uncommitted diff in the worktree for you to review, test and merge. `lmg ask clean HARNESS NAME` removes the worktree and its branch, and refuses while it holds uncommitted files (`TASK.md` aside), commits that `HEAD` has not got, or gitignored files the job made, unless given `--force`. It counts commits on the branch and wherever the worktree's `HEAD` is, so work an agent committed on a detached `HEAD` is not lost, and a git call that fails refuses rather than reading as nothing to lose. Git deletes a worktree's ignored files with it, so an ignored file is left out of the count only where the main checkout has the same file, byte for byte -- a file of the same name with other contents is the job's work, and a folder both have, such as `logs/`, is looked into file by file -- or where it lies in a virtual environment, a cache such as `.pytest_cache` or a `__pycache__`: a local copy of something rebuilt at will.

A follow-up is a new prompt file of the same name plus the id: `lmg ask codex edit fix-ledger-prompt.md <id>`. It runs in the same worktree, made again if a read job's was removed, and may change mode. The agent is told that `TASK.md` now holds a new task, because a session told only to read it again answered the old one from memory. An agy follow-up books only its own tokens: agy's count runs over the whole conversation, so the last earlier run of the same conversation is taken off it.

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

| window                  | left | spare | task cost | tasks left | resets                          |
| ----------------------- | ---: | ----: | --------: | ---------: | ------------------------------- |
| Claude 5h               |  93% |  +22% |      0.4% |        232 | in 3h32m, at 9:50 AM            |
| Codex week              |  59% |  +12% |      0.4% |        147 | in 3d6h, at 1:15 PM on 03 Oct   |
| agy Gemini 5h           |  95% |  +30% |        5% |         19 | in 3h16m, at 9:33 AM            |
| agy Claude and GPT week |  68% |  -15% |         - |          - | in 5d19h, at 2:11 AM on 06 Oct  |
```

Spare is what is left less what would be left had the window been spent evenly over its length: +12% is that much ahead of pace, -15% that much behind. Task cost is what one task of that plan's harness has taken from the window on average, and tasks left is what is left over it; `lmg usage tasks` shows what the cost is measured from. agy's Claude and GPT group shows `-`, since the ledger keeps agy's Gemini group only. A 5-hour window that runs out may reset before the weekly one does, so which one limits you is read off when each resets. A reset today gives only its time; a later one gives its date too. A plan that could not be read is named under the table with the reason. A window its plan does not report is not listed: Claude's endpoint does not always give a weekly figure.

## Measuring a task

`lmg ask` takes its own readings. For a task of your own, take a reading before it and one after, each under the task's name:

```bash
lmg usage "before: pi design review"
# ... the task ...
lmg usage "after: pi design review"
```

A note is a word, a colon and what it is about; `before:` and `after:` are the two a task is paired from. Both rows go to `src/llm_gateway/usage-ledger.csv`. A task's cost is only what its windows lost between its own two readings, so other spending before or after it does not count. A window that went up was reset during the task, so it is left out rather than counted as a gain.

Tasks are grouped by the harness that ran them, the first word of their name. An `lmg ask` job is `agy`, `claude` or `codex`, and a review is `codex review SHA`, so it counts as Codex. Each spends the plan of its own name. A task of your own may be named anything, such as `pi x`: it has no cost column of its own, but `lmg usage tasks` lists it. It is taken to spend every plan whose window fell while it ran, and a harness task it overlaps on one of those plans is left out of that harness's cost, since the two cannot be told apart. In the task list an `agy`, `claude` or `codex` task shows only its own plan's windows, since a fall on another plan is somebody else's spending.

The two ledgers are gitignored: they are your records, not the tool's.

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
