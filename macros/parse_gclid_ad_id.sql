-- Real gclids are opaque tokens; Google Ads' click-view report is how you'd
-- normally resolve one back to an ad. This demo's synthetic gclids encode
-- the ad_id directly (see scripts/generate_example_seeds.py) as a stand-in
-- for that lookup, so it can be parsed straight out of the raw value.
{% macro parse_gclid_ad_id(gclid_column) %}
    {{ safe_cast_integer("SPLIT_PART(NULLIF(" ~ gclid_column ~ ", ''), '-', 2)") }}
{% endmacro %}
