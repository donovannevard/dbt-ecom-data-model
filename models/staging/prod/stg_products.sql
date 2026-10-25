WITH
    source_data AS (
        {{ deduplicate_source(source('extract', 'prod_products'), ['id']) }}
    ),
    renamed AS (
        SELECT
            CAST(id AS INTEGER) AS id,
            name,
            category,
            CAST(price AS {{ numeric_type(10, 2) }}) AS price,
            CAST(unit_cost AS {{ numeric_type(10, 2) }}) AS unit_cost,
            currency_code,
            CAST(created_at AS DATE) AS created_at,
            _fivetran_synced AS _loaded_at
        FROM source_data
    )
SELECT * FROM renamed
