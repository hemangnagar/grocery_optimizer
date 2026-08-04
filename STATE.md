# Project State — grocery-optimizer

_Working handoff doc. Last updated 2026-08-04._

## Build-order status (see CLAUDE.md for the plan)

NOTE: CLAUDE.md's build order was renumbered for the v2 pivot (single-store
verdict as primary product behavior). The pre-pivot steps below (1-8, "backend
core") are DONE and stay listed for history; v2 step numbering restarts at 2
("Single-store verdict engine") since v2 step 1 = this whole pre-pivot list.

### Pre-pivot backend core (DONE)
- [x] **1. Scaffold + DuckDB schema** — bronze/silver/gold in `src/grocery_optimizer/sql/`.
- [x] **2. Kroger API client + key test** — Harris Teeter IS exposed (chain code `HART`); location-filtered pricing works.
- [x] **3. Lidl ESI prober + KCL/Aldi parser** — overview + Aldi deals work; Lidl product-level DEFERRED (JS/image-walled SPA).
- [x] **4. Whole Foods** — replicated JSON API via httpx (not Playwright); Vienna store 10065.
- [x] **5. Silver normalize + RapidFuzz resolution** — units, canonical unit prices, conservative token_sort_ratio matching.
- [x] **6. Synthetic basket + optimizer + gold savings view** — `grocery-optimize`.
- [ ] **7. Windows Task Scheduler Tuesday-night pull** — TODO.
- [~] **8. Agentic layer** — entity-resolution **adjudicator DONE** (`grocery-adjudicate`); parser self-healing + plan narrator TODO.

### v2 pivot build order
- [x] **2. Single-store verdict engine** — `silver/verdict.py` (`build_verdict`):
  per-store totals ranked with a full-coverage-required winner, missing-item
  flags for partial-coverage stores, exact-brand (match_confidence >= 0.97) vs
  flexible (>= 0.85) substitution modes, split-savings footnote with a
  deterministic "probably not worth it" heuristic. `grocery-optimize` now
  prints the verdict first, split detail as a footnote. Tests: `tests/test_verdict.py`.
- [x] **3. Category taxonomy at silver + hard cross-category match block** —
  `silver/taxonomy.py`: deterministic coarse categories
  (produce/protein/dairy/snack/pantry/frozen/household) assigned at ingest from
  source hint first, then ordered name rules where FORM beats INGREDIENT
  ("chicken crackers"→snack, "chicken broth"→pantry). Hard guard everywhere:
  resolve.py never scores cross-category candidates, basket matching filters by
  the list item's coarse category, the adjudicator queue skips cross-category
  pairs (zero tokens), and `grocery-normalize` backfills pre-taxonomy rows +
  severs existing cross-category rapidfuzz links for re-resolution
  (`enforce_category_guard`; llm/manual links are spared). NULL category never
  blocks (recall over precision). `coarse_category` added to source_products,
  canonical_products, gold_current_prices (ALTER IF NOT EXISTS migration in
  02_silver.sql). Tests: `tests/test_taxonomy.py`.
- [x] **4. LLM adjudicator + DuckDB verdict cache + human-review queue** —
  `silver/verdict_cache.py` + `grocery-adjudicate-list`: (normalized_list_term,
  canonical_id) verdicts cached in `match_verdicts` (UNIQUE pair key, upsert);
  cache consulted BEFORE any LLM call so each pair costs one call ever;
  basket/gold selection reads ONLY the cache (`confident_matches` >= 0.85 +
  heuristic fallback minus cached no_matches) -> deterministic replay.
  Below-threshold verdicts stored but unusable until a human resolves them via
  `match_review_queue` (`review_verdict`: approve keeps, reject flips, both ->
  confidence 1.0, decided_by='human'). Category guard outranks the cache.
  Distinct from the step-8 entity-resolution adjudicator (`grocery-adjudicate`).
  Tests: `tests/test_verdict_cache.py`.
- [x] **5. PWA frontend** — FastAPI (`api/app.py`, `grocery-serve`, port 8177)
  serving gold-only endpoints (`/api/verdict`, `/api/basket`); one-screen
  mobile PWA (`webapp/`: verdict hero, ranked stores, coverage flags,
  exact/flexible toggle, split footnote, installable manifest + SW, dark/light).
  `scripts/seed_demo.py` (`grocery-seed-demo`) seeds a deterministic synthetic
  demo dataset (4 stores x 26 items) designed so the substitution modes
  diverge (flexible → Harris Teeter $123.17; exact → Whole Foods, kroger's two
  0.90-conf store-brand subs drop out) and Aldi is cheap-but-partial. Demo
  screenshots in `docs/screenshots/`, embedded in README. Tests: `tests/test_api.py`.
- [ ] **6. Windows Task Scheduler job** — TODO (same as pre-pivot step 7).
- [~] **7. Parser self-healing agent + weekly narrator** — **narrator DONE**
  (`gold/narrator.py`, `grocery-narrate`): deterministic fact pack from the
  verdict engine (every total/delta/count precomputed + gold price_id
  provenance) -> LLM writes one paragraph -> hard audit gate rejects any
  number not in the fact pack (raises, stores nothing) -> audited narration
  stored in `narrations` with pipeline-appended citations. `/api/narrative`
  + PWA serve ONLY stored rows (narrate once weekly, zero tokens per view).
  `--facts-only` previews for free. Tests: `tests/test_narrator.py`.
  Parser self-healing agent TODO.
- [ ] **8. Showcase phase** — TODO.

## Coverage (in gold now)
Kroger/Harris Teeter (~1561, official API, commercially safe) · Whole Foods (~180, open API, safe) ·
Trader Joe's (~300, GraphQL via Edge, **personal-use / ToS-review-before-commercial**) · Aldi (~18, KCL aggregator).
Investigated but DEFERRED: Safeway (`xapi`, brittle/ToS), Giant (Ahold, gated).

## How to run
```powershell
uv run grocery-init-db          # (re)build schema
uv run grocery-kroger-fetch     # Kroger catalog (needs KROGER_* in .env)
uv run grocery-wfm-fetch        # Whole Foods
uv run grocery-tj-fetch         # Trader Joe's (Edge/Playwright)
uv run grocery-kcl-fetch        # Aldi via KCL
uv run grocery-lidl-probe       # Lidl overview
uv run grocery-normalize [--rebuild]   # bronze -> silver -> gold + resolve
uv run grocery-adjudicate [--limit N]  # LLM entity adjudicator (spends API credits)
uv run grocery-adjudicate-list [--limit N] [--dry-run]  # list-term adjudicator -> verdict cache
uv run grocery-narrate [--facts-only]  # audited weekly narration -> narrations table + PWA
uv run grocery-optimize         # synthetic basket -> single-store verdict (split as footnote)
uv run grocery-seed-demo        # deterministic synthetic demo dataset (no creds)
uv run grocery-serve            # FastAPI + verdict PWA on http://localhost:8177
uv run pytest                   # 91 tests
```
Latest run: no single in-range store covers all 26 items yet (thin cross-store
recall, see limitation 3 below), so the split ($124.99 across 4 stores) is
reported as the only full-coverage option. Exact vs flexible substitution
modes already diverge on real data (e.g. kroger covers 14/26 in exact mode vs
12/26 in flexible, at different totals).

## Known limitations (next-up work)
1. **Item selection is heuristic until the verdict cache warms** — keyword
   matching picks odd products ("Chicken Breast Bites" for chicken). v2 step 4
   fixes this as the cache fills: run `grocery-adjudicate-list` once per new
   candidate set and cached verdicts override the heuristic deterministically.
2. **Size normalization** — multi-unit packs ("1 oz 16 ct", 10-lb potato bag) take the first size token in `units.py`, so some unit-price/spread comparisons are apples-to-oranges (e.g. Russet Potato $5.99 vs $1.29).
3. **Cross-store recall is thin** (~6 of 1531 canonicals at 2+ stores) — grows by draining the ~575-pair `resolution_queue` via `grocery-adjudicate` (set `GROCERY_ADJUDICATOR_MODEL=claude-haiku-4-5` to cut cost). This is also why no store wins outright yet — verdict logic is correct but starved of coverage.
4. **Exact-brand mode is a confidence-threshold proxy, not real brand tracking** — `basket_items`/canonical products have no "requested brand" field yet (no real user-entered lists exist), so v2-step-2's "exact brands" mode approximates it via a stricter `match_confidence >= 0.97` cutoff. Revisit once category taxonomy (v2 step 3) and real user lists land.

## Environment / ops notes
- Secrets in **project `.env` only** (gitignored): `KROGER_CLIENT_ID/SECRET`, `ANTHROPIC_API_KEY`. `config.py` loads `.env` with `override=True`.
- A global User-scope `ANTHROPIC_API_KEY` existed and was **removed** (it forced API-rate billing, per CLAUDE.md). Restart Claude Code in a fresh shell to return to subscription billing.
- `git` is not on PATH — PortableGit at `%LOCALAPPDATA%\Programs\PortableGit\cmd`; prepend it per command. Branch `main`; remote `origin` set (GitHub), nothing pushed. `data/` is gitignored (the moat).

## Suggested next steps
_(updated 2026-08-04 — steps 3/4/5 + narrator shipped this session)_

On the home box (needs .env / Windows):
- `git pull origin main && uv sync`, then `uv run grocery-normalize` on the
  real DB (backfills taxonomy + severs cross-category links) → drain
  `resolution_queue` via `grocery-adjudicate` (Haiku for cost) to grow
  cross-store recall → `grocery-adjudicate-list` to warm the verdict cache →
  `grocery-narrate` for the first real audited narration → re-screenshot the
  PWA showing verdict + narrative.
- v2 step 6: Windows Task Scheduler Tuesday-night pull.

Remote-session friendly (no creds needed):
- **Geolocation + store coverage** (agreed next feature): populate `stores`
  lat/lon via chain locator APIs (Kroger Locations API is official), static
  zip→centroid geocode for HOME_ZIP, haversine radius filter in gold, PWA
  distance chips, demo-seed lat/lons. Groundwork exists: `config.HOME_ZIP`,
  `SEARCH_RADIUS_MILES`, empty `stores.lat/lon/zip` columns.
- Parser self-healing agent (last dashed box on the diagram) → size
  normalization fix (limitation 2) → step 8 showcase polish.
