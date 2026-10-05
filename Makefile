# Everything you need to run, test and explore this project.
# `make` on its own lists the targets.

VENV    := .venv
DAGSTER_HOME := $(CURDIR)/.dagster
export DAGSTER_HOME

# Warehouse credentials come from the environment. If a .env file exists the
# verify targets load it, so they work without exporting anything by hand.
#
# Loaded by scripts/with_env.py, NOT by sourcing it in a shell. Sourcing makes
# the shell parse each line as an assignment, which silently eats a backslash
# in a password, expands $VAR and executes backticks -- and the only symptom is
# an authentication error that points nowhere near the cause.
#
# dbt itself does NOT read .env. Only these targets do.
# Override to keep per-warehouse credentials side by side rather than
# overwriting one set with the other:
#     ENV_FILE=.env.snowflake make verify-warehouse TARGET=snowflake_prod
ENV_FILE ?= .env
export ENV_FILE

WITH_ENV := $(PY) scripts/with_env.py
PY      := $(VENV)/bin/python
PIP     := $(VENV)/bin/pip
DBT     := $(VENV)/bin/dbt

.DEFAULT_GOAL := help
.PHONY: help dbt install install-orchestration verify-airflow-examples verify-warehouse verify-warehouse-smoke check-env deps seed build up test docs docs-serve query dagster dagster-run docker-dagster docker-up docker-build docker-shell docker-run docker-down regen-seeds parse-all clean fresh shell

help:  ## Show this help
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[1m%-14s\033[0m %s\n", $$1, $$2}'
	@echo ""
	@echo "  First time:  make up        (installs everything and builds)"
	@echo "  Explore:     make docs      (lineage graph + column docs in browser)"
	@echo "  Ad-hoc SQL:  make query Q=\"select * from {{ ref('agg_channel') }}\""
	@echo "  No Python?   make docker-up  (builds and serves in Docker)"
	@echo "  Orchestrate: make install-orchestration && make dagster"

install:  ## Create the virtualenv and install dbt-duckdb
	python3 -m venv $(VENV)
	$(PIP) install --quiet --upgrade pip
	$(PIP) install --quiet -r requirements.txt
	@$(DBT) --version

deps:  ## Install dbt packages (dbt_utils)
	$(DBT) deps

seed:  ## Load reference data + the synthetic demo dataset
	$(DBT) seed

build:  ## Build every model, snapshot and test
	$(DBT) build

up: install deps seed build  ## One-shot setup from a fresh clone
	@echo ""
	@echo "Done. Try:  make docs   or   make query Q=\"select * from {{ ref('agg_channel') }}\""

test:  ## Run the test suite only
	$(DBT) test

# --static bundles the whole site into one self-contained HTML file, so there
# is no server to start, no port to collide and no Ctrl+C needed. Use
# `make docs-serve` if you want the hosted version instead.
docs:  ## Build the lineage graph + column docs and open them in your browser
	$(DBT) docs generate --static
	@echo ""
	@echo "Opening target/static_index.html"
	@(command -v open >/dev/null 2>&1 && open target/static_index.html) \
		|| (command -v xdg-open >/dev/null 2>&1 && xdg-open target/static_index.html) \
		|| echo "Open this file in your browser: target/static_index.html"

docs-serve:  ## Serve the docs at localhost:8080 instead (Ctrl+C to stop)
	$(DBT) docs generate
	@echo "Serving at http://localhost:8080 — Ctrl+C to stop"
	-@$(DBT) docs serve --port 8080 --no-browser 2>/dev/null
	@echo "Docs server stopped."

# Usage: make query Q="select * from {{ ref('agg_channel') }}"
query:  ## Run ad-hoc SQL against the built warehouse (use Q="...", ref() works)
	@test -n "$(Q)" || { echo "Set Q to the SQL you want to run, e.g."; \
		echo '  make query Q="select * from {{ ref(ONE_QUOTEagg_channelONE_QUOTE) }}"' | sed "s/ONE_QUOTE/'/g"; \
		exit 1; }
	@$(DBT) show --inline "$(Q)" --limit $(or $(LIMIT),20)

regen-seeds:  ## Regenerate the synthetic demo dataset (deterministic)
	$(PY) scripts/generate_example_seeds.py

# Proves the project configures cleanly on all three supported warehouses.
# Needs the Snowflake and Redshift adapters: pip install dbt-snowflake dbt-redshift
parse-all:  ## Parse against DuckDB, Snowflake and Redshift targets
	@for t in dev snowflake_prod redshift_prod; do \
		printf "%-16s" "$$t"; \
		REDSHIFT_HOST=placeholder REDSHIFT_PORT=5439 SNOWFLAKE_ACCOUNT=placeholder \
		SNOWFLAKE_TRANSFORM_ROLE=placeholder SNOWFLAKE_TRANSFORM_WAREHOUSE=placeholder \
		DB_TRANSFORM_USER=placeholder DB_TRANSFORM_PASSWORD=placeholder \
		DB_TRANSFORM_DATABASE=placeholder \
		$(DBT) parse --target $$t --no-partial-parse >/dev/null 2>&1 \
			&& echo "OK" || echo "FAILED"; \
	done

shell:  ## Open a DuckDB SQL shell on the built database (needs the duckdb CLI)
	@command -v duckdb >/dev/null 2>&1 || (echo "duckdb CLI not found. Install with: brew install duckdb"; exit 1)
	duckdb dev.duckdb

# ---------------------------------------------------------------------------
# Real-warehouse verification
#
# DuckDB is executed on every push; Snowflake and Redshift are only
# config-parsed. These targets are what turns that into a tested claim once a
# real warehouse is reachable. Credentials come from the environment -- see
# .env.<warehouse>.example and docs/warehouse-verification.md.
# ---------------------------------------------------------------------------

# Usage: make dbt CMD="build --target redshift_prod"
dbt:  ## Run any dbt command with .env loaded (use CMD="build --target ...")
	@test -n "$(CMD)" || { echo 'Set CMD, e.g. make dbt CMD="build --target redshift_prod"'; exit 1; }
	@$(WITH_ENV) $(DBT) $(CMD)

check-env:  ## Show which credentials a target needs and what is missing (TARGET=...)
	@test -n "$(TARGET)" || { echo "Set TARGET, e.g. make check-env TARGET=snowflake_prod"; exit 1; }
	@$(PY) scripts/check_target_env.py $(TARGET)

# Builds only the models that exercise a dispatched macro, the incremental
# merge and both snapshot strategies -- i.e. everything that could plausibly
# behave differently on another warehouse. Minutes rather than a full build,
# so a dialect problem surfaces while the cluster is still up.
verify-warehouse-smoke:  ## Dialect-sensitive subset only (TARGET=snowflake_prod|redshift_prod)
	@test -n "$(TARGET)" || { echo "Set TARGET, e.g. make verify-warehouse-smoke TARGET=snowflake_prod"; exit 1; }
	@$(PY) scripts/check_target_env.py $(TARGET)
	@$(WITH_ENV) $(DBT) debug --target $(TARGET)
	@$(WITH_ENV) $(DBT) deps
	@$(WITH_ENV) $(DBT) seed --target $(TARGET)
	# --indirect-selection=cautious: without it dbt pulls in relationships
	# tests whose *other* side is outside this selection, and they fail
	# because that table was never built -- which looks exactly like a
	# dialect bug and is not one.
	@$(WITH_ENV) $(DBT) build --target $(TARGET) --indirect-selection=cautious --select \
		+dim_date \
		+int_marketing__channel_classification \
		+int_session__marketing_channel_classification \
		+fct_visits \
		+currency_rates_snapshot \
		+products_snapshot
	@echo ""
	@echo "Smoke test passed on $(TARGET). Now run: make verify-warehouse TARGET=$(TARGET)"

verify-warehouse:  ## Full build + tests against a real warehouse (TARGET=...)
	@test -n "$(TARGET)" || { echo "Set TARGET, e.g. make verify-warehouse TARGET=snowflake_prod"; exit 1; }
	@$(PY) scripts/check_target_env.py $(TARGET)
	@$(WITH_ENV) $(DBT) debug --target $(TARGET)
	@$(WITH_ENV) $(DBT) deps
	@$(WITH_ENV) $(DBT) seed --target $(TARGET)
	@$(WITH_ENV) $(DBT) build --target $(TARGET)
	@echo ""
	@$(WITH_ENV) $(DBT) build --target $(TARGET) --select fct_visits
	@echo ""
	@echo "==================================================================="
	@echo " Full build and test suite passed on $(TARGET)."
	@echo " The second fct_visits build exercised the incremental merge path."
	@echo ""
	@echo " Update the warehouse support table in README.md:"
	@echo "   $(TARGET) -> Fully executed, verified $$(date +%Y-%m-%d)"
	@echo "==================================================================="

# ---------------------------------------------------------------------------
# Orchestration — Dagster. dbt has no scheduler; this is what runs it.
# ---------------------------------------------------------------------------

install-orchestration:  ## Add Dagster to the virtualenv
	$(PIP) install --quiet -r requirements-orchestration.txt
	@$(VENV)/bin/dagster --version

# The asset graph is built from the dbt manifest, so it has to exist first.
dagster:  ## Run the Dagster UI at localhost:3000 (asset graph, schedule, runs)
	@test -x $(VENV)/bin/dagster || { echo "Dagster not installed. Run: make install-orchestration"; exit 1; }
	@mkdir -p $(DAGSTER_HOME)
	$(DBT) parse
	@echo "Dagster UI -> http://localhost:3000   (Ctrl+C to stop)"
	-@$(VENV)/bin/dagster dev -m orchestration.definitions -p 3000

dagster-run:  ## Run the whole pipeline through Dagster, no UI
	@test -x $(VENV)/bin/dagster || { echo "Dagster not installed. Run: make install-orchestration"; exit 1; }
	@mkdir -p $(DAGSTER_HOME)
	$(DBT) parse
	$(VENV)/bin/dagster asset materialize --select '*' -m orchestration.definitions

# Parse-checks the reference Airflow DAGs in orchestration/external/ by
# importing them for real. Deliberately not part of CI: coupling this
# project's build to Airflow's release cycle would mean red builds for
# reasons unrelated to the data model.
verify-airflow-examples:  ## Import-check the reference Airflow DAGs (installs Airflow in .venv-airflow)
	@test -d $(VENV)-airflow || python3 -m venv $(VENV)-airflow
	@$(VENV)-airflow/bin/pip install --quiet --upgrade pip
	@$(VENV)-airflow/bin/pip install --quiet astronomer-cosmos
	@$(DBT) parse -q
	@AIRFLOW_HOME="$(CURDIR)/$(VENV)-airflow/home" \
		DBT_PROJECT_DIR="$(CURDIR)" \
		DBT_EXECUTABLE_PATH="$(CURDIR)/$(VENV)/bin/dbt" \
		DBT_TARGET=dev \
		$(VENV)-airflow/bin/python orchestration/external/parse_check.py \
			orchestration/external/airflow_cosmos_dag.py \
			orchestration/external/airflow_bash_dag.py

# ---------------------------------------------------------------------------
# Docker — the no-Python-on-your-machine path
# ---------------------------------------------------------------------------

docker-up:  ## Build and run everything in Docker, docs at localhost:8080
	docker compose up --build

docker-build:  ## Build the Docker image only
	docker compose build

docker-shell:  ## Interactive shell inside the container
	docker compose run --rm cli bash

# Usage: make docker-run CMD="dbt test"
docker-run:  ## Run one dbt command in the container (use CMD="dbt test")
	@test -n "$(CMD)" || { echo 'Set CMD, e.g. make docker-run CMD="dbt test"'; exit 1; }
	docker compose run --rm cli $(CMD)

docker-dagster:  ## Run Dagster in Docker, UI at localhost:3000
	docker compose --profile orchestration up --build

docker-down:  ## Stop all containers and remove them
	docker compose --profile orchestration --profile tools down

clean:  ## Remove build artefacts and the local database
	rm -rf target dbt_packages logs dev.duckdb dev.duckdb.wal ci.duckdb ci.duckdb.wal

fresh: clean deps seed build  ## Wipe everything and rebuild from scratch
