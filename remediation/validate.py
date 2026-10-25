"""Phase 3: decide whether a proposed patch may become a branch.

A patch is a copy of the project with changes applied: Claude's edits or a
human's. It is checked against the base copy it came from, in order, and the
first failing check rejects it:

  0. failure    — the base build actually has something failing; a patch to
                  a healthy build has nothing to be validated against
  1. changes    — something actually changed
  2. paths      — every changed file is editable and none is protected
  3. parse      — `dbt parse` succeeds, with deprecation warnings as errors
                  (as CI runs it)
  4. tests      — no existing test removed, redefined, re-configured or
                  disabled, and no macro an existing test depends on edited.
                  This is the hard reject: making a test pass by weakening it
                  is the failure this whole layer exists to prevent. New tests
                  are allowed.
  5. meta       — meta.remediation_tier and meta.pii unchanged everywhere,
                  and not introduced on new nodes
  6. build      — `dbt seed` + `dbt build` with the fault still present: every
                  originally failing test passes and nothing else fails

The tests and meta checks compare the base and patched manifests rather than
diffing YAML, so they also catch a `config()` block written into a model's
SQL, or a test disabled somewhere else.

A patch that passes is then routed. The risk tier comes from lineage in the
BASE manifest (risk.py); escalations can only raise it, never lower it:
  - confidence below validator.min_confidence_for_auto
  - an edited model now returns fewer rows than before (a fix that filters
    rows out can make a test pass by hiding data)

    python -m remediation.validate --base <dir> --patched <dir>
"""

import argparse
import json
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path

from remediation.config import load_manifest, load_policy, matches_any
from remediation.dbtrun import PASSING, build, clear_build, ensure_deps, log_tail, run_dbt, statuses
from remediation.inject import SCRATCH_MARKER
from remediation.risk import RiskModel

# Never compared between trees: build output, installed packages, databases.
IGNORED_PARTS = {"target", "logs", "dbt_packages", "__pycache__"}
IGNORED_SUFFIXES = (".duckdb", ".duckdb.wal")
IGNORED_NAMES = {SCRATCH_MARKER}

# Test config that changes what a test checks or whether it blocks.
TEST_CONFIG_KEYS = ("enabled", "severity", "where", "warn_if", "error_if", "fail_calc", "limit")


@dataclass
class Check:
    name: str
    ok: bool
    detail: str = ""


@dataclass
class Validation:
    passed: bool = False
    checks: list[Check] = field(default_factory=list)
    changed_files: list[str] = field(default_factory=list)
    tier: str | None = None
    tier_reasons: list[dict] = field(default_factory=list)
    escalations: list[str] = field(default_factory=list)
    route: str | None = None  # auto | quarantine, set only when passed
    originally_failing: dict[str, str] = field(default_factory=dict)

    @property
    def rejection(self) -> str | None:
        failed = next((c for c in self.checks if not c.ok), None)
        return f"{failed.name}: {failed.detail}" if failed else None

    def to_dict(self) -> dict:
        return {**asdict(self), "rejection": self.rejection}


# -- individual checks (pure functions, unit-tested against synthetic data) --------

def _tracked(root: Path) -> dict[str, Path]:
    files = {}
    for path in root.rglob("*"):
        rel = path.relative_to(root)
        if (path.is_file() and not IGNORED_PARTS & set(rel.parts)
                and not path.name.endswith(IGNORED_SUFFIXES) and path.name not in IGNORED_NAMES):
            files[rel.as_posix()] = path
    return files


def changed_files(base: Path, patched: Path) -> list[str]:
    a, b = _tracked(base), _tracked(patched)
    return sorted(p for p in set(a) | set(b)
                  if p not in a or p not in b or a[p].read_bytes() != b[p].read_bytes())


def path_problems(paths: list[str], policy: dict) -> list[str]:
    problems = []
    for p in paths:
        if matches_any(p, policy["protected_paths"]):
            problems.append(f"{p} is a protected path")
        elif not matches_any(p, policy["editable_paths"]):
            problems.append(f"{p} is outside the editable paths")
    return problems


def _test_signature(node: dict) -> dict:
    meta = node.get("test_metadata") or {}
    return {
        "test": meta.get("name"),
        "namespace": meta.get("namespace"),
        "kwargs": meta.get("kwargs"),
        "column": node.get("column_name"),
        "attached": node.get("attached_node"),
        "config": {k: node.get("config", {}).get(k) for k in TEST_CONFIG_KEYS},
        # Singular tests are their SQL.
        "sql": None if meta else node.get("raw_code"),
    }


def _project_macros_used(manifest: dict, macro_ids: list[str]) -> set[str]:
    macros, project = manifest.get("macros", {}), manifest["metadata"].get("project_name")
    seen, stack = set(), list(macro_ids)
    while stack:
        uid = stack.pop()
        if uid in seen or uid not in macros:
            continue
        seen.add(uid)
        stack.extend(macros[uid]["depends_on"]["macros"])
    return {macros[u]["original_file_path"] for u in seen if macros[u].get("package_name") == project}


def tampered_tests(base: dict, patched: dict, changed: list[str]) -> list[str]:
    problems = []
    tests = {u: n for u, n in base["nodes"].items() if n["resource_type"] == "test"}
    for uid, node in sorted(tests.items()):
        other = patched["nodes"].get(uid)
        if other is None:
            problems.append(f"test {node['name']} was removed or its definition changed")
            continue
        a, b = _test_signature(node), _test_signature(other)
        if a != b:
            fields = sorted(k for k in a if a[k] != b[k])
            problems.append(f"test {node['name']} changed ({', '.join(fields)})")
        for macro_file in _project_macros_used(base, node["depends_on"]["macros"]) & set(changed):
            problems.append(f"{macro_file} is used by test {node['name']}")
    return problems


def _protected_meta(node: dict, keys: list[str]) -> dict:
    meta = {**(node.get("meta") or {}), **(node.get("config", {}).get("meta") or {})}
    out = {k: meta.get(k) for k in keys if k in meta}
    for name, col in (node.get("columns") or {}).items():
        col_meta = {**(col.get("meta") or {}), **(col.get("config", {}).get("meta") or {})}
        for k in keys:
            if k in col_meta:
                out[f"{name}.{k}"] = col_meta[k]
    return out


def meta_problems(base: dict, patched: dict, keys: list[str]) -> list[str]:
    problems = []
    groups = ("nodes", "sources")
    base_all = {u: n for g in groups for u, n in base.get(g, {}).items()}
    for uid, node in {u: n for g in groups for u, n in patched.get(g, {}).items()}.items():
        if node.get("resource_type") == "test":
            continue
        before = _protected_meta(base_all[uid], keys) if uid in base_all else {}
        after = _protected_meta(node, keys)
        if before != after:
            keys_changed = sorted(k for k in set(before) | set(after) if before.get(k) != after.get(k))
            what = "introduces" if uid not in base_all else "changes"
            problems.append(f"{uid.split('.')[-1]} {what} protected meta: {', '.join(keys_changed)}")
    return problems


def row_losses(base_db: Path, patched_db: Path, relations: dict[str, str]) -> list[str]:
    """Models (name -> relation) that return fewer rows after the patch."""
    import duckdb

    losses = []
    with duckdb.connect() as con:
        con.execute(f"ATTACH '{base_db}' AS base (READ_ONLY)")
        con.execute(f"ATTACH '{patched_db}' AS dev (READ_ONLY)")
        for name, relation in relations.items():
            schema, table = [p.strip('"') for p in relation.split(".")[-2:]]
            try:
                before = con.execute(f'SELECT count(*) FROM base."{schema}"."{table}"').fetchone()[0]
                after = con.execute(f'SELECT count(*) FROM dev."{schema}"."{table}"').fetchone()[0]
            except duckdb.Error:
                continue  # not built in the base (skipped) — nothing to compare
            if after < before:
                losses.append(f"{name} returns {before - after} fewer rows ({before} -> {after})")
    return losses


# -- the validator ----------------------------------------------------------------

class Validator:
    """base: the project the patch was made against, with its build artifacts
    (target/manifest.json and run_results.json). base_db: a frozen snapshot of
    the base build, for the row-loss escalation; optional."""

    def __init__(self, base: Path, policy: dict | None = None, base_db: Path | None = None):
        self.base = base
        self.policy = policy or load_policy()
        self.base_db = base_db
        self.base_manifest = load_manifest(base / "target" / "manifest.json")
        self.base_status = statuses(base / "target")

    def validate(self, patched: Path, log_path: Path, confidence: float | None = None) -> Validation:
        v = Validation()
        originally = [u for u, s in self.base_status.items() if s not in PASSING | {"skipped", "warn"}]

        def check(name: str, problems: list[str], ok_detail: str = "") -> bool:
            v.checks.append(Check(name, not problems, "; ".join(problems) if problems else ok_detail))
            return not problems

        if not check("failure", [] if originally else ["the base build has no failures to fix"],
                     f"{len(originally)} failing node(s) in the base build"):
            return v
        v.changed_files = changed_files(self.base, patched)
        if not check("changes", [] if v.changed_files else ["the patch changes nothing"],
                     f"{len(v.changed_files)} file(s): {', '.join(v.changed_files)}"):
            return v
        if not check("paths", path_problems(v.changed_files, self.policy), "all editable"):
            return v

        with open(log_path, "w") as log:
            clear_build(patched)
            ensure_deps(patched, log)
            # Deprecations as errors, exactly as CI's portability job: a patch
            # that parses with a deprecation warning would otherwise pass here
            # and fail CI (the Phase 5 demo proved this gap was real).
            parsed = run_dbt(patched, log, "parse", "--warn-error-options", '{"error": ["Deprecations"]}') == 0
        if not check("parse", [] if parsed else [f"dbt parse failed:\n{log_tail(log_path)}"], "ok"):
            return v
        patched_manifest = load_manifest(patched / "target" / "manifest.json")

        if not check("tests", tampered_tests(self.base_manifest, patched_manifest, v.changed_files),
                     "no existing test touched"):
            return v
        if not check("meta", meta_problems(self.base_manifest, patched_manifest, self.policy["protected_meta_keys"]),
                     "protected meta unchanged"):
            return v

        with open(log_path, "a") as log:
            try:
                build(patched, log)
            except RuntimeError as exc:
                check("build", [str(exc)])
                return v
        after = statuses(patched / "target")
        name = lambda uid: patched_manifest["nodes"].get(uid, {}).get("name", uid)  # noqa: E731
        v.originally_failing = {name(u): after.get(u, "missing") for u in originally}
        bad = {name(u): s for u, s in after.items() if s not in PASSING}
        # A warn is a pass for blocking purposes: warn-severity tests exist
        # precisely so a build can surface a problem without stopping.
        bad = {n: s for n, s in bad.items() if s != "warn"}
        problems = [f"{n}: {s}" for n, s in sorted(bad.items())]
        if not check("build", problems, f"clean; {len(originally)} originally failing node(s) now pass"):
            if "error" in bad.values():
                v.checks[-1].detail += f"\n{log_tail(log_path)}"
            return v

        v.passed = True
        self._route(v, patched, patched_manifest, confidence)
        return v

    def _route(self, v: Validation, patched: Path, patched_manifest: dict, confidence: float | None) -> None:
        assessment = RiskModel(self.base_manifest, self.policy).assess_files(v.changed_files)
        v.tier, v.tier_reasons = assessment.tier, assessment.to_dict()["reasons"]
        floor = self.policy["validator"]["min_confidence_for_auto"]
        if confidence is not None and confidence < floor:
            v.escalations.append(f"confidence {confidence:.2f} is below {floor}")
        if self.base_db and (patched / "dev.duckdb").exists():
            edited = {n["name"]: n["relation_name"] for n in patched_manifest["nodes"].values()
                      if n["resource_type"] == "model" and n.get("original_file_path") in v.changed_files
                      and n.get("relation_name")}
            v.escalations += row_losses(self.base_db, patched / "dev.duckdb", edited)
        v.route = "quarantine" if v.tier != self.policy["tiers"][0] or v.escalations else "auto"


def render(v: Validation, title: str = "Validation") -> str:
    lines = [f"## {title}", ""]
    if v.passed:
        lines.append(f"**PASSED**, routed **{v.route}** (lineage tier: {v.tier})")
    else:
        lines.append(f"**REJECTED** at {v.rejection}")
    lines.append("")
    for c in v.checks:
        lines.append(f"- {'✓' if c.ok else '✗'} **{c.name}**: {c.detail.splitlines()[0] if c.detail else ''}")
        lines += [f"    {line}" for line in c.detail.splitlines()[1:]]
    if v.originally_failing:
        lines += ["", "Originally failing:"] + [f"- `{n}`: {s}" for n, s in v.originally_failing.items()]
    if v.passed:
        lines += ["", "Why this tier:"] + [f"- `{r['file']}`: {r['detail']}" for r in v.tier_reasons]
        if v.escalations:
            lines += ["", "Escalated to quarantine:"] + [f"- {e}" for e in v.escalations]
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--base", type=Path, required=True, help="project the patch was made against, already built")
    parser.add_argument("--patched", type=Path, required=True, help="copy of the base with the changes applied")
    parser.add_argument("--base-db", type=Path, help="frozen snapshot of the base build (row-loss check)")
    parser.add_argument("--confidence", type=float)
    parser.add_argument("--out", type=Path, help="write validation.json and validation.md here")
    args = parser.parse_args(argv)

    v = Validator(args.base, base_db=args.base_db).validate(
        args.patched, (args.out or args.patched.parent) / "validate.log", args.confidence)
    if args.out:
        args.out.mkdir(parents=True, exist_ok=True)
        (args.out / "validation.json").write_text(json.dumps(v.to_dict(), indent=2) + "\n")
        (args.out / "validation.md").write_text(render(v))
    print(render(v))
    return 0 if v.passed else 1


if __name__ == "__main__":
    sys.exit(main())
