/**
 * Before/after comparison slider.
 *
 * The "after" image is clipped to the slider position and sits over the "before",
 * so dragging wipes between them in place. A side-by-side would halve each image;
 * the whole question here is whether the building is unchanged, and that is far
 * easier to judge when the two frames are registered on top of each other.
 */

import { useCallback, useRef, useState } from 'react';
import { imageUrl } from '../api/client';

interface Props {
  beforeSrc: string;
  afterSrc: string;
  alt: string;
}

export function BeforeAfter({ beforeSrc, afterSrc, alt }: Props) {
  const [position, setPosition] = useState(50);
  const containerRef = useRef<HTMLDivElement>(null);
  const dragging = useRef(false);

  const updateFromClientX = useCallback((clientX: number) => {
    const el = containerRef.current;
    if (!el) return;
    const rect = el.getBoundingClientRect();
    const pct = ((clientX - rect.left) / rect.width) * 100;
    setPosition(Math.min(100, Math.max(0, pct)));
  }, []);

  const onPointerDown = (e: React.PointerEvent) => {
    dragging.current = true;
    e.currentTarget.setPointerCapture(e.pointerId);
    updateFromClientX(e.clientX);
  };

  const onPointerMove = (e: React.PointerEvent) => {
    if (dragging.current) updateFromClientX(e.clientX);
  };

  const onPointerUp = (e: React.PointerEvent) => {
    dragging.current = false;
    e.currentTarget.releasePointerCapture(e.pointerId);
  };

  // Keyboard access: the slider is the primary control on this page, so it must
  // not be mouse-only.
  const onKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === 'ArrowLeft') setPosition((p) => Math.max(0, p - 5));
    if (e.key === 'ArrowRight') setPosition((p) => Math.min(100, p + 5));
  };

  return (
    <div
      className="ba"
      ref={containerRef}
      onPointerDown={onPointerDown}
      onPointerMove={onPointerMove}
      onPointerUp={onPointerUp}
    >
      {/* Base layer is the AFTER (composite). The clipped overlay is the BEFORE,
          shown on the left of the handle — so the left reads "Before" and the
          right reads "After", matching the labels and the usual reading order. */}
      <img
        className="ba__img"
        src={imageUrl(afterSrc)}
        alt={`${alt} — with planters`}
        draggable={false}
      />
      <div className="ba__after" style={{ clipPath: `inset(0 ${100 - position}% 0 0)` }}>
        <img
          className="ba__img"
          src={imageUrl(beforeSrc)}
          alt={`${alt} — before`}
          draggable={false}
        />
      </div>

      <div className="ba__line" style={{ left: `${position}%` }}>
        <div
          className="ba__handle"
          role="slider"
          tabIndex={0}
          aria-label={`Reveal planters on ${alt}`}
          aria-valuemin={0}
          aria-valuemax={100}
          aria-valuenow={Math.round(position)}
          onKeyDown={onKeyDown}
        >
          <span aria-hidden="true">‹›</span>
        </div>
      </div>

      <span className="ba__tag ba__tag--left">Before</span>
      <span className="ba__tag ba__tag--right">After</span>
    </div>
  );
}
