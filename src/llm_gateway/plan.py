"""What the Claude, Codex and agy plans were spent on: every call's tokens, who made it, and its public-API dollars.

    lmg usage plan                            # the last five hours
    lmg usage plan --hours 24
    lmg usage plan --since "2026-09-27 12:00"
    lmg usage worth                           # each 5h and weekly window's size and use, in dollars

A plan's limit counts every call made on it, whoever made it, so this reads every record each client leaves:

- Claude: ~/.claude/projects, Claude Code's transcripts. Pi's claude-bridge runs Claude Code, so its calls are here too, as
  entrypoint `sdk-ts`; a person at Claude Code is `cli`. One call is several lines, one a content block, each repeating the
  usage, so calls are counted once by message id. A line with no tokens is not a call: the bridge seeds each session it
  starts with the conversation so far, written as `cli` lines of zero usage within a few milliseconds (measured
  2026-09-27). Pi's own `anthropic` provider bypasses Claude Code and is read from Pi.
- Codex: ~/.codex/sessions rollouts, whose token_count events carry a running total, so a call is the rise in it; and Pi's
  `openai-codex` provider, which talks to the plan directly and leaves nothing in ~/.codex.
- Pi's side calls, which are not messages: compactions (see pi()), recorded on a `compaction` line, or not at all when the
  claude-bridge ran one, and then estimated; and pi-hermes-memory's reviews, recorded nowhere and estimated (reviews()).
  An estimated source says `(est.)`.
- agy: ~/.gemini/antigravity-cli/conversations, one SQLite file a conversation. A model call is a gen_metadata row, and its
  time is that of the matching model-response step (step_type 15, one a row, in order).

Models run locally (Qwen under Claude Code, Ollama under Codex) are skipped: they spend no plan.
`lmg usage worth` lines this up with the ledger `lmg usage NOTE` fills: between two rows where a window's figure
fell, the calls made are what that many points cost, and a hundred points are the window. The ledger's whole percents
make one gap rough and the sum fair. It asks lmg usage for what is left now.
"""
import argparse
import collections
import csv
import datetime
import json
import pathlib
import re
import sqlite3
import sys

from . import usage

HOME = pathlib.Path.home()
PRICES = pathlib.Path(__file__).with_name("prices.csv")
PLANS = ("claude", "codex", "agy")
# agy stores its model as a number. Each was matched to the one model name found in the conversation files that used only it.
AGY_MODELS = {1318: "gemini-3.8-flash-high", 1319: "gemini-3.8-flash-medium", 1320: "gemini-3.8-flash-low",
              1298: "gemini-3.7-flash-high", 1020: "gemini-3.5-flash-low"}

ENTRYPOINTS = {"cli": "claude code", "sdk-ts": "pi bridge", "sdk-cli": "claude -p"}
Call = collections.namedtuple("Call", "time plan source model input cache_read cache_write cache_write_1h output")


def when(stamp):
    """An ISO timestamp as the transcripts write it -> seconds."""
    return datetime.datetime.fromisoformat(stamp.replace("Z", "+00:00")).timestamp()


def lines(pattern, since):
    """Every JSON line of the files matching pattern under ~ that were written to after since."""
    for path in HOME.glob(pattern):
        if path.stat().st_mtime < since:
            continue
        with open(path, encoding="utf-8", errors="replace") as handle:
            for line in handle:
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if isinstance(row, dict):  # a bare null or number is valid JSON and no record
                    yield path, row


def claude(since):
    calls = {}
    for _, row in lines(".claude/projects/**/*.jsonl", since):
        message = row.get("message")
        if not isinstance(message, dict) or "usage" not in message or not str(message.get("model", "")).startswith("claude-"):
            continue
        usage, split = message["usage"], message["usage"].get("cache_creation") or {}
        hour = split.get("ephemeral_1h_input_tokens", 0)
        if not usage.get("output_tokens") and not usage.get("cache_read_input_tokens") and not usage.get("input_tokens"):
            continue
        entry = row.get("entrypoint")
        calls[message.get("id") or row.get("uuid")] = Call(
            when(row["timestamp"]), "claude", ENTRYPOINTS.get(entry, str(entry)),
            message["model"], usage.get("input_tokens", 0), usage.get("cache_read_input_tokens", 0),
            usage.get("cache_creation_input_tokens", 0) - hour, hour, usage.get("output_tokens", 0))
    return list(calls.values())


def codex(since):
    out, last, meta, model = [], {}, {}, {}
    for path, row in lines(".codex/sessions/**/rollout-*.jsonl", since):
        payload = row.get("payload") or {}
        if row.get("type") == "session_meta":
            meta[path] = payload
        elif row.get("type") == "turn_context":
            model[path] = payload.get("model")
        elif payload.get("type") == "token_count" and payload.get("info"):
            if meta.get(path, {}).get("model_provider") != "openai":
                continue
            total = payload["info"]["total_token_usage"]
            was = last.get(path, {})
            last[path] = total
            rise = {k: total.get(k, 0) - was.get(k, 0) for k in ("input_tokens", "cached_input_tokens", "cache_write_input_tokens", "output_tokens")}
            if not any(rise.values()):
                continue
            # input_tokens is the whole prompt, cached reads and cache writes included, so both come out of it.
            out.append(Call(when(row["timestamp"]), "codex", meta[path].get("originator", "codex"), model.get(path, "?"),
                            rise["input_tokens"] - rise["cached_input_tokens"] - rise["cache_write_input_tokens"],
                            rise["cached_input_tokens"],
                            rise["cache_write_input_tokens"], 0, rise["output_tokens"]))
    return out


def subscribed():
    """The Pi providers signed in by subscription (OAuth) in ~/.pi/agent/auth.json. Only each entry's `type` is read."""
    try:
        entries = json.loads((HOME / ".pi/agent/auth.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return set()
    return {provider for provider, entry in entries.items() if isinstance(entry, dict) and entry.get("type") == "oauth"}


def compaction_model():
    """(provider, model) of ~/.pi/agent/extensions/custom-compaction.ts, which reads `compaction.model` from Pi's settings."""
    try:
        named = json.loads((HOME / ".pi/agent/settings.json").read_text(encoding="utf-8"))["compaction"]["model"]
    except (OSError, ValueError, KeyError, TypeError):
        return None
    provider, _, model = named.partition("/")
    return provider, model


def hermes_model():
    """(provider, model, recent, since) of pi-hermes-memory's reviews, from ~/.pi/agent/hermes-memory-config.json:
    llmModelOverride, and reviewRecentMessages, the last so many messages a review sends (0, the default, is all of them),
    taken to hold from the file's last change and before it to have been 0."""
    path = HOME / ".pi/agent/hermes-memory-config.json"
    try:
        config = json.loads(path.read_text(encoding="utf-8"))
        provider, _, model = config["llmModelOverride"].partition("/")
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return None
    return provider, model, int(config.get("reviewRecentMessages") or 0), path.stat().st_mtime


# pi-hermes-memory reviews a session every 10 turns or 15 tool calls, once it has 3 user messages (its constants.ts and
# handlers/background-review.ts), with one in-process complete() that nothing records. What it sends is every message of
# the branch, text blocks only, each cut to 500 characters (types.ts getMessageText), under a prompt holding the memory
# files. The prompt and the answer are guesses; the messages are counted. A long session's branch outgrows the model:
# gpt-6 and gpt-5.6 take 922,000 input tokens at most (developers.openai.com/api/docs/models), so a review past that is
# refused unbilled, and on 2026-09-27 327 of 335 were.
REVIEW_TURNS, REVIEW_TOOLS, REVIEW_USERS, REVIEW_CUT = 10, 15, 3, 500
REVIEW_PROMPT, REVIEW_ANSWER, REVIEW_MOST = 3000, 500, 922_000


def text_of(message):
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(b["text"] for b in content if isinstance(b, dict) and b.get("type") == "text" and isinstance(b.get("text"), str))
    return ""


def reviews(path, rows, hermes):
    """The hermes reviews one session file's (row, fresh) pairs would have set off, as estimated calls. A row copied in
    by a fork is part of what a review sends but sets none off, having set its own off in the session it came from."""
    sizes, turns, tools, users = [], 0, 0, 0
    _, _, recent, since = hermes
    for row, fresh in rows:
        message = row.get("message")
        if row.get("type") != "message" or not isinstance(message, dict):
            continue
        sizes.append(min(len(text_of(message)), REVIEW_CUT) + 14)  # and its "[ASSISTANT]: " prefix
        users += message.get("role") == "user"
        if message.get("role") != "assistant" or not fresh:
            continue
        turns += 1
        content = message.get("content")
        tools += sum(1 for b in content if isinstance(b, dict) and b.get("type") == "toolCall") if isinstance(content, list) else 0
        if (turns >= REVIEW_TURNS or tools >= REVIEW_TOOLS) and users >= REVIEW_USERS:
            turns = tools = 0
            sent = REVIEW_PROMPT + sum(sizes[-recent:] if recent and when(row["timestamp"]) >= since else sizes) // 4
            if sent > REVIEW_MOST:
                continue
            yield Call(when(row["timestamp"]), "codex" if hermes[0] == "openai-codex" else "claude", "hermes review (est.)",
                       hermes[1], sent, 0, 0, 0, REVIEW_ANSWER)


def pi(since):
    """Pi's calls straight to a plan, and its compactions, which are not messages.

    A compaction is a `compaction` line, and one of three things ran it:
    - custom-compaction.ts (fromHook, usage recorded): the model in settings' compaction.model, read as it is now, since
      nothing records what it was then;
    - Pi's own (not fromHook, usage recorded): the session's model at the time;
    - the claude-bridge's takeover (fromHook, usage all nought): a Claude Code child with persistSession false, so it is in
      neither ~/.claude nor Pi's usage. Its tokens are estimated: tokensBefore read uncached, and the summary at four
      characters a token. Claude Code may write that prompt to a cache at up to 2x input, so this is low if anything.
    """
    plans = {"anthropic": "claude", "openai-codex": "codex", "claude-bridge": "claude"}
    # A provider signed in with an API key bills the API, not a plan, so only a subscription login counts.
    plans = {provider: plan for provider, plan in plans.items()
             if provider == "claude-bridge" or provider in subscribed()}
    hook, hermes = compaction_model(), hermes_model()
    current = {}  # session file -> (provider, model) last used
    session = collections.defaultdict(list)
    # Sessions nest (a task's own sessions sit a folder down), and a fork copies the history it was forked from into
    # its own file, 138 calls by 2026-09-27; so a call or a compaction is counted once, by response id and time.
    counted = set()
    for path, row in lines(".pi/agent/sessions/**/*.jsonl", since):
        message = row.get("message")
        if row.get("type") == "model_change":
            current[path] = row.get("provider"), row.get("modelId")
        elif isinstance(message, dict) and message.get("role") == "assistant" and message.get("provider"):
            current[path] = message["provider"], message.get("model")
        fresh = True
        if row.get("type") == "compaction" or isinstance(message, dict) and "usage" in message:
            key = (row.get("id"), row.get("timestamp")) if row.get("type") == "compaction" else \
                (message.get("responseId"), message.get("timestamp"))
            fresh = key not in counted
            counted.add(key)
        session[path].append((row, fresh))
        if not fresh:
            continue
        if row.get("type") == "compaction":
            usage = row.get("usage") or {}
            if usage.get("totalTokens"):
                provider, model = hook if row.get("fromHook") and hook else current.get(path, (None, None))
                if provider in plans and provider != "claude-bridge":
                    yield Call(when(row["timestamp"]), plans[provider], "pi compaction", model, usage.get("input", 0),
                               usage.get("cacheRead", 0), usage.get("cacheWrite", 0), 0, usage.get("output", 0))
            elif row.get("fromHook") and current.get(path, (None,))[0] == "claude-bridge":
                yield Call(when(row["timestamp"]), "claude", "pi compaction (est.)", current[path][1],
                           int(row.get("tokensBefore") or 0), 0, 0, 0, len(row.get("summary") or "") // 4)
            continue
        if not isinstance(message, dict) or message.get("provider") not in plans or "usage" not in message:
            continue
        if message["provider"] == "claude-bridge":
            continue  # the same call is in ~/.claude, as entrypoint sdk-ts
        usage = message["usage"]
        yield Call(message["timestamp"] / 1000, plans[message["provider"]], "pi", message.get("model", "?"),
                   usage.get("input", 0), usage.get("cacheRead", 0), usage.get("cacheWrite", 0), 0, usage.get("output", 0))
    if hermes and hermes[0] in plans:
        for path, rows in session.items():
            yield from reviews(path, rows, hermes)


def protobuf(data):
    """A protobuf message -> [(field, value)], a length-delimited value parsed as a message where it is one."""
    def varint(i):
        n = shift = 0
        while True:
            byte = data[i]
            i += 1
            n |= (byte & 0x7F) << shift
            shift += 7
            if byte < 0x80:
                return n, i
    out, i = [], 0
    while i < len(data):
        key, i = varint(i)
        field, wire = key >> 3, key & 7
        if field == 0:
            raise ValueError("field 0")
        if wire == 0:
            value, i = varint(i)
        elif wire in (1, 5):
            width = 8 if wire == 1 else 4
            value, i = int.from_bytes(data[i:i + width], "little"), i + width
        elif wire == 2:
            size, i = varint(i)
            value, i = data[i:i + size], i + size
            if i > len(data):
                raise ValueError("overrun")
            try:
                value = protobuf(value) if value else value
            except (ValueError, IndexError):
                pass
        else:
            raise ValueError(f"wire type {wire}")
        out.append((field, value))
    return out


def field(message, *path):
    """The first value at a path of field numbers, or None."""
    for number in path:
        message = next((v for f, v in message if f == number), None) if isinstance(message, list) else None
    return message


def agy(since):
    out = []
    for path in (HOME / ".gemini/antigravity-cli/conversations").glob("*.db"):
        if path.stat().st_mtime < since:
            continue
        db = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
        try:
            gens = db.execute("select data from gen_metadata order by idx").fetchall()
            steps = db.execute("select metadata from steps where step_type = 15 order by idx").fetchall()
        except sqlite3.Error:
            continue
        finally:
            db.close()
        for (gen,), (step,) in zip(gens, steps):
            try:
                usage, stamp = field(protobuf(gen), 1, 4), field(protobuf(step), 1, 1)
            except (ValueError, IndexError):
                continue
            if not isinstance(usage, list) or not isinstance(stamp, int):
                continue
            number = field(usage, 1)
            out.append(Call(stamp, "agy", "agy", AGY_MODELS.get(number, f"agy model {number}"),
                            field(usage, 2) or 0, field(usage, 5) or 0, 0, 0, field(usage, 3) or 0))
    return out


def prices():
    with open(PRICES, encoding="utf-8") as handle:
        return {row["model"]: {k: float(v) for k, v in row.items() if k not in ("model", "source")} for row in csv.DictReader(handle)}


def base(model):
    """The API model a plan's name is priced as: less Claude Code's context suffix and agy's thinking level."""
    return re.sub(r"-(low|medium|high)$", "", model.split("[")[0])


# OpenAI: a prompt over 272K input tokens is priced at 2x input and cache rates and 1.5x output for the whole request, on
# the models its pricing page gives a long-context column (developers.openai.com/api/docs/models/gpt-6-sol, 2026-09-27).
LONG = re.compile(r"gpt-(6|5\.6)-|gpt-5\.[45]$")


def dollars(call, table):
    rate = table.get(base(call.model))
    if rate is None:
        return None
    prompt, answer = 1, 1
    if LONG.match(base(call.model)) and call.input + call.cache_read + call.cache_write > 272_000:
        prompt, answer = 2, 1.5
    return (prompt * (call.input * rate["input"] + call.cache_read * rate["cache_read"] + call.cache_write * rate["cache_write"]
                      + call.cache_write_1h * rate["cache_write_1h"]) + answer * call.output * rate["output"]) / 1e6


def everything(since):
    return sorted(claude(since) + codex(since) + list(pi(since)) + agy(since))


def summed(calls, table):
    """calls -> (count, input, cache read, cache write, output, dollars, unpriced models)"""
    usd, unpriced = 0.0, set()
    for call in calls:
        cost = dollars(call, table)
        if cost is None:
            unpriced.add(call.model)
        else:
            usd += cost
    total = [sum(getattr(c, k) for c in calls) for k in ("input", "cache_read", "cache_write", "cache_write_1h", "output")]
    return len(calls), total[0], total[1], total[2] + total[3], total[4], usd, unpriced


def money(usd):
    """An amount marked as dollars: $2.62, $1,204.50."""
    return f"${usd:,.2f}"


def report(since, table):
    calls = [c for c in everything(since) if c.time >= since]
    print(f"since {datetime.datetime.fromtimestamp(since):%Y-%m-%d %H:%M}, at public API rates ({PRICES})")
    rows = []
    for plan in PLANS:
        mine = [c for c in calls if c.plan == plan]
        groups = collections.defaultdict(list)
        for call in mine:
            groups[call.source].append(call)
        for source, group in sorted(groups.items()) + ([("all", mine)] if len(groups) > 1 else []):
            n, i, r, w, o, usd, unpriced = summed(group, table)
            note = ", ".join(sorted(unpriced))
            rows.append([plan, source, n, f"{i:,}", f"{r:,}", f"{w:,}", f"{o:,}", money(usd), note])
    print("\n".join(usage.table(
        ["plan", "source", "calls", "input", "cache read", "cache write", "output", "API cost", "unpriced models"], rows, text=(0, 1, 8),
        notes={"source": "what made the calls; all is the plan's sources summed",
               "input, cache read, cache write, output": "tokens",
               "API cost": "what those tokens would cost at the public API rates in prices.csv",
               "unpriced models": "models with no price in prices.csv, left out of API cost"})))


def stamp(text):
    """A ledger time: to the second, or to the minute in a row written before seconds were."""
    return datetime.datetime.fromisoformat(text).timestamp()


def per_point(ledger, calls, plan, kind, table):
    """(points, dollars) over the ledger, a stretch of rows in one window at a time: first row's figure less the last's,
    and every call between them. Taken gap by gap instead, a gap whose fall rounds to nought loses its calls and the next
    gap gains its points, which halved the weekly rate.

    A stretch ends where the figure rises, which is a reset, or the reset moves, which is a new window or another
    account -- agy's was swapped twice on 2026-09-27. A 5h gap over three hours may hide a reset unseen, so it ends one.
    """
    # The ledger's first rows kept no reset; there a 5h fall is taken on the three-hour rule alone and a weekly one not at all.
    rows = [(stamp(r["time"]), int(r[f"{plan}_{kind}_left"]), r[f"{plan}_{kind}_reset"] and stamp(r[f"{plan}_{kind}_reset"]))
            for r in ledger if r.get(f"{plan}_{kind}_left")]
    stretches = [[rows[0]]] if rows else []
    for (a, was, reset), row in zip(rows, rows[1:]):
        b, now, then = row
        same = abs(then - reset) <= 600 if reset and then else kind == "5h" and not reset and not then
        if now <= was and same and (kind == "week" or b - a <= 3 * 3600):
            stretches[-1].append(row)
        else:
            stretches.append([row])
    points, spent = 0, []
    for stretch in stretches:
        (a, was, _), (b, now, _) = stretch[0], stretch[-1]
        if now < was:
            points += was - now
            spent += [c for c in calls if c.plan == plan and a <= c.time < b]
    *_, usd, unpriced = summed(spent, table)
    # A call that cannot be priced would read as free and shrink the window; the dollars are unknown instead.
    return points, None if unpriced else usd


def worth(argv=()):
    """`lmg usage worth`: each window in API dollars, the whole of it and what has gone since it opened."""
    if argv:
        print(f"lmg usage worth takes no options, not {' '.join(argv)}", file=sys.stderr)
        return 2
    table = prices()
    now = usage.windows(usage.asked())
    if not usage.LEDGER.exists():
        print(f"no ledger yet at {usage.LEDGER}; `lmg usage NOTE` starts one")
        return
    with open(usage.LEDGER, encoding="utf-8") as handle:
        ledger = list(csv.DictReader(handle))
    if not ledger:
        print(f"no readings yet in {usage.LEDGER}; `lmg usage NOTE` adds one")
        return
    calls = everything(min(stamp(r["time"]) for r in ledger) - usage.WEEK)
    print("each plan window in dollars at public API rates")
    rows = []
    for plan in PLANS:
        for kind, length in (("5h", usage.FIVE_HOURS), ("week", usage.WEEK)):
            window = now.get(f"{plan}_{kind}")
            if window is None:  # Claude's plan has no weekly window
                continue
            points, usd = per_point(ledger, calls, plan, kind, table)
            opened = window.resets - length
            *_, used, unpriced = summed([c for c in calls if c.plan == plan and c.time >= opened], table)
            used = "-" if unpriced else money(used)
            if not points or usd is None:
                # no fall in the ledger to size it by, or a call in one that has no price
                rows.append([f"{plan} {kind}", f"{window.left}%", f"{points}%", "-", "-", used, "-"])
                continue
            rate = usd / points
            rows.append([f"{plan} {kind}", f"{window.left}%", f"{points}%", money(rate), money(rate * 100),
                         used, money(rate * (100 - window.left))])
    print("\n".join(usage.table(
        ["window", "left", "drop measured", "$ per 1%", "full window", "spent (calls)", "spent (meter)"], rows,
        notes={"left": "% of this window left now",
               "drop measured": "every drop the ledger saw, summed over all past windows of that kind, so it can"
                                " pass 100%; $ per 1% is measured over it",
               "-": "unknown: no drop to measure yet, or a call in it from a model with no price in prices.csv",
               "$ per 1%": "dollars of calls made while those drops happened, divided by the drop",
               "full window": "$ per 1% times 100",
               "spent (calls)": "every recorded call since this window opened, priced",
               "spent (meter)": "the % this window has lost times $ per 1%; it differs from spent (calls) by what"
                                " the rate misjudges, or by calls these records do not hold"})))


def main(argv=None):
    parser = argparse.ArgumentParser(prog="lmg usage plan", description=__doc__.splitlines()[0])
    parser.add_argument("--hours", type=float, default=5)
    parser.add_argument("--since", help="YYYY-MM-DD HH:MM, local time; overrides --hours")
    args = parser.parse_args(argv)
    table = prices()
    since = (datetime.datetime.strptime(args.since, "%Y-%m-%d %H:%M").timestamp() if args.since
             else datetime.datetime.now().timestamp() - args.hours * 3600)
    report(since, table)

