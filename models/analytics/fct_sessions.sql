WITH
    sessions AS (
        SELECT * FROM {{ ref('stg_google_analytics__sessions') }}
    ),
    classified AS (
        SELECT * FROM {{ ref('int_session__marketing_channel_classification') }}
    ),
    fct AS (
        SELECT
            s.session_id,
            s.session_date,
            s.source,
            s.medium,
            s.campaign,
            c.channel_grouping,
            c.subchannel,
            s.device_category,
            s.pageviews,
            s.session_duration_seconds
        FROM sessions AS s
        INNER JOIN classified AS c
            ON s.session_id = c.session_id
    )
SELECT * FROM fct
