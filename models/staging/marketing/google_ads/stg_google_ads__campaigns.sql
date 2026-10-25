WITH
    source_data AS (
        {{ deduplicate_source(source('extract', 'google_ads_campaigns'), ['id']) }}
    ),
    renamed AS (
        SELECT
            CAST(id AS INTEGER) AS id,
            name,
            status,
            campaign_type,
            CAST(start_date AS DATE) AS start_date,
            CAST(end_date AS DATE) AS end_date,
            _fivetran_synced AS _loaded_at
        FROM source_data
    )
SELECT * FROM renamed
