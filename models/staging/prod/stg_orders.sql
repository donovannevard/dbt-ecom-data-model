WITH
    source_data AS (
        SELECT * FROM {{ source('extract', 'prod_orders') }}
    ),
    renamed AS (
        SELECT
            CAST(id AS INTEGER) AS id,
            CAST(user_id AS INTEGER) AS user_id,
            CAST(order_date AS DATE) AS order_date,
            status,
            currency_code,
            NULLIF(gclid, '') AS gclid,
            _fivetran_synced AS _loaded_at
        FROM source_data
    )
SELECT * FROM renamed
