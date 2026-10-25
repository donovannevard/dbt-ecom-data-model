"""Alert on quarantined source records, and keep one issue per source current.

deduplicate_source() holds conflicting versions of a record out of staging,
and the warn-severity no_conflicting_duplicates test stores them. A warning
in a build log is easy to miss, so after every build this turns the
quarantine into something that notifies a person and tracks the fix:

  conflicts in a source, no open issue   open an issue: the versions, the
                                         downstream models now missing that
                                         data, and the runbook
  conflicts, issue already open          refresh it; comment only if the set
                                         of quarantined keys changed, so a
                                         daily build doesn't spam it
  no conflicts any more, issue open      close it: resolved at source

GitHub's notifications are the alert. If REMEDIATION_WEBHOOK_URL is set (a
Slack-style incoming webhook), each change is posted there too.

    python -m remediation.quarantine report                # what's quarantined now
    python -m remediation.quarantine sync                  # local stand-in tracker
    python -m remediation.quarantine sync --github         # real issues (needs gh auth)
"""

import argparse
import hashlib
import json
import os
import sys
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

from remediation.config import REPO_ROOT, load_manifest, load_policy
from remediation.diagnose import REDACTED, _jsonable, pii_columns
from remediation.github import GitHubTracker, LocalTracker, marker, read_marker

CONFLICT_TEST = "no_conflicting_duplicates"
DUPLICATE_TEST = "unique_combination_of_columns"
LABEL = "quarantine"
MAX_ROWS = 50


@dataclass
class Entry:
    source: str          # e.g. extract.stripe_payments
    source_uid: str
    kind: str            # conflict | duplicate
    status: str          # pass | warn | fail | error
    failures: int
    relation: str | None
    key_columns: list[str]
    rows: list[dict] = field(default_factory=list)

    @property
    def flagged(self) -> bool:
        return self.status in ("warn", "fail", "error") and self.failures > 0


def collect(target_dir: Path, manifest: dict) -> list[Entry]:
    """Every duplicate and conflict check in the last run, flagged or not:
    a passing conflict check is what closes an issue."""
    results = json.loads((target_dir / "run_results.json").read_text())["results"]
    entries = []
    for r in results:
        node = manifest["nodes"].get(r["unique_id"], {})
        meta = node.get("test_metadata") or {}
        kind = {CONFLICT_TEST: "conflict", DUPLICATE_TEST: "duplicate"}.get(meta.get("name"))
        source_uid = next((d for d in node.get("depends_on", {}).get("nodes", []) if d.startswith("source.")), None)
        if not kind or not source_uid:
            continue
        src = manifest["sources"][source_uid]
        kwargs = meta.get("kwargs") or {}
        entries.append(Entry(
            source=f"{src['source_name']}.{src['name']}", source_uid=source_uid, kind=kind,
            status=r["status"], failures=r.get("failures") or 0,
            relation=r.get("relation_name") or node.get("relation_name"),
            key_columns=list(kwargs.get("key_columns") or kwargs.get("combination_of_columns") or []),
        ))
    return sorted(entries, key=lambda e: (e.source, e.kind))


# -- reading the quarantined rows ------------------------------------------------

class DuckDBRows:
    def __init__(self, db_path: Path):
        self.db_path = db_path

    def __call__(self, relation: str, limit: int) -> list[dict]:
        import duckdb

        # Attached under the catalog name the relation uses, so a frozen
        # snapshot under any filename resolves the same as the live file.
        catalog = relation.split(".")[0].strip('"')
        with duckdb.connect() as con:
            con.execute(f"ATTACH '{self.db_path}' AS \"{catalog}\" (READ_ONLY)")
            cursor = con.execute(f"SELECT * FROM {relation} LIMIT {int(limit)}")
            cols = [d[0] for d in cursor.description]
            return [dict(zip(cols, map(_jsonable, row))) for row in cursor.fetchall()]


class DbtShowRows:
    """Any warehouse, through the shared target-aware reader (warehouse.py):
    the same profile, target and credentials the build used."""

    def __init__(self, project_dir: Path):
        from remediation.warehouse import DbtShowReader

        self.reader = DbtShowReader(project_dir)

    def __call__(self, relation: str, limit: int) -> list[dict]:
        return self.reader.query(f"select * from {relation}", limit)[1]


def load_rows(entries: list[Entry], read, pii: set[str]) -> None:
    for e in entries:
        if not (e.flagged and e.kind == "conflict" and e.relation):
            continue
        try:
            rows = read(e.relation, MAX_ROWS)
        except Exception as exc:  # a missing table must not stop the alert itself
            rows = [{"error": f"could not read {e.relation}: {exc}"}]
        e.rows = [{k: (REDACTED if k.lower() in pii and v is not None else v) for k, v in r.items()} for r in rows]


# -- rendering ----------------------------------------------------------------------

def _get(row: dict, column: str):
    """Case-insensitive: Snowflake returns unquoted column names upper-cased
    (ID, not id), DuckDB, BigQuery and Redshift lower-cased."""
    if column in row:
        return row[column]
    return next((v for k, v in row.items() if k.lower() == column.lower()), None)


def _keys(e: Entry) -> list[str]:
    return sorted({" / ".join(str(_get(r, k)) for k in e.key_columns) for r in e.rows if "error" not in r})


def keys_fingerprint(e: Entry) -> str:
    return hashlib.sha256("\n".join(_keys(e)).encode()).hexdigest()[:12]


def downstream_models(manifest: dict, uid: str) -> list[str]:
    seen, stack, out = {uid}, [uid], []
    while stack:
        for child in manifest["child_map"].get(stack.pop(), []):
            if child not in seen and child.split(".")[0] in ("model", "snapshot"):
                seen.add(child)
                stack.append(child)
                out.append(child.split(".")[-1])
    return sorted(out)


def _table(rows: list[dict]) -> list[str]:
    if not rows:
        return ["_(no rows readable)_"]
    cols = list(rows[0])
    lines = ["| " + " | ".join(cols) + " |", "|" + "---|" * len(cols)]
    lines += ["| " + " | ".join(str(r.get(c, "")).replace("|", "\\|") for c in cols) + " |" for r in rows]
    return lines


def render_issue(e: Entry, manifest: dict, duplicate: Entry | None, run_label: str) -> tuple[str, str]:
    keys = _keys(e)
    title = f"Quarantine: {e.source} — {len(keys)} conflicting key{'s' if len(keys) != 1 else ''}"
    affected = downstream_models(manifest, e.source_uid)
    table = e.relation.split(".")[-1].strip('"') if e.relation else "?"
    lines = [
        marker("quarantine", e.source), marker("quarantine-keys", keys_fingerprint(e)), "",
        f"**{len(keys)} key(s) in `{e.source}` have conflicting versions** (key: "
        f"`{', '.join(e.key_columns)}`), so `deduplicate_source()` is holding them out of staging. "
        "The build is still running; these records are simply missing from everything downstream "
        "until the conflict is resolved at source.",
        "", f"Last seen: {run_label}", "",
        "### The conflicting versions", "", *_table(e.rows[:MAX_ROWS]), "",
        "Personal data is redacted. The full rows are in the warehouse:", "",
        f"```sql\nselect * from {e.relation};\n```", "",
        "### What's missing downstream", "",
        "These records are absent from: " + ", ".join(f"`{m}`" for m in affected) + ".", "",
    ]
    if duplicate and duplicate.flagged:
        lines += [f"The source also has re-delivered copies ({duplicate.failures} duplicate key(s) in total, "
                  "identical ones included); those are collapsed automatically and need no action here.", ""]
    lines += [
        "### How to resolve", "",
        "1. **Investigate.** Compare the versions above: which columns differ, and when was each loaded "
        "(`_fivetran_synced`)? Locally, `make quarantine` lists everything quarantined and `make duckdb-ui` "
        f"lets you query `dbt_test__audit.{table}`.",
        "2. **Decide which version is right** with whoever owns the data (finance for payments, the "
        "application team for orders and customers). Neither version is used until then, deliberately.",
        "3. **Fix it at source.** Correct or remove the wrong record in the source system or landing "
        "table, then let the pipeline run. The key leaves quarantine on its own, and **this issue closes "
        "itself** on the first build that finds no conflicts.",
        "4. **If this source legitimately keeps several versions per key** (an append-only change log, "
        "say), that's a modelling change, not a data fix: give its staging model explicit latest-version "
        "semantics in a reviewed PR. Never weaken or remove the duplicate checks to make this go away.",
        "", "<sub>Managed by `remediation/quarantine.py`: refreshed on every build, closed automatically "
        "when resolved. Edit the title freely; the hidden marker keeps track.</sub>",
    ]
    return title, "\n".join(lines)


def render_report(entries: list[Entry]) -> str:
    conflicts = [e for e in entries if e.kind == "conflict" and e.flagged]
    duplicates = [e for e in entries if e.kind == "duplicate" and e.flagged]
    lines = ["## Quarantine", ""]
    if not conflicts:
        lines.append("Nothing quarantined: no source has conflicting versions of a record.")
    for e in conflicts:
        lines += [f"### {e.source}: {len(_keys(e))} conflicting key(s)", "", *_table(e.rows), ""]
    if duplicates:
        lines += ["", "Re-delivered copies, collapsed automatically (tell the upstream team):"]
        lines += [f"- `{e.source}`: {e.failures} duplicate key(s)" for e in duplicates]
    return "\n".join(lines) + "\n"


# -- sync ---------------------------------------------------------------------------

def notify(text: str) -> None:
    url = os.environ.get("REMEDIATION_WEBHOOK_URL")
    if not url:
        return
    req = urllib.request.Request(url, data=json.dumps({"text": text}).encode(),
                                 headers={"Content-Type": "application/json"})
    try:
        urllib.request.urlopen(req, timeout=10)
    except OSError as exc:  # an alert channel failing must not fail the build
        print(f"warning: webhook notification failed: {exc}", file=sys.stderr)


def sync(entries: list[Entry], tracker, manifest: dict, run_label: str) -> list[str]:
    actions = []
    by_kind = {(e.source, e.kind): e for e in entries}
    flagged = {e.source: e for e in entries if e.kind == "conflict" and e.flagged}
    resolved = {e.source for e in entries if e.kind == "conflict" and not e.flagged and e.status == "pass"}

    for source, e in flagged.items():
        title, body = render_issue(e, manifest, by_kind.get((source, "duplicate")), run_label)
        existing = tracker.find_open("issue", LABEL, "quarantine", source)
        if existing is None:
            item = tracker.create_issue(title, body, [LABEL, "data-issue"])
            actions.append(f"opened #{item['number']} for {source}")
            notify(f":rotating_light: {title}: {item['url']}")
        else:
            changed = read_marker(existing["body"], "quarantine-keys") != keys_fingerprint(e)
            tracker.update(existing["number"], title, body)
            if changed:
                tracker.comment(existing["number"], f"The quarantined keys changed in {run_label}: now {len(_keys(e))}.")
                notify(f"{title} (changed): {existing['url']}")
                actions.append(f"updated #{existing['number']} for {source} (keys changed)")
            else:
                actions.append(f"refreshed #{existing['number']} for {source}")

    for item in tracker.list_open("issue", LABEL):
        source = read_marker(item["body"], "quarantine")
        if source in resolved:
            tracker.close(item["number"], f"Resolved: no conflicting versions in `{source}` as of {run_label}. "
                                          "The records are back in staging and downstream.")
            notify(f":white_check_mark: Quarantine resolved for {source}: {item['url']}")
            actions.append(f"closed #{item['number']} for {source} (resolved)")
    return actions or ["nothing to do"]


def alert(project_dir: Path, target_dir: Path, *, github: bool = False, tracker_dir: Path | None = None,
          db: Path | None = None, run_label: str | None = None) -> list[str]:
    """Sync quarantine issues from a finished run. For orchestrators: call it
    after the build, with the target directory that run wrote to."""
    manifest = load_manifest(target_dir / "manifest.json")
    entries = collect(target_dir, manifest)
    load_rows(entries, _reader(project_dir, manifest, db), pii_columns(manifest, load_policy()))
    meta = json.loads((target_dir / "run_results.json").read_text())["metadata"]
    label = run_label or f"run {meta.get('invocation_id', '?')[:8]} ({meta.get('generated_at', '')[:19]})"
    if not github:
        return sync(entries, LocalTracker(tracker_dir or LocalTracker().directory), manifest, label)
    from remediation.ghapp import acting_as_app, repo_slug

    with acting_as_app(repo_slug(project_dir)):  # dbt-ecom-remediation[bot] when configured
        return sync(entries, GitHubTracker(project_dir), manifest, label)


def _reader(project_dir: Path, manifest: dict, db: Path | None):
    from remediation.warehouse import CURRENT

    db = db or project_dir / "dev.duckdb"
    if CURRENT.is_duckdb and manifest["metadata"].get("adapter_type") == "duckdb" and db.exists():
        return DuckDBRows(db)
    return DbtShowRows(project_dir)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("command", choices=["report", "sync"])
    parser.add_argument("--project-dir", type=Path, default=REPO_ROOT)
    parser.add_argument("--target-dir", type=Path, help="default: <project-dir>/target")
    parser.add_argument("--db", type=Path, help="DuckDB file to read rows from (default: dbt show)")
    parser.add_argument("--github", action="store_true", help="sync real GitHub issues (default: local stand-in)")
    parser.add_argument("--tracker-dir", type=Path, help="local stand-in directory")
    parser.add_argument("--run-label", default=None, help="how this run is named in issues")
    parser.add_argument("--target", default=None, help="dbt target the build ran on (default: DuckDB)")
    args = parser.parse_args(argv)

    from remediation.chaos import default_env_file
    from remediation.warehouse import use
    use(args.target, default_env_file(args.target))

    target = args.target_dir or args.project_dir / "target"
    if args.command == "report":
        manifest = load_manifest(target / "manifest.json")
        entries = collect(target, manifest)
        load_rows(entries, _reader(args.project_dir, manifest, args.db), pii_columns(manifest, load_policy()))
        print(render_report(entries))
        return 0
    for action in alert(args.project_dir, target, github=args.github, tracker_dir=args.tracker_dir,
                        db=args.db, run_label=args.run_label):
        print(action)
    if not args.github:
        print(f"(local stand-in: {args.tracker_dir or LocalTracker().directory})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
