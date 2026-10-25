"""What GitHub Actions runs when the `dbt build` workflow fails.

Called by .github/workflows/remediation.yml on every completed `dbt build`
run, after it has checked out and built the base branch (which reproduces
the failure being remediated):

  failed on the base branch (main)     open a remediation (pipeline.run):
                                       a PR, or an issue; an open item for the
                                       same failure just gets a comment
  failed on an ai-remediation/* PR     Phase 5 (pipeline.retry_pr): stop if
                                       Claude wrote the failing attempt;
                                       otherwise give Claude one attempt with
                                       the CI output, pushed to the same branch
  passed on an ai-remediation/* PR     Phase 6 (merge_if_eligible): merge it,
                                       if it was approved for auto-merge
  anything else                        nothing

Every write to GitHub is made as the dbt-ecom-remediation App. That is
required here, not optional: pushes made with Actions' own GITHUB_TOKEN don't
trigger workflows, so a new attempt would never get CI and the loop would
stall silently.

    python -m remediation.ci --event "$GITHUB_EVENT_PATH"
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from remediation.config import REPO_ROOT, load_policy
from remediation.diagnose import REDACTED, build_bundle

EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
# dbt and Actions log prefixes that add nothing for a diagnosis.
NOISE = re.compile(r"^\S+\t\S+\t\d{4}-\d\d-\d\dT[\d:.]+Z ")


def failed_jobs(run_id: int, repo_dir: Path) -> list[str]:
    out = subprocess.run(["gh", "run", "view", str(run_id), "--json", "jobs",
                          "--jq", '.jobs[] | select(.conclusion == "failure") | .name'],
                         cwd=repo_dir, capture_output=True, text=True, check=True).stdout
    return [line for line in out.splitlines() if line]


def failed_log(run_id: int, repo_dir: Path, chars: int) -> str:
    """The tail of the failed steps' logs, de-noised and with emails scrubbed.
    The tail is where the error is."""
    out = subprocess.run(["gh", "run", "view", str(run_id), "--log-failed"],
                         cwd=repo_dir, capture_output=True, text=True).stdout
    lines = [NOISE.sub("", line) for line in out.splitlines()]
    text = EMAIL.sub(REDACTED, "\n".join(line for line in lines if line.strip()))
    return text[-chars:]


def merge_if_eligible(pr: dict, head_sha: str, policy: dict, tracker) -> dict:
    """Phase 6: CI passed on a remediation PR. Merge it only if all of:
      - it carries the auto-merge label AND the bot's own marker saying the
        pipeline approved it (a label added by hand is not enough);
      - it isn't blocked;
      - the AI_REMEDIATION_AUTOMERGE kill switch is on right now;
      - its head is still the exact commit CI passed on."""
    from remediation.github import read_marker

    labels = pr.get("labels", [])
    if "ai-remediation:auto-merge" not in labels or "ai-remediation:blocked" in labels:
        return {"action": "not_auto_merge", "number": pr["number"]}
    if read_marker(pr["body"], "remediation-auto-merge") != "yes":
        return {"action": "not_approved", "number": pr["number"],
                "detail": "auto-merge label present but the pipeline never approved this PR"}
    if os.environ.get("AI_REMEDIATION_AUTOMERGE", "").lower() != "true":
        tracker.comment(pr["number"], "CI passed, but auto-merge is switched off (AI_REMEDIATION_AUTOMERGE). "
                                      "Leaving this for a person to merge.")
        return {"action": "kill_switch_off", "number": pr["number"]}
    problem = tracker.merge(pr["number"], policy["auto_merge"]["method"], head_sha)
    if problem:
        tracker.comment(pr["number"], f"CI passed on `{head_sha[:7]}` but the merge was refused: {problem}")
        return {"action": "merge_refused", "number": pr["number"], "detail": problem}
    tracker.comment(pr["number"], f"Merged automatically: CI passed on `{head_sha[:7]}`, the commit merged.")
    return {"action": "merged", "number": pr["number"]}


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True, check=True).stdout.strip()


def main(argv: list[str] | None = None) -> int:
    # Only what the light merge job has installed (pyyaml): the agent and
    # DuckDB are imported below, on the failure path, after the merge path.
    from remediation.ghapp import acting_as_app, repo_slug
    from remediation.github import GitHubTracker

    LABEL = "ai-remediation"

    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--event", type=Path, required=True, help="the workflow_run event payload")
    parser.add_argument("--project-dir", type=Path, default=REPO_ROOT)
    args = parser.parse_args(argv)

    run_info = json.loads(args.event.read_text())["workflow_run"]
    branch, conclusion, run_id = run_info["head_branch"], run_info["conclusion"], run_info["id"]
    policy = load_policy()
    base, prefix = policy["github"]["base_branch"], policy["github"]["branch_prefix"]
    project = args.project_dir.resolve()

    if conclusion == "success" and branch.startswith(prefix):
        with acting_as_app(repo_slug(project), required=True):
            tracker = GitHubTracker(project)
            pr = next((p for p in tracker.list_open("pr", LABEL) if p.get("head") == branch), None)
            outcome = merge_if_eligible(pr, run_info["head_sha"], policy, tracker) if pr else \
                {"action": "no_open_pr", "branch": branch}
        print(json.dumps(outcome, indent=2))
        return 0
    if conclusion != "failure" or not (branch == base or branch.startswith(prefix)):
        print(f"nothing to do: {branch} {conclusion}")
        return 0

    from remediation.agent import diagnose
    from remediation.explore import freeze
    from remediation.pipeline import retry_pr, run

    # Pushes go through gh's credential helper, so they carry the App token
    # set by acting_as_app (actions/checkout must not persist its own token).
    _git(project, "config", "credential.https://github.com.helper", "")
    _git(project, "config", "--add", "credential.https://github.com.helper", "!gh auth git-credential")
    bundle = build_bundle(project, project / "target", project / "dev.duckdb")
    work = Path(tempfile.mkdtemp(prefix="remediation-ci-"))
    base_db = None
    if (project / "dev.duckdb").exists():
        base_db = work / "base.duckdb"
        freeze(project / "dev.duckdb", base_db)
    ask = lambda b, p, previous: diagnose(b, p, policy=policy, previous=previous)  # noqa: E731
    label = f"CI run {run_id} on `{branch}`"

    try:
        with acting_as_app(repo_slug(project), required=True):
            tracker = GitHubTracker(project)
            _git(project, "fetch", "origin", base)
            if branch == base:
                result = run(bundle, project, policy, tracker, repo=project, base_ref=f"origin/{base}",
                             remote="origin", ask=ask, work_dir=work, base_db=base_db, run_label=label)
            else:
                pr = next((p for p in tracker.list_open("pr", LABEL) if p.get("head") == branch), None)
                if pr is None:
                    print(f"no open remediation PR for {branch}")
                    return 0
                _git(project, "fetch", "origin", branch)
                result = retry_pr(pr, bundle, project, policy, tracker, repo=project, base_ref=f"origin/{base}",
                                  remote="origin", ask=ask, work_dir=work, run_label=label, base_db=base_db,
                                  ci_jobs=failed_jobs(run_id, project),
                                  ci_log=failed_log(run_id, project, policy["ci"]["log_chars"]))
    finally:
        shutil.rmtree(work, ignore_errors=True)

    summary = json.dumps(result.__dict__, indent=2)
    print(summary)
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as fh:
            fh.write(f"## Remediation\n\n`{result.action}` #{result.number or '-'} {result.detail}\n\n"
                     f"```json\n{summary}\n```\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
