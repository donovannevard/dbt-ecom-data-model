{#
    Keys with two or more conflicting versions of the same record: the rows
    deduplicate_source() excludes from staging. With store_failures on (the
    project default) the failing rows are kept in dbt_test__audit, which makes
    this test's stored failures the quarantine table for that source.

    Declare it with severity: warn, as models/sources.yaml does: a conflict
    should be surfaced and resolved upstream, not stop every model downstream.
    Identical re-delivered copies are not conflicts and do not fail this test;
    a unique test on the same key alerts on those.
#}

{% test no_conflicting_duplicates(model, key_columns, ignore_columns=['_fivetran_synced']) %}
{%- if execute -%}
    {%- set ignored = ignore_columns | map('lower') | list -%}
    {%- set compared = [] -%}
    {%- for column in adapter.get_columns_in_relation(model) -%}
        {%- if column.name | lower not in ignored -%}{%- do compared.append(column.name) -%}{%- endif -%}
    {%- endfor -%}
{%- else -%}
    {%- set compared = key_columns -%}
{%- endif -%}

SELECT r.*
FROM {{ model }} AS r
INNER JOIN (
    SELECT {{ key_columns | join(', ') }}
    FROM {{ model }}
    WHERE {% for k in key_columns %}{{ k }} IS NOT NULL{{ ' AND ' if not loop.last }}{% endfor %}
    GROUP BY {{ key_columns | join(', ') }}
    HAVING COUNT(DISTINCT {{ dbt_utils.generate_surrogate_key(compared) }}) > 1
) AS conflicts
    ON {% for k in key_columns %}r.{{ k }} = conflicts.{{ k }}{{ ' AND ' if not loop.last }}{% endfor %}

{% endtest %}
