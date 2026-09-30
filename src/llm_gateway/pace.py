"""The usage calculator: what each task cost, and how many more fit now.

    lmg usage pace            # from the ledger's last reading
    lmg usage pace --fresh    # take a reading first
    lmg usage pace --brief    # costs and room, no task table

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
    parser.add_argument("--fresh", action="store_true", help="take a reading first, as the note 'reading: pace'")
    parser.add_argument("--brief", action="store_true", help="costs and room, without the table of every task")
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)
    if args.fresh:
        usage.record("reading: pace", usage.asked())
    if not usage.LEDGER.exists():
        print(f"no ledger yet at {usage.LEDGER}; run with --fresh, or lmg usage with a note")
        return
    with usage.LEDGER.open(encoding="utf-8", newline="") as book:
        rows = list(csv.DictReader(book))
    done = tasks(rows)
    plans = spends((name, used) for name, _, used, _ in done)
    if not args.brief:
        # A window no row has read is left out: Claude's plan, for one, may have no weekly window.
        shown = [w for w in WINDOWS if any(row[f"{w}_left"] for row in rows)]
        print("every task in the ledger")
        show(usage.table(
            ["task", "took"] + shown + ["overlapped"],
            [[name, usage.span(minutes * 60)] + [percent(used[w]) if w in used else "-" for w in shown]
             + ["yes" if shared else ""] for name, minutes, used, shared in done],
            notes={"took": "time between the task's before: and after: readings",
                   ", ".join(shown): "% of that window the task used; - is a reading with no figure for it",
                   "overlapped": "another task on the same plan ran at the same time, so this one's cost is not"
                                 " its own and is left out of the typical cost"}))
        print()

    costs = {}
    for harness, plan in plans.items():
        spent = [used for name, _, used, shared in done
                 if harness_of(name) == harness and not shared]
        costs[harness] = (len(spent), {w.split("_")[1]: typical([u[w] for u in spent if w in u])
                                       for w in WINDOWS
                                       if plan and w.startswith(plan) and any(w in u for u in spent)})
    if costs:
        print("what one task on each harness costs")
        show(usage.table(
            ["harness", "plan", "tasks measured", "5h window cost", "week window cost"],
            [[harness, plans[harness] or "-", alone] + [percent(c[k]) if k in c else "-" for k in LENGTH]
             for harness, (alone, c) in costs.items()], text=(0, 1),
            notes={"harness": "what ran the task: the first word of its name",
                   "plan": "the plan the harness is named for, or else the one its tasks took the most from;"
                           " - while they have taken nothing",
                   "tasks measured": "tasks on this harness that ran without overlap",
                   "5h window cost, week window cost": "typical % of that window one task takes: the mean, less"
                                                        " the dearest task once there are five"}))

    latest = next((row for row in reversed(rows) if any(row[f"{w}_reset"] for w in WINDOWS)),
                  None)
    if latest is None:
        print("\nno reading with reset times yet; run with --fresh")
        return
    # The windows themselves are `lmg usage`'s table; this one only needs how far ahead of pace each stood.
    print(f"\npaced on the reading at {latest['time']} ({latest['note']}); lmg usage shows the windows now\n")
    stand = standing(latest)
    fits = [fit(harness, plan, done, stand) for harness, plan in plans.items() if plan]
    print("how many more tasks fit without falling behind pace")
    show(usage.table(
        ["harness", "paced on", "cost per task", "fit now", "then one more every", "tasks measured"], fits, text=(0, 1),
        notes={"paced on": "of the plan's windows, the one that fits the fewest more tasks",
               "cost per task": "typical % of that window one task takes, as in the table above",
               "fit now": "tasks that can run now before the window falls behind even pace",
               "then one more every": "how often one more fits after that, as the window's pace catches up",
               "tasks measured": "tasks on this harness measured on that window"}))


def fit(harness, plan, done, stand):
    """HARNESS's row of the fit table: paced on whichever of PLAN's windows fits the fewest more of its tasks."""
    rows = []
    for window in (w for w in WINDOWS if w.startswith(plan) and w in stand):
        seen = [used[window] for name, _, used, shared in done
                if harness_of(name) == harness and not shared and window in used]
        spare = max(stand[window][2], 0)
        if not seen:
            continue
        if typical(seen):
            # The same typical cost the table above gives, the dearest task left out.
            cost = typical(seen)
            rows.append((whole(spare / cost),
                         [harness, window, f"{cost:.2f}%", f"{whole(spare / cost)}", every(window, cost), len(seen)]))
        else:
            # Every task moved it 0%, so each cost under the meter's 1%. Not 1% over all of them: they may each have
            # fallen in a window of its own, so the one bound they give is 1% a task.
            rows.append((spare, [harness, window, "under 1%", f"at least {spare}", f"{every(window, 1)} at most",
                                 len(seen)]))
    if not rows:
        return [harness, "-", "-", "no task yet" if any(w.startswith(plan) for w in stand) else "no reading", "-", 0]
    return min(rows, key=lambda r: r[0])[1]


def show(lines):
    print("\n".join(lines))

