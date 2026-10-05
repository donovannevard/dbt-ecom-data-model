-- No real stock/inventory source is wired up in this quickstart — this
-- derives product movement from order volume instead. A production setup
-- would join a genuine stock-level source (e.g. Shopify inventory_levels)
-- to get actual on-hand quantities alongside sell-through.
WITH
    products AS (
        SELECT * FROM {{ ref('dim_products') }}
    ),
    transactions AS (
        SELECT * FROM {{ ref('fct_transactions') }}
    ),
    movement AS (
        SELECT
            product_id,
            SUM(quantity) AS units_sold,
            COUNT(DISTINCT order_id) AS orders_containing_product,
            SUM(gross_revenue) AS gross_revenue
        FROM transactions
        GROUP BY product_id
    ),
    enriched AS (
        SELECT
            p.id,
            p.name,
            p.category,
            p.price,
            p.currency_code,
            COALESCE(m.units_sold, 0) AS units_sold,
            COALESCE(m.orders_containing_product, 0) AS orders_containing_product,
            COALESCE(m.gross_revenue, 0) AS gross_revenue
        FROM products AS p
        LEFT JOIN movement AS m
            ON p.id = m.product_id
    )
SELECT * FROM enriched
