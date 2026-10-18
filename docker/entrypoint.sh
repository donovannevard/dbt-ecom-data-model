#!/usr/bin/env sh
#
# Default container behaviour: build the whole project, then serve the docs.
#
# Any command passed to `docker run`/`docker compose run` replaces this
# entirely, so the image doubles as a dbt CLI:
#   docker compose run --rm dbt dbt test
#   docker compose run --rm dbt dbt ls --resource-type model
set -eu

if [ "$#" -gt 0 ]; then
    exec "$@"
fi

echo "---------------------------------------------------------------"
echo " Loading reference data and the synthetic demo dataset"
echo "---------------------------------------------------------------"
# Seeds load separately from `dbt build` on purpose: they stand in for
# ELT-landed raw tables that staging reaches via source(), so dbt has no DAG
# edge from them to the models that read them. See the README.
dbt seed

echo "---------------------------------------------------------------"
echo " Building every model and snapshot, and running every test"
echo "---------------------------------------------------------------"
dbt build

echo "---------------------------------------------------------------"
echo " Generating docs"
echo "---------------------------------------------------------------"
dbt docs generate

echo ""
echo "==============================================================="
# docker-compose.yml passes DBT_DOCS_PORT so this banner names the host port
# the docs are actually reachable on. Without compose we cannot know what the
# caller mapped, so say so rather than printing a URL that may not work --
# guessing here is exactly what sends someone to the wrong port.
if [ -n "${DBT_DOCS_PORT:-}" ]; then
    echo " Ready. Open http://localhost:${DBT_DOCS_PORT}"
else
    echo " Ready. The docs are served on port 8080 inside the container."
    echo " Open whichever host port you published it to."
fi
echo ""
echo " The lineage graph is the button in the bottom right."
echo " Ctrl+C here (or docker compose down) to stop."
echo "==============================================================="
echo ""

# --host 0.0.0.0 is what makes this reachable from the host; dbt binds to
# localhost by default, which inside a container means nothing outside it.
exec dbt docs serve --host 0.0.0.0 --port 8080 --no-browser
