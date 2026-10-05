#!/usr/bin/env python3
"""
Generates the synthetic "raw" dataset under seeds/example/**.

These CSVs stand in for a real ELT tool's landing tables (see the
`+schema: extract` override in dbt_project.yml) so the staging layer has
something to build on without a live Fivetran/Stripe/ad-platform connection.

Re-run this to refresh/resize the demo dataset:
    python3 scripts/generate_example_seeds.py
"""

import csv
import os
import random
import string
from datetime import date, datetime, timedelta

random.seed(42)

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SEEDS_ROOT = os.path.join(REPO_ROOT, "seeds", "example")

WINDOW_START = date(2025, 4, 1)
WINDOW_END = date(2026, 6, 30)
FX_WINDOW_START = WINDOW_START  # cover the full order/payment history

# ---------------------------------------------------------------------------
# Demo economics.
#
# Ad spend is *derived from* order revenue rather than generated
# independently, which is the only way to make the demo's ROAS land somewhere
# credible. Orders and their gclid attribution are generated first; spend per
# campaign is then back-solved so that blended gross ROAS across the
# gclid-attributable (Google) channels hits TARGET_GROSS_ROAS exactly.
#
# Net ROAS is not dialled in directly. It falls out of two trading facts: the
# return rate (ORDER_STATUS_WEIGHTS below) and gross margin per category
# (CATEGORY_GROSS_MARGIN). As generated, ~17% of attributed revenue is
# returned and the blended gross margin is ~72%, so contribution lands at
# ~60% of gross revenue and the 2.0 gross ROAS becomes a ~1.2 net ROAS.
# Change either of those and net ROAS moves with them, as it should.
#
# Facebook spend cannot be attributed at all — there is no fbclid equivalent
# wired up — so it is set as a share of Google spend and its ROAS shows as
# zero by design. See the README's note on attribution limits.
# ---------------------------------------------------------------------------

TARGET_GROSS_ROAS = 2.0
FACEBOOK_SPEND_SHARE = 0.45

# Relative efficiency per campaign, normalised so the blended figure equals
# TARGET_GROSS_ROAS. Brand search earns the most per pound (the customer was
# already looking for you); display retargeting the least. Flat ROAS across
# every channel would be the tell-tale sign of a made-up dataset.
GOOGLE_CAMPAIGN_ROAS_INDEX = {
    "Google Brand Search": 1.45,
    "Google Non-Brand Search": 0.85,
    "Google Shopping Feed": 1.10,
    "Google Banner Retargeting": 0.70,
}

# Cost per click and click-through rate per campaign, at roughly realistic
# Google Ads levels: brand search is cheap and clicks well, non-brand search
# is the expensive one, display is cheap per click and barely clicks at all.
GOOGLE_CAMPAIGN_CPC = {
    "Google Brand Search": 0.45,
    "Google Non-Brand Search": 1.05,
    "Google Shopping Feed": 0.55,
    "Google Banner Retargeting": 0.28,
}
GOOGLE_CAMPAIGN_CTR = {
    "Google Brand Search": 0.090,
    "Google Non-Brand Search": 0.045,
    "Google Shopping Feed": 0.035,
    "Google Banner Retargeting": 0.006,
}
FACEBOOK_CPC = 0.65
FACEBOOK_CTR = 0.012

# Platform-reported conversions exceed real attributed orders — every ad
# platform claims credit generously. Kept as a deliberate, modest overcount
# so the two numbers are close but not identical, as they are in real life.
PLATFORM_CONVERSION_OVERCOUNT = 1.3


def write_csv(relative_path, fieldnames, rows):
    path = os.path.join(SEEDS_ROOT, relative_path)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    print(f"wrote {len(rows):>5} rows -> {relative_path}")


def random_date(start, end):
    delta_days = (end - start).days
    return start + timedelta(days=random.randint(0, delta_days))


def random_datetime(start, end):
    d = random_date(start, end)
    return datetime.combine(d, datetime.min.time()) + timedelta(
        hours=random.randint(0, 23), minutes=random.randint(0, 59), seconds=random.randint(0, 59)
    )


def synced_at(d):
    # Fivetran-style sync timestamp: a bit after the event itself.
    return f"{d.isoformat()}T{random.randint(0, 23):02d}:{random.randint(0, 59):02d}:00Z"


def weighted_choice(pairs):
    r = random.random()
    cum = 0.0
    for value, weight in pairs:
        cum += weight
        if r <= cum:
            return value
    return pairs[-1][0]


def make_gclid(ad_id):
    # Real gclids are opaque, but this demo encodes the ad_id so downstream
    # models can resolve attribution by parsing it back out (see
    # macros/parse_gclid_ad_id.sql) — a stand-in for what you'd otherwise get
    # from Google Ads' click-view report joined on date.
    suffix = "".join(random.choices(string.ascii_lowercase + string.digits, k=10))
    return f"gclid-{ad_id}-{suffix}"


# ---------------------------------------------------------------------------
# prod: customers, products
# ---------------------------------------------------------------------------

FIRST_NAMES = [
    "Olivia", "Liam", "Emma", "Noah", "Ava", "Oliver", "Isla", "George",
    "Amelia", "Harry", "Freya", "Jack", "Grace", "Charlie", "Poppy", "Jacob",
    "Sophie", "Leo", "Ivy", "Arthur", "Chloe", "Oscar", "Mia", "Alfie",
    "Ella", "Henry", "Lily", "Theo", "Daisy", "Finn", "Rosie", "Max",
    "Evie", "Sam", "Layla", "Alex", "Zara", "Dylan", "Nina", "Ethan",
]
LAST_NAMES = [
    "Smith", "Jones", "Taylor", "Brown", "Williams", "Wilson", "Johnson",
    "Davies", "Robinson", "Wright", "Thompson", "Evans", "Walker", "White",
    "Roberts", "Green", "Hall", "Wood", "Clarke", "Hughes",
]
COUNTRY_TO_CURRENCY = {
    "GB": "GBP", "US": "USD", "IE": "EUR", "FR": "EUR", "DE": "EUR",
    "ES": "EUR", "NL": "EUR", "AU": "AUD", "CA": "CAD",
}
CUSTOMER_COUNTRIES = list(COUNTRY_TO_CURRENCY)

customers = []
for i in range(1, 41):
    first = random.choice(FIRST_NAMES)
    last = random.choice(LAST_NAMES)
    created = random_date(WINDOW_START, WINDOW_END)
    customers.append({
        "id": i,
        "first_name": first,
        "last_name": last,
        "email": f"{first.lower()}.{last.lower()}{i}@example.com",
        "country_code": random.choice(CUSTOMER_COUNTRIES),
        "created_at": created.isoformat(),
        "_fivetran_synced": synced_at(created),
    })
customers_by_id = {c["id"]: c for c in customers}
write_csv("prod/prod_customers.csv",
          ["id", "first_name", "last_name", "email", "country_code", "created_at", "_fivetran_synced"],
          customers)

# Gross margin by category, at levels that are ordinary for an own-brand
# apparel retailer: accessories and apparel carry the best markup, resold
# electronics much the worst.
CATEGORY_GROSS_MARGIN = {
    "Apparel": 0.755,
    "Footwear": 0.695,
    "Accessories": 0.795,
    "Electronics": 0.435,
    "Home": 0.725,
}

PRODUCTS = [
    ("Classic Tee", "Apparel", 19.99),
    ("Merino Jumper", "Apparel", 64.99),
    ("Rain Jacket", "Apparel", 89.99),
    ("Denim Jeans", "Apparel", 54.99),
    ("Trail Runner", "Footwear", 79.99),
    ("Leather Boot", "Footwear", 119.99),
    ("Canvas Sneaker", "Footwear", 44.99),
    ("Wool Beanie", "Accessories", 14.99),
    ("Leather Belt", "Accessories", 29.99),
    ("Crossbody Bag", "Accessories", 49.99),
    ("Wireless Earbuds", "Electronics", 69.99),
    ("Smart Water Bottle", "Electronics", 34.99),
    ("Ceramic Mug Set", "Home", 24.99),
    ("Linen Throw", "Home", 39.99),
    ("Scented Candle", "Home", 17.99),
]
products = []
for i, (name, category, price) in enumerate(PRODUCTS, start=1):
    created = random_date(WINDOW_START, WINDOW_START + timedelta(days=30))
    # Per-product jitter around the category margin, so cost is not a fixed
    # multiple of price across the catalogue.
    margin = CATEGORY_GROSS_MARGIN[category] + random.uniform(-0.03, 0.03)
    products.append({
        "id": i,
        "name": name,
        "category": category,
        "price": price,
        "unit_cost": round(price * (1 - margin), 2),
        "currency_code": "GBP",
        "created_at": created.isoformat(),
        "_fivetran_synced": synced_at(created),
    })
write_csv("prod/prod_products.csv",
          ["id", "name", "category", "price", "unit_cost", "currency_code", "created_at",
           "_fivetran_synced"],
          products)

# ---------------------------------------------------------------------------
# marketing: google ads (campaigns/ad_groups/ads/clicks), facebook ads
# (campaigns/ad_sets/ads — Facebook's "ads" source carries daily performance
# directly since there's no separate stats source declared). Generated before
# orders/visits so their ad ids exist to be encoded into gclids below.
# ---------------------------------------------------------------------------


def flight_window():
    flight_start = random_date(WINDOW_START, WINDOW_END - timedelta(days=60))
    flight_len = random.randint(45, 120)
    flight_end = min(flight_start + timedelta(days=flight_len), WINDOW_END)
    return flight_start, flight_end


# Campaign names deliberately follow a "platform + taxonomy keyword" naming
# convention (a common real-world ad-ops practice) so
# int_marketing__channel_classification can classify spend into the same
# marketing_channel_taxonomy used for session classification, just by
# pattern-matching the campaign name — no separate mapping table needed.
GOOGLE_CAMPAIGN_NAMES = ["Google Brand Search", "Google Non-Brand Search", "Google Shopping Feed", "Google Banner Retargeting"]
GOOGLE_CAMPAIGN_TYPES = {
    "Google Brand Search": "search",
    "Google Non-Brand Search": "search",
    "Google Shopping Feed": "shopping",
    "Google Banner Retargeting": "display",
}
google_campaigns = []
google_ad_groups = []
google_ads = []
google_ad_days = []  # performance figures filled in once revenue is known
ad_id_to_campaign_name = {}
ad_group_id = 1
ad_id = 1

for i, name in enumerate(GOOGLE_CAMPAIGN_NAMES, start=1):
    flight_start, flight_end = flight_window()
    google_campaigns.append({
        "id": i,
        "name": name,
        "status": "active",
        "campaign_type": GOOGLE_CAMPAIGN_TYPES[name],
        "start_date": flight_start.isoformat(),
        "end_date": flight_end.isoformat(),
        "_fivetran_synced": synced_at(flight_end),
    })
    for ag_n in range(1, 3):
        google_ad_groups.append({
            "id": ad_group_id,
            "campaign_id": i,
            "name": f"{name} - Ad Group {ag_n}",
            "status": "active",
            "_fivetran_synced": synced_at(flight_end),
        })
        for a_n in range(1, 3):
            google_ads.append({
                "id": ad_id,
                "ad_group_id": ad_group_id,
                "headline": f"{name} Offer {a_n}",
                "status": "active",
                "ad_type": "responsive_search_ad",
                "_fivetran_synced": synced_at(flight_end),
            })
            ad_id_to_campaign_name[ad_id] = name
            perf_start = random_date(flight_start, max(flight_start, flight_end - timedelta(days=30)))
            perf_days = random.randint(25, 45)
            for day_offset in range(perf_days):
                perf_date = perf_start + timedelta(days=day_offset)
                if perf_date > WINDOW_END:
                    break
                google_ad_days.append({
                    "ad_id": ad_id,
                    "campaign_name": name,
                    "date": perf_date,
                    # Relative share of the campaign's spend on this ad-day.
                    "weight": random.uniform(0.6, 1.4),
                })
            ad_id += 1
        ad_group_id += 1

valid_google_ad_ids = [a["id"] for a in google_ads]

write_csv("marketing/google_ads_campaigns.csv",
          ["id", "name", "status", "campaign_type", "start_date", "end_date", "_fivetran_synced"],
          google_campaigns)
write_csv("marketing/google_ads_ad_groups.csv",
          ["id", "campaign_id", "name", "status", "_fivetran_synced"],
          google_ad_groups)
write_csv("marketing/google_ads_ads.csv",
          ["id", "ad_group_id", "headline", "status", "ad_type", "_fivetran_synced"],
          google_ads)
FACEBOOK_CAMPAIGN_NAMES = ["Facebook Cpc Retargeting", "Facebook Cpc Lookalike Prospecting", "Facebook Leadgen Campaign"]
facebook_campaigns = []
facebook_ad_sets = []
facebook_ad_days = []  # performance figures filled in once revenue is known
ad_set_id = 1
fb_ad_id = 1

for i, name in enumerate(FACEBOOK_CAMPAIGN_NAMES, start=1):
    flight_start, flight_end = flight_window()
    facebook_campaigns.append({
        "id": i,
        "name": name,
        "objective": "lead_generation" if "Leadgen" in name else "conversions",
        "status": "active",
        "start_date": flight_start.isoformat(),
        "end_date": flight_end.isoformat(),
        "_fivetran_synced": synced_at(flight_end),
    })
    for as_n in range(1, 3):
        facebook_ad_sets.append({
            "id": ad_set_id,
            "campaign_id": i,
            "name": f"{name} - Ad Set {as_n}",
            "status": "active",
            "daily_budget": round(random.uniform(15, 80), 2),
            "_fivetran_synced": synced_at(flight_end),
        })
        for a_n in range(1, 3):
            perf_start = random_date(flight_start, max(flight_start, flight_end - timedelta(days=30)))
            perf_days = random.randint(25, 40)
            for day_offset in range(perf_days):
                perf_date = perf_start + timedelta(days=day_offset)
                if perf_date > WINDOW_END:
                    break
                facebook_ad_days.append({
                    "ad_id": fb_ad_id,
                    "ad_set_id": ad_set_id,
                    "name": f"{name} Creative {a_n}",
                    "date": perf_date,
                    "weight": random.uniform(0.6, 1.4),
                })
            fb_ad_id += 1
        ad_set_id += 1

write_csv("marketing/facebook_ads_campaigns.csv",
          ["id", "name", "objective", "status", "start_date", "end_date", "_fivetran_synced"],
          facebook_campaigns)
write_csv("marketing/facebook_ads_ad_sets.csv",
          ["id", "campaign_id", "name", "status", "daily_budget", "_fivetran_synced"],
          facebook_ad_sets)
# ---------------------------------------------------------------------------
# commercial: exchange rates (generated before payments so a fx lookup
# exists to convert non-GBP charges back to the reporting currency)
# ---------------------------------------------------------------------------

REPORTING_CURRENCY = "GBP"
FX_TARGETS = [("GBP", "USD", 1.27), ("GBP", "EUR", 1.17), ("GBP", "CAD", 1.72), ("GBP", "AUD", 1.93)]
exchange_rates = []
fx_rate_lookup = {}  # (date_iso, target_currency) -> rate
d = FX_WINDOW_START
while d <= WINDOW_END:
    for base, target, base_rate in FX_TARGETS:
        rate = round(base_rate + random.uniform(-0.02, 0.02), 4)
        exchange_rates.append({
            "date": d.isoformat(),
            "base_currency": base,
            "target_currency": target,
            "rate": rate,
            "_fivetran_synced": synced_at(d),
        })
        fx_rate_lookup[(d.isoformat(), target)] = rate
    d += timedelta(days=1)
write_csv("commercial/exchange_rates.csv",
          ["date", "base_currency", "target_currency", "rate", "_fivetran_synced"],
          exchange_rates)


def to_local_currency(gbp_amount, currency, on_date_iso):
    if currency == REPORTING_CURRENCY:
        return gbp_amount
    rate = fx_rate_lookup[(on_date_iso, currency)]
    return round(gbp_amount * rate, 2)


# ---------------------------------------------------------------------------
# prod: orders, order_items
# ---------------------------------------------------------------------------

ORDER_STATUS_WEIGHTS = [("completed", 0.75), ("refunded", 0.10), ("cancelled", 0.15)]
ORDER_GCLID_PROBABILITY = 0.35

orders = []
order_items = []
order_item_id = 1
order_totals = {}
for order_id in range(1, 151):
    order_date = random_date(WINDOW_START, WINDOW_END)
    status = weighted_choice(ORDER_STATUS_WEIGHTS)
    customer = random.choice(customers)
    has_gclid = random.random() < ORDER_GCLID_PROBABILITY
    orders.append({
        "id": order_id,
        "user_id": customer["id"],
        "order_date": order_date.isoformat(),
        "status": status,
        "currency_code": "GBP",
        "gclid": make_gclid(random.choice(valid_google_ad_ids)) if has_gclid else "",
        "_fivetran_synced": synced_at(order_date),
    })

    n_items = random.choices([1, 2, 3, 4], weights=[0.35, 0.35, 0.2, 0.1])[0]
    total = 0.0
    for _ in range(n_items):
        product = random.choice(products)
        qty = random.choices([1, 2, 3], weights=[0.7, 0.25, 0.05])[0]
        order_items.append({
            "id": order_item_id,
            "order_id": order_id,
            "product_id": product["id"],
            "quantity": qty,
            "unit_price": product["price"],
            "_fivetran_synced": synced_at(order_date),
        })
        total += qty * product["price"]
        order_item_id += 1
    order_totals[order_id] = round(total, 2)

write_csv("prod/prod_orders.csv",
          ["id", "user_id", "order_date", "status", "currency_code", "gclid", "_fivetran_synced"],
          orders)
write_csv("prod/prod_order_items.csv",
          ["id", "order_id", "product_id", "quantity", "unit_price", "_fivetran_synced"],
          order_items)

# ---------------------------------------------------------------------------
# commercial: stripe payments/refunds. The order/product catalogue is always
# priced in GBP, but Stripe settles each customer in their own local
# currency — so the charged amount is the GBP order total converted at that
# day's rate. int_order__transactions converts it back to the reporting
# currency for revenue reporting.
# ---------------------------------------------------------------------------

# NOTE: real Stripe amounts land in minor units (pence), not decimal major
# units — kept as decimal here to keep the demo dataset readable; a real
# staging model would need to divide by 100.
stripe_payments = []
stripe_refunds = []
payment_id = 1
refund_id = 1
for o in orders:
    if o["status"] == "cancelled":
        continue
    created = date.fromisoformat(o["order_date"])
    customer = customers_by_id[o["user_id"]]
    currency = COUNTRY_TO_CURRENCY[customer["country_code"]]
    gbp_total = order_totals[o["id"]]
    local_amount = to_local_currency(gbp_total, currency, created.isoformat())
    pid = f"pi_{payment_id:06d}"
    stripe_payments.append({
        "id": pid,
        "order_id": o["id"],
        "amount": local_amount,
        "currency": currency.lower(),
        "status": "succeeded",
        "created_at": created.isoformat(),
        "_fivetran_synced": synced_at(created),
    })
    if o["status"] == "refunded":
        refund_date = created + timedelta(days=random.randint(1, 14))
        stripe_refunds.append({
            "id": f"re_{refund_id:06d}",
            "payment_id": pid,
            # full refund, same currency/rate as the original charge
            "amount": local_amount,
            "reason": random.choice(["requested_by_customer", "duplicate", "fraudulent"]),
            "created_at": refund_date.isoformat(),
            "_fivetran_synced": synced_at(refund_date),
        })
        refund_id += 1
    payment_id += 1

write_csv("commercial/stripe/stripe_payments.csv",
          ["id", "order_id", "amount", "currency", "status", "created_at", "_fivetran_synced"],
          stripe_payments)
write_csv("commercial/stripe/stripe_refunds.csv",
          ["id", "payment_id", "amount", "reason", "created_at", "_fivetran_synced"],
          stripe_refunds)

# ---------------------------------------------------------------------------
# marketing performance: spend back-solved from attributed revenue
#
# Deferred to here because it depends on the orders generated above. For each
# Google campaign we know the gross revenue of the orders whose gclid resolves
# to one of its ads — the same set of orders fct_transactions will attribute
# to it — so spend is set to hit the target ROAS instead of being drawn from
# thin air. Cancelled orders are excluded, because they never produced a
# Stripe payment and so never reach fct_transactions.
# ---------------------------------------------------------------------------


def ad_id_from_gclid(gclid):
    # "gclid-<ad_id>-<suffix>" — mirrors macros/parse_gclid_ad_id.sql
    return int(gclid.split("-")[1])


attributed_gross = {name: 0.0 for name in GOOGLE_CAMPAIGN_NAMES}
attributed_orders = {name: 0 for name in GOOGLE_CAMPAIGN_NAMES}
for o in orders:
    if not o["gclid"] or o["status"] == "cancelled":
        continue
    campaign_name = ad_id_to_campaign_name[ad_id_from_gclid(o["gclid"])]
    attributed_gross[campaign_name] += order_totals[o["id"]]
    attributed_orders[campaign_name] += 1

# Normalise the relative efficiency indices so blended gross ROAS across all
# Google campaigns is exactly TARGET_GROSS_ROAS:
#   spend_c = attributed_c / (k * index_c)
#   blended = sum(attributed) / sum(spend) = k * sum(attributed) / sum(attributed_c / index_c)
total_attributed = sum(attributed_gross.values())
weighted = sum(attributed_gross[n] / GOOGLE_CAMPAIGN_ROAS_INDEX[n] for n in GOOGLE_CAMPAIGN_NAMES)
k = TARGET_GROSS_ROAS * weighted / total_attributed

google_campaign_spend = {
    n: attributed_gross[n] / (k * GOOGLE_CAMPAIGN_ROAS_INDEX[n]) for n in GOOGLE_CAMPAIGN_NAMES
}


def distribute(rows, total, weight_key="weight"):
    """Split `total` across `rows` in proportion to their weights.

    The last row absorbs the rounding residual so the parts sum to the whole
    exactly — otherwise the demo's headline ROAS would drift by a few pence
    against the target for no visible reason.
    """
    if not rows:
        return []
    weight_sum = sum(r[weight_key] for r in rows)
    amounts = [round(total * r[weight_key] / weight_sum, 2) for r in rows]
    amounts[-1] = round(amounts[-1] + (total - sum(amounts)), 2)
    return amounts


google_clicks = []
click_id = 1
for campaign_name in GOOGLE_CAMPAIGN_NAMES:
    rows = [r for r in google_ad_days if r["campaign_name"] == campaign_name]
    costs = distribute(rows, google_campaign_spend[campaign_name])
    cpc = GOOGLE_CAMPAIGN_CPC[campaign_name]
    ctr = GOOGLE_CAMPAIGN_CTR[campaign_name]

    emitted = []
    for row, cost in zip(rows, costs):
        clicks = max(0, round(cost / (cpc * random.uniform(0.85, 1.15))))
        impressions = max(clicks, round(clicks / (ctr * random.uniform(0.8, 1.2)))) if clicks else 0
        emitted.append({
            "id": click_id,
            "ad_id": row["ad_id"],
            "date": row["date"].isoformat(),
            "impressions": impressions,
            "clicks": clicks,
            "cost": cost,
            "conversions": 0,
            "_fivetran_synced": synced_at(row["date"]),
        })
        click_id += 1

    # Platform-reported conversions: a modest overcount of the orders actually
    # attributed to this campaign, landed on random ad-days rather than spread
    # evenly, because a small advertiser does not convert every single day.
    target_conversions = round(attributed_orders[campaign_name] * PLATFORM_CONVERSION_OVERCOUNT)
    for _ in range(target_conversions):
        if emitted:
            random.choice(emitted)["conversions"] += 1
    google_clicks.extend(emitted)

write_csv("marketing/google_ads_clicks.csv",
          ["id", "ad_id", "date", "impressions", "clicks", "cost", "conversions", "_fivetran_synced"],
          google_clicks)

# Facebook: unattributable, so spend is simply a share of Google's.
facebook_total_spend = sum(google_campaign_spend.values()) * FACEBOOK_SPEND_SHARE
facebook_ads = []
fb_row_id = 1
fb_costs = distribute(facebook_ad_days, facebook_total_spend)
for row, spend in zip(facebook_ad_days, fb_costs):
    clicks = max(0, round(spend / (FACEBOOK_CPC * random.uniform(0.85, 1.15))))
    impressions = max(clicks, round(clicks / (FACEBOOK_CTR * random.uniform(0.8, 1.2)))) if clicks else 0
    facebook_ads.append({
        "id": fb_row_id,
        "ad_id": row["ad_id"],
        "ad_set_id": row["ad_set_id"],
        "date": row["date"].isoformat(),
        "name": row["name"],
        "status": "active",
        "impressions": impressions,
        "clicks": clicks,
        "spend": spend,
        "conversions": max(0, round(clicks * random.uniform(0.004, 0.012))),
        "_fivetran_synced": synced_at(row["date"]),
    })
    fb_row_id += 1

write_csv("marketing/facebook_ads_ads.csv",
          ["id", "ad_id", "ad_set_id", "date", "name", "status", "impressions", "clicks",
           "spend", "conversions", "_fivetran_synced"],
          facebook_ads)

# ---------------------------------------------------------------------------
# prod: web_visits — raw, granular, first-party pageview/click-landing
# events. Unlike GA4's pre-aggregated sessions below, this is the kind of
# ever-growing event log that's naturally suited to incremental loading
# (see models/analytics/fct_visits.sql).
# ---------------------------------------------------------------------------

LANDING_PAGES = ["/", "/products", "/products/tee", "/products/jacket", "/checkout", "/about", "/blog/sizing-guide"]
VISIT_GCLID_PROBABILITY = 0.30

web_visits = []
for visit_id in range(1, 901):
    visited_at = random_datetime(WINDOW_START, WINDOW_END)
    has_gclid = random.random() < VISIT_GCLID_PROBABILITY
    is_known_customer = random.random() < 0.4
    web_visits.append({
        "id": visit_id,
        "visited_at": visited_at.isoformat() + "Z",
        "customer_id": random.choice(customers)["id"] if is_known_customer else "",
        "gclid": make_gclid(random.choice(valid_google_ad_ids)) if has_gclid else "",
        "landing_page": random.choice(LANDING_PAGES),
        "device_category": random.choice(["desktop", "mobile", "tablet"]),
        "_fivetran_synced": synced_at(visited_at.date()),
    })
write_csv("prod/prod_web_visits.csv",
          ["id", "visited_at", "customer_id", "gclid", "landing_page", "device_category", "_fivetran_synced"],
          web_visits)

# ---------------------------------------------------------------------------
# commercial: GA4 sessions
# ---------------------------------------------------------------------------

# GA4 sessions: each scenario's `campaign` embeds the literal substring that
# marketing_channel_taxonomy.campaign_name_pattern needs to match against
# CONCAT_WS('-', source, medium, campaign) in int_session__marketing_channel_classification.
# Note: the taxonomy's "google.*(shopping|cse|pla)" pattern also matches the
# word "display" (it contains "pla"), so the GDN scenario below deliberately
# avoids pairing source=google with medium=display to dodge that collision.
# The email/newsletter pair is deliberately overlapping — both match, and the
# model's "longest pattern wins" tiebreak resolves it to Newsletter, which is
# the intended demonstration of that logic.
SESSION_SCENARIOS = [
    ("google", "cpc", "google-brand-search"),
    ("google", "cpc", "google-non-brand-generic"),
    ("google", "cpc", "google-shopping-feed"),
    ("bing", "cpc", "bing-non-brand-search"),
    ("google", "organic", "google-organic-search"),
    ("facebook", "cpc", "facebook-cpc-retarget"),
    ("facebook", "organic", "facebook-organic-page-post"),
    ("instagram", "cpc", "instagram-cpc-story-ads"),
    ("instagram", "organic", "instagram-organic-reels"),
    ("gdn", "display", "banner-network-retargeting"),
    ("affiliate", "referral", "affiliate-super-partner"),
    ("cj", "affiliate", "cj-network-impact"),
    ("email", "email", "jan-promo-blast"),
    ("email", "email", "newsletter-weekly-digest"),
    ("referral-partner-site", "referral", "jan-collab"),
    ("facebook", "lead_ad", "leadgen-form"),
    ("direct", "none", "(direct)"),  # deliberately unmatched -> "Other"
]
DEVICE_CATEGORIES = ["desktop", "mobile", "tablet"]

sessions = []
session_id = 1
for source, medium, campaign_base in SESSION_SCENARIOS:
    n = random.randint(25, 45)
    for _ in range(n):
        session_date = random_date(WINDOW_START, WINDOW_END)
        sessions.append({
            "id": session_id,
            "session_date": session_date.isoformat(),
            "source": source,
            "medium": medium,
            "campaign": f"{campaign_base}-{session_id}",
            "device_category": random.choice(DEVICE_CATEGORIES),
            "pageviews": random.randint(1, 12),
            "session_duration_seconds": random.randint(10, 900),
            "_fivetran_synced": synced_at(session_date),
        })
        session_id += 1
write_csv("commercial/google_analytics/google_analytics_sessions.csv",
          ["id", "session_date", "source", "medium", "campaign", "device_category",
           "pageviews", "session_duration_seconds", "_fivetran_synced"],
          sessions)

print("done.")
