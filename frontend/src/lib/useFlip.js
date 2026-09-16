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

    for (const node of nodes) {
      const id = node.dataset.id;
      const before = positions.current.get(id);
      const after = next.get(id);
      if (before === undefined || after === undefined) continue;

      const delta = before - after;
      if (Math.abs(delta) < 2) continue;

      if (reduced) continue;
      node.animate(
        [{ transform: `translateY(${delta}px)` }, { transform: 'translateY(0)' }],
        { duration: DURATION, easing: EASING, composite: 'replace' },
      );
    }
    positions.current = next;
  }, deps);
}
