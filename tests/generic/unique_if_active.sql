{#
    Uniqueness that only applies to rows in a given state — e.g. campaign
    names must be unique among *active* campaigns, but a name may legitimately
    be reused after the original is archived.
#}

{% test unique_if_active(model, column_name, status_column, status_value) %}

SELECT
    {{ column_name }} AS unique_field,
    COUNT(*) AS n_records
FROM {{ model }}
WHERE {{ status_column }} = '{{ status_value }}'
    AND {{ column_name }} IS NOT NULL
GROUP BY {{ column_name }}
HAVING COUNT(*) > 1

{% endtest %}
