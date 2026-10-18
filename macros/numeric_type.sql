{#
    A fixed-point numeric type, for use inside CAST.

    BigQuery rejects parameterised types in a CAST expression outright:
    "Parameterized types are not allowed in CAST expressions". Its NUMERIC is
    fixed at (38, 9) regardless, so the precision and scale are simply dropped
    there -- nothing is lost, since (38, 9) is more precise than anything this
    project asks for.

    The precision must NOT be dropped on the other adapters, which is why this
    is dispatched rather than globally simplified to `numeric`:

        Snowflake  bare NUMERIC == NUMBER(38, 0)   -- scale 0
        Redshift   bare NUMERIC == NUMERIC(18, 0)  -- scale 0
        DuckDB     bare NUMERIC == DECIMAL(18, 3)

    So `CAST(amount AS NUMERIC)` would silently round every monetary value to
    whole pounds on two of the four warehouses, and every test would still
    pass. A quiet wrong answer is worse than a loud failure, hence the
    explicit precision everywhere it is supported.
#}

{% macro numeric_type(precision, scale) -%}
    {{ return(adapter.dispatch('numeric_type', 'dbt_ecom_data_model')(precision, scale)) }}
{%- endmacro %}

{% macro default__numeric_type(precision, scale) -%}
    NUMERIC({{ precision }}, {{ scale }})
{%- endmacro %}

{% macro bigquery__numeric_type(precision, scale) -%}
    NUMERIC
{%- endmacro %}
