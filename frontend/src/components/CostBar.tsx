/**
 * Estimated cost of a run, broken down by what it was spent on.
 *
 * The estimate is the pipeline's own count of billable calls (cache hits are
 * free and never counted) times the configured provider prices. It is an
 * estimate, and labelled as one — the exact invoice is the provider's.
 */

import type { RunCost } from '../types';

const LABELS: Record<string, string> = {
  places: 'Places search',
  streetview: 'Street View',
  places_photos: 'Places photos',
  vision: 'Vision model',
  image: 'Image model',
};

function usd(n: number): string {
  return n < 0.01 && n > 0 ? `$${n.toFixed(4)}` : `$${n.toFixed(2)}`;
}

export function CostBar({ cost }: { cost: RunCost | null }) {
  if (!cost) return null;

  const lines = Object.entries(cost.cost_usd).filter(([, v]) => v > 0);

  return (
    <div className="cost">
      <div className="cost__total">
        <span className="cost__label">Estimated cost</span>
        <span className="cost__value">{usd(cost.total_cost_usd)}</span>
      </div>
      {lines.length > 0 && (
        <ul className="cost__lines">
          {lines.map(([k, v]) => (
            <li key={k}>
              <span>{LABELS[k] ?? k}</span>
              <span className="cost__num">{usd(v)}</span>
            </li>
          ))}
        </ul>
      )}
      <p className="cost__note">
        Estimate from counted API calls. {cost.counts.image_generations ?? 0} image
        {cost.counts.image_generations === 1 ? '' : 's'} generated ·{' '}
        {cost.counts.vision_calls ?? 0} vision calls.
      </p>
    </div>
  );
}
