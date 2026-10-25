import pytest

from remediation import gitops
from remediation.gitops import commit_to_branch, failure_id, git
from remediation.validate import Validation

BOT = ("remediation-bot", "bot@example.com")
ME = {"GIT_AUTHOR_NAME": "me", "GIT_AUTHOR_EMAIL": "me@x", "GIT_COMMITTER_NAME": "me", "GIT_COMMITTER_EMAIL": "me@x"}


@pytest.fixture
def repo(tmp_path):
    remote, repo = tmp_path / "remote.git", tmp_path / "repo"
    git(tmp_path, "init", "--bare", "-b", "main", str(remote))
    git(tmp_path, "init", "-b", "main", str(repo))
    (repo / "models").mkdir()
    (repo / "models" / "m.sql").write_text("select 1\n")
    (repo / "models" / "old.sql").write_text("select 0\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-m", "base", env=ME)
    git(repo, "remote", "add", "origin", str(remote))
    git(repo, "push", "origin", "main")
    return repo, remote


def test_failure_id_is_order_insensitive_and_stable():
    a = {"failures": [{"unique_id": "test.x"}, {"unique_id": "test.y"}]}
    b = {"failures": [{"unique_id": "test.y"}, {"unique_id": "test.x"}]}
    assert failure_id(a) == failure_id(b) and len(failure_id(a)) == 12
    assert failure_id(a) != failure_id({"failures": [{"unique_id": "test.x"}]})


def test_branch_name():
    bundle = {"failures": [{"name": "t", "model": "fct_Transactions!"}]}
    assert gitops.branch_name(bundle, "abcdef123456", "ai-remediation/") == "ai-remediation/fct-transactions-abcdef12"


def test_commit_pushes_a_branch_without_touching_the_checkout(repo):
    repo_dir, remote = repo
    sha, diff = commit_to_branch(repo_dir, "main", "ai-remediation/x", {"models/m.sql": b"select 2\n",
                                 "models/old.sql": None}, "fix: x\n\nRemediation-Attempt: 1\n", BOT, "origin")
    assert "-select 1" in diff and "+select 2" in diff and "deleted file" in diff
    assert git(remote, "rev-parse", "refs/heads/ai-remediation/x") == sha
    assert git(remote, "log", "-1", "--format=%an <%ae>", "ai-remediation/x") == "remediation-bot <bot@example.com>"
    # The working checkout is untouched: still on main, same files, no local branch, no worktree left.
    assert git(repo_dir, "branch", "--list").strip() == "* main"
    assert (repo_dir / "models" / "m.sql").read_text() == "select 1\n"
    assert len(git(repo_dir, "worktree", "list").splitlines()) == 1


def test_replacing_a_branch_needs_the_right_lease(repo):
    repo_dir, _ = repo
    first, _ = commit_to_branch(repo_dir, "main", "b", {"models/m.sql": b"a\n"}, "one", BOT, "origin")
    second, _ = commit_to_branch(repo_dir, "main", "b", {"models/m.sql": b"b\n"}, "two", BOT, "origin", lease=first)
    with pytest.raises(RuntimeError, match="push"):
        commit_to_branch(repo_dir, "main", "b", {"models/m.sql": b"c\n"}, "three", BOT, "origin", lease=first)
    assert second != first


def test_commit_message_and_body_carry_machine_readable_state():
    bundle = {"failures": [{"unique_id": "t1", "name": "unique_m_id", "status": "fail", "model": "m",
                            "kind": "test_failure", "test": {"type": "unique"}}]}
    record = {"model": "claude-opus-5-5", "diagnosis": {"root_cause": "Wrong key.", "confidence": 0.9,
              "evidence": ["e"], "self_assessed_risk": "low"}}
    v = Validation(passed=True, tier="auto", route="auto")
    msg = gitops.commit_message(bundle, record, v, "abc", 1, ("Claude", "noreply@anthropic.com"))
    assert msg.splitlines()[0] == "fix(m): repair 1 failing unique check"
    assert "Remediation-Failure-Id: abc" in msg and "Remediation-Route: auto" in msg
    assert msg.rstrip().endswith("Co-Authored-By: Claude <noreply@anthropic.com>")
    state = {"attempt": 2, "ci_failures": 1, "cost_usd": 0.12,
             "history": [{"attempt": 1, "jobs": ["config parses"], "log": "boom"}]}
    body = gitops.pr_body(bundle, record, v, [{"attempt": 1, "cost_usd": 0.05}], "x" * 40000, "abc", state, "note",
                          "Auto-merge off: routed quarantine.")
    assert "<!-- remediation-failure-id: abc -->" in body and "<!-- remediation-ci-failures: 1 -->" in body
    assert "<!-- remediation-attempt: 2 -->" in body and "diff truncated" in body
    assert "Attempt 1** failed CI in: config parses" in body and "Auto-merge off" in body
    from remediation.github import read_marker
    assert gitops.decode_history(read_marker(body, "remediation-history")) == state["history"]
