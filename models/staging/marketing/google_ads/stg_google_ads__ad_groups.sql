WITH
    source_data AS (
        {{ deduplicate_source(source('extract', 'google_ads_ad_groups'), ['id']) }}
    ),
    renamed AS (
        SELECT
            CAST(id AS INTEGER) AS id,
            CAST(campaign_id AS INTEGER) AS campaign_id,
            name,
            status,
            _fivetran_synced AS _loaded_at
        FROM source_data
    )
SELECT * FROM renamed
