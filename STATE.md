# Project State — grocery-optimizer

_Working handoff doc. Last updated 2026-07-14._

## Build-order status (see CLAUDE.md for the plan)
- [x] **1. Scaffold + DuckDB schema** — bronze/silver/gold in `src/grocery_optimizer/sql/`.
- [x] **2. Kroger API client + key test** — Harris Teeter IS exposed (chain code `HART`); location-filtered pricing works.
- [x] **3. Lidl ESI prober + KCL/Aldi parser** — overview + Aldi deals work; Lidl product-level DEFERRED (JS/image-walled SPA).
- [x] **4. Whole Foods** — replicated JSON API via httpx (not Playwright); Vienna store 10065.
- [x] **5. Silver normalize + RapidFuzz resolution** — units, canonical unit prices, conservative token_sort_ratio matching.
- [x] **6. Synthetic basket + optimizer + gold savings view** — `grocery-optimize`.
- [ ] **7. Windows Task Scheduler Tuesday-night pull** — TODO.
- [~] **8. Agentic layer** — entity-resolution **adjudicator DONE** (`grocery-adjudicate`); parser self-healing + plan narrator TODO.

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
uv run grocery-optimize         # synthetic basket -> cheapest store split
uv run pytest                   # 33 tests
```
Latest optimizer result: a 26-item family-of-four basket = **$124.99 across 4 stores**.

## Known limitations (next-up work)
1. **Item selection is heuristic** — keyword matching picks odd products ("Chicken Breast Bites" for chicken, milk-as-quart). Fix = the step-8 **plan narrator / LLM** picking the right canonical.
2. **Size normalization** — multi-unit packs ("1 oz 16 ct", 10-lb potato bag) take the first size token in `units.py`, so some unit-price/spread comparisons are apples-to-oranges (e.g. Russet Potato $5.99 vs $1.29).
3. **Cross-store recall is thin** (~6 of 1531 canonicals at 2+ stores) — grows by draining the ~575-pair `resolution_queue` via `grocery-adjudicate` (set `GROCERY_ADJUDICATOR_MODEL=claude-haiku-4-5` to cut cost).

## Environment / ops notes
- Secrets in **project `.env` only** (gitignored): `KROGER_CLIENT_ID/SECRET`, `ANTHROPIC_API_KEY`. `config.py` loads `.env` with `override=True`.
- A global User-scope `ANTHROPIC_API_KEY` existed and was **removed** (it forced API-rate billing, per CLAUDE.md). Restart Claude Code in a fresh shell to return to subscription billing.
- `git` is not on PATH — PortableGit at `%LOCALAPPDATA%\Programs\PortableGit\cmd`; prepend it per command. Branch `main`; remote `origin` set (GitHub), nothing pushed. `data/` is gitignored (the moat).

## Suggested next steps
Plan narrator (smarter picks + cited weekly plan) → size normalization fix → step 7 (Task Scheduler).
