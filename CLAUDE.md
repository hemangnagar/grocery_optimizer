# Grocery Basket Optimizer — Project Spec

## What this is

A DC-metro grocery price optimization pipeline. Ingests weekly prices/deals from
multiple chains (Giant, Safeway, Harris Teeter, Whole Foods, Aldi, Lidl), normalizes
disparate sources into canonical products, and recommends the cheapest store split
for a predicted weekly basket. Runs on an always-on Windows home server (Minisforum
UM870). MVP is personal-use; portfolio piece second; possible venture later.

## Core design principle (non-negotiable)

The deterministic pipeline is the sole source of truth. LLM agents assist judgment
and narrate results — they never invent data, never fabricate a join, never write
directly to gold. Every number in any user-facing output must be queryable from the
gold layer.

## Architecture: lightweight medallion (vocabulary intentional, formality minimal)

* **Bronze**: raw timestamped responses (JSON/HTML) saved to disk verbatim before any
parsing. This is the time machine — weekly ads expire and are unrecoverable, so
raw preservation is mandatory. Never parse-and-discard.
* **Silver**: parsed + normalized records in DuckDB. Entity resolution to canonical
products, canonical unit prices ($/lb vs per-package vs per-count), per-source
confidence scores.
* **Gold**: query-facing views — cheapest source per canonical item this week,
basket optimization output.
* NO Spark, NO Delta, NO orchestration framework, NO embeddings, NO vector store.
DuckDB + files + Task Scheduler. Total infra should stay \~small. All code must
run on Windows natively (pathlib for paths, no bash-isms in scripts).

## Data sources (feasibility-tested July 2026)

|Source|Access|Approach|Confidence tier|
|-|-|-|-|
|Kroger Developer API|Official, free, OAuth2 client-credentials|Products/Catalog API, location-filtered pricing. VERIFY whether Harris Teeter banner locations are exposed (10-min key test). Worst case: Kroger-banner calibration data|Gold-standard|
|Whole Foods (wholefoodsmarket.com)|No robots block; Next.js shell, prices render client-side per store|Playwright headless OR replicate frontend JSON API calls with store ID|High|
|Lidl|Site JS-walled, but flyer backend exposes parameterized ESI endpoints (e.g. lidl.com/flyer/esi-overview/...\&region\_shortname=P1A\&store\_id=NNNN)|Probe/enumerate ESI flyer endpoints directly|High|
|Aldi|Instacart storefront, hard robots.txt block — do NOT scrape directly|Aggregator parsing (The Krazy Coupon Lady serves structured deals with prices, regular prices, discount %, sizes, expiry, and embedded Aldi canonical product IDs — good join keys)|Medium|
|Giant (Ahold), Safeway (Albertsons)|Sites gated/JS-rendered/sign-in walls|Aggregator parsing (weeklyadfinder, weeklyadhunters etc.); some serve structured text, some only ad images (image path = OCR/vision, defer)|Medium|

Pull schedule: most chains refresh ads Wednesdays → Windows Task Scheduler job
(schtasks) Tuesday night running the ingestion entry point.

## Synthetic-first validation

Before any real customer data: generate synthetic family-of-four weekly baskets
(realistic categories: produce, proteins, dairy, kids' snacks, staples) and run them
against real scraped prices to prove the optimizer and measure savings. Real receipts
come later from beta users to calibrate.

## v2 PIVOT: single-store verdict (primary product behavior)

Basket-splitting is behaviorally weak — nobody drives to 3 stores to save $18.
Primary output is ONE store recommendation for the user's whole list:

* Sum the basket WITHIN each store, rank store totals: "Giant wins this week at
$91.40 — Safeway would've been $107.80."
* Keep the multi-store split computed silently; surface only as a delta footnote:
"A perfect 3-store split would save another $8.20 — probably not worth it."
* Coverage rule: a store can only win if it covers the list; missing items either
penalized with a shadow price or flagged: "Lidl: $79 but missing 3 of 18 items."
* Substitution honesty: two verdict modes — "cheapest with exact brands" vs
"cheapest if flexible (store brands OK)" — driven by match confidence scores.

## Category guard + LLM adjudicator + verdict cache (fixes chicken-crackers class bugs)

Known failure: fuzzy matching matched list item "chicken" to "chicken crackers"
(snack) instead of drumsticks/breast (protein). Fix is layered:

1. DETERMINISTIC FIRST: coarse category taxonomy assigned at silver
(protein / produce / dairy / snack / pantry / frozen / household). Hard rule:
matches may never cross categories. Zero tokens spent, kills most errors.
2. LLM ADJUDICATOR for survivors: ambiguous (list\_term, candidate\_product) pairs
go to an LLM returning {verdict: match|no\_match, confidence, reason}.
3. VERDICT CACHE (the "library"): every adjudication stored in a DuckDB table
keyed on (normalized\_list\_term, product\_id). Cache is consulted BEFORE any LLM
call — each ambiguous pair costs one LLM call ever. Gold reads ONLY cached
verdicts, never live LLM output → fully deterministic replay. Below-confidence
verdicts land in a human-review queue. The cache compounds into a proprietary
regional matching library and is itself a showcase artifact.

## Frontend (build as PWA first, NOT native iOS yet)

* Responsive PWA: paste/manage grocery list → one-screen verdict (winning store,
total, delta vs other stores, split-savings footnote, coverage + substitution
flags, exact-brands/flexible toggle). Installable to iPhone home screen.
* Local network only for now (served from the UM870). NO public internet exposure
yet; native iOS/App Store (Expo wrapper, $99 dev account, tunnel + auth) is a
possible phase two AFTER showcase — do not build it first.
* Backend: FastAPI serving gold-layer queries. Frontend never computes prices.

## Agentic layer (showcase, built AFTER deterministic core works)

1. **Parser self-healing agent**: on schema drift, diff new raw structure vs expected,
propose patched parser, validate by re-parsing last known-good bronze files,
promote only if output matches. Deterministic validation gate.
2. **Entity-resolution adjudicator**: RapidFuzz handles easy \~80% of product matching
deterministically; ambiguous remainder goes to LLM adjudicator returning verdict +
confidence; below-threshold → human review queue, never silently into gold.
3. **Weekly plan narrator**: reads gold view, produces "your basket, split across N
stores, saving $X" — every number queried, cite gold row IDs.

## Stack

Python 3.12, DuckDB, httpx, Playwright (WFM only), RapidFuzz, pandas. Secrets in
per-project .env (python-dotenv), never in Windows user/system environment
variables — this machine also runs trading bots with API keys; ANTHROPIC\_API\_KEY
must NOT be set globally or Claude Code bills API rates instead of subscription.

## Repo hygiene (two-repo pattern)

* THIS repo may eventually go public (MIT license): architecture, parsers, agents,
synthetic generator, small seeded demo dataset only.
* NEVER commit: harvested price history, real store configs beyond samples, API keys,
any real customer/receipt data. Accumulated price history is the moat and stays dark.
* Check retailer ToS before anything commercial; personal-use experiment for now.
* Respect robots.txt (this is why Aldi is aggregator-only). Polite rate limits on
all fetching.

## Build order (v2 — backend core exists as of July 14 session)

1. ~~Scaffold + schema + fetchers + basic optimizer~~ (done evening 1)
2. Single-store verdict engine: per-store basket totals, ranking, split-delta
footnote, coverage rule, substitution modes
3. Category taxonomy at silver + hard cross-category match block
4. LLM adjudicator + DuckDB verdict cache + human-review queue
5. PWA frontend (FastAPI gold endpoints + one-screen verdict UI)
6. Windows Task Scheduler job for Tuesday-night pulls
7. Parser self-healing agent + weekly narrator
8. Showcase phase: two-repo split, scrub secrets/harvested history, seed demo
dataset, MIT license, README with architecture diagram, demo video, deck

