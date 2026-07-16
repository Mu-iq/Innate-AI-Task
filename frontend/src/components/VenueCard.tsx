/**
 * One accepted venue: who it is, the before/after, and why it got through.
 */

import type { Thresholds, VenueResult } from '../types';
import { BeforeAfter } from './BeforeAfter';
import { DecisionTrail } from './DecisionTrail';

interface Props {
  venue: VenueResult;
  thresholds: Thresholds | null;
}

export function VenueCard({ venue, thresholds }: Props) {
  const sourceLabel =
    venue.image_source === 'streetview'
      ? `Street View · heading ${venue.heading_used?.toFixed(0)}°`
      : 'Places photo (fallback)';

  return (
    <article className="card">
      <header className="card__head">
        <div>
          <h3 className="card__name">{venue.name}</h3>
          <p className="card__addr">
            {venue.address}
            {venue.postcode && !venue.address.includes(venue.postcode) ? ` · ${venue.postcode}` : ''}
          </p>
        </div>
        <div className="card__tags">
          <span className="tag tag--accent">{venue.area}</span>
          <span className="tag" title={sourceLabel}>
            {venue.image_source === 'streetview' ? 'Street View' : 'Places photo'}
          </span>
        </div>
      </header>

      <BeforeAfter beforeSrc={venue.frontage_url} afterSrc={venue.composite_url} alt={venue.name} />

      <p className="card__caption">
        {sourceLabel}
        {venue.pano_distance_m != null && ` · camera ${venue.pano_distance_m.toFixed(0)} m away`}
        {' · '}
        {venue.product_slug.replace('_', ' ')} fitted
      </p>

      <DecisionTrail venue={venue} thresholds={thresholds} />
    </article>
  );
}
