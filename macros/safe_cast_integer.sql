{#
    Cast to integer, yielding NULL instead of an error on anything that is not
    an integer.

    DuckDB and Snowflake both have TRY_CAST. Redshift has no TRY_CAST and no
    equivalent — a plain CAST of a non-numeric string aborts the whole query —
    so there the value is guarded with a numeric regex first and CAST only runs
    on input known to be safe.

    Needed in two places that both handle genuinely dirty input: an optional
    customer id on anonymous web visits, and the ad id parsed out of a gclid,
    which is attacker-ish data in the sense that it arrives from a URL and may
    be truncated or malformed.
#}

{% macro safe_cast_integer(expression) -%}
    {{ return(adapter.dispatch('safe_cast_integer', 'dbt_ecom_data_model')(expression)) }}
{%- endmacro %}

{% macro default__safe_cast_integer(expression) -%}
    TRY_CAST({{ expression }} AS INTEGER)
{%- endmacro %}

{% macro redshift__safe_cast_integer(expression) -%}
    CASE
        WHEN ({{ expression }}) ~ '^[0-9]+$' THEN CAST({{ expression }} AS INTEGER)
    END
{%- endmacro %}
