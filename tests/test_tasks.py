import pytest

from llm_gateway import tasks


def row(time, note, **left):
    out = {"time": time, "note": note}
    for window in tasks.WINDOWS:
        out[f"{window}_left"] = str(left[window]) if window in left else ""
        out[f"{window}_reset"] = ""
    return out


def test_a_task_costs_what_its_own_two_readings_lost():
    rows = [row("2026-09-27 10:00", "before: codex x", codex_week=80, agy_week=60),
            row("2026-09-27 10:30", "after: codex x", codex_week=78, agy_week=61)]
    [(name, minutes, used, shared)] = tasks.paired(rows)
    assert (name, minutes, shared) == ("codex x", 30, False)
    # agy's window rose, so it was reset during the task and is left out rather than counted as a gain.
    assert used == {"codex_week": 2}


def test_an_overlap_on_the_same_plan_is_shared():
    rows = [row("2026-09-27 10:00", "before: codex a", codex_week=80),
            row("2026-09-27 10:10", "before: codex review b", codex_week=80),
            row("2026-09-27 10:10", "before: agy c", agy_week=50),
            row("2026-09-27 10:20", "after: codex a", codex_week=79),
            row("2026-09-27 10:30", "after: codex review b", codex_week=78),
            row("2026-09-27 10:40", "after: agy c", agy_week=49)]
    assert {name: shared for name, _, _, shared in tasks.paired(rows)} == {
        "codex a": True, "codex review b": True, "agy c": False}


def test_typical_drops_the_dearest_once_there_are_five():
    assert tasks.typical([0, 1, 1]) == pytest.approx(2 / 3)
    assert tasks.typical([1, 1, 1, 1, 9]) == 1


def test_one_task_that_moved_a_window_is_not_dropped_as_the_dearest():
    # Four tasks under the meter's 1% and one of 5%: dropping it would say every task cost under 1%.
    assert tasks.cost([0, 0, 0, 0, 5]) == "1%" and tasks.room([0, 0, 0, 0, 5], 60) == 60


def test_tasks_left_is_divided_by_the_cost_unrounded():
    # Seven tasks of 1/7% each: rounded to 0.1% first, 93% left would be 930 tasks rather than 651.
    assert tasks.room([0, 0, 0, 0, 0, 0, 1, 1], 93) == 651 and tasks.cost([0, 0, 0, 0, 0, 0, 1, 1]) == "0.1%"


def test_a_harness_named_for_a_plan_spends_it_and_another_spends_what_fell_while_it_ran():
    assert tasks.spends("codex x", {}) == {"codex"}
    assert tasks.spends("pi a", {"claude_5h": 3, "codex_5h": 1, "agy_5h": 0}) == {"claude", "codex"}
    assert tasks.spends("new c", {}) == set()


def test_overlap_is_decided_on_the_plans_each_task_spent():
    # pi mostly spends Claude, but here it ran beside a Codex task while Codex's window fell: both are left out.
    rows = [row("2026-09-27 09:00", "before: pi a", claude_5h=80), row("2026-09-27 09:10", "after: pi a", claude_5h=75),
            row("2026-09-27 10:00", "before: pi b", codex_5h=80), row("2026-09-27 10:01", "before: codex c", codex_5h=80),
            row("2026-09-27 10:20", "after: pi b", codex_5h=77), row("2026-09-27 10:30", "after: codex c", codex_5h=75)]
    assert [(name, shared) for name, _, _, shared in tasks.paired(rows)] == [
        ("pi a", False), ("pi b", True), ("codex c", True)]


def test_a_reset_during_a_task_is_no_cost():
    before = row("2026-09-27 10:00", "before: codex x", codex_5h=80)
    after = row("2026-09-27 10:30", "after: codex x", codex_5h=70)
    before["codex_5h_reset"], after["codex_5h_reset"] = "2026-09-27 10:15", "2026-09-27 15:15"
    [(_, _, used, _)] = tasks.paired([before, after])
    assert used == {}


def test_whole_tasks_are_not_undercounted_at_the_boundary():
    assert (1 // 0.1, tasks.whole(1 / 0.1), tasks.whole(0.3 / 0.1), tasks.whole(0.29 / 0.1)) == (9.0, 10, 3, 2)


def test_a_ledger_time_is_read_to_the_second_or_to_the_minute():
    assert tasks.moment("2026-09-28 09:59:30").second == 30
    assert tasks.moment("2026-09-28 09:59").minute == 59


def test_a_task_named_nothing_has_no_harness():
    rows = [row("2026-09-27 10:00", "before: ", codex_week=80), row("2026-09-27 10:30", "after: ", codex_week=79)]
    [(name, _, used, _)] = tasks.paired(rows)
    assert tasks.harness_of(name) == "" and tasks.spends(name, used) == {"codex"}


def test_a_task_starting_on_a_window_not_yet_started_costs_its_fall():
    # The before reading made up a reset of five hours on; the task's first call set the real one twenty minutes later.
    before = row("2026-09-27 10:00:00", "before: pi x", claude_5h=100)
    after = row("2026-09-27 10:40:00", "after: pi x", claude_5h=93)
    before["claude_5h_reset"], after["claude_5h_reset"] = "2026-09-27 15:00:00", "2026-09-27 15:20:00"
    [(_, _, used, _)] = tasks.paired([before, after])
    assert used == {"claude_5h": 7}


def test_an_overlap_of_harnesses_that_have_spent_nothing_is_not_shared():
    rows = [row("2026-09-27 10:00", "before: a x", codex_week=80),
            row("2026-09-27 10:05", "before: b y", codex_week=80),
            row("2026-09-27 10:20", "after: a x", codex_week=80),
            row("2026-09-27 10:30", "after: b y", codex_week=80)]
    assert [shared for *_, shared in tasks.paired(rows)] == [False, False]


def test_an_overlap_of_other_harnesses_on_the_plan_they_spent_is_shared():
    rows = [row("2026-09-27 10:00", "before: pi x", claude_5h=80),
            row("2026-09-27 10:05", "before: claude y", claude_5h=80),
            row("2026-09-27 10:20", "after: pi x", claude_5h=78),
            row("2026-09-27 10:30", "after: claude y", claude_5h=77)]
    assert [shared for *_, shared in tasks.paired(rows)] == [True, True]


def test_help_and_an_option_it_has_not(capsys):
    with pytest.raises(SystemExit) as done:
        tasks.main(["--help"])
    assert done.value.code == 0 and "lmg usage tasks" in capsys.readouterr().out
    with pytest.raises(SystemExit) as done:
        tasks.main(["--tasks"])
    assert done.value.code == 2


def ledger_of(tmp_path, monkeypatch, rows):
    ledger = tmp_path / "usage-ledger.csv"
    monkeypatch.setattr(tasks.usage, "LEDGER", ledger)
    with ledger.open("w", encoding="utf-8", newline="") as book:
        import csv
        writer = csv.DictWriter(book, ["time", "note"] + [f"{w}_{k}" for w in tasks.WINDOWS for k in ("left", "reset")])
        writer.writeheader()
        writer.writerows(rows)


def test_a_window_s_cost_and_tasks_left_come_from_its_harness_s_tasks(tmp_path, monkeypatch):
    # 5h: 60 left at 2% a task runs 30; week: every task moved it nought, so each cost under 1%. One task is rough.
    ledger_of(tmp_path, monkeypatch, [row("2026-09-27 10:00", "before: codex a", codex_5h=90, codex_week=50),
                                      row("2026-09-27 10:30", "after: codex a", codex_5h=88, codex_week=50)])

    class Reading:
        left = 60
    assert tasks.costs({"codex_5h": Reading(), "codex_week": Reading(), "agy_5h": Reading()}) == {
        "codex_5h": ("~2%", 30), "codex_week": ("~under 1%", "at least 60"), "agy_5h": ("-", "no task yet")}


def test_the_list_names_each_window_s_cost_every_task_and_rough_costs(tmp_path, monkeypatch, capsys):
    ledger_of(tmp_path, monkeypatch, [row("2026-09-27 10:00", "before: codex a", codex_5h=90),
                                      row("2026-09-27 10:30", "after: codex a", codex_5h=88),
                                      row("2026-09-27 11:00", "before: pi x", codex_5h=88),
                                      row("2026-09-27 11:30", "after: pi x", codex_5h=85)])
    assert tasks.main([]) == 0
    out = capsys.readouterr().out
    assert "| Codex 5h |       ~2% |              1 |" in out and "- ~: a cost from fewer than 5 tasks" in out
    # pi x ran alone, so what fell is its own; codex a's row shows only its own plan's windows.
    assert "| pi x" in out and out.index("every task") < out.index("what a task costs")


def test_a_ledger_missing_a_column_is_listed_rather_than_a_traceback(tmp_path, monkeypatch, capsys):
    ledger_of(tmp_path, monkeypatch, [row("2026-09-27 10:00", "before: codex a", codex_5h=90),
                                      row("2026-09-27 10:30", "after: codex a", codex_5h=88)])
    path = tasks.usage.LEDGER
    lines = path.read_text().splitlines()
    path.write_text("\n".join(",".join(c.split(",")[:4]) for c in lines) + "\n")
    assert tasks.main([]) == 0 and "every task in the ledger" in capsys.readouterr().out


def test_an_empty_ledger_is_said(tmp_path, monkeypatch, capsys):
    ledger_of(tmp_path, monkeypatch, [])
    assert tasks.main([]) == 0 and "no task in the ledger yet" in capsys.readouterr().out


def test_a_task_with_no_after_counts_as_running_beside_the_tasks_it_overlaps():
    rows = [row("2026-09-27 10:00", "before: codex a", codex_week=80),
            row("2026-09-27 10:05", "before: codex dead", codex_week=80),
            row("2026-09-27 10:05", "before: agy c", agy_week=50),
            row("2026-09-27 10:30", "after: codex a", codex_week=78),
            row("2026-09-27 10:40", "after: agy c", agy_week=49),
            row("2026-09-27 11:00", "before: codex dead", codex_week=78),
            row("2026-09-27 11:20", "after: codex dead", codex_week=77),
            row("2026-09-27 12:00", "before: codex b", codex_week=77),
            row("2026-09-27 12:10", "after: codex b", codex_week=76)]
    # The first `codex dead` never finished: it ran from 10:05 until its name started again at 11:00, beside
    # `codex a` on Codex; `agy c` spent another plan, and `codex b` ran after it had ended.
    assert {name: shared for name, _, _, shared in tasks.paired(rows)} == {
        "codex a": True, "agy c": False, "codex dead": False, "codex b": False}


def test_a_task_still_running_marks_a_task_beside_it_and_a_name_that_is_no_plan_marks_every_plan():
    now = tasks.moment("2026-09-27 11:00")
    beside_codex = [row("2026-09-27 10:00", "before: codex open", codex_week=80),
                    row("2026-09-27 10:05", "before: codex a", codex_week=80),
                    row("2026-09-27 10:20", "after: codex a", codex_week=79)]
    beside_pi = [row("2026-09-27 10:00", "before: pi x", claude_5h=80),
                 row("2026-09-27 10:05", "before: agy a", agy_week=50),
                 row("2026-09-27 10:20", "after: agy a", agy_week=49)]
    assert [shared for *_, shared in tasks.paired(beside_codex, now)] == [True]
    assert [shared for *_, shared in tasks.paired(beside_pi, now)] == [True]


def test_a_task_with_no_after_is_not_taken_to_outrun_the_longest_task_that_finished():
    rows = [row("2026-09-27 10:00", "before: codex dead", codex_week=80),
            row("2026-09-27 10:00", "before: codex a", codex_week=80),
            row("2026-09-27 10:30", "after: codex a", codex_week=79),
            row("2026-09-28 10:00", "before: codex b", codex_week=70),
            row("2026-09-28 10:10", "after: codex b", codex_week=69)]
    # `codex dead` ran beside `codex a`, but a day later it is no longer taken to be running.
    assert {name: shared for name, _, _, shared in tasks.paired(rows, tasks.moment("2026-09-29 00:00"))} == {
        "codex a": True, "codex b": False}
