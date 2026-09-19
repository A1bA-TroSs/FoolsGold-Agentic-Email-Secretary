import { useLayoutEffect, useRef } from 'react';

/* FLIP reordering.

   When a score changes, rows move. Without this they teleport, and the user
   cannot tell whether the email they just acted on moved up, moved down, or
   vanished -- which is exactly the feedback that makes a ranked list feel
   trustworthy rather than arbitrary.

   First / Last / Invert / Play: measure where every row was, let React paint
   the new order, measure again, then animate each row from its old offset back
   to zero. The transform runs on the compositor, so a 200-row list stays smooth.

   The easing overshoots slightly on settle -- that little bounce is what makes
   the movement read as physical rather than mechanical.
*/
const DURATION = 420;
const EASING = 'cubic-bezier(.22,.98,.30,1.12)';
/* Roughly one screen of slack either side, so a row scrolling into view
   is not caught mid-animation. */
const VIEWPORT_MARGIN = 600;

export function useFlip(containerRef, deps, { enabled = true } = {}) {
  const positions = useRef(new Map());
  const first = useRef(true);

  useLayoutEffect(() => {
    const root = containerRef.current;
    if (!root) return;

    const nodes = Array.from(root.querySelectorAll('[data-id]'));
    const next = new Map();
    for (const node of nodes) next.set(node.dataset.id, node.getBoundingClientRect().top);

    // Skip the very first pass: everything is "new", and the entry animation
    // already covers it. Animating both at once looks like a glitch.
    if (first.current || !enabled) {
      first.current = false;
      positions.current = next;
      return;
    }

    const reduced = window.matchMedia?.('(prefers-reduced-motion: reduce)').matches;

    /* Only animate what the user can actually see.

       Measured: ticking the top row of a 187-row list changed its score, which
       moved every row below it, which created 186 Web Animations inside one
       frame -- a 233ms long animation frame with 182ms of blocking script and
       *zero* style-and-layout time. It was never paint; it was allocating one
       animation per row for rows nobody was looking at.

       FLIP exists to answer "where did the thing I just touched go?", and that
       question can only be asked about rows on screen. Everything outside the
       viewport is allowed to teleport, because teleporting offscreen is
       invisible by definition. The margin keeps a row that is about to scroll
       into view from arriving mid-flight. */
    const viewportTop = -VIEWPORT_MARGIN;
    const viewportBottom = (window.innerHeight || 0) + VIEWPORT_MARGIN;

    for (const node of nodes) {
      const id = node.dataset.id;
      const before = positions.current.get(id);
      const after = next.get(id);
      if (before === undefined || after === undefined) continue;

      const delta = before - after;
      if (Math.abs(delta) < 2) continue;

      // Offscreen both before and after: nobody saw it move.
      if ((before < viewportTop || before > viewportBottom)
          && (after < viewportTop || after > viewportBottom)) continue;

      if (reduced) continue;
      node.animate(
        [{ transform: `translateY(${delta}px)` }, { transform: 'translateY(0)' }],
        { duration: DURATION, easing: EASING, composite: 'replace' },
      );
    }
    positions.current = next;
  }, deps);
}
