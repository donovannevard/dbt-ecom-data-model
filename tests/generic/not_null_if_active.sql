{% test not_null_if_active(model, column_name, status_column, status_value) %}

SELECT
    *
FROM {{ model }}
WHERE {{ status_column }} = '{{ status_value }}'
AND {{ column_name }} IS NULL

{% endtest %}