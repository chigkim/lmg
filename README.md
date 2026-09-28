# askai

Hand one job to agy, Claude Code or Codex in its own git worktree of the repository you are in, and watch what the subscription plans have left: how much of each window, what spent it, and how many more tasks of a kind fit before it runs out.

| Command | What it does |
| --- | --- |
| `askai HARNESS MODE PROMPT [ID]` | Runs one job, or follows up the one `ID` names, with a usage reading either side. `review SHA`, `clean HARNESS NAME` and `list` are its other forms. |
| `askai usage [NOTE]` | Each plan's 5-hour and weekly windows: percent left, when each resets, and how that stands against an even pace. With a note, it also adds a row to the ledger. |
| `askai plan [--hours N \| --since "YYYY-MM-DD HH:MM" \| --limits]` | Every call each plan was spent on, read from the clients' own records, grouped by who made it, with tokens and what they would cost on the public API. `--limits` sizes each window in dollars from the ledger. |
| `askai pace [--fresh] [--brief]` | What each kind of task costs a window, from paired `before:` and `after:` rows in the ledger, and how many more fit now. |

It is one command, `askai`. Only its job forms run a model. `askai usage` asks `codex app-server` for Codex's limits, reads agy's quota out of the log `agy models` writes, and asks the endpoint Claude Code's own `/usage` reads, with the CLI's login token held in memory for that one request and never printed or stored. When that request cannot be made or fails -- an expired token, no credentials file, no network, or an error status -- it falls back to a one-word Haiku call, which costs a little and refreshes the login.

## Install

Python 3.11 or later, no dependencies. Install it editable, because the ledgers are written beside the scripts and a normal install would put them inside uv's tool folder, where the next reinstall deletes them:

```bash
uv tool install -e .
```

`askai` then runs from any folder. Edits to the checkout take effect at once; a change to `pyproject.toml` needs `uv tool install -e --reinstall .`.

A plan whose client is not on `PATH` (`claude`, `codex`, `agy`) is reported as one that could not be asked, and the others are still read. `askai` needs the harness it runs on `PATH`, and `git`.

## Running a job

```bash
askai agy|claude|codex read|edit PROMPT [ID]   # a job, or a follow-up to the one ID names
askai review SHA                               # codex exec review of one commit
askai clean agy|claude|codex NAME [--force]    # remove a job's worktree and branch, if nothing is lost
askai list                                     # every open job: its runs, answer, changes and id
```

| Option | What it does |
| --- | --- |
| `--repo PATH` | The repository, instead of the one the current folder is in. |
| `--home PATH` | Where `worktrees/` and `runs/` go, instead of the repository's home. |
| `--model NAME` | The harness's model, instead of its default: `gemini-3.8-flash-medium`, `claude-opus-5-5`, `gpt-6-sol`. |
| `--timeout T` | agy's print timeout, `3600s` unless given. |
| `--force` | `clean` only: remove the worktree even though it holds uncommitted files or unmerged commits. |

A repository's home is `PARENT/.askai/REPO`, a folder beside it, so repositories that share a parent folder do not share jobs. `src/askai/homes.toml`, gitignored, names another for a repository; a relative home is taken from the repository:

```toml
[homes]
"D:/code/game" = ".."    # D:/code/worktrees and D:/code/runs
```

A job is named after its prompt file, less `.md` and a trailing `-prompt`: `fix-ledger-prompt.md` is the job `fix-ledger`.

1. `askai` makes `HOME/worktrees/HARNESS-NAME`, a worktree on a branch of that name at `HEAD`, or reuses it for a follow-up. A new job whose worktree is still there from an earlier one is refused, rather than handed that job's changes.
2. It moves the prompt in as `TASK.md` and keeps a numbered copy as `HOME/runs/HARNESS/NAME-task-N.md`.
3. It takes a reading as `before: HARNESS NAME`, runs the harness in the worktree told only to read `TASK.md`, and takes the `after:` reading.
4. The harness's last message is the answer, `HOME/runs/HARNESS/NAME-N.md`, beside its raw output, `NAME-N.jsonl`. The session id to follow up with is printed. The raw output is streamed as the job runs, so a job stopped part way still has its id, and `askai list` shows it.
5. One row goes to `src/askai/token-ledger.csv`: time, job, repository, model, uncached input, cache reads, cache writes, output, and the public-API cost.

Edit work is left as an uncommitted diff in the worktree for you to review, test and merge. `askai clean HARNESS NAME` removes the worktree and its branch, and refuses while it holds uncommitted files (`TASK.md` aside) or commits that `HEAD` has not got, unless given `--force`.

A follow-up is a new prompt file of the same name plus the id: `askai codex edit fix-ledger-prompt.md <id>`. It runs in the same worktree and may change mode. The agent is told that `TASK.md` now holds a new task, because a session told only to read it again answered the old one from memory. An agy follow-up books only its own tokens: agy's count runs over the whole conversation, so the last earlier run of the same conversation is taken off it.

### Modes

`read` lets the agent read and search files, and nothing else. `edit` bypasses every permission prompt and sandbox.

| Harness | `read` | `edit` |
| --- | --- | --- |
| agy | No bypass flag, so headless agy denies every write and command. A denial ends the run with no answer, and `askai` says so. | `--dangerously-skip-permissions` |
| claude | Only `Read`, `Grep` and `Glob`, and no MCP servers. Hooks in your settings or the repository's still run. | `--dangerously-skip-permissions` |
| codex | The read-only sandbox, where it may still run commands to read with. | `--dangerously-bypass-approvals-and-sandbox` |

A worktree is where a job starts, not a fence. An edit job can reach anything your account can, and a read job can read files outside the worktree.

A new worktree holds what git tracks and nothing else: no `.venv`, no `node_modules`, no gitignored config or keys. An edit job sets up what it needs, such as the `.venv` a first `uv run` makes from `uv.lock`. A read job cannot, so it cannot run the tests.

### Stopping a job

Every process `askai` starts dies with it, however it is stopped. On Windows it puts itself in a job object that kills its members when it exits; elsewhere the harness runs in its own process group, which is killed on the way out.

## Measuring a task

`askai` takes its own readings. For a task of your own, take a reading before it and one after, each under the task's name:

```bash
askai usage "before: session design review"
# ... the task ...
askai usage "after: session design review"
```

Both rows go to `src/askai/usage-ledger.csv`. A task's cost is only what its windows lost between its own two readings, so other spending before or after it does not count. A window that went up was reset during the task, so it is left out rather than counted as a gain.

`askai pace` groups tasks by the first word of their name, so an `askai` job is of the kind `agy`, `claude` or `codex`. `src/askai/pace.toml` says which window each kind is paced on, and the plan in that window's name is the one the kind spends:

```toml
[paced]
session = "claude_5h"
agy = "agy_week"
claude = "claude_5h"
codex = "codex_week"
review = "codex_week"
```

A window is `claude`, `codex` or `agy`, then `_5h` or `_week`. Without the file, `askai pace` still lists every task but costs and paces none.

The two ledgers and `pace.toml` are gitignored: they are your records, not the tool's.

## Where `askai plan` reads

A plan's limit counts every call made on it, by whatever made it, so `askai plan` reads every record the clients leave:

- Claude: `~/.claude/projects`, Claude Code's transcripts, including calls made through Pi's claude-bridge.
- Codex: `~/.codex/sessions`, and Pi's `openai-codex` provider in `~/.pi/agent/sessions`.
- agy: `~/.gemini/antigravity-cli/conversations`.
- Pi's compactions and pi-hermes-memory's reviews, which are estimated where nothing records them and marked `(est.)`.

Prices are in `src/askai/prices.csv`, per million tokens, with the page each came from and the date it was checked. A model not in it is listed as unpriced rather than counted as free.

## Tests

```bash
uv run python -m pytest -q
```

The tests run no harness and ask no plan. The job test replaces the harness with a stub and uses a throwaway repository.
