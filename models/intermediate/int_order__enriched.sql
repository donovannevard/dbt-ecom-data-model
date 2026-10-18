WITH
    orders AS (
        SELECT * FROM {{ ref('stg_orders') }}
    ),
    customers AS (
        SELECT * FROM {{ ref('int_customer__enriched') }}
    ),
    order_items AS (
        SELECT * FROM {{ ref('int_order_items__enriched') }}
    ),
    google_ads AS (
        SELECT * FROM {{ ref('stg_google_ads__ads') }}
    ),
    google_ad_groups AS (
        SELECT * FROM {{ ref('stg_google_ads__ad_groups') }}
    ),
    google_campaigns AS (
        SELECT * FROM {{ ref('stg_google_ads__campaigns') }}
    ),
    item_rollup AS (
        SELECT
            order_id,
            COUNT(*) AS item_count,
            SUM(quantity) AS unit_count,
            SUM(gross_amount) AS order_subtotal
        FROM order_items
        GROUP BY order_id
    ),
    -- Last-click attribution: an order's gclid (when present) is resolved
    -- back to the Google Ads campaign/ad that drove it via
    -- parse_gclid_ad_id. Orders with no gclid (organic, direct, other
    -- channels, or other ad platforms) simply get NULL attribution here.
    attribution AS (
        SELECT
            o.id AS order_id,
            'google_ads' AS attributed_platform,
            gc.id AS attributed_campaign_id,
            gc.name AS attributed_campaign_name,
            ga.id AS attributed_ad_id,
            ga.headline AS attributed_ad_name
        FROM orders AS o
        INNER JOIN google_ads AS ga
            ON {{ parse_gclid_ad_id('o.gclid') }} = ga.id
        INNER JOIN google_ad_groups AS gag
            ON ga.ad_group_id = gag.id
        INNER JOIN google_campaigns AS gc
            ON gag.campaign_id = gc.id
    ),
    enriched AS (
        SELECT
            o.id,
            o.user_id,
            c.country_code AS customer_country_code,
            c.country_name AS customer_country_name,
            o.order_date,
            o.status,
            o.currency_code,
            o.gclid,
            a.attributed_platform,
            a.attributed_campaign_id,
            a.attributed_campaign_name,
            a.attributed_ad_id,
            a.attributed_ad_name,
            COALESCE(ir.item_count, 0) AS item_count,
            COALESCE(ir.unit_count, 0) AS unit_count,
            COALESCE(ir.order_subtotal, 0) AS order_subtotal,
            o._loaded_at
        FROM orders AS o
        LEFT JOIN customers AS c
            ON o.user_id = c.id
        LEFT JOIN item_rollup AS ir
            ON o.id = ir.order_id
        LEFT JOIN attribution AS a
            ON o.id = a.order_id
    )
SELECT * FROM enriched
