import pytest

from remediation import pipeline
from remediation.github import LocalTracker, marker
from remediation.remediate import Outcome
from remediation.tests.test_gitops import ME
from remediation.gitops import git
from remediation.validate import Validation

BUNDLE = {"failures": [{"unique_id": "test.p.unique_m_id", "name": "unique_m_id", "status": "fail", "model": "m",
                        "kind": "test_failure", "test": {"type": "unique"}}]}


def _record(verdict="code_fix", error=None):
    if error:
        return {"error": error, "diagnosis": None}
    return {"error": None, "model": "claude-opus-5-5", "cost_usd": 0.05, "diagnosis": {
        "verdict": verdict, "confidence": 0.9, "root_cause": "Because.", "evidence": ["e"], "upstream_action":
        "Fix the source." if verdict != "code_fix" else "", "self_assessed_risk": "low",
        "edits": [{"path": "models/m.sql", "old": "1", "new": "2"}] if verdict == "code_fix" else []}}


@pytest.fixture
def env(tmp_path, policy, monkeypatch):
    repo = tmp_path / "repo"
    git(tmp_path, "init", "-b", "main", str(repo))
    (repo / "models").mkdir()
    (repo / "models" / "m.sql").write_text("select 1\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-m", "base", env=ME)
    tracker = LocalTracker(tmp_path / "tracker")

    def go(record, outcome=None, **kw):
        if outcome is not None:
            def fake(bundle, base, patched, log, policy, **k):
                patched.mkdir(parents=True, exist_ok=True)
                (patched / "models").mkdir(exist_ok=True)
                (patched / "models" / "m.sql").write_text("select 2\n")
                return outcome
            monkeypatch.setattr(pipeline, "remediate", fake)
        ask = kw.pop("ask", lambda *a: pytest.fail("should not ask"))
        return pipeline.run(kw.pop("bundle", BUNDLE), repo, policy, tracker, repo=repo, base_ref="main",
                            remote=None, ask=ask, work_dir=tmp_path / "work", first=record, **kw)
    return go, tracker


def _validated(route):
    v = Validation(passed=True, tier=route, route=route, changed_files=["models/m.sql"])
    return Outcome(status="validated", record=_record(), validation=v, attempts=[{"attempt": 1, "cost_usd": 0.05}])


def test_nothing_failed(env):
    go, _ = env
    assert go(None, bundle={"failures": []}).action == "nothing_failed"


@pytest.mark.parametrize("route, reviewers", [("auto", []), ("quarantine", ["donovannevard"])])
def test_validated_fix_opens_a_labelled_pr(env, route, reviewers):
    go, tracker = env
    result = go(_record(), _validated(route))
    assert result.action == "pr_opened"
    [pr] = tracker.list_open("pr", "ai-remediation")
    assert pr["labels"] == ["ai-remediation", f"risk:{route}"] and pr["reviewers"] == reviewers
    assert pr["head"].startswith("ai-remediation/m-") and "+select 2" in pr["body"]


@pytest.mark.parametrize("verdict, label", [("data_issue", "data-issue"), ("needs_human", "needs-human")])
def test_non_code_verdicts_open_issues(env, verdict, label):
    go, tracker = env
    assert go(_record(verdict)).action == "issue_opened"
    [issue] = tracker.list_open("issue", "ai-remediation")
    assert label in issue["labels"] and "Fix the source." in issue["body"]


def test_diagnosis_error_opens_needs_human_issue(env):
    go, tracker = env
    assert go(_record(error="declined (cyber)")).action == "issue_opened"
    assert "declined (cyber)" in tracker.list_open("issue", "ai-remediation")[0]["body"]


def test_rejected_fix_opens_issue_with_attempts(env):
    go, tracker = env
    rejected = Outcome(status="rejected", record=_record(),
                       attempts=[{"attempt": 1, "validation": "tests: test x changed (config)"}])
    assert go(_record(), rejected).action == "issue_opened"
    assert "tests: test x changed" in tracker.list_open("issue", "ai-remediation")[0]["body"]


def test_recurring_failure_comments_and_never_asks(env):
    go, tracker = env
    go(_record("data_issue"))
    result = go(None)  # ask would fail the test
    assert result.action == "recurred"
    assert "recurred" in tracker.list_open("issue", "ai-remediation")[0]["comments"][0]


def test_secret_scan_blocks_keys_and_env_files(tmp_path):
    from remediation.chaos import secret_problems

    (tmp_path / "ok.sql").write_text("select 1")
    (tmp_path / ".env.example").write_text("ANTHROPIC_API_KEY=\n")
    assert secret_problems(tmp_path) == []
    (tmp_path / "mentions.py").write_text("# keys start with sk-ant- and look like sk-ant-...\n")
    assert secret_problems(tmp_path) == []  # a mention isn't a secret
    (tmp_path / ".env").write_text("X=1")
    # Built at runtime so this test file never contains a key-shaped string.
    (tmp_path / "leak.py").write_text("key = '" + "sk-ant-" + "api03" + "x" * 40 + "'")
    (tmp_path / "id_rsa").write_text("-----BEGIN " + "RSA PRIVATE KEY-----\nabc\n")
    assert sorted(p.split(":")[0] for p in secret_problems(tmp_path)) == [".env", "id_rsa", "leak.py"]


def test_secret_scan_passes_on_the_project_itself():
    # The scanner, its tests and the rest of the repo must not trip it, or
    # every sandbox push is refused (it once flagged its own patterns).
    from remediation.chaos import _source_files, secret_problems
    from remediation.config import REPO_ROOT
    import shutil, tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as tmp:
        for rel in _source_files():
            if rel.startswith(".env"):
                continue
            (Path(tmp) / rel).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(REPO_ROOT / rel, Path(tmp) / rel)
        assert secret_problems(Path(tmp)) == []


def test_open_pr_is_refreshed_on_a_moved_base(tmp_path, policy, monkeypatch):
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

    def fake(bundle, base, patched, log, policy, **k):
        (patched / "models").mkdir(parents=True, exist_ok=True)
        (patched / "models" / "m.sql").write_text("select 2\n")
        return _validated("auto")
    monkeypatch.setattr(pipeline, "remediate", fake)
    kw = dict(repo=repo, base_ref="main", remote="origin", ask=lambda *a: pytest.fail("no asking"),
              work_dir=tmp_path / "work", first=_record(), refresh=True)

    opened = pipeline.run(BUNDLE, repo, policy, tracker, **kw)
    branch = tracker.list_open("pr", "ai-remediation")[0]["head"]
    first_sha = git(repo, "ls-remote", "origin", f"refs/heads/{branch}").split()[0]

    (repo / "README.md").write_text("main moved on\n")  # the base advances underneath the PR
    git(repo, "add", "-A")
    git(repo, "commit", "-m", "update", env=ME)
    refreshed = pipeline.run(BUNDLE, repo, policy, tracker, **kw)

    assert (opened.action, refreshed.action, refreshed.number) == ("pr_opened", "pr_refreshed", opened.number)
    new_sha = git(repo, "ls-remote", "origin", f"refs/heads/{branch}").split()[0]
    assert new_sha != first_sha
    assert git(repo, "merge-base", "--is-ancestor", "main", new_sha) == ""  # rebuilt on the new main
    pr = tracker.list_open("pr", "ai-remediation")[0]
    assert "Refreshed" in pr["comments"][0] and len(tracker.list_open("pr", "ai-remediation")) == 1


def test_github_sandbox_syncs_instead_of_needing_an_empty_repo(tmp_path):
    from remediation.chaos import _sync_github_sandbox

    remote = tmp_path / "remote.git"
    git(tmp_path, "init", "--bare", "-b", "main", str(remote))

    def sync(content, label):
        repo = tmp_path / "sandbox"
        import shutil
        shutil.rmtree(repo, ignore_errors=True)
        repo.mkdir()
        (repo / "m.sql").write_text(content)
        git(repo, "init", "-b", "main")
        git(repo, "add", "-A")
        import os
        os.environ.update(ME)
        try:
            _sync_github_sandbox(repo, "o/r", label, url=str(remote))
        finally:
            for k in ME:
                os.environ.pop(k, None)

    sync("v1\n", "first")
    sync("v2\n", "second")
    sync("v2\n", "unchanged")  # nothing to commit, nothing pushed
    log = git(remote, "log", "--format=%s", "main").splitlines()
    assert log == ["Sync sandbox with the current project (second fault applied)",
                   "Sync sandbox with the current project (first fault applied)"]


def test_a_closed_pr_is_never_refreshed_a_new_one_opens(env):
    go, tracker = env
    first = go(_record(), _validated("auto"), refresh=True)
    tracker.close(first.number, "closed by a person")
    second = go(_record(), _validated("auto"), refresh=True)
    assert second.action == "pr_opened" and second.number != first.number


def test_github_listing_reads_live_state_and_filters_labels_itself(monkeypatch):
    import json as _json

    from remediation.github import GitHubTracker

    calls = []

    def fake_gh(self, *args, body=None):
        calls.append(args)
        return _json.dumps([
            {"number": 1, "title": "a", "body": "", "url": "u1", "state": "OPEN", "headRefName": "b1",
             "labels": [{"name": "ai-remediation"}]},
            {"number": 2, "title": "b", "body": "", "url": "u2", "state": "OPEN", "headRefName": "b2",
             "labels": [{"name": "other"}]},
            {"number": 3, "title": "c", "body": "", "url": "u3", "state": "CLOSED", "headRefName": "b3",
             "labels": [{"name": "ai-remediation"}]},
        ])
    monkeypatch.setattr(GitHubTracker, "_gh", fake_gh)
    items = GitHubTracker().list_open("pr", "ai-remediation")
    assert [i["number"] for i in items] == [1] and items[0]["head"] == "b1"
    assert "--label" not in calls[0]  # the search index path is never used


def test_new_pr_never_overwrites_a_closed_prs_branch(tmp_path, policy, monkeypatch):
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

    def fake(bundle, base, patched, log, policy, **k):
        (patched / "models").mkdir(parents=True, exist_ok=True)
        (patched / "models" / "m.sql").write_text("select 2\n")
        return _validated("auto")
    monkeypatch.setattr(pipeline, "remediate", fake)
    kw = dict(repo=repo, base_ref="main", remote="origin", ask=None, work_dir=tmp_path / "work", first=_record())

    first = pipeline.run(BUNDLE, repo, policy, tracker, **kw)
    old_branch = tracker.list_open("pr", "ai-remediation")[0]["head"]
    old_sha = git(repo, "ls-remote", "origin", f"refs/heads/{old_branch}").split()[0]
    tracker.close(first.number, "closed")
    second = pipeline.run(BUNDLE, repo, policy, tracker, **kw)

    new_branch = tracker.list_open("pr", "ai-remediation")[0]["head"]
    assert second.action == "pr_opened" and new_branch == f"{old_branch}-2"
    assert git(repo, "ls-remote", "origin", f"refs/heads/{old_branch}").split()[0] == old_sha  # untouched
