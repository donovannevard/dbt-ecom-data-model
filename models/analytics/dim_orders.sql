SELECT
    id,
    user_id AS customer_id,
    customer_country_code,
    customer_country_name,
    order_date,
    status,
    currency_code,
    gclid,
    attributed_platform,
    attributed_campaign_id,
    attributed_campaign_name,
    attributed_ad_id,
    attributed_ad_name,
    item_count,
    unit_count,
    order_subtotal
FROM {{ ref('int_order__enriched') }}
