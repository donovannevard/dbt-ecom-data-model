import pytest

from remediation.risk import CODEOWNERS_PATH, RiskModel, render_codeowners


@pytest.fixture
def risk(manifest, policy):
    return RiskModel(manifest, policy)


def test_policy_default_is_strictest(policy):
    assert policy["default_tier"] == policy["tiers"][-1]


def test_agent_cannot_edit_its_own_guardrails(policy):
    from remediation.config import matches_any
    for path in [".github/workflows/dbt_ci.yml", ".github/CODEOWNERS", "remediation/policy.yaml",
                 "remediation/tests/test_risk.py", "dbt_project.yml", "tests/generic/positive_value.sql"]:
        assert matches_any(path, policy["protected_paths"]), path


def test_auto_model_with_auto_lineage_is_auto(risk):
    assert risk.assess_files(["models/stg_b.sql"]).tier == "auto"


def test_downstream_quarantine_escalates_and_names_the_path(risk):
    a = risk.assess_files(["models/stg_a.sql"])
    assert a.tier == "quarantine"
    assert a.reasons[0].path == ["stg_a", "int_a", "fct_rev"]


def test_untagged_node_takes_default(risk):
    assert risk.assess_files(["models/untagged.sql"]).tier == "quarantine"


def test_unknown_tier_value_fails_safe(risk):
    assert risk.assess_files(["models/typo.sql"]).tier == "quarantine"


def test_unmapped_and_protected_files_are_quarantined(risk):
    assert risk.assess_files(["docs/notes.md"]).reasons[0].detail == "maps to no dbt node"
    assert risk.assess_files(["remediation/risk.py"]).reasons[0].detail == "protected path"


def test_yaml_file_takes_strictest_model_it_patches(risk):
    assert risk.assess_files(["models/schema_b.yml"]).tier == "auto"
    assert risk.assess_files(["models/schema.yml"]).tier == "quarantine"  # stg_a feeds fct_rev


def test_macro_tier_follows_transitive_callers(risk):
    # inner is called only via outer, which only dim_b (auto) calls.
    assert risk.assess_files(["macros/inner.sql"]).tier == "auto"
    assert risk.assess_files(["macros/shared.sql"]).tier == "quarantine"


def test_macro_used_by_test_tiers_as_attached_model(risk):
    assert risk.nodes_for_file("macros/test_macro.sql") == {"model.proj.dim_b"}


def test_change_set_takes_strictest_file(risk):
    assert risk.assess_files(["models/stg_b.sql", "models/stg_a.sql"]).tier == "quarantine"


def test_empty_change_set_is_quarantined(risk):
    assert risk.assess_files([]).tier == "quarantine"


# -- against the real project ------------------------------------------------

AUTO_MODELS = {
    "dim_date", "fct_sessions", "agg_marketing",
    "int_session__marketing_channel_classification", "stg_google_analytics__sessions",
}


def test_real_auto_tier_is_exactly_the_reviewed_set(real_manifest, policy):
    # Widening the auto tier must be a deliberate edit to this test.
    risk = RiskModel(real_manifest, policy)
    auto = {
        n["name"] for uid, n in real_manifest["nodes"].items()
        if n["resource_type"] == "model" and risk.assess_node(uid).tier == "auto"
    }
    assert auto == AUTO_MODELS


@pytest.mark.parametrize("path, tier", [
    ("macros/week_bounds.sql", "auto"),               # dim_date only
    ("macros/classify_by_taxonomy.sql", "quarantine"),  # also used by marketing -> agg_channel
    ("models/staging/marketing/google_ads/stg_google_ads__ads.sql", "quarantine"),  # gclid -> revenue
    ("models/staging/prod/stg_web_visits.sql", "quarantine"),  # fct_visits carries customer_id
    ("models/staging/staging.yaml", "quarantine"),     # mixed-tier YAML
    ("seeds/marketing_channel_taxonomy.csv", "quarantine"),
])
def test_real_project_tiers(real_manifest, policy, path, tier):
    assert RiskModel(real_manifest, policy).assess_files([path]).tier == tier


def test_codeowners_is_up_to_date(real_manifest, policy):
    rendered = render_codeowners(RiskModel(real_manifest, policy))
    assert CODEOWNERS_PATH.read_text() == rendered, \
        "run `python -m remediation.risk codeowners --write`"
