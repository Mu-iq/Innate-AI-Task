# Storefront capture & visualisation

An automated prospecting engine for a London outdoor-planter supplier. It finds independent street-facing venues with bare frontages, photographs each one's **real** entrance, composites the client's **actual** planters onto it at the correct scale, and decides for itself whether the result is good enough to send to the owner.

**No venue in the output was chosen by a human.** Every accept and reject is a named constant in [`backend/app/config.py`](backend/app/config.py).

**Live demo:** https://innate-ai-task.vercel.app/

**Read this first** — what it is, how to run it, and the assumptions it makes. **Then
[design.md](design.md)** for the reasoning: automated venue selection, image sourcing, the
compositing method, fallback logic, imagery rights, and the rejection criteria.

---

## The pipeline

```
[1] discover  → Google Places (New)   → independent, street-facing candidates
[2] capture   → Street View metadata → bearing → static image
                fallback: Places Photos
[3] assess    → Gemini vision → usable frontage? bareness 0–10 + product choice
[4] measure   → Gemini vision → door height → px_per_metre → expected planter size
[5] composite → Nano Banana (frontage + 3 real product refs) → visual
[6] verify    → Gemini vision → before/after diff → accept, or retry once, then abandon
```

Each stage is a pure typed function returning **either** a result **or** a structured `Rejection` with reasons. Every run — its funnel, decisions, and images — is saved to Supabase and browsable in the UI as run history.

---

## Setup

### Prerequisites

- Python 3.11+
- Node 18+
- A Google Maps key, a Gemini key, and a Supabase project (all below)

### 1. Environment

```bash
cp .env.example .env
```

| Key | Where to get it | Notes |
|---|---|---|
| `GOOGLE_MAPS_API_KEY` | [Google Maps Platform](https://console.cloud.google.com/google/maps-apis) | Enable **Places API (New)** and **Street View Static API**, and turn on billing — without it every call returns `REQUEST_DENIED`. Use Places API **(New)**; the legacy endpoints aren't enabled on new keys. |
| `GEMINI_API_KEY` | [Google AI Studio](https://aistudio.google.com/apikey) | Covers both the vision and image models. The free tier is ~5 requests/min, which makes runs slow — set `GEMINI_MIN_INTERVAL_S=13` to pace them, or enable billing. |

### 2. Database

1. Create a project at [supabase.com](https://supabase.com).
2. SQL editor → run each file in [`supabase/migrations/`](supabase/migrations/) **in order**. This creates the tables, the storage bucket, and public read access.
3. Settings → API → copy into `.env`:

```
SUPABASE_URL=https://xxxx.supabase.co
SUPABASE_SERVICE_ROLE_KEY=...     # backend only — bypasses row-level security
SUPABASE_BUCKET=storefront-visuals
```

### 3. Backend

```bash
cd backend
pip install -r requirements.txt
python -m pytest tests/ -q      # geo maths + discovery filters
python run.py                   # http://localhost:8001/docs
```

It logs **`PERSISTENCE: ON`** at startup once the database is reachable.

> `.env` is read once at startup. Auto-reload restarts on code changes but **not** on `.env` changes — restart the server yourself after editing it.

### 4. Frontend

```bash
cd frontend
npm install
npm run dev                     # http://localhost:5173
```

It finds the backend on `localhost:8001` automatically. Only set `frontend/.env.local` if you moved the port.

---

## Running a pipeline

Press **Run pipeline** in the UI, or from the command line:

```bash
cd backend && python -m scripts.run_pipeline
```

Either way it saves to the database, uploads images to the bucket, and appears in the UI's **Runs** panel. Selecting a past run shows that run's venues, decision trail and images exactly as they were.

`python -m scripts.design_tables` prints the funnel/venue/rejection tables for design.md from the latest run.

### Run settings

The **Settings** control next to the Run button adjusts, per run:

| Setting | Default | Hard cap |
|---|---|---|
| Venues to process | 8 | 12 |
| Stop after accepting | 3 | 6 |

The server clamps every request to those caps regardless of what's asked for. Discovery still pulls ~285 candidates either way — these only limit what reaches the paid stages.

### Cost

Every run measures its own cost — billable calls counted (cache hits are free), Gemini tokens read from each response — and shows the breakdown in the UI. A typical run (8 venues, 3 accepted) is **≈ $0.84**; re-running the same venues costs ~$0.30 because Places and Street View come from cache.

| Setting | Does |
|---|---|
| `DRY_RUN=true` | Runs the whole funnel but never pays for image generation |
| `CACHE_ENABLED=false` | Forces regeneration — costs real money |
| `MAX_RUNS_PER_HOUR` | Caps how often the Run button can spend |

---

## Where things live

| | Location |
|---|---|
| Run + funnel + models + thresholds + cost | `runs` table |
| Venues, deduped by Google place_id | `venues` table |
| Per-run outcome + decision trail | `run_results` table |
| Frontage / composite / prompt | bucket: `runs/<run_key>/<place_id>/…` |
| Product plates | bucket: `products/plates/<slug>.png` |
| Gemini call traces, scratch images, API cache | `backend/outputs/`, `backend/.cache/` (gitignored) |

The database stores object **paths**; public URLs are derived at read time, so renaming the bucket needs no data migration.

---

## Deploying

Backend → **Cloud Run** ([`backend/Dockerfile`](backend/Dockerfile)), frontend → **Vercel** ([`vercel.json`](vercel.json)), data in **Supabase**.

The short version:

```bash
gcloud run deploy storefront-backend --source backend --region europe-west2 \
  --allow-unauthenticated --no-cpu-throttling --max-instances 1 \
  --set-env-vars "GOOGLE_MAPS_API_KEY=...,GEMINI_API_KEY=...,SUPABASE_URL=...,SUPABASE_SERVICE_ROLE_KEY=..."
```

Then import the repo on Vercel, set `VITE_API_BASE_URL` to the Cloud Run URL, and set
`CORS_EXTRA_ORIGINS` on the backend to the Vercel URL.

> **`--no-cpu-throttling` is not optional.** `/api/run` does its work in a background task after the
> response returns, and Cloud Run throttles CPU to ~0 once a response is sent. Without the flag a run
> starts, hands back a `run_id`, and then freezes for ever.

---

## Assumptions

1. **The planter dimensions are estimates.** The client supplied three reference photos but no spec
   sheet, so the heights in `PRODUCT_SPECS` (0.65–1.00 m vessels) are read off the photography
   against pavement slabs and doorways. They set the expected planter size in every composite, so
   they're the first thing to replace with real figures.
2. **A UK commercial doorway is 2.03 m** — a 1981 mm leaf plus frame. This is the scale anchor for
   the whole pipeline. Real shopfront doors vary (2.0–2.3 m), so it carries ~10% error, which is
   precisely why `SCALE_TOLERANCE` is 40% rather than 10% (design.md §6.2).
3. **Scale is measured on the vessel, never the planting.** The palm in `planter_2` is ~2 m and
   foliage is seasonal; the pot is a manufactured constant, so it's the only number the verifier can
   check against.
4. **Street View imagery is current enough.** It can be years old — a frontage assessed as bare may
   have been planted since the survey car passed.
5. **The three reference photos are the client's complete catalogue.**
6. **A venue with an owner is a venue worth pitching.** The chain blocklist assumes a branch manager
   can't say yes to a planter, so chains are filtered out regardless of how bare their frontage is.

---

## API

```
POST /api/run              → { run_id }   optional body: { max_venues, target_accepted }
GET  /api/status/{run_id}  → { stage, processed, accepted, rejected, done }
GET  /api/runs             → [ RunSummary ]            run history, newest first
GET  /api/results?run=key  → { venues, rejected, funnel, thresholds, cost }
GET  /api/health           → key presence (never values), persistence state, settings bounds
```

---

## Repo layout

```
├── design.md                      ← the reasoning, and the decisions to defend
├── README.md
├── .env.example
├── vercel.json                    ← frontend build config
├── supabase/migrations/           ← run these first: tables + storage bucket
├── backend/
│   ├── Dockerfile                 ← the Cloud Run image
│   ├── run.py                     ← local dev server on :8001
│   └── app/
│       ├── config.py              ← every threshold, model string, product spec
│       ├── schemas.py             ← pydantic models for every boundary
│       ├── main.py                ← FastAPI app, CORS, health
│       ├── routers/               ← /api/run, /api/status, /api/runs, /api/results
│       ├── services/
│       │   ├── pipeline.py        ← the orchestrator: the only file that knows all six stages
│       │   ├── discovery.py capture.py assess.py measure.py composite.py verify.py
│       │   ├── products.py        ← auto-crops the client's reference photos
│       │   ├── repository.py      ← all SQL
│       │   └── storage.py         ← bucket layout + uploads
│       ├── clients/               ← gemini.py, google_maps.py (retry, pacing, cache)
│       └── utils/                 ← geo.py (bearing), images.py, cache.py, metrics.py, logging.py
│   ├── scripts/                   ← run_pipeline.py, design_tables.py, probe_image_model.py
│   ├── tests/                     ← geo bearings + discovery filters: the two
│   │                                places a silent error looks like a decision
│   └── data/products/             ← the client's three real planters
└── frontend/src/
    ├── App.tsx
    ├── api/client.ts              ← reads runs/history from the API
    └── components/                ← VenueCard, BeforeAfter, DecisionTrail, RejectedList,
                                     RunHistory, RunControls, CostBar
```

---

Frontage imagery via Google Street View and Google Places. Composites are generated visualisations, not photographs of installed products. See design.md for the imagery-rights position.
