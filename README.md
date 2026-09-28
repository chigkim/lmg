# ask-ai

Hand one job to agy, Claude Code or Codex, in its own git worktree of the repository you are in, and record what it spent.

```bash
ask-ai agy|claude|codex read|edit PROMPT [ID]   # a job, or a follow-up to the one ID names
ask-ai review SHA                               # codex exec review of one commit
ask-ai clean agy|claude|codex NAME              # remove a job's worktree and branch
```

| Option | What it does |
| --- | --- |
| `--repo PATH` | The repository, instead of the one the current folder is in. |
| `--home PATH` | Where `worktrees/` and `runs/` go, instead of the folder the repository sits in. |
| `--model NAME` | The harness's model, instead of its default: `gemini-3.8-flash-medium`, `claude-opus-5-5`, `gpt-6-sol`. |
| `--timeout T` | agy's print timeout, `3600s` unless given. |

## Install

Python 3.11 or later, [ai-usage](../ai-usage) checked out beside this folder, and the harnesses you mean to use on `PATH`. Install it editable, because the token ledger is written beside the script:

```bash
uv tool install -e .
```

`ask-ai` needs `ai-usage` for the plan readings taken either side of a job and for the public-API prices. The path dependency in `pyproject.toml` installs it into the same tool environment, editable, so both tools read and write the same ledger files in the `ai-usage` checkout.

## What a job is

A job is named after its prompt file, less `.md` and a trailing `-prompt`: `fix-ledger-prompt.md` is the job `fix-ledger`.

1. `ask-ai` makes `HOME/worktrees/HARNESS-NAME`, a worktree on a branch of that name at `HEAD`, or reuses it for a follow-up.
2. It moves the prompt in as `TASK.md` and keeps a numbered copy as `HOME/runs/HARNESS/NAME-task-N.md`.
3. It takes an `ai-usage` reading as `before: HARNESS NAME`, runs the harness in the worktree told only to read `TASK.md`, and takes the `after:` reading.
4. The harness's last message is the answer, `HOME/runs/HARNESS/NAME-N.md`, beside its raw output. The session id to follow up with is printed.
5. One row goes to `src/ask_ai/token-ledger.csv`: time, job, repository, model, uncached input, cache reads, cache writes, output, and the public-API cost.

Edit work is left as an uncommitted diff in the worktree for you to review, test and merge. `ask-ai clean HARNESS NAME` removes the worktree and its branch.

A follow-up is a new prompt file of the same name plus the id: `ask-ai codex edit fix-ledger-prompt.md <id>`. It runs in the same worktree and may change mode.

## Modes

`read` lets the agent read and search files, and nothing else. `edit` bypasses every permission prompt and sandbox.

| Harness | `read` | `edit` |
| --- | --- | --- |
| agy | No bypass flag, so headless agy denies every write and command. A denial ends the run with no answer, and `ask-ai` says so. | `--dangerously-skip-permissions` |
| claude | Only `Read`, `Grep` and `Glob`, and no MCP servers. | `--dangerously-skip-permissions` |
| codex | The read-only sandbox, where it may still run commands to read with. | `--dangerously-bypass-approvals-and-sandbox` |

A worktree is where a job starts, not a fence. An edit job can reach anything your account can, and a read job can read files outside the worktree.

A new worktree holds what git tracks and nothing else: no `.venv`, no `node_modules`, no gitignored config or keys. An edit job sets up what it needs, such as the `.venv` a first `uv run` makes from `uv.lock`. A read job cannot, so it cannot run the tests.

## Stopping a job

Every process `ask-ai` starts dies with it, however it is stopped. On Windows it puts itself in a job object that kills its members when it exits; elsewhere the harness runs in its own process group, which is killed on the way out.

## Tests

```bash
uv run python -m pytest -q
```

The tests run no harness. The job test replaces the harness with a stub and uses a throwaway repository.
