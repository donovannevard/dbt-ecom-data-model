WITH
    customers AS (
        SELECT * FROM {{ ref('stg_customers') }}
    ),
    countries AS (
        SELECT * FROM {{ ref('country_codes') }}
    ),
    enriched AS (
        SELECT
            c.id,
            c.first_name,
            c.last_name,
            c.email,
            c.country_code,
            co.country_name,
            co.region,
            co.sub_region,
            co.currency_code AS home_currency_code,
            co.timezone_name,
            c.created_at,
            c._loaded_at
        FROM customers AS c
        LEFT JOIN countries AS co
            ON c.country_code = co.alpha_2
    )
SELECT * FROM enriched
