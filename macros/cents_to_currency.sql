-- Many payment processors (Stripe included) land amounts in minor units
-- (pence/cents). This project's demo seed data already stores decimal
-- major-unit amounts for readability (see the note in
-- stg_stripe__payments.sql), so it isn't used here — but real client data
-- almost always needs it.
{% macro cents_to_currency(column) %}
    ({{ column }} / 100.0)
{% endmacro %}
