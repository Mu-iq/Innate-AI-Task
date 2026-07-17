-- ===========================================================================
-- Storefront capture & visualisation — core schema
--
-- Run:  supabase db push
--   or: paste into the Supabase SQL editor and execute.
--
-- Shape:
--   runs         one row per pipeline execution   (the funnel, the models, the config)
--   venues       one row per real-world venue     (deduped by Google place_id, outlives runs)
--   run_results  one row per (run, venue) outcome (accepted or rejected, with the trail)
--
-- venues is separate from run_results on purpose. A venue is a fact about
-- London; a result is a judgement made about it at a point in time, by a
-- specific model, against specific thresholds. Re-running the pipeline must add
-- history, not overwrite it — you want to be able to ask "did this venue's
-- bareness score change after the 2027 Street View refresh?" and get an answer.
-- ===========================================================================

create extension if not exists "pgcrypto";  -- gen_random_uuid()

-- ---------------------------------------------------------------------------
-- runs
-- ---------------------------------------------------------------------------

create table if not exists public.runs (
    id              uuid primary key default gen_random_uuid(),

    -- Human-sortable id minted by the pipeline, e.g. "20260716-013126-825e9e".
    -- Kept as the public handle so URLs and logs stay readable.
    run_key         text        not null unique,

    status          text        not null default 'queued'
                    check (status in ('queued', 'running', 'succeeded', 'failed')),
    stage           text        not null default 'queued',
    error           text,

    -- What was actually used. Recorded per run because both are probe-resolved
    -- at runtime and can change underneath us — gemini-2.5-flash was retired
    -- mid-build while still being advertised by models.list().
    vision_model    text,
    image_model     text,
    dry_run         boolean     not null default false,

    -- Config snapshot. jsonb because the set of knobs genuinely evolves, and a
    -- historical run must keep the thresholds it was actually judged against.
    thresholds      jsonb       not null default '{}'::jsonb,
    max_venues      integer,
    target_accepted integer,

    -- The funnel. Explicit columns, not jsonb: the shape is fixed, design.md
    -- quotes these numbers, and they are the thing you most want to aggregate
    -- across runs.
    funnel_discovered           integer not null default 0,
    funnel_after_chain_filter   integer not null default 0,
    funnel_after_status_filter  integer not null default 0,
    funnel_entered_pipeline     integer not null default 0,
    funnel_capture_ok           integer not null default 0,
    funnel_assess_ok            integer not null default 0,
    funnel_measure_ok           integer not null default 0,
    funnel_composite_ok         integer not null default 0,
    funnel_accepted             integer not null default 0,

    -- Live counters, updated as the run progresses so the UI can poll.
    processed       integer     not null default 0,
    accepted        integer     not null default 0,
    rejected        integer     not null default 0,

    started_at      timestamptz not null default now(),
    finished_at     timestamptz,
    created_at      timestamptz not null default now(),
    updated_at      timestamptz not null default now(),

    -- A finished run must say when it finished; an unfinished one must not.
    constraint runs_finished_at_consistent check (
        (status in ('succeeded', 'failed')) = (finished_at is not null)
    )
);

comment on table  public.runs is 'One pipeline execution: its funnel, models, thresholds and outcome.';
comment on column public.runs.thresholds is 'Snapshot of config.py accept/reject constants this run was judged against.';

-- History is browsed newest-first; that is the only hot query on this table.
create index if not exists runs_started_at_desc_idx on public.runs (started_at desc);
create index if not exists runs_status_idx          on public.runs (status);

-- ---------------------------------------------------------------------------
-- venues
-- ---------------------------------------------------------------------------

create table if not exists public.venues (
    id                 uuid        primary key default gen_random_uuid(),

    -- Google Places id. The natural key: stable, and what dedupes across the
    -- 15 discovery queries that surface the same venue repeatedly.
    place_id           text        not null unique,

    name               text        not null,
    address            text        not null default '',
    postcode           text        not null default '',
    lat                double precision not null,
    lng                double precision not null,
    area               text        not null default '',

    primary_type       text        not null default '',
    types              text[]      not null default '{}',
    business_status    text        not null default '',
    rating             numeric(2, 1),
    user_ratings_total integer,

    first_seen_at      timestamptz not null default now(),
    last_seen_at       timestamptz not null default now(),

    constraint venues_lat_valid check (lat between -90 and 90),
    constraint venues_lng_valid check (lng between -180 and 180)
);

comment on table public.venues is 'Real-world venues, deduped by Google place_id. Outlives any single run.';

create index if not exists venues_area_idx     on public.venues (area);
create index if not exists venues_postcode_idx on public.venues (postcode);

-- ---------------------------------------------------------------------------
-- run_results
-- ---------------------------------------------------------------------------

create table if not exists public.run_results (
    id             uuid        primary key default gen_random_uuid(),
    run_id         uuid        not null references public.runs(id)   on delete cascade,
    venue_id       uuid        not null references public.venues(id) on delete restrict,

    outcome        text        not null check (outcome in ('accepted', 'rejected')),

    -- The stage that produced this outcome: where it was accepted, or where it died.
    stage          text        not null
                   check (stage in ('discover','capture','assess','measure','composite','verify')),

    -- Only meaningful for rejections.
    --   'decision' — the pipeline looked and said no. The deliverable.
    --   'error'    — the pipeline never got to decide (quota, retired model, network).
    -- Kept apart so a 429 is never counted as a judgement the system never made.
    kind           text        check (kind in ('decision', 'error')),

    reasons        text[]      not null default '{}',
    detail         text        not null default '',

    -- Capture provenance. Which source, and how the camera was aimed.
    image_source   text        check (image_source in ('streetview', 'places_photo')),
    heading_used   numeric(6, 2),
    pano_distance_m numeric(6, 1),

    product_slug   text,

    -- The decision trail. jsonb: these are model outputs whose shape is owned by
    -- the pydantic schemas, and pinning them into columns would mean a migration
    -- every time a prompt gains a field.
    assessment     jsonb,
    measurement    jsonb,
    verification   jsonb,

    -- Object paths within the storage bucket (not URLs — the bucket may be
    -- renamed or fronted by a CDN, and a stored URL would rot).
    frontage_path  text,
    composite_path text,

    attempts       integer     not null default 1 check (attempts >= 1),
    created_at     timestamptz not null default now(),

    -- One outcome per venue per run.
    constraint run_results_unique_per_run unique (run_id, venue_id),

    -- Invariants enforced here rather than trusted to application code:

    -- An accepted venue must have both images, or the UI has nothing to show.
    constraint run_results_accepted_has_images check (
        outcome <> 'accepted' or (frontage_path is not null and composite_path is not null)
    ),
    -- An accepted venue must carry the trail that accepted it.
    constraint run_results_accepted_has_trail check (
        outcome <> 'accepted'
        or (assessment is not null and measurement is not null and verification is not null)
    ),
    -- The rejection log IS a deliverable. A rejection without a reason is a bug.
    constraint run_results_rejected_has_reason check (
        outcome <> 'rejected' or coalesce(array_length(reasons, 1), 0) >= 1
    ),
    -- Every rejection is either a decision or an error. Never neither.
    constraint run_results_rejected_has_kind check (
        outcome <> 'rejected' or kind is not null
    )
);

comment on table  public.run_results is 'Per-run, per-venue outcome with the full decision trail.';
comment on column public.run_results.kind is 'Rejections only: decision = the pipeline judged; error = it never got to.';
comment on column public.run_results.frontage_path is 'Object path inside the storage bucket, not a URL.';

create index if not exists run_results_run_id_idx        on public.run_results (run_id);
create index if not exists run_results_venue_id_idx      on public.run_results (venue_id);
create index if not exists run_results_run_outcome_idx   on public.run_results (run_id, outcome);
-- Partial index: the accepted set is small and read on every page load.
create index if not exists run_results_accepted_idx      on public.run_results (run_id)
    where outcome = 'accepted';

-- ---------------------------------------------------------------------------
-- updated_at maintenance
-- ---------------------------------------------------------------------------

create or replace function public.set_updated_at()
returns trigger
language plpgsql
as $$
begin
    new.updated_at = now();
    return new;
end;
$$;

drop trigger if exists runs_set_updated_at on public.runs;
create trigger runs_set_updated_at
    before update on public.runs
    for each row execute function public.set_updated_at();

-- ---------------------------------------------------------------------------
-- run_summaries — what the history list reads
--
-- A view rather than a client-side join: the frontend should not need to know
-- how the funnel is stored to render "285 → 3 accepted".
-- ---------------------------------------------------------------------------

create or replace view public.run_summaries as
select
    r.run_key,
    r.status,
    r.stage,
    r.started_at,
    r.finished_at,
    r.dry_run,
    r.vision_model,
    r.image_model,
    r.funnel_discovered,
    r.funnel_entered_pipeline,
    r.funnel_accepted,
    r.accepted,
    r.rejected,
    r.error,
    extract(epoch from (coalesce(r.finished_at, now()) - r.started_at))::int as duration_s,
    count(rr.id) filter (where rr.outcome = 'rejected' and rr.kind = 'decision') as rejected_decisions,
    count(rr.id) filter (where rr.outcome = 'rejected' and rr.kind = 'error')    as rejected_errors
from public.runs r
left join public.run_results rr on rr.run_id = r.id
group by r.id
order by r.started_at desc;

comment on view public.run_summaries is 'Newest-first run history for the UI, with decisions and errors counted apart.';

-- ---------------------------------------------------------------------------
-- Row Level Security
--
-- There is no auth in this prototype and the data is not sensitive — it is
-- public venues and generated marketing visuals. So: the anon key may READ
-- everything and WRITE nothing. Only the backend, holding the service_role key
-- (which bypasses RLS), may write.
--
-- That split is what makes it safe to ship the anon key to the browser.
-- ---------------------------------------------------------------------------

alter table public.runs        enable row level security;
alter table public.venues      enable row level security;
alter table public.run_results enable row level security;

drop policy if exists "runs are publicly readable" on public.runs;
create policy "runs are publicly readable"
    on public.runs for select to anon, authenticated using (true);

drop policy if exists "venues are publicly readable" on public.venues;
create policy "venues are publicly readable"
    on public.venues for select to anon, authenticated using (true);

drop policy if exists "run_results are publicly readable" on public.run_results;
create policy "run_results are publicly readable"
    on public.run_results for select to anon, authenticated using (true);

-- No insert/update/delete policies exist by design. Without a permissive policy,
-- RLS denies those to anon and authenticated. service_role bypasses RLS entirely.
