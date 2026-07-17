/**
 * The page.
 *
 * Two-phase load (see api/client.ts): the most recent saved results render
 * instantly, then a live connection upgrades them in the background if one is
 * available. The UI never surfaces which of those happened, or any of the
 * infrastructure behind it — a viewer sees results and, when a connection is
 * available, the ability to start a new run.
 */

import { useCallback, useEffect, useRef, useState } from 'react';
import {
  deleteRun,
  getHealth,
  getStatus,
  hasBackend,
  listRuns,
  loadResults,
  startRun,
  type BackendState,
  type Health,
  type RunSettingsInput,
} from './api/client';
import { CostBar } from './components/CostBar';
import { RejectedList } from './components/RejectedList';
import { RunControls } from './components/RunControls';
import { RunHistory } from './components/RunHistory';
import { VenueCard } from './components/VenueCard';
import type { ResultsPayload, RunStatus, RunSummary } from './types';

const POLL_MS = 2000;

/**
 * Consecutive failed polls before we call a run lost.
 *
 * A run makes dozens of poll requests over several minutes; one dropped packet,
 * or a backend restart, must not be reported as a dead run. Five misses is ~10s
 * of genuine silence.
 */
const POLL_MAX_MISSES = 5;

/**
 * How often to re-attempt the live backend while it is not connected.
 *
 * The backend is frequently not up at the moment the page loads: uvicorn is
 * restarting after a code change in dev, or a free-tier instance is cold-starting
 * in production. A single attempt at mount would strand the page on the snapshot
 * until someone thought to refresh — so we keep trying quietly until it answers,
 * and the page upgrades itself the moment it does.
 */
const RECONNECT_MS = 10_000;

/**
 * The funnel, left to right.
 *
 * Each bar is scaled within its own PHASE, not against the single largest number.
 * That is deliberate: discovery works in hundreds and the paid pipeline is capped
 * at single digits, so one shared scale renders 8, 3 and 2 as three identical
 * slivers — hiding the only part a reader actually cares about. Scaling per phase
 * means each bar answers "what fraction of this phase's intake survived", which is
 * the honest question for both halves.
 *
 * The step between the phases is a deliberate cap (MAX_VENUES), not a rejection,
 * so it starts the second phase at full width rather than reading as a 97% cull.
 */
function FunnelBar({ data }: { data: ResultsPayload }) {
  const f = data.funnel;

  const discovery = [
    { label: 'Discovered', n: f.discovered },
    { label: 'Passed filters', n: f.after_status_filter },
  ];
  const pipeline = [
    { label: 'Entered pipeline', n: f.entered_pipeline },
    { label: 'Photographed', n: f.capture_ok },
    { label: 'Assessed usable', n: f.assess_ok },
    { label: 'Composited', n: f.composite_ok },
    { label: 'Accepted', n: f.accepted },
  ];

  // Guard both divides: a run that found nothing must not produce NaN widths.
  const discoveryPeak = Math.max(f.discovered, 1);
  const pipelinePeak = Math.max(f.entered_pipeline, 1);

  const steps = [
    ...discovery.map((s) => ({ ...s, pct: (s.n / discoveryPeak) * 100 })),
    ...pipeline.map((s) => ({ ...s, pct: (s.n / pipelinePeak) * 100 })),
  ];

  return (
    <ol className="funnel">
      {steps.map((s, i) => (
        <li
          key={s.label}
          className={i === steps.length - 1 ? 'funnel__step funnel__step--end' : 'funnel__step'}
        >
          <span className="funnel__n">{s.n}</span>
          <span className="funnel__label">{s.label}</span>
          {/* Floor of 3% so a surviving step is never invisible. */}
          <span
            className="funnel__bar"
            style={{ width: `${s.n === 0 ? 0 : Math.max(3, s.pct)}%` }}
          />
        </li>
      ))}
    </ol>
  );
}

export default function App() {
  const [data, setData] = useState<ResultsPayload | null>(null);
  const [backend, setBackend] = useState<BackendState>(hasBackend() ? 'connecting' : 'none');
  const [health, setHealth] = useState<Health | null>(null);
  const [running, setRunning] = useState(false);
  const [runError, setRunError] = useState<string | null>(null);
  const [status, setStatus] = useState<RunStatus | null>(null);

  // Run history, and which run the page is currently showing. null = latest.
  const [runs, setRuns] = useState<RunSummary[]>([]);
  const [selectedRun, setSelectedRun] = useState<string | null>(null);
  const timer = useRef<number | null>(null);

  const refreshRuns = useCallback(async () => {
    setRuns(await listRuns());
  }, []);

  /** Load a run (latest, or a selected one) plus history and health. */
  const load = useCallback(
    async (runKey?: string | null): Promise<boolean> => {
      if (!hasBackend()) {
        setBackend('none');
        return false;
      }
      try {
        const payload = await loadResults(runKey);
        setData(payload);
        setBackend('live');
        getHealth().then(setHealth);
        void refreshRuns();
        return true;
      } catch {
        setBackend('unreachable');
        return false;
      }
    },
    [refreshRuns],
  );

  // First load. Retries below handle a backend that is still starting.
  useEffect(() => {
    void load(null);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [load]);

  // Keep reaching for the backend until it answers, so a page opened while the
  // server is still starting recovers on its own instead of looking broken.
  useEffect(() => {
    if (backend === 'live' || backend === 'none') return;
    const id = window.setInterval(() => void load(selectedRun), RECONNECT_MS);
    return () => window.clearInterval(id);
  }, [backend, load, selectedRun]);

  useEffect(
    () => () => {
      if (timer.current) window.clearInterval(timer.current);
    },
    [],
  );

  // Switch which run is displayed. Pulls that run fresh so its stats and images
  // match exactly.
  const onSelectRun = useCallback(
    (runKey: string | null) => {
      setSelectedRun(runKey);
      void load(runKey);
    },
    [load],
  );

  const poll = useCallback(
    (runId: string) => {
      if (timer.current) window.clearInterval(timer.current);

      // A run takes minutes and makes dozens of poll requests. Treating the
      // first failed one as fatal declares the run dead over a single dropped
      // packet — which it did, on runs that then completed perfectly. Only give
      // up after several consecutive failures.
      let misses = 0;
      let lastProcessed = -1;

      timer.current = window.setInterval(async () => {
        try {
          const s = await getStatus(runId);
          misses = 0;
          setStatus(s);
          void refreshRuns();

          // Pull the run's own data whenever a venue finishes, so the funnel and
          // the cards below track the run in progress instead of sitting on the
          // previous run's results until the end.
          if (s.processed !== lastProcessed) {
            lastProcessed = s.processed;
            void load(null);
          }

          if (s.done) {
            if (timer.current) window.clearInterval(timer.current);
            setRunning(false);
            setSelectedRun(null);
            await load(null);
          }
        } catch {
          misses += 1;
          if (misses >= POLL_MAX_MISSES) {
            if (timer.current) window.clearInterval(timer.current);
            setRunning(false);
            setRunError(
              'Lost contact with the run. It may still be going — check the run history below.',
            );
            void load(null);
          }
        }
      }, POLL_MS);
    },
    [load, refreshRuns],
  );

  /** Delete a run, then reload. If it was the one on screen, fall back to latest. */
  const onDeleteRun = useCallback(
    async (runKey: string) => {
      try {
        await deleteRun(runKey);
      } catch (e) {
        setRunError((e as Error).message);
        return;
      }
      const next = selectedRun === runKey ? null : selectedRun;
      setSelectedRun(next);
      await load(next);
    },
    [selectedRun, load],
  );

  const onRun = useCallback(
    async (settings: RunSettingsInput) => {
      setRunning(true);
      setRunError(null);
      setStatus(null);
      try {
        const { run_id, adopted } = await startRun(settings);
        if (adopted) setRunError('A run is already in progress — showing its results.');
        // Switch to the new run at once. Without this the page keeps showing the
        // previous run's funnel and venues while the new one works, which reads
        // as results that belong to the run you just started.
        setSelectedRun(null);
        await load(null);
        poll(run_id);
      } catch (e) {
        setRunning(false);
        setRunError((e as Error).message);
      }
    },
    [poll, load],
  );

  // Still connecting, nothing to show yet.
  if (!data) {
    return (
      <main className="wrap">
        <header className="hero">
          <p className="hero__eyebrow">Prospecting engine</p>
          <h1 className="hero__title">Storefront capture &amp; visualisation</h1>
        </header>
        <div className="placeholder">
          <h2>{backend === 'unreachable' ? 'Connecting…' : 'Loading…'}</h2>
          <p>
            {backend === 'unreachable'
              ? 'Reaching the service. This will continue automatically.'
              : 'Fetching the latest results.'}
          </p>
        </div>
      </main>
    );
  }

  const runControls = hasBackend() && (
    <RunControls
      backend={backend}
      health={health}
      running={running}
      status={status}
      runError={runError}
      onRun={onRun}
    />
  );

  const notRunYet = !data.run_id && data.funnel.discovered === 0;
  if (notRunYet) {
    return (
      <main className="wrap">
        <header className="hero">
          <p className="hero__eyebrow">Prospecting engine</p>
          <h1 className="hero__title">Storefront capture &amp; visualisation</h1>
          <p className="hero__sub">
            Finds independent London venues with bare frontages, photographs each real entrance,
            composites the client's planters onto it, and decides for itself whether the result is
            good enough to send to the owner.
          </p>
        </header>
        <div className="placeholder">
          <h2>No results to show yet</h2>
          <p>Start a run to find venues and generate the first set of visuals.</p>
          {runControls}
        </div>
      </main>
    );
  }

  const generated = new Date(data.generated_at).toLocaleDateString('en-GB', {
    day: 'numeric',
    month: 'short',
    year: 'numeric',
  });

  // Decisions and errors are counted apart everywhere. Rolling them together
  // would report judgements the pipeline never made.
  const errorCount = data.rejected.filter((r) => r.kind === 'error').length;
  const decisionCount = data.rejected.length - errorCount;

  const latestKey = runs[0]?.run_key;
  const viewingOlder = selectedRun !== null && selectedRun !== latestKey;

  return (
    <main className="wrap">
      <header className="hero">
        <p className="hero__eyebrow">Prospecting engine</p>
        <h1 className="hero__title">Storefront capture &amp; visualisation</h1>
        <p className="hero__sub">
          Finds independent London venues with bare frontages, photographs each one's real entrance,
          composites the client's actual planters onto it at the correct scale, and decides for
          itself whether the result is good enough to send to the owner. No venue on this page was
          chosen by a human.
        </p>

        {runControls}
      </header>

      {/* Previous runs — a first-class section. Shown only when run history is
          actually available, so an unconfigured deployment simply doesn't have
          the panel rather than displaying setup instructions to a viewer. */}
      {hasBackend() && health?.persistence === 'on' && (
        <RunHistory
          runs={runs}
          selected={selectedRun}
          onSelect={onSelectRun}
          onDelete={onDeleteRun}
          running={running}
        />
      )}

      <section className="section">
        <div className="viewhead">
          <h2 className="section__title">
            {viewingOlder ? 'Viewing run' : 'Latest run'}
            {data.run_id && <code className="viewhead__key">{data.run_id}</code>}
          </h2>
          {viewingOlder && (
            <button className="linkbtn" onClick={() => onSelectRun(null)}>
              ← Back to latest
            </button>
          )}
        </div>

        <p className="hero__meta viewhead__meta">
          {generated}
          {data.settings && ` · ${data.settings.max_venues} venues processed`}
          {data.dry_run && ' · preview run (no images generated)'}
        </p>

        <FunnelBar data={data} />
        <CostBar cost={data.cost} />
      </section>

      <section className="section">
        <h2 className="section__title">
          Accepted <span className="section__count">{data.venues.length}</span>
        </h2>
        <p className="section__sub">
          Drag each slider to compare. The decision trail under every card shows the scores and
          checks that let it through.
        </p>

        {data.venues.length === 0 ? (
          <p className="empty">
            {errorCount > 0 ? (
              <>
                No venue reached a verdict in this run — the pipeline hit{' '}
                <strong>{errorCount} error{errorCount === 1 ? '' : 's'}</strong> before it could
                judge them. That is the pipeline breaking, not deciding. See “Not decided” below.
              </>
            ) : (
              <>
                No venue cleared verification in this run. Every rejection below is a decision the
                pipeline made — which is it working, not failing.
              </>
            )}
          </p>
        ) : (
          <div className="grid">
            {data.venues.map((v) => (
              <VenueCard key={v.id} venue={v} thresholds={data.thresholds} />
            ))}
          </div>
        )}
      </section>

      <section className="section section--rejects">
        <h2 className="section__title">
          Rejected <span className="section__count">{decisionCount}</span>
          {errorCount > 0 && (
            <span className="section__count section__count--err" title="Errors, not decisions">
              +{errorCount} not decided
            </span>
          )}
        </h2>
        <p className="section__sub">
          Every candidate the pipeline discarded, with the reason it was discarded. This is the
          evidence that selection was automated: a hand-curated demo has nothing to put here.
          {errorCount > 0 &&
            ' Errors are listed separately — a venue the pipeline never judged is not a venue it rejected.'}
        </p>
        <RejectedList rejected={data.rejected} />
      </section>

      <footer className="foot">
        <p>
          Frontage imagery via Google Street View and Google Places. Composites are generated
          visualisations, not photographs of installed products. See <code>design.md</code> for the
          imagery-rights position.
        </p>
      </footer>
    </main>
  );
}
