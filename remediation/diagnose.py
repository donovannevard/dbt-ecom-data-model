"""Build a diagnostic bundle from a failed dbt run.

The bundle is everything a reviewer — human or model — needs to diagnose a
failure without access to the warehouse: what failed and how, the test's
definition, the failing model's source and compiled SQL, a sample of the
stored failure rows, upstream lineage, the risk tier a fix would carry, and
recent history of the affected file.

PII is redacted here, before the bundle leaves the machine: any column named
in policy.diagnostics.pii_columns or tagged meta.pii anywhere in the manifest
is masked in failure rows, and email addresses are scrubbed from free text
such as error messages.

Anything under the bundle's "_meta" key is harness bookkeeping (e.g. which
scenario was injected) and must never be included in a model prompt — the
scenario id alone gives the answer away.

    python -m remediation.diagnose --project-dir . --out target/remediation
"""

import argparse
import datetime as dt
import json
import re
import subprocess
import sys
from collections import deque
from decimal import Decimal
from pathlib import Path

from remediation.config import load_manifest, load_policy
from remediation.risk import RiskModel
from remediation.warehouse import ReadError, literal, reader_for

FAILED = {"fail", "error", "runtime error"}
REDACTED = "[REDACTED]"
EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")


def _truncate(text: str | None, limit: int) -> str | None:
    if text is None or len(text) <= limit:
        return text
    return text[:limit] + f"\n-- [truncated: {len(text) - limit} more characters]"


def _scrub(text: str | None) -> str | None:
    return EMAIL.sub(REDACTED, text) if text else text


def _jsonable(value):
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, Decimal):
        return float(value)
    return str(value)


def pii_columns(manifest: dict, policy: dict) -> set[str]:
    columns = {c.lower() for c in policy["diagnostics"]["pii_columns"]}
    for node in [*manifest["nodes"].values(), *manifest.get("sources", {}).values()]:
        for name, col in (node.get("columns") or {}).items():
            meta = {**(col.get("meta") or {}), **(col.get("config", {}).get("meta") or {})}
            if meta.get("pii"):
                columns.add(name.lower())
    return columns


def _redact(columns: list[str], rows: list[dict], pii: set[str]) -> tuple[list[dict], list[str]]:
    masked = [c for c in columns if c.lower() in pii]
    return [{c: (REDACTED if c in masked and r.get(c) is not None else r.get(c)) for c in columns}
            for r in rows], masked


def _ordered(reader, relation: str, first: list[str] = ()) -> str:
    """`order by` every column (key columns first): a sample must be the same
    rows every run, or the bundle changes and the response cache misses."""
    names = [c["name"] for c in reader.columns(relation)]
    order = list(first) + [n for n in names if n.lower() not in {f.lower() for f in first}]
    return f" order by {', '.join(order)}" if order else ""


def failure_rows(reader, relation: str | None, limit: int, pii: set[str]) -> dict:
    if not relation:
        return {"unavailable": "test does not store failures"}
    if reader is None:
        return {"unavailable": "no access to the database (CI artifacts only)"}
    try:
        columns, raw = reader.query(f"select * from {relation}{_ordered(reader, relation)}", limit)
    except ReadError as exc:
        return {"unavailable": f"could not read {relation}: {exc}"}
    rows, masked = _redact(columns, raw, pii)
    return {"relation": relation, "columns": columns, "rows": rows, "redacted_columns": masked}


def source_schema(reader, relation: str | None, pii: set[str], sample: int = 3) -> dict:
    """Real column names (and, where the warehouse says, types) of a source
    table, plus a few redacted rows.

    Staging models rename source columns, so a fix to one needs the source's
    actual column names. Without them the model has to guess, and a plausible
    guess (session_id for a column really called id) compiles to an error.
    """
    if not relation or reader is None:
        return {"unavailable": "no access to the database"}
    try:
        columns = reader.columns(relation)
    except ReadError as exc:
        return {"unavailable": f"could not describe {relation}: {exc}"}
    rows = failure_rows(reader, relation, sample, pii)
    return {"columns": columns, "sample_rows": rows.get("rows", []),
            "redacted_columns": rows.get("redacted_columns", [])}


DUPLICATE_KEY_TESTS = {"unique": "column_name", "unique_combination_of_columns": "combination_of_columns"}


def duplicate_key_rows(reader, relation: str | None, test: dict, stored: dict,
                       limit: int, pii: set[str]) -> dict | None:
    """For a failing uniqueness test, the model's full rows behind each
    duplicated key.

    The stored failures only say a key occurs N times. The rows themselves
    show whether the copies are identical (a re-delivery, or a fan-out in the
    SQL) or disagree (two real records sharing a key) — the distinction a
    diagnosis turns on.
    """
    meta = test.get("test_metadata") or {}
    arg = DUPLICATE_KEY_TESTS.get(meta.get("name"))
    if not arg or not relation or "rows" not in stored or reader is None:
        return None
    kwargs = meta.get("kwargs") or {}
    keys = kwargs.get(arg) or test.get("column_name")
    keys = [keys] if isinstance(keys, str) else list(keys or [])
    if not keys:
        return None
    # `unique` stores the key as unique_field; the combination test stores
    # the key columns under their own names (Snowflake upper-cases them).
    def get(row, col):
        return row.get(col, row.get(col.upper(), row.get(col.lower())))
    stored_cols = ["unique_field"] if meta.get("name") == "unique" else keys
    values = [tuple(get(row, c) for c in stored_cols) for row in stored["rows"]]
    values = [v for v in values if REDACTED not in v][:limit]
    if not values:
        return None
    # Portable SQL: unquoted identifiers, inlined literals (see warehouse.py).
    where = " or ".join("(" + " and ".join(f"{k} = {literal(v)}" for k, v in zip(keys, vals)) + ")"
                        for vals in values)
    try:
        columns, raw = reader.query(f"select * from {relation} where {where}{_ordered(reader, relation, keys)}", limit)
    except ReadError:
        return None
    rows, masked = _redact(columns, raw, pii)
    return {"columns": columns, "rows": rows, "redacted_columns": masked, "key_columns": keys}


def upstream(manifest: dict, uid: str, hops: int) -> list[tuple[str, int]]:
    parent_map = manifest["parent_map"]
    seen, out = {uid}, []
    queue = deque([(uid, 0)])
    while queue:
        current, depth = queue.popleft()
        if depth == hops:
            continue
        for parent in parent_map.get(current, []):
            if parent not in seen:
                seen.add(parent)
                out.append((parent, depth + 1))
                queue.append((parent, depth + 1))
    return out


def recent_commits(root: Path, path: str | None, n: int = 5) -> list[str]:
    if not path:
        return []
    try:
        result = subprocess.run(
            ["git", "-C", str(root), "log", f"-n{n}", "--date=short", "--format=%h %ad %s", "--", path],
            capture_output=True, text=True, check=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return []
    return result.stdout.splitlines()


def _node_summary(manifest: dict, uid: str) -> dict:
    node = manifest["nodes"].get(uid) or manifest.get("sources", {}).get(uid) or {}
    patch = node.get("patch_path")
    return {
        "unique_id": uid,
        "name": node.get("name"),
        "resource_type": node.get("resource_type"),
        "file": node.get("original_file_path"),
        "yaml_file": patch.split("://", 1)[-1] if patch else None,
    }


def _test_definition(manifest: dict, test: dict, limit: int) -> dict:
    meta = test.get("test_metadata") or {}
    config = test.get("config", {})
    definition = {
        "type": meta.get("name", "singular"),
        "namespace": meta.get("namespace"),
        "arguments": {k: v for k, v in (meta.get("kwargs") or {}).items() if k != "model"},
        "column": test.get("column_name"),
        "severity": config.get("severity"),
        "where": config.get("where"),
        "file": test.get("original_file_path"),
    }
    # Project-defined generic tests aren't common knowledge; include their SQL.
    project = manifest["metadata"].get("project_name")
    macro = manifest.get("macros", {}).get(f"macro.{project}.test_{meta.get('name')}")
    if macro:
        definition["definition_sql"] = _truncate(macro["macro_sql"], limit)
    if not meta:
        definition["definition_sql"] = _truncate(test.get("raw_code"), limit)
    return definition


def build_bundle(project_dir: Path, target_dir: Path, db_path: Path | None, scenario_id: str | None = None,
                 reader=None) -> dict:
    """project_dir: where the failed run happened (for git history and, on a
    cloud warehouse, for `dbt show`). db_path: its DuckDB file, if DuckDB."""
    policy = load_policy()
    limits = policy["diagnostics"]
    sql_limit = limits["max_sql_chars"]
    manifest = load_manifest(target_dir / "manifest.json")
    run_results = json.loads((target_dir / "run_results.json").read_text())
    risk = RiskModel(manifest, policy)
    pii = pii_columns(manifest, policy)
    reader = reader or reader_for(project_dir, db_path)

    counts: dict[str, int] = {}
    for r in run_results["results"]:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    skipped = sorted(
        r["unique_id"].split(".")[-1] if not r["unique_id"].startswith("test.")
        else manifest["nodes"].get(r["unique_id"], {}).get("name", r["unique_id"])
        for r in run_results["results"] if r["status"] == "skipped"
    )

    # Models and sources are stored once and referenced by name: several
    # failures from one cause share most of their lineage, and repeating it
    # per failure multiplied the bundle (and the prompt) for no new signal.
    models: dict[str, dict] = {}
    sources: dict[str, dict] = {}

    def add_model(uid: str, failing: bool) -> str:
        node = manifest["nodes"][uid]
        entry = models.get(node["name"])
        if entry is None:
            entry = models[node["name"]] = {
                **_node_summary(manifest, uid),
                "materialized": node["config"].get("materialized"),
                "raw_sql": _truncate(node.get("raw_code"), sql_limit),
            }
        if failing and "compiled_sql" not in entry:
            # Compiled SQL only where a test failed: it's long, and upstream
            # models' raw SQL says the same thing.
            entry["compiled_sql"] = _truncate(node.get("compiled_code"), sql_limit)
            entry["recent_commits"] = recent_commits(project_dir, entry["file"])
        return node["name"]

    def add_source(uid: str) -> str:
        node = manifest["sources"][uid]
        if node["name"] not in sources:
            sources[node["name"]] = {**_node_summary(manifest, uid),
                                     **source_schema(reader, node.get("relation_name"), pii)}
        return node["name"]

    failures = []
    for result in run_results["results"]:
        if result["status"] not in FAILED:
            continue
        uid = result["unique_id"]
        node = manifest["nodes"].get(uid, {})
        is_test = node.get("resource_type") == "test"
        model_uid = node.get("attached_node") if is_test else uid
        if is_test and not model_uid:
            deps = [d for d in node.get("depends_on", {}).get("nodes", []) if d.startswith("model.")]
            model_uid = deps[0] if deps else None

        entry = {
            "kind": "test_failure" if is_test else "node_error",
            "unique_id": uid,
            "name": node.get("name"),
            "status": result["status"],
            "message": _scrub(result.get("message")),
            "failures": result.get("failures"),
            "compiled_sql": _truncate(result.get("compiled_code") or node.get("compiled_code"), sql_limit),
        }
        if is_test:
            entry["test"] = _test_definition(manifest, node, sql_limit)
            relation = result.get("relation_name") or node.get("relation_name")
            entry["failure_rows"] = failure_rows(reader, relation, limits["max_failure_rows"], pii)

        if model_uid and model_uid in manifest["nodes"]:
            entry["model"] = add_model(model_uid, failing=True)
            # What a fix to this model's SQL would be routed as.
            entry["fix_risk"] = risk.assess_files([models[entry["model"]]["file"]]).to_dict()
            if is_test:
                rows = duplicate_key_rows(reader, manifest["nodes"][model_uid].get("relation_name"),
                                          node, entry["failure_rows"], limits["max_failure_rows"], pii)
                if rows:
                    entry["rows_for_duplicate_keys"] = rows
            entry["upstream"] = []
            for parent, depth in upstream(manifest, model_uid, limits["lineage_hops"]):
                if parent.startswith("model."):
                    name, kind = add_model(parent, failing=False), "model"
                elif parent.startswith("source."):
                    name, kind = add_source(parent), "source"
                else:
                    name, kind = parent.split(".")[-1], parent.split(".")[0]
                entry["upstream"].append({"name": name, "resource_type": kind, "hops": depth})
        failures.append(entry)

    # Warn-severity results don't stop anything, but they are context: a
    # failure next to a duplicate-key warning on its source reads differently.
    warnings = [
        {"name": manifest["nodes"].get(r["unique_id"], {}).get("name", r["unique_id"]),
         "unique_id": r["unique_id"], "message": _scrub(r.get("message")), "failures": r.get("failures")}
        for r in run_results["results"] if r["status"] == "warn"
    ]

    return {
        "schema_version": 2,
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "run": {
            "dbt_version": run_results["metadata"].get("dbt_version"),
            "adapter": manifest["metadata"].get("adapter_type"),
            "invocation_id": run_results["metadata"].get("invocation_id"),
            "elapsed_seconds": round(run_results.get("elapsed_time", 0), 1),
            "status_counts": counts,
            "skipped": skipped,
        },
        "failures": failures,
        "models": models,
        "sources": sources,
        "warnings": warnings,
        "_meta": {
            "scenario_id": scenario_id,
            "note": "harness metadata — never include in a model prompt",
        },
    }


def render_summary(bundle: dict) -> str:
    run = bundle["run"]
    counts = ", ".join(f"{v} {k}" for k, v in sorted(run["status_counts"].items()))
    lines = [f"## dbt diagnostic bundle", "", f"dbt {run['dbt_version']} — {counts}", ""]
    if bundle["_meta"].get("scenario_id"):
        lines += [f"Injected scenario: `{bundle['_meta']['scenario_id']}`", ""]
    if not bundle["failures"]:
        return "\n".join(lines + ["No failures."]) + "\n"
    lines += ["| Failure | Model | Fix tier | Rows sampled |", "|---|---|---|---|"]
    for f in bundle["failures"]:
        rows = f.get("failure_rows", {})
        sampled = len(rows["rows"]) if "rows" in rows else rows.get("unavailable", "—")
        tier = f.get("fix_risk", {}).get("tier", "—")
        lines.append(f"| `{f['name']}` ({f['status']}) | `{f.get('model', '—')}` | {tier} | {sampled} |")
    if run["skipped"]:
        lines += ["", f"{len(run['skipped'])} downstream node(s) skipped."]
    return "\n".join(lines) + "\n"


def write_bundle(bundle: dict, out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "bundle.json").write_text(json.dumps(bundle, indent=2) + "\n")
    (out_dir / "summary.md").write_text(render_summary(bundle))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--project-dir", type=Path, default=Path("."))
    parser.add_argument("--target-dir", type=Path, help="default: <project-dir>/target")
    parser.add_argument("--db", type=Path, help="default: <project-dir>/dev.duckdb")
    parser.add_argument("--scenario", help="recorded under _meta only")
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)

    project = args.project_dir.resolve()
    bundle = build_bundle(
        project,
        args.target_dir or project / "target",
        args.db or project / "dev.duckdb",
        args.scenario,
    )
    write_bundle(bundle, args.out)
    print(f"{len(bundle['failures'])} failure(s) -> {args.out / 'bundle.json'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
