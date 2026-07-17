-- ===========================================================================
-- Per-run cost + the settings the run was launched with.
--
-- Run after the first two migrations. Safe to re-run (idempotent).
--
-- total_cost_usd is a real column (not buried in jsonb) so it can be summed and
-- sorted cheaply — "spend this week" is one query. The full breakdown and the
-- billable call counts live in `metrics` jsonb alongside it.
-- ===========================================================================

alter table public.runs
    add column if not exists total_cost_usd numeric(10, 4) not null default 0,
    add column if not exists metrics        jsonb        not null default '{}'::jsonb;

comment on column public.runs.total_cost_usd is 'Estimated USD cost of this run, from counted calls x config prices.';
comment on column public.runs.metrics is 'Billable call counts, token usage, and the per-line cost breakdown.';

-- Surface cost in the history view the UI reads.
--
-- Dropped and recreated rather than CREATE OR REPLACE: replace can only append
-- columns, and these new ones sit in the middle, which Postgres reads as
-- renaming the existing ones ("cannot change name of view column").
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
