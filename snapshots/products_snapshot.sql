{% snapshot products_snapshot %}

{#
    Check strategy, not timestamp: agg_products is a daily snapshot of the
    current catalogue (its `date` is CURRENT_DATE), so a timestamp strategy
    would cut a new SCD2 version every single day whether or not the price
    moved. Comparing `price` directly records a new version only on an
    actual price change, which is the history this table exists to hold.
#}

{{ config(
    unique_key='id',
    strategy='check',
    check_cols=['price'],
    invalidate_hard_deletes=True
) }}

SELECT
    id,
    price,
    CAST(date AS DATE) AS date
FROM {{ ref('agg_products') }}

{% endsnapshot %}
