{#
    Month and weekday names, which no two warehouses spell the same way.

    DuckDB has no TO_CHAR at all (it uses strftime); Snowflake's TO_CHAR takes
    'MMMM'/'MON' rather than Postgres' 'Month'/'Mon'. The original Postgres
    spelling would not have produced correct output on Snowflake either, so
    these are dispatched rather than written inline.

    default__ targets DuckDB; redshift__ and snowflake__ cover the two
    production options. Redshift is Postgres-derived and does have TO_CHAR,
    but its output is blank-padded to a fixed width ('January  '), so every
    Redshift variant is TRIM-wrapped.

    A fourth warehouse needs its own implementations here and nothing else.
#}

{% macro month_name(subject) -%}
    {{ return(adapter.dispatch('month_name', 'dbt_ecom_data_model')(subject)) }}
{%- endmacro %}

{% macro default__month_name(subject) -%}
    STRFTIME({{ subject }}, '%B')
{%- endmacro %}

{% macro redshift__month_name(subject) -%}
    TRIM(TO_CHAR({{ subject }}, 'Month'))
{%- endmacro %}

{% macro snowflake__month_name(subject) -%}
    TO_CHAR({{ subject }}, 'MMMM')
{%- endmacro %}


{% macro month_short(subject) -%}
    {{ return(adapter.dispatch('month_short', 'dbt_ecom_data_model')(subject)) }}
{%- endmacro %}

{% macro default__month_short(subject) -%}
    STRFTIME({{ subject }}, '%b')
{%- endmacro %}

{% macro redshift__month_short(subject) -%}
    TRIM(TO_CHAR({{ subject }}, 'Mon'))
{%- endmacro %}

{% macro snowflake__month_short(subject) -%}
    MONTHNAME({{ subject }})
{%- endmacro %}


{% macro day_name(subject) -%}
    {{ return(adapter.dispatch('day_name', 'dbt_ecom_data_model')(subject)) }}
{%- endmacro %}

{% macro default__day_name(subject) -%}
    STRFTIME({{ subject }}, '%A')
{%- endmacro %}

{% macro redshift__day_name(subject) -%}
    TRIM(TO_CHAR({{ subject }}, 'Day'))
{%- endmacro %}

{% macro snowflake__day_name(subject) -%}
    {#- Snowflake has no full-weekday-name format: 'DY' and DAYNAME() both
        return the three-letter abbreviation, so the full name is mapped
        explicitly. Keyed off DAYNAME rather than DAYOFWEEK so the result
        does not depend on the WEEK_START session parameter. -#}
    CASE DAYNAME({{ subject }})
        WHEN 'Mon' THEN 'Monday'
        WHEN 'Tue' THEN 'Tuesday'
        WHEN 'Wed' THEN 'Wednesday'
        WHEN 'Thu' THEN 'Thursday'
        WHEN 'Fri' THEN 'Friday'
        WHEN 'Sat' THEN 'Saturday'
        WHEN 'Sun' THEN 'Sunday'
    END
{%- endmacro %}


{% macro day_short(subject) -%}
    {{ return(adapter.dispatch('day_short', 'dbt_ecom_data_model')(subject)) }}
{%- endmacro %}

{% macro default__day_short(subject) -%}
    STRFTIME({{ subject }}, '%a')
{%- endmacro %}

{% macro redshift__day_short(subject) -%}
    TRIM(TO_CHAR({{ subject }}, 'Dy'))
{%- endmacro %}

{% macro snowflake__day_short(subject) -%}
    DAYNAME({{ subject }})
{%- endmacro %}
