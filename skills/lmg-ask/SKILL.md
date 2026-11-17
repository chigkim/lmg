---
name: lmg-ask
description: Hand a job (research, review, code change) to agy, Claude Code or Codex with `lmg ask`, then read and check its answer. Use when the user asks to send work to another agent or model through lmg.
---

# Handing jobs to agy, Claude Code or Codex with `lmg ask`

Use `lmg ask` to give one job to another agent. A job is a folder that holds a `task.md`. The answers are saved in the same folder.

```
lmg ask HARNESS[/MODEL] read|edit FOLDER [ID]    # FOLDER/task.md as a new job, or a follow-up to the job ID names
lmg ask [codex[/MODEL]] review SHA FOLDER        # Codex review of one commit, its answer in FOLDER
lmg ask models                                   # every HARNESS/MODEL the harnesses offer
```

HARNESS is `agy`, `claude` or `codex`. Name a model after it, such as `lmg ask claude/sonnet read FOLDER` or `lmg ask agy/gemini-4-hydrogen-high read FOLDER`. Without one, the harness runs its default model, and `review` uses Codex's. There is no `--model` option.

Options: `--reasoning LEVEL` (the harness's reasoning effort for this run, such as `high`; give it again on a follow-up; Claude and agy take `low|medium|high|xhigh|max`; without it, each harness uses its own setting: Codex's `model_reasoning_effort` in `~/.codex/config.toml`, Claude's `effortLevel` in `~/.claude/settings.json`, and for agy the level in the model name, such as `-medium`, which is the clearer way to set agy's), `--timeout T` (agy only, default 3600s), `--force` (allow an edit job in a repository's main checkout).

`lmg ask models` prints every `HARNESS/MODEL` the three harnesses list now. Exit 1 means one harness could not answer, and the rest are still printed.

The `lmg` on PATH is installed from GitHub, not from the checkout. After pushing a change, reinstall it with `uv tool install --reinstall "llm-gateway @ git+https://github.com/chigkim/lmg"`, or it keeps the old defaults and prices. Do not reinstall while the gateway serves your own session.

## Setting up the folder

- Make a folder for the job and write the task to `FOLDER/task.md` (any case, so `TASK.md` works too). The job is named after the folder.
- Make the task self-contained. The agent sees only the task and the files, not this conversation.
- The harness runs at the top of the git repository the folder is in, or in the folder itself if it is in no repository.
- `lmg ask` makes no worktree. For an edit job in a repository, make a worktree first (`git worktree add ../wt-NAME -b NAME`) and put the folder inside it. An edit job in the main checkout is refused unless given `--force`.
- A read job can use a folder in the main checkout. Keep it out of git, for example under a gitignored `.pi/`.
- To send one task to several agents, use one folder per agent.
- For `review`, the folder must be inside the repository that holds the commit. SHA can be any commit ref, such as `HEAD~1` or a tag.

## Choosing a mode

- `read`: read and search files only. Claude gets Read, Grep, Glob, WebSearch, WebFetch and ToolSearch plus its MCP servers, and no tool that writes a file or runs a command. Codex gets a read-only sandbox. agy is denied every write and command, and a denied action ends the run without an answer.
- `edit`: bypasses all permission prompts and sandboxes. The agent can write files and run commands. Use it when the job needs to change code or run commands; for research, tell the agent in the task not to edit files.
- For web research, Claude works in `read` mode.
- Codex can search the web in `read` mode, because `web_search = "live"` is set in `~/.codex/config.toml` and its built-in search tool runs outside the sandbox. Use `read` for Codex research.

## Running and reading results

- Jobs take minutes. Start them with `bg_run` and `isAgent: true`, for example `lmg ask codex read D:\code\git\REPO\.pi\jobs\my-task`, then wait for the notification.
- Run N copies `task.md` to `taskN.md`, and the agent reads that copy. `task.md` stays in place, so it can be edited for the next run.
- The answer is the agent's final message, saved by `lmg ask` as `FOLDER/responseN.md`, next to the raw `responseN.jsonl` (and Codex's `responseN.log`).
- The output ends with the session id, a token line with the API-equivalent cost (`unknown` when the model has no row in `prices.csv`), the answer path, a ready-made follow-up command, `exit N`, and a usage reading.
- After a job ends, check the answer file before using it: confirm it exists, has real content, and did not refuse or work around a missing tool. For claude and agy the file is always written, even if empty; Codex writes it only when it finishes.
- Exit codes: 2 within a second means the job never started (bad argument, missing folder or `task.md`, harness not on PATH, an edit job refused in a main checkout, or a usage ledger that cannot be written, for example because it is open in Excel). 124 means agy hit `--timeout` and the answer is partial, even though agy itself reports success. 1 with a line like `ended early`, `failed` or `denied ...` means claude or agy stopped without a full answer. Read the task output before retrying.
- To follow up, edit `task.md` and run the same command with the id printed at the end of the last run. A follow-up may switch modes. The id is saved even when a run is killed or times out, so a stopped job can be continued.
- Each job adds a `before:` and an `after:` usage reading to `usage-ledger.csv` and one row to `token-ledger.csv`, both in `~/.lmg`. `lmg usage` reads them.
