SELECT
    id,
    name,
    category,
    price,
    unit_cost,
    unit_gross_profit,
    gross_margin_rate,
    currency_code,
    currency_symbol,
    decimal_places,
    created_at
FROM {{ ref('int_product__enriched') }}
