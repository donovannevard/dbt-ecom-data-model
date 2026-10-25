WITH
    source_data AS (
        {{ deduplicate_source(source('extract', 'prod_customers'), ['id']) }}
    ),
    renamed AS (
        SELECT
            CAST(id AS INTEGER) AS id,
            first_name,
            last_name,
            email,
            country_code,
            CAST(created_at AS DATE) AS created_at,
            _fivetran_synced AS _loaded_at
        FROM source_data
    )
SELECT * FROM renamed
