/**
 * Everything the pipeline threw out, and why.
 *
 * The most important section on the page. Anyone can show three good-looking
 * composites; only an automated pipeline can show its own discards. It renders
 * in full, in the normal flow, never behind a tab — hiding it would defeat the
 * point of it.
 *
 * Decisions and errors are kept apart. A venue rejected because it is a chain is
 * the system working; a venue skipped because the API returned 429 is the system
 * breaking, and the pipeline never formed a view about that venue at all.
 * Listing them together would pad the funnel with judgements that were never
 * made — so errors get their own section, clearly labelled as not-a-judgement.
 */

import type { Rejection, Stage } from '../types';

const STAGE_LABELS: Record<Stage, string> = {
  discover: 'Filtered at discovery',
  capture: 'No usable photograph',
  assess: 'Frontage not suitable',
  measure: 'Could not establish scale',
  composite: 'Generation failed',
  verify: 'Failed verification',
};

const STAGE_BLURBS: Record<Stage, string> = {
  discover: 'Chains, non-street-facing units, and venues that are not operational.',
  capture: 'No Street View panorama close enough, and no usable Places photo either.',
  assess:
    'Photographed, but the entrance was not visible, the framing was unusable, or the frontage was already dressed.',
  measure: 'The doorway could not be measured reliably, so no scale could be derived.',
  composite: 'The image model did not return a usable generation.',
  verify: 'A planter was generated, but the result was not safe to send to the owner.',
};

// Pipeline order, so the list reads as a funnel top to bottom.
const STAGE_ORDER: Stage[] = ['discover', 'capture', 'assess', 'measure', 'composite', 'verify'];

function groupByStage(items: Rejection[]) {
  return STAGE_ORDER.map((stage) => ({
    stage,
    items: items.filter((r) => r.stage === stage),
  })).filter((g) => g.items.length > 0);
}

function RejectRow({ r }: { r: Rejection }) {
  return (
    <li className="reject">
      <div className="reject__who">
        <span className="reject__name">{r.venue_name}</span>
        {r.address && <span className="reject__addr">{r.address}</span>}
      </div>
      <ul className={r.kind === 'error' ? 'reject__reasons reject__reasons--err' : 'reject__reasons'}>
        {r.reasons.map((reason, i) => (
          <li key={i}>{reason}</li>
        ))}
      </ul>
      {r.detail && <p className="reject__detail">{r.detail}</p>}
    </li>
  );
}

export function RejectedList({ rejected }: { rejected: Rejection[] }) {
  const decisions = rejected.filter((r) => r.kind !== 'error');
  const errors = rejected.filter((r) => r.kind === 'error');

  if (rejected.length === 0) {
    return (
      <p className="empty">
        Nothing was rejected in this run. That is unusual — check the funnel above.
      </p>
    );
  }

  return (
    <div className="rejects">
      {decisions.length === 0 && (
        <p className="empty">
          The pipeline made no rejection decisions in this run — every candidate that reached a
          judgement passed, or the run stopped early on errors (below).
        </p>
      )}

      {groupByStage(decisions).map(({ stage, items }) => (
        <section key={stage} className="rejects__group">
          <header className="rejects__head">
            <h3 className="rejects__title">
              {STAGE_LABELS[stage]} <span className="rejects__count">{items.length}</span>
            </h3>
            <p className="rejects__blurb">{STAGE_BLURBS[stage]}</p>
          </header>
          <ul className="rejects__list">
            {items.map((r) => (
              <RejectRow key={`${r.venue_id}-${r.stage}`} r={r} />
            ))}
          </ul>
        </section>
      ))}

      {errors.length > 0 && (
        <section className="rejects__group rejects__group--err">
          <header className="rejects__head">
            <h3 className="rejects__title">
              Not decided — pipeline errors <span className="rejects__count">{errors.length}</span>
            </h3>
            <p className="rejects__blurb">
              These venues were <strong>never judged</strong>. The pipeline failed before it could
              form a view — an API quota, a retired model, a network fault. They are listed
              separately because counting them as rejections would credit the system with decisions
              it never made.
            </p>
          </header>
          <ul className="rejects__list">
            {errors.map((r) => (
              <RejectRow key={`${r.venue_id}-${r.stage}-err`} r={r} />
            ))}
          </ul>
        </section>
      )}
    </div>
  );
}
