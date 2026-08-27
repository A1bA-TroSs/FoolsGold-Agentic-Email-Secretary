import { useEffect, useState } from 'react';
import { useT } from '../lib/i18n.js';

/* Startup.

   Booting means: reach the backend, read settings, check the mail source, load
   the list. That is a second or two of nothing, and a bare spinner during it
   makes the app feel broken before it has done anything. This shows the mark
   settling into place and names the step in progress, so the wait reads as work
   rather than as a hang.

   It also holds for a short minimum so it never flashes on a fast start -- a
   splash that appears and disappears in 80ms is worse than none. */
const STAGE_KEYS = ['bootWaking', 'bootFinding', 'bootRanking'];

export default function Splash({ done, detail }) {
  const t = useT();
  const [stage, setStage] = useState(0);
  const [gone, setGone] = useState(false);

  useEffect(() => {
    const timer = setInterval(() => setStage((s) => Math.min(STAGE_KEYS.length - 1, s + 1)), 620);
    return () => clearInterval(timer);
  }, []);

  useEffect(() => {
    if (!done) return undefined;
    const timer = setTimeout(() => setGone(true), 260);   // let the fade finish
    return () => clearTimeout(timer);
  }, [done]);

  if (gone) return null;

  return (
    <div className={`splash ${done ? 'leaving' : ''}`} role="status" aria-live="polite">
      <div className="splash-mark">
        <span className="splash-ring" />
        <img src="./logo.png" alt="" />
      </div>
      <div className="splash-name">{t('appName')}</div>
      <div className="splash-stage">{detail || t(STAGE_KEYS[stage])}</div>
      <div className="splash-track"><i /></div>
    </div>
  );
}
