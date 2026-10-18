{% test accepted_values_if_status(model, column_name, values, status_column, status_value) %}

SELECT
    *
FROM {{ model }}
WHERE {{ status_column }} = '{{ status_value }}'
AND {{ column_name }} NOT IN ({% for value in values %}'{{ value }}'{% if not loop.last %}, {% endif %}{% endfor %})

{% endtest %}