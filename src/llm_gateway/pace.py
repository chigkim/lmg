"""What can run now: how many more tasks each harness fits before a window falls behind even pace.

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

Pace is even spending: a window with a quarter of its length left to run is
on pace with a quarter of it left, which over a 5-hour window is 10% a half
hour. What a window has
beyond its even line is its spare, and the jobs that fit are that spare over
what one job on that harness costs there. A weekly window reads in whole points
and one job rarely moves it, so a harness whose jobs have not yet moved it is
given the one bound it allows: each cost under the meter's 1%, so at least
spare more fit.
"""
import csv
import statistics
import sys
from datetime import datetime

from . import usage

LENGTH = {"5h": 5 * 3600, "week": 7 * 24 * 3600}
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
    print(f"what can run now, on the reading at {latest['time']}")
    notes = {"harness": "what ran the task: the first word of its name",
             "plan": "the plan the harness is named for, or else the one its tasks took the most from",
             "runs out first": "of the plan's windows, the one that fits the fewest more tasks",
             "cost per task": "typical % of that window one task takes: the mean of the last ten, less the dearest"
                              " once there are five",
             "fit now": "tasks that can start now and leave the window on or ahead of even pace",
             "then one more every": "how often one more fits after that, as the even line falls",
             "tasks measured": "tasks on this harness that moved or were read on that window without overlap"}
    rough = [row[0] for row in fits if isinstance(row[-1], int) and 0 < row[-1] < ROUGH]
    if rough:
        notes["rough"] = f"{', '.join(rough)}: under {ROUGH} tasks measured, so the cost is a first guess"
    if hidden:
        notes["left out"] = f"{', '.join(hidden)}: fewer than {LEAST} tasks, so a one-off rather than a harness"
    show(usage.table(["harness", "plan", "runs out first", "cost per task", "fit now", "then one more every",
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


def fit(harness, plan, done, stand):
    """HARNESS's row: paced on whichever of PLAN's windows fits the fewest more of its tasks."""
    rows = []
    for window in (w for w in WINDOWS if w.startswith(plan) and w in stand):
        seen = [used[window] for name, _, used, shared in done
                if harness_of(name) == harness and not shared and window in used]
        spare = max(stand[window][2], 0)
        if not seen:
            continue
        if typical(seen):
            # The same typical cost as every other figure here, the dearest task left out.
            cost = typical(seen)
            rows.append((whole(spare / cost), [harness, plan, window, f"{cost:.2f}%", f"{whole(spare / cost)}",
                                               every(window, cost), len(seen)]))
        else:
            # Every task moved it 0%, so each cost under the meter's 1%. Not 1% over all of them: they may each have
            # fallen in a window of its own, so the one bound they give is 1% a task.
            rows.append((spare, [harness, plan, window, "under 1%", f"at least {spare}",
                                 f"{every(window, 1)} at most", len(seen)]))
    if not rows:
        return [harness, plan, "-", "-", "no task yet" if any(w.startswith(plan) for w in stand) else "no reading",
                "-", 0]
    return min(rows, key=lambda r: r[0])[1]


def show(lines):
    print("\n".join(lines))

