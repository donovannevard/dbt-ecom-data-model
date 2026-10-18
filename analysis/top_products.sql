/*
Products ranked by contribution, with margin and return rate.

The interesting output is the disagreement between the revenue ranking and the
contribution ranking: a high-revenue product on a thin margin, or one with a
heavy return rate, can sit well down this list.

Run with:  dbt show --inline "$(cat analysis/top_products.sql)" --limit 20   (the --limit is what caps the rows)
*/

SELECT
    t.product_id,
    p.name                               AS product_name,
    p.category,
    SUM(t.quantity)                      AS units_sold,
    ROUND(SUM(t.gross_revenue), 2)       AS gross_revenue,
    ROUND(SUM(t.contribution_margin), 2) AS contribution,
    ROUND(100.0 * p.gross_margin_rate, 1) AS list_margin_pct,
    ROUND(100.0 * {{ safe_divide('SUM(t.refund_amount)', 'SUM(t.gross_revenue)') }}, 1) AS return_rate_pct
FROM {{ ref('fct_transactions') }} AS t
LEFT JOIN {{ ref('dim_products') }} AS p
    ON t.product_id = p.id
GROUP BY 1, 2, 3, p.gross_margin_rate
ORDER BY contribution DESC
