import copy

import pytest

from remediation.validate import changed_files, meta_problems, path_problems, tampered_tests


def _manifest():
    test = {
        "name": "accepted_values_stg_orders_status", "resource_type": "test", "column_name": "status",
        "attached_node": "model.p.stg_orders", "raw_code": "",
        "test_metadata": {"name": "accepted_values", "namespace": None,
                          "kwargs": {"column_name": "status", "values": ["a", "b"]}},
        "config": {"enabled": True, "severity": "error", "where": None, "warn_if": "!= 0",
                   "error_if": "!= 0", "fail_calc": "count(*)", "limit": None},
        "depends_on": {"macros": ["macro.p.helper"], "nodes": ["model.p.stg_orders"]},
    }
    model = {"name": "stg_orders", "resource_type": "model",
             "config": {"meta": {"remediation_tier": "quarantine"}},
             "columns": {"email": {"config": {"meta": {"pii": True}}}}}
    return {
        "metadata": {"project_name": "p"},
        "nodes": {"test.p.av.1": test, "model.p.stg_orders": model},
        "sources": {},
        "macros": {"macro.p.helper": {"package_name": "p", "original_file_path": "macros/helper.sql",
                                      "depends_on": {"macros": []}}},
    }


def test_unchanged_manifest_has_no_problems():
    m = _manifest()
    assert tampered_tests(m, copy.deepcopy(m), []) == []
    assert meta_problems(m, copy.deepcopy(m), ["remediation_tier", "pii"]) == []


def test_removed_or_redefined_test_is_rejected():
    m, p = _manifest(), _manifest()
    del p["nodes"]["test.p.av.1"]  # redefining a test changes its unique_id
    assert "removed or its definition changed" in tampered_tests(m, p, [])[0]


@pytest.mark.parametrize("key, value", [("severity", "warn"), ("enabled", False),
                                        ("where", "status != 'x'"), ("error_if", ">100")])
def test_reconfigured_test_is_rejected(key, value):
    m, p = _manifest(), _manifest()
    p["nodes"]["test.p.av.1"]["config"][key] = value
    assert "changed (config)" in tampered_tests(m, p, [])[0]


def test_widened_arguments_are_rejected_even_with_same_id():
    m, p = _manifest(), _manifest()
    p["nodes"]["test.p.av.1"]["test_metadata"]["kwargs"]["values"].append("c")
    assert "changed (kwargs)" in tampered_tests(m, p, [])[0]


def test_editing_a_macro_a_test_uses_is_rejected():
    m = _manifest()
    assert "used by test" in tampered_tests(m, copy.deepcopy(m), ["macros/helper.sql"])[0]


def test_new_tests_are_allowed():
    m, p = _manifest(), _manifest()
    p["nodes"]["test.p.new.2"] = {**m["nodes"]["test.p.av.1"], "name": "new"}
    assert tampered_tests(m, p, []) == []


def test_meta_changes_and_introductions_are_rejected():
    m, p = _manifest(), _manifest()
    p["nodes"]["model.p.stg_orders"]["config"]["meta"]["remediation_tier"] = "auto"
    p["nodes"]["model.p.stg_orders"]["columns"]["email"]["config"]["meta"] = {}
    [problem] = meta_problems(m, p, ["remediation_tier", "pii"])
    assert "email.pii" in problem and "remediation_tier" in problem
    p2 = _manifest()
    p2["nodes"]["model.p.new"] = {"name": "new", "resource_type": "model",
                                  "config": {"meta": {"remediation_tier": "auto"}}}
    assert "introduces" in meta_problems(m, p2, ["remediation_tier"])[0]


def test_paths(policy):
    assert path_problems(["models/staging/x.sql", "macros/m.sql", "models/a/b.yml"], policy) == []
    problems = path_problems(["dbt_project.yml", "tests/generic/t.sql", "README.md", "analysis/x.sql"], policy)
    assert len(problems) == 4 and "protected" in problems[0] and "outside" in problems[2]


def test_changed_files_ignores_build_output(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    for root in (a, b):
        (root / "models").mkdir(parents=True)
        (root / "models/x.sql").write_text("select 1")
        (root / "target").mkdir()
    (b / "models/x.sql").write_text("select 2")
    (b / "models/new.sql").write_text("select 3")
    (b / "target/manifest.json").write_text("{}")
    (b / "dev.duckdb").write_text("")
    assert changed_files(a, b) == ["models/new.sql", "models/x.sql"]
