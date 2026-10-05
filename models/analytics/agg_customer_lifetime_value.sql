-- Historical LTV (lifetime_value) is the actual, observed net revenue to
-- date. projected_value_1y/3y/5y is a simple run-rate projection — average
-- order value times purchase frequency, held constant forward — not a
-- churn-adjusted or probabilistic model (e.g. BG/NBD). It's meant as a
-- generic baseline every retaining-customer business can use immediately;
-- swap in a proper retention-curve or cohort-decay model per client once
-- there's enough order history to fit one.
WITH
    customers AS (
        SELECT * FROM {{ ref('dim_customers') }}
    ),
    transactions AS (
        SELECT * FROM {{ ref('fct_transactions') }}
    ),
    ltv AS (
        SELECT
            customer_id,
            COUNT(DISTINCT order_id) AS order_count,
            SUM(gross_revenue) AS lifetime_gross_revenue,
            SUM(refund_amount) AS lifetime_refund_amount,
            SUM(net_revenue) AS lifetime_value,
            MIN(order_date) AS first_order_date,
            MAX(order_date) AS most_recent_order_date
        FROM transactions
        GROUP BY customer_id
    ),
    enriched AS (
        SELECT
            c.id,
            c.first_name,
            c.last_name,
            c.email,
            c.country_name,
            c.home_currency_code,
            COALESCE(l.order_count, 0) AS order_count,
            COALESCE(l.lifetime_gross_revenue, 0) AS lifetime_gross_revenue,
            COALESCE(l.lifetime_refund_amount, 0) AS lifetime_refund_amount,
            COALESCE(l.lifetime_value, 0) AS lifetime_value,
            l.first_order_date,
            l.most_recent_order_date
        FROM customers AS c
        LEFT JOIN ltv AS l
            ON c.id = l.customer_id
    ),
    -- Floored at 1 year so a brand-new or single-order customer doesn't
    -- produce an absurd, near-divide-by-zero purchase frequency.
    tenure AS (
        SELECT
            *,
            GREATEST(most_recent_order_date - first_order_date, 0) AS tenure_days
        FROM enriched
    ),
    behavior AS (
        SELECT
            *,
            GREATEST(tenure_days / 365.0, 1) AS tenure_years,
            {{ safe_divide('lifetime_value', 'order_count') }} AS avg_order_value
        FROM tenure
    ),
    frequency AS (
        SELECT
            *,
            {{ safe_divide('order_count', 'tenure_years') }} AS purchase_frequency_per_year
        FROM behavior
    ),
    projected AS (
        SELECT
            *,
            -- Floored at zero. A customer whose refunds equal or exceed their
            -- purchases has no projected future value, and running a negative
            -- rate forward five years is meaningless rather than merely
            -- pessimistic.
            --
            -- This is not hypothetical: a fully refunded order placed in a
            -- foreign currency nets slightly *below* zero, because the Stripe
            -- charge is rounded to 2dp in the customer's currency and
            -- converting it back at the same day's rate cannot exactly
            -- recover the original GBP amount. lifetime_value keeps that
            -- small negative, because it is what actually happened; the
            -- forward projection does not inherit it.
            GREATEST(avg_order_value * purchase_frequency_per_year, 0) AS projected_annual_value
        FROM frequency
    ),
    final AS (
        SELECT
            id,
            first_name,
            last_name,
            email,
            country_name,
            home_currency_code,
            order_count,
            lifetime_gross_revenue,
            lifetime_refund_amount,
            lifetime_value,
            first_order_date,
            most_recent_order_date,
            avg_order_value,
            purchase_frequency_per_year,
            projected_annual_value * 1 AS projected_value_1y,
            projected_annual_value * 3 AS projected_value_3y,
            projected_annual_value * 5 AS projected_value_5y
        FROM projected
    )
SELECT * FROM final
