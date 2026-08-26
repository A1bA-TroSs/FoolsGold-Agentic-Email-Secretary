import { useEffect, useRef } from 'react';
import { useFlip } from '../lib/useFlip.js';
import { CheckIcon, ClockIcon, MuteIcon, PaperclipIcon, PinIcon } from './Icons.jsx';

function when(iso) {
  if (!iso) return '';
  const d = new Date(iso);
  const now = new Date();
  if (d.toDateString() === now.toDateString()) {
    return d.toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' });
  }
  if (d.getFullYear() === now.getFullYear()) {
    return d.toLocaleDateString([], { month: 'short', day: 'numeric' });
  }
  return d.toLocaleDateString([], { year: '2-digit', month: 'short', day: 'numeric' });
}

function dueLabel(deadline) {
  if (!deadline) return null;
  const today = new Date(); today.setHours(0, 0, 0, 0);
  const due = new Date(`${deadline}T00:00:00`);
  const days = Math.round((due - today) / 86400000);
  if (days < -21) return null;          // a deadline this old no longer scores, so stop shouting about it
  if (days < 0) return `${-days}d overdue`;
  if (days === 0) return 'due today';
  if (days === 1) return 'due tomorrow';
  if (days <= 7) return `due in ${days}d`;
  return `due ${deadline.slice(5)}`;
}

const ACTIONS = [
  { verdict: 'pinned',        Icon: PinIcon,   title: 'Pin to the top (p)' },
  { verdict: 'done',          Icon: CheckIcon, title: 'Mark handled (e)' },
  { verdict: 'not_important', Icon: MuteIcon,  title: 'Not important (x)' },
  { verdict: 'snoozed',       Icon: ClockIcon, title: 'Snooze a day (s)' },
];

export default function MailList({ items, selectedId, cursorId, onSelect, onFeedback, leavingId }) {
  const listRef = useRef(null);

  // Rows slide to their new rank instead of teleporting there.
  useFlip(listRef, [items.map((m) => `${m.id}:${m.score}`).join('|')]);

  // Keep the keyboard cursor in view when it moves off-screen.
  useEffect(() => {
    if (!cursorId || !listRef.current) return;
    const node = listRef.current.querySelector(`[data-id="${CSS.escape(cursorId)}"]`);
    node?.scrollIntoView({ block: 'nearest' });
  }, [cursorId]);

  if (!items.length) {
    return <div className="empty">Nothing here. Try another filter, or hit refresh.</div>;
  }

  return (
    <div ref={listRef}>
      {items.map((m) => {
        const due = dueLabel(m.deadline);
        return (
          <div
            key={m.id}
            data-id={m.id}
            role="button"
            tabIndex={0}
            className={[
              'mail-item',
              m.bucket || '',
              m.verdict || '',
              m.is_read ? '' : 'unread',
              m.id === selectedId ? 'selected' : '',
              m.id === cursorId ? 'cursor' : '',
              m.id === leavingId ? 'leaving' : '',
            ].filter(Boolean).join(' ')}
            onClick={() => onSelect(m.id)}
            onKeyDown={(e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); onSelect(m.id); } }}
          >
            <div className="row-actions" onClick={(e) => e.stopPropagation()}>
              {ACTIONS.map(({ verdict, Icon, title }) => (
                <button
                  key={verdict}
                  title={title}
                  aria-label={title}
                  className={m.verdict === verdict ? 'on' : ''}
                  onClick={() => onFeedback(m.id, m.verdict === verdict ? null : verdict)}
                >
                  <Icon />
                </button>
              ))}
            </div>

            <div className="row">
              <span className="subj">{m.subject}</span>
              <span className="when">{when(m.received_at)}</span>
            </div>

            <div className="from">
              {m.from_name || m.from_address}
              {m.has_attachments ? <> &nbsp;<PaperclipIcon /></> : null}
            </div>

            <div className="row" style={{ gap: 5, marginTop: 4, flexWrap: 'wrap' }}>
              {m.bucket && <span className={`tag ${m.bucket}`}>{m.bucket}</span>}
              {due && <span className="tag due">{due}</span>}
              {m.is_flagged ? <span className="tag due">starred</span> : null}
              {m.is_answered ? <span className="tag noise">replied</span> : null}
              {m.verdict && <span className="tag noise verdict-chip">{m.verdict.replace('_', ' ')}</span>}
              {(m.matched || []).slice(0, 2).map((t) => (
                <span key={t} className="tag fyi">{t}</span>
              ))}
            </div>

            <div className="snip">{m.body_preview}</div>
          </div>
        );
      })}
    </div>
  );
}
