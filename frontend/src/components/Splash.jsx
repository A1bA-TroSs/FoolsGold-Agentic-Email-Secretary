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
      {/* Pyrite, falling. Eight shards, not a particle storm -- restraint even
          in the one place this app allows itself a flourish, because the splash
          is the only screen with no content to compete with.

          Angular, because pyrite grows in cubes and its whole trick is a flat
          facet catching light; round sparkles would be some other mineral. Each
          shard is dull on the way down and turns gold for a moment as it passes
          -- which is the name, animated: it looks like gold exactly once.

          Pure CSS on purpose. This runs while the backend is waking, the
          mailbox is being read and 200 emails are being ranked, so a
          requestAnimationFrame particle loop would take main-thread time from
          the work the user is actually waiting for. Transform and opacity only,
          so it stays on the compositor. */}
      <div className="splash-flecks" aria-hidden="true">
        {Array.from({ length: 8 }, (_, i) => <i key={i} />)}
      </div>

      <div className="splash-mark">
        <span className="splash-ring" />
        <img className="brand-mark" src="./logo.png" alt="" />
      </div>
      <div className="splash-name">{t('appName')}</div>
      <div className="splash-stage">{detail || t(STAGE_KEYS[stage])}</div>
      <div className="splash-track"><i /></div>
    </div>
  );
}
