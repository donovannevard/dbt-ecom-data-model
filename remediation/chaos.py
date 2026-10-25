"""Fault-injection scenarios, run step by step or end to end.

Each scenario gets a persistent workspace you can inspect:

    .chaos/
      _baseline/          the project built with no fault, rebuilt only when
                          the code changes (fingerprinted)
      <scenario>/
        broken/           project copy with the fault injected
        clean.duckdb      snapshot of the clean build      } compare with
        broken.duckdb     snapshot of the broken build     } remediation.explore
        bundle.json       what the model is sent (PII redacted)
        diagnosis.json    Claude's answer               (after `diagnose`)
        fixed/            broken/ + Claude's edits       (after `fix`)
        fixed.duckdb      snapshot of the fixed build    (after `fix`/`rebuild`)
        fix.json/.md      did the fix work?

Your checkout is never modified: every project copy is the repo's tracked and
unignored files as they are on disk, so uncommitted work is included.

Step by step (only `diagnose` calls the API):

    python -m remediation.chaos run refund_sign_flip
    python -m remediation.chaos diagnose refund_sign_flip
    python -m remediation.chaos fix refund_sign_flip
    python -m remediation.chaos rebuild refund_sign_flip   # after editing fixed/ yourself

End to end, all scenarios, with scored diagnoses:

    python -m remediation.chaos run all --diagnose

`run` also checks each scenario still does what it claims: the expected tests
fail and the fix carries the expected risk tier. A scenario that no longer
bites has rotted and fails the run. A wrong diagnosis is a metric, not a
failure.
"""

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from remediation.config import REPO_ROOT, load_manifest, load_policy
from remediation.dbtrun import PASSING, build
from remediation.diagnose import build_bundle, write_bundle
from remediation.edits import check_edits
from remediation.explore import CHAOS_DIR, compare, freeze
from remediation.inject import SCRATCH_MARKER, Scenario, apply, load_all
from remediation.risk import RiskModel

BASELINE = "_baseline"


@dataclass
class Outcome:
    scenario: str
    ok: bool
    seconds: float
    failing_tests: list[str] = field(default_factory=list)
    warning_tests: list[str] = field(default_factory=list)
    missing_expected: list[dict] = field(default_factory=list)
    unexpected: list[str] = field(default_factory=list)
    expected_tier: str = ""
    actual_tier: str = ""
    error: str | None = None
    # Set only with --diagnose; see score_diagnosis.
    diagnosis: dict | None = None


# -- project copies and builds ------------------------------------------------

def _source_files() -> list[str]:
    listed = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
        capture_output=True, text=True, check=True,
    ).stdout.split("\0")
    return sorted(rel for rel in listed if rel and (REPO_ROOT / rel).is_file())


# What a dbt build reads. The baseline is rebuilt only when one of these
# changes: a fix to the remediation tooling shouldn't cost a full rebuild on a
# cloud warehouse.
DBT_INPUTS = ("models/", "macros/", "seeds/", "snapshots/", "tests/", "analysis/", "docs/",
              "dbt_project.yml", "packages.yml", "package-lock.yml", "profiles.yml")


def fingerprint() -> str:
    digest = hashlib.sha256()
    for rel in (r for r in _source_files() if r.startswith(DBT_INPUTS)):
        digest.update(rel.encode() + b"\0" + (REPO_ROOT / rel).read_bytes() + b"\0")
    return digest.hexdigest()


def make_scratch(dest: Path) -> Path:
    """Copy tracked + unignored files (and installed dbt packages) to dest."""
    for rel in _source_files():
        target = dest / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(REPO_ROOT / rel, target)
    if (REPO_ROOT / "dbt_packages").is_dir():
        shutil.copytree(REPO_ROOT / "dbt_packages", dest / "dbt_packages")
    (dest / SCRATCH_MARKER).write_text("fault-injection scratch copy; safe to delete\n")
    return dest


def _link_or_copy(src: Path, dest: Path) -> None:
    dest.unlink(missing_ok=True)
    try:
        os.link(src, dest)  # free, and survives the baseline being rebuilt
    except OSError:
        shutil.copy2(src, dest)


def ensure_baseline(root: Path) -> Path | None:
    """Build the project clean (no fault) and require every node to pass,
    rebuilding only when the source has changed. On DuckDB, returns the frozen
    clean.duckdb snapshot; on a cloud warehouse, None (the build itself is the
    check that the project runs clean there)."""
    from remediation.warehouse import CURRENT

    base = root / BASELINE
    clean = base / "clean.duckdb"
    current = fingerprint()
    stamp = base / "fingerprint"
    if stamp.exists() and stamp.read_text() == current and (clean.exists() or not CURRENT.is_duckdb):
        return clean if CURRENT.is_duckdb else None
    print("-> building clean baseline", flush=True)
    shutil.rmtree(base, ignore_errors=True)
    project = make_scratch(base / "project")
    with open(base / "dbt.log", "w") as log:
        build(project, log)
    results = json.loads((project / "target" / "run_results.json").read_text())["results"]
    bad = [r["unique_id"] for r in results if r["status"] not in PASSING]
    if bad:
        raise RuntimeError(f"the project does not build clean on {CURRENT.label} ({len(bad)} failures, "
                           f"see {base / 'dbt.log'}); fix that before injecting faults")
    if CURRENT.is_duckdb:
        freeze(project / "dev.duckdb", clean)
    shutil.rmtree(project)
    stamp.write_text(current)
    return clean if CURRENT.is_duckdb else None


# -- run ----------------------------------------------------------------------

def matches(node: dict, expected: dict) -> bool:
    meta = node.get("test_metadata") or {}
    # Source tests have no attached_node; the source they test is their one dependency.
    target = node.get("attached_node") or next(iter(node.get("depends_on", {}).get("nodes", [])), "")
    attached = target.split(".")[-1]
    return (
        attached == expected["model"]
        and meta.get("name") == expected["test"]
        and expected.get("column") in (None, node.get("column_name"))
    )


def score_diagnosis(scenario: Scenario, record: dict, broken: Path) -> dict:
    """Score one diagnosis against the scenario's expectations.

    passed = verdict acceptable AND, if the verdict is code_fix: edits exist,
    apply cleanly to the broken tree, and (for code scenarios) touch every
    file the fault lives in. Whether the fix works is `fix`'s job.
    """
    # cost_usd is what this run spent: zero when replayed from the cache.
    score = {"passed": False, "error": record.get("error"), "cost_usd": record.get("cost_usd"),
             "cached": bool((record.get("cache") or {}).get("hit"))}
    d = record.get("diagnosis")
    if not d:
        return score
    touched = sorted({e["path"] for e in d["edits"]})
    problems = check_edits(broken, d["edits"]) if d["edits"] else []
    score.update({
        "verdict": d["verdict"],
        "confidence": d["confidence"],
        "verdict_ok": d["verdict"] in scenario.expect["verdicts"],
        "edited_files": touched,
        "edits_apply": bool(d["edits"]) and not problems,
        "edit_problems": problems,
    })
    if scenario.kind == "code":
        score["files_ok"] = set(scenario.expect["fix_files"]) <= set(touched)
    if d["verdict"] == "code_fix":
        score["passed"] = score["verdict_ok"] and score["edits_apply"] and score.get("files_ok", True)
    else:
        score["passed"] = score["verdict_ok"]
    return score


def run_scenario(scenario: Scenario, root: Path, policy: dict, clean: Path | None,
                 ask=None) -> Outcome:
    """ask: callable(bundle, project_dir, previous) -> agent record, or None
    to skip diagnosis. With it, a code fix is also validated (Phase 3)."""
    started = time.monotonic()
    outcome = Outcome(scenario.id, ok=False, seconds=0, expected_tier=scenario.expect.get("tier", ""))
    ws = root / scenario.id
    shutil.rmtree(ws, ignore_errors=True)
    ws.mkdir(parents=True)
    broken = make_scratch(ws / "broken")
    apply(scenario, broken)
    with open(ws / "dbt.log", "w") as log:
        try:
            build(broken, log)
        except RuntimeError as exc:
            outcome.error = str(exc)
            return outcome

    from remediation.warehouse import reader_for

    target = broken / "target"
    # Data is read back from wherever the build ran: the DuckDB file, or the
    # cloud warehouse through `dbt show` in the broken project.
    bundle = build_bundle(REPO_ROOT, target, broken / "dev.duckdb", scenario.id,
                          reader=reader_for(broken, broken / "dev.duckdb"))
    write_bundle(bundle, ws)
    if (broken / "dev.duckdb").exists():
        freeze(broken / "dev.duckdb", ws / "broken.duckdb")
        (broken / "dev.duckdb").unlink()
    if clean is not None:
        _link_or_copy(clean, ws / "clean.duckdb")

    manifest = load_manifest(target / "manifest.json")
    failing = [manifest["nodes"].get(f["unique_id"], {}) for f in bundle["failures"]]
    warned = [manifest["nodes"].get(w["unique_id"], {}) for w in bundle["warnings"]]
    outcome.failing_tests = sorted(n.get("name", "?") for n in failing)
    outcome.warning_tests = sorted(n.get("name", "?") for n in warned)
    expected_fail = scenario.expect.get("failing_tests", [])
    expected_warn = scenario.expect.get("warning_tests", [])
    outcome.missing_expected = (
        [e for e in expected_fail if not any(matches(n, e) for n in failing)]
        + [e for e in expected_warn if not any(matches(n, e) for n in warned)]
    )
    # Expected warnings are exact: identical copies must not trip the conflict
    # check, for instance. An absorbed scenario must also fail nothing, and
    # may only change the tables it names.
    outcome.unexpected = [f"warned: {n['name']}" for n in warned if not any(matches(n, e) for e in expected_warn)]
    if scenario.absorbed:
        outcome.unexpected += [f"failed: {n['name']}" for n in failing]
        allowed = scenario.expect.get("only_differs_from_clean")
        # Comparing tables needs the frozen DuckDB snapshots; on a cloud
        # warehouse that one assertion is skipped (the warnings still count).
        if allowed is not None and clean is not None:
            changed = {r["table"] for r in compare(clean, ws / "broken.duckdb") if r["status"] != "same"}
            outcome.unexpected += [f"also changed: {t}" for t in sorted(changed - set(allowed))]
    else:
        risk = RiskModel(manifest, policy)
        if scenario.kind == "code":
            outcome.actual_tier = risk.assess_files(scenario.expect["fix_files"]).tier
        else:
            model = expected_fail[0]["model"]
            uid = next(u for u, n in manifest["nodes"].items() if n["name"] == model and n["resource_type"] == "model")
            outcome.actual_tier = risk.assess_node(uid).tier
    outcome.ok = (not outcome.missing_expected and not outcome.unexpected
                  and outcome.actual_tier == outcome.expected_tier)
    # What code this workspace was built from, so a later step (chaos-pr
    # pushing to a GitHub sandbox) can tell when it has gone stale.
    (ws / "fingerprint").write_text(fingerprint())

    # Only diagnose a scenario that bit: a bundle for the wrong failure would
    # score the model against expectations that no longer apply.
    if ask and outcome.ok and bundle["failures"]:
        from remediation.agent import write_record
        record = ask(bundle, broken, None)
        write_record(record, ws)
        outcome.diagnosis = score_diagnosis(scenario, record, broken)
        # A diagnosis can score well and still not work: "PASS" means right
        # verdict and edits that apply; "validated" means the patched project
        # survives Phase 3. Both are reported.
        d = record.get("diagnosis") or {}
        if d.get("verdict") == "code_fix" and d.get("edits"):
            fixed, _ = fix_workspace(scenario, ws, policy, record, ask)
            outcome.diagnosis["fix"] = fixed.status
            outcome.diagnosis["route"] = fixed.validation.route if fixed.validation else None
            outcome.diagnosis["attempts"] = len(fixed.attempts)
            outcome.diagnosis["cost_usd"] = round(fixed.cost_usd, 4)
        (ws / "score.json").write_text(json.dumps(outcome.diagnosis, indent=2) + "\n")

    outcome.seconds = round(time.monotonic() - started, 1)
    return outcome


# -- diagnose / fix / rebuild -------------------------------------------------

def _workspace(scenario: Scenario, root: Path) -> Path:
    ws = root / scenario.id
    if not (ws / "bundle.json").exists() or not (ws / "broken").is_dir():
        raise SystemExit(f"no workspace for {scenario.id!r}: run `make chaos SCENARIO={scenario.id}` first")
    return ws


def _expectation(scenario: Scenario) -> str:
    if scenario.absorbed:
        return f"Scenario expects: absorbed (warnings only)\n  {' '.join(scenario.description.split())}"
    lines = [f"Scenario expects: verdict {' or '.join(scenario.expect['verdicts'])}, tier {scenario.expect['tier']}"]
    if scenario.expect.get("fix_files"):
        lines.append(f"  fault is in: {', '.join(scenario.expect['fix_files'])}")
    lines.append(f"  {' '.join(scenario.description.split())}")
    return "\n".join(lines)


def diagnose_workspace(scenario: Scenario, root: Path, policy: dict, fresh: bool = False) -> int:
    from remediation.agent import ResponseCache, diagnose, render, write_record

    ws = _workspace(scenario, root)
    bundle = json.loads((ws / "bundle.json").read_text())
    if not bundle["failures"]:
        print(f"Nothing failed in {scenario.id}: the project absorbed the fault "
              f"({len(bundle['warnings'])} warning(s)), so there is nothing to diagnose.")
        return 0
    print(f"-> asking Claude about {scenario.id}"
          + (" (fresh call, ignoring the local cache)" if fresh else " (replayed if already answered in the last 24h)"),
          flush=True)
    record = diagnose(bundle, ws / "broken", policy=policy,
                      cache=ResponseCache.from_policy(policy), fresh=fresh)
    write_record(record, ws)
    score = score_diagnosis(scenario, record, ws / "broken")
    (ws / "score.json").write_text(json.dumps(score, indent=2) + "\n")

    print("\n" + render(record))
    print(_expectation(scenario))
    if not record["error"]:
        checks = [f"verdict {'ok' if score['verdict_ok'] else 'WRONG'}"]
        if score["verdict"] == "code_fix":
            checks.append(f"edits {'apply' if score['edits_apply'] else 'DO NOT APPLY'}")
            if "files_ok" in score:
                checks.append(f"files {'right' if score['files_ok'] else 'WRONG'}")
        print(f"Score: {'PASS' if score['passed'] else 'MISS'} ({', '.join(checks)})")
        if score["verdict"] == "code_fix" and score["edits_apply"]:
            from remediation.warehouse import CURRENT
            t = "" if CURRENT.is_duckdb else f" TARGET={CURRENT.target}"
            print(f"\nNext: make chaos-fix SCENARIO={scenario.id}{t}   (validates the fix: Phase 3)")
    return 1 if record["error"] else 0


def make_asker(policy: dict, fresh: bool = False, client=None):
    """agent.diagnose with the local response cache, announcing each paid call."""
    from remediation.agent import ResponseCache, diagnose

    cache = ResponseCache.from_policy(policy)

    def ask(bundle, project, previous):
        if previous:
            print(f"   validator rejected attempt {len(previous)}; asking Claude again with the reason", flush=True)
        return diagnose(bundle, project, client=client, policy=policy, cache=cache, fresh=fresh, previous=previous)
    return ask


def fix_workspace(scenario: Scenario, ws: Path, policy: dict, first: dict, ask):
    """Phase 3 on a workspace: apply the diagnosis's edits to a copy, validate
    it, and retry with feedback on rejection. Returns (outcome, report)."""
    from remediation.agent import write_record
    from remediation.remediate import remediate, save

    bundle = json.loads((ws / "bundle.json").read_text())
    outcome = remediate(bundle, ws / "broken", ws / "fixed", ws / "fix.log", policy,
                        base_db=ws / "broken.duckdb", first=first, ask=ask)
    save(outcome, ws)
    if outcome.record is not first and outcome.record is not None:
        write_record(outcome.record, ws / "retry")  # the attempt that ended the loop
    report = {"status": outcome.status, "attempts": outcome.attempts, "tables_differing_from_clean": None}
    if (ws / "fixed" / "dev.duckdb").exists():
        freeze(ws / "fixed" / "dev.duckdb", ws / "fixed.duckdb")
        (ws / "fixed" / "dev.duckdb").unlink()
        diffs = [r for r in compare(ws / "clean.duckdb", ws / "fixed.duckdb") if r["status"] != "same"]
        report["tables_differing_from_clean"] = [r["table"] for r in diffs]
    md = render_fix(scenario, outcome, report)
    (ws / "fix.json").write_text(json.dumps({**report, **outcome.to_dict()}, indent=2) + "\n")
    (ws / "fix.md").write_text(md)
    return outcome, report


def render_fix(scenario: Scenario, outcome, report: dict) -> str:
    from remediation.validate import render

    lines = [f"# Fix: {scenario.id}", ""]
    if len(outcome.attempts) > 1:
        lines += ["Attempts:"] + [
            f"- {a['attempt']}: {a.get('validation') or a.get('error') or a.get('verdict')} (${a['cost_usd']:.3f})"
            for a in outcome.attempts] + [""]
    if outcome.validation:
        lines.append(render(outcome.validation, "Validation"))
    else:
        lines.append(f"Status: {outcome.status}\n")
    diffs = report.get("tables_differing_from_clean")
    if diffs is not None:
        lines.append(f"Tables still differing from the clean build: {len(diffs)}")
        lines += [f"- `{t}`" for t in diffs]
        if diffs and scenario.kind == "data":
            lines.append("  (expected for a data scenario: the injected source rows are still there)")
        elif not diffs:
            lines.append("  (the fixed build's data matches the clean build exactly)")
        lines += ["", f"Compare yourself: make chaos-changes SCENARIO={scenario.id} AGAINST=fixed"]
    return "\n".join(lines) + "\n"


def fix_command(scenario: Scenario, root: Path, policy: dict, fresh: bool = False) -> int:
    ws = _workspace(scenario, root)
    path = ws / "diagnosis.json"
    if not path.exists():
        raise SystemExit(f"no diagnosis yet: run `make chaos-diagnose SCENARIO={scenario.id}` first")
    first = json.loads(path.read_text())
    d = first.get("diagnosis")
    if not d:
        raise SystemExit("the diagnosis errored; nothing to apply")
    if d["verdict"] != "code_fix" or not d["edits"]:
        print(f"Verdict is {d['verdict']}: no edits to apply. For a data issue the fix belongs upstream.")
        return 0
    print(f"-> validating Claude's fix for {scenario.id} "
          f"(up to {policy['validator']['max_retries']} paid retries if rejected)", flush=True)
    outcome, _ = fix_workspace(scenario, ws, policy, first, make_asker(policy, fresh))
    print("\n" + (ws / "fix.md").read_text())
    return 0 if outcome.status == "validated" else 1


def rebuild_command(scenario: Scenario, root: Path, policy: dict) -> int:
    from remediation.validate import Validator, render

    ws = _workspace(scenario, root)
    fixed = ws / "fixed"
    if not fixed.is_dir():
        shutil.copytree(ws / "broken", fixed, ignore=shutil.ignore_patterns("target", "logs", "*.duckdb*"))
        print(f"Created {fixed.relative_to(REPO_ROOT) if REPO_ROOT in fixed.parents else fixed} from broken/.\n"
              f"Make your fix there, then run `make chaos-rebuild SCENARIO={scenario.id}` again.")
        return 0
    print(f"-> validating your fix in {fixed.name}/", flush=True)
    v = Validator(ws / "broken", policy, ws / "broken.duckdb").validate(fixed, ws / "fix.log")
    md = render(v, f"Validation: {scenario.id} (your fix)")
    if (fixed / "dev.duckdb").exists():
        freeze(fixed / "dev.duckdb", ws / "fixed.duckdb")
        (fixed / "dev.duckdb").unlink()
        diffs = [r["table"] for r in compare(ws / "clean.duckdb", ws / "fixed.duckdb") if r["status"] != "same"]
        md += f"\nTables still differing from the clean build: {len(diffs)}\n" + "".join(f"- `{t}`\n" for t in diffs)
    (ws / "validation.md").write_text(md)
    print("\n" + md)
    return 0 if v.passed else 1


def saved_diagnosis(ws: Path) -> dict | None:
    """The workspace's diagnosis, or else the most recent paid answer for the
    same failure from the local response cache, even if the prompt format has
    changed since. A demo convenience: never pay twice for the same failure."""
    from remediation.agent import CACHE_DIR, record_from_snapshot

    if (ws / "diagnosis.json").exists():
        return json.loads((ws / "diagnosis.json").read_text())
    wanted = {f["unique_id"] for f in json.loads((ws / "bundle.json").read_text())["failures"]}
    best = None
    for path in CACHE_DIR.glob("*.json"):
        try:
            entry = json.loads(path.read_text())
            text = entry["request"]["messages"][0]["content"]
            sent = json.loads(text[text.index("```json") + 7:text.index("\n```", text.index("```json"))])
        except (ValueError, KeyError, OSError):
            continue
        if {f["unique_id"] for f in sent.get("failures", [])} == wanted and entry["response"].get("text"):
            if best is None or entry["saved_at"] > best["saved_at"]:
                best = entry
    if best is None:
        return None
    record = record_from_snapshot(best["response"])
    record["cost_usd"] = 0.0
    record["cache"] = {"hit": True, "saved_at": best["saved_at"], "note": "replayed earlier paid answer"}
    return record


# The shape of a real secret, not just its prefix: a bare "sk-ant-" also
# matches this file and any test that mentions a key. None of these patterns
# matches its own source text.
SECRET_PATTERNS = {
    "Anthropic API key": re.compile(r"sk-ant-[A-Za-z0-9_-]{20,}"),
    "private key": re.compile(r"-----BEGIN (?:[A-Z]+ )?PRIVATE KEY-----"),
    "AWS secret key": re.compile(r"aws_secret_access_key\s*[=:]\s*['\"]?[A-Za-z0-9/+]{40}"),
    "GitHub token": re.compile(r"\b(?:ghp|gho|ghs|ghu|github_pat)_[A-Za-z0-9_]{30,}"),
}


def secret_problems(root: Path) -> list[str]:
    """Anything in a tree about to be pushed that looks like a credential."""
    problems = []
    for path in root.rglob("*"):
        rel = path.relative_to(root).as_posix()
        if not path.is_file() or rel.startswith(".git/"):
            continue
        if path.name == ".env" or (path.name.startswith(".env.") and not path.name.endswith(".example")):
            problems.append(f"{rel}: environment file")
            continue
        try:
            text = path.read_text(errors="ignore")
        except OSError:
            continue
        problems += [f"{rel}: looks like a {name}" for name, rx in SECRET_PATTERNS.items() if rx.search(text)]
    return problems


def make_sandbox(ws: Path, github_repo: str | None = None, label: str = "",
                 source: Path | None = None) -> tuple[Path, str]:
    """A git repo whose main is the broken project (fault committed), pushed
    to a remote: a local bare repo by default, or a real (private) GitHub
    repo. Only the sandbox's own `origin` points there; the real checkout's
    remotes are never touched.

    A GitHub sandbox is reused, not recreated: if its main already exists,
    the current project (with this scenario's fault) is committed on top of
    it as an ordinary update by whoever runs this, and pushed fast-forward."""
    from remediation.gitops import git

    repo, remote = ws / "sandbox", ws / "sandbox-remote.git"
    for p in (repo, remote):
        shutil.rmtree(p, ignore_errors=True)
    shutil.copytree(source or ws / "broken", repo, ignore=shutil.ignore_patterns(
        "target", "logs", "dbt_packages", "*.duckdb*", SCRATCH_MARKER, ".dagster", ".remediation-github"))
    if github_repo:
        problems = secret_problems(repo)
        if problems:
            raise SystemExit("refusing to push: possible secrets in the sandbox copy:\n  " + "\n  ".join(problems))
    else:
        git(ws, "init", "--bare", "-b", "main", str(remote))
    git(repo, "init", "-b", "main")
    git(repo, "add", "-A")
    if github_repo:
        return repo, _sync_github_sandbox(repo, github_repo, label)
    git(repo, "commit", "-m", "main, with the scenario's fault",
        env={"GIT_AUTHOR_NAME": "chaos", "GIT_AUTHOR_EMAIL": "chaos@localhost",
             "GIT_COMMITTER_NAME": "chaos", "GIT_COMMITTER_EMAIL": "chaos@localhost"})
    git(repo, "remote", "add", "origin", str(remote))
    git(repo, "push", "origin", "main")
    return repo, str(remote)


def _sync_github_sandbox(repo: Path, github_repo: str, label: str, url: str | None = None) -> str:
    """Commit the staged tree on top of the sandbox's current main (or as its
    first commit) with the runner's own git identity, and push. A general
    update, so it is the maintainer's commit, never the bot's."""
    from remediation.gitops import git

    url = url or f"https://github.com/{github_repo}.git"
    git(repo, "remote", "add", "origin", url)
    # gh as credential helper for this repo only, never globally.
    git(repo, "config", "credential.https://github.com.helper", "")
    git(repo, "config", "--add", "credential.https://github.com.helper", "!gh auth git-credential")
    message = f"Sync sandbox with the current project ({label} fault applied)" if label else "Sync sandbox"
    if git(repo, "ls-remote", "origin", "refs/heads/main"):
        git(repo, "fetch", "origin", "main")
        # Point main at the remote's tip without touching the staged tree, so
        # the next commit is "current project" on top of the sandbox history.
        git(repo, "update-ref", "refs/heads/main", "FETCH_HEAD")
        if subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=repo).returncode == 0:
            print("   sandbox main already matches the current project")
            return url
        git(repo, "commit", "-m", message)
        git(repo, "push", "origin", "main")
        print(f"   pushed an update to {github_repo} main")
    else:
        git(repo, "commit", "-m", message)
        git(repo, "push", "origin", "main")
        print(f"   pushed the first main to {github_repo}")
    return url


def sandbox_command(target: str, scenarios: dict, root: Path, policy: dict, github_repo: str) -> int:
    """Set the GitHub sandbox's main to the clean project, or to the project
    with a scenario's fault committed: an ordinary commit by whoever runs
    this, exactly like a person pushing a bad change. With the remediation
    workflow enabled in the sandbox, that push is all it takes to start the
    autonomous loop."""
    if target == "clean":
        ws = root / "_clean"
        shutil.rmtree(ws, ignore_errors=True)
        make_scratch(ws / "project")
        make_sandbox(ws, github_repo, "no fault: clean project", source=ws / "project")
        return 0
    scenario = scenarios[target]
    ws = root / scenario.id
    stamp = ws / "fingerprint"
    if not stamp.exists() or stamp.read_text() != fingerprint():
        print(f"-> building the {scenario.id} workspace from the current code (no API calls)", flush=True)
        run_scenario(scenario, root, policy, ensure_baseline(root))
    make_sandbox(ws, github_repo, scenario.id)
    return 0


def pr_command(scenario: Scenario, root: Path, policy: dict, fresh_sandbox: bool = False,
               github_repo: str | None = None, diagnosis_file: Path | None = None) -> int:
    """Phase 4 end to end against a sandbox repo: a local bare remote and
    stand-in tracker by default, or a real private GitHub repo."""
    from remediation.agent import write_record
    from remediation.github import GitHubTracker, LocalTracker
    from remediation.pipeline import run

    ws = _workspace(scenario, root)
    bundle = json.loads((ws / "bundle.json").read_text())
    if not bundle["failures"]:
        print(f"Nothing failed in {scenario.id} (absorbed); no PR or issue to open.")
        return 0
    # A hand-written diagnosis (e.g. a deliberately flawed first attempt for
    # the Phase 5 demo) still goes through the validator like any other.
    first = (json.loads(diagnosis_file.read_text()) if diagnosis_file else saved_diagnosis(ws))
    if first is None:
        print("No saved diagnosis for this failure: run `make chaos-diagnose` first (one API call).")
        return 1
    stamp = ws / "fingerprint"
    if github_repo and (not stamp.exists() or stamp.read_text() != fingerprint()):
        # The workspace's project copy predates the current code; pushing it
        # would publish stale code. Rebuild it (free), keeping the diagnosis.
        print(f"-> {scenario.id} workspace predates the current code: rebuilding it (no API calls)", flush=True)
        clean = ensure_baseline(root)
        rebuilt = run_scenario(scenario, root, policy, clean)
        if not rebuilt.ok:
            print(f"   the scenario no longer behaves as expected after the rebuild: {rebuilt.unexpected or rebuilt.missing_expected}")
            return 1
        bundle = json.loads((ws / "bundle.json").read_text())
    if not (ws / "diagnosis.json").exists() and not diagnosis_file:
        write_record(first, ws)  # keep it: the response cache expires after 24h
    if github_repo:
        from remediation.ghapp import acting_as_app, configured

        print(f"-> syncing {github_repo} main with the current project (as you)", flush=True)
        make_sandbox(ws, github_repo, scenario.id)
        tracker = GitHubTracker(ws / "sandbox")
        app = acting_as_app(github_repo)
        print("   remediation acts as " + ("dbt-ecom-remediation[bot]" if configured() else
                                          "you: the GitHub App isn't configured (REMEDIATION_APP_ID)"), flush=True)
    else:
        if fresh_sandbox or not (ws / "sandbox").is_dir():
            make_sandbox(ws)
        tracker = LocalTracker(ws / "github")
    asked = []

    def ask(b, p, previous):  # only reached on a validator rejection
        asked.append(1)
        return make_asker(policy)(b, p, previous)

    where = github_repo or f"sandbox repo, local tracker in {ws.name}/github/"
    print(f"-> Phase 4 for {scenario.id}: {where}", flush=True)
    if not github_repo:
        from contextlib import nullcontext
        app = nullcontext()
    with app:
        result = run(bundle, ws / "broken", policy, tracker, repo=ws / "sandbox", base_ref="main", remote="origin",
                     ask=ask, work_dir=ws / "pipeline", base_db=ws / "broken.duckdb", first=first,
                     run_label=f"chaos run of {scenario.id}", refresh=bool(github_repo))
    print(f"\n{result.action}: #{result.number} {result.detail}")
    if github_repo and result.url:
        print(f"   {result.url}")
    elif result.action in ("pr_opened", "issue_opened", "recurred"):
        kind = "pr" if result.action == "pr_opened" else ("issue" if result.action == "issue_opened" else None)
        if kind:
            print(f"   rendered: {(ws / 'github' / f'{kind}-{result.number}.md').relative_to(REPO_ROOT)}")
    if result.action == "pr_opened" and not github_repo:
        from remediation.gitops import git
        print("   remote branches:", git(ws, "--git-dir", str(ws / "sandbox-remote.git"), "branch", "--list"))
    if asked:
        print(f"   ({len(asked)} paid retry call(s) were needed)")
    return 0


# -- report and CLI ---------------------------------------------------------------

def render_report(outcomes: list[Outcome]) -> str:
    lines = [
        "## Fault-injection scenarios", "",
        "| Scenario | As expected | Tier | Failing | Warnings | Time |",
        "|---|---|---|---|---|---|",
    ]
    for o in outcomes:
        problems = [o.error] if o.error else []
        problems += [f"missing {e.get('test')} on {e.get('model')}" for e in o.missing_expected] + o.unexpected
        bit = "yes" if not problems else "**no**: " + "; ".join(problems)
        if not o.expected_tier:
            tier = "absorbed"
        else:
            tier = o.actual_tier if o.actual_tier == o.expected_tier else f"**{o.actual_tier or '?'}** (expected {o.expected_tier})"
        lines.append(f"| `{o.scenario}` | {bit} | {tier} | {len(o.failing_tests)} | {len(o.warning_tests)} | {o.seconds}s |")
    passed = sum(o.ok for o in outcomes)
    lines += ["", f"{passed}/{len(outcomes)} scenarios OK."]

    scored = [o for o in outcomes if o.diagnosis is not None]
    if scored:
        lines += [
            "", "## Claude diagnosis", "",
            "| Scenario | Verdict | Confidence | Edits apply | Right files | Diagnosis | Fix | Route | Cost |",
            "|---|---|---|---|---|---|---|---|---|",
        ]
        for o in scored:
            d = o.diagnosis
            if d.get("error"):
                lines.append(f"| `{o.scenario}` | **error** | | | | {d['error']} | | | |")
                continue
            verdict = d["verdict"] if d["verdict_ok"] else f"**{d['verdict']}**"
            applies = "—" if d["verdict"] != "code_fix" else ("yes" if d["edits_apply"] else "**no**")
            files = "—" if "files_ok" not in d or d["verdict"] != "code_fix" else ("yes" if d["files_ok"] else "**no**")
            cost = "cached" if d.get("cached") else (f"${d['cost_usd']:.3f}" if d.get("cost_usd") is not None else "")
            fix = {"validated": "validated", None: "—"}.get(d.get("fix"), f"**{d.get('fix')}**")
            if d.get("attempts", 1) > 1:
                fix += f" ({d['attempts']} attempts)"
            lines.append(f"| `{o.scenario}` | {verdict} | {d['confidence']:.2f} | {applies} | {files} | "
                         f"{'pass' if d['passed'] else '**miss**'} | {fix} | {d.get('route') or '—'} | {cost} |")
        total = sum(d.get("cost_usd") or 0 for d in (o.diagnosis for o in scored))
        hits = sum(o.diagnosis["passed"] for o in scored)
        fixes = [o.diagnosis.get("fix") for o in scored if o.diagnosis.get("fix")]
        lines += ["", f"{hits}/{len(scored)} diagnoses passed; "
                      f"{fixes.count('validated')}/{len(fixes)} code fixes validated; ~${total:.2f} total."]
    return "\n".join(lines) + "\n"


def _next_steps(scenario_id: str) -> str:
    from remediation.warehouse import CURRENT

    if not CURRENT.is_duckdb:
        t = f" TARGET={CURRENT.target}"
        return "\n".join([
            "", "Next steps (the table-comparison commands need DuckDB, so they don't apply here):",
            f"  make chaos-diagnose SCENARIO={scenario_id}{t}   ask Claude (one API call)",
            f"  make chaos-fix      SCENARIO={scenario_id}{t}   validate the fix, built on {CURRENT.target}",
        ])
    return "\n".join([
        "", "Next steps:",
        f"  make chaos-changes  SCENARIO={scenario_id}                 which tables the fault changed",
        f"  make chaos-diff     SCENARIO={scenario_id} TABLE=<schema.table>  the rows themselves",
        f"  make chaos-ui       SCENARIO={scenario_id}                 browse clean/broken in the DuckDB UI",
        f"  make chaos-diagnose SCENARIO={scenario_id}                 ask Claude (one API call)",
    ])


def default_env_file(target: str | None) -> Path | None:
    """.env.snowflake for snowflake_*, and so on, unless ENV_FILE names
    something other than the generic .env (the Makefile's default)."""
    import os

    chosen = os.environ.get("ENV_FILE")
    if chosen and Path(chosen).name != ".env":
        return REPO_ROOT / chosen if not Path(chosen).is_absolute() else Path(chosen)
    if not target or target in ("dev", "ci"):
        return None
    return REPO_ROOT / f".env.{target.split('_')[0]}"


def _load_api_env() -> None:
    """ANTHROPIC_API_KEY and the App settings live in .env, separate from any
    warehouse's credentials; load them without overriding the shell."""
    import os

    from remediation.warehouse import load_env_file

    path = REPO_ROOT / ".env"
    if path.exists():
        for key, value in load_env_file(path).items():
            os.environ.setdefault(key, value)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--root", type=Path, default=None,
                        help="workspace root (default .chaos/, or .chaos/_wh_<target>/ on a cloud warehouse)")
    parser.add_argument("--target", default=None,
                        help="dbt target to build on, e.g. snowflake_prod, redshift_prod, bigquery_local")
    parser.add_argument("--env-file", type=Path, default=None,
                        help="credentials for --target (default: .env.snowflake / .env.redshift / .env.bigquery)")
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run", help="inject, build and bundle")
    run.add_argument("scenario", help="scenario id, or 'all'")
    run.add_argument("--diagnose", action="store_true",
                     help="also send each bundle to Claude and score the diagnosis (costs API credits)")
    for name, help_ in [("diagnose", "ask Claude about an existing workspace (one API call)"),
                        ("fix", "validate Claude's edits (Phase 3), retrying with feedback if rejected"),
                        ("rebuild", "validate fixed/ as it stands (your own fix)"),
                        ("pr", "Phase 4 against a sandbox repo: open the PR or issue this failure would get")]:
        sub.add_parser(name, help=help_).add_argument("scenario")
    sub.choices["pr"].add_argument("--github-repo", metavar="OWNER/NAME",
                                   help="a real private GitHub repo to use as the sandbox remote")
    sub.choices["pr"].add_argument("--diagnosis", type=Path,
                                   help="use this diagnosis JSON instead of the saved one (still validated)")
    sb = sub.add_parser("sandbox", help="set the GitHub sandbox's main to the clean project or a scenario's fault")
    sb.add_argument("target", help="'clean' or a scenario id")
    sb.add_argument("--github-repo", metavar="OWNER/NAME", required=True)
    for p in (run, sub.choices["diagnose"], sub.choices["fix"]):
        p.add_argument("--fresh", action="store_true", help="ignore the local response cache")
    args = parser.parse_args(argv)

    from remediation.warehouse import CURRENT, use
    _load_api_env()
    use(args.target, args.env_file or default_env_file(args.target))
    if args.root is None:
        args.root = CHAOS_DIR if CURRENT.is_duckdb else CHAOS_DIR / f"_wh_{CURRENT.target}"
    if not CURRENT.is_duckdb:
        print(f"-> warehouse: {CURRENT.label}, workspaces in {args.root.relative_to(REPO_ROOT)}", flush=True)

    scenarios = load_all()
    policy = load_policy()
    if args.command == "sandbox":
        if args.target != "clean" and args.target not in scenarios:
            print(f"unknown target {args.target!r}; options: clean, {', '.join(scenarios)}", file=sys.stderr)
            return 2
        return sandbox_command(args.target, scenarios, args.root, policy, args.github_repo)
    if args.command != "run" or args.scenario != "all":
        if args.scenario not in scenarios:
            print(f"unknown scenario {args.scenario!r}; options: {', '.join(scenarios)}"
                  + (", all" if args.command == "run" else ""), file=sys.stderr)
            return 2
    if args.command == "diagnose":
        return diagnose_workspace(scenarios[args.scenario], args.root, policy, fresh=args.fresh)
    if args.command in ("fix", "rebuild", "pr"):
        if args.command == "pr":
            return pr_command(scenarios[args.scenario], args.root, policy, github_repo=args.github_repo,
                              diagnosis_file=args.diagnosis)
        if args.command == "fix":
            return fix_command(scenarios[args.scenario], args.root, policy, fresh=args.fresh)
        return rebuild_command(scenarios[args.scenario], args.root, policy)

    selected = list(scenarios.values()) if args.scenario == "all" else [scenarios[args.scenario]]
    ask = None
    if args.diagnose:
        import anthropic
        ask = make_asker(policy, args.fresh, client=anthropic.Anthropic())

    args.root.mkdir(parents=True, exist_ok=True)
    clean = ensure_baseline(args.root)
    outcomes = []
    for scenario in selected:
        print(f"-> {scenario.id}", flush=True)
        outcome = run_scenario(scenario, args.root, policy, clean, ask)
        line = (f"   {'OK  ' if outcome.ok else 'FAIL'} {outcome.seconds}s, "
                f"{len(outcome.failing_tests)} failing, {len(outcome.warning_tests)} warning, "
                f"{'tier ' + outcome.actual_tier if outcome.actual_tier else 'absorbed'}")
        if outcome.diagnosis:
            d = outcome.diagnosis
            line += (f"; diagnosis error: {d['error']}" if d.get("error")
                     else f"; diagnosis {d['verdict']} ({'pass' if d['passed'] else 'miss'})")
        print(line, flush=True)
        outcomes.append(outcome)

    report = render_report(outcomes)
    (args.root / "report.md").write_text(report)
    (args.root / "outcomes.json").write_text(json.dumps([asdict(o) for o in outcomes], indent=2) + "\n")
    print("\n" + report)
    if len(selected) == 1 and not args.diagnose:
        print((args.root / selected[0].id / "summary.md").read_text())
        print(_next_steps(selected[0].id))
    # Scenario rot and API errors fail the run; a wrong diagnosis is a metric.
    api_errors = any(o.diagnosis and o.diagnosis.get("error") for o in outcomes)
    return 0 if all(o.ok for o in outcomes) and not api_errors else 1


if __name__ == "__main__":
    sys.exit(main())
