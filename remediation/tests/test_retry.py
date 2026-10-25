"""Phase 5 (retry_pr) and Phase 6 (auto_merge_decision), against a real git
repo with a bare remote and the local tracker."""

import pytest

from remediation import pipeline
from remediation.github import LocalTracker, read_marker
from remediation.gitops import git
from remediation.remediate import Outcome
from remediation.tests.test_gitops import ME
from remediation.tests.test_pipeline import BUNDLE, _record
from remediation.validate import Validation


def _validated(route="auto", files=("models/m.sql",)):
    v = Validation(passed=True, tier=route, route=route, changed_files=list(files))
    return Outcome(status="validated", record=_record(), validation=v, attempts=[{"attempt": 1, "cost_usd": 0.05}],
                   cost_usd=0.05)


def _deterministic():
    return {**_record(), "model": "deterministic remediation", "served_by": "deterministic remediation"}


@pytest.fixture
def world(tmp_path, policy, monkeypatch, request):
    first = request.param if hasattr(request, "param") else _deterministic()
    remote = tmp_path / "remote.git"
    git(tmp_path, "init", "--bare", "-b", "main", str(remote))
    repo = tmp_path / "repo"
    git(tmp_path, "init", "-b", "main", str(repo))
    (repo / "models").mkdir()
    (repo / "models" / "m.sql").write_text("select 1\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-m", "base", env=ME)
    git(repo, "remote", "add", "origin", str(remote))
    git(repo, "push", "origin", "main")
    tracker = LocalTracker(tmp_path / "tracker")
    content = {"next": "select 2\n"}

    def fake_remediate(bundle, base, patched, log, policy, **k):
        (patched / "models").mkdir(parents=True, exist_ok=True)
        (patched / "models" / "m.sql").write_text(content["next"])
        outcome = _validated()
        outcome.record = k.get("first") or outcome.record  # as the real one: the attempt it was given
        return outcome
    monkeypatch.setattr(pipeline, "remediate", fake_remediate)
    monkeypatch.setattr(pipeline, "auto_merge_decision", lambda *a: (False, "off in tests"))
    pipeline.run(BUNDLE, repo, policy, tracker, repo=repo, base_ref="main", remote="origin", ask=None,
                 work_dir=tmp_path / "work", first=first)
    git(repo, "fetch", "origin")
    asked = []

    def retry(**over):
        pr = tracker.list_open("pr", "ai-remediation")[0]
        git(repo, "fetch", "origin")
        kw = dict(repo=repo, base_ref="main", remote="origin", work_dir=tmp_path / "work",
                  ask=lambda b, p, previous: asked.append(previous) or _record(),
                  ci_jobs=["config parses on Snowflake, Redshift + BigQuery"],
                  ci_log="MissingArgumentsPropertyInGenericTestDeprecation")
        kw.update(over)
        return pipeline.retry_pr(pr, kw.pop("bundle", BUNDLE), repo, policy, tracker, run_label="ci", **kw)
    return tracker, retry, content, asked, repo


def _pr(tracker):
    return tracker.list_open("pr", "ai-remediation")[0]


def test_route_b_deterministic_fix_fails_ci_claude_gets_one_attempt(world):
    tracker, retry, content, asked, repo = world
    content["next"] = "select 3\n"
    result = retry()
    assert result.action == "retried"
    pr = _pr(tracker)
    assert read_marker(pr["body"], "remediation-attempt") == "2"
    assert read_marker(pr["body"], "remediation-ci-failures") == "1"
    assert read_marker(pr["body"], "remediation-claude-attempts") == "1"
    assert "Deprecation" in asked[0][0]["rejection"] and "+select 2" in asked[0][0]["edits"]
    assert "select 3" in git(repo, "show", f"origin/{pr['head']}:models/m.sql")
    assert "Attempt 2 pushed" in pr["comments"][-1]
    # Claude's attempt is credited to Claude.
    assert git(repo, "log", "-1", "--format=%an", f"origin/{pr['head']}") == "Claude"


def test_route_b_claudes_attempt_failing_ci_stops_the_loop(world):
    tracker, retry, content, asked, _ = world
    content["next"] = "select 3\n"
    assert retry().action == "retried"
    assert retry().action == "blocked"
    pr = _pr(tracker)
    assert "ai-remediation:blocked" in pr["labels"] and len(asked) == 1
    assert "at most 1 attempt per PR" in pr["comments"][-1]
    assert retry().action == "already_blocked"


@pytest.mark.parametrize("world", [_record()], indirect=True)
def test_route_a_claudes_own_fix_failing_ci_stops_without_asking(world):
    tracker, retry, content, asked, _ = world
    assert retry().action == "blocked" and asked == []
    assert read_marker(_pr(tracker)["body"], "remediation-ci-failures") == "1"


def test_a_deterministic_fix_is_not_credited_to_claude(world):
    tracker, retry, content, asked, repo = world
    head = _pr(tracker)["head"]
    log = git(repo, "log", "-1", "--format=%an <%ae>%n%B", f"origin/{head}")
    assert log.startswith("dbt-ecom-remediation <") and "Co-Authored-By: Claude" not in log


def test_an_attempt_identical_to_a_failed_one_blocks(world):
    tracker, retry, content, asked, _ = world
    content["next"] = "select 2\n"  # the same change that just failed CI
    assert retry().action == "blocked"
    assert "oscillation" in _pr(tracker)["comments"][-1]


def test_the_cost_ceiling_blocks(world, policy):
    tracker, retry, content, asked, _ = world
    pr = _pr(tracker)
    tracker.update(pr["number"], pr["title"], pr["body"].replace("<!-- remediation-cost: 0.0500 -->",
                                                                 "<!-- remediation-cost: 5.0000 -->"))
    assert retry().action == "blocked" and not asked
    assert "ceiling" in _pr(tracker)["comments"][-1]


def test_a_base_that_no_longer_fails_closes_the_pr(world):
    tracker, retry, *_ = world
    assert retry(bundle={"failures": []}).action == "closed_not_needed"
    assert tracker.list_open("pr", "ai-remediation") == []


def test_a_changed_base_failure_blocks(world):
    tracker, retry, *_ = world
    other = {"failures": [{**BUNDLE["failures"][0], "unique_id": "test.p.something_else"}]}
    assert retry(bundle=other).action == "blocked"


# -- Phase 6 -------------------------------------------------------------------

@pytest.fixture
def automerge_env(monkeypatch, real_manifest):
    monkeypatch.setenv("AI_REMEDIATION_AUTOMERGE", "true")


def _v(route, files, escalations=()):
    return Validation(passed=True, route=route, tier=route, changed_files=list(files), escalations=list(escalations))


def test_allowlisted_auto_model_may_auto_merge(automerge_env, policy):
    from remediation.config import REPO_ROOT
    ok, why = pipeline.auto_merge_decision(policy, _v("auto", ["models/analytics/agg_marketing.sql"]), REPO_ROOT)
    assert ok and "agg_marketing" in why


def test_macro_change_counts_its_calling_models(automerge_env, policy):
    from remediation.config import REPO_ROOT
    ok, _ = pipeline.auto_merge_decision(policy, _v("auto", ["macros/week_bounds.sql"]), REPO_ROOT)
    assert ok  # week_bounds reaches only dim_date, which is allowlisted


@pytest.mark.parametrize("validation, reason", [
    (_v("quarantine", ["models/analytics/fct_transactions.sql"]), "routed `quarantine`"),
    (_v("auto", ["models/analytics/fct_sessions.sql"]), "not approved for auto-merge: fct_sessions"),
    (_v("quarantine", ["models/analytics/agg_marketing.sql"], ["confidence 0.60 is below 0.8"]), "confidence"),
])
def test_auto_merge_refusals(automerge_env, policy, validation, reason):
    from remediation.config import REPO_ROOT
    ok, why = pipeline.auto_merge_decision(policy, validation, REPO_ROOT)
    assert not ok and reason in why


def test_the_repository_variable_is_a_kill_switch(monkeypatch, policy, real_manifest):
    from remediation.config import REPO_ROOT
    monkeypatch.setenv("AI_REMEDIATION_AUTOMERGE", "false")
    ok, why = pipeline.auto_merge_decision(policy, _v("auto", ["models/analytics/agg_marketing.sql"]), REPO_ROOT)
    assert not ok and "AI_REMEDIATION_AUTOMERGE" in why


def test_eligible_pr_gets_auto_merge_and_label(tmp_path, policy, monkeypatch):
    repo = tmp_path / "repo"
    git(tmp_path, "init", "-b", "main", str(repo))
    (repo / "models").mkdir()
    (repo / "models" / "m.sql").write_text("select 1\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-m", "base", env=ME)
    tracker = LocalTracker(tmp_path / "tracker")

    def fake(bundle, base, patched, log, policy, **k):
        (patched / "models").mkdir(parents=True, exist_ok=True)
        (patched / "models" / "m.sql").write_text("select 2\n")
        return _validated()
    monkeypatch.setattr(pipeline, "remediate", fake)
    monkeypatch.setattr(pipeline, "auto_merge_decision", lambda *a: (True, "`m` approved for auto-merge"))
    pipeline.run(BUNDLE, repo, policy, tracker, repo=repo, base_ref="main", remote=None, ask=None,
                 work_dir=tmp_path / "work", first=_record())
    pr = _pr(tracker)
    assert "ai-remediation:auto-merge" in pr["labels"] and read_marker(pr["body"], "remediation-auto-merge") == "yes"
    assert "Auto-merge on" in pr["body"]


def test_risk_label_follows_the_current_attempt(world, monkeypatch):
    tracker, retry, content, asked, _ = world
    pr = _pr(tracker)
    tracker.remove_labels(pr["number"], ["risk:auto"])
    tracker.add_labels(pr["number"], ["risk:quarantine"])  # as if attempt 1 had been quarantine
    content["next"] = "select 3\n"
    assert retry().action == "retried"  # attempt 2 validates as auto
    labels = _pr(tracker)["labels"]
    assert "risk:auto" in labels and "risk:quarantine" not in labels
