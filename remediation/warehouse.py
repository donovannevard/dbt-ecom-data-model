"""Which warehouse the remediation tooling builds on and reads from.

DuckDB by default (the profile's `dev` target). For the warehouse round, a
target such as `snowflake_prod`, `redshift_prod` or `bigquery_local`, with its
credentials from the matching .env file (`.env.snowflake` and so on):

    use("snowflake_prod", Path(".env.snowflake"))

Every dbt invocation (dbtrun.run_dbt) then carries `--target` and those
credentials, so fault injection, the validator and the CI loop all build on
that warehouse without each needing its own plumbing.

Reading data back (failing rows, source columns, duplicate-key rows) goes
through a Reader:

  DuckDBReader   straight from the DuckDB file, fast
  DbtShowReader  any warehouse, through `dbt show` and the same profile the
                 build used, in its own target path so the build's
                 run_results.json is never overwritten

SQL sent to a reader is kept portable: unquoted identifiers (double quotes
mean a string in BigQuery) and literals inlined by literal(), since `dbt show`
takes no bind parameters.
"""

import importlib.util
import json
import os
import subprocess
import tempfile
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path

from remediation.config import REPO_ROOT

DUCKDB_TARGETS = {None, "dev", "ci"}


@dataclass
class Warehouse:
    target: str | None = None
    env: dict = field(default_factory=dict)

    @property
    def is_duckdb(self) -> bool:
        return self.target in DUCKDB_TARGETS

    @property
    def label(self) -> str:
        return self.target or "dev (DuckDB)"

    def dbt_args(self) -> list[str]:
        return ["--target", self.target] if self.target else []


CURRENT = Warehouse()


def load_env_file(path: Path) -> dict:
    """Parse a .env file with the project's own literal parser
    (scripts/env_file.py), so a password with \\ $ or # survives intact."""
    spec = importlib.util.spec_from_file_location("env_file", REPO_ROOT / "scripts" / "env_file.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.parse(path)


def use(target: str | None, env_file: Path | None = None) -> Warehouse:
    """Select the warehouse for the rest of this process."""
    env = load_env_file(env_file) if env_file and env_file.exists() else {}
    if target and target not in DUCKDB_TARGETS and env_file and not env_file.exists():
        raise SystemExit(f"{env_file} not found: the {target} credentials live there")
    CURRENT.target, CURRENT.env = (None if target == "dev" else target), env
    return CURRENT


def subprocess_env(project: Path) -> dict:
    """Environment for a dbt subprocess: credentials from the warehouse's .env
    file, never overriding a variable already set (as scripts/env_file.py)."""
    # Absolute: dbt runs with cwd=project, so a relative path would be
    # resolved from inside itself and not exist.
    env = {**os.environ, "DBT_PROFILES_DIR": str(Path(project).resolve())}
    for key, value in CURRENT.env.items():
        if not env.get(key):
            env[key] = value
    return env


class ReadError(RuntimeError):
    pass


def _jsonable(value):
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, Decimal):
        return float(value)
    return str(value)


def literal(value) -> str:
    """A SQL literal valid on DuckDB, Snowflake, Redshift and BigQuery."""
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, (int, float)):
        return repr(value)
    return "'" + str(value).replace("'", "''") + "'"


class DuckDBReader:
    def __init__(self, db_path: Path):
        self.db_path = db_path

    def query(self, sql: str, limit: int) -> tuple[list[str], list[dict]]:
        import duckdb

        try:
            with duckdb.connect(str(self.db_path), read_only=True) as con:
                cursor = con.execute(f"SELECT * FROM ({sql}) AS q LIMIT {int(limit)}")
                columns = [d[0] for d in cursor.description]
                return columns, [dict(zip(columns, map(_jsonable, row))) for row in cursor.fetchall()]
        except duckdb.Error as exc:
            raise ReadError(str(exc).splitlines()[0]) from None

    def columns(self, relation: str) -> list[dict]:
        import duckdb

        try:
            with duckdb.connect(str(self.db_path), read_only=True) as con:
                return [{"name": r[0], "type": r[1]} for r in con.execute(f"DESCRIBE {relation}").fetchall()]
        except duckdb.Error as exc:
            raise ReadError(str(exc).splitlines()[0]) from None


class DbtShowReader:
    """Reads through `dbt show` on the current target: works on any adapter,
    at the cost of a few seconds per query on a cloud warehouse."""

    def __init__(self, project_dir: Path, warehouse: Warehouse = CURRENT):
        self.project_dir, self.warehouse = Path(project_dir).resolve(), warehouse

    def query(self, sql: str, limit: int) -> tuple[list[str], list[dict]]:
        from remediation.dbtrun import dbt_executable

        with tempfile.TemporaryDirectory(prefix="dbt-show-") as tmp:
            # The virtualenv's dbt, never a bare `dbt`: make doesn't activate
            # the venv, so `dbt` isn't on PATH there.
            cmd = [dbt_executable(), "show", "--inline", sql, "--limit", str(int(limit)), "--output", "json", "-q",
                   "--target-path", tmp, *self.warehouse.dbt_args()]
            result = subprocess.run(cmd, cwd=self.project_dir, env=subprocess_env(self.project_dir),
                                    capture_output=True, text=True)
        if result.returncode != 0 or "{" not in result.stdout:
            detail = (result.stdout + result.stderr).strip().splitlines()
            raise ReadError(next((line for line in detail if "rror" in line), detail[-1] if detail else "dbt show failed"))
        rows = json.loads(result.stdout[result.stdout.index("{"):])["show"]
        return (list(rows[0]) if rows else []), rows

    def columns(self, relation: str) -> list[dict]:
        # No portable DESCRIBE: names from one row; types aren't needed for a
        # diagnosis, the names are what a staging fix gets wrong.
        names, _ = self.query(f"select * from {relation}", 1)
        return [{"name": n, "type": None} for n in names]


def reader_for(project_dir: Path, db_path: Path | None):
    """The right reader for the current warehouse, or None if there's no way
    to read data back (DuckDB with no database file, e.g. CI artifacts)."""
    if not CURRENT.is_duckdb or os.environ.get("REMEDIATION_PORTABLE_READS") == "1":
        return DbtShowReader(project_dir)
    return DuckDBReader(db_path) if db_path and db_path.exists() else None
