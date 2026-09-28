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
                if was and now and int(was) >= int(now) and same_window(before, row, window):
                    used[window] = int(was) - int(now)
            paired.append((name, moment(before["time"]), moment(row["time"]), used))
    return [(name, (end - start).total_seconds() / 60, used,
             any(s < end and start < e for j, (n, s, e, _) in enumerate(paired)
                 if j != i and plan(n) == plan(name)))
            for i, (name, start, end, used) in enumerate(paired)]


def same_window(before, after, window):
    """Whether two readings are of one window: a reset time that moved is a new window, whose figure is no fall from
    the old one's. A reading with no reset time, as the ledger's first rows are, is taken at its word."""
    was, now = before[f"{window}_reset"], after[f"{window}_reset"]
    return not (was and now) or abs((moment(now) - moment(was)).total_seconds()) <= 600


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
    for kind, plan in SPENDS.items():
        spent = [used for name, _, used, shared in done
                 if name.split()[0] == kind and not shared]
        costs[kind] = (len(spent), {w.split("_")[1]: typical([u[w] for u in spent if w in u])
                                    for w in WINDOWS
                                    if w.startswith(plan) and any(w in u for u in spent)})
    if costs:
        print("what one task of each kind costs")
        show(usage.table(
            ["kind", "plan", "tasks measured", "5h window cost", "week window cost"],
            [[kind, SPENDS[kind], alone] + [percent(c[k]) if k in c else "-" for k in LENGTH]
             for kind, (alone, c) in costs.items()], text=(0, 1),
            notes={"kind": "the first word of a task's name",
                   "plan": "the plan this kind spends, from pace.toml",
                   "tasks measured": "tasks of this kind that ran without overlap",
                   "5h window cost, week window cost": "typical % of that window one task takes: the mean, less"
                                                        " the dearest task once there are five"}))

    latest = next((row for row in reversed(rows) if any(row[f"{w}_reset"] for w in WINDOWS)),
                  None)
    if latest is None:
        print("\nno reading with reset times yet; run with --fresh")
        return
    print(f"\nstanding at {latest['time']} ({latest['note']})")
    stand = standing(latest)
    show(usage.table(
        ["window", "left", "left at even pace", "ahead of pace"],
        [[window, percent(left), percent(even), percent(spare)] for window, (left, even, spare) in stand.items()],
        notes={"left": "% of the window left at that reading",
               "left at even pace": "% that would be left if the window were spent evenly over its length",
               "ahead of pace": "left less left at even pace; negative is behind pace"}))
    print()
    fits = []
    for kind, window in PACED.items():
        seen = [used[window] for name, _, used, shared in done
                if name.split()[0] == kind and not shared and window in used]
        if window not in stand:
            fits.append([kind, window, "-", "no reading", "-", "-"])
            continue
        spare = max(stand[window][2], 0)
        if not seen:
            fits.append([kind, window, "-", "no task yet", "-", 0])
        elif sum(seen):
            cost = statistics.mean(seen)
            fits.append([kind, window, f"{cost:.2f}%", f"{spare // cost:g}", every(window, cost), len(seen)])
        else:
            # Every task moved it 0%, so each cost under the meter's 1%. Not 1% over all of them: they may each have
            # fallen in a window of its own, so the one bound they give is 1% a task.
            fits.append([kind, window, "under 1%", f"at least {spare}", f"{every(window, 1)} at most", len(seen)])
    print("how many more tasks fit without falling behind pace")
    show(usage.table(
        ["kind", "paced on", "cost per task", "fit now", "then one more every", "tasks measured"], fits, text=(0, 1),
        notes={"paced on": "the one window pace.toml names for this kind",
               "cost per task": "mean % of that window one task has used",
               "fit now": "tasks that can run now before the window falls behind even pace",
               "then one more every": "how often one more fits after that, as the window's pace catches up",
               "tasks measured": "tasks the cost per task is the mean of"}))


def show(lines):
    print("\n".join(lines))


if __name__ == "__main__":
    main()
