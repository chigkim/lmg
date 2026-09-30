import pytest

from llm_gateway import pace


def row(time, note, **left):
    out = {"time": time, "note": note}
    for window in pace.WINDOWS:
        out[f"{window}_left"] = str(left[window]) if window in left else ""
        out[f"{window}_reset"] = ""
    return out


def test_a_task_costs_what_its_own_two_readings_lost():
    rows = [row("2026-09-27 10:00", "before: critic x", codex_week=80, agy_week=60),
            row("2026-09-27 10:30", "after: critic x", codex_week=78, agy_week=61)]
    [(name, minutes, used, shared)] = pace.tasks(rows)
    assert (name, minutes, shared) == ("critic x", 30, False)
    # agy's window rose, so it was reset during the task and is left out rather than counted as a gain.
    assert used == {"codex_week": 2}


def test_an_overlap_on_the_same_plan_is_shared(monkeypatch):
    monkeypatch.setattr(pace, "SPENDS", {"critic": "codex", "review": "codex", "worker": "agy"})
    rows = [row("2026-09-27 10:00", "before: critic a", codex_week=80),
            row("2026-09-27 10:10", "before: review b", codex_week=80),
            row("2026-09-27 10:10", "before: worker c", agy_week=50),
            row("2026-09-27 10:20", "after: critic a", codex_week=79),
            row("2026-09-27 10:30", "after: review b", codex_week=78),
            row("2026-09-27 10:40", "after: worker c", agy_week=49)]
    assert {name: shared for name, _, _, shared in pace.tasks(rows)} == {
        "critic a": True, "review b": True, "worker c": False}


def test_typical_drops_the_dearest_once_there_are_five():
    assert pace.typical([0, 1, 1]) == pytest.approx(0.7)
    assert pace.typical([1, 1, 1, 1, 9]) == 1


def test_pace_toml_names_a_window_each_kind(tmp_path):
    path = tmp_path / "pace.toml"
    path.write_text('[paced]\ncritic = "codex_week"\n', encoding="utf-8")
    assert pace.paced(path) == {"critic": "codex_week"}
    assert pace.paced(tmp_path / "missing.toml") == {}


def test_pace_toml_refuses_a_window_that_does_not_exist(tmp_path):
    path = tmp_path / "pace.toml"
    path.write_text('[paced]\ncritic = "codex_month"\n', encoding="utf-8")
    with pytest.raises(SystemExit, match="codex_month"):
        pace.paced(path)


def test_a_reset_during_a_task_is_no_cost():
    before = row("2026-09-27 10:00", "before: critic x", codex_5h=80)
    after = row("2026-09-27 10:30", "after: critic x", codex_5h=70)
    before["codex_5h_reset"], after["codex_5h_reset"] = "2026-09-27 10:15", "2026-09-27 15:15"
    [(_, _, used, _)] = pace.tasks([before, after])
    assert used == {}


def test_whole_tasks_are_not_undercounted_at_the_boundary():
    assert (1 // 0.1, pace.whole(1 / 0.1), pace.whole(0.3 / 0.1), pace.whole(0.29 / 0.1)) == (9.0, 10, 3, 2)


def test_a_ledger_time_is_read_to_the_second_or_to_the_minute():
    assert pace.moment("2026-09-28 09:59:30").second == 30
    assert pace.moment("2026-09-28 09:59").minute == 59


def test_a_task_named_nothing_has_no_kind_and_does_not_stop_pace():
    rows = [row("2026-09-27 10:00", "before: ", codex_week=80), row("2026-09-27 10:30", "after: ", codex_week=79)]
    [(name, _, _, _)] = pace.tasks(rows)
    assert (pace.kind_of(name), pace.plan(name)) == ("", None)


def test_standing_signs_the_spare_and_says_when_each_window_resets():
    reading = row("2020-01-01 10:00:00", "reading", codex_5h=30, codex_week=60)
    reading["codex_5h_reset"] = "2020-01-01 12:30"
    reading["codex_week_reset"] = "2020-01-04 10:00"
    stand = pace.standing(reading)
    # Half the 5h window to run and 30% left is behind pace; three days of seven and 60% left is ahead.
    assert stand["codex_5h"][:3] == (30, 50, -20)
    assert stand["codex_week"][:3] == (60, 43, 17)
    assert stand["codex_5h"][3] == "12:30 PM on 01 Jan, in 2h 30m"
    assert stand["codex_week"][3] == "10:00 AM on 04 Jan, in 3d"
