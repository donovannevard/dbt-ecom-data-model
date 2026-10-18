-- Reuses marketing_channel_taxonomy — the same seed that classifies GA4
-- sessions in int_session__marketing_channel_classification — to classify
-- ad platform campaigns into a channel, by pattern-matching the campaign
-- name directly. This only works because campaign names follow a
-- "platform + taxonomy keyword" convention (see the comment in
-- scripts/generate_example_seeds.py); a client without that convention
-- already in place needs an explicit campaign_id -> channel mapping seed
-- instead.
--
-- The taxonomy is compiled into a CASE of literal patterns rather than joined
-- on: Redshift will not accept a regex pattern that comes from a column. See
-- macros/classify_by_taxonomy.sql.
-- The taxonomy seed is read at compile time inside taxonomy_case(), where the
-- ref() sits inside an execute-guarded conditional that dbt cannot see when it
-- builds the graph. This hint restores the dependency, so the seed is still
-- guaranteed to be built before this model.
-- (Note: Jinja parses SQL comments too, so this text deliberately avoids
-- writing Jinja delimiters inside a comment -- they would be executed.)
-- depends_on: {{ ref('marketing_channel_taxonomy') }}
{%- set campaign_key = "LOWER(REPLACE(campaign_name, ' ', '-'))" %}

WITH
    campaigns AS (
        SELECT DISTINCT platform, campaign_id, campaign_name
        FROM {{ ref('int_marketing__combined') }}
    ),
    classified AS (
        SELECT
            platform,
            campaign_id,
            campaign_name,
            -- Campaigns matching no pattern fall through to 'Other' rather
            -- than NULL, so channel_grouping is never null and the spend side
            -- of agg_channel always reconciles to total spend.
            COALESCE({{ taxonomy_case(campaign_key, 'channel') }}, 'Other') AS channel_grouping,
            {{ taxonomy_case(campaign_key, 'subchannel') }} AS subchannel
        FROM campaigns
    )
SELECT
    platform,
    campaign_id,
    campaign_name,
    channel_grouping,
    subchannel
FROM classified
