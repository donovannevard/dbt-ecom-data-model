WITH
    source_data AS (
        SELECT * FROM {{ source('extract', 'facebook_ads_ad_sets') }}
    ),
    renamed AS (
        SELECT
            CAST(id AS INTEGER) AS id,
            CAST(campaign_id AS INTEGER) AS campaign_id,
            name,
            status,
            CAST(daily_budget AS NUMERIC(10, 2)) AS daily_budget,
            _fivetran_synced AS _loaded_at
        FROM source_data
    )
SELECT * FROM renamed
