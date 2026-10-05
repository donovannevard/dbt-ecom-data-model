WITH
    marketing AS (
        SELECT * FROM {{ ref('int_marketing__combined') }}
    ),
    channels AS (
        SELECT * FROM {{ ref('int_marketing__channel_classification') }}
    ),
    fct AS (
        SELECT
            m.platform,
            m.campaign_id,
            m.campaign_name,
            c.channel_grouping,
            c.subchannel,
            m.ad_id,
            m.ad_name,
            m.date,
            m.impressions,
            m.clicks,
            m.cost AS spend,
            m.conversions,
            {{ safe_divide('m.clicks', 'm.impressions') }} AS ctr,
            {{ safe_divide('m.cost', 'm.clicks') }} AS cpc
        FROM marketing AS m
        LEFT JOIN channels AS c
            ON m.platform = c.platform
            AND m.campaign_id = c.campaign_id
    )
SELECT * FROM fct
