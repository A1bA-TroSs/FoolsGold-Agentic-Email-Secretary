import { useEffect, useRef, useState } from 'react';
import { useFlip } from '../lib/useFlip.js';
import { useT } from '../lib/i18n.js';
import { dueChip } from '../lib/due.js';
import { ClockIcon, HighlightIcon, MuteIcon, PaperclipIcon, PinIcon } from './Icons.jsx';
import HighlightPicker from './HighlightPicker.jsx';

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

// Same rule as the briefing, from the same place -- these two used to have
// separate copies of the arithmetic and disagreed about when to stop.
const dueLabel = (deadline, t) => dueChip(deadline, t)?.label ?? null;

/* The two verdicts that need a gesture rather than a menu live outside this
   list: "done" is the checkbox, and the third button is mute. It was labelled
   "Not important" back when it demoted a single message; it now bans the whole
   sender, so that label promised something far smaller than what it does. */
const DWELL_MS = 420;

const ACTIONS = [
  { verdict: 'pinned',  Icon: PinIcon,   key: 'pinToTop',   hint: 'p', cls: '' },
  { verdict: 'mute',    Icon: MuteIcon,  key: 'muteSenderAction', hint: 'x', cls: 'mute' },
  { verdict: 'snoozed', Icon: ClockIcon, key: 'snoozeADay', hint: 's', cls: '' },
];

function Tick() {
  return (
    <svg viewBox="0 0 16 16" aria-hidden="true">
      <path d="M2.5 8.5 6.2 12 13.5 4" />
    </svg>
  );
}

export default function MailList({
  items, selectedId, cursorId, onSelect, onFeedback, onMute, onHighlight, leaving, mutedView,
}) {
  const listRef = useRef(null);
  const t = useT();
  const [pickerFor, setPickerFor] = useState(null);

  /* The row actions only arm after the pointer has rested on a row for a
     moment. Before this they appeared instantly on hover, sitting over the
     subject line -- so reaching for an email to *read* it frequently pinned or
     muted it instead, and the undo toast became part of normal use. */
  const [armedId, setArmedId] = useState(null);
  const dwell = useRef(null);

  const startDwell = (id) => {
    clearTimeout(dwell.current);
    dwell.current = setTimeout(() => setArmedId(id), DWELL_MS);
  };
  const cancelDwell = () => {
    clearTimeout(dwell.current);
    setArmedId(null);
  };
  useEffect(() => () => clearTimeout(dwell.current), []);

  // Rows slide to their new rank rather than teleporting there.
  useFlip(listRef, [items.map((m) => `${m.id}:${m.score}:${m.verdict || ''}`).join('|')]);

  useEffect(() => {
    if (!cursorId || !listRef.current) return;
    const node = listRef.current.querySelector(`[data-id="${CSS.escape(cursorId)}"]`);
    node?.scrollIntoView({ block: 'nearest' });
  }, [cursorId]);

  if (!items.length) {
    return <div className="empty">{t('emptyList')}</div>;
  }

  return (
    <div ref={listRef}>
      {items.map((m) => {
        const due = dueLabel(m.deadline, t);
        const done = m.verdict === 'done';
        const anim = leaving?.id === m.id ? leaving.kind : null;

        return (
          <div
            key={m.id}
            data-id={m.id}
            role="button"
            tabIndex={0}
            className={[
              'mail-item',
              m.highlight ? `hl hl-${m.highlight}` : '',
              m.bucket || '',
              m.verdict || '',
              m.is_read ? '' : 'unread',
              m.id === selectedId ? 'selected' : '',
              m.id === cursorId ? 'cursor' : '',
              anim === 'done' ? 'completing done-out' : '',
              anim === 'banish' ? 'banishing' : '',
              anim === 'burn' ? 'burning' : '',
              mutedView ? 'from-muted' : '',
            ].filter(Boolean).join(' ')}
            onClick={() => onSelect(m.id)}
            onMouseEnter={() => startDwell(m.id)}
            onMouseLeave={cancelDwell}
            onFocus={() => setArmedId(m.id)}
            onKeyDown={(e) => {
              if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); onSelect(m.id); }
            }}
          >
            {/* The checkbox is the primary gesture: this list is a to-do list. */}
            <label className="row-check" onClick={(e) => e.stopPropagation()}
                   title={done ? t('markNotDone') : `${t('markHandled')} (e)`}>
              <input
                type="checkbox"
                checked={done}
                aria-label={`${t('markHandled')}: ${m.subject}`}
                onChange={() => onFeedback(m.id, done ? null : 'done')}
              />
              <span className="box"><Tick /></span>
              <span className="ripple" />
            </label>

            <div className={`row-actions ${armedId === m.id ? 'armed' : ''}`}
                 onClick={(e) => e.stopPropagation()}
                 aria-hidden={armedId !== m.id}>
              {ACTIONS.map(({ verdict, Icon, key, hint, cls }) => (
                <button
                  key={verdict}
                  title={`${t(key)} (${hint})`}
                  aria-label={t(key)}
                  className={[cls, m.verdict === verdict ? 'on' : ''].filter(Boolean).join(' ')}
                  onClick={() => (verdict === 'mute'
                    ? onMute(m)
                    : onFeedback(m.id, m.verdict === verdict ? null : verdict))}
                >
                  <Icon />
                </button>
              ))}
              {/* The opposite of the mute beside it: same gesture, same place,
                  same scope -- the whole correspondent, not this message. */}
              <button
                className={`hl-btn ${m.highlight ? `on hl-${m.highlight}` : ''}`}
                title={t('highlightSender')} aria-label={`${t('highlightSender')}: ${m.subject}`}
                onClick={() => setPickerFor(pickerFor === m.id ? null : m.id)}
              >
                <HighlightIcon />
              </button>
            </div>

            {pickerFor === m.id && (
              <HighlightPicker
                current={m.highlight}
                label={`${t('highlightSender')}: ${m.from_name || m.from_address}`}
                onPick={(c) => { setPickerFor(null); onHighlight(m.from_address, c); }}
                onClear={() => { setPickerFor(null); onHighlight(m.from_address, null); }}
                onClose={() => setPickerFor(null)}
              />
            )}

            <div className="row">
              <span className="subj">{m.subject}</span>
              <span className="when">{when(m.received_at)}</span>
            </div>

            <div className="from">
              {m.from_name || m.from_address}
              {m.has_attachments ? <> &nbsp;<PaperclipIcon /></> : null}
            </div>

            <div className="row" style={{ gap: 5, marginTop: 4, flexWrap: 'wrap' }}>
              {m.bucket && <span className={`tag ${m.bucket}`}>{t(`tag${m.bucket[0].toUpperCase()}${m.bucket.slice(1)}`)}</span>}
              {/* Labelled, never silent. An unexplained wrong row reads as a
                  bug; a labelled one is an invitation to correct it -- and that
                  correction is the only unbiased evidence the ranker ever gets,
                  because everything else it learns from was already shown. */}
              {m.explored ? (
                <span className="tag explored" title={t('exploredTitle')}>{t('exploredBadge')}</span>
              ) : null}
              {due && <span className="tag due">{due}</span>}
              {m.is_flagged ? <span className="tag due">{t('tagStarred')}</span> : null}
              {m.is_answered ? <span className="tag noise">{t('tagReplied')}</span> : null}
              {m.verdict && m.verdict !== 'done' && (
                <span className="tag noise verdict-chip">
                  {t({ pinned: 'pinToTop', not_important: 'notImportant', snoozed: 'snoozeADay' }[m.verdict] || m.verdict)}
                </span>
              )}
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
