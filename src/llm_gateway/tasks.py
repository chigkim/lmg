"""What each task cost the windows of its plan, and the average cost `lmg usage` works out tasks left from.

    lmg usage tasks    # every task, then each window's task cost and how many tasks it is from

A task is a `before: NAME` row and the next `after: NAME` row in the ledger,
written by `lmg usage "before: NAME"` and `lmg usage "after: NAME"` around it.
The first word of NAME is the harness that ran the task, `agy`, `claude` or
`codex` as `lmg ask` writes it, a review included, and each spends the plan
of its own name. A task of your own may be named anything, such as `pi x`:
it has no cost of its own in `lmg usage`, but it is listed here, and a
harness task it overlaps on the plan it spent is left out of that harness's
cost.

A task's cost is only what its windows lost between its own two readings --
never the drift between other rows, since another session may be spending
the same plans -- and a window that rose instead was reset during the task
and is left out rather than counted as a gain. Spending elsewhere can still
fall inside a task's own two readings, so the cost on a harness leaves out its
dearest task once there are five (`typical`); a task another task here
overlapped on a plan both spent is marked `shared` and left out, since the two
cannot be told apart.

The tasks a window can run are what it has left over what one task costs it.
A weekly window reads in whole points and one task rarely moves it, so a
harness whose tasks have not yet moved it is given the one bound it allows:
each cost under the meter's 1%, so at least as many as the points left.
"""
import csv
import statistics
import sys
from datetime import datetime

from . import usage

WINDOWS = usage.WINDOWS
PLANS = ("claude", "codex", "agy")
RECENT = 10   # the last tasks a cost is the mean of
ROUGH = usage.ROUGH


def main(argv=None):
    import argparse
    parser = argparse.ArgumentParser(prog="lmg usage tasks", description=__doc__.splitlines()[0])
    parser.parse_args(sys.argv[1:] if argv is None else argv)
    rows = ledger()
    done = paired(rows)
    if not done:
        print("no task in the ledger yet: lmg ask takes a reading before and after each job")
        return 0
    # A window no row has read is left out: Claude's plan, for one, may have no weekly window.
    windows = [w for w in WINDOWS if any(row.get(f"{w}_left") for row in rows)]
    labels = [usage.LABELS[w] for w in windows]
    print("every task in the ledger")
    show(usage.table(
        ["task", "took"] + labels + ["overlapped"],
        [[name, usage.span(minutes * 60)] + [fell(name, used, w) for w in windows] + ["yes" if shared else ""]
         for name, minutes, used, shared in done],
        notes={"took": "time between the task's before: and after: readings",
               ", ".join(labels): "% of that window the task used; - is a window outside the plan of an agy,"
                                  " claude or codex task, or a reading with no figure for it",
               "overlapped": "another task on a plan this one spent ran at the same time, so this one's cost is"
                             " not its own and is left out of the task cost"}))
    # Last, so a long list does not scroll the figures it adds up to off the screen.
    notes = {"task cost": f"% of that window one task takes: the mean of the last {RECENT}, less the dearest once"
                          " there are five; lmg usage divides what is left by it",
             "tasks measured": f"tasks on this harness read on that window without overlap; the cost is from the"
                               f" last {RECENT} of them"}
    measured = [[usage.LABELS[w], cost(seen(done, w)), len(seen(done, w))] for w in windows]
    if any(row[1].startswith("~") for row in measured):
        notes["~"] = usage.ROUGH_NOTE
    print("\nwhat a task costs each window")
    show(usage.table(["window", "task cost", "tasks measured"], measured, notes=notes))
    return 0


def fell(name, used, window):
    """What a task took from a window, for the list: - outside its plan when it is an agy, claude or codex task,
    since another session's spending there is none of its own."""
    harness = harness_of(name)
    if window not in used or (harness in PLANS and not window.startswith(harness)):
        return "-"
    return percent(used[window])


def costs(windows):
    """{window: (task cost, tasks left)} for `lmg usage`'s table, from {window: Window} read now and the ledger."""
    done = paired(ledger())
    return {window: (cost(seen(done, window)), room(seen(done, window), reading.left))
            for window, reading in windows.items()}


def cost(points):
    """What one task costs a window, from the POINTS its tasks took; ~ in front when they are too few to trust."""
    if not points:
        return "-"
    # Every task moved it 0%, so each cost under the meter's 1%.
    shown = percent(typical(points)) if any(points) else "under 1%"
    return f"~{shown}" if len(points) < ROUGH else shown


def room(points, left):
    """How many more tasks a window with LEFT points runs, from the POINTS its tasks took."""
    if not points:
        return "no task yet"
    if any(points):
        return whole(left / typical(points))
    # Not 1% over all of them: they may each have fallen in a window of its own, so the one bound they give is
    # 1% a task.
    return f"at least {left}"


def seen(done, window):
    """The points each unshared task of the harness a window belongs to took from it."""
    harness = window.split("_")[0]
    return [used[window] for name, _, used, shared in done
            if harness_of(name) == harness and not shared and window in used]


def spends(name, used):
    """The plans a task spent: the one an agy, claude or codex task is named for, or, for any other, every plan
    whose window fell while it ran. That can be another task's spending, so an overlap it makes errs toward
    leaving a cost out rather than counting one twice."""
    harness = harness_of(name)
    if harness in PLANS:
        return {harness}
    return {window.split("_")[0] for window, fall in used.items() if fall}


def percent(points):
    """A share of a window, marked as one: 3%, 0.3%."""
    return f"{round(points, 1):g}%"


def ledger():
    if not usage.LEDGER.exists():
        return []
    with usage.LEDGER.open(encoding="utf-8", newline="") as book:
        return list(csv.DictReader(book))


def moment(text):
    """A ledger time: to the second, or to the minute in a row written before seconds were."""
    return datetime.fromisoformat(text)


def whole(ratio):
    """How many whole tasks a ratio of room to cost is. Floor division of floats undercounts at the boundary:
    1 // 0.1 is 9.0, because 0.1 is a little over a tenth; the ratio, rounded off first, is not."""
    return int(round(ratio, 9))


def paired(rows):
    """(name, minutes, {window: points used}, shared) per paired task. Shared is an overlap with a task that spent
    a plan this one spent, whose points the two readings cannot tell apart; a codex job running beside an agy job
    spends another plan and leaves the codex job's figure its own. A ledger missing a window's columns, as an older
    layout or a hand edit may, has no figure for it."""
    open_, pairs = {}, []
    for row in rows:
        side, _, name = row["note"].partition(": ")
        if side == "before":
            open_[name] = row
        elif side == "after" and name in open_:
            before = open_.pop(name)
            used = {}
            for window in WINDOWS:
                was, now = before.get(f"{window}_left"), row.get(f"{window}_left")
                if was and now and int(was) >= int(now) and same_window(before, row, window):
                    used[window] = int(was) - int(now)
            pairs.append((name, moment(before["time"]), moment(row["time"]), used))
    plans = [spends(name, used) for name, _, _, used in pairs]
    return [(name, (end - start).total_seconds() / 60, used,
             any(s < end and start < e and plans[i] & plans[j] for j, (_, s, e, _) in enumerate(pairs) if j != i))
            for i, (name, start, end, used) in enumerate(pairs)]


def same_window(before, after, window):
    """Whether two readings are of one window: a reset time that moved is a new window, whose figure is no fall from
    the old one's. A reading with no reset time, as the ledger's first rows are, is taken at its word. A window whole
    at the first reading had nothing to lose to a reset: one not started yet was given a made-up reset of a length
    from then, which the real one, set by the first call, lands after."""
    if int(before[f"{window}_left"] or 0) == 100:
        return True
    was, now = before.get(f"{window}_reset"), after.get(f"{window}_reset")
    return not (was and now) or abs((moment(now) - moment(was)).total_seconds()) <= 600


def harness_of(name):
    """The harness that ran a task, the first word of its name; '' for a task named nothing."""
    return (name.split() or [""])[0]


def typical(points):
    """What one task on a harness costs a window: the mean of its last `RECENT`,
    less the dearest when there are enough to spare it and it is not the only
    one that moved it. A mean, because a
    window reads in whole points and most tasks move it by nought or one, so
    a median says nought for a harness that plainly spends; the dearest dropped,
    because spending from another session inside a task's own two readings
    only ever adds."""
    recent = sorted(points[-RECENT:])
    if len(recent) >= 5:
        recent = recent[:-1]
    return statistics.mean(recent) or statistics.mean(sorted(points[-RECENT:]))


def show(lines):
    print("\n".join(lines))
