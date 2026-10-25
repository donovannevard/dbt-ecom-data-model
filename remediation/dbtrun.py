"""Run dbt inside a project copy (a chaos workspace or a validator sandbox)."""

import json
import shutil
import subprocess
import sys
from pathlib import Path

PASSING = {"pass", "success"}


def dbt_executable() -> str:
    beside = Path(sys.executable).parent / "dbt"
    return str(beside) if beside.exists() else (shutil.which("dbt") or "dbt")


def run_dbt(project: Path, log, *args: str) -> int:
    """dbt on the current warehouse (remediation.warehouse): its --target and
    credentials are added to every call."""
    from remediation.warehouse import CURRENT, subprocess_env

    full = [*args, *CURRENT.dbt_args()]
    log.write(f"\n$ dbt {' '.join(full)}\n")
    log.flush()
    return subprocess.run(
        [dbt_executable(), *full], cwd=project, env=subprocess_env(project), stdout=log, stderr=subprocess.STDOUT
    ).returncode


def clear_build(project: Path) -> None:
    for stale in ("target", "dev.duckdb", "dev.duckdb.wal"):
        path = project / stale
        shutil.rmtree(path) if path.is_dir() else path.unlink(missing_ok=True)


def ensure_deps(project: Path, log) -> None:
    if not (project / "dbt_packages").is_dir() and run_dbt(project, log, "deps"):
        raise RuntimeError("dbt deps failed")


def build(project: Path, log) -> None:
    """deps (if needed), seed, build. Exit codes are not errors here — a
    broken build is often the point — but missing artifacts are.

    --full-refresh, because on a shared warehouse consecutive builds (clean,
    broken, patched) write to the same schemas: seeds are recreated, so a
    faulty seed's column types can't stick, and the incremental model is
    rebuilt rather than merged into another scenario's rows."""
    clear_build(project)
    ensure_deps(project, log)
    run_dbt(project, log, "seed", "--full-refresh")
    run_dbt(project, log, "build", "--full-refresh")
    if not (project / "target" / "run_results.json").exists():
        raise RuntimeError(f"dbt produced no run_results.json; see {log.name}")


def statuses(target: Path) -> dict[str, str]:
    results = json.loads((target / "run_results.json").read_text())["results"]
    return {r["unique_id"]: r["status"] for r in results}


def log_tail(path: Path, lines: int = 12) -> str:
    """The last error-looking lines of a dbt log, for rejection messages."""
    text = path.read_text().splitlines() if path.exists() else []
    hits = [i for i, line in enumerate(text) if "Error" in line or "error" in line]
    start = max(hits[0] - 1, 0) if hits else max(len(text) - lines, 0)
    return "\n".join(line.strip() for line in text[start:start + lines] if line.strip())
