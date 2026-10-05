WITH
    orders AS (
        SELECT * FROM {{ ref('stg_orders') }}
    ),
    payments AS (
        SELECT * FROM {{ ref('stg_stripe__payments') }}
    ),
    refunds AS (
        SELECT * FROM {{ ref('stg_stripe__refunds') }}
    ),
    exchange_rates AS (
        SELECT * FROM {{ ref('stg_exchange_rates') }}
    ),
    refund_rollup AS (
        SELECT
            payment_id,
            SUM(amount) AS refund_amount,
            MAX(created_at) AS refund_date
        FROM refunds
        GROUP BY payment_id
    ),
    -- Converts each payment's local-currency amount to the reporting
    -- currency using that day's rate; refunds reuse the same rate as their
    -- parent payment (the common accounting convention, and it sidesteps
    -- needing a second rate lookup for a refund date that could fall
    -- outside the exchange rate seed's covered window).
    payments_with_fx AS (
        SELECT
            p.*,
            CASE
                WHEN p.currency = '{{ var("reporting_currency") }}' THEN 1
                ELSE er.rate
            END AS fx_rate
        FROM payments AS p
        LEFT JOIN exchange_rates AS er
            ON er.base_currency = '{{ var("reporting_currency") }}'
            AND er.target_currency = p.currency
            AND er.date = p.created_at
    ),
    transactions AS (
        SELECT
            o.id AS order_id,
            p.id AS payment_id,
            p.status AS payment_status,
            p.created_at AS payment_date,
            p.currency,
            p.fx_rate,
            p.amount AS gross_amount,
            COALESCE(r.refund_amount, 0) AS refund_amount,
            r.refund_date,
            {{ safe_divide('p.amount', 'p.fx_rate') }} AS gross_amount_reporting,
            {{ safe_divide('COALESCE(r.refund_amount, 0)', 'p.fx_rate') }} AS refund_amount_reporting,
            {{ safe_divide('COALESCE(r.refund_amount, 0)', 'p.amount') }} AS refund_rate
        FROM orders AS o
        LEFT JOIN payments_with_fx AS p
            ON o.id = p.order_id
        LEFT JOIN refund_rollup AS r
            ON p.id = r.payment_id
    ),
    with_net AS (
        SELECT
            *,
            gross_amount_reporting - refund_amount_reporting AS net_amount_reporting
        FROM transactions
    )
SELECT * FROM with_net
