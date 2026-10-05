WITH
    source_data AS (
        SELECT * FROM {{ source('extract', 'google_ads_clicks') }}
    ),
    renamed AS (
        SELECT
            CAST(id AS INTEGER) AS id,
            CAST(ad_id AS INTEGER) AS ad_id,
            CAST(date AS DATE) AS date,
            CAST(impressions AS INTEGER) AS impressions,
            CAST(clicks AS INTEGER) AS clicks,
            CAST(cost AS NUMERIC(10, 2)) AS cost,
            CAST(conversions AS INTEGER) AS conversions,
            _fivetran_synced AS _loaded_at
        FROM source_data
    )
SELECT * FROM renamed
