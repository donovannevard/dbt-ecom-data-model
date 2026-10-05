# Running this project from your own orchestrator

The dbt project has **no dependency on any orchestrator.** Delete the
`orchestration/` directory entirely and `dbt build` still works, CI still
passes, and nothing in `models/`, `macros/` or `seeds/` changes. The built-in
Dagster setup one level up is a convenience, not a coupling.

That is deliberate. Most clients already run something — usually Airflow — and
a dbt project that insists on its own scheduler is a dbt project that has to be
rewritten before it can be adopted.

## The integration contract

Any orchestrator needs to do four things. Everything else is detail.

**1. Make the dbt packages available.** Either bake them into your image at
build time (preferred — no network call per run) or run `dbt deps` as a task.

```bash
dbt deps
```

**2. Ensure the raw tables exist before building models.**

This is the one non-obvious constraint, and it is the reason the demo needs a
task your real pipeline will not. The synthetic dataset in `seeds/example/**`
stands in for ELT-landed raw tables, and staging models reach it through
`source()` — exactly as they would against a real warehouse. **dbt therefore
has no DAG edge from those seeds to the models that read them**, so a single
`dbt build` against an empty warehouse races them.

```bash
dbt seed --select path:seeds/example     # demo only
```

Against real client data you **delete that step**. The raw tables are produced
by Fivetran/Airbyte/whatever, and this DAG should instead depend on that tool
finishing — an `ExternalTaskSensor`, a dataset trigger, or simply running after
the ingestion DAG in the same schedule.

**3. Build and test everything.**

```bash
dbt build --target snowflake_prod
```

`build` rather than `run`: it executes seeds, snapshots, models and tests in
DAG order, so a failing test stops its downstream instead of being discovered
after a dashboard has already read the bad data.

**4. Point dbt at the right profile and target.**

| Variable | Purpose |
|---|---|
| `DBT_PROFILES_DIR` | Directory holding `profiles.yml`. The repo's own copy is committed and contains no secrets — every warehouse value is read from the environment. |
| `--target` | `dev`/`ci` are DuckDB; `snowflake_prod`/`snowflake_dev` are Snowflake; `redshift_prod`/`redshift_dev` are Redshift. |
| `SNOWFLAKE_*`, `DB_TRANSFORM_*`, `REDSHIFT_*` | Warehouse credentials, as listed in `.env.snowflake.example` / `.env.redshift.example`. Supply them however your orchestrator manages secrets. |

That is the whole contract. Three commands and some environment.

## Artifacts worth wiring up

After a run, `target/` holds JSON your orchestrator can use:

- **`manifest.json`** — the project graph. Persist it between runs to enable
  state-based selection, which is how you build only what changed:
  ```bash
  dbt build --select state:modified+ --state path/to/previous/manifest
  ```
- **`run_results.json`** — per-node timing and status. The input for run-duration
  dashboards or alerting on a model that has started taking twice as long.
- **`sources.json`** — written by `dbt source freshness`. Worth a separate task
  before the build: if the raw data is stale, failing early is better than
  cheerfully rebuilding the warehouse from yesterday's data.

## The two reference DAGs here

Both are parse-checked against **Airflow 3.3.2** and **astronomer-cosmos
1.15.1** on Python 3.13 — imported, rendered, and their task graphs asserted.
They are not executed by CI, deliberately: coupling this project's build to
Airflow's release cycle would mean a red build for reasons that have nothing to
do with the data model.

### `airflow_cosmos_dag.py` — one Airflow task per dbt model

[astronomer-cosmos](https://github.com/astronomer/astronomer-cosmos) parses
`manifest.json` and renders the dbt DAG as Airflow tasks, so the Airflow graph
mirrors the dbt graph. Renders to **50 tasks** for this project.

Use it when you want per-model visibility: a failure shows as a red box on
`fct_transactions` rather than on a monolithic "dbt_build", and you can retry
one model instead of the whole project.

Note the `LoadMode.DBT_MANIFEST` setting. Cosmos can also discover models by
shelling out to `dbt ls` at DAG-parse time, and since Airflow re-parses DAG
files on a short interval, that is a well-known way to melt a scheduler. Parse
the committed manifest instead.

### `airflow_bash_dag.py` — three tasks, no extra dependencies

Plain `BashOperator` calls to the dbt CLI. Works on any Airflow with no Cosmos
and no extra providers.

You get one task for the entire dbt DAG, so a failure means reading dbt's logs
to find the model, and retries are all-or-nothing. That is a reasonable trade if
you want the orchestration layer to stay boring. Move to Cosmos when per-model
visibility starts to matter.

### Reproducing the parse check

```bash
python3 -m venv .venv-airflow
.venv-airflow/bin/pip install astronomer-cosmos
AIRFLOW_HOME="$PWD/.venv-airflow/home" \
DBT_PROJECT_DIR="$PWD" \
DBT_EXECUTABLE_PATH="$PWD/.venv/bin/dbt" \
DBT_TARGET=dev \
  .venv-airflow/bin/python -c "
import importlib.util
spec = importlib.util.spec_from_file_location('d', 'orchestration/external/airflow_cosmos_dag.py')
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
print(len(m.dag.tasks), 'tasks')"
```

Or `make verify-airflow-examples`, which does the same thing.

## dbt Cloud

The least work of the three main routes, because dbt Cloud *is* a scheduler and
this project needs no changes to run there.

**Setup**

1. Connect the repo. dbt Cloud reads `dbt_project.yml` and `packages.yml` as-is.
2. Configure a connection and environment. **dbt Cloud does not use
   `profiles.yml`** — warehouse credentials and the target database/schema are
   configured in its UI, and the committed `profiles.yml` is simply ignored.
   The `profile:` key in `dbt_project.yml` is likewise unused there.
3. Create a job with these commands, **in this order**:
   ```
   dbt seed --select path:seeds/example
   dbt build
   ```
   A dbt Cloud job runs an ordered list of commands, which is exactly what the
   seed-before-build constraint needs. `dbt deps` runs automatically before
   every job, so it is not in the list.
4. Set the schedule on the job. 06:00 is what the other orchestrators here use.

**For real client data**, drop the seed command entirely — the raw tables come
from the client's ELT tool and `models/sources.yaml` points at them.

**What you get for free:** scheduling, run history, logs, alerting, hosted
documentation (so `dbt docs` is served for you), and CI jobs that run on pull
requests against a temporary schema. The deferral feature also lets a PR job
build only changed models against production's existing tables, which is the
managed equivalent of the `--select state:modified+` pattern described above.

**What to be aware of:** dbt Cloud runs dbt and nothing else. If the warehouse
build needs to be sequenced against anything that is not dbt — an ingestion
job, a reverse-ETL push, an ML training step — that coordination has to live
somewhere else, which usually means you end up with an orchestrator anyway and
dbt Cloud reduced to the thing it triggers.

## Other orchestrators

| Tool | How |
|---|---|
| **cron** | `dbt deps && dbt seed --select path:seeds/example && dbt build`. Works. No retries, no history, no alerting — fine for a side project, not for a client. |
| **GitHub Actions** | Add `schedule:` to a workflow and reuse the steps in `.github/workflows/dbt_ci.yml`. Free, zero infrastructure, adequate for daily runs. |
| **Prefect** | `prefect-dbt` wraps the same CLI calls; the contract above is unchanged. |
| **Dagster** | Already here — see [`../definitions.py`](../definitions.py). |
