"""The usage calculator: what each task cost, and how many more fit now.

    ai-pace            # from the ledger's last reading
    ai-pace --fresh    # take a reading first
    ai-pace --brief    # costs and room, no task table

A task is a `before: NAME` row and the next `after: NAME` row in the ledger,
written by `ai-usage "before: NAME"` and `ai-usage "after: NAME"` around it.
The first word of NAME is the task's kind, and `pace.toml` beside the ledger
names the one window each kind is paced on:

    [paced]
    session = "claude_5h"    # `before: session x` spends Claude, paced on its 5-hour window
    critic = "codex_week"
    worker = "agy_week"

A window is a plan and a length, `claude`, `codex` or `agy` and `5h` or
`week`, and the plan is what the kind spends. Without the file every task is
still listed, but no kind is costed or paced.

A task's cost is only what its windows lost between its own two readings --
never the drift between other rows, since another session may be spending
the same plans -- and a window that rose instead was reset during the task
and is left out rather than counted as a gain. Spending elsewhere can still
fall inside a task's own two readings, so the cost of a kind leaves out its
dearest task (`typical`); a task another task here overlapped is marked
`shared` and left out, since the two cannot be told apart.

Pace is even spending: a window with a quarter of its length left to run is
on pace with a quarter of it left, which over a 5-hour window is 10% a half
hour. What a window has
beyond its even line is its spare, and the jobs that fit are that spare over
what one job of that kind costs there. A weekly window reads in whole points
and one job rarely moves it, so a kind whose jobs have not yet moved it is
given the bound its count allows: n jobs that cost nought each cost under
1/n of a point, so at least spare times n more fit.
"""
import csv
import statistics
import sys
import tomllib
from datetime import datetime

from ai_usage import usage

LENGTH = {"5h": 5 * 3600, "week": 7 * 24 * 3600}
WINDOWS = usage.WINDOWS
RECENT = 10


def paced(path=usage.DATA / "pace.toml"):
    """{kind: window} out of pace.toml, or nothing when there is none."""
    if not path.exists():
        return {}
    with path.open("rb") as handle:
        kinds = tomllib.load(handle).get("paced", {})
    for kind, window in kinds.items():
        if window not in WINDOWS:
            raise SystemExit(f"{path}: {kind} = {window!r} is not one of {', '.join(WINDOWS)}")
    return kinds


PACED = paced()
# The plan each kind of task spends: the plan of the window it is paced on.
SPENDS = {kind: window.split("_")[0] for kind, window in PACED.items()}


def moment(text):
    return datetime.strptime(text, "%Y-%m-%d %H:%M")


def tasks(rows):
    """(name, minutes, {window: points used}, shared) per paired task. Shared is an overlap with a task that spends
    the same plan, whose points the two readings cannot tell apart; a critic running beside a worker spends another
    plan and leaves the critic's figure its own."""
    open_, paired = {}, []
    for row in rows:
        kind, _, name = row["note"].partition(": ")
        if kind == "before":
            open_[name] = row
        elif kind == "after" and name in open_:
            before = open_.pop(name)
            used = {}
            for window in WINDOWS:
                was, now = before[f"{window}_left"], row[f"{window}_left"]
                if was and now and int(was) >= int(now):
                    used[window] = int(was) - int(now)
            paired.append((name, moment(before["time"]), moment(row["time"]), used))
    return [(name, (end - start).total_seconds() / 60, used,
             any(s < end and start < e for n, s, e, _ in paired
                 if (n, s) != (name, start) and plan(n) == plan(name)))
            for name, start, end, used in paired]


def plan(name):
    return SPENDS.get(name.split()[0])


def typical(points):
    """What one task of a kind costs a window: the mean of its last `RECENT`,
    less the dearest when there are enough to spare it. A mean, because a
    window reads in whole points and most tasks move it by nought or one, so
    a median says nought for a kind that plainly spends; the dearest dropped,
    because spending from another session inside a task's own two readings
    only ever adds."""
    recent = sorted(points[-RECENT:])
    if len(recent) >= 5:
        recent = recent[:-1]
    return round(statistics.mean(recent), 1)


def every(window, cost):
    """How often one more task of that cost comes free, as the even line
    falls 100 points over the window's length."""
    return usage.span(cost / 100 * LENGTH[window.split("_")[1]])


def standing(row):
    """{window: (left, even, spare)} for each window the row has a reset for."""
    taken = moment(row["time"])
    out = {}
    for window in WINDOWS:
        left, reset = row[f"{window}_left"], row[f"{window}_reset"]
        if left and reset:
            length = LENGTH[window.split("_")[1]]
            to_run = min(max((moment(reset) - taken).total_seconds(), 0), length)
            even = round(100 * to_run / length)
            out[window] = (int(left), even, int(left) - even)
    return out


def percent(points):
    """A share of a window, marked as one: 3%, 0.3%."""
    return f"{points:g}%"


def main():
    if "--fresh" in sys.argv:
        usage.record("reading: pace", usage.asked())
    if not usage.LEDGER.exists():
        print(f"no ledger yet at {usage.LEDGER}; run with --fresh, or ai-usage with a note")
        return
    with usage.LEDGER.open(encoding="utf-8", newline="") as book:
        rows = list(csv.DictReader(book))
    done = tasks(rows)
    if not PACED:
        print(f"no {usage.DATA / 'pace.toml'}, so no kind of task is costed or paced\n")
    if "--brief" not in sys.argv:
        # A window no row has read is left out: Claude's plan, for one, may have no weekly window.
        shown = [w for w in WINDOWS if any(row[f"{w}_left"] for row in rows)]
        print(f"{'task':<34}{'took':>7}" + "".join(f"{w:>12}" for w in shown))
        for name, minutes, used, shared in done:
            print(f"{name[:34]:<34}{usage.span(minutes * 60):>7}"
                  + "".join(f"{percent(used[w]) if w in used else '-':>12}" for w in shown)
                  + ("  shared" if shared else ""))
        print()

    costs = {}
    for kind, plan in SPENDS.items():
        spent = [used for name, _, used, shared in done
                 if name.split()[0] == kind and not shared]
        costs[kind] = {w: typical([u[w] for u in spent if w in u])
                       for w in WINDOWS
                       if w.startswith(plan) and any(w in u for u in spent)}
        figures = ", ".join(f"{w} {percent(c)}" for w, c in costs[kind].items()) or "no paired task yet"
        print(f"cost of a {kind} task ({len(spent)} alone): {figures}")

    latest = next((row for row in reversed(rows) if any(row[f"{w}_reset"] for w in WINDOWS)),
                  None)
    if latest is None:
        print("\nno reading with reset times yet; run with --fresh")
        return
    print(f"\nstanding at {latest['time']} ({latest['note']}):")
    stand = standing(latest)
    for window, (left, even, spare) in stand.items():
        print(f"  {window:<12}{left:>4}% left, even pace {even:>3}%, "
              + (f"{spare}% to spare" if spare >= 0 else f"{-spare}% over pace"))
    print()
    for kind, window in PACED.items():
        seen = [used[window] for name, _, used, shared in done
                if name.split()[0] == kind and not shared and window in used]
        if window not in stand:
            print(f"  {kind:<8} no reading of {window}")
            continue
        spare = max(stand[window][2], 0)
        if not seen:
            print(f"  {kind:<8} no paired task yet; {window} has {spare}% to spare")
        elif sum(seen):
            cost = statistics.mean(seen)
            print(f"  {kind:<8} {spare // cost:g} more now, then one every "
                  f"{every(window, cost)} ({window}, {cost:.2f}% a task over {len(seen)})")
        else:
            print(f"  {kind:<8} at least {spare * len(seen)} more now, then one every "
                  f"{every(window, 1 / len(seen))} at most ({window}, "
                  f"{len(seen)} tasks moved it 0%)")


if __name__ == "__main__":
    main()
