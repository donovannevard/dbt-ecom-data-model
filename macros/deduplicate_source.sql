{#
    Deterministic duplicate handling for a raw source table, used by every
    staging model in place of a bare `SELECT * FROM source(...)`.

    ELT tools re-deliver rows: a sync retries after a partial failure, or a
    backfill overlaps a previous load. Halting every downstream model over one
    re-delivered row is out of proportion, but so is silently picking one row
    when two versions of the same record disagree. So, per key:

      one version, one or more copies   collapsed to a single row (the latest
                                        load); nothing is lost, because every
                                        copy is identical
      two or more conflicting versions  excluded from staging entirely: no one
                                        can say which version is true, so none
                                        reaches revenue. The rows land in the
                                        no_conflicting_duplicates test's
                                        stored failures (the quarantine table)
                                        and that test warns
      null key                          passed through untouched, so not_null
                                        tests downstream still fail loudly

    "Version" compares every column except ignore_columns: load metadata that
    legitimately differs between copies of the same record.

    Portable across DuckDB, Snowflake, Redshift and BigQuery: derived tables
    rather than nested WITH (not reliably supported inside a subquery on
    Redshift), GROUP BY rather than COUNT(DISTINCT) OVER (unsupported on
    Redshift and BigQuery), and dbt_utils' surrogate key for the row hash.

    Reads the source's columns at run time, so at parse time it renders a
    plain SELECT *.
#}

{% macro deduplicate_source(relation, key_columns, ignore_columns=['_fivetran_synced']) -%}
{%- if not execute -%}
    SELECT * FROM {{ relation }}
{%- else -%}
    {%- set columns = adapter.get_columns_in_relation(relation) | map(attribute='name') | list -%}
    {%- set ignored = ignore_columns | map('lower') | list -%}
    {%- set compared = [] -%}
    {%- set load_columns = [] -%}
    {%- for column in columns -%}
        {%- if column | lower in ignored -%}
            {%- do load_columns.append(column) -%}
        {%- else -%}
            {%- do compared.append(column) -%}
        {%- endif -%}
    {%- endfor -%}
    {%- set version = dbt_utils.generate_surrogate_key(compared) -%}

    SELECT {{ columns | join(', ') }}
    FROM (
        SELECT
            s.*,
            ROW_NUMBER() OVER (
                PARTITION BY {% for k in key_columns %}s.{{ k }}{{ ', ' if not loop.last }}{% endfor %}
                ORDER BY {% for c in (load_columns or key_columns) %}s.{{ c }}{{ ' DESC' if load_columns }}{{ ', ' if not loop.last }}{% endfor %}
            ) AS _dedup_row_number
        FROM {{ relation }} AS s
        INNER JOIN (
            SELECT {{ key_columns | join(', ') }}
            FROM {{ relation }}
            WHERE {% for k in key_columns %}{{ k }} IS NOT NULL{{ ' AND ' if not loop.last }}{% endfor %}
            GROUP BY {{ key_columns | join(', ') }}
            HAVING COUNT(DISTINCT {{ version }}) = 1
        ) AS single_version
            ON {% for k in key_columns %}s.{{ k }} = single_version.{{ k }}{{ ' AND ' if not loop.last }}{% endfor %}
    ) AS ranked
    WHERE _dedup_row_number = 1

    UNION ALL

    SELECT {{ columns | join(', ') }}
    FROM {{ relation }}
    WHERE {% for k in key_columns %}{{ k }} IS NULL{{ ' OR ' if not loop.last }}{% endfor %}
{%- endif -%}
{%- endmacro %}
