-- ===========================================================================
-- Per-run duplicate policy.
--
-- Run after the first three migrations. Safe to re-run.
--
-- allow_duplicates records whether a run was permitted to re-process venues that
-- an earlier run already accepted. Stored per run, not in config, because it is
-- a property of that run's intent: "show me the same venues again" and "show me
-- something new" are both legitimate, and a reader of the history needs to know
-- which one they are looking at.
--
-- Default true = the old behaviour, so existing rows stay accurate.
-- ===========================================================================

alter table public.runs
    add column if not exists allow_duplicates boolean not null default true;

comment on column public.runs.allow_duplicates is
    'False = this run skipped venues already accepted by an earlier run.';

-- Rebuilt to expose the new column. Dropped rather than replaced: CREATE OR
-- REPLACE can only append columns, and this one sits mid-list.
drop view if exists public.run_summaries;

create view public.run_summaries as
select
    r.run_key,
    r.status,
    r.stage,
    r.started_at,
    r.finished_at,
    r.dry_run,
    r.vision_model,
    r.image_model,
    r.max_venues,
    r.target_accepted,
    r.allow_duplicates,
    r.total_cost_usd,
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

-- Deleting a run must take its results with it. run_results.run_id already
-- cascades (see the init migration), so `delete from runs where run_key = ...`
-- is enough. venues are deliberately NOT touched: a venue is a fact about
-- London, not output of the run that happened to look at it, and run_results
-- references it with `on delete restrict` precisely so a delete cannot quietly
-- take one with it.

