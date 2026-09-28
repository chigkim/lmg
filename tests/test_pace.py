import pytest

from ask_ai import pace


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
