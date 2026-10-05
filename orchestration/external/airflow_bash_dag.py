"""Reference Airflow DAG: run this dbt project with no extra dependencies.

The plain-operator route. Three BashOperator tasks invoking the dbt CLI, which
works on any Airflow version with no Cosmos and no provider packages beyond
the standard ones.

Trade-off against the Cosmos version in this directory: you get one task for
the entire dbt DAG rather than one task per model. A failure tells you "the
build failed" and you read the dbt logs to find out which model, instead of
seeing a red box on `fct_transactions`. Retries are all-or-nothing too.

That is an entirely reasonable trade if you have a handful of dbt projects and
want the orchestration layer to stay boring. Prefer the Cosmos version once
per-model visibility or selective retries start to matter.

This file is reference material: the repo does not depend on Airflow and CI
does not execute it. Parse-checked against Airflow 3.3.
"""

import os
from datetime import datetime, timedelta

from airflow.providers.standard.operators.bash import BashOperator
from airflow.sdk import DAG

DBT_PROJECT_DIR = os.environ.get("DBT_PROJECT_DIR", "/opt/airflow/dbt/dbt-ecom-data-model")
DBT_EXECUTABLE = os.environ.get("DBT_EXECUTABLE_PATH", "/usr/local/bin/dbt")
DBT_TARGET = os.environ.get("DBT_TARGET", "snowflake_prod")

# Run every dbt command from the project directory with a known profiles
# location, so nothing depends on the worker's working directory.
DBT_ENV = {
    "DBT_PROFILES_DIR": DBT_PROJECT_DIR,
    # dbt writes target/ and logs/; point them somewhere writable if the
    # project directory is mounted read-only, which is good practice.
    "DBT_LOG_PATH": os.environ.get("DBT_LOG_PATH", f"{DBT_PROJECT_DIR}/logs"),
}


def dbt_command(command: str) -> str:
    return f"cd {DBT_PROJECT_DIR} && {DBT_EXECUTABLE} {command} --target {DBT_TARGET}"


with DAG(
    dag_id="dbt_ecom_data_model_bash",
    description="Build the e-commerce warehouse with plain dbt CLI calls.",
    schedule="0 6 * * *",
    start_date=datetime(2026, 1, 1),
    catchup=False,
    max_active_runs=1,
    default_args={"retries": 1, "retry_delay": timedelta(minutes=5)},
    tags=["dbt", "ecommerce"],
) as dag:
    # Installs dbt packages (dbt_utils). Safe to run every time and cheap;
    # bake it into your image instead if you would rather not hit the network
    # on every run.
    deps = BashOperator(
        task_id="dbt_deps",
        bash_command=f"cd {DBT_PROJECT_DIR} && {DBT_EXECUTABLE} deps",
        env=DBT_ENV,
        append_env=True,
    )

    # The demo dataset that stands in for ELT-landed raw tables. Staging models
    # reach it through source(), so dbt has no DAG edge from these seeds to the
    # models that read them -- a single `dbt build` on an empty warehouse would
    # race them. Loading them as a separate upstream task is the fix.
    #
    # Against real client data: delete this task. The raw tables come from your
    # ELT tool, and this DAG should instead wait on that tool's completion.
    seed_demo_data = BashOperator(
        task_id="load_raw_landing_tables",
        bash_command=dbt_command("seed --select path:seeds/example"),
        env=DBT_ENV,
        append_env=True,
    )

    # `build`, not `run`: seeds, snapshots, models and tests in DAG order, so a
    # failing test stops its downstream rather than being found afterwards.
    build = BashOperator(
        task_id="dbt_build",
        bash_command=dbt_command("build"),
        env=DBT_ENV,
        append_env=True,
    )

    deps >> seed_demo_data >> build
