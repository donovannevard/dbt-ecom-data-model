WITH
    source_data AS (
        SELECT * FROM {{ source('extract', 'facebook_ads_campaigns') }}
    ),
    renamed AS (
        SELECT
            CAST(id AS INTEGER) AS id,
            name,
            objective,
            status,
            CAST(start_date AS DATE) AS start_date,
            CAST(end_date AS DATE) AS end_date,
            _fivetran_synced AS _loaded_at
        FROM source_data
    )
SELECT * FROM renamed
