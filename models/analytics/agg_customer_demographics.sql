WITH
    customers AS (
        SELECT * FROM {{ ref('dim_customers') }}
    ),
    enriched AS (
        SELECT
            id,
            first_name,
            last_name,
            email,
            country_code,
            country_name,
            region,
            sub_region,
            home_currency_code,
            timezone_name,
            COUNT(*) OVER (PARTITION BY country_code) AS customers_in_country,
            COUNT(*) OVER (PARTITION BY region) AS customers_in_region
        FROM customers
    )
SELECT * FROM enriched
