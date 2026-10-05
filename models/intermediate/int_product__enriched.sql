WITH
    products AS (
        SELECT * FROM {{ ref('stg_products') }}
    ),
    currencies AS (
        SELECT * FROM {{ ref('currency_info') }}
    ),
    enriched AS (
        SELECT
            p.id,
            p.name,
            p.category,
            p.price,
            p.unit_cost,
            p.price - p.unit_cost AS unit_gross_profit,
            {{ safe_divide('p.price - p.unit_cost', 'p.price') }} AS gross_margin_rate,
            p.currency_code,
            cu.currency_symbol,
            cu.decimal_places,
            p.created_at,
            p._loaded_at
        FROM products AS p
        LEFT JOIN currencies AS cu
            ON p.currency_code = cu.currency_code
    )
SELECT * FROM enriched
