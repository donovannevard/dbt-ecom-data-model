#!/usr/bin/env sh
#
# Starts the Dagster orchestrator: asset graph, schedule and run history at
# http://localhost:3000.
#
# Any command passed in replaces this, so the image also works as a one-shot
# runner, which is what the CI job uses:
#   docker compose run --rm dagster dagster asset materialize --select '*' -m orchestration.definitions
set -eu

if [ "$#" -gt 0 ]; then
    exec "$@"
fi

# Dagster builds its asset graph from the dbt manifest at import time, so the
# manifest has to exist before the module is loaded. `dbt parse` writes it
# without touching the warehouse.
echo "Parsing the dbt project to build the asset graph..."
dbt parse

echo ""
echo "==============================================================="
echo " Dagster is starting. Open http://localhost:${DAGSTER_PORT:-3000}"
echo ""
echo " - Assets tab: the full graph, raw landing tables -> marts"
echo " - Automation tab: the daily_refresh schedule (off by default)"
echo " - Click 'Materialize all' to run the whole pipeline"
echo "==============================================================="
echo ""

# `dagster dev` runs the webserver and daemon in one process, which is right
# for local use and for a demo. A real deployment runs dagster-webserver and
# dagster-daemon as separate long-lived services against a Postgres instance.
exec dagster dev -m orchestration.definitions -h 0.0.0.0 -p 3000
