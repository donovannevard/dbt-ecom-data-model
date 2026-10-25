"""Look inside a scenario's databases: clean, broken and fixed, side by side.

Each chaos workspace (.chaos/<scenario>/) keeps point-in-time snapshots:

    clean.duckdb    the project built with no fault
    broken.duckdb   the same project with the scenario's fault injected
    fixed.duckdb    after `chaos fix` (Claude's edits) or `chaos rebuild` (yours)

The snapshots are "frozen": dbt-duckdb creates staging models as views whose
SQL names the `dev` catalog, so they break as soon as a database is attached
under any other name. freeze() materialises every view as a table, which makes
each snapshot self-contained and lets all three open together.

    python -m remediation.explore changes <scenario>                 # clean vs broken
    python -m remediation.explore changes <scenario> --to fixed      # clean vs fixed
    python -m remediation.explore diff <scenario> extract.stripe_payments
    python -m remediation.explore shell <scenario>                   # duckdb CLI
    python -m remediation.explore ui <scenario>                      # DuckDB UI in the browser
    python -m remediation.explore duckdb-ui     # `make duckdb-ui`: your build + every scenario
"""

import argparse
import os
import shutil
import sys
from pathlib import Path

import duckdb

from remediation.config import REPO_ROOT

CHAOS_DIR = REPO_ROOT / ".chaos"
SNAPSHOTS = ("clean", "broken", "fixed")
# Store-failures tables differ whenever a test fails; the bundle already has
# them. Shown with --all.
NOISY_SCHEMAS = ("dbt_test__audit",)
# Build-time metadata (snapshot validity timestamps, scd ids) differs between
# any two builds, so it is ignored when comparing rows.
IGNORED_COLUMN_PREFIX = "dbt_"


def freeze(src: Path, dest: Path) -> None:
    """Copy a dbt-duckdb database and materialise its views as tables."""
    shutil.copy2(src, dest)
    # Attached as `dev` so the views' fully qualified references resolve.
    with duckdb.connect() as con:
        con.execute(f"ATTACH '{dest}' AS dev")
        views = con.execute(
            "SELECT schema_name, view_name FROM duckdb_views() "
            "WHERE database_name = 'dev' AND NOT internal"
        ).fetchall()
        # Materialise everything before dropping anything, so a view that
        # reads another view still resolves.
        for schema, view in views:
            con.execute(f'CREATE TABLE dev."{schema}"."{view}__frozen" AS SELECT * FROM dev."{schema}"."{view}"')
        for schema, view in views:
            con.execute(f'DROP VIEW dev."{schema}"."{view}"')
            con.execute(f'ALTER TABLE dev."{schema}"."{view}__frozen" RENAME TO "{view}"')
        con.execute("DETACH dev")


def workspace(scenario: str, root: Path = CHAOS_DIR) -> Path:
    ws = root / scenario
    if not (ws / "broken.duckdb").exists():
        raise SystemExit(f"no workspace for {scenario!r}: run `make chaos SCENARIO={scenario}` first")
    return ws


def snapshot(ws: Path, name: str) -> Path:
    path = ws / f"{name}.duckdb"
    if not path.exists():
        hint = " — run `make chaos-fix` or `make chaos-rebuild` first" if name == "fixed" else ""
        raise SystemExit(f"{path.relative_to(REPO_ROOT) if REPO_ROOT in path.parents else path} not found{hint}")
    return path


def _attach(con, path: Path, alias: str) -> None:
    con.execute(f"ATTACH '{path}' AS {alias} (READ_ONLY)")


def _tables(con, alias: str) -> dict[str, list[str]]:
    rows = con.execute(
        "SELECT schema_name, table_name, column_name FROM duckdb_columns() "
        "WHERE database_name = ? ORDER BY schema_name, table_name, column_index", [alias]
    ).fetchall()
    tables: dict[str, list[str]] = {}
    for schema, table, column in rows:
        tables.setdefault(f"{schema}.{table}", []).append(column)
    return tables


def _comparable(cols_a: list[str], cols_b: list[str]) -> list[str]:
    return [c for c in cols_a if c in cols_b and not c.startswith(IGNORED_COLUMN_PREFIX)]


def _quoted(table: str) -> str:
    schema, name = table.split(".", 1)
    return f'"{schema}"."{name}"'


def compare(path_a: Path, path_b: Path, include_noisy: bool = False) -> list[dict]:
    """Per table: row counts and rows present in one side but not the other.

    EXCEPT ALL, not EXCEPT, so a duplicated row counts as a difference.
    """
    out = []
    with duckdb.connect() as con:
        _attach(con, path_a, "a")
        _attach(con, path_b, "b")
        ta, tb = _tables(con, "a"), _tables(con, "b")
        for table in sorted(set(ta) | set(tb)):
            if not include_noisy and table.split(".")[0] in NOISY_SCHEMAS:
                continue
            entry = {"table": table}
            if table not in tb or table not in ta:
                entry["status"] = "only_in_a" if table in ta else "only_in_b"
                out.append(entry)
                continue
            q = _quoted(table)
            cols = ", ".join(f'"{c}"' for c in _comparable(ta[table], tb[table]))
            entry["rows_a"] = con.execute(f"SELECT count(*) FROM a.{q}").fetchone()[0]
            entry["rows_b"] = con.execute(f"SELECT count(*) FROM b.{q}").fetchone()[0]
            try:
                entry["only_a"] = con.execute(
                    f"SELECT count(*) FROM (SELECT {cols} FROM a.{q} EXCEPT ALL SELECT {cols} FROM b.{q})").fetchone()[0]
                entry["only_b"] = con.execute(
                    f"SELECT count(*) FROM (SELECT {cols} FROM b.{q} EXCEPT ALL SELECT {cols} FROM a.{q})").fetchone()[0]
            except duckdb.Error as exc:
                entry["status"] = f"incomparable: {exc}".splitlines()[0]
                out.append(entry)
                continue
            if set(ta[table]) != set(tb[table]):
                entry["columns_changed"] = sorted(set(ta[table]) ^ set(tb[table]))
            changed = entry["only_a"] or entry["only_b"] or entry.get("columns_changed")
            entry["status"] = "changed" if changed else "same"
            out.append(entry)
    return out


def diff_rows(path_a: Path, path_b: Path, table: str, limit: int = 20) -> tuple[list[str], list, list]:
    with duckdb.connect() as con:
        _attach(con, path_a, "a")
        _attach(con, path_b, "b")
        ta, tb = _tables(con, "a"), _tables(con, "b")
        if table not in ta or table not in tb:
            raise SystemExit(f"{table} is not in both snapshots")
        cols = _comparable(ta[table], tb[table])
        sel, q = ", ".join(f'"{c}"' for c in cols), _quoted(table)
        only_a = con.execute(f"SELECT {sel} FROM a.{q} EXCEPT ALL SELECT {sel} FROM b.{q} LIMIT {int(limit)}").fetchall()
        only_b = con.execute(f"SELECT {sel} FROM b.{q} EXCEPT ALL SELECT {sel} FROM a.{q} LIMIT {int(limit)}").fetchall()
    return cols, only_a, only_b


def render_changes(results: list[dict], a: str, b: str) -> str:
    changed = [r for r in results if r["status"] != "same"]
    lines = [f"{len(changed)} of {len(results)} tables differ between {a} and {b}", ""]
    if not changed:
        return "\n".join(lines)
    width = max(len(r["table"]) for r in changed)
    lines.append(f"{'table':<{width}}  {a + ' rows':>11}  {b + ' rows':>11}  {'only ' + a:>11}  {'only ' + b:>11}")
    for r in changed:
        if "rows_a" not in r:
            side = a if r["status"] == "only_in_a" else b
            lines.append(f"{r['table']:<{width}}  only in {side}: not built in the other "
                         "(dbt skips models downstream of a failure)")
            continue
        extra = f"  columns: {', '.join(r['columns_changed'])}" if r.get("columns_changed") else ""
        extra += f"  {r['status']}" if r["status"].startswith("incomparable") else ""
        lines.append(f"{r['table']:<{width}}  {r['rows_a']:>11}  {r['rows_b']:>11}  "
                     f"{r.get('only_a', '?'):>11}  {r.get('only_b', '?'):>11}{extra}")
    return "\n".join(lines)


def _print_rows(title: str, cols: list[str], rows: list) -> None:
    print(f"\n{title} ({len(rows)} shown)")
    if not rows:
        return
    widths = [max(len(c), *(len(str(r[i])) for r in rows)) for i, c in enumerate(cols)]
    widths = [min(w, 40) for w in widths]
    print("  ".join(c[:w].ljust(w) for c, w in zip(cols, widths)))
    for r in rows:
        print("  ".join(str(v)[:w].ljust(w) for v, w in zip(r, widths)))


def duckdb_command(ws: Path, ui: bool) -> list[str]:
    attach = [f"ATTACH '{ws / f'{n}.duckdb'}' AS {n} (READ_ONLY);" for n in SNAPSHOTS
              if (ws / f"{n}.duckdb").exists()]
    cmd = ["duckdb", "-cmd", " ".join(attach + ["USE broken;"])]
    return cmd + ["-ui"] if ui else cmd


UI_SNAPSHOT_DIR = REPO_ROOT / "target" / "duckdb-ui"


def snapshot_dev(dev: Path, dest_dir: Path = UI_SNAPSHOT_DIR) -> Path:
    """Copy dev.duckdb (and any pending WAL) so the UI never holds a lock on
    the file dbt writes to. Attached as `dev`, so views resolve unfrozen."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / "dev.duckdb"
    (dest_dir / "dev.duckdb.wal").unlink(missing_ok=True)
    shutil.copy2(dev, dest)
    wal = dev.with_name(dev.name + ".wal")
    if wal.exists():
        shutil.copy2(wal, dest_dir / "dev.duckdb.wal")
    return dest


def warehouse_attachments(dev_snapshot: Path | None, root: Path = CHAOS_DIR) -> list[tuple[str, Path]]:
    """(alias, path) for the local build and every fault-injection snapshot."""
    out = [("dev", dev_snapshot)] if dev_snapshot else []
    baseline = root / "_baseline" / "clean.duckdb"
    if baseline.exists():
        out.append(("clean", baseline))
    if root.is_dir():
        for ws in sorted(p for p in root.iterdir() if p.is_dir() and not p.name.startswith("_")):
            for name in ("broken", "fixed"):
                if (ws / f"{name}.duckdb").exists():
                    out.append((f"{ws.name}_{name}", ws / f"{name}.duckdb"))
    return out


def warehouse_ui_command(attachments: list[tuple[str, Path]]) -> list[str]:
    sql = [f"ATTACH '{path}' AS \"{alias}\" (READ_ONLY);" for alias, path in attachments]
    sql.append(f'USE "{attachments[0][0]}";')
    return ["duckdb", "-cmd", " ".join(sql), "-ui"]


def launch_warehouse_ui(root: Path) -> None:
    if not shutil.which("duckdb"):
        raise SystemExit("duckdb CLI not found: brew install duckdb")
    dev = REPO_ROOT / "dev.duckdb"
    snap = snapshot_dev(dev) if dev.exists() else None
    attachments = warehouse_attachments(snap, root)
    if not attachments:
        raise SystemExit("nothing to show: run `make build` (or `make chaos SCENARIO=...`) first")
    if not snap:
        print("No dev.duckdb yet (run `make build`); showing fault-injection databases only.")
    print("Attached read-only:")
    for alias, _ in attachments:
        what = {"dev": "snapshot of your local build, taken now",
                "clean": "fault-injection baseline (no fault)"}.get(alias, "fault-injection scenario")
        print(f"  {alias:<44} {what}")
    print("\nUI at http://localhost:4213 — leave this terminal open; .quit here stops it.\n"
          "Run `make duckdb-ui` again after a build to refresh the dev snapshot.")
    sys.stdout.flush()
    cmd = warehouse_ui_command(attachments)
    os.execvp(cmd[0], cmd)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--root", type=Path, default=CHAOS_DIR)
    sub = parser.add_subparsers(dest="command", required=True)
    ch = sub.add_parser("changes", help="tables that differ between two snapshots")
    df = sub.add_parser("diff", help="rows that differ in one table")
    for p in (ch, df):
        p.add_argument("scenario")
        p.add_argument("--from", dest="a", choices=SNAPSHOTS, default="clean")
        p.add_argument("--to", dest="b", choices=SNAPSHOTS, default="broken")
    df.add_argument("table", help="schema.table, e.g. extract.stripe_payments or main.fct_transactions")
    df.add_argument("--limit", type=int, default=20)
    ch.add_argument("--all", action="store_true", help=f"include {', '.join(NOISY_SCHEMAS)}")
    for name in ("shell", "ui"):
        sub.add_parser(name, help="duckdb with every snapshot attached").add_argument("scenario")
    sub.add_parser("duckdb-ui", help="DuckDB UI on your local build plus every scenario snapshot")
    args = parser.parse_args(argv)

    if args.command == "duckdb-ui":
        launch_warehouse_ui(args.root)
        return 0
    ws = workspace(args.scenario, args.root)
    if args.command == "changes":
        results = compare(snapshot(ws, args.a), snapshot(ws, args.b), include_noisy=args.all)
        print(render_changes(results, args.a, args.b))
    elif args.command == "diff":
        cols, only_a, only_b = diff_rows(snapshot(ws, args.a), snapshot(ws, args.b), args.table, args.limit)
        print(f"{args.table}: {args.a} vs {args.b} (ignoring {IGNORED_COLUMN_PREFIX}* columns)")
        _print_rows(f"Only in {args.a}", cols, only_a)
        _print_rows(f"Only in {args.b}", cols, only_b)
    else:
        if not shutil.which("duckdb"):
            raise SystemExit("duckdb CLI not found: brew install duckdb")
        cmd = duckdb_command(ws, ui=args.command == "ui")
        attached = [n for n in SNAPSHOTS if (ws / f"{n}.duckdb").exists()]
        print(f"Attached read-only: {', '.join(attached)} (default: broken). "
              f"Query e.g. SELECT * FROM clean.main.fct_transactions")
        if args.command == "ui":
            print("The UI opens in your browser; Ctrl+C here to stop it.")
        sys.stdout.flush()
        os.execvp(cmd[0], cmd)
    return 0


if __name__ == "__main__":
    sys.exit(main())
