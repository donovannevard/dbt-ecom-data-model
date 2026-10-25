"""Turn a validated patch into a branch, a commit and a pull request body.

Git work happens in a temporary detached worktree of the repository, and the
result is pushed as HEAD:refs/heads/<branch>. No local branch is created and
the checkout someone is working in is never touched.

Every remediation is identified by a failure id: a hash of the failing nodes'
unique ids. It goes in the commit trailers and, as a hidden marker, in the PR
body, so the same failure recurring finds its open PR instead of opening a
second one. State lives in GitHub, not on any machine.
"""

import base64
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from remediation.validate import Validation
from remediation.validate import render as render_validation

MAX_DIFF_CHARS = 30000


def failure_id(bundle: dict) -> str:
    ids = sorted(f["unique_id"] for f in bundle["failures"])
    return hashlib.sha256("\n".join(ids).encode()).hexdigest()[:12]


def branch_name(bundle: dict, fid: str, prefix: str) -> str:
    first = bundle["failures"][0].get("model") or bundle["failures"][0]["name"]
    slug = re.sub(r"[^a-z0-9]+", "-", str(first).lower()).strip("-")[:40]
    return f"{prefix}{slug}-{fid[:8]}"


def git(cwd: Path, *args: str, env: dict | None = None) -> str:
    result = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True,
                            env={**os.environ, **(env or {})})
    if result.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout.strip()


def commit_to_branch(repo: Path, base_ref: str, branch: str, files: dict[str, bytes | None],
                     message: str, author: tuple[str, str], remote: str | None,
                     lease: str | None = None) -> tuple[str, str]:
    """Commit `files` (path -> new bytes, or None to delete) on top of base_ref
    and push to remote as `branch`. lease: the sha the remote branch must
    still point at when replacing an existing branch. Returns (sha, diff)."""
    name, email = author
    identity = {"GIT_AUTHOR_NAME": name, "GIT_AUTHOR_EMAIL": email,
                "GIT_COMMITTER_NAME": name, "GIT_COMMITTER_EMAIL": email}
    tmp = Path(tempfile.mkdtemp(prefix="remediation-worktree-"))
    worktree = tmp / "wt"
    git(repo, "worktree", "add", "--detach", str(worktree), base_ref)
    try:
        for path, content in files.items():
            target = worktree / path
            if content is None:
                target.unlink(missing_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(content)
        git(worktree, "add", "-A", "--", *files)
        git(worktree, "commit", "-m", message, env=identity)
        sha = git(worktree, "rev-parse", "HEAD")
        diff = git(worktree, "diff", "HEAD~1", "HEAD")
        if remote:
            push = ["push", remote, f"HEAD:refs/heads/{branch}"]
            if lease:
                push.insert(1, f"--force-with-lease=refs/heads/{branch}:{lease}")
            git(worktree, *push)
        return sha, diff
    finally:
        git(repo, "worktree", "remove", "--force", str(worktree))
        shutil.rmtree(tmp, ignore_errors=True)


def commit_message(bundle: dict, record: dict, validation: Validation, fid: str, attempt: int,
                   co_author: tuple[str, str] | None = None) -> str:
    d = record["diagnosis"]
    models = sorted({f["model"] for f in bundle["failures"] if f.get("model")})
    # Built from the failure rather than cut from prose: a truncated first
    # sentence of the root cause makes a poor subject line.
    tests = sorted({(f.get("test") or {}).get("type") or f["kind"] for f in bundle["failures"]})
    n = len(bundle["failures"])
    subject = f"fix({', '.join(models[:2]) or 'dbt'}): repair {n} failing {'/'.join(tests)} check{'s' if n > 1 else ''}"
    if len(subject) > 72:
        subject = subject[:69].rstrip() + "..."
    trailers = [
        f"Remediation-Failure-Id: {fid}",
        f"Remediation-Attempt: {attempt}",
        f"Remediation-Tier: {validation.tier}",
        f"Remediation-Route: {validation.route}",
        f"Remediation-Confidence: {d['confidence']:.2f}",
        f"Remediation-Model: {record.get('served_by') or record.get('model')}",
    ]
    if co_author:
        # Squash merges re-attribute a commit to whoever merges it; a
        # co-author trailer is how the fix stays credited to its author.
        trailers.append(f"Co-Authored-By: {co_author[0]} <{co_author[1]}>")
    return f"{subject}\n\n{d['root_cause'].strip()}\n\n" + "\n".join(trailers) + "\n"


def pr_title(bundle: dict, record: dict) -> str:
    target = bundle["failures"][0].get("model") or bundle["failures"][0]["name"]
    extra = len(bundle["failures"]) - 1
    title = f"[ai-remediation] Fix {target}" + (f" (+{extra} more failures)" if extra else "")
    return title[:100]


def encode_history(history: list[dict]) -> str:
    return base64.urlsafe_b64encode(json.dumps(history).encode()).decode()


def decode_history(value: str | None) -> list[dict]:
    if not value:
        return []
    try:
        return json.loads(base64.urlsafe_b64decode(value.encode()))
    except (ValueError, json.JSONDecodeError):
        return []


def pr_body(bundle: dict, record: dict, validation: Validation, outcome_attempts: list[dict],
            diff: str, fid: str, state: dict, route_note: str, automerge_note: str = "") -> str:
    """state: attempt, ci_failures, cost_usd, history (one entry per CI
    failure). Kept in hidden markers, which is where Phase 5 reads it back:
    the PR is the loop's only memory."""
    d = record["diagnosis"]
    from remediation.github import marker

    if len(diff) > MAX_DIFF_CHARS:
        diff = diff[:MAX_DIFF_CHARS] + "\n... (diff truncated; see the Files tab)"
    route = validation.route
    history = state.get("history", [])
    lines = [
        marker("remediation-failure-id", fid), marker("remediation-attempt", str(state["attempt"])),
        marker("remediation-ci-failures", str(state.get("ci_failures", 0))),
        marker("remediation-cost", f"{state.get('cost_usd', 0):.4f}"),
        marker("remediation-claude-attempts", str(state.get("claude_attempts", 0))),
        marker("remediation-auto-merge", "yes" if state.get("auto_merge") else "no"),
        marker("remediation-history", encode_history(history)), "",
        f"> **Route: `{route}`.** {route_note}",
    ]
    if automerge_note:
        lines += [">", f"> {automerge_note}"]
    lines += [
        "", "## What failed", "",
        *[f"- `{f['name']}` ({f['status']}) on `{f.get('model', '?')}`" for f in bundle["failures"]], "",
        f"## Diagnosis (attempt {state['attempt']})", "",
        f"**Confidence {d['confidence']:.2f}**, self-assessed risk {d['self_assessed_risk']}.", "",
        d["root_cause"].strip(), "",
    ]
    if d["evidence"]:
        lines += ["Evidence:", "", *[f"- {e}" for e in d["evidence"]], ""]
    lines += ["## The change", "", "```diff", diff.rstrip(), "```", "",
              render_validation(validation, "Validation").replace("## Validation", "## Validation (Phase 3)"), ""]
    if history:
        lines += ["## CI history", ""]
        for h in history:
            lines += [f"- **Attempt {h['attempt']}** failed CI in: {', '.join(h.get('jobs') or ['?'])}"]
        lines += [f"", f"{state.get('ci_failures', 0)} of {state.get('max_ci_failures', 3)} CI failures used.", ""]
    if len(outcome_attempts) > 1:
        lines += ["### Validator attempts (this round)", "",
                  *[f"- Attempt {a['attempt']}: {a.get('validation') or a.get('error') or a.get('verdict')}"
                    for a in outcome_attempts], ""]
    lines += [
        "## Reviewing", "",
        "- CI runs on this PR like any other; required checks must pass before it can merge.",
        "- To check it locally: `gh pr checkout <number> && make build`.",
        "- To undo after merging: `git revert <merge commit>`.", "",
        f"<sub>Opened by dbt-ecom-remediation (`remediation/pipeline.py`). Diagnosis by "
        f"{record.get('served_by') or record.get('model')}; ~${state.get('cost_usd', 0):.3f} so far on this "
        "failure. Validated before this commit was pushed: no test was weakened, no protected path or risk "
        "tier was touched, and the project builds clean with the fault present.</sub>",
    ]
    return "\n".join(lines)
