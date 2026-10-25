# AI remediation layer

A loop that turns a failing data-quality check into a reviewed fix:

```
dbt test fails ─> diagnostic bundle ─> Claude proposes a patch ─> code validates it
     ─> branch + PR ─> CI ─┬─ green, auto tier ────────> auto-merge
                           ├─ green, quarantine tier ──> human merges
                           └─ red ─> back to Claude with the CI output
                                     (at most 3 CI failures, then blocked)
```

**Status:** Phases 0–6 are built: risk tiers and guardrails, fault injection
and diagnostic bundles, read-only diagnosis by Claude, the validator that
decides whether a patch may become a branch, the pipeline that opens the PR
or issue, the CI feedback loop, and auto-merge for allowlisted models. They
run end to end in a private sandbox repo. Phase 7 is listed at the end.

Ahead of all of this sits a deterministic layer: duplicate source rows are
handled by code, without stopping the build and without involving a model
(see [Duplicates](#duplicates-handled-by-code-not-by-ai)). The AI layer is for
failures no rule anticipates.

## Design rules

1. **Risk comes from lineage, not the model.** A fix's tier is the most
   restrictive tier of every node it touches *and everything downstream*. A
   one-line change to `stg_exchange_rates` is a revenue change. The model's
   own risk assessment is informational only.
2. **GitHub enforces the quarantine.** The generated `.github/CODEOWNERS`
   plus branch protection mean a bot cannot merge a quarantined change even if
   the remediation code has a bug.
3. **Test changes are hard-rejected.** Changing a test so that it passes is
   the main failure mode to guard against. Any change to an existing test's
   definition, arguments, severity, thresholds or `where` clause fails
   validation outright, in every tier. New tests may be added.
4. **"Not a code bug" is a valid answer.** Many failures are bad source data.
   A `data_issue` verdict opens an issue rather than a PR.
5. **State lives in GitHub.** Attempt counts go in PR labels and commit
   trailers, so the loop needs no database of its own.

## Risk tiers (Phase 0)

Two tiers, set in `meta.remediation_tier`:

| Tier | Merge | Models |
|---|---|---|
| `auto` | Automatic once CI is green (from Phase 6) | `dim_date`, `fct_sessions`, `agg_marketing`, `int_session__marketing_channel_classification`, `stg_google_analytics__sessions` |
| `quarantine` | A human must approve | Everything else |

The default is `quarantine`, set project-wide in `dbt_project.yml`. Only a
model's own YAML can opt it into `auto`. Because opting in has no effect when
anything downstream is quarantined, the auto tier ends up small. Nearly
everything in this project feeds `agg_channel`, whose ROAS is built on
revenue, and the Google Ads staging models feed `fct_transactions` through
gclid attribution. That narrow auto tier is the intended outcome.

Some results that follow from the lineage rules:

| Change | Tier | Why |
|---|---|---|
| `macros/week_bounds.sql` | auto | only `dim_date` calls it |
| `macros/classify_by_taxonomy.sql` | quarantine | also classifies marketing spend, which feeds `agg_channel` |
| `stg_google_ads__ads.sql` | quarantine | gclid attribution reaches `fct_transactions` |
| `stg_web_visits.sql` | quarantine | `fct_visits` carries `customer_id` |
| `models/staging/staging.yaml` | quarantine | a YAML file takes the strictest model it defines |
| a snapshot, seed or unmapped file | quarantine | history cannot be rebuilt; unknown means unsafe |

```bash
make risk FILES="models/analytics/dim_date.sql macros/week_bounds.sql"
python -m remediation.risk tiers          # every node, its tier and why
```

### Protected paths

`remediation/policy.yaml` lists paths no patch may touch, in any tier. They
include `.github/**`, `remediation/**`, `tests/**`, `seeds/**`, `snapshots/**`
and the project and dependency configs. The agent must not be able to edit
its own guardrails. The policy also protects the `remediation_tier` and `pii`
meta keys: risk is always computed from the base branch's manifest, but a
merged patch that flipped a model to `auto` would affect every later run.

### Opting a model into `auto`

1. Add `config: meta: remediation_tier: auto` to the model's YAML.
2. Add the model to `AUTO_MODELS` in `remediation/tests/test_risk.py`. That
   test requires the auto tier to be exactly the reviewed set, so widening it
   is always a deliberate change.
3. Run `make codeowners` to regenerate `.github/CODEOWNERS`. CI fails if it is
   stale.

### Branch protection

CODEOWNERS only has an effect once branch protection requires code-owner
review. This is a repository setting and is **not** applied by this repo. To
turn it on:

```bash
gh api -X PUT repos/donovannevard/dbt-ecom-data-model/branches/main/protection \
  --input - <<'JSON'
{
  "required_status_checks": {
    "strict": true,
    "contexts": ["dbt build (DuckDB)", "remediation guardrails"]
  },
  "enforce_admins": false,
  "required_pull_request_reviews": {
    "require_code_owner_reviews": true,
    "required_approving_review_count": 0
  },
  "restrictions": null
}
JSON
```

With `required_approving_review_count: 0`, approval is required only when a
PR touches an owned file. Auto-tier files are the only unowned ones, so a PR
that touches only those can merge on green CI. Every other PR needs the code
owner. CODEOWNERS fails safe: a new file is owned by default.

Keep `enforce_admins` false only while the repo has a single maintainer. It
lets the admin push directly, and the bot account must never be an admin.

## Fault injection (Phase 1)

`remediation/scenarios/` holds realistic ways to break the project. Each
scenario also records its expected outcome:

| Scenario | Kind | Tier | What breaks |
|---|---|---|---|
| `stripe_duplicate_payment` | data | absorbed | Connector re-delivers a payment, identical copy |
| `stripe_conflicting_payment` | data | absorbed | Two versions of one payment disagree on amount |
| `ga_duplicate_session` | data | absorbed | GA4 export duplicates a session |
| `orders_null_customer` | data | quarantine | Order with no customer |
| `orders_unknown_status` | data | quarantine | New `shipped` status appears |
| `facebook_negative_spend` | data | quarantine | Refunded ad charge as negative spend |
| `week_start_off_by_one` | code | auto | Monday offset wrong in `week_bounds` |
| `ga_session_id_mapping` | code | auto | `session_id` mapped from `pageviews` |
| `marketing_key_missing_platform` | code | auto | Surrogate key drops `platform`; ids collide |
| `refund_sign_flip` | code | quarantine | Prorated refund goes negative |
| `orders_customer_key_swap` | code | quarantine | `user_id` mapped from the order id |

### Walking through a scenario

Each step is a separate command, and only `chaos-diagnose` spends API credits:

```bash
make chaos          SCENARIO=refund_sign_flip    # inject the fault and build
make chaos-changes  SCENARIO=refund_sign_flip    # which tables the fault changed
make chaos-diff     SCENARIO=refund_sign_flip TABLE=main.fct_transactions   # the rows
make chaos-ui       SCENARIO=refund_sign_flip    # or chaos-shell: query them yourself
make chaos-diagnose SCENARIO=refund_sign_flip    # one Claude call; prints the expected answer after
make chaos-fix      SCENARIO=refund_sign_flip    # apply Claude's edits to a copy, rebuild, verify
make chaos-changes  SCENARIO=refund_sign_flip AGAINST=fixed   # does fixed match clean?
```

Each scenario keeps a workspace in `.chaos/<scenario>/` (gitignored):

| Path | |
|---|---|
| `broken/` | Project copy with the fault injected |
| `clean.duckdb`, `broken.duckdb` | Snapshots of the build without and with the fault |
| `bundle.json`, `summary.md` | What Claude is sent |
| `diagnosis.json`, `.md`; `score.json` | Claude's answer, scored against the scenario |
| `fixed/`, `fixed.duckdb` | Project with Claude's edits applied, and its build |
| `fix.json`, `fix.md` | Whether the fix worked |

`chaos-shell` and `chaos-ui` open the DuckDB CLI or browser UI with every
snapshot attached read-only as `clean`, `broken` and `fixed`, so one query can
compare them:

```sql
SELECT 'clean' AS db, count(*) FROM clean.main.fct_transactions WHERE refund_amount < 0
UNION ALL
SELECT 'broken', count(*) FROM broken.main.fct_transactions WHERE refund_amount < 0;
```

`make duckdb-ui` is the general-purpose view. It shows your local build as
`dev` and the shared baseline as `clean`, and adds each scenario's databases
as `<scenario>_broken` and `<scenario>_fixed`. `make chaos-clean` removes them
again. Only one UI can run at a time, because both use port 4213.

The snapshots are **frozen**: views are materialised as tables. dbt-duckdb
creates staging views whose SQL names the `dev` catalog, so they break as soon
as a database is attached under another name. Freezing makes every snapshot
self-contained. The DuckDB UI downloads its interface from `ui.duckdb.org` on
first use; your data stays local.

**A fix is verified** when every originally failing test passes and the full
build has no other failures. `fix.md` also lists tables whose data still
differs from the clean build. For a code fault, a correct fix leaves none. For
a data fault, the injected source rows are still there, so `extract` tables
are expected to differ.

**To try your own fix,** run `make chaos-rebuild`. The first run creates
`fixed/` from `broken/`. Edit it, then run `make chaos-rebuild` again to build
and verify it.

Project copies contain the repo's tracked and unignored files as they are on
disk, so uncommitted work is included. Your checkout is never modified, and
`inject` refuses to write to any directory without a `.remediation-scratch`
marker. The clean baseline is built once and reused until any source file
changes. Each workspace takes about 70MB; `make chaos-clean` removes them all.

### End to end

```bash
make chaos SCENARIO=all            # every scenario, ~3 min, no API calls
make chaos-diagnose SCENARIO=all   # every scenario plus a scored diagnosis; ~$1-3
```

A run also checks the scenario itself: the expected tests must fail, and the
fix must carry the expected tier. If a scenario no longer causes its failure,
it has rotted and the run fails. The cheap half of that check, that every
fault still applies to the current code, runs in CI on every push.

In GitHub Actions, run **chaos (fault injection)** from the Actions tab
(`workflow_dispatch`, `main` only). Bundles are uploaded as the
`chaos-bundles` artifact.

### Writing a scenario

```yaml
id: my_scenario            # must match the filename
title: One line
kind: data | code
description: >             # for humans; never shown to the model
  What breaks and what a correct response looks like.
faults:                    # applied in order; all-or-nothing
  - {op: replace, file: models/x.sql, old: "exact text, once", new: "..."}
  - {op: set_value, file: seeds/example/.../t.csv, column: c, rows: [3], value: ""}
  - {op: duplicate_row, file: seeds/example/.../t.csv, row: 5}
expect:
  failing_tests:           # matched on model + test type (+ column), not hashed names
    - {model: x, test: unique, column: id}
  verdicts: [code_fix]     # any of: code_fix, data_issue, needs_human
  tier: auto | quarantine
  fix_files: [models/x.sql]  # code scenarios only
```

An **absorbed** scenario is a fault the project handles itself: nothing
fails, specific `warn` tests fire, and (with `only_differs_from_clean`) the
fault reaches nothing beyond the tables named. Warnings are matched exactly,
so an identical duplicate that trips the conflict check fails the scenario.
There is nothing to diagnose, so absorbed scenarios skip Claude.

## Duplicates: handled by code, not by AI

Every staging model reads its source through
[`deduplicate_source()`](../macros/deduplicate_source.sql):

- **Identical copies** of a record are collapsed to one row. Nothing is lost.
- **Conflicting versions** of a record (same key, different values) are
  excluded from staging, because no rule can say which version is true.
- **Null keys** pass through untouched, so `not_null` tests still fire.

Every source table carries two `warn` tests:
`dbt_utils.unique_combination_of_columns` on the key (any duplicate), and
[`no_conflicting_duplicates`](../tests/generic/no_conflicting_duplicates.sql).
The second test's stored failures are the **quarantine table**, for example
`dbt_test__audit.source_no_conflicting_duplicates_extract_stripe_payments_id`,
holding every version of every conflicting key. Neither test stops the build.

The trade-off is explicit: a conflicting payment's revenue is missing from
the marts until the conflict is resolved at source, instead of the whole
pipeline halting or one version being picked silently.
`stripe_conflicting_payment` shows it: the payment is held out of staging, so
its order's two line items drop out of `fct_transactions`, and both
warnings fire.

### Quarantine alerts

A warning in a build log is easy to miss, so
[`remediation/quarantine.py`](../remediation/quarantine.py) turns the
quarantine into an issue that notifies someone and tracks the fix, **one per
source table**:

| After a build | What happens |
|---|---|
| A source has conflicts and no open issue | An issue opens, labelled `quarantine` and `data-issue`. It shows the conflicting versions (PII redacted), the downstream models now missing those records, and a four-step runbook. |
| Conflicts persist | The issue is refreshed. It gets a comment only if the set of quarantined keys changed, so a daily build doesn't spam it. |
| The conflicts are gone | The issue **closes itself** with a "resolved" comment. |

GitHub's notifications are the alert. If `REMEDIATION_WEBHOOK_URL` is set (a
Slack-style incoming webhook), every open, change and close is posted there
too. A failing webhook never fails the build.

**The fix flow** (the runbook in each issue):

1. Investigate: compare the versions and their load times (`make quarantine`,
   `make duckdb-ui`).
2. Decide which version is right with the data's owner.
3. Fix it at source and let the pipeline run. The key leaves quarantine, and
   the issue closes, on its own.
4. If the source legitimately keeps several versions per key, make that a
   reviewed modelling change. Never weaken the checks.

It runs **automatically after Dagster's scheduled build**. One thing had to
be handled here: Dagster's `dbt build` selects only its own assets, so the
source checks never ran under Dagster. The hook in
[`orchestration/definitions.py`](../orchestration/definitions.py) runs
`dbt test --select source:*` itself, then syncs from that run's results.
Issues go to the local stand-in by default; set `REMEDIATION_GITHUB=1` for
real ones. Rows are read straight from DuckDB, or through `dbt show` on any
other warehouse, in its own target directory so the build's
`run_results.json` is never overwritten.

```bash
make quarantine                    # what's quarantined in the last local build
make quarantine-sync               # open / refresh / close issues (local stand-in)
make quarantine-sync GITHUB=1      # the same on GitHub (needs `gh auth login`)
```

Tested end to end: Dagster run inside the `stripe_conflicting_payment`
workspace opened the issue, and a clean build closed it.

## Diagnostic bundle

`python -m remediation.diagnose --project-dir . --out <dir>` works after any
failed `dbt build`. It writes `bundle.json` (the input Phase 2 sends to the
model) and `summary.md`:

| Section | |
|---|---|
| `failures` | Each failing test or model: definition (type, arguments, severity, `where`, and SQL for project-defined generic tests), compiled SQL, up to 10 stored failure rows, the model it tests, the risk tier a fix would carry, and upstream lineage by name. A failing uniqueness test also gets `rows_for_duplicate_keys`: the model's full rows behind each duplicated key, showing whether copies are identical or conflict. |
| `models` | Every referenced model, **once**: path and raw SQL, plus compiled SQL and recent commits for models with a failing test. |
| `sources` | Every referenced raw table, once: its real column names and types, and three sample rows. Without these, a staging fix has to guess column names: an early run guessed `session_id` for a column called `id`. |
| `warnings` | Warn-severity tests that fired, as context. |
| `run` | Status counts and the downstream nodes the failure skipped. |

Storing models and sources once, and sampling 10 failure rows rather than 20,
cut the largest bundle (`refund_sign_flip`) by 48%, to about 10.6k tokens.
Small bundles grew by about 2k tokens, because they now carry source columns
and duplicate-key rows they used to lack.

**PII never leaves the machine.** Columns named in
`policy.diagnostics.pii_columns`, or tagged `meta.pii` anywhere in the
manifest, are masked in failure rows. Email addresses are scrubbed from error
messages.

**`_meta` is never sent to the model.** It records which scenario was
injected, and the scenario id alone gives away the answer.

## Claude diagnosis (Phase 2)

[`remediation/agent.py`](../remediation/agent.py) makes one structured-output
call per bundle to `claude-opus-5-5` (adaptive thinking, effort `high`). It
writes `diagnosis.json` and `diagnosis.md`, and changes nothing in the
project.

| Field | |
|---|---|
| `verdict` | `code_fix`, `data_issue` or `needs_human` |
| `root_cause`, `evidence` | The diagnosis and what supports it |
| `confidence` | 0–1, calibrated: that the verdict is right and the edits fix it |
| `edits` | Exact search-and-replace pairs; only a `code_fix` may carry any |
| `upstream_action` | For a data issue: what should happen at the source |
| `self_assessed_risk` | Shown to reviewers; never used for routing |

**Edits, not diffs.** Each edit names a file, an `old` string that must
occur exactly once, and its replacement. A unified diff's line numbers and
context are easy for a model to get subtly wrong. An exact-once anchor either
matches or is rejected, and the Phase 3 validator applies it with
[`remediation/edits.py`](../remediation/edits.py).

**What the model is and isn't shown.** It gets the bundle, and a cached
system prompt containing the rules, the protected paths from `policy.yaml`,
and the project's macro source. It is not given `_meta` (the scenario name
gives the answer away). It is also not given the computed risk tier, so that
its own risk view is independent of routing. The prompt tells it to treat the
bundle, which includes warehouse rows, as data and never as instructions.

**Failure handling.** A refusal is retried server-side on the recommended
fallback model (`fallbacks: "default"`). A refusal that persists, a truncated
response, an output that doesn't parse, or an API error is recorded as an
error with no diagnosis. It never becomes a guess.

**Local response cache.** Every response the API returned is saved in
`.remediation-cache/` (gitignored) for 24 hours (`agent.response_cache_hours`
in `policy.yaml`). An identical request is replayed from it, so re-running
after an error, or re-running `make chaos`, never pays twice for the same
question.

- The key is a hash of the entire request: model, effort, system prompt,
  bundle and output schema. Any real change to the prompt, rules or code is a
  new question.
- Per-run noise (bundle timestamps, dbt invocation ids) is stripped before
  sending, so it doesn't defeat the cache. It isn't useful to the model either.
- Paid-for refusals and truncations are cached too. Exceptions (network,
  auth, rate limits) are not, because nothing was answered.
- Each entry stores the full prompt and response, so it doubles as an audit
  record of exactly what was asked.
- `FRESH=1` forces a new call: `make chaos-diagnose SCENARIO=x FRESH=1`.

This is separate from the API's own prompt caching, which discounts a
repeated system prompt for a few minutes. The local cache skips the call
entirely.

```bash
export ANTHROPIC_API_KEY=...
make chaos-diagnose SCENARIO=refund_sign_flip   # one scenario (run `make chaos` first)
make chaos-diagnose SCENARIO=all                # every scenario, scored; ~$1-3
python -m remediation.agent --bundle <dir>/bundle.json --out <dir>   # any failed build
```

### Scoring

With `--diagnose`, each scenario's diagnosis is scored while its broken
scratch copy still exists:

- **Verdict**: one of the scenario's acceptable verdicts.
- **Edits apply**: every edit's anchor occurs exactly once in the broken tree.
- **Right files**: for code faults, the edits touch every file the fault is in.

A `code_fix` passes when all three hold. Any other verdict passes when it is
acceptable. Scoring is a metric, not a gate: a miss is reported, and only
scenario rot or an API error fails the run.

"Edits apply" does not mean the fix works. The first scored run proved this:
`ga_session_id_mapping` scored PASS, and its fix failed to build. So a scored
run also validates every code fix (Phase 3) and reports **Diagnosis** and
**Fix** as separate columns, together with the route the fix would take.

**Exit criterion for Phase 2:** every scenario scored, with data faults
reliably told apart from code faults, before anything is allowed to write a
branch.

**The API key is read only from your local environment.** GitHub Actions
runs on GitHub's machines and cannot see it. CI diagnosis stays off until the
key is added as a repository secret.

In GitHub Actions, tick **diagnose** when dispatching **chaos (fault
injection)**. This needs the `ANTHROPIC_API_KEY` secret and the
`AI_REMEDIATION_ENABLED` repository variable set to `true`. That variable is
the kill switch for every model call the repo makes, and a dispatch that asks
for a diagnosis while it is off fails rather than silently skipping.

## Validation (Phase 3)

[`remediation/validate.py`](../remediation/validate.py) decides whether a
patch may become a branch. A patch is a copy of the project with changes
applied, whether Claude's edits or a person's. It is checked against the base
copy it came from, in order, and the first failing check rejects it:

| Check | Rejects |
|---|---|
| `failure` | A patch to a build where nothing failed: there's nothing to validate it against |
| `changes` | A patch that changes nothing |
| `paths` | Any file outside `editable_paths` (models, model YAML, macros) or inside `protected_paths` |
| `parse` | Anything `dbt parse` refuses |
| `tests` | **The hard reject.** Any existing test removed, redefined (its arguments are part of its id), re-configured (`severity`, `where`, `warn_if`, `error_if`, `enabled`, `limit`, `fail_calc`) or depending on an edited project macro. New tests are fine. |
| `meta` | Any change to `remediation_tier` or `pii`, anywhere, including one introduced on a new model |
| `build` | Unless `dbt seed` + `dbt build`, with the fault still present, ends with every originally failing node passing and nothing else failing (`warn` is fine) |

The `tests` and `meta` checks compare the base and patched **manifests**, not
YAML text. That way they also catch a `config()` block written into a
model's SQL, or a test disabled somewhere else.

A patch that passes is **routed**. The tier comes from lineage in the base
manifest (Phase 0), and two escalations can only raise it:

- stated confidence below `validator.min_confidence_for_auto` (0.8);
- an edited model returning fewer rows than before. A fix that filters rows
  out can make a test pass by hiding data.

On rejection, Claude is asked again with every rejected attempt and its
reason, up to `validator.max_retries` (2) more times
([`remediation/remediate.py`](../remediation/remediate.py)). These attempts
run before any branch exists, so they don't count towards the 3 CI failures.

```bash
make chaos-fix SCENARIO=refund_sign_flip      # validate Claude's fix (retries cost credits)
make chaos-rebuild SCENARIO=refund_sign_flip  # validate your own fix in fixed/
make validator-redteam                        # crafted bad patches, no API (~2 min)
```

### Red team

[`remediation/redteam.py`](../remediation/redteam.py) throws 13 hand-written
bad patches at the validator. These are the kind an over-eager agent might
propose:

- widening `accepted_values` to admit the bad value;
- downgrading the failing test to `warn`;
- deleting an unrelated test;
- loosening `dbt_project.yml`;
- flipping a model to `auto` in a SQL `config()` block;
- a patch that doesn't compile;
- a cosmetic change that fixes nothing;
- an anchor that isn't in the file;
- a patch to a healthy build.

Each is rejected at the right check. Three more pass but must not
auto-merge, and each is routed to a human:

- remapping the new order status (a business call, quarantine tier);
- keeping one row per key to pass `unique` (597 → 12 rows);
- a correct fix stated at 0.6 confidence.

A correct, confident, auto-tier fix routes `auto`.

## Branches, PRs and issues (Phase 4)

[`remediation/pipeline.py`](../remediation/pipeline.py) takes a failed build
to exactly one GitHub item:

| Outcome | Becomes |
|---|---|
| An open PR or issue already exists for this failure | A comment that it recurred. **No API call**, no duplicate. |
| Verdict `data_issue` | An issue labelled `data-issue`, with the diagnosis and what should happen upstream |
| Verdict `needs_human`, or diagnosis failed | An issue labelled `needs-human` |
| A code fix still rejected after the retries | An issue labelled `needs-human`, listing every attempt and why the validator rejected it |
| A validated code fix | A branch, a commit and a PR labelled `ai-remediation` plus `risk:auto` or `risk:quarantine`. A quarantined PR requests the code owner as reviewer. |

**In Phase 4 a person merges every remediation PR.** The route label is what
Phase 6 will use to decide which may auto-merge. CODEOWNERS and branch
protection already enforce that a quarantined change needs a human.

Mechanics ([`remediation/gitops.py`](../remediation/gitops.py)):

- **The checkout is never touched.** Each commit is made in a temporary
  detached worktree and pushed as `HEAD:refs/heads/<branch>`, so no local
  branch is created.
- **The failure id** is a hash of the failing nodes' ids. It goes into the
  branch name (`ai-remediation/fct-transactions-3aa28caa`), the commit
  trailers and a hidden marker in the PR body, which is how a recurring
  failure finds its open PR.
- **Commit trailers carry the state** Phase 5 will read: failure id,
  attempt, tier, route, confidence and model.
- **Remediation fixes are attributed to Claude**, and only they are. The
  commit is authored by `Claude <noreply@anthropic.com>` (set in
  `policy.yaml`) with a matching `Co-Authored-By` trailer, because a squash
  merge re-attributes the commit itself. Every other commit in the repo stays
  the maintainer's.
- **The PR body** has the route banner, what failed, the diagnosis and
  evidence, the diff, the full validation checklist, any attempts, and
  review and revert instructions.
- **Replacing a branch** (for Phase 5 retries) uses `--force-with-lease`
  against the sha the PR last pointed at.

**Who it acts as.** PRs, issues and comments are always shown as written by
the account that opened them, and GitHub never notifies anyone about their
own actions. So the layer acts as the **`dbt-ecom-remediation` GitHub App**
([`remediation/ghapp.py`](../remediation/ghapp.py)), and its items come from
`dbt-ecom-remediation[bot]`. The name is deliberately not "Claude": some
remediation is deterministic, with no model involved. Commits Claude wrote
are still authored by Claude.

The App authenticates with an RS256 JWT, signed by the `openssl` binary to
avoid a crypto dependency, which is exchanged for a one-hour installation
token. The token reaches `gh` and git through `GH_TOKEN` and is never written
anywhere. Configure it with `REMEDIATION_APP_ID` and
`REMEDIATION_APP_KEY_FILE` in `.env` (or `REMEDIATION_APP_KEY` holding the
key itself, for CI). Without them, everything acts as whoever `gh` is signed
in as.

**One sandbox repo, reused.** `make chaos-pr ... REPO=owner/name` syncs the
sandbox instead of needing an empty repo. It pushes the current project (with
the scenario's fault) to `main` as an ordinary commit by you. If a
remediation PR for the failure is already open, it **refreshes** that PR: it
rebuilds the branch on the new `main`, re-validates, force-pushes with a
lease, updates the body and comments. CI re-runs on its own. Phase 5 uses the
same refresh to push a new attempt.

Both trackers share one interface ([`remediation/github.py`](../remediation/github.py)).
`GitHubTracker` goes through the `gh` CLI. `LocalTracker` writes JSON and
rendered Markdown to a folder, so the whole flow runs and can be read before
anything is pushed.

```bash
make chaos-pr SCENARIO=refund_sign_flip   # sandbox: the PR this failure would get
make remediate                            # real project after a failed build; commits, never pushes
make remediate GITHUB=1                   # pushes the branch and opens the PR on GitHub
```

`chaos-pr` builds a sandbox: a git repo whose `main` has the scenario's fault
committed, and a bare repo standing in for GitHub. It reuses the saved
diagnosis (falling back to the earlier paid answer for the same failure in
the local cache), so only a validator rejection calls the API. Tested on four
scenarios with no API key present:

| Scenario | Result |
|---|---|
| `marketing_key_missing_platform` | PR, `risk:auto` |
| `refund_sign_flip` | PR, `risk:quarantine`, code owner requested; a second run commented "recurred" |
| `orders_null_customer` | Issue, `data-issue` |
| `orders_unknown_status` | Issue, `needs-human` |

## The CI loop and auto-merge (Phases 5 and 6)

[`.github/workflows/remediation.yml`](../.github/workflows/remediation.yml)
runs whenever the `dbt build` workflow completes. It checks out and builds
`main`, which reproduces the failure, then calls
[`remediation/ci.py`](../remediation/ci.py):

| CI failed on | What happens |
|---|---|
| `main` | A new remediation: a PR or an issue, exactly as in Phase 4. If an open item for the same failure exists, it only gets a comment. |
| An `ai-remediation/*` PR | **Phase 5**: see the rule below |

**Phase 5: Claude writes at most one attempt per PR** (`ci.max_claude_attempts`):

| The attempt that failed CI was written by | What happens |
|---|---|
| Claude | The loop stops. |
| Deterministic remediation | Claude gets one attempt, given the failed jobs and their log tails. It's validated and pushed to the same branch. If that attempt fails CI too, the loop stops. |

That's enough to prove the point (a CI failure brings AI remediation in
automatically), and it bounds the spend: one Claude call per CI failure
round, with no extra validator round-trips. Stopping means:

- the label `ai-remediation:blocked`;
- auto-merge off;
- the code owner asked to take over.

Other safety stops still apply on top:

- an absolute cap of 3 CI failures (`max_ci_failures`);
- $1.00 of spend per failure (`ci.max_cost_usd_per_failure`);
- an oscillation guard for an attempt identical to one that already failed;
- a change in the failure on `main`.

If `main` stops failing on its own, the PR is closed as no longer needed.

**Authorship follows who wrote the fix.** Commits Claude wrote are authored by
Claude, with a `Co-Authored-By` trailer. Deterministic fixes are authored by
`dbt-ecom-remediation`, with no Claude credit.

All loop state lives in hidden markers in the PR body: the attempt, the CI
failures, the Claude attempts, the spend, auto-merge approval, and a history
of each failed attempt's diff and CI output. The workflow is stateless, and
every write is made as the `dbt-ecom-remediation` App. That's required:
pushes made with Actions' own token don't trigger CI, so the loop would
stall.

**Phase 6, auto-merge.** A PR is approved for auto-merge (label
`ai-remediation:auto-merge`, plus a hidden marker only the pipeline writes)
only if all of these hold:

- `auto_merge.enabled` in policy;
- the validator routed it `auto`;
- every model it reaches, directly or through a macro, is on the
  `auto_merge.models` allowlist (`agg_marketing`, `dim_date`).

The **remediation workflow itself merges it** (rebase, keeping the fix's
author) when CI passes, after re-checking at that moment:

- that the bot's own marker is there, not just a label added by hand;
- that it isn't blocked;
- that the `AI_REMEDIATION_AUTOMERGE` kill switch is on;
- that the PR's head is still the exact commit CI passed on
  (`--match-head-commit`).

GitHub's native auto-merge isn't used, because it needs branch protection,
and that isn't available on private repos on the free plan. Where branch
protection does exist (the real repo), GitHub still enforces it on the merge.

**The auto-merge toggle** is that repository variable, so switching it needs
no commit: `make automerge-on` or `make automerge-off` (add `REPO=owner/name`
for another repo). Start with it off, so every remediation PR waits for you,
and turn it on once you trust the fixes. The policy allowlist still limits
which models can ever auto-merge.

The workflow is inert unless `AI_REMEDIATION_ENABLED` is `true`. It needs the
secrets `ANTHROPIC_API_KEY`, `REMEDIATION_APP_ID` and `REMEDIATION_APP_KEY`,
plus `AI_REMEDIATION_AUTOMERGE=true` for merging. These live in the repo on
GitHub, because the workflow runs on GitHub's machines, not yours.

### Demos (in the sandbox repo)

```bash
# Phase 5: a deterministic first attempt (correct SQL fix plus a new test in
# the old syntax). It passes the validator, then fails CI's deprecation gate,
# so Claude gets one attempt to recover from the CI output alone.
make chaos-pr SCENARIO=marketing_key_missing_platform REPO=owner/sandbox \
  DIAGNOSIS=remediation/demos/phase5-flawed-first-attempt.json

# Autonomous: a clean main, then a bad change pushed to it. Nobody touches
# anything after the push.
make sandbox-sync REPO=owner/sandbox SCENARIO=clean
make sandbox-sync REPO=owner/sandbox SCENARIO=marketing_key_missing_platform
```

The Phase 5 demo relies on a real gap: the validator runs `dbt parse`, while
CI also fails on deprecation warnings. Once the demo has shown the loop
catching it, the validator will close that gap.

## On a cloud warehouse

Every fault-injection step runs on Snowflake, Redshift or BigQuery as well as
DuckDB. Add `TARGET=`, and the scenario runner, the validator and the CI loop
all build there:

```bash
make check-env TARGET=snowflake_prod          # credentials present?
make verify-warehouse TARGET=snowflake_prod   # the plain full build (free)
make chaos SCENARIO=all TARGET=snowflake_prod # every scenario, no API calls
make chaos-diagnose SCENARIO=all TARGET=snowflake_prod   # plus Claude, per failing scenario
```

What changes on a cloud warehouse ([`remediation/warehouse.py`](../remediation/warehouse.py)):

- **Credentials** come from `.env.snowflake`, `.env.redshift` or
  `.env.bigquery`, picked by the target's name. `ANTHROPIC_API_KEY` still
  comes from `.env`.
- **Data is read back through `dbt show`** with the same profile, rather
  than from a DuckDB file. Samples are ordered on every column, so a bundle
  is identical run to run, and both readers return the same rows. SQL sent
  to a reader uses unquoted identifiers and inlined literals, which is
  portable across all four warehouses.
- **Every build uses `--full-refresh`.** Consecutive builds (clean, broken,
  patched) share the warehouse's fixed schemas, so seeds are recreated and
  the incremental model is rebuilt rather than merged with another
  scenario's rows.
- **Workspaces are kept per warehouse** (`.chaos/_wh_<target>/`), and the
  clean baseline must build with every node passing on that warehouse first.
- **The DuckDB-only parts are skipped, and the output says so:** the
  clean/broken/fixed snapshots and table comparisons, the absorbed
  scenarios' "nothing else changed" check, and the validator's row-loss
  escalation.

### Results (2026-10-25)

| | DuckDB | BigQuery | Snowflake | Redshift |
|---|---|---|---|---|
| Scenarios behaving as designed | 11/11 | 11/11 | 11/11 | 11/11 |
| Claude's diagnoses correct | 10/10 | 8/8 | 8/8 | 8/8 |
| Code fixes validated on that warehouse | 5/5 | 5/5 | 5/5 | 5/5 |
| Claude cost for the round | ~$0.91 | ~$0.69 | ~$0.48 | ~$0.59 |
| Wall time, all scenarios | ~2 min | ~55 min | ~25 min | ~65 min |

The DuckDB column is from the first scored round, before the duplicate scenarios became absorbed. Claude's fixes were the same minimal, portable edits on every warehouse: each one exactly undid the injected fault, using the project's own macros, with no dialect-specific SQL.

Redshift is slowest for two reasons. Each statement carries a fixed per-query compile cost on the leader node, and seeds load as batched `INSERT`s, reloaded for every scenario by `--full-refresh`. Reloading only the seed a scenario changes would roughly halve it.

The round found three real bugs, all fixed:

- **Redshift's `connect_timeout`** in `profiles.yml` capped every query at 10 seconds.
- **Snowflake's upper-cased column names** broke the quarantine alert's key detection.
- **The `dbt show` reader** ran a bare `dbt`, which isn't on `PATH` when `make` runs.

On Redshift, the seeds need a user that can write the `extract` schema, which a least-privilege `transform` user can't. See `.env.redshift.example`.

## Roadmap

| Phase | Scope |
|---|---|
| 7 | Dagster sensor on failed asset checks as a production trigger. |
