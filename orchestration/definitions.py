"""Dagster orchestration for the dbt project.

Why this exists
---------------
dbt has no scheduler. `dbt build` transforms data when something invokes it,
and `dbt docs` is a static catalogue, not a control plane. Production needs an
orchestrator, and this is it: Dagster loads every dbt model as an asset, so the
dbt DAG becomes part of a wider graph that can be scheduled, observed and
backfilled.

The one thing this adds that dbt cannot express
-----------------------------------------------
The demo dataset in `seeds/example/**` stands in for ELT-landed raw tables.
Staging models reach it through `source()`, exactly as they would against a
real warehouse, so dbt has no DAG edge from those seeds to the models that read
them — which is why the README tells you to run `dbt seed` *before*
`dbt build`, and why a single `dbt build` on an empty database races them.

An orchestrator is the right place to fix that. Those seeds materialise the
same asset keys that dbt assigns to its sources, so declaring them as one
upstream asset makes the dependency explicit and the ordering guaranteed. The
raw landing tables become a real node in the graph rather than a step someone
has to remember.

Run it
------
    dagster dev -m orchestration.definitions      # UI at localhost:3000

Airflow equivalent: astronomer-cosmos parses the same manifest into a DAG.
Dagster is used here because it runs in a single process with no scheduler,
webserver or metadata database to stand up, and its asset graph shows the dbt
lineage directly.
"""

# NB: no `from __future__ import annotations` here. It turns annotations into
# strings, and Dagster validates the `context` parameter by comparing the real
# class — postponed evaluation makes that check fail with a confusing message.
import json
import os
from pathlib import Path

from dagster import (
    AssetExecutionContext,
    AssetSelection,
    AssetSpec,
    Definitions,
    MaterializeResult,
    ScheduleDefinition,
    define_asset_job,
    multi_asset,
)
from dagster_dbt import DagsterDbtTranslator, DbtCliResource, dbt_assets

DBT_PROJECT_DIR = Path(__file__).resolve().parent.parent
DBT_MANIFEST = DBT_PROJECT_DIR / "target" / "manifest.json"

# dbt selector for the synthetic dataset that impersonates raw landing tables.
# Everything under seeds/example/** is "raw"; the reference seeds elsewhere in
# seeds/ are genuinely dbt-managed and stay part of the dbt asset graph.
RAW_SEED_SELECTOR = "path:seeds/example"


if not DBT_MANIFEST.exists():
    raise RuntimeError(
        f"dbt manifest not found at {DBT_MANIFEST}.\n"
        "Dagster builds its asset graph from the manifest, so generate it first:\n"
        "    dbt deps && dbt parse\n"
        "(or just `make build`, which does both along with everything else)."
    )


def _raw_landing_specs() -> list[AssetSpec]:
    """One asset spec per dbt source, keyed exactly as dbt_assets keys them.

    Matching the keys is what links this asset to every staging model: dbt's
    manifest says those models depend on these sources, so Dagster resolves the
    edge automatically and nothing downstream can run before the raw tables
    exist.
    """
    manifest = json.loads(DBT_MANIFEST.read_text())
    translator = DagsterDbtTranslator()
    return [
        AssetSpec(
            key=translator.get_asset_key(source),
            group_name="raw_landing",
            kinds={"duckdb", "csv"},
            description=(
                f"Raw `{source['schema']}.{source['name']}`, as an ELT tool would land it. "
                "Materialised in this demo from a synthetic seed file."
            ),
        )
        for source in manifest["sources"].values()
    ]


RAW_LANDING_SPECS = _raw_landing_specs()


@multi_asset(
    specs=RAW_LANDING_SPECS,
    name="raw_landing_tables",
    # One dbt invocation loads all of them, so this asset cannot be subset.
    can_subset=False,
)
def raw_landing_tables(context: AssetExecutionContext, dbt: DbtCliResource):
    """Load the synthetic raw dataset. Stands in for an ELT tool's sync.

    No `context=` on the dbt invocation: these seeds carry the *source* asset
    keys, not their own, so letting dagster-dbt translate dbt's own events here
    would emit materialisations for keys this asset does not own.
    """
    context.log.info("Loading synthetic raw landing tables via dbt seed")
    dbt.cli(["seed", "--select", RAW_SEED_SELECTOR]).wait()

    for spec in RAW_LANDING_SPECS:
        yield MaterializeResult(asset_key=spec.key)


@dbt_assets(
    manifest=DBT_MANIFEST,
    # The example seeds are excluded because they are represented by
    # raw_landing_tables above — including them here would define the same
    # asset keys twice. The reference seeds are not excluded: models ref() them,
    # so they belong in the dbt graph.
    exclude=RAW_SEED_SELECTOR,
)
def dbt_project_assets(context: AssetExecutionContext, dbt: DbtCliResource):
    """Every dbt model, snapshot, reference seed and test.

    `dbt build` rather than `dbt run`, so tests execute in DAG order alongside
    the models they guard and a failing test stops its downstream rather than
    being discovered afterwards.
    """
    yield from dbt.cli(["build"], context=context).stream()


# A single job over the whole graph: raw landing first, then the dbt DAG.
build_everything = define_asset_job(
    name="build_everything",
    selection=AssetSelection.all(),
    description="Load raw landing tables, then build and test the entire dbt project.",
)

# 06:00 UTC daily — after an overnight ELT window would realistically finish.
# Deliberately not hourly: nothing upstream changes that often, and a schedule
# that runs more often than its inputs change is just noise and warehouse spend.
daily_refresh = ScheduleDefinition(
    name="daily_refresh",
    job=build_everything,
    cron_schedule="0 6 * * *",
    execution_timezone="Europe/London",
    description="Nightly full refresh of the warehouse.",
)

defs = Definitions(
    assets=[raw_landing_tables, dbt_project_assets],
    jobs=[build_everything],
    schedules=[daily_refresh],
    resources={
        "dbt": DbtCliResource(project_dir=os.fspath(DBT_PROJECT_DIR)),
    },
)
