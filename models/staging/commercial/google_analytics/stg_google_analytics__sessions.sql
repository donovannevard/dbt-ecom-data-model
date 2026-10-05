WITH
    source_data AS (
        SELECT * FROM {{ source('extract', 'google_analytics_sessions') }}
    ),
    renamed AS (
        SELECT
            CAST(id AS INTEGER) AS session_id,
            CAST(session_date AS DATE) AS session_date,
            source,
            medium,
            campaign,
            device_category,
            CAST(pageviews AS INTEGER) AS pageviews,
            CAST(session_duration_seconds AS INTEGER) AS session_duration_seconds,
            _fivetran_synced AS _loaded_at
        FROM source_data
    )
SELECT * FROM renamed
