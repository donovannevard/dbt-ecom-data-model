/*
Top 20 customers by contribution, not by revenue.

Ranking customers on gross revenue flatters the ones who buy a lot and return
a lot. This ranks on what each customer actually contributed after their own
returns and the cost of what they kept, and shows the return rate alongside so
a high-revenue/high-return customer is visible rather than hidden.

Run with:  dbt show --inline "$(cat analysis/top_customers.sql)" --limit 20   (the --limit is what caps the rows)
*/

SELECT
    t.customer_id,
    c.first_name || ' ' || c.last_name AS customer_name,
    c.country_name,
    COUNT(DISTINCT t.order_id)         AS orders,
    ROUND(SUM(t.gross_revenue), 2)     AS gross_revenue,
    ROUND(SUM(t.refund_amount), 2)     AS refunds,
    ROUND(SUM(t.cogs), 2)              AS cogs,
    ROUND(SUM(t.contribution_margin), 2) AS contribution,
    ROUND(100.0 * {{ safe_divide('SUM(t.refund_amount)', 'SUM(t.gross_revenue)') }}, 1) AS return_rate_pct
FROM {{ ref('fct_transactions') }} AS t
LEFT JOIN {{ ref('dim_customers') }} AS c
    ON t.customer_id = c.id
GROUP BY 1, 2, 3
ORDER BY contribution DESC
