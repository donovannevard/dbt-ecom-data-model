-- Period-over-period change as a ratio (0.10 = +10%), for MoM/YoY-style
-- comparisons. Wraps safe_divide so a zero/NULL prior period returns NULL
-- rather than erroring or a misleading infinite/zero result.
{% macro pct_change(current_column, previous_column) %}
    {{ safe_divide(current_column ~ ' - (' ~ previous_column ~ ')', previous_column) }}
{% endmacro %}
