"""How many more tasks each harness can run: what one costs on average, and what its plan has left.

    lmg usage pace            # one row a harness, on a reading taken now
    lmg usage pace --tasks    # and every task the costs are measured from

It takes a reading of its own unless the ledger's last row is under a minute
old, as it is when `lmg ask` has just written its after reading.

A task is a `before: NAME` row and the next `after: NAME` row in the ledger,
written by `lmg usage "before: NAME"` and `lmg usage "after: NAME"` around it.
The first word of NAME is the harness that ran the task -- `lmg ask` writes
`agy`, `claude` or `codex`, a review included, and a task of your own names
the harness you work in, such as `pi`. A harness named for a plan spends that
plan; any other spends the plan its tasks took the most points from, so a Pi
session on Claude is costed on Claude once one of its tasks has moved it.
Each harness is paced on whichever of its plan's windows fits the fewest more
tasks, the one that runs out first.

A task's cost is only what its windows lost between its own two readings --
never the drift between other rows, since another session may be spending
the same plans -- and a window that rose instead was reset during the task
and is left out rather than counted as a gain. Spending elsewhere can still
fall inside a task's own two readings, so the cost on a harness leaves out its
dearest task (`typical`); a task another task here overlapped is marked
`shared` and left out, since the two cannot be told apart.

The tasks a window can run are what it has left over what one task costs it,
and a harness can run as many as the window of its plan that allows the
fewest. A weekly window reads in whole points and one task rarely moves it,
so a harness whose tasks have not yet moved it is given the one bound it
allows: each cost under the meter's 1%, so at least as many as the points left.
"""
import csv
import statistics
import sys
from datetime import datetime

from . import usage

WINDOWS = usage.WINDOWS
PLANS = ("claude", "codex", "agy")
RECENT = 10
FRESH = 60    # seconds a ledger reading stays fresh enough to pace on
LEAST = 3     # tasks a name other than a plan's needs before it is a harness rather than a one-off
ROUGH = 5     # tasks measured below which a cost is a first guess


def spends(tasks):
    """{harness: plan} for every harness among TASKS, pairs of (name, {window: points used}). A harness named for a
    plan spends it; any other spends the plan its tasks took the most points from, or None while they have taken
    none. A tie goes to the plan named first in PLANS."""
    taken = {}
    for name, used in tasks:
        harness = harness_of(name)
        if harness:
            points = taken.setdefault(harness, dict.fromkeys(PLANS, 0))
            for window, fall in used.items():
                points[window.split("_")[0]] += fall
    return {harness: harness if harness in PLANS else (max(PLANS, key=points.get) if any(points.values()) else None)
            for harness, points in taken.items()}


def moment(text):
    """A ledger time: to the second, or to the minute in a row written before seconds were."""
    return datetime.fromisoformat(text)


def whole(ratio):
    """How many whole tasks a ratio of room to cost is. Floor division of floats undercounts at the boundary:
    1 // 0.1 is 9.0, because 0.1 is a little over a tenth; the ratio, rounded off first, is not."""
    return int(round(ratio, 9))


def tasks(rows):
    """(name, minutes, {window: points used}, shared) per paired task. Shared is an overlap with a task that spends
    the same plan, whose points the two readings cannot tell apart; a codex job running beside an agy job spends
    another plan and leaves the codex job's figure its own."""
    open_, paired = {}, []
    for row in rows:
        side, _, name = row["note"].partition(": ")
        if side == "before":
            open_[name] = row
        elif side == "after" and name in open_:
            before = open_.pop(name)
            used = {}
            for window in WINDOWS:
                was, now = before[f"{window}_left"], row[f"{window}_left"]
                if was and now and int(was) >= int(now) and same_window(before, row, window):
                    used[window] = int(was) - int(now)
            paired.append((name, moment(before["time"]), moment(row["time"]), used))
    plan = spends((name, used) for name, _, _, used in paired)
    return [(name, (end - start).total_seconds() / 60, used,
             any(s < end and start < e for j, (n, s, e, _) in enumerate(paired)
                 if j != i and plan.get(harness_of(name)) is not None
                 and plan.get(harness_of(n)) == plan.get(harness_of(name))))
            for i, (name, start, end, used) in enumerate(paired)]


def same_window(before, after, window):
    """Whether two readings are of one window: a reset time that moved is a new window, whose figure is no fall from
    the old one's. A reading with no reset time, as the ledger's first rows are, is taken at its word. A window whole
    at the first reading had nothing to lose to a reset: one not started yet was given a made-up reset of a length
    from then, which the real one, set by the first call, lands after."""
    if int(before[f"{window}_left"] or 0) == 100:
        return True
    was, now = before[f"{window}_reset"], after[f"{window}_reset"]
    return not (was and now) or abs((moment(now) - moment(was)).total_seconds()) <= 600


def harness_of(name):
    """The harness that ran a task, the first word of its name; '' for a task named nothing."""
    return (name.split() or [""])[0]


def typical(points):
    """What one task on a harness costs a window: the mean of its last `RECENT`,
    less the dearest when there are enough to spare it. A mean, because a
    window reads in whole points and most tasks move it by nought or one, so
    a median says nought for a harness that plainly spends; the dearest dropped,
    because spending from another session inside a task's own two readings
    only ever adds."""
    recent = sorted(points[-RECENT:])
    if len(recent) >= 5:
        recent = recent[:-1]
    return round(statistics.mean(recent), 1)


def standing(row):
    """{window: (points left, when it resets)} for each window the row has a reset for."""
    return {window: (int(row[f"{window}_left"]), moment(row[f"{window}_reset"]))
            for window in WINDOWS if row[f"{window}_left"] and row[f"{window}_reset"]}


def percent(points):
    """A share of a window, marked as one: 3%, 0.3%."""
    return f"{points:g}%"


def main(argv=None):
    import argparse
    parser = argparse.ArgumentParser(prog="lmg usage pace", description=__doc__.splitlines()[0])
    parser.add_argument("--tasks", action="store_true", help="list every task the costs are measured from")
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)
    rows = ledger()
    if not rows or (datetime.now() - moment(rows[-1]["time"])).total_seconds() > FRESH:
        usage.record("reading: pace", usage.asked())
        rows = ledger()
    done = tasks(rows)
    latest = next((row for row in reversed(rows) if any(row[f"{w}_reset"] for w in WINDOWS)), None)
    if latest is None:
        print("no plan could be read, so nothing can be paced; lmg usage says why")
        return 1
    stand = standing(latest)
    counted = {}
    for name, *_ in done:
        counted[harness_of(name)] = counted.get(harness_of(name), 0) + 1
    plans = spends((name, used) for name, _, used, _ in done)
    shown = [h for h, plan in plans.items() if plan and (h in PLANS or counted[h] >= LEAST)]
    hidden = sorted(h for h in plans if h not in shown)
    fits = [fit(harness, plans[harness], done, stand) for harness in shown]
    print(f"how many more tasks each harness can run, on the reading at {latest['time']}")
    notes = {"harness": "what ran the task: the first word of its name",
             "plan": "the plan the harness is named for, or else the one its tasks took the most from",
             "window": "of the plan's windows, the one that runs out first",
             "average cost": "% of that window one task takes: the mean of the last ten, less the dearest once"
                             " there are five",
             "left": "% of that window left now",
             "tasks left": "left over average cost: how many more tasks run before the window is empty",
             "resets in": "when the window is whole again",
             "tasks measured": "tasks on this harness read on that window without overlap"}
    rough = [row[0] for row in fits if isinstance(row[-1], int) and 0 < row[-1] < ROUGH]
    if rough:
        notes["rough"] = f"{', '.join(rough)}: under {ROUGH} tasks measured, so the cost is a first guess"
    if hidden:
        notes["left out"] = f"{', '.join(hidden)}: fewer than {LEAST} tasks, so a one-off rather than a harness"
    show(usage.table(["harness", "plan", "window", "average cost", "left", "tasks left", "resets in",
                      "tasks measured"], fits, text=(0, 1, 2), notes=notes))
    if args.tasks:
        # A window no row has read is left out: Claude's plan, for one, may have no weekly window.
        windows = [w for w in WINDOWS if any(row[f"{w}_left"] for row in rows)]
        print("\nevery task in the ledger")
        show(usage.table(
            ["task", "took"] + windows + ["overlapped"],
            [[name, usage.span(minutes * 60)] + [percent(used[w]) if w in used else "-" for w in windows]
             + ["yes" if shared else ""] for name, minutes, used, shared in done],
            notes={"took": "time between the task's before: and after: readings",
                   ", ".join(windows): "% of that window the task used; - is a reading with no figure for it",
                   "overlapped": "another task on the same plan ran at the same time, so this one's cost is not"
                                 " its own and is left out of the typical cost"}))
    return 0


def ledger():
    if not usage.LEDGER.exists():
        return []
    with usage.LEDGER.open(encoding="utf-8", newline="") as book:
        return list(csv.DictReader(book))


def fit(harness, plan, done, stand, now=None):
    """HARNESS's row: on whichever of PLAN's windows runs the fewest more of its tasks."""
    now = now or datetime.now()
    rows = []
    for window in (w for w in WINDOWS if w.startswith(plan) and w in stand):
        seen = [used[window] for name, _, used, shared in done
                if harness_of(name) == harness and not shared and window in used]
        if not seen:
            continue
        left, reset = stand[window]
        resets = usage.span((reset - now).total_seconds())
        if typical(seen):
            cost = typical(seen)
            runs = whole(left / cost)
            rows.append((runs, [harness, plan, window, percent(cost), f"{left}%", runs, resets, len(seen)]))
        else:
            # Every task moved it 0%, so each cost under the meter's 1%. Not 1% over all of them: they may each have
            # fallen in a window of its own, so the one bound they give is 1% a task.
            rows.append((left, [harness, plan, window, "under 1%", f"{left}%", f"at least {left}", resets,
                                len(seen)]))
    if not rows:
        return [harness, plan, "-", "-", "-", "no task yet" if any(w.startswith(plan) for w in stand)
                else "no reading", "-", 0]
    return min(rows, key=lambda r: r[0])[1]


def show(lines):
    print("\n".join(lines))

