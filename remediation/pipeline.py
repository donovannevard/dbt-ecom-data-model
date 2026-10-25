"""Phase 4: a failed build in, a pull request or an issue out.

  1. Build the diagnostic bundle. No failures: stop.
  2. Already an open PR or issue for this failure (same failure id)? Comment
     that it recurred and stop. No API call, no duplicate PR.
  3. Diagnose. A data issue or needs-human verdict becomes an issue carrying
     the diagnosis: there's nothing to patch in this repo.
  4. A code fix goes through the Phase 3 validator, with retries. Validated:
     commit to a new branch and open a PR, labelled with its route. Still
     rejected after the retries: an issue with every attempt and why it failed.

In this phase every PR is merged by a person. The route label (risk:auto /
risk:quarantine) is what Phase 6 will use to decide which may auto-merge;
CODEOWNERS and branch protection already enforce that quarantined changes
need a human.

    python -m remediation.pipeline                    # local: no push, local stand-in tracker
    python -m remediation.pipeline --github           # real: push the branch, open the PR (needs gh auth)
"""

import argparse
import json
import shutil
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

from remediation.config import REPO_ROOT, load_policy
from remediation.diagnose import build_bundle
from remediation.github import GitHubTracker, LocalTracker, marker
from remediation.gitops import branch_name, commit_message, commit_to_branch, failure_id, pr_body, pr_title
from remediation.remediate import remediate

LABEL = "ai-remediation"
ROUTE_NOTES = {
    "auto": ("Lineage reaches no revenue or customer model and the validator raised no escalation. "
             "In Phase 4 a person still merges every remediation PR; this route is what Phase 6 will "
             "auto-merge once auto-tier models have a clean record."),
    "quarantine": ("This change touches revenue or customer data (or was escalated): a code owner "
                   "must review and merge it. CODEOWNERS and branch protection enforce that."),
}


@dataclass
class Result:
    action: str  # nothing_failed | recurred | issue_opened | pr_opened
    number: int | None = None
    url: str | None = None
    detail: str = ""


def _issue(tracker, bundle: dict, fid: str, title: str, labels: list[str], sections: list[str]) -> Result:
    failures = "\n".join(f"- `{f['name']}` ({f['status']}) on `{f.get('model', '?')}`" for f in bundle["failures"])
    body = "\n".join([marker("remediation-failure-id", fid), "", "## What failed", "", failures, "", *sections,
                      "", "<sub>Opened by the AI remediation layer (`remediation/pipeline.py`). The same failure "
                      "recurring is noted here rather than re-diagnosed while this issue is open.</sub>"])
    item = tracker.create_issue(title, body, [LABEL, *labels])
    return Result("issue_opened", item["number"], item.get("url"), title)


def _diagnosis_sections(d: dict) -> list[str]:
    out = ["## Diagnosis", "", f"**Verdict: `{d['verdict']}`**, confidence {d['confidence']:.2f}.", "",
           d["root_cause"].strip(), ""]
    if d["evidence"]:
        out += ["Evidence:", "", *[f"- {e}" for e in d["evidence"]], ""]
    if d["upstream_action"]:
        out += ["## What should happen", "", d["upstream_action"].strip(), ""]
    return out


def auto_merge_decision(policy: dict, validation, project_dir: Path) -> tuple[bool, str]:
    """Phase 6: may this validated patch merge itself once CI is green?

    Autonomy is granted, never assumed. Every condition must hold:
      - auto_merge.enabled in policy.yaml, and the AI_REMEDIATION_AUTOMERGE
        repository variable is "true" (the kill switch, flippable without a
        commit);
      - the validator routed it `auto` (lineage tier auto, no escalation);
      - every model the change reaches by direct edit or macro is on the
        auto_merge.models allowlist.
    GitHub then enforces the rest: required checks must pass, and CODEOWNERS
    still demands a review for any owned file.
    """
    import os

    from remediation.config import load_manifest
    from remediation.risk import RiskModel

    am = policy["auto_merge"]
    if not am.get("enabled"):
        return False, "auto-merge is disabled in policy.yaml"
    if os.environ.get("AI_REMEDIATION_AUTOMERGE", "").lower() != "true":
        return False, "the AI_REMEDIATION_AUTOMERGE repository variable is not `true`"
    if validation.route != "auto":
        return False, f"routed `{validation.route}`" + (f" ({'; '.join(validation.escalations)})"
                                                       if validation.escalations else "")
    risk = RiskModel(load_manifest(project_dir / "target" / "manifest.json"), policy)
    touched = sorted({uid.split(".")[-1] for f in validation.changed_files for uid in risk.nodes_for_file(f)
                      if uid.startswith("model.")})
    outside = [m for m in touched if m not in am["models"]]
    if not touched or outside:
        return False, f"touches models not approved for auto-merge: {', '.join(outside or ['(none identified)'])}"
    return True, f"`{', '.join(touched)}` approved for auto-merge"


def is_claude(record: dict) -> bool:
    """Did a model write this attempt, or deterministic remediation?"""
    return (record.get("served_by") or record.get("model") or "").startswith("claude")


def _automerge_note(eligible: bool, reason: str) -> str:
    if eligible:
        return (f"**Auto-merge on:** {reason}. The remediation workflow merges this PR (rebase) as soon as CI "
                "passes on this exact commit. Label `ai-remediation:auto-merge`.")
    return f"Auto-merge off: {reason}. A person merges this PR."


def _apply_auto_merge(tracker, number: int, eligible: bool) -> None:
    """The label is the signal; the merge itself happens in ci.py when CI
    passes, after re-checking the kill switch and the head commit."""
    if eligible:
        tracker.add_labels(number, ["ai-remediation:auto-merge"])
    else:
        tracker.remove_labels(number, ["ai-remediation:auto-merge"])


def _changed_files(work_dir: Path, validation) -> dict[str, bytes | None]:
    patched = work_dir / "patched"
    return {f: ((patched / f).read_bytes() if (patched / f).exists() else None) for f in validation.changed_files}


def _publish(tracker, policy: dict, bundle: dict, outcome, project_dir: Path, work_dir: Path, *, repo: Path,
             base_ref: str, remote: str | None, fid: str, state: dict, branch: str, lease: str | None = None,
             pr: dict | None = None) -> tuple[dict, str]:
    """Commit the validated patch to `branch` and open (pr=None) or update the
    PR, with auto-merge decided afresh each time. Returns (pr, diff)."""
    v, final, gh = outcome.validation, outcome.record, policy["github"]
    # Claude is credited only for fixes Claude wrote; deterministic fixes are
    # the remediation layer's own.
    claude = is_claude(final)
    author = (gh["bot_name"], gh["bot_email"]) if claude else (gh["deterministic_name"], gh["deterministic_email"])
    _, diff = commit_to_branch(repo, base_ref, branch, _changed_files(work_dir, v),
                               commit_message(bundle, final, v, fid, state["attempt"], author if claude else None),
                               author, remote, lease=lease)
    eligible, why = auto_merge_decision(policy, v, project_dir)
    state = {**state, "max_ci_failures": policy["max_ci_failures"], "auto_merge": eligible}
    body = pr_body(bundle, final, v, outcome.attempts, diff, fid, state, ROUTE_NOTES[v.route],
                   _automerge_note(eligible, why))
    if pr is None:
        reviewers = [policy["codeowner"].lstrip("@")] if v.route == "quarantine" else []
        pr = tracker.create_pr(branch, gh["base_branch"], pr_title(bundle, final), body,
                               [LABEL, f"risk:{v.route}"], reviewers)
    else:
        tracker.update(pr["number"], pr_title(bundle, final), body)
        # The route can change between attempts (a deterministic attempt that
        # edited YAML was quarantine; Claude's that didn't, auto). The risk
        # label follows the current attempt, never the first.
        stale = [f"risk:{r}" for r in policy["tiers"] if r != v.route]
        tracker.remove_labels(pr["number"], stale)
        tracker.add_labels(pr["number"], [f"risk:{v.route}"])
    _apply_auto_merge(tracker, pr["number"], eligible)
    return pr, diff


def _pr_state(pr: dict) -> dict:
    from remediation.github import read_marker
    from remediation.gitops import decode_history

    return {"attempt": int(read_marker(pr["body"], "remediation-attempt") or 1),
            "ci_failures": int(read_marker(pr["body"], "remediation-ci-failures") or 0),
            "cost_usd": float(read_marker(pr["body"], "remediation-cost") or 0),
            "claude_attempts": int(read_marker(pr["body"], "remediation-claude-attempts") or 0),
            "history": decode_history(read_marker(pr["body"], "remediation-history"))}


def run(bundle: dict, project_dir: Path, policy: dict, tracker, *, repo: Path, base_ref: str,
        remote: str | None, ask, work_dir: Path, base_db: Path | None = None, first: dict | None = None,
        run_label: str = "this run", refresh: bool = False) -> Result:
    """project_dir: the built project the failure came from (its target/ holds
    the failed run). repo/base_ref: where the branch is cut from; base_ref must
    contain the same code project_dir was built from. remote: push target, or
    None to commit without pushing. first: an existing diagnosis to use
    instead of asking (it still goes through validation)."""
    if not bundle["failures"]:
        return Result("nothing_failed")
    fid = failure_id(bundle)
    gh = policy["github"]

    for kind in ("pr", "issue"):
        existing = tracker.find_open(kind, LABEL, "remediation-failure-id", fid)
        # Confirm against the item itself before acting on it: a listing can
        # be stale, and commenting on (or pushing to) a closed PR helps no one.
        if existing and not tracker.is_open(existing["number"]):
            existing = None
        if existing and kind == "pr" and refresh and first is not None:
            return _refresh_pr(existing, bundle, project_dir, policy, tracker, repo=repo, base_ref=base_ref,
                               remote=remote, ask=ask, work_dir=work_dir, base_db=base_db, first=first,
                               fid=fid, run_label=run_label)
        if existing:
            tracker.comment(existing["number"], f"The same failure recurred in {run_label}. Not re-diagnosed "
                                                f"while this {'PR' if kind == 'pr' else 'issue'} is open.")
            return Result("recurred", existing["number"], existing.get("url"), f"open {kind} for this failure")

    record = first or ask(bundle, project_dir, None)
    d = record.get("diagnosis")
    if record.get("error") or not d:
        return _issue(tracker, bundle, fid, f"[ai-remediation] Could not diagnose {bundle['failures'][0]['name']}",
                      ["needs-human"], ["## What happened", "", f"Diagnosis failed: {record.get('error')}"])
    if d["verdict"] != "code_fix" or not d["edits"]:
        label = "data-issue" if d["verdict"] == "data_issue" else "needs-human"
        what = "Data issue" if label == "data-issue" else "Needs a decision"
        return _issue(tracker, bundle, fid, f"[ai-remediation] {what}: {bundle['failures'][0].get('model') or bundle['failures'][0]['name']}",
                      [label], _diagnosis_sections(d))

    outcome = remediate(bundle, project_dir, work_dir / "patched", work_dir / "validate.log", policy,
                        base_db=base_db, first=record, ask=ask)
    if outcome.status != "validated":
        return _no_valid_fix_issue(tracker, bundle, fid, outcome, d)

    branch = branch_name(bundle, fid, gh["branch_prefix"])
    if remote:
        # A closed PR for the same failure leaves its branch behind; never
        # overwrite it (that rewrites the closed PR). Take the next free name.
        from remediation.gitops import git
        taken = {line.split("refs/heads/")[-1] for line in
                 git(repo, "ls-remote", remote, f"refs/heads/{branch}*").splitlines()}
        base_name, n = branch, 2
        while branch in taken:
            branch, n = f"{base_name}-{n}", n + 1
    state = {"attempt": 1, "ci_failures": 0, "cost_usd": outcome.cost_usd, "history": [],
             "claude_attempts": 1 if is_claude(outcome.record) else 0}
    pr, _ = _publish(tracker, policy, bundle, outcome, project_dir, work_dir, repo=repo, base_ref=base_ref,
                     remote=remote, fid=fid, state=state, branch=branch)
    return Result("pr_opened", pr["number"], pr.get("url"), f"{branch} ({outcome.validation.route})")


def _no_valid_fix_issue(tracker, bundle: dict, fid: str, outcome, d: dict) -> Result:
    attempts = [f"- Attempt {a['attempt']}: {a.get('validation') or a.get('error') or a.get('verdict')}"
                for a in outcome.attempts]
    final = outcome.record["diagnosis"] if outcome.record and outcome.record.get("diagnosis") else d
    return _issue(tracker, bundle, fid, f"[ai-remediation] No valid fix for {bundle['failures'][0].get('model')}",
                  ["needs-human"], _diagnosis_sections(final)
                  + ["## Why no PR", "", f"Every proposed fix was rejected by the validator ({outcome.status}):",
                     "", *attempts])


def _remote_sha(repo: Path, remote: str | None, branch: str) -> str | None:
    from remediation.gitops import git

    return git(repo, "ls-remote", remote, f"refs/heads/{branch}").split()[0] if remote else None


def _refresh_pr(pr: dict, bundle: dict, project_dir: Path, policy: dict, tracker, *, repo: Path,
                base_ref: str, remote: str | None, ask, work_dir: Path, base_db: Path | None, first: dict,
                fid: str, run_label: str) -> Result:
    """Rebuild an open remediation PR's branch on the current base: re-apply
    and re-validate the same fix, force-push with a lease on the sha the
    branch had, refresh the body. CI on the PR then re-runs on its own."""
    outcome = remediate(bundle, project_dir, work_dir / "patched", work_dir / "validate.log", policy,
                        base_db=base_db, first=first, ask=ask)
    if outcome.status != "validated":
        tracker.comment(pr["number"], f"Tried to refresh this PR in {run_label}, but the fix no longer "
                                      f"validates against the current base ({outcome.status}). Leaving it as is.")
        return Result("refresh_failed", pr["number"], pr.get("url"), outcome.status)
    branch = pr["head"]
    _publish(tracker, policy, bundle, outcome, project_dir, work_dir, repo=repo, base_ref=base_ref, remote=remote,
             fid=fid, state=_pr_state(pr), branch=branch, lease=_remote_sha(repo, remote, branch), pr=pr)
    tracker.comment(pr["number"], f"Refreshed in {run_label}: rebuilt on the current `{policy['github']['base_branch']}` "
                                  f"and re-validated (route `{outcome.validation.route}`). CI re-runs on the new commit.")
    return Result("pr_refreshed", pr["number"], pr.get("url"), f"{branch} ({outcome.validation.route})")


def block(tracker, pr: dict, policy: dict, reason: str, run_label: str) -> Result:
    """Stop the loop: no more attempts, auto-merge off, the code owner asked
    to take over. The PR stays open with its history for them."""
    tracker.remove_labels(pr["number"], ["ai-remediation:auto-merge"])
    tracker.add_labels(pr["number"], ["ai-remediation:blocked", "needs-human"])
    tracker.comment(pr["number"], f"**Stopped ({run_label}): {reason}**\n\nNo further automatic attempts will be "
                                  f"made on this PR. {policy['codeowner']}, over to you: the CI history and every "
                                  "attempt are in the description.")
    return Result("blocked", pr["number"], pr.get("url"), reason)


def retry_pr(pr: dict, bundle: dict, project_dir: Path, policy: dict, tracker, *, repo: Path, base_ref: str,
             remote: str | None, ask, work_dir: Path, ci_jobs: list[str], ci_log: str, run_label: str,
             base_db: Path | None = None) -> Result:
    """Phase 5: CI failed on a remediation PR. Count it, and either stop or
    ask Claude again with every earlier attempt and what CI said about it,
    validate the new attempt, and push it to the same branch."""
    from remediation.gitops import git

    if "ai-remediation:blocked" in pr.get("labels", []):
        return Result("already_blocked", pr["number"], pr.get("url"))
    if not bundle["failures"]:
        tracker.close(pr["number"], f"The base no longer fails in {run_label}, so this fix isn't needed. Closing.")
        return Result("closed_not_needed", pr["number"], pr.get("url"))
    fid = failure_id(bundle)
    state = _pr_state(pr)
    from remediation.github import read_marker
    if read_marker(pr["body"], "remediation-failure-id") != fid:
        return block(tracker, pr, policy, "the failure on the base branch has changed since this PR was opened",
                     run_label)

    previous_diff = git(repo, "diff", f"{base_ref}...{remote}/{pr['head']}") if remote else ""
    state["ci_failures"] += 1
    state["history"].append({
        "attempt": state["attempt"], "jobs": ci_jobs,
        "signature": _diff_signature(previous_diff),
        "diff": previous_diff[:4000], "log": ci_log[:3000],
    })
    if state["ci_failures"] >= policy["max_ci_failures"]:
        tracker.update(pr["number"], pr["title"], _with_state(pr["body"], state))
        return block(tracker, pr, policy, f"CI failed {state['ci_failures']} times "
                                          f"(the limit is {policy['max_ci_failures']})", run_label)
    limit = policy["ci"]["max_claude_attempts"]
    if state["claude_attempts"] >= limit:
        tracker.update(pr["number"], pr["title"], _with_state(pr["body"], state))
        return block(tracker, pr, policy, f"CI failed on an attempt Claude wrote, and Claude gets at most "
                                          f"{limit} attempt{'s' if limit != 1 else ''} per PR", run_label)
    ceiling = policy["ci"]["max_cost_usd_per_failure"]
    if state["cost_usd"] >= ceiling:
        tracker.update(pr["number"], pr["title"], _with_state(pr["body"], state))
        return block(tracker, pr, policy, f"spent ${state['cost_usd']:.2f} on this failure "
                                          f"(the ceiling is ${ceiling:.2f})", run_label)

    previous = [{"edits": h["diff"], "rejection": f"CI failed in {', '.join(h['jobs']) or 'unknown jobs'}:\n{h['log']}"}
                for h in state["history"]]
    tracker.comment(pr["number"], f"CI failed on attempt {state['attempt']} ({', '.join(ci_jobs) or 'unknown jobs'}). "
                                  f"Asking for attempt {state['attempt'] + 1} with the CI output "
                                  f"({state['ci_failures']} of {policy['max_ci_failures']} CI failures used).")
    record = ask(bundle, project_dir, previous)
    # One Claude call per CI failure: no extra validator round-trips here.
    outcome = remediate(bundle, project_dir, work_dir / "patched", work_dir / "validate.log", policy,
                        base_db=base_db, first=record, ask=ask, previous=previous, max_retries=0)
    state["cost_usd"] += outcome.cost_usd
    if outcome.status != "validated":
        tracker.update(pr["number"], pr["title"], _with_state(pr["body"], state))
        return block(tracker, pr, policy, f"no new attempt passed validation ({outcome.status})", run_label)

    new_sig = _diff_signature(_attempt_diff(project_dir, work_dir, outcome.validation))
    if new_sig in {h.get("signature") for h in state["history"]}:
        tracker.update(pr["number"], pr["title"], _with_state(pr["body"], state))
        return block(tracker, pr, policy, "the new attempt is identical to one that already failed CI "
                                          "(oscillation guard)", run_label)

    state["attempt"] += 1
    state["claude_attempts"] += 1 if is_claude(outcome.record) else 0
    branch = pr["head"]
    _publish(tracker, policy, bundle, outcome, project_dir, work_dir, repo=repo, base_ref=base_ref, remote=remote,
             fid=fid, state=state, branch=branch, lease=_remote_sha(repo, remote, branch), pr=pr)
    tracker.comment(pr["number"], f"Attempt {state['attempt']} pushed: "
                                  f"{outcome.record['diagnosis']['root_cause'].strip().split('. ')[0]}.")
    return Result("retried", pr["number"], pr.get("url"), f"attempt {state['attempt']}")


def _diff_signature(diff: str) -> str:
    """What an attempt changes, reduced to its added and removed lines, so two
    attempts compare equal however their diffs were produced."""
    import hashlib

    changed = [line for line in diff.splitlines()
               if line[:1] in "+-" and not line.startswith(("+++", "---"))]
    return hashlib.sha256("\n".join(changed).encode()).hexdigest()[:12]


def _attempt_diff(project_dir: Path, work_dir: Path, validation) -> str:
    import difflib

    out = []
    for f in validation.changed_files:
        before = (project_dir / f).read_text().splitlines() if (project_dir / f).exists() else []
        after_path = work_dir / "patched" / f
        after = after_path.read_text().splitlines() if after_path.exists() else []
        out += difflib.unified_diff(before, after, f"a/{f}", f"b/{f}", lineterm="")
    return "\n".join(out)


def _with_state(body: str, state: dict) -> str:
    """Rewrite the state markers in an existing body (when stopping, so the
    final CI failure is recorded)."""
    import re as _re

    from remediation.gitops import encode_history

    out = body
    for key, value in [("remediation-ci-failures", str(state["ci_failures"])),
                       ("remediation-cost", f"{state['cost_usd']:.4f}"),
                       ("remediation-history", encode_history(state["history"]))]:
        out = _re.sub(rf"<!-- {key}: .*? -->", f"<!-- {key}: {value} -->", out)
    return out


def main(argv: list[str] | None = None) -> int:
    from remediation.agent import ResponseCache, diagnose

    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--project-dir", type=Path, default=REPO_ROOT)
    parser.add_argument("--github", action="store_true",
                        help="push the branch to origin and use real GitHub issues/PRs")
    parser.add_argument("--tracker-dir", type=Path, help="local stand-in directory")
    parser.add_argument("--run-label", default="this run")
    args = parser.parse_args(argv)

    policy = load_policy()
    project = args.project_dir.resolve()
    bundle = build_bundle(project, project / "target", project / "dev.duckdb")
    cache = ResponseCache.from_policy(policy)
    ask = lambda b, p, previous: diagnose(b, p, policy=policy, cache=cache, previous=previous)  # noqa: E731
    if args.github:
        from remediation.ghapp import acting_as_app, repo_slug
        from remediation.gitops import git

        tracker, remote = GitHubTracker(project), "origin"
        base_ref = f"origin/{policy['github']['base_branch']}"
        git(project, "fetch", "origin", policy["github"]["base_branch"])
        app = acting_as_app(repo_slug(project))
    else:
        from contextlib import nullcontext

        # Local mode never pushes: the commit is made (so the diff is real) and
        # the PR lands in the stand-in tracker.
        tracker, remote, base_ref = LocalTracker(args.tracker_dir or LocalTracker().directory), None, "HEAD"
        app = nullcontext()
    work = Path(tempfile.mkdtemp(prefix="remediation-pipeline-"))
    try:
        with app:  # as dbt-ecom-remediation[bot] when the App is configured
            result = run(bundle, project, policy, tracker, repo=project, base_ref=base_ref, remote=remote,
                         ask=ask, work_dir=work, run_label=args.run_label)
    finally:
        shutil.rmtree(work, ignore_errors=True)
    print(json.dumps(result.__dict__, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
