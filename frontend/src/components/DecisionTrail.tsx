/**
 * The decision trail: every score and check that let this venue through.
 *
 * This is the evidence that the pipeline decided rather than a human. It shows
 * the numbers, the thresholds they were compared against, and the checks the
 * verifier ran — including on accepted venues, where nothing went wrong.
 *
 * Thresholds come from results.json, not from constants here, so this panel
 * always describes the bar the run actually applied.
 */

import type { Thresholds, VenueResult } from '../types';

function Check({ ok, label }: { ok: boolean; label: string }) {
  return (
    <li className={ok ? 'check check--ok' : 'check check--bad'}>
      <span className="check__mark" aria-hidden="true">
        {ok ? '✓' : '✕'}
      </span>
      <span>{label}</span>
    </li>
  );
}

interface Props {
  venue: VenueResult;
  thresholds: Thresholds | null;
}

export function DecisionTrail({ venue, thresholds }: Props) {
  const { assessment: a, measurement: m, verification: v } = venue;

  const bareBar = thresholds?.frontage_bare_threshold ?? 6;
  const doorM = thresholds?.standard_door_height_m ?? 2.03;
  const tolPct = ((thresholds?.scale_tolerance ?? 0.4) * 100).toFixed(0);
  const maxAttempts = thresholds?.max_composite_attempts ?? 2;

  return (
    <div className="trail">
      <section className="trail__stage">
        <h4 className="trail__title">
          <span className="trail__num">3</span> Assessed
        </h4>
        <div className="trail__metric">
          <div className="meter">
            <div className="meter__fill" style={{ width: `${a.frontage_bare_score * 10}%` }} />
            <div
              className="meter__threshold"
              style={{ left: `${bareBar * 10}%` }}
              title={`Threshold: ${bareBar}/10`}
            />
          </div>
          <p className="trail__metric-label">
            Bareness <strong>{a.frontage_bare_score}/10</strong>{' '}
            <span className="muted">(needs ≥ {bareBar})</span>
          </p>
        </div>
        <ul className="checks">
          <Check ok={a.entrance_visible} label="Entrance visible" />
          <Check ok={a.framing_usable} label="Framing usable" />
        </ul>
        <p className="trail__note">
          Shopfront read as <strong>{a.frontage_palette.replace('_', ' ')}</strong> → selected the{' '}
          <strong>{venue.product_slug.replace('_', ' ')}</strong> for contrast.
        </p>
        {a.obstructions.length > 0 && (
          <p className="trail__note muted">Obstructions noted: {a.obstructions.join(', ')}</p>
        )}
      </section>

      <section className="trail__stage">
        <h4 className="trail__title">
          <span className="trail__num">4</span> Measured
        </h4>
        <dl className="kv">
          <dt>Door height</dt>
          <dd>{m.door_height_px.toFixed(0)} px</dd>
          <dt>Scale</dt>
          <dd>{m.px_per_metre.toFixed(0)} px/m</dd>
          <dt>Planter should be</dt>
          <dd>{m.expected_planter_px.toFixed(0)} px</dd>
          <dt>Light</dt>
          <dd>{m.light_direction}</dd>
        </dl>
        <p className="trail__note muted">
          Scale derived from the door at an assumed {doorM.toFixed(2)} m, not guessed by the model.
        </p>
      </section>

      <section className="trail__stage">
        <h4 className="trail__title">
          <span className="trail__num">6</span> Verified
        </h4>
        <ul className="checks">
          <Check ok={v.building_unaltered} label="Building & signage unaltered" />
          <Check ok={v.product_faithful_to_reference} label="Product faithful to reference" />
          <Check ok={v.grounded_with_shadow} label="Grounded with consistent shadow" />
          <Check ok={!v.planter_blocks_entrance} label="Entrance not blocked" />
          <Check
            ok={v.scale_within_tolerance !== false}
            label={
              v.scale_ratio != null
                ? `Scale within ±${tolPct}% (rendered at ${(v.scale_ratio * 100).toFixed(0)}% of expected)`
                : 'Scale plausible'
            }
          />
        </ul>
        <p className="trail__note muted">
          Passed on attempt {venue.attempts} of {maxAttempts}.
        </p>
      </section>
    </div>
  );
}
