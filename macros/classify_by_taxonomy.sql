{#
    Compiles seeds/marketing_channel_taxonomy.csv into a CASE expression.

    The obvious implementation is a join: cross the rows against the taxonomy
    seed and match each campaign name against campaign_name_pattern. That works
    on DuckDB and Snowflake and cannot work on Redshift, which requires a regex
    pattern to be a constant (see regexp_contains.sql). Reading the seed at
    compile time and emitting one literal WHEN branch per pattern works
    everywhere.

    Two things fall out of it for free:

    - No ROW_NUMBER / "longest pattern wins" window. Branches are emitted
      longest-pattern-first and CASE stops at the first match, so the most
      specific pattern wins by construction.
    - No join, so the row count cannot change if two patterns match.

    The cost is that the taxonomy is resolved when the model compiles, not when
    it runs. Editing the CSV therefore needs `dbt seed` *and* a rebuild of the
    models -- which was already true, since the models had to re-run to pick up
    a changed classification anyway.
#}

{% macro taxonomy_patterns() %}
    {#- During parsing there is no warehouse connection; return nothing so the
        model still compiles for `dbt parse`. At run time execute is true. -#}
    {% if not execute %}
        {{ return([]) }}
    {% endif %}

    {% set query %}
        SELECT campaign_name_pattern, channel, subchannel
        FROM {{ ref('marketing_channel_taxonomy') }}
    {% endset %}

    {% set rows = run_query(query).rows %}

    {#- Longest pattern first, so CASE resolves to the most specific match. -#}
    {% set ordered = [] %}
    {% for row in rows %}
        {% do ordered.append((row[0] | length, row[0], row[1], row[2])) %}
    {% endfor %}

    {{ return(ordered | sort(attribute='0', reverse=True)) }}
{% endmacro %}


{% macro taxonomy_case(subject, field) %}
    {%- set index = 2 if field == 'channel' else 3 -%}
    {%- set rows = taxonomy_patterns() -%}
    {%- if not rows -%}
        CAST(NULL AS VARCHAR)
    {%- else -%}
        CASE
        {%- for row in rows %}
            WHEN {{ regexp_contains(subject, row[1]) }} THEN '{{ row[index] | replace("'", "''") }}'
        {%- endfor %}
        END
    {%- endif -%}
{% endmacro %}
