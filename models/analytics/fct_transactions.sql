-- Grain: one row per order item. Refunds happen at the payment (order) level
-- in the source data, so they're prorated across an order's line items by
-- each item's share of the order subtotal. Only orders with a real Stripe
-- payment are included — cancelled orders never had money move, so they're
-- excluded here (they still show up in dim_orders as a dimension).
--
-- The product catalogue (and so order_subtotal / gross_revenue below) is
-- always priced in the reporting currency, but a Stripe payment can be in
-- whatever currency the customer was charged in — so the proration must use
-- int_order__transactions' already-converted refund_amount_reporting, not
-- the raw local-currency refund_amount, or this ratio is comparing two
-- different currencies for any non-reporting-currency customer.
WITH
    order_items AS (
        SELECT * FROM {{ ref('int_order_items__enriched') }}
    ),
    orders AS (
        SELECT * FROM {{ ref('int_order__enriched') }}
    ),
    transactions AS (
        SELECT * FROM {{ ref('int_order__transactions') }}
    ),
    prorated AS (
        SELECT
            oi.id AS order_item_id,
            oi.order_id,
            o.user_id AS customer_id,
            oi.product_id,
            o.order_date,
            oi.quantity,
            oi.unit_price,
            oi.gross_amount AS gross_revenue,
            oi.gross_cost AS gross_cost,
            oi.gross_amount * {{ safe_divide('t.refund_amount_reporting', 'o.order_subtotal') }} AS refund_amount,
            oi.currency_code AS reporting_currency,
            t.currency AS payment_currency,
            o.status AS order_status,
            o.attributed_platform,
            o.attributed_campaign_id,
            o.attributed_campaign_name
        FROM order_items AS oi
        INNER JOIN orders AS o
            ON oi.order_id = o.id
        INNER JOIN transactions AS t
            ON oi.order_id = t.order_id
        WHERE t.payment_id IS NOT NULL
    ),
    fct AS (
        SELECT
            *,
            gross_revenue - refund_amount AS net_revenue,

            -- Cost of goods is charged on units actually kept. A returned
            -- item comes back into stock, so its cost is credited back with
            -- its revenue — charging full COGS against refunded revenue
            -- would understate contribution on any line with a return.
            --
            -- The retained fraction is floored at zero. A fully refunded
            -- foreign-currency order can prorate to marginally *more* than
            -- 100% refunded (the Stripe charge is rounded to 2dp in the
            -- customer's currency, so converting back cannot exactly recover
            -- the original), and without the floor that turns into a negative
            -- cost of goods that credits contribution upwards.
            gross_cost * GREATEST(1 - {{ safe_divide('refund_amount', 'gross_revenue') }}, 0) AS cogs,

            -- Contribution after returns and cost of goods: what a channel
            -- actually contributes before fixed costs, and the correct
            -- denominator-side figure for a margin-aware ROAS.
            (gross_revenue - refund_amount)
                - gross_cost * GREATEST(1 - {{ safe_divide('refund_amount', 'gross_revenue') }}, 0)
                AS contribution_margin
        FROM prorated
    )
SELECT * FROM fct
