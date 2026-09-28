# ai-usage

Three commands for watching the Claude, Codex and agy subscription plans: how much of each window is left, what spent it, and how many more tasks of a kind fit before a window runs out.

| Command | What it prints |
| --- | --- |
| `ai-usage [NOTE]` | Each plan's 5-hour and weekly windows: percent left, when each resets, and how that stands against an even pace. With a note, it also adds a row to the ledger. |
| `ai-plan [--hours N \| --since "YYYY-MM-DD HH:MM" \| --limits]` | Every call each plan was spent on, read from the clients' own records, grouped by who made it, with tokens and what they would cost on the public API. `--limits` sizes each window in dollars from the ledger. |
| `ai-pace [--fresh] [--brief]` | What each kind of task costs a window, from paired `before:` and `after:` rows in the ledger, and how many more fit now. |

Nothing here runs a model to find out. `ai-usage` asks `codex app-server` for Codex's limits, reads agy's quota out of the log `agy models` writes, and asks the endpoint Claude Code's own `/usage` reads, with the CLI's login token held in memory for that one request and never printed or stored. Only when that token has expired does it fall back to a one-word Haiku call.

## Install

Python 3.11 or later, no dependencies. Install it editable, because the ledger is written beside the scripts and a normal install would put it inside uv's tool folder, where the next reinstall deletes it:

```bash
uv tool install -e .
```

The three commands then run from any folder. Edits to the checkout take effect at once; a change to `pyproject.toml` needs `uv tool install -e --reinstall .`.

A plan whose client is not on `PATH` (`claude`, `codex`, `agy`) is reported as one that could not be asked, and the others are still read.

## Measuring a task

Take a reading before a task and one after, each under the task's name:

```bash
ai-usage "before: critic design review"
# ... the task ...
ai-usage "after: critic design review"
```

Both rows go to `src/ai_usage/usage-ledger.csv`. A task's cost is only what its windows lost between its own two readings, so other spending before or after it does not count. A window that went up was reset during the task, so it is left out rather than counted as a gain.

`ai-pace` groups tasks by the first word of their name. `src/ai_usage/pace.toml` says which window each kind is paced on, and the plan in that window's name is the one the kind spends:

```toml
[paced]
session = "claude_5h"
critic = "codex_week"
review = "codex_week"
worker = "agy_week"
```

A window is `claude`, `codex` or `agy`, then `_5h` or `_week`. Without the file, `ai-pace` still lists every task but costs and paces none.

Both the ledger and `pace.toml` are gitignored: they are your records, not the tool's.

## Where `ai-plan` reads

A plan's limit counts every call made on it, by whatever made it, so `ai-plan` reads every record the clients leave:

- Claude: `~/.claude/projects`, Claude Code's transcripts, including calls made through Pi's claude-bridge.
- Codex: `~/.codex/sessions`, and Pi's `openai-codex` provider in `~/.pi/agent/sessions`.
- agy: `~/.gemini/antigravity-cli/conversations`.
- Pi's compactions and pi-hermes-memory's reviews, which are estimated where nothing records them and marked `(est.)`.

Prices are in `src/ai_usage/prices.csv`, per million tokens, with the page each came from and the date it was checked. A model not in it is listed as unpriced rather than counted as free.

## Tests

```bash
uv run pytest
```
