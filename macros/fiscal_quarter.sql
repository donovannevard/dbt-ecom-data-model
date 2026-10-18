-- Fiscal quarter (1-4) for a date, using the same fiscal-year start month
-- as fiscal_year(). With the default start month of 1, this matches the
-- calendar quarter.
{% macro fiscal_quarter(date_column) %}
    (FLOOR(MOD(EXTRACT(MONTH FROM {{ date_column }}) - {{ var('fiscal_year_start_month', 1) }} + 12, 12) / 3) + 1)
{% endmacro %}
