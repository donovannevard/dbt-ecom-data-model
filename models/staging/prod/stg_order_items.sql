WITH
    source_data AS (
        SELECT * FROM {{ source('extract', 'prod_order_items') }}
    ),
    renamed AS (
        SELECT
            CAST(id AS INTEGER) AS id,
            CAST(order_id AS INTEGER) AS order_id,
            CAST(product_id AS INTEGER) AS product_id,
            CAST(quantity AS INTEGER) AS quantity,
            CAST(unit_price AS NUMERIC(10, 2)) AS unit_price,
            _fivetran_synced AS _loaded_at
        FROM source_data
    )
SELECT * FROM renamed
