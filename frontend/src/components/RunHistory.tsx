/**
 * Previous runs — the first section under the hero.
 *
 * Rendered only when run history is available (see App), so it never has to
 * explain missing infrastructure to a viewer. Two states:
 *   no runs yet  → invite the first run rather than showing a blank box.
 *   N runs       → the list, newest first, click to inspect any of them.
 *
 * Each row counts decisions and errors apart: "3 accepted · 6 rejected · 2
 * errored" is the truth; "3 accepted · 8 rejected" is not. A run that errored on
 * every venue rejected nothing.
 */

import type { ReactNode } from 'react';
import type { RunSummary } from '../types';

function ago(iso: string): string {
  const s = Math.max(0, (Date.now() - new Date(iso).getTime()) / 1000);
  if (s < 60) return 'just now';
  if (s < 3600) return `${Math.floor(s / 60)}m ago`;
  if (s < 86400) return `${Math.floor(s / 3600)}h ago`;
  return `${Math.floor(s / 86400)}d ago`;
}

function duration(sec: number | null): string {
  if (sec == null) return '—';
  if (sec < 60) return `${sec}s`;
  return `${Math.floor(sec / 60)}m ${sec % 60}s`;
}

const STATUS_LABEL: Record<RunSummary['status'], string> = {
  queued: 'Queued',
  running: 'Running',
  succeeded: 'Done',
  failed: 'Failed',
};

interface Props {
  runs: RunSummary[];
  selected: string | null;
  onSelect: (runKey: string | null) => void;
  running: boolean;
}

function Shell({ children }: { children: ReactNode }) {
  return (
    <section className="history">
      <header className="history__head">
        <h2 className="history__title">Runs</h2>
        <p className="history__sub">
          Every pipeline run is kept. Pick one to inspect exactly what it decided, with the images
          it generated at the time.
        </p>
      </header>
      {children}
    </section>
  );
}

export function RunHistory({ runs, selected, onSelect, running }: Props) {
  if (runs.length === 0) {
    return (
      <Shell>
        <p className="history__empty">
          {running
            ? 'Your first run is in progress — it will appear here as soon as it finishes.'
            : 'No runs yet. Start one above and it will be saved here.'}
        </p>
      </Shell>
    );
  }

  const latest = runs[0]?.run_key;

  return (
    <Shell>
      <ul className="history__list">
        {runs.map((r) => {
          const isSelected =
            selected === r.run_key || (selected === null && r.run_key === latest);
          return (
            <li key={r.run_key}>
              <button
                className={isSelected ? 'runrow runrow--on' : 'runrow'}
                onClick={() => onSelect(r.run_key === latest ? null : r.run_key)}
                aria-current={isSelected}
              >
                <span className={`runrow__status runrow__status--${r.status}`}>
                  {STATUS_LABEL[r.status]}
                </span>

                <span className="runrow__key">
                  {r.run_key}
                  {r.run_key === latest && <em className="runrow__latest">latest</em>}
                  {r.dry_run && <em className="runrow__dry">dry run</em>}
                </span>

                <span className="runrow__stats">
                  <b>{r.accepted}</b> accepted
                  <span className="runrow__dot">·</span>
                  {r.rejected_decisions} rejected
                  {r.rejected_errors > 0 && (
                    <>
                      <span className="runrow__dot">·</span>
                      <span className="runrow__err">{r.rejected_errors} errored</span>
                    </>
                  )}
                </span>

                <span className="runrow__meta">
                  {r.total_cost_usd > 0 && (
                    <span className="runrow__cost">${r.total_cost_usd.toFixed(2)}</span>
                  )}
                  {r.discovered} found · {duration(r.duration_s)} · {ago(r.started_at)}
                </span>
              </button>
            </li>
          );
        })}
      </ul>
    </Shell>
  );
}
