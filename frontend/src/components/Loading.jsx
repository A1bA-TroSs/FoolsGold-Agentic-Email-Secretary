import { MailSyncIcon, ThinkingIcon } from './Icons.jsx';

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

export function SyncingNote({ children = 'Reading your mailbox…' }) {
  return <div className="loading-note"><MailSyncIcon />{children}</div>;
}

export function ThinkingNote({ children = 'Reading through what arrived…' }) {
  return <div className="loading-note"><ThinkingIcon />{children}</div>;
}

export function Sweep() {
  return <div className="sweep"><i /></div>;
}
