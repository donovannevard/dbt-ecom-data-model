/*
The headline marketing question: which channels actually pay for themselves?

Gross ROAS is what the ad platforms report. Net ROAS is after returns and cost
of goods. The verdict column is driven by net ROAS, because a channel above 1.0
gross and below 1.0 net is losing money while appearing to work.

Channels with no attributed revenue (Facebook, Lead Generation) are not
underperforming — they are unattributable here, since attribution is
gclid-based and Google-only. They are labelled separately rather than being
reported as a zero.

Run with:  dbt show --inline "$(cat analysis/channel_performance.sql)" --limit 20
*/

SELECT
    channel_grouping,
    ROUND(spend, 2)                           AS spend,
    clicks,
    attributed_orders                         AS orders,
    ROUND(attributed_gross_revenue, 2)        AS gross_revenue,
    ROUND(attributed_contribution_margin, 2)  AS contribution,
    ROUND(gross_roas, 2)                      AS gross_roas,
    ROUND(net_roas, 2)                        AS net_roas,
    CASE
        WHEN attributed_gross_revenue = 0 THEN 'not attributable (no click id)'
        WHEN net_roas >= 1.5              THEN 'scale up'
        WHEN net_roas >= 1.0              THEN 'profitable'
        ELSE 'losing money after costs'
    END                                       AS verdict
FROM {{ ref('agg_channel') }}
ORDER BY spend DESC
