SELECT
    id,
    order_id,
    product_id,
    product_name,
    product_category,
    quantity,
    unit_price,
    gross_amount,
    currency_code
FROM {{ ref('int_order_items__enriched') }}
