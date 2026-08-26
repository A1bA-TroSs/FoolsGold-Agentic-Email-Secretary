import { useEffect, useState } from 'react';

/* Startup.

   Booting means: reach the backend, read settings, check the mail source, load
   the list. That is a second or two of nothing, and a bare spinner during it
   makes the app feel broken before it has done anything. This shows the mark
   settling into place and names the step in progress, so the wait reads as work
   rather than as a hang.

   It also holds for a short minimum so it never flashes on a fast start -- a
   splash that appears and disappears in 80ms is worse than none. */
const STAGES = [
  'Waking the backend',
  'Finding your mailbox',
  'Ranking what arrived',
];

export default function Splash({ done, detail }) {
  const [stage, setStage] = useState(0);
  const [gone, setGone] = useState(false);

  useEffect(() => {
    const t = setInterval(() => setStage((s) => Math.min(STAGES.length - 1, s + 1)), 620);
    return () => clearInterval(t);
  }, []);

  useEffect(() => {
    if (!done) return undefined;
    const t = setTimeout(() => setGone(true), 260);   // let the fade finish
    return () => clearTimeout(t);
  }, [done]);

  if (gone) return null;

  return (
    <div className={`splash ${done ? 'leaving' : ''}`} role="status" aria-live="polite">
      <div className="splash-mark">
        <span className="splash-ring" />
        <img src="./logo.png" alt="" />
      </div>
      <div className="splash-name">Fool&rsquo;s Gold</div>
      <div className="splash-stage">{detail || STAGES[stage]}</div>
      <div className="splash-track"><i /></div>
    </div>
  );
}
