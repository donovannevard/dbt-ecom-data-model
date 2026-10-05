-- Facebook's "ads" source carries daily performance directly (one row per
-- ad per day) since there's no separate stats/insights source declared —
-- unlike Google Ads, where stg_google_ads__ads is a pure dimension table
-- and stg_google_ads__clicks holds the daily performance grain.
WITH
    source_data AS (
        SELECT * FROM {{ source('extract', 'facebook_ads_ads') }}
    ),
    renamed AS (
        SELECT
            CAST(id AS INTEGER) AS id,
            CAST(ad_id AS INTEGER) AS ad_id,
            CAST(ad_set_id AS INTEGER) AS ad_set_id,
            CAST(date AS DATE) AS date,
            name,
            status,
            CAST(impressions AS INTEGER) AS impressions,
            CAST(clicks AS INTEGER) AS clicks,
            CAST(spend AS NUMERIC(10, 2)) AS spend,
            CAST(conversions AS INTEGER) AS conversions,
            _fivetran_synced AS _loaded_at
        FROM source_data
    )
SELECT * FROM renamed
