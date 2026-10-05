{% docs gclid_attribution %}
Attribution in this project is **click-id based**, not modelled.

Orders and web visits both carry a Google Click ID (`gclid`). `parse_gclid_ad_id`
resolves it back to an ad, and the ad → ad group → campaign hierarchy gives the
campaign. An order is credited to exactly the campaign whose ad the customer
clicked — last non-direct click, with no fractional or time-decay weighting.

Two consequences worth knowing before reading any number built on this:

- **Google Ads only.** There is no `fbclid` equivalent wired up and no
  session-to-order join, so Facebook, organic, email and referral revenue is
  not attributed to any channel. Those channels show spend with zero revenue
  rather than an estimate.
- **Undercounts rather than guesses.** An order placed after an ad click but
  without the gclid surviving to checkout is treated as unattributed, not
  redistributed. The spend side of every channel is complete; the revenue side
  is only as complete as click-id coverage.
{% enddocs %}


{% docs reporting_currency %}
Monetary amounts are in the project's single reporting currency, set by
`vars.reporting_currency` in `dbt_project.yml` (default `GBP`).

The product catalogue is priced in this currency, but Stripe settles each
customer in their own, so payments and refunds are converted back at the rate
for the transaction date. A refund is converted at its **original payment's**
rate rather than the refund date's — the standard accounting treatment, and it
avoids a rate lookup for a date that may fall outside the FX history.
{% enddocs %}


{% docs gross_roas %}
Attributed gross revenue divided by spend: revenue **before** returns and
before cost of goods, per unit of spend.

This is the figure ad platforms report and the one most dashboards show. It is
the right number for judging whether advertising generates demand, and the
wrong number for judging whether that demand is profitable.
{% enddocs %}


{% docs net_roas %}
Attributed contribution divided by spend: revenue **after** returns and cost of
goods, per unit of spend.

This is the figure that decides whether a channel deserves budget. A channel
can sit comfortably above 1.0 on gross ROAS and still destroy money once
returns and product cost land — in the demo dataset, Display returns £1.45 per
£1 gross and 57p net, while being the largest line of spend in the account.

Break-even is 1.0, not 0: below that the channel costs more than the
contribution it generates.
{% enddocs %}


{% docs contribution_margin %}
Revenue after refunds and after cost of goods, before any fixed or operating
costs.

Cost of goods is charged only on units actually kept: a returned item comes
back into stock, so its cost is credited along with its revenue. Charging full
COGS against refunded revenue would understate contribution on every line
carrying a return.
{% enddocs %}


{% docs loaded_at %}
When the ELT tool last synced this row, carried through from the source
system's sync column (`_fivetran_synced` in the demo dataset) and renamed to a
tool-agnostic `_loaded_at` at the staging boundary.

Used for source freshness checks and as the `updated_at` basis for
timestamp-strategy snapshots. It reflects **load** time, not business event
time — never use it as an event date.
{% enddocs %}
