{{ config(materialized = 'table') }}

-- start_date can be pushed earlier if historical data predates 2000-01-01;
-- end_date auto-extends 10 years past the current year on every run.
WITH
    date_spine AS (
        {{ dbt_utils.date_spine(
            datepart = "day",
            start_date = "CAST('2000-01-01' AS DATE)",
            end_date = dbt.dateadd('year', 10, dbt.date_trunc('year', 'CURRENT_DATE'))
        ) }}
    )
SELECT
    CAST(date_day AS DATE) AS date,

    -- Basic attributes
    EXTRACT(YEAR FROM date_day) AS year,
    EXTRACT(QUARTER FROM date_day) AS quarter,
    EXTRACT(MONTH FROM date_day) AS month_number,
    {{ month_name('date_day') }} AS month_name,
    {{ month_short('date_day') }} AS month_short,
    EXTRACT(DAY FROM date_day) AS day_of_month,

    -- Week attributes (ISO standard)
    EXTRACT(WEEK FROM date_day) AS week_number,
    {{ day_of_week_number('date_day') }} AS day_of_week_number,  -- 0 = Sunday, 6 = Saturday
    {{ day_name('date_day') }} AS day_of_week_name,
    {{ day_short('date_day') }} AS day_of_week_short,

    -- Week start/end – Monday-start week (common in UK/Europe)
    -- Derived from day_of_week_number rather than date_trunc('week'), which
    -- returns Monday on DuckDB/Postgres/Redshift, Sunday on BigQuery and
    -- whatever WEEK_START says on Snowflake. See macros/week_bounds.sql.
    {{ week_start_monday('date_day') }} AS week_start_date,
    {{ week_end_sunday('date_day') }} AS week_end_date,

    -- Month start/end
    {{ dbt.date_trunc('month', 'date_day') }} AS month_start_date,
    {{ dbt.last_day('date_day', 'month') }} AS month_end_date,

    -- Year start/end
    {{ dbt.date_trunc('year', 'date_day') }} AS year_start_date,
    -- dbt.dateadd, not `+ INTERVAL '1 year'`: Redshift rejects month/year
    -- interval arithmetic on a column ("Interval values with month or year
    -- parts are not supported") because it executes on compute nodes. Day
    -- intervals above are fine. Verified on a live cluster.
    {{ dbt.dateadd('day', -1, dbt.dateadd('year', 1, dbt.date_trunc('year', 'date_day'))) }} AS year_end_date,

    -- Fiscal calendar (vars.fiscal_year_start_month in dbt_project.yml; 1 = matches calendar year)
    {{ fiscal_year('date_day') }} AS fiscal_year,
    {{ fiscal_quarter('date_day') }} AS fiscal_quarter,

    -- Flags (direct boolean expressions)
    {{ day_of_week_number('date_day') }} IN (0, 6) AS is_weekend,
    date_day = CURRENT_DATE AS is_today

FROM date_spine
ORDER BY 1