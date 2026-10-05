-- Snapshotted daily by snapshots/products_snapshot.sql to build SCD Type 2
-- price history, so this always reflects "today's" product catalogue state.
SELECT
    CURRENT_DATE AS date,
    id,
    name,
    category,
    price,
    currency_code
FROM {{ ref('dim_products') }}
