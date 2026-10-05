WITH
    source_data AS (
        SELECT * FROM {{ source('extract', 'exchange_rates') }}
    ),
    renamed AS (
        SELECT
            CAST(date AS DATE) AS date,
            base_currency,
            target_currency,
            CAST(rate AS NUMERIC(10, 4)) AS rate,
            _fivetran_synced AS _loaded_at
        FROM source_data
    )
SELECT * FROM renamed
