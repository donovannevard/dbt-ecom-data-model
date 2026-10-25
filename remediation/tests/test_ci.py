import json
import subprocess
import sys

import pytest

from remediation import ci
from remediation.github import LocalTracker, marker


def _pr(tracker, auto=True, label=True):
    labels = ["ai-remediation"] + (["ai-remediation:auto-merge"] if label else [])
    body = marker("remediation-auto-merge", "yes" if auto else "no")
    return tracker.create_pr("ai-remediation/x", "main", "t", body, labels, [])


@pytest.fixture
def tracker(tmp_path, monkeypatch):
    monkeypatch.setenv("AI_REMEDIATION_AUTOMERGE", "true")
    return LocalTracker(tmp_path / "t")


def test_merges_approved_pr_at_the_tested_commit(tracker, policy):
    pr = _pr(tracker)
    assert ci.merge_if_eligible(pr, "abc1234def", policy, tracker)["action"] == "merged"
    item = tracker._item(pr["number"])[1]
    assert item["state"] == "merged" and item["merged_sha"] == "abc1234def" and item["merged_with"] == "rebase"


def test_a_hand_added_label_is_not_enough(tracker, policy):
    pr = _pr(tracker, auto=False)
    assert ci.merge_if_eligible(pr, "abc", policy, tracker)["action"] == "not_approved"


def test_unlabelled_or_blocked_prs_are_left_alone(tracker, policy):
    assert ci.merge_if_eligible(_pr(tracker, label=False), "abc", policy, tracker)["action"] == "not_auto_merge"
    blocked = _pr(tracker)
    blocked["labels"].append("ai-remediation:blocked")
    assert ci.merge_if_eligible(blocked, "abc", policy, tracker)["action"] == "not_auto_merge"


def test_kill_switch_is_checked_at_merge_time(tracker, policy, monkeypatch):
    monkeypatch.setenv("AI_REMEDIATION_AUTOMERGE", "false")
    pr = _pr(tracker)
    assert ci.merge_if_eligible(pr, "abc", policy, tracker)["action"] == "kill_switch_off"
    assert tracker._item(pr["number"])[1]["state"] == "open"


def test_merge_path_needs_only_pyyaml():
    # The light merge job installs pyyaml alone: importing ci and running its
    # merge path must not pull in anthropic or duckdb.
    code = ("import sys; import remediation.ci, remediation.ghapp, remediation.github; "
            "print(sorted(m for m in ('anthropic', 'duckdb', 'pydantic') if m in sys.modules))")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True).stdout
    assert out.strip() == "[]"
