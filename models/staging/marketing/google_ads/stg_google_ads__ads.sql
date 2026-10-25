WITH
    source_data AS (
        {{ deduplicate_source(source('extract', 'google_ads_ads'), ['id']) }}
    ),
    renamed AS (
        SELECT
            CAST(id AS INTEGER) AS id,
            CAST(ad_group_id AS INTEGER) AS ad_group_id,
            headline,
            status,
            ad_type,
            _fivetran_synced AS _loaded_at
        FROM source_data
    )
SELECT * FROM renamed
