{#
    Monday-start week boundaries, identical on every warehouse.

    The obvious implementation -- date_trunc('week', d) with an offset -- is not
    portable, because what date_trunc('week') returns is warehouse-defined:

      DuckDB / Postgres / Redshift : Monday
      BigQuery                     : Sunday
      Snowflake                    : follows the WEEK_START parameter

    The original model truncated and added a day, which produced Tuesday on
    DuckDB and would have produced Monday on BigQuery. Same SQL, different
    answers, and no test caught it because every value was still a plausible
    date.

    Deriving the offset from day_of_week_number instead -- itself normalised to
    0 = Sunday on all four adapters -- removes the warehouse dependency
    entirely. days_since_monday = MOD(dow + 6, 7), so Monday maps to 0 and
    Sunday to 6, which is the ISO-8601 convention and the one UK and European
    reporting expects.
#}

{% macro week_start_monday(subject) -%}
    {%- set days_since_monday = '(0 - MOD(' ~ day_of_week_number(subject) ~ ' + 6, 7))' -%}
    CAST({{ dbt.dateadd('day', days_since_monday, subject) }} AS DATE)
{%- endmacro %}


{% macro week_end_sunday(subject) -%}
    {%- set days_until_sunday = '(6 - MOD(' ~ day_of_week_number(subject) ~ ' + 6, 7))' -%}
    CAST({{ dbt.dateadd('day', days_until_sunday, subject) }} AS DATE)
{%- endmacro %}
