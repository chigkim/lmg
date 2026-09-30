import pytest

from llm_gateway import pace


def row(time, note, **left):
    out = {"time": time, "note": note}
    for window in pace.WINDOWS:
        out[f"{window}_left"] = str(left[window]) if window in left else ""
        out[f"{window}_reset"] = ""
    return out


def test_a_task_costs_what_its_own_two_readings_lost():
    rows = [row("2026-09-27 10:00", "before: codex x", codex_week=80, agy_week=60),
            row("2026-09-27 10:30", "after: codex x", codex_week=78, agy_week=61)]
    [(name, minutes, used, shared)] = pace.tasks(rows)
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
    assert {name: shared for name, _, _, shared in pace.tasks(rows)} == {
        "codex a": True, "codex review b": True, "agy c": False}


def test_typical_drops_the_dearest_once_there_are_five():
    assert pace.typical([0, 1, 1]) == pytest.approx(0.7)
    assert pace.typical([1, 1, 1, 1, 9]) == 1


def test_a_harness_named_for_a_plan_spends_it_and_another_spends_what_its_tasks_took():
    assert pace.spends([("codex x", {}), ("pi a", {"claude_5h": 3, "codex_5h": 1}), ("pi b", {"claude_week": 1}),
                        ("new c", {})]) == {"codex": "codex", "pi": "claude", "new": None}


def test_a_harness_is_paced_on_the_window_that_fits_fewest():
    done = [("codex a", 10, {"codex_5h": 2, "codex_week": 1}, False)]
    # 5h: 40 spare at 2% a task fits 20; week: 10 spare at 1% fits 10, so the week binds.
    stand = {"codex_5h": (90, 50, 40), "codex_week": (60, 50, 10)}
    assert pace.fit("codex", "codex", done, stand)[:4] == ["codex", "codex_week", "1.00%", "10"]
    stand["codex_5h"] = (60, 50, 10)
    assert pace.fit("codex", "codex", done, stand)[:4] == ["codex", "codex_5h", "2.00%", "5"]
    assert pace.fit("codex", "codex", [], stand)[3] == "no task yet"
    assert pace.fit("codex", "codex", done, {})[3] == "no reading"


def test_a_reset_during_a_task_is_no_cost():
    before = row("2026-09-27 10:00", "before: codex x", codex_5h=80)
    after = row("2026-09-27 10:30", "after: codex x", codex_5h=70)
    before["codex_5h_reset"], after["codex_5h_reset"] = "2026-09-27 10:15", "2026-09-27 15:15"
    [(_, _, used, _)] = pace.tasks([before, after])
    assert used == {}


def test_whole_tasks_are_not_undercounted_at_the_boundary():
    assert (1 // 0.1, pace.whole(1 / 0.1), pace.whole(0.3 / 0.1), pace.whole(0.29 / 0.1)) == (9.0, 10, 3, 2)


def test_a_ledger_time_is_read_to_the_second_or_to_the_minute():
    assert pace.moment("2026-09-28 09:59:30").second == 30
    assert pace.moment("2026-09-28 09:59").minute == 59


def test_a_task_named_nothing_has_no_harness_and_does_not_stop_pace():
    rows = [row("2026-09-27 10:00", "before: ", codex_week=80), row("2026-09-27 10:30", "after: ", codex_week=79)]
    [(name, _, used, _)] = pace.tasks(rows)
    assert pace.harness_of(name) == "" and pace.spends([(name, used)]) == {}


def test_standing_signs_the_spare():
    reading = row("2020-01-01 10:00:00", "reading", codex_5h=30, codex_week=60)
    reading["codex_5h_reset"] = "2020-01-01 12:30"
    reading["codex_week_reset"] = "2020-01-04 10:00"
    # Half the 5h window to run and 30% left is behind pace; three days of seven and 60% left is ahead.
    assert pace.standing(reading) == {"codex_5h": (30, 50, -20), "codex_week": (60, 43, 17)}


def test_a_task_starting_on_a_window_not_yet_started_costs_its_fall():
    # The before reading made up a reset of five hours on; the task's first call set the real one twenty minutes later.
    before = row("2026-09-27 10:00:00", "before: pi x", claude_5h=100)
    after = row("2026-09-27 10:40:00", "after: pi x", claude_5h=93)
    before["claude_5h_reset"], after["claude_5h_reset"] = "2026-09-27 15:00:00", "2026-09-27 15:20:00"
    [(_, _, used, _)] = pace.tasks([before, after])
    assert used == {"claude_5h": 7}


def test_an_overlap_of_harnesses_that_have_spent_nothing_is_not_shared():
    rows = [row("2026-09-27 10:00", "before: a x", codex_week=80),
            row("2026-09-27 10:05", "before: b y", codex_week=80),
            row("2026-09-27 10:20", "after: a x", codex_week=80),
            row("2026-09-27 10:30", "after: b y", codex_week=80)]
    assert [shared for *_, shared in pace.tasks(rows)] == [False, False]


def test_an_overlap_of_other_harnesses_on_the_plan_they_spent_is_shared():
    rows = [row("2026-09-27 10:00", "before: pi x", claude_5h=80),
            row("2026-09-27 10:05", "before: claude y", claude_5h=80),
            row("2026-09-27 10:20", "after: pi x", claude_5h=78),
            row("2026-09-27 10:30", "after: claude y", claude_5h=77)]
    assert [shared for *_, shared in pace.tasks(rows)] == [True, True]


def test_pace_takes_help_and_refuses_an_option_it_has_not(capsys):
    with pytest.raises(SystemExit) as done:
        pace.main(["--help"])
    assert done.value.code == 0 and "--brief" in capsys.readouterr().out
    with pytest.raises(SystemExit) as done:
        pace.main(["--bogus"])
    assert done.value.code == 2
