WITH
    source_data AS (
        SELECT * FROM {{ source('extract', 'prod_web_visits') }}
    ),
    renamed AS (
        SELECT
            CAST(id AS INTEGER) AS id,
            CAST(visited_at AS TIMESTAMP) AS visited_at,
            -- Already a nullable integer in the source (see
            -- seeds/example/example_seeds.yaml); anonymous visits arrive as
            -- NULL, so this is a type assertion, not a rescue cast.
            CAST(customer_id AS INTEGER) AS customer_id,
            NULLIF(gclid, '') AS gclid,
            landing_page,
            device_category,
            _fivetran_synced AS _loaded_at
        FROM source_data
    )
SELECT * FROM renamed
