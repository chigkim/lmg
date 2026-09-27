"""The usage calculator: what each task cost, and how many more fit now.

    uv run --no-sync python ../tools/pace.py            # from the ledger's last reading
    uv run --no-sync python ../tools/pace.py --fresh    # take a reading first
    uv run --no-sync python ../tools/pace.py --brief    # costs and room, no task table

A task is a `before: NAME` row and the next `after: NAME` row in
usage-ledger.csv. `ask.sh` writes both for a hand-off (NAME is `critic x`,
`worker x` or `review sha`), and the session writes its own around a task of
its own with `usage.py "before: session x"` and `usage.py "after: session x"`.
A task's cost is only what its windows lost between its own two readings --
never the drift between other rows, since another session may be spending
the same plans -- and a window that rose instead was reset during the task
and is left out rather than counted as a gain. Spending elsewhere can still
fall inside a task's own two readings, so the cost of a kind leaves out its
dearest task (`typical`); a task another task here overlapped is marked
`shared` and left out, since the two cannot be told apart.

Pace is even spending: a window with a quarter of its length left to run is
on pace with a quarter of it left. The owner's pace for the session is 10% a
half hour, which is exactly even over a 5-hour window, and the weekly windows
are what the critic and the worker are paced on (`PACED`). What a window has
beyond its even line is its spare, and the jobs that fit are that spare over
what one job of that kind costs there. A weekly window reads in whole points
and one job rarely moves it, so a kind whose jobs have not yet moved it is
given the bound its count allows: n jobs that cost nought each cost under
1/n of a point, so at least spare times n more fit.
"""
import csv
import pathlib
import statistics
import subprocess
import sys
from datetime import datetime

TOOLS = pathlib.Path(__file__).parent
LEDGER = TOOLS / "usage-ledger.csv"
LENGTH = {"5h": 5 * 3600, "week": 7 * 24 * 3600}
# The plan each kind of task spends.
SPENDS = {"session": "claude", "critic": "codex", "review": "codex", "worker": "agy"}
WINDOWS = [f"{plan}_{kind}" for plan in ("claude", "codex", "agy") for kind in LENGTH]
# The one window each kind is paced on: the session's 5-hour window, and the
# weekly window of the plan the critic or the worker spends.
PACED = {"session": "claude_5h", "critic": "codex_week", "review": "codex_week",
         "worker": "agy_week"}
RECENT = 10


def moment(text):
    return datetime.strptime(text, "%Y-%m-%d %H:%M")


def tasks(rows):
    """(name, minutes, {window: points used}, shared) per paired task."""
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
                 if (n, s) != (name, start)))
            for name, start, end, used in paired]


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
    minutes = cost / 100 * LENGTH[window.split("_")[1]] / 60
    return f"{minutes:.0f} min" if minutes < 90 else f"{minutes / 60:.1f} h"


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


def main():
    if "--fresh" in sys.argv:
        subprocess.run([sys.executable, str(TOOLS / "usage.py"), "reading: pace"],
                       stdout=subprocess.DEVNULL, check=True)
    with LEDGER.open(encoding="utf-8", newline="") as book:
        rows = list(csv.DictReader(book))
    done = tasks(rows)
    if "--brief" not in sys.argv:
        shown = [w for w in WINDOWS if w != "claude_week"]
        print(f"{'task':<34}{'min':>5}" + "".join(f"{w:>12}" for w in shown))
        for name, minutes, used, shared in done:
            print(f"{name[:34]:<34}{minutes:>5.0f}"
                  + "".join(f"{used.get(w, '-'):>12}" for w in shown)
                  + ("  shared" if shared else ""))
        print()

    costs = {}
    for kind, plan in SPENDS.items():
        spent = [used for name, _, used, shared in done
                 if name.split()[0] == kind and not shared]
        costs[kind] = {w: typical([u[w] for u in spent if w in u])
                       for w in WINDOWS
                       if w.startswith(plan) and any(w in u for u in spent)}
        figures = ", ".join(f"{w} {c:g}" for w, c in costs[kind].items()) or "no paired task yet"
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
              + (f"{spare} to spare" if spare >= 0 else f"{-spare} over pace"))
    print()
    for kind, window in PACED.items():
        seen = [used[window] for name, _, used, shared in done
                if name.split()[0] == kind and not shared and window in used]
        if window not in stand:
            print(f"  {kind:<8} no reading of {window}")
            continue
        spare = max(stand[window][2], 0)
        if not seen:
            print(f"  {kind:<8} no paired task yet; {window} has {spare} to spare")
        elif sum(seen):
            cost = statistics.mean(seen)
            print(f"  {kind:<8} {spare // cost:g} more now, then one every "
                  f"{every(window, cost)} ({window}, {cost:.2f} a task over {len(seen)})")
        else:
            print(f"  {kind:<8} at least {spare * len(seen)} more now, then one every "
                  f"{every(window, 1 / len(seen))} at most ({window}, "
                  f"{len(seen)} tasks moved it nought)")


if __name__ == "__main__":
    main()
