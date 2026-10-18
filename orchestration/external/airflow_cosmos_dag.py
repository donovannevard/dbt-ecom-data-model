"""Reference Airflow DAG: run this dbt project with astronomer-cosmos.

Cosmos reads the dbt `manifest.json` and renders one Airflow task per dbt
model, so the Airflow graph mirrors the dbt DAG. Nothing in the dbt project
needs to change — that is the point of keeping orchestration out of the models.

This file is reference material. It lives in the dbt repo so the integration
contract is documented next to the thing being integrated, but the repo does
not depend on Airflow and CI does not execute this. It is parse-checked
against Airflow 3.3 / Cosmos 1.15 only.

Install into your Airflow environment:
    pip install astronomer-cosmos dbt-duckdb     # or dbt-snowflake / dbt-redshift

Then either copy this file into your Airflow `dags/` folder, or add this repo
as a submodule / pip-installable path and point DBT_PROJECT_DIR at it.
"""

import os
from datetime import datetime
from pathlib import Path

from airflow.providers.standard.operators.bash import BashOperator
from airflow.sdk import DAG
from cosmos import (
    DbtTaskGroup,
    ExecutionConfig,
    ExecutionMode,
    LoadMode,
    ProfileConfig,
    ProjectConfig,
    RenderConfig,
    TestBehavior,
)

# Where the dbt project lives inside your Airflow image. Override with an env
# var rather than editing this file.
DBT_PROJECT_DIR = Path(os.environ.get("DBT_PROJECT_DIR", "/opt/airflow/dbt/dbt-ecom-data-model"))

# Path to the dbt executable. In an Airflow image dbt usually lives in its own
# virtualenv to keep its dependency tree away from Airflow's.
DBT_EXECUTABLE = os.environ.get("DBT_EXECUTABLE_PATH", "/usr/local/bin/dbt")

# Which profiles.yml target to build. The repo's committed profiles.yml has
# `dev`/`ci` on DuckDB and `prod`/`redshift_prod` for real warehouses.
DBT_TARGET = os.environ.get("DBT_TARGET", "snowflake_prod")

# The synthetic dataset that stands in for ELT-landed raw tables. Against a
# real warehouse these tables come from Fivetran/Airbyte and this selector
# should be dropped entirely along with the seed task below.
RAW_SEED_SELECTOR = "path:seeds/example"
LOAD_DEMO_SEEDS = os.environ.get("DBT_LOAD_DEMO_SEEDS", "true").lower() == "true"

profile_config = ProfileConfig(
    profile_name="dbt_ecom_data_model",
    target_name=DBT_TARGET,
    # Reuse the repo's committed profiles.yml. It contains no secrets — every
    # warehouse value is read from the environment — so the credentials your
    # Airflow workers already hold are what get used.
    profiles_yml_filepath=DBT_PROJECT_DIR / "profiles.yml",
)

with DAG(
    dag_id="dbt_ecom_data_model",
    description="Build the e-commerce warehouse: raw landing tables, then the dbt DAG.",
    # 06:00, after an overnight ELT window would realistically land.
    schedule="0 6 * * *",
    start_date=datetime(2026, 1, 1),
    catchup=False,
    max_active_runs=1,
    default_args={"retries": 1},
    tags=["dbt", "ecommerce"],
) as dag:
    # ------------------------------------------------------------------
    # The ordering dbt cannot express.
    #
    # The demo seeds impersonate raw landing tables and staging models reach
    # them through source(), not ref() — so dbt has no DAG edge from the seeds
    # to the models that read them, and a single `dbt build` against an empty
    # warehouse races them. Cosmos renders the dbt DAG faithfully, which means
    # it inherits that gap. Making the seed load an explicit upstream task is
    # how an orchestrator fixes it.
    #
    # Against real client data there are no demo seeds, the raw tables are
    # produced by your ELT tool, and this task is replaced by a sensor or a
    # dependency on the ELT DAG instead.
    # ------------------------------------------------------------------
    load_raw_landing_tables = BashOperator(
        task_id="load_raw_landing_tables",
        bash_command=(
            f"cd {DBT_PROJECT_DIR} && "
            f"{DBT_EXECUTABLE} seed --target {DBT_TARGET} --select {RAW_SEED_SELECTOR}"
        ),
    )

    transform = DbtTaskGroup(
        group_id="transform",
        project_config=ProjectConfig(
            dbt_project_path=DBT_PROJECT_DIR,
            # Parse the committed manifest rather than shelling out to
            # `dbt ls` at DAG-parse time. Airflow re-parses DAG files on a
            # short interval, and a subprocess per parse is a well-known way
            # to melt a scheduler.
            manifest_path=DBT_PROJECT_DIR / "target" / "manifest.json",
        ),
        profile_config=profile_config,
        execution_config=ExecutionConfig(
            # LOCAL runs dbt in the worker process. Swap for KUBERNETES or
            # DOCKER to isolate dbt's dependency tree from Airflow's, which is
            # what most production deployments end up doing.
            execution_mode=ExecutionMode.LOCAL,
            dbt_executable_path=DBT_EXECUTABLE,
        ),
        render_config=RenderConfig(
            load_method=LoadMode.DBT_MANIFEST,
            # Tests run as part of `dbt build` for each node, so a failing
            # test blocks its downstream instead of being discovered later.
            test_behavior=TestBehavior.BUILD,
            # Excluded because the seed task above already loaded them;
            # rendering them here would duplicate the work.
            exclude=[RAW_SEED_SELECTOR],
        ),
        default_args={"retries": 1},
    )

    if LOAD_DEMO_SEEDS:
        load_raw_landing_tables >> transform
