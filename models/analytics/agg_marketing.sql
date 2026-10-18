-- Campaign-grain rollup of fct_marketing (ad + date grain). campaign_id
-- alone isn't unique across platforms (Google and Facebook both start
-- numbering from 1), so the surrogate key is platform-qualified.
WITH
    marketing AS (
        SELECT * FROM {{ ref('fct_marketing') }}
    ),
    campaign_rollup AS (
        SELECT
            platform,
            campaign_id,
            campaign_name,
            SUM(impressions) AS impressions,
            SUM(clicks) AS clicks,
            SUM(spend) AS spend,
            SUM(conversions) AS conversions,
            {{ safe_divide('SUM(clicks)', 'SUM(impressions)') }} AS ctr,
            {{ safe_divide('SUM(spend)', 'SUM(clicks)') }} AS cpc,
            {{ safe_divide('SUM(spend)', 'SUM(conversions)') }} AS cost_per_conversion
        FROM marketing
        GROUP BY platform, campaign_id, campaign_name
    ),
    with_id AS (
        SELECT
            {{ dbt_utils.generate_surrogate_key(['platform', 'campaign_id']) }} AS id,
            *
        FROM campaign_rollup
    )
SELECT * FROM with_id
