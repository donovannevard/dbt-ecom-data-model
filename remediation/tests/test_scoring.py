import pytest

from remediation.chaos import score_diagnosis
from remediation.inject import load_all

SCENARIOS = load_all()


@pytest.fixture
def broken(tmp_path):
    (tmp_path / "macros").mkdir()
    (tmp_path / "macros/week_bounds.sql").write_text("'(1 - MOD('\n")
    return tmp_path


def _record(verdict, edits=(), error=None):
    if error:
        return {"error": error, "diagnosis": None}
    return {"error": None, "usage": {"cost_usd": 0.1},
            "diagnosis": {"verdict": verdict, "confidence": 0.9, "edits": list(edits)}}


FIX = {"path": "macros/week_bounds.sql", "old": "'(1 - MOD('", "new": "'(0 - MOD('"}


def test_code_scenario_correct_fix_passes(broken):
    s = score_diagnosis(SCENARIOS["week_start_off_by_one"], _record("code_fix", [FIX]), broken)
    assert s["passed"] and s["edits_apply"] and s["files_ok"]


def test_code_scenario_wrong_verdict_misses(broken):
    assert not score_diagnosis(SCENARIOS["week_start_off_by_one"], _record("data_issue"), broken)["passed"]


def test_code_fix_with_unappliable_edit_misses(broken):
    bad = {**FIX, "old": "not in file"}
    s = score_diagnosis(SCENARIOS["week_start_off_by_one"], _record("code_fix", [bad]), broken)
    assert not s["passed"] and s["edit_problems"]


def test_fix_in_wrong_file_misses(broken):
    (broken / "models").mkdir()
    (broken / "models/dim_date.sql").write_text("week_start\n")
    edit = {"path": "models/dim_date.sql", "old": "week_start", "new": "x"}
    s = score_diagnosis(SCENARIOS["week_start_off_by_one"], _record("code_fix", [edit]), broken)
    assert s["edits_apply"] and not s["files_ok"] and not s["passed"]


def test_data_scenario_data_issue_passes(broken):
    assert score_diagnosis(SCENARIOS["orders_null_customer"], _record("data_issue"), broken)["passed"]


def test_data_scenario_quiet_dedupe_misses(broken):
    # orders_null_customer accepts only data_issue: no code can invent the missing customer.
    s = score_diagnosis(SCENARIOS["orders_null_customer"], _record("code_fix", [FIX]), broken)
    assert not s["verdict_ok"] and not s["passed"]


def test_api_error_is_not_a_pass(broken):
    s = score_diagnosis(SCENARIOS["orders_null_customer"], _record(None, error="boom"), broken)
    assert not s["passed"] and s["error"] == "boom"
