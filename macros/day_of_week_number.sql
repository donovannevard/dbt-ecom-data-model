{#
    Day of week as 0 = Sunday .. 6 = Saturday, consistently on every warehouse.

    `EXTRACT(DOW FROM ...)` is accepted by DuckDB, Postgres, Redshift and
    Snowflake and returns 0 for Sunday. BigQuery does not accept DOW at all --
    it spells the part DAYOFWEEK, and returns 1 for Sunday through 7 for
    Saturday, so it is shifted by one to agree with the others.

    Getting this wrong would be quiet rather than loud: dim_date.is_weekend
    tests the result against (0, 6), so an off-by-one would mark Monday and
    Friday as the weekend and still build cleanly.

    Note on Snowflake: EXTRACT(DOW) there honours the WEEK_START session
    parameter. The default of 0 gives Sunday = 0, which is what this assumes.
    A client whose Snowflake account sets WEEK_START=1 needs this macro, not
    the models, adjusted.
#}

{% macro day_of_week_number(subject) -%}
    {{ return(adapter.dispatch('day_of_week_number', 'dbt_ecom_data_model')(subject)) }}
{%- endmacro %}

{% macro default__day_of_week_number(subject) -%}
    EXTRACT(DOW FROM {{ subject }})
{%- endmacro %}

{% macro bigquery__day_of_week_number(subject) -%}
    (EXTRACT(DAYOFWEEK FROM DATE({{ subject }})) - 1)
{%- endmacro %}
