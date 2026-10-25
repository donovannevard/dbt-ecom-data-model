import pytest

from remediation.config import REPO_ROOT
from remediation.inject import (
    SCRATCH_MARKER, Scenario, ScenarioError, apply, check_fault, load_all,
)
from remediation.risk import RiskModel

SCENARIOS = load_all()


def test_library_covers_both_kinds_tiers_and_absorbed_faults():
    kinds = {s.kind for s in SCENARIOS.values()}
    tiers = {s.expect["tier"] for s in SCENARIOS.values() if not s.absorbed}
    assert kinds == {"data", "code"} and tiers == {"auto", "quarantine"}
    assert any(s.absorbed for s in SCENARIOS.values())


def test_absorbed_scenarios_take_no_verdict_or_tier(tmp_path):
    path = tmp_path / "x.yaml"
    path.write_text("id: x\ntitle: t\nkind: data\ndescription: d\n"
                    "faults: [{op: duplicate_row, file: f.csv, row: 0}]\n"
                    "expect: {failing_tests: [], warning_tests: [{model: m, test: unique}], tier: auto}\n")
    with pytest.raises(ScenarioError, match="absorbed"):
        Scenario.load(path)


@pytest.mark.parametrize("scenario", SCENARIOS.values(), ids=SCENARIOS.keys())
def test_every_fault_still_applies_to_the_current_tree(scenario):
    # Catches rot cheaply: a refactor that moves an anchor breaks this, not
    # just the slow end-to-end chaos run.
    for fault in scenario.faults:
        check_fault(REPO_ROOT, fault)


@pytest.mark.parametrize(
    "scenario", [s for s in SCENARIOS.values() if s.kind == "code"],
    ids=[s.id for s in SCENARIOS.values() if s.kind == "code"],
)
def test_code_scenario_tier_matches_lineage(scenario, real_manifest, policy):
    tier = RiskModel(real_manifest, policy).assess_files(scenario.expect["fix_files"]).tier
    assert tier == scenario.expect["tier"]


def test_apply_refuses_unmarked_directory(tmp_path):
    with pytest.raises(ScenarioError, match="not a scratch checkout"):
        apply(SCENARIOS["week_start_off_by_one"], tmp_path)


def _scratch(tmp_path, files: dict) -> None:
    (tmp_path / SCRATCH_MARKER).write_text("")
    for rel, text in files.items():
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_text(text)


def _scenario(faults, kind="data") -> Scenario:
    return Scenario("t", "t", kind, "", faults, {"failing_tests": [{}], "verdicts": ["data_issue"], "tier": "auto"})


def test_csv_ops(tmp_path):
    _scratch(tmp_path, {"s.csv": "id,v\n1,a\n2,b\n"})
    apply(_scenario([
        {"op": "duplicate_row", "file": "s.csv", "row": 0},
        {"op": "set_value", "file": "s.csv", "column": "v", "rows": [2], "value": ""},
    ]), tmp_path)
    assert (tmp_path / "s.csv").read_text() == "id,v\n1,a\n1,a\n2,\n"


def test_ambiguous_anchor_rejected_without_half_applying(tmp_path):
    _scratch(tmp_path, {"a.sql": "x\n", "b.sql": "y y\n"})
    with pytest.raises(ScenarioError, match="found 2"):
        apply(_scenario([
            {"op": "replace", "file": "a.sql", "old": "x", "new": "z"},
            {"op": "replace", "file": "b.sql", "old": "y", "new": "z"},
        ]), tmp_path)
    assert (tmp_path / "a.sql").read_text() == "x\n"


def test_bad_row_and_column_rejected(tmp_path):
    _scratch(tmp_path, {"s.csv": "id\n1\n"})
    with pytest.raises(ScenarioError, match="out of range"):
        check_fault(tmp_path, {"op": "duplicate_row", "file": "s.csv", "row": 5})
    with pytest.raises(ScenarioError, match="no column"):
        check_fault(tmp_path, {"op": "set_value", "file": "s.csv", "column": "nope", "rows": [0], "value": ""})
