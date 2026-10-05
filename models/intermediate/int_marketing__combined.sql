-- Google Ads and Facebook Ads land at different natural grains (Google's
-- daily performance lives in a separate `clicks` table keyed by ad_id;
-- Facebook's is embedded directly on `ads`, one row per ad per day) — this
-- model walks each platform's own campaign hierarchy up to a common,
-- platform-agnostic shape before combining them.
WITH
    google_clicks AS (
        SELECT * FROM {{ ref('stg_google_ads__clicks') }}
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
    google_performance AS (
        SELECT
            'google_ads' AS platform,
            gc.id AS campaign_id,
            gc.name AS campaign_name,
            ga.id AS ad_id,
            ga.headline AS ad_name,
            gcl.date AS date,
            gcl.impressions,
            gcl.clicks,
            gcl.cost,
            gcl.conversions
        FROM google_clicks AS gcl
        INNER JOIN google_ads AS ga ON gcl.ad_id = ga.id
        INNER JOIN google_ad_groups AS gag ON ga.ad_group_id = gag.id
        INNER JOIN google_campaigns AS gc ON gag.campaign_id = gc.id
    ),
    facebook_ads AS (
        SELECT * FROM {{ ref('stg_facebook_ads__ads') }}
    ),
    facebook_ad_sets AS (
        SELECT * FROM {{ ref('stg_facebook_ads__ad_sets') }}
    ),
    facebook_campaigns AS (
        SELECT * FROM {{ ref('stg_facebook_ads__campaigns') }}
    ),
    facebook_performance AS (
        SELECT
            'facebook_ads' AS platform,
            fc.id AS campaign_id,
            fc.name AS campaign_name,
            fa.ad_id AS ad_id,
            fa.name AS ad_name,
            fa.date AS date,
            fa.impressions,
            fa.clicks,
            fa.spend AS cost,
            fa.conversions
        FROM facebook_ads AS fa
        INNER JOIN facebook_ad_sets AS fas ON fa.ad_set_id = fas.id
        INNER JOIN facebook_campaigns AS fc ON fas.campaign_id = fc.id
    ),
    combined AS (
        SELECT * FROM google_performance
        UNION ALL
        SELECT * FROM facebook_performance
    )
SELECT * FROM combined
