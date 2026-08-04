# Grocery Basket Optimizer

A DC-metro grocery price optimization pipeline. Ingests weekly prices/deals from
multiple chains (Giant, Safeway, Harris Teeter, Whole Foods, Aldi, Lidl),
normalizes disparate sources into canonical products, and recommends the cheapest
store split for a predicted weekly basket.

Lightweight medallion architecture on **DuckDB + files + Task Scheduler** — no
Spark, no orchestration framework, no vector store. Runs natively on Windows.

- **Bronze** — raw timestamped fetch responses saved to disk verbatim (the time
  machine; weekly ads expire). DuckDB holds a manifest of every artifact.
- **Silver** — parsed, normalized records: canonical products, canonical unit
  prices, per-source confidence, entity resolution.
- **Gold** — query-facing views: cheapest source per canonical item this week,
  and (later) basket optimization output.

The deterministic pipeline is the sole source of truth. Agents assist judgment
and narrate results — they never write directly to gold.

## Architecture

The LLM proposes; the pipeline disposes. LLMs sit at three judgment points, each
behind a deterministic gate — they return verdicts and proposals, and only the
pipeline writes. Gold reads cached verdicts, never live LLM output, so every run
replays identically. A full annotated diagram lives at
[`docs/architecture.html`](docs/architecture.html); the flow in brief:

```mermaid
flowchart LR
    subgraph SRC [Sources]
        K[Kroger / Harris Teeter API]
        W[Whole Foods JSON]
        T[Trader Joe's GraphQL]
        A[Aldi via aggregator]
        L[Lidl ESI flyers]
    end
    subgraph BR [Bronze]
        R[raw JSON/HTML, verbatim
        + manifest]
    end
    subgraph SV [Silver - DuckDB]
        N[normalize to canonical unit prices]
        X[coarse category taxonomy]
        E[RapidFuzz entity resolution]
    end
    subgraph GD [Gold - views]
        G[current prices /
        cheapest per item /
        single-store verdict]
    end
    K --> R
    W --> R
    T --> R
    A --> R
    L --> R
    R --> N --> X --> E
    E -->|trusted links, conf >= 0.85| G
    G -.->|planned| F[FastAPI -> PWA]

    subgraph AI [AI surface - proposes, never writes to gold]
        Q[resolution_queue] --> C[verdict cache*] --> J[LLM adjudicator]
        J -->|below threshold| H[human review queue]
        P[parser self-healing agent*]
        NR[weekly narrator*]
    end
    E -->|ambiguous ~20%| Q
    J -->|verdict applied by the pipeline| E
    R -.->|schema drift| P
    P -.->|patch validated on bronze replay| N
    G -.->|reads gold only, cites row IDs| NR
```

\* planned — see the build order in `CLAUDE.md`.

## Quickstart

```powershell
uv venv
uv pip install -e ".[dev]"

# Build (or refresh) the DuckDB schema
uv run grocery-init-db

# Run tests
uv run pytest
```

Then copy `.env.example` to `.env` and fill in credentials as you build out the
fetchers.

## Layout

```
src/grocery_optimizer/
  config.py     paths + .env loading
  db.py         connection + schema init
  sql/          bronze / silver / gold DDL
  bronze/       fetchers (Kroger, Whole Foods, Trader Joe's, Aldi/KCL, Lidl)
  silver/       normalization, taxonomy, entity resolution, adjudicator, verdict
  gold/         query helpers
  scripts/      entry points
docs/           architecture diagram
```

See `CLAUDE.md` for the full project spec and build order.
