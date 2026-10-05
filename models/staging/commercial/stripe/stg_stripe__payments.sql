-- NOTE: real Stripe amounts arrive in minor units (pence), so a production
-- staging model would need `amount / 100.0`. This project's example seed
-- data already stores decimal major-unit amounts for readability, so no
-- conversion is applied here — check your actual raw data before copying.
WITH
    source_data AS (
        SELECT * FROM {{ source('extract', 'stripe_payments') }}
    ),
    renamed AS (
        SELECT
            id,
            CAST(order_id AS INTEGER) AS order_id,
            CAST(amount AS NUMERIC(10, 2)) AS amount,
            UPPER(currency) AS currency,
            status,
            CAST(created_at AS DATE) AS created_at,
            _fivetran_synced AS _loaded_at
        FROM source_data
    )
SELECT * FROM renamed
