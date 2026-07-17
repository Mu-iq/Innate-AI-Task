/**
 * A small "what does this mean?" marker.
 *
 * The page is full of pipeline vocabulary — "passed filters", "assessed usable",
 * "not decided" — that is obvious once you know the system and opaque before
 * then. Rather than pad the layout with explanatory prose nobody rereads, the
 * terms carry their own definition on hover and focus.
 *
 * Focusable and keyboard-reachable: a tooltip only mouse users can open is a
 * tooltip half the readers don't have.
 */

interface Props {
  text: string;
  /** Where the bubble sits. Use "left" near the right edge or it clips. */
  align?: 'center' | 'left';
}

export function Info({ text, align = 'center' }: Props) {
  return (
    <span className={`info info--${align}`} tabIndex={0} role="note" aria-label={text}>
      <span className="info__mark" aria-hidden="true">
        ?
      </span>
      <span className="info__bubble" role="tooltip">
        {text}
      </span>
    </span>
  );
}
