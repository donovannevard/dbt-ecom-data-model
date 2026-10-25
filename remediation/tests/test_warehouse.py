from pathlib import Path

import pytest

from remediation import warehouse as w


@pytest.fixture(autouse=True)
def reset():
    yield
    w.CURRENT.target, w.CURRENT.env = None, {}


@pytest.mark.parametrize("value, sql", [(None, "NULL"), (True, "TRUE"), (3, "3"), (1.5, "1.5"),
                                        ("pi_1", "'pi_1'"), ("O'Brien", "'O''Brien'")])
def test_literals_are_portable(value, sql):
    assert w.literal(value) == sql


def test_default_is_duckdb(tmp_path):
    w.use(None)
    assert w.CURRENT.is_duckdb and w.CURRENT.dbt_args() == []
    assert isinstance(w.reader_for(tmp_path, tmp_path / "dev.duckdb"), type(None))  # no file: no reader
    (tmp_path / "dev.duckdb").touch()
    assert isinstance(w.reader_for(tmp_path, tmp_path / "dev.duckdb"), w.DuckDBReader)


def test_a_cloud_target_reads_through_dbt_show_with_its_credentials(tmp_path, monkeypatch):
    env = tmp_path / ".env.snowflake"
    env.write_text("SNOWFLAKE_ACCOUNT=org-acct\nDB_TRANSFORM_PASSWORD='p$ss\\word#1'\n")
    w.use("snowflake_prod", env)
    assert not w.CURRENT.is_duckdb and w.CURRENT.dbt_args() == ["--target", "snowflake_prod"]
    assert isinstance(w.reader_for(tmp_path, None), w.DbtShowReader)
    monkeypatch.delenv("DB_TRANSFORM_PASSWORD", raising=False)
    monkeypatch.setenv("SNOWFLAKE_ACCOUNT", "from-shell")
    env_vars = w.subprocess_env(tmp_path)
    assert env_vars["DB_TRANSFORM_PASSWORD"] == "p$ss\\word#1"  # literal, as scripts/env_file.py
    assert env_vars["SNOWFLAKE_ACCOUNT"] == "from-shell"        # the shell wins
    assert env_vars["DBT_PROFILES_DIR"] == str(tmp_path)


def test_a_cloud_target_without_its_env_file_stops(tmp_path):
    with pytest.raises(SystemExit, match="credentials"):
        w.use("redshift_prod", tmp_path / ".env.redshift")


def test_env_file_follows_the_target(monkeypatch):
    from remediation.chaos import default_env_file
    from remediation.config import REPO_ROOT

    monkeypatch.setenv("ENV_FILE", ".env")  # the Makefile's default
    assert default_env_file("snowflake_prod") == REPO_ROOT / ".env.snowflake"
    assert default_env_file("bigquery_local") == REPO_ROOT / ".env.bigquery"
    assert default_env_file("dev") is None and default_env_file(None) is None
    monkeypatch.setenv("ENV_FILE", ".env.custom")  # an explicit choice wins
    assert default_env_file("snowflake_prod") == REPO_ROOT / ".env.custom"


def test_dbt_show_reader_matches_duckdb_on_the_real_build(monkeypatch):
    # The portable path, end to end, against the local DuckDB build. PATH is
    # stripped to what `make` gives a non-activated shell: an early version
    # ran a bare `dbt`, which passed here and failed for the user.
    from remediation.config import REPO_ROOT

    monkeypatch.setenv("PATH", "/usr/bin:/bin")

    if not (REPO_ROOT / "dev.duckdb").exists():
        pytest.skip("no local build")
    relation = '"dev"."extract"."stripe_payments"'
    sql = f"select id, amount from {relation} order by id"
    a = w.DuckDBReader(REPO_ROOT / "dev.duckdb").query(sql, 3)
    # A relative project path, as typed on the command line, must work too.
    import os
    monkeypatch.chdir(REPO_ROOT.parent)
    b = w.DbtShowReader(Path(REPO_ROOT.name)).query(sql, 3)
    assert a[0] == b[0] == ["id", "amount"] and [r["id"] for r in a[1]] == [r["id"] for r in b[1]]


def test_tooling_changes_dont_invalidate_the_baseline():
    from remediation import chaos

    assert not any(r.startswith(chaos.DBT_INPUTS) for r in ["remediation/chaos.py", "README.md", "Makefile"])
    assert all(r.startswith(chaos.DBT_INPUTS) for r in ["models/staging/prod/stg_orders.sql", "macros/x.sql",
                                                         "seeds/example/prod/prod_orders.csv", "dbt_project.yml"])
