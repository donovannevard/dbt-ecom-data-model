-- Fiscal year for a date, given a configurable fiscal-year start month
-- (vars.fiscal_year_start_month in dbt_project.yml, defaults to 1 = the
-- fiscal year matches the calendar year). E.g. with start month 4 (UK tax
-- year style), a date in Feb 2026 falls in fiscal year 2025.
{% macro fiscal_year(date_column) %}
    CASE
        WHEN EXTRACT(MONTH FROM {{ date_column }}) >= {{ var('fiscal_year_start_month', 1) }}
        THEN EXTRACT(YEAR FROM {{ date_column }})
        ELSE EXTRACT(YEAR FROM {{ date_column }}) - 1
    END
{% endmacro %}
