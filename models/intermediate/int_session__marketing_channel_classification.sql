-- marketing_channel_taxonomy has no source/medium/priority columns, only a
-- single campaign_name_pattern regex per channel — so it is matched against
-- source/medium/campaign combined, preferring the most specific (longest)
-- pattern when more than one matches.
--
-- The taxonomy is compiled into a CASE of literal patterns rather than joined
-- on, because Redshift will not accept a regex pattern that comes from a
-- column. Emitting branches longest-pattern-first means CASE resolves to the
-- most specific match without a window function.
-- See macros/classify_by_taxonomy.sql.
--
-- COALESCE + || rather than CONCAT_WS: Redshift has no CONCAT_WS, and a NULL
-- anywhere in a || chain would otherwise null the whole classification key.
-- The taxonomy seed is read at compile time inside taxonomy_case(), where the
-- ref() sits inside an execute-guarded conditional that dbt cannot see when it
-- builds the graph. This hint restores the dependency, so the seed is still
-- guaranteed to be built before this model.
-- (Note: Jinja parses SQL comments too, so this text deliberately avoids
-- writing Jinja delimiters inside a comment -- they would be executed.)
-- depends_on: {{ ref('marketing_channel_taxonomy') }}
{%- set session_key = "COALESCE(source, '') || '-' || COALESCE(medium, '') || '-' || COALESCE(campaign, '')" %}

WITH
    source_data AS (
        SELECT
            session_id,
            source,
            medium,
            campaign
        FROM {{ ref('stg_google_analytics__sessions') }}
    ),
    classified AS (
        SELECT
            session_id,
            source,
            medium,
            campaign,
            COALESCE({{ taxonomy_case(session_key, 'channel') }}, 'Other') AS channel_grouping,
            {{ taxonomy_case(session_key, 'subchannel') }} AS subchannel
        FROM source_data
    )
SELECT
    session_id,
    source,
    medium,
    campaign,
    channel_grouping,
    subchannel
FROM classified
