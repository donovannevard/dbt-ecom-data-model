{#
    Custom schemas are absolute, not prefixed.

    dbt's default behaviour concatenates the target schema onto any custom
    schema, so `+schema: extract` on a dev target whose schema is `test`
    materialises into `test_extract`. That breaks the demo dataset: the
    `seeds/example/**` CSVs stand in for ELT-landed raw tables, and
    `models/sources.yaml` declares them at `extract` — the schema name a real
    Fivetran/Airbyte landing area actually has. A prefixed `test_extract`
    would never be found.

    This override makes a custom schema mean exactly what it says, which is
    already this project's convention elsewhere: snapshots pin
    `+target_schema: snapshots` regardless of target.

    Consequence, by design: environments are separated by *database*, not by
    schema prefix. Point each target's database at its own warehouse database
    (dev, ci, prod) — see the note on the `ci` target in profiles.yml. Sharing
    one database across targets would let their seeds and snapshot history
    collide.
#}

{% macro generate_schema_name(custom_schema_name, node) -%}

    {%- set default_schema = target.schema -%}

    {%- if custom_schema_name is none -%}
        {{ default_schema }}
    {%- else -%}
        {{ custom_schema_name | trim }}
    {%- endif -%}

{%- endmacro %}
