WITH
    order_items AS (
        SELECT * FROM {{ ref('stg_order_items') }}
    ),
    products AS (
        SELECT * FROM {{ ref('int_product__enriched') }}
    ),
    enriched AS (
        SELECT
            oi.id,
            oi.order_id,
            oi.product_id,
            p.name AS product_name,
            p.category AS product_category,
            oi.quantity,
            oi.unit_price,
            p.unit_cost,
            oi.quantity * oi.unit_price AS gross_amount,
            oi.quantity * p.unit_cost AS gross_cost,
            p.currency_code,
            oi._loaded_at
        FROM order_items AS oi
        LEFT JOIN products AS p
            ON oi.product_id = p.id
    )
SELECT * FROM enriched
