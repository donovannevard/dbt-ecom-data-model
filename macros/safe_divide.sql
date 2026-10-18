{% macro safe_divide(numerator, denominator) %}
    CASE
        WHEN ({{ denominator }}) = 0 THEN NULL
        ELSE ({{ numerator }}) / ({{ denominator }})
    END
{% endmacro %}