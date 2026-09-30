"""How many more tasks each harness can run: what one costs on average, and what its plan has left.

    lmg usage pace            # one row a harness, on a reading taken now
    lmg usage pace --tasks    # and every task the costs are measured from

It takes a reading of its own unless the ledger's last row is under a minute
old, as it is when `lmg ask` has just written its after reading.

A task is a `before: NAME` row and the next `after: NAME` row in the ledger,
written by `lmg usage "before: NAME"` and `lmg usage "after: NAME"` around it.
The first word of NAME is the harness that ran the task, `agy`, `claude` or
`codex` as `lmg ask` writes it, a review included, and each spends the plan
of its own name. A task of your own may be named anything, such as `pi x`:
it gets no row, but it is listed under --tasks, and a harness task it
overlaps on the plan it spent is left out of that harness's cost.

A task's cost is only what its windows lost between its own two readings --
never the drift between other rows, since another session may be spending
the same plans -- and a window that rose instead was reset during the task
and is left out rather than counted as a gain. Spending elsewhere can still
fall inside a task's own two readings, so the cost on a harness leaves out its
dearest task (`typical`); a task another task here overlapped is marked
`shared` and left out, since the two cannot be told apart.

The tasks a window can run are what it has left over what one task costs it.
Each window of the harness's plan gets a row, 5-hour and weekly: a 5-hour
window that runs out soon may reset first, so which one binds is the
reader's to judge from when each resets. A weekly window reads in whole points and one task rarely moves it,
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
    fits = [row for harness in PLANS for row in fit(harness, done, stand)]
    print(f"how many more tasks each harness can run, on the reading at {latest['time']}")
    notes = {"harness": "what ran the task: the first word of its name",
             "window": "each window of the harness's plan the reading has: 5-hour and weekly",
             "average cost": "% of that window one task takes: the mean of the last ten, less the dearest once"
                             " there are five",
             "left": "% of that window left now",
             "tasks left": "left over average cost: how many more tasks run before the window is empty",
             "resets in": "when the window is whole again",
             "tasks measured": "tasks on this harness read on that window without overlap"}
    rough = sorted({row[0] for row in fits if 0 < row[-1] < ROUGH}, key=[row[0] for row in fits].index)
    if rough:
        notes["rough"] = f"{', '.join(rough)}: under {ROUGH} tasks measured, so the cost is a first guess"
    show(usage.table(["harness", "window", "average cost", "left", "tasks left", "resets in", "tasks measured"],
                     fits, text=(0, 1), notes=notes))
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


def fit(harness, done, stand, now=None):
    """HARNESS's rows, one for each window of its plan the reading has."""
    now = now or datetime.now()
    rows = []
    for window in (w for w in WINDOWS if w.startswith(harness) and w in stand):
        seen = [used[window] for name, _, used, shared in done
                if harness_of(name) == harness and not shared and window in used]
        left, reset = stand[window]
        resets = usage.span((reset - now).total_seconds())
        if not seen:
            rows.append([harness, window, "-", f"{left}%", "no task yet", resets, 0])
        elif typical(seen):
            cost = typical(seen)
            runs = whole(left / cost)
            rows.append([harness, window, percent(cost), f"{left}%", runs, resets, len(seen)])
        else:
            # Every task moved it 0%, so each cost under the meter's 1%. Not 1% over all of them: they may each have
            # fallen in a window of its own, so the one bound they give is 1% a task.
            rows.append([harness, window, "under 1%", f"{left}%", f"at least {left}", resets, len(seen)])
    return rows or [[harness, "-", "-", "-", "no reading", "-", 0]]


def show(lines):
    print("\n".join(lines))

