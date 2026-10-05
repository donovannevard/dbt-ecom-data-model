{#
    "Does this string contain a match for this regex", where the pattern is
    supplied as a Jinja string and compiles to a SQL *literal*.

    The pattern cannot be a column reference, and that is a hard constraint
    rather than a style choice. Redshift rejects a non-constant regex pattern
    outright: `~`, REGEXP_INSTR, REGEXP_COUNT and SIMILAR TO all fail with
    "The pattern must be a valid UTF-8 literal character expression" when the
    pattern comes from a joined column, because the predicate executes on
    compute nodes where it cannot be constant-folded. All four were tested
    against a live cluster; none works. DuckDB and Snowflake accept a column
    pattern happily, which is exactly how this went unnoticed until a real
    Redshift run.

    That is why the channel taxonomy is compiled into a CASE expression of
    literal patterns -- see classify_by_taxonomy.sql -- rather than joined on.

    Dialects: Snowflake spells it REGEXP_LIKE; DuckDB and Redshift use the
    POSIX `~` operator. All three anchor to the whole string, so the pattern is
    padded with .* on both sides -- and *grouped* while padding, because
    '.*' || 'a|b' || '.*' parses as (.*a)|(b.*) and silently matches nothing.
#}

{% macro regexp_contains(subject, pattern_text) -%}
    {%- set literal = "'.*(" ~ (pattern_text | replace("'", "''")) ~ ").*'" -%}
    {{ return(adapter.dispatch('regexp_contains', 'dbt_ecom_data_model')(subject, literal)) }}
{%- endmacro %}

{% macro default__regexp_contains(subject, literal) -%}
    {{ subject }} ~ {{ literal }}
{%- endmacro %}

{% macro snowflake__regexp_contains(subject, literal) -%}
    REGEXP_LIKE({{ subject }}, {{ literal }})
{%- endmacro %}
