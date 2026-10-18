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
    created_at
FROM {{ ref('int_customer__enriched') }}
