/**
 * Live progress while a run is in flight.
 *
 * A spinner tells you nothing. This shows the six stages, lights up the one
 * currently executing, and names the venue it is working on — so a watcher can
 * see that the pipeline is a pipeline, and roughly how long is left. It doubles
 * as an explanation of the system for anyone seeing it for the first time.
 */

import type { RunStatus } from '../types';

const STAGES = [
  { key: 'discover', label: 'Discover', hint: 'Google Places: find independent street-facing venues' },
  { key: 'capture', label: 'Capture', hint: "Street View at a heading computed from the panorama's own position" },
  { key: 'assess', label: 'Assess', hint: 'Is the entrance visible, the framing usable, the frontage bare?' },
  { key: 'measure', label: 'Measure', hint: 'Measure the doorway to convert pixels into real metres' },
  { key: 'composite', label: 'Composite', hint: "Place the client's real planters at the correct scale" },
  { key: 'verify', label: 'Verify', hint: 'Diff before/after — is it safe to send to the owner?' },
] as const;

/** setup and queued sit before the pipeline proper; treat them as stage 0. */
function activeIndex(stage: string): number {
  const i = STAGES.findIndex((s) => s.key === stage);
  if (i >= 0) return i;
  if (stage === 'done' || stage === 'failed') return STAGES.length;
  return -1; // queued / setup
}

export function RunProgress({ status }: { status: RunStatus | null }) {
  if (!status) {
    return (
      <div className="prog">
        <p className="prog__now">Starting…</p>
      </div>
    );
  }

  const active = activeIndex(status.stage);
  const preamble = active === -1;

  return (
    <div className="prog">
      <div className="prog__head">
        <p className="prog__now">
          {preamble ? (
            'Preparing — resolving models and product references…'
          ) : status.venue ? (
            <>
              <strong>{status.venue}</strong>
              {status.venue_total > 0 && (
                <span className="prog__count">
                  {' '}
                  · venue {status.venue_index} of {status.venue_total}
                </span>
              )}
            </>
          ) : (
            'Finding venues across five London areas…'
          )}
        </p>
        <p className="prog__tally">
          <b>{status.accepted}</b> accepted · {status.rejected} rejected
        </p>
      </div>

      <ol className="prog__stages">
        {STAGES.map((s, i) => {
          const state = i < active ? 'done' : i === active ? 'on' : 'todo';
          return (
            <li key={s.key} className={`prog__stage prog__stage--${state}`} title={s.hint}>
              <span className="prog__dot" />
              <span className="prog__label">{s.label}</span>
            </li>
          );
        })}
      </ol>

      {status.error && <p className="prog__err">{status.error}</p>}
    </div>
  );
}
