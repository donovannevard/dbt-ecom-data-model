-- Channel-grain rollup of marketing spend and (where attributable) revenue.
--
-- Spend/clicks/impressions are complete across every platform and campaign,
-- since channel classification runs off the campaign name (see
-- int_marketing__channel_classification), not a click id.
--
-- Attributed revenue is gclid-based (see int_order__enriched), which only
-- covers Google Ads-driven orders in this demo — Facebook and organic/
-- email/referral revenue isn't attributed to a channel here, since there's
-- no Facebook click-id equivalent or session-to-order join wired up yet.
-- roas/roi will be understated for channels without attributed revenue, not
-- wrong — treat this as the spend side being complete and the revenue side
-- being a worked example to extend, not a finished blended view.
WITH
    marketing AS (
        SELECT * FROM {{ ref('fct_marketing') }}
    ),
    spend_rollup AS (
        SELECT
            channel_grouping,
            SUM(impressions) AS impressions,
            SUM(clicks) AS clicks,
            SUM(spend) AS spend,
            SUM(conversions) AS conversions
        FROM marketing
        GROUP BY channel_grouping
    ),
    transactions AS (
        SELECT * FROM {{ ref('fct_transactions') }}
    ),
    channels AS (
        SELECT * FROM {{ ref('int_marketing__channel_classification') }}
    ),
    revenue_rollup AS (
        SELECT
            c.channel_grouping,
            COUNT(DISTINCT t.order_id) AS attributed_orders,
            SUM(t.gross_revenue) AS attributed_gross_revenue,
            SUM(t.net_revenue) AS attributed_net_revenue,
            SUM(t.cogs) AS attributed_cogs,
            SUM(t.contribution_margin) AS attributed_contribution_margin
        FROM transactions AS t
        INNER JOIN channels AS c
            ON t.attributed_platform = c.platform
            AND t.attributed_campaign_id = c.campaign_id
        GROUP BY c.channel_grouping
    ),
    combined AS (
        SELECT
            {{ dbt_utils.generate_surrogate_key(['s.channel_grouping']) }} AS id,
            s.channel_grouping,
            s.impressions,
            s.clicks,
            s.spend,
            s.conversions,
            COALESCE(r.attributed_orders, 0) AS attributed_orders,
            COALESCE(r.attributed_gross_revenue, 0) AS attributed_gross_revenue,
            COALESCE(r.attributed_net_revenue, 0) AS attributed_net_revenue,
            COALESCE(r.attributed_cogs, 0) AS attributed_cogs,
            COALESCE(r.attributed_contribution_margin, 0) AS attributed_contribution_margin,

            -- Gross ROAS: attributed revenue before returns, per unit of
            -- spend. The figure ad platforms report and the one most
            -- dashboards show.
            {{ safe_divide('COALESCE(r.attributed_gross_revenue, 0)', 's.spend') }} AS gross_roas,

            -- Net ROAS: contribution after returns AND cost of goods, per
            -- unit of spend. The figure that decides whether a channel is
            -- worth funding — a channel can be comfortably above 1.0 gross
            -- and still lose money once returns and product cost land.
            {{ safe_divide('COALESCE(r.attributed_contribution_margin, 0)', 's.spend') }} AS net_roas,

            {{ safe_divide('COALESCE(r.attributed_contribution_margin, 0) - s.spend', 's.spend') }} AS roi
        FROM spend_rollup AS s
        LEFT JOIN revenue_rollup AS r
            ON s.channel_grouping = r.channel_grouping
    )
SELECT * FROM combined
