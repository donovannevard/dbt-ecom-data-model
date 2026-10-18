-- Order-grain rollup of fct_transactions (which is order-item grain).
--
-- NB: the CTE below is `order_rollup`, not `rollup`. ROLLUP is a reserved
-- keyword in BigQuery (GROUP BY ROLLUP(...)), so a CTE of that name fails to
-- parse there with "Expected keyword SELECT but got keyword ROLLUP", while
-- DuckDB, Snowflake and Redshift all accept it happily.
WITH
    transactions AS (
        SELECT * FROM {{ ref('fct_transactions') }}
    ),
    order_rollup AS (
        SELECT
            order_id AS id,
            customer_id,
            order_date,
            order_status,
            reporting_currency,
            COUNT(*) AS item_count,
            SUM(quantity) AS unit_count,
            SUM(gross_revenue) AS gross_revenue,
            SUM(refund_amount) AS refund_amount,
            SUM(net_revenue) AS net_revenue
        FROM transactions
        GROUP BY order_id, customer_id, order_date, order_status, reporting_currency
    )
SELECT * FROM order_rollup
