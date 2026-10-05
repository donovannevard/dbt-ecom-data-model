# Verifying against a real Snowflake or Redshift warehouse

DuckDB is executed end to end on every push. Snowflake and Redshift are only
*config-parsed* in CI — every dispatched macro resolves and every config is
valid for that adapter, but no SQL has ever run against either. This page is
how to close that gap when a real warehouse is reachable, and what to look at
when it reports something.

## Where connection details go

`profiles.yml` is committed and contains **no credentials** — every warehouse
value is an `env_var()` reference, so the file is safe to publish and the
secrets live in your environment.

Which variables a target needs:

```bash
make check-env TARGET=snowflake_prod
make check-env TARGET=redshift_prod
```

That prints each variable and whether it is set, so a failed run tells you
*which* one is missing rather than dbt's terse "Env var required but not
provided".

| Variable | Used by | Notes |
|---|---|---|
| `DB_TRANSFORM_USER` | both | |
| `DB_TRANSFORM_PASSWORD` | both | |
| `DB_TRANSFORM_DATABASE` | both | Point at a **throwaway** database — see below |
| `SNOWFLAKE_ACCOUNT` | Snowflake | Account identifier, e.g. `abc12345.eu-west-1` |
| `SNOWFLAKE_TRANSFORM_ROLE` | Snowflake | Needs CREATE SCHEMA on the database |
| `SNOWFLAKE_TRANSFORM_WAREHOUSE` | Snowflake | A small/auto-suspend warehouse is plenty |
| `REDSHIFT_HOST` | Redshift | Cluster endpoint, without the port |
| `REDSHIFT_PORT` | Redshift | **Optional** — defaults to 5439 |

### Three ways to supply them

**1. Export in your shell** — simplest for a one-off test:

```bash
export SNOWFLAKE_ACCOUNT=... SNOWFLAKE_TRANSFORM_ROLE=... SNOWFLAKE_TRANSFORM_WAREHOUSE=...
export DB_TRANSFORM_USER=... DB_TRANSFORM_PASSWORD=... DB_TRANSFORM_DATABASE=...
dbt build --target snowflake_prod
```

**2. A `.env` file** — better when a cluster is up for a while:

```bash
cp .env.snowflake.example .env.snowflake   # or .env.redshift.example
make verify-warehouse-smoke TARGET=snowflake_prod
```

⚠️ **dbt Core does not read `.env` files.** This catches people out, because
dbt Cloud and Dagster both do. The `make` targets load it for you:

```bash
make dbt CMD="build --target snowflake_prod"    # .env applied
dbt build --target snowflake_prod               # .env NOT applied
```

**Do not use `set -a && source .env`**, the usual shell idiom. Sourcing makes
the shell parse each line as an assignment, so characters significant to the
shell are consumed: a backslash in a password disappears, `$VAR` is expanded to
something else, and backticks execute. The symptom is an authentication failure
that points nowhere near the cause — which is exactly how a 25-character
password arrived at Redshift as 24 characters during this project's own
verification run.

`scripts/with_env.py` parses the file instead: split on the first `=`, strip one
layer of matching quotes, take the rest literally. Existing environment
variables win over the file, so a real secret exported by CI is never shadowed.

### A password that will not authenticate

If credentials are definitely correct but the warehouse rejects them, check how
the value was captured. A password copied out of JSON — `terraform output`
without `-raw`, a Secrets Manager payload, a CloudFormation response — arrives
**escaped**, and the literal text is not the password:

```bash
terraform output -raw redshift_master_password     # -raw avoids JSON escaping
```

Store the real characters, and wrap the value in single quotes if it contains
anything exotic:

```
DB_TRANSFORM_PASSWORD='the-actual-password'
```

**3. CI secrets** — for automated runs, set the same names as repository
secrets and expose them as `env:` on the job. The existing `portability` job in
`.github/workflows/dbt_ci.yml` shows the pattern with placeholders.

### Holding credentials for two warehouses at once

`profiles.yml` reads the same `DB_TRANSFORM_USER` and `DB_TRANSFORM_PASSWORD`
for every warehouse, so a single `.env` can only describe one of them —
filling in Snowflake overwrites Redshift. Use a file per warehouse:

```bash
cp .env.snowflake.example .env.snowflake
cp .env.redshift.example  .env.redshift

ENV_FILE=.env.snowflake make verify-warehouse TARGET=snowflake_prod
ENV_FILE=.env.redshift  make verify-warehouse TARGET=redshift_prod
```

Each template carries only the variables its own target needs — six for
Snowflake, four for Redshift — and documents the gotchas specific to that
warehouse, including the two that cost real time here: a Redshift `REDSHIFT_HOST`
must be the bare hostname with no `:5439` or `/database`, and a password pasted
from JSON output is escaped and will be rejected as an authentication failure.

The `.gitignore` depends on the `.example` suffix: **files ending `.example`
are templates and are tracked**; **everything else matching `.env*` holds real
credentials and is ignored**. So `.env.snowflake` never gets committed while
`.env.snowflake.example` always does.

`make check-env` prints which file it read, so there is never ambiguity about
which credentials a run actually used.

## Running it

```bash
make verify-warehouse-smoke TARGET=snowflake_prod   # minutes: dialect-sensitive only
make verify-warehouse       TARGET=snowflake_prod   # full build + tests
```

Both run `make check-env` and `dbt debug` first, so a credential or networking
problem surfaces before anything tries to build. Use `TARGET=redshift_prod` for
Redshift.

**Run the smoke target first.** It builds only the models that exercise a
dispatched macro, the incremental merge or a snapshot strategy — everything
that could plausibly behave differently on another warehouse. If the dialect is
wrong you find out in minutes rather than after a full build, which matters when
the cluster is up on a timer.

**Point `DB_TRANSFORM_DATABASE` at a throwaway database.** `macros/generate_schema_name.sql`
makes custom schemas absolute, so this project writes to `extract`, `seeds` and
`snapshots` regardless of target. Sharing a database between targets would have
them overwrite each other's snapshot history.

## What is actually at risk

Everything else is plain ANSI. These are the constructs with a per-warehouse
implementation or a known adapter difference, and therefore the only places a
real run can tell you something a parse cannot.

| What | Where | What to check |
|---|---|---|
| Regex matching | `macros/regexp_contains.sql` | `int_*_channel_classification` classify into Brand / Non Brand / Shopping / Display / Social / **Lead Generation** — not `Other`. An `Other` row where a pattern should have matched means the regex flavour differs. |
| Month / weekday names | `macros/date_names.sql` | `dim_date.month_name` reads `January` not `JANUARY`, `JAN` or `January  ` (Redshift blank-pads; the macro TRIMs). `day_of_week_name` reads `Monday`, not `Mon`. |
| Integer cast of dirty input | `macros/safe_cast_integer.sql` | `stg_web_visits` builds at all, and `fct_visits.attributed_ad_id` is populated for gclid rows. Redshift has no `TRY_CAST`, so it regex-guards instead; if that path is wrong the model errors rather than returning NULLs. |
| Incremental merge | `models/analytics/fct_visits.sql` | The **second** build is the test — `verify-warehouse` runs it twice for this reason. Confirms `incremental_strategy='merge'` with `unique_key` is supported and does not duplicate rows. |
| Snapshot strategies | `snapshots/` | `currency_rates_snapshot` uses `timestamp`, `products_snapshot` uses `check` on `price`. Both should create rows on first run and add none on an unchanged second run. |
| Seed column types | `seeds/example/example_seeds.yaml` | `prod_web_visits.customer_id` and the `gclid` columns must land as NULL, not `0` or `''`. Check `select count(*) from fct_visits where attributed_platform is not null` — it should be ~283 of 900, not 0 and not 900. |
| Date arithmetic | `models/analytics/dim_date.sql` | Row count ~13,100 (2000-01-01 to ten years past this year). A wildly different count means `dbt.dateadd` or `date_spine` behaved differently. |
| Numeric precision | `fct_transactions`, `agg_channel` | Gross ROAS 2.00 and net ROAS 1.20 on the attributable channels. A material drift means `numeric(p,s)` rounding differs. |

## Confirming the numbers match DuckDB

The same dataset should produce the same answers on any warehouse. Run this
against the verified target and compare to the DuckDB figures:

```sql
-- expect: gross_roas 2.0000, net_roas ~1.2009
select
    round(sum(attributed_gross_revenue) / sum(spend), 4)       as gross_roas,
    round(sum(attributed_contribution_margin) / sum(spend), 4) as net_roas
from <database>.main.agg_channel
where attributed_gross_revenue > 0;

-- expect: 6 rows, including a 'Lead Generation' row (regex grouping works)
select channel_grouping, round(spend, 2), round(gross_roas, 2), round(net_roas, 2)
from <database>.main.agg_channel
order by spend desc;

-- expect: 900 total, ~283 attributed, ~550 with a NULL customer_id
select count(*), count(attributed_platform), count(*) - count(customer_id)
from <database>.main.fct_visits;
```

Note the schema will not be `main` on Snowflake or Redshift — it is whatever
the target's `schema:` is set to (`transform` for the `*_prod` targets).

## Redshift-specific

A production Redshift deployment should add `dist` and `sort` configs to the
large fact tables. None are set here deliberately: the right distribution key
depends on node count and the queries actually run against the cluster, and a
wrong `DISTKEY` is materially worse than none — it skews storage and forces
broadcast joins. While you have a cluster up is a reasonable time to experiment,
but treat any result from a 900-row demo dataset as meaningless for sizing.

`fct_transactions` would most likely want `dist='order_id'` with
`sort='order_date'`, and `fct_visits` `sort='visited_at'`.

## Identifier quoting: do not

This is the convention that made Snowflake work, and it is worth stating because
it is easy to undo by accident.

**No SQL identifier in this project is quoted.** Snowflake folds unquoted
identifiers to UPPER case and preserves quoted ones verbatim; DuckDB and
Redshift fold to lower case. So `SELECT CURRENT_DATE AS "date"` creates a column
that only a quoted lowercase reference can find — and a downstream model saying
`CAST(date AS DATE)` resolves to `DATE`, which does not exist. On DuckDB and
Redshift both spellings land in the same place and the bug is invisible.

Five models failed this way on the first Snowflake run. If you add a model, let
the adapter fold: write `AS date`, not `AS "date"`. The only double quotes left
in the SQL are Jinja string delimiters (`var("reporting_currency")`,
`datepart = "day"`).

## Findings from the Snowflake run, 2026-10-05

| Symptom | Cause | Fix |
|---|---|---|
| `invalid identifier '"date"'` (5 models) | Mixing `AS "date"` with unquoted `date` downstream. Snowflake folds unquoted to UPPER, keeps quoted verbatim | All SQL identifier quoting removed |
| `Object '"date_spine"' does not exist` | Quoted reference to a CTE declared unquoted | Same |

## Findings from the Redshift run, 2026-10-05

Both of these were already fixed; they are recorded because they are the
argument for running against a real warehouse rather than trusting a parse.
Both have the same root cause: Redshift applies restrictions to expressions
evaluated on **compute nodes** that do not apply to constants folded on the
leader node. A probe using literals succeeded while the identical construct
against joined tables failed — so test with the real query shape, not a
scratch `SELECT`.

| Symptom | Cause | Fix |
|---|---|---|
| `The pattern must be a valid UTF-8 literal character expression` | Regex pattern supplied from a joined column. `~`, `REGEXP_INSTR`, `REGEXP_COUNT` and `SIMILAR TO` **all** reject a non-constant pattern | Taxonomy compiled into a `CASE` of literal patterns at build time (`macros/classify_by_taxonomy.sql`) |
| `Interval values with month or year parts are not supported` | `col + INTERVAL '1 year'`. Day intervals are fine; month/year are not | `{{ dbt.dateadd('year', 1, col) }}` → `DATEADD` |

Two incidental lessons worth keeping:

- **Jinja parses SQL comments.** Writing `{%` inside a `--` comment executes it.
- **`--indirect-selection=cautious`** is required for any partial build, or dbt
  pulls in `relationships` tests whose other side was not selected and they
  fail for reasons that look like dialect bugs.

## Afterwards

Update the warehouse support table in the README. The point of that table is
that it distinguishes executed from parsed, so it is worth keeping honest:

```markdown
| `snowflake_prod` / `snowflake_dev` | Snowflake | Production | **Fully executed**, verified YYYY-MM-DD on dbt 1.12 |
```

If anything needed a fix to get green, that fix belongs in the dispatched macro
rather than in a model — that is the whole reason the dialect differences are
isolated in three files.
