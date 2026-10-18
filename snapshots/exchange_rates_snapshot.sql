{% snapshot currency_rates_snapshot %}

{#
    Timestamp strategy: stg_exchange_rates carries a real load timestamp
    (`_loaded_at`, renamed from the ELT tool's `_fivetran_synced`), so a
    changed rate is detectable without comparing every column.
#}

{{ config(
    unique_key=['date', 'base_currency', 'target_currency'],
    strategy='timestamp',
    updated_at='_loaded_at',
    invalidate_hard_deletes=True
) }}

SELECT
    CAST(date AS DATE) AS date,
    base_currency,
    target_currency,
    rate AS exchange_rate,
    _loaded_at
FROM {{ ref('stg_exchange_rates') }}

{% endsnapshot %}
