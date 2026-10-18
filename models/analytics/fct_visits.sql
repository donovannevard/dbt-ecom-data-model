-- Incremental by design: web visits are a raw, ever-growing event log (one
-- row per pageview-landing event), unlike GA4's pre-aggregated sessions in
-- fct_sessions. This is the reference incremental fact table for the
-- project — see dbt_project.yml's note on setting materialized='incremental'
-- per-model rather than via a (non-functional) project-level name pattern.
{{ config(
    materialized='incremental',
    unique_key='visit_id',
    incremental_strategy='merge'
) }}

WITH
    visits AS (
        SELECT * FROM {{ ref('stg_web_visits') }}
        {% if is_incremental() %}
        WHERE visited_at > (SELECT MAX(visited_at) FROM {{ this }})
        {% endif %}
    ),
    google_ads AS (
        SELECT * FROM {{ ref('stg_google_ads__ads') }}
    ),
    google_ad_groups AS (
        SELECT * FROM {{ ref('stg_google_ads__ad_groups') }}
    ),
    google_campaigns AS (
        SELECT * FROM {{ ref('stg_google_ads__campaigns') }}
    ),
    attribution AS (
        SELECT
            v.id AS visit_id,
            'google_ads' AS attributed_platform,
            gc.id AS attributed_campaign_id,
            gc.name AS attributed_campaign_name,
            ga.id AS attributed_ad_id,
            ga.headline AS attributed_ad_name
        FROM visits AS v
        INNER JOIN google_ads AS ga
            ON {{ parse_gclid_ad_id('v.gclid') }} = ga.id
        INNER JOIN google_ad_groups AS gag
            ON ga.ad_group_id = gag.id
        INNER JOIN google_campaigns AS gc
            ON gag.campaign_id = gc.id
    ),
    fct AS (
        SELECT
            v.id AS visit_id,
            v.visited_at,
            v.customer_id,
            v.gclid,
            a.attributed_platform,
            a.attributed_campaign_id,
            a.attributed_campaign_name,
            a.attributed_ad_id,
            a.attributed_ad_name,
            v.landing_page,
            v.device_category,
            v._loaded_at
        FROM visits AS v
        LEFT JOIN attribution AS a
            ON v.id = a.visit_id
    )
SELECT * FROM fct
