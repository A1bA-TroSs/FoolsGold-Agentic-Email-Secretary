import { useEffect, useState } from 'react';
import { MailSyncIcon, ThinkingIcon } from './Icons.jsx';
import { useT } from '../lib/i18n.js';

/* Loading states that say what is actually happening.

   A generic spinner tells the user "wait" and nothing else. Reading a mailbox,
   asking a model, and rendering a list fail differently and take different
   amounts of time, so they get different indicators. */

export function SkeletonList({ rows = 7 }) {
  return (
    <div aria-hidden="true">
      {Array.from({ length: rows }, (_, i) => (
        <div className="skel-row" key={i}>
          <div className="skel-bar" style={{ width: `${58 + ((i * 13) % 30)}%` }} />
          <div className="skel-bar" style={{ width: '34%', height: 7 }} />
          <div className="skel-bar" style={{ width: `${70 + ((i * 7) % 22)}%`, height: 7 }} />
        </div>
      ))}
    </div>
  );
}

export function SyncingNote({ children }) {
  const t = useT();
  return <div className="loading-note"><MailSyncIcon />{children || t('readingMailbox')}</div>;
}

export function ThinkingNote({ children }) {
  const t = useT();
  return <div className="loading-note"><ThinkingIcon />{children || t('thinking')}</div>;
}

export function Sweep() {
  return <div className="sweep"><i /></div>;
}


/* What the sync is actually doing, in the user's language.

   This replaced a hardcoded English `Syncing…` -- a generic label in one
   language, on a wait that runs for seconds. Published guidance puts anything
   in the 2-10 second band past the point where a bare spinner is the right
   instrument: it says "wait" without saying what for. The sweep already carries
   the motion, so this carries the words, and they change as the work does.

   Steps advance on a timer rather than from real progress, because the sync is
   a single POST with no interim signal. That is honest as long as the words
   stay true of the whole operation -- "reading your mailbox" then "ranking what
   arrived" are both things that are happening -- and it never claims a
   percentage it does not have. */
export function SyncStep() {
  const t = useT();
  const [step, setStep] = useState(0);

  useEffect(() => {
    const timer = setInterval(() => setStep((s) => Math.min(1, s + 1)), 1400);
    return () => clearInterval(timer);
  }, []);

  return (
    <span className="sync-step">
      <i className="dot" />
      {t(step === 0 ? 'syncReading' : 'syncRanking')}
    </span>
  );
}
