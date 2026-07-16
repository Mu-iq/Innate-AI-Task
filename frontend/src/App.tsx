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
 * How often to re-attempt the live backend while it is not connected.
 *
 * The backend is frequently not up at the moment the page loads: uvicorn is
 * restarting after a code change in dev, or a free-tier instance is cold-starting
 * in production. A single attempt at mount would strand the page on the snapshot
 * until someone thought to refresh — so we keep trying quietly until it answers,
 * and the page upgrades itself the moment it does.
 */
const RECONNECT_MS = 10_000;

function FunnelBar({ data }: { data: ResultsPayload }) {
  const f = data.funnel;
  const steps = [
    { label: 'Discovered', n: f.discovered },
    { label: 'Passed filters', n: f.after_status_filter },
    { label: 'Entered pipeline', n: f.entered_pipeline },
    { label: 'Photographed', n: f.capture_ok },
    { label: 'Assessed usable', n: f.assess_ok },
    { label: 'Composited', n: f.composite_ok },
    { label: 'Accepted', n: f.accepted },
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
      timer.current = window.setInterval(async () => {
        try {
          const s = await getStatus(runId);
          setStatus(s);
          // Refresh history mid-run so its live counters tick, not just at the end.
          void refreshRuns();
          if (s.done) {
            if (timer.current) window.clearInterval(timer.current);
            setRunning(false);
            // A finished run becomes the latest. Snap to it and pull fresh stats.
            setSelectedRun(null);
            await load(null);
          }
        } catch {
          if (timer.current) window.clearInterval(timer.current);
          setRunning(false);
          setRunError('Lost the connection while the run was in progress. Please try again.');
        }
      }, POLL_MS);
    },
    [load, refreshRuns],
  );

  const onRun = useCallback(
    async (settings: RunSettingsInput) => {
      setRunning(true);
      setRunError(null);
      setStatus(null);
      try {
        const { run_id, adopted } = await startRun(settings);
        if (adopted) setRunError('A run is already in progress — showing its results.');
        poll(run_id);
      } catch (e) {
        setRunning(false);
        setRunError((e as Error).message);
      }
    },
    [poll],
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
        <RunHistory runs={runs} selected={selectedRun} onSelect={onSelectRun} running={running} />
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
