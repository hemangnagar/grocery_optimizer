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
- **Bronze**: raw timestamped responses (JSON/HTML) saved to disk verbatim before any
  parsing. This is the time machine — weekly ads expire and are unrecoverable, so
  raw preservation is mandatory. Never parse-and-discard.
- **Silver**: parsed + normalized records in DuckDB. Entity resolution to canonical
  products, canonical unit prices ($/lb vs per-package vs per-count), per-source
  confidence scores.
- **Gold**: query-facing views — cheapest source per canonical item this week,
  basket optimization output.
- NO Spark, NO Delta, NO orchestration framework, NO embeddings, NO vector store.
  DuckDB + files + Task Scheduler. Total infra should stay ~small. All code must
  run on Windows natively (pathlib for paths, no bash-isms in scripts).

## Data sources (feasibility-tested July 2026)
| Source | Access | Approach | Confidence tier |
|---|---|---|---|
| Kroger Developer API | Official, free, OAuth2 client-credentials | Products/Catalog API, location-filtered pricing. VERIFY whether Harris Teeter banner locations are exposed (10-min key test). Worst case: Kroger-banner calibration data | Gold-standard |
| Whole Foods (wholefoodsmarket.com) | No robots block; Next.js shell, prices render client-side per store | Playwright headless OR replicate frontend JSON API calls with store ID | High |
| Lidl | Site JS-walled, but flyer backend exposes parameterized ESI endpoints (e.g. lidl.com/flyer/esi-overview/...&region_shortname=P1A&store_id=NNNN) | Probe/enumerate ESI flyer endpoints directly | High |
| Aldi | Instacart storefront, hard robots.txt block — do NOT scrape directly | Aggregator parsing (The Krazy Coupon Lady serves structured deals with prices, regular prices, discount %, sizes, expiry, and embedded Aldi canonical product IDs — good join keys) | Medium |
| Giant (Ahold), Safeway (Albertsons) | Sites gated/JS-rendered/sign-in walls | Aggregator parsing (weeklyadfinder, weeklyadhunters etc.); some serve structured text, some only ad images (image path = OCR/vision, defer) | Medium |

Pull schedule: most chains refresh ads Wednesdays → Windows Task Scheduler job
(schtasks) Tuesday night running the ingestion entry point.

## Synthetic-first validation
Before any real customer data: generate synthetic family-of-four weekly baskets
(realistic categories: produce, proteins, dairy, kids' snacks, staples) and run them
against real scraped prices to prove the optimizer and measure savings. Real receipts
come later from beta users to calibrate.

## Agentic layer (showcase, built AFTER deterministic core works)
1. **Parser self-healing agent**: on schema drift, diff new raw structure vs expected,
   propose patched parser, validate by re-parsing last known-good bronze files,
   promote only if output matches. Deterministic validation gate.
2. **Entity-resolution adjudicator**: RapidFuzz handles easy ~80% of product matching
   deterministically; ambiguous remainder goes to LLM adjudicator returning verdict +
   confidence; below-threshold → human review queue, never silently into gold.
3. **Weekly plan narrator**: reads gold view, produces "your basket, split across N
   stores, saving $X" — every number queried, cite gold row IDs.

## Stack
Python 3.12, DuckDB, httpx, Playwright (WFM only), RapidFuzz, pandas. Secrets in
per-project .env (python-dotenv), never in Windows user/system environment
variables — this machine also runs trading bots with API keys; ANTHROPIC_API_KEY
must NOT be set globally or Claude Code bills API rates instead of subscription.

## Repo hygiene (two-repo pattern)
- THIS repo may eventually go public (MIT license): architecture, parsers, agents,
  synthetic generator, small seeded demo dataset only.
- NEVER commit: harvested price history, real store configs beyond samples, API keys,
  any real customer/receipt data. Accumulated price history is the moat and stays dark.
- Check retailer ToS before anything commercial; personal-use experiment for now.
- Respect robots.txt (this is why Aldi is aggregator-only). Polite rate limits on
  all fetching.

## Build order
1. Scaffold + DuckDB schema (bronze manifest, silver products/prices, gold views)
2. Kroger API client + key test (does Harris Teeter appear?)
3. Bronze fetchers: Lidl ESI prober, KCL aggregator parser
4. Whole Foods Playwright scraper (inspect real JSON responses first)
5. Silver normalization + RapidFuzz entity resolution
6. Synthetic basket generator + optimizer + gold savings view
7. Windows Task Scheduler job for Tuesday-night pulls
8. Agentic layer (order above)
