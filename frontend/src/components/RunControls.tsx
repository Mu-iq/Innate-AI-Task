/**
 * Start a run, with adjustable settings and live progress.
 *
 * The settings are the ones that decide how long a run takes and what it costs.
 * Defaults and maximums come from the backend (`/api/health`), so the inputs can
 * never offer a value the server would reject — and the server clamps anyway.
 */

import { useState } from 'react';
import type { BackendState, Health, RunSettingsInput } from '../api/client';
import type { RunStatus } from '../types';
import { Info } from './Info';
import { RunProgress } from './RunProgress';

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
  tip,
  value,
  min,
  max,
  disabled,
  onChange,
}: {
  label: string;
  hint: string;
  tip: string;
  value: number;
  min: number;
  max: number;
  disabled: boolean;
  onChange: (n: number) => void;
}) {
  return (
    <label className="field">
      <span className="field__label">
        {label} <Info text={tip} />
      </span>
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
  const [allowDuplicates, setAllowDuplicates] = useState(true);
  const [open, setOpen] = useState(false);

  const mv = maxVenues ?? bounds?.max_venues.default ?? 8;
  const tg = target ?? bounds?.target_accepted.default ?? 3;

  return (
    <div className="runbar">
      <div className="run">
        <button
          className="btn"
          onClick={() =>
            onRun({ max_venues: mv, target_accepted: tg, allow_duplicates: allowDuplicates })
          }
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

        {runError && <span className="run__err">{runError}</span>}
      </div>

      {running && <RunProgress status={status} />}

      {open && bounds && !running && (
        <div className="settings">
          <NumberField
            label="Venues to process"
            hint="enter the paid stages"
            tip="How many of the ~285 discovered venues get photographed, assessed and composited. Discovery is cheap and always runs in full; this caps only the expensive part."
            value={mv}
            min={bounds.max_venues.min}
            max={bounds.max_venues.max}
            disabled={running}
            onChange={setMaxVenues}
          />
          <NumberField
            label="Stop after accepting"
            hint="ends the run early"
            tip="The run stops as soon as this many venues pass verification, so a good run costs less than a bad one."
            value={Math.min(tg, mv)}
            min={bounds.target_accepted.min}
            max={Math.min(bounds.target_accepted.max, mv)}
            disabled={running}
            onChange={setTarget}
          />

          <label className="toggle">
            <input
              type="checkbox"
              checked={allowDuplicates}
              disabled={running}
              onChange={(e) => setAllowDuplicates(e.target.checked)}
            />
            <span className="toggle__body">
              <span className="toggle__label">
                Allow repeat venues <Info text="When off, the run skips venues an earlier run already accepted, so it spends its budget on new frontages instead of regenerating a visual you already have. Previously-rejected venues are still retried — a parked van may have moved." />
              </span>
              <span className="field__hint">
                {allowDuplicates ? 'may re-process past venues' : 'only venues not yet accepted'}
              </span>
            </span>
          </label>

          <p className="settings__note">
            More venues means a longer run and a higher bill — the last step generates a real image
            per venue.
          </p>
        </div>
      )}

      {!running && !open && (
        <p className="run__hint">
          Finds venues, captures each frontage, composites the planters, and verifies the result.
          Takes a few minutes.
        </p>
      )}
    </div>
  );
}
