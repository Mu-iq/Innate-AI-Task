/**
 * Start a run, with adjustable settings.
 *
 * The two knobs — how many venues to process, and how many acceptances to stop
 * at — are the ones that decide how long a run takes and how much it costs.
 * Defaults and the maximums come from the backend (`/api/health`), so the inputs
 * can never offer a value the server would reject. The server clamps regardless;
 * this just keeps the UI honest about the ceiling.
 */

import { useState } from 'react';
import type { BackendState, Health, RunSettingsInput } from '../api/client';
import type { RunStatus } from '../types';

function StatusDot({ state }: { state: BackendState }) {
  if (state === 'live') {
    return (
      <span className="status status--ok" title="Connected — you can start new runs">
        <span className="status__dot" /> Live
      </span>
    );
  }
  if (state === 'connecting') {
    return (
      <span className="status status--wait" title="Connecting">
        <span className="status__dot" /> Connecting…
      </span>
    );
  }
  return (
    <span className="status status--idle" title="Showing the most recent saved results">
      <span className="status__dot" /> Saved results
    </span>
  );
}

function prettyStage(stage: string): string {
  if (!stage) return 'Working…';
  if (stage === 'setup') return 'Preparing…';
  if (stage === 'discover') return 'Finding venues…';
  if (stage === 'done') return 'Finished';
  if (stage === 'failed') return 'Stopped';
  if (stage.startsWith('processing ')) return `Processing ${stage.slice('processing '.length)}…`;
  return stage.charAt(0).toUpperCase() + stage.slice(1);
}

interface Props {
  backend: BackendState;
  health: Health | null;
  running: boolean;
  status: RunStatus | null;
  runError: string | null;
  onRun: (settings: RunSettingsInput) => void;
}

function NumberField({
  label,
  hint,
  value,
  min,
  max,
  disabled,
  onChange,
}: {
  label: string;
  hint: string;
  value: number;
  min: number;
  max: number;
  disabled: boolean;
  onChange: (n: number) => void;
}) {
  return (
    <label className="field">
      <span className="field__label">{label}</span>
      <input
        className="field__input"
        type="number"
        min={min}
        max={max}
        value={value}
        disabled={disabled}
        onChange={(e) => {
          const n = Number(e.target.value);
          if (Number.isFinite(n)) onChange(Math.max(min, Math.min(max, Math.round(n))));
        }}
      />
      <span className="field__hint">
        {hint} · max {max}
      </span>
    </label>
  );
}

export function RunControls({ backend, health, running, status, runError, onRun }: Props) {
  const bounds = health?.settings;
  const [maxVenues, setMaxVenues] = useState<number | null>(null);
  const [target, setTarget] = useState<number | null>(null);
  const [open, setOpen] = useState(false);

  // Resolve effective values: user choice, else the backend default.
  const mv = maxVenues ?? bounds?.max_venues.default ?? 8;
  const tg = target ?? bounds?.target_accepted.default ?? 3;

  return (
    <div className="runbar">
      <div className="run">
        <button
          className="btn"
          onClick={() => onRun({ max_venues: mv, target_accepted: tg })}
          disabled={running}
        >
          {running ? 'Running…' : 'Run pipeline'}
        </button>
        <StatusDot state={backend} />

        {bounds && (
          <button
            className="linkbtn"
            onClick={() => setOpen((v) => !v)}
            disabled={running}
            aria-expanded={open}
          >
            {open ? 'Hide settings' : 'Settings'}
          </button>
        )}

        {running && !status && <span className="run__status">Starting the pipeline…</span>}
        {status && (
          <span className="run__status">
            {prettyStage(status.stage)} · {status.accepted} accepted · {status.rejected} rejected
          </span>
        )}
        {runError && <span className="run__err">{runError}</span>}
      </div>

      {open && bounds && (
        <div className="settings">
          <NumberField
            label="Venues to process"
            hint="how many enter the paid stages"
            value={mv}
            min={bounds.max_venues.min}
            max={bounds.max_venues.max}
            disabled={running}
            onChange={setMaxVenues}
          />
          <NumberField
            label="Stop after accepting"
            hint="ends the run early once reached"
            value={Math.min(tg, mv)}
            min={bounds.target_accepted.min}
            max={Math.min(bounds.target_accepted.max, mv)}
            disabled={running}
            onChange={setTarget}
          />
          <p className="settings__note">
            More venues means a longer run and higher cost — the last step generates a real image
            per venue. The run stops as soon as it accepts enough.
          </p>
        </div>
      )}

      {!running && !status && !open && (
        <p className="run__hint">
          Finds venues, captures each frontage, composites the planters, and verifies the result.
          Takes a few minutes.
        </p>
      )}
    </div>
  );
}
