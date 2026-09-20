import { memo, useCallback, useEffect, useRef, useState } from 'react';
import { useFlip } from '../lib/useFlip.js';
import { useT } from '../lib/i18n.js';
import { dueChip } from '../lib/due.js';
import { ClockIcon, HighlightIcon, IrrelevantIcon, MuteIcon, PaperclipIcon, PinIcon } from './Icons.jsx';
import HighlightPicker from './HighlightPicker.jsx';
/* The shared checkmark. This file used to define its own identical copy, so
   the component whose own comment says it is "shared so the priority list,
   the briefing and the calendar all animate the same stroke" was in fact
   used by two of the three -- and a prop added to the shared one reached
   everything except the list people actually tick things off in. */
import Tick from './Tick.jsx';

/* The row timestamp.

   This was the single most expensive function in the app, and nothing about it
   looks expensive. Profiled during a completion animation on a 187-row list, it
   held 58ms of self time -- more than React, more than the FLIP reorder, more
   than layout and paint put together, which measured zero.

   The cause is `toLocaleTimeString` / `toLocaleDateString`: each call
   constructs a fresh `Intl.DateTimeFormat`, which is one of the most expensive
   things in the platform. Building the formatter is the cost; `.format()` on an
   existing one is nearly free. Ticking one row re-renders every row, so the app
   was building three-hundred-odd Intl formatters inside a single frame, and the
   garbage from them showed up as another 47ms of GC.

   Two fixes, both boring: keep the formatters, and remember the answers. The
   cache is keyed on the ISO string and cleared when the day rolls over, because
   "today" is part of the output and a cached "09:00" would outlive its meaning
   at midnight. */
const FORMATTERS = new Map();
function formatter(key, opts) {
  let f = FORMATTERS.get(key);
  if (!f) { f = new Intl.DateTimeFormat(undefined, opts); FORMATTERS.set(key, f); }
  return f;
}

const WHEN_CACHE = new Map();
let whenDay = '';

function when(iso) {
  if (!iso) return '';
  const now = new Date();
  const today = now.toDateString();
  if (today !== whenDay) { WHEN_CACHE.clear(); whenDay = today; }

  const hit = WHEN_CACHE.get(iso);
  if (hit !== undefined) return hit;

  const d = new Date(iso);
  let out;
  if (d.toDateString() === today) {
    out = formatter('time', { hour: 'numeric', minute: '2-digit' }).format(d);
  } else if (d.getFullYear() === now.getFullYear()) {
    out = formatter('sameYear', { month: 'short', day: 'numeric' }).format(d);
  } else {
    out = formatter('other', { year: '2-digit', month: 'short', day: 'numeric' }).format(d);
  }
  WHEN_CACHE.set(iso, out);
  return out;
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
  /* "Not about me." The only gesture here that produces a clean negative.
     Everything else the app watches -- reading, replying, dwelling -- can only
     ever say yes; nothing a person does passively says "this kind of thing
     does not concern me", so no amount of observation can learn it.

     Deliberately next to mute and deliberately not the same thing. Mute is a
     standing decision about a correspondent; this is about one message and,
     through its category, about a kind of message. The department that sends
     the exam timetable also sends the hackathon invitations. */
  { verdict: 'not_relevant', Icon: IrrelevantIcon, key: 'notRelevant', hint: 'i', cls: 'dismissed' },
  { verdict: 'mute',    Icon: MuteIcon,  key: 'muteSenderAction', hint: 'x', cls: 'mute' },
  { verdict: 'snoozed', Icon: ClockIcon, key: 'snoozeADay', hint: 's', cls: '' },
];

/* The stored reason code turned into the user's language.

   `none` renders nothing on purpose: "no strong signal" is true but useless
   under a subject line, and a chip that says nothing trains people to stop
   reading the chips. */
export function whyLabel(t, m) {
  const code = m?.reason_code;
  if (!code || code === 'none') return '';
  const arg = m.reason_arg ?? '';
  if (code === 'deadline') {
    const days = Number(arg);
    if (!Number.isFinite(days)) return '';
    if (days === 0) return t('whyDeadlineToday');
    if (days < 0) return t('whyDeadlineOver', { arg: String(-days) });
    return t('whyDeadline', { arg: String(days) });
  }
  const key = 'why' + code.charAt(0).toUpperCase() + code.slice(1);
  const text = t(key, { arg });
  return text === key ? '' : text;
}

/* One row, memoised.

   The list is the only place in the app that renders a few hundred of
   anything, and it is also the place where a single click changes exactly one
   of them. Before this split, ticking one email off rebuilt all 187 rows:
   `items.map` produced a fresh element tree, React reconciled every one of
   them, and the 800ms completion animation ran across a 219ms frame it had to
   share with work that nothing had asked for.

   Memoisation only pays if the props hold still, so everything variable is
   reduced to a scalar before it crosses this boundary. The row is told whether
   *it* is armed, selected, under the cursor or leaving -- it is never handed
   `armedId`, `selectedId`, `cursorId` or `leaving` and left to compare, because
   those change whenever any row changes and would defeat the memo for all of
   them to inform one. Same reason the callbacks arrive through `useEvent`:
   a handler that is reborn with the state it reads is a handler that
   re-renders every row that holds it.

   Note what is NOT compared: `m` itself is compared by identity, which works
   only because `setMail(rows => rows.map(m => m.id === id ? {...m, verdict} : m))`
   returns the *same object* for untouched rows. A reload that rebuilds every
   row object is a full re-render, and should be -- the data really did
   change. */
const MailRow = memo(function MailRow({
  m, t, armed, picking, anim, selected, cursor, mutedView, doneView, dismissedView,
  onSelect, onFeedback, onMute, onHighlight, onArm, onDisarm, onPicker,
}) {
  const due = dueLabel(m.deadline, t);
  const done = m.verdict === 'done';
  // Computed once. It was called twice per row -- once to decide
  // whether to render the chip and once to fill it.
  const why = whyLabel(t, m);

  return (
    <div
      data-id={m.id}
      role="button"
      tabIndex={0}
      className={[
        'mail-item',
        m.highlight ? `hl hl-${m.highlight}` : '',
        m.bucket || '',
        m.verdict || '',
        m.is_read ? '' : 'unread',
        selected ? 'selected' : '',
        cursor ? 'cursor' : '',
        anim === 'done' ? 'completing done-out' : '',
        anim === 'banish' ? 'banishing' : '',
        anim === 'burn' ? 'burning' : '',
        // Neutral exit: leaving the completed box is not an achievement and
        // not a rejection, so it gets the plain lift rather than either.
        anim === 'restore' ? 'leaving' : '',
        mutedView ? 'from-muted' : '',
        doneView ? 'from-done' : '',
        dismissedView ? 'from-done' : '',
      ].filter(Boolean).join(' ')}
      onClick={() => onSelect(m.id)}
      onMouseEnter={() => onArm(m.id)}
      onMouseLeave={onDisarm}
      onFocus={() => onArm(m.id, true)}
      onKeyDown={(e) => {
        if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); onSelect(m.id); }
      }}
    >
      {/* The completion glow, mounted only while it is playing. It carries the
          gold tint and the ring that used to be a `background-color` and a
          `box-shadow` animation on the row itself -- both of which repainted
          the row, and the list around it, on every frame. See `.done-fx`. */}
      {anim === 'done' && <span className="done-fx" aria-hidden="true"><i /></span>}

      {/* The checkbox is the primary gesture: this list is a to-do list. */}
      <label className="row-check" onClick={(e) => e.stopPropagation()}
             title={done ? (doneView ? t('doneUndo') : t('markNotDone'))
                         : `${t('markHandled')} (e)`}>
        <input
          type="checkbox"
          checked={done}
          aria-label={`${t('markHandled')}: ${m.subject}`}
          onChange={() => onFeedback(m.id, done ? null : 'done')}
        />
        <span className="box"><Tick spark on={done} /></span>
        <span className="ripple" />
      </label>

      <div className={`row-actions ${armed ? 'armed' : ''}`}
           onClick={(e) => e.stopPropagation()}
           aria-hidden={!armed}>
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
          onClick={() => onPicker(picking ? null : m.id)}
        >
          <HighlightIcon />
        </button>
      </div>

      {picking && (
        <HighlightPicker
          current={m.highlight}
          label={`${t('highlightSender')}: ${m.from_name || m.from_address}`}
          onPick={(c) => { onPicker(null); onHighlight(m.from_address, c); }}
          onClear={() => { onPicker(null); onHighlight(m.from_address, null); }}
          onClose={() => onPicker(null)}
        />
      )}

      <div className="row">
        <span className="subj">{m.subject}</span>
        <span className="when">{when(m.received_at)}</span>
      </div>

      <div className="from">
        {m.from_name || m.from_address}
        {m.has_attachments ? <> &nbsp;<PaperclipIcon /></> : null}
        {/* This row stands for more than one message.
            Counted, never silent: the ranked list hides the other copies, and
            a list that quietly drops mail is indistinguishable from one that
            lost it. The number is what turns "where did the rest go" into
            "the department sent this three times". */}
        {m.copies > 1 ? (
          <span className="copies" title={t('copiesTitle', { n: String(m.copies) })}>
            ×{m.copies}
          </span>
        ) : null}
      </div>

      <div className="row" style={{ gap: 5, marginTop: 4, flexWrap: 'wrap' }}>
        {/* When it was finished, on the row, because the box is sorted by it
            and a list ordered by something invisible cannot be read. */}
        {/* What kind of thing the app thinks this is. Shown only in the review
            lists, where the user is auditing the decision rather than reading
            mail -- a chip on every row in the priority list would be one more
            thing to read past. */}
        {(doneView || dismissedView) && m.category ? (
          <span className="tag cat">{t(`cat_${m.category}`)}</span>
        ) : null}
        {doneView && m.decided_at ? (
          <span className="tag done-at" title={t('doneUndo')}>
            {t('doneAt', { arg: when(m.decided_at) })}
          </span>
        ) : null}
        {m.bucket && <span className={`tag ${m.bucket}`}>{t(`tag${m.bucket[0].toUpperCase()}${m.bucket.slice(1)}`)}</span>}
        {/* Labelled, never silent. An unexplained wrong row reads as a
            bug; a labelled one is an invitation to correct it -- and that
            correction is the only unbiased evidence the ranker ever gets,
            because everything else it learns from was already shown. */}
        {m.explored ? (
          <span className="tag explored" title={t('exploredTitle')}>{t('exploredBadge')}</span>
        ) : null}
        {/* One reason, not six. A ranking explained by every contributing
            factor is not explained -- the user cannot tell which one was
            wrong, which is exactly what a correction has to say. */}
        {why ? (
          <span className="tag why" title={t('whyLabel')}>{why}</span>
        ) : null}
        {due && <span className="tag due">{due}</span>}
        {m.is_flagged ? <span className="tag due">{t('tagStarred')}</span> : null}
        {m.is_answered ? <span className="tag noise">{t('tagReplied')}</span> : null}
        {/* The verdict, as a chip -- except in the list that IS that verdict,
            where it would be printed on every row and say nothing.

            The map is exhaustive by necessity, not tidiness: an unmapped
            verdict falls through to `t(m.verdict)`, and `t` returns the key it
            was given when it has no translation. That is how a raw
            `NOT_RELEVANT` ended up on screen in four languages the day this
            gesture was added. */}
        {m.verdict && m.verdict !== 'done'
          && !(doneView && m.verdict === 'done')
          && !(dismissedView && m.verdict === 'not_relevant') && (
          <span className="tag noise verdict-chip">
            {t({ pinned: 'pinToTop', not_important: 'notImportant',
                 not_relevant: 'notRelevant', snoozed: 'snoozeADay' }[m.verdict] || m.verdict)}
          </span>
        )}
        {/* Coerced, not trusted. `matched` is stored comma-joined and
            split into an array by the router; anything that skips that
            step -- an older row, a different caller, a fixture -- hands a
            STRING here, and `"CO-OP".slice(0,2).map` throws. React then
            unmounts the whole tree, so one malformed field costs the
            entire window rather than one chip. Same lesson as
            `_addressed_directly` crashing the classification batch on a
            bare recipient string. */}
        {(Array.isArray(m.matched) ? m.matched : []).slice(0, 2).map((topic) => (
          <span key={topic} className="tag fyi">{topic}</span>
        ))}
      </div>

      <div className="snip">{m.body_preview}</div>
    </div>
  );
});

export default function MailList({
  items, selectedId, cursorId, onSelect, onFeedback, onMute, onHighlight, leaving,
  mutedView, doneView, dismissedView,
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

  /* Stable for the same reason the handlers from App are: these cross the
     memo boundary, and a fresh closure here would re-render every row on
     every render. `immediate` is the focus path, which must not wait -- a
     keyboard user never dwells. */
  const arm = useCallback((id, immediate = false) => {
    clearTimeout(dwell.current);
    if (immediate) { setArmedId(id); return; }
    dwell.current = setTimeout(() => setArmedId(id), DWELL_MS);
  }, []);
  const disarm = useCallback(() => {
    clearTimeout(dwell.current);
    setArmedId(null);
  }, []);
  useEffect(() => () => clearTimeout(dwell.current), []);

  // Rows slide to their new rank rather than teleporting there.
  useFlip(listRef, [items.map((m) => `${m.id}:${m.score}:${m.verdict || ''}`).join('|')]);

  useEffect(() => {
    if (!cursorId || !listRef.current) return;
    const node = listRef.current.querySelector(`[data-id="${CSS.escape(cursorId)}"]`);
    node?.scrollIntoView({ block: 'nearest' });
  }, [cursorId]);

  if (!items.length) {
    return <div className="empty">{t(dismissedView ? 'emptyDismissed' : doneView ? 'emptyDone' : 'emptyList')}</div>;
  }

  return (
    <div ref={listRef}>
      {items.map((m) => (
        <MailRow
          key={m.id}
          m={m}
          t={t}
          armed={armedId === m.id}
          picking={pickerFor === m.id}
          anim={leaving?.id === m.id ? leaving.kind : null}
          selected={m.id === selectedId}
          cursor={m.id === cursorId}
          mutedView={!!mutedView}
          doneView={!!doneView}
          dismissedView={!!dismissedView}
          onSelect={onSelect}
          onFeedback={onFeedback}
          onMute={onMute}
          onHighlight={onHighlight}
          onArm={arm}
          onDisarm={disarm}
          onPicker={setPickerFor}
        />
      ))}
    </div>
  );
}
