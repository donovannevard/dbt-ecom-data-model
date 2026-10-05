WITH
    source_data AS (
        SELECT * FROM {{ source('extract', 'stripe_refunds') }}
    ),
    renamed AS (
        SELECT
            id,
            payment_id,
            CAST(amount AS NUMERIC(10, 2)) AS amount,
            reason,
            CAST(created_at AS DATE) AS created_at,
            _fivetran_synced AS _loaded_at
        FROM source_data
    )
SELECT * FROM renamed
