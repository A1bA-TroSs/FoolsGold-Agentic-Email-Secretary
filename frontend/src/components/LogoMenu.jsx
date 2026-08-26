import { useEffect, useRef } from 'react';
import { MailSyncIcon, RefreshIcon } from './Icons.jsx';

/* The logo is the app's only always-present control, so it gets the job nothing
   else wants: a status panel. What is in the inbox, where the mail is coming
   from, whether AI is running, when it last synced. Previously it was a
   decorative image that did nothing when clicked, which invites the click and
   then ignores it. */
export default function LogoMenu({ open, onClose, counts, source, ai, lastSync, onSync, syncing, version }) {
  const ref = useRef(null);

  useEffect(() => {
    if (!open) return undefined;
    const onDown = (e) => { if (!ref.current?.contains(e.target)) onClose(); };
    const onKey = (e) => { if (e.key === 'Escape') onClose(); };
    document.addEventListener('mousedown', onDown);
    document.addEventListener('keydown', onKey);
    return () => {
      document.removeEventListener('mousedown', onDown);
      document.removeEventListener('keydown', onKey);
    };
  }, [open, onClose]);

  if (!open) return null;

  const total = (counts.action || 0) + (counts.fyi || 0) + (counts.noise || 0);
  const when = lastSync
    ? new Date(lastSync).toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' })
    : 'not yet';

  return (
    <div className="logo-menu" ref={ref} role="dialog" aria-label="Status">
      <div className="lm-head">
        <img src="./logo.png" alt="" />
        <div>
          <strong>Fool&rsquo;s Gold</strong>
          <span>{version || 'v1.0'}</span>
        </div>
      </div>

      <div className="lm-stats">
        {[
          { k: 'action', label: 'Needs you' },
          { k: 'fyi', label: 'FYI' },
          { k: 'noise', label: 'Noise' },
        ].map(({ k, label }) => (
          <div className="lm-stat" key={k}>
            <b>{counts[k] || 0}</b>
            <span>{label}</span>
            <i style={{ width: total ? `${((counts[k] || 0) / total) * 100}%` : 0 }} />
          </div>
        ))}
      </div>

      <dl className="lm-rows">
        <div><dt>Source</dt><dd>{source?.account || source?.label || '—'}</dd></div>
        <div>
          <dt>AI</dt>
          <dd>{ai?.off ? 'off — structural only' : ai?.available ? 'running' : 'unavailable'}</dd>
        </div>
        <div><dt>Last sync</dt><dd>{when}</dd></div>
      </dl>

      <button className="btn primary" style={{ width: '100%' }} onClick={onSync} disabled={syncing}>
        {syncing ? <><MailSyncIcon /> Syncing…</> : <><RefreshIcon /> Sync now</>}
      </button>
    </div>
  );
}
