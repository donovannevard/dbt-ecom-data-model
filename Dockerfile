# Two images from one build, sharing a base layer:
#
#   target: dbt            (default) builds the project and serves dbt docs
#   target: orchestration            adds Dagster: asset graph, schedule, runs
#
# Dagster is a separate stage on purpose. It pulls in a few hundred MB that
# nobody who just wants to build the project or read the docs should have to
# download, so `docker compose up` stays light and the orchestrator is opt-in.
FROM python:3.12-slim-bookworm AS base

# git is listed by `dbt debug` as a required dependency, and is needed if
# packages.yml ever points at a git revision rather than the dbt hub.
RUN apt-get update \
    && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    # Makes dbt find the committed profiles.yml, whose default target is the
    # local DuckDB file. Nothing here needs credentials.
    DBT_PROFILES_DIR=/dbt

WORKDIR /dbt

# Layered deliberately, cheapest-to-invalidate last: Python deps change
# rarely, dbt packages less often than models, models constantly. Editing a
# model rebuilds only the final COPY.
COPY requirements.txt ./
RUN pip install -r requirements.txt

COPY dbt_project.yml packages.yml package-lock.yml ./
RUN dbt deps

COPY . .

# Run as a non-root user. target/, the DuckDB file and Dagster's run history
# are all written at runtime inside the image, so that user has to own the
# project directory.
RUN useradd --create-home --uid 10001 dbt \
    && mkdir -p /dbt/.dagster \
    && chown -R dbt:dbt /dbt


# ---------------------------------------------------------------------------
# Default image: build the project, then serve the documentation.
# ---------------------------------------------------------------------------
FROM base AS dbt

USER dbt
EXPOSE 8080

# Reports unhealthy if the docs server stops answering.
#
# A HEAD request, not a GET: dbt serves the docs with Python's
# SimpleHTTPRequestHandler, and a GET whose body we abandon early makes the
# server log a ConnectionResetError traceback on every single check. HEAD
# returns headers only, so the connection closes cleanly and the container
# logs stay readable.
HEALTHCHECK --interval=30s --timeout=5s --start-period=120s --retries=3 \
    CMD python -c "import urllib.request as u; u.urlopen(u.Request('http://127.0.0.1:8080/', method='HEAD'), timeout=4)"

ENTRYPOINT ["/dbt/docker/entrypoint.sh"]


# ---------------------------------------------------------------------------
# Orchestration image: the same project plus Dagster.
# ---------------------------------------------------------------------------
FROM base AS orchestration

COPY requirements-orchestration.txt ./
RUN pip install -r requirements-orchestration.txt

# Dagster writes run history, logs and schedule state here.
ENV DAGSTER_HOME=/dbt/.dagster

USER dbt
EXPOSE 3000

HEALTHCHECK --interval=30s --timeout=5s --start-period=120s --retries=3 \
    CMD python -c "import urllib.request as u; u.urlopen(u.Request('http://127.0.0.1:3000/', method='HEAD'), timeout=4)"

ENTRYPOINT ["/dbt/docker/entrypoint-dagster.sh"]
