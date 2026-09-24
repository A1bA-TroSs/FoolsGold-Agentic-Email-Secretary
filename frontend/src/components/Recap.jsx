import { memo, useState } from 'react';
import { useT } from '../lib/i18n.js';
import { dueChip } from '../lib/due.js';

/* What piled up, before you read any of it.

   The briefing beside this answers "what should I do next". This answers the
   question you have before that one -- "I haven't opened my mail; what's in
   there?" -- and it is deliberately NOT a report on what the app decided. The
   suppressed count is one grey line at the bottom, not the point.

   Two rules hold the whole thing up:

   1. **One line is one email.** Never a paragraph that mentions three things,
      because then each mention needs an anchor and an anchor nobody checked is
      how a summary starts pointing at the wrong mail. Here a line *is* an
      email (or one announcement sent three times), so the link cannot be wrong.
   2. **Expanding never leaves the card.** The payload carries every line; the
      collapse is local. Orientation is the only thing this card sells and a
      navigation in the middle of it spends what it just bought. */

// Visible before a section collapses. Smaller than the briefing's seven: that
// is a list you work through, this is a list you read.
const COLLAPSED = 6;

const SECTION_KEYS = { needs: 'recapNeeds', dated: 'recapDated', bulk: 'recapBulk' };

function sectionLabel(key, t) {
  if (SECTION_KEYS[key]) return t(SECTION_KEYS[key]);
  if (key === 'cat:other') return t('recapOther');
  if (key.startsWith('cat:')) return t(`cat_${key.slice(4)}`);
  return key;
}

const Line = memo(function Line({ line, t, active, onOpen }) {
  const due = dueChip(line.deadline, t);
  /* The subject is the title, always -- the same conclusion the briefing
     reached the hard way. What the app inferred sits underneath it: the
     obligation the mail created if it created one, otherwise the model's
     one-line summary of what it says, otherwise nothing at all. The ranking
     rationale is deliberately not offered here: it explains the app's sorting,
     and this card is not about the app. */
  const said = line.task || line.summary || '';
  return (
    <li className={`recap-line${active ? ' active' : ''}`}>
      <button className="recap-open" onClick={() => onOpen(line.email_id)}
              title={line.subject}>
        <span className="recap-subject">{line.subject || t('noSubject')}</span>
        {said && <span className="recap-said">{said}</span>}
        <span className="recap-meta">
          {due && <span className={`tag ${due.urgent ? 'due' : 'fyi'}`}>{due.label}</span>}
          {line.copies > 1 && (
            <span className="copies" title={t('copiesTitle', { n: String(line.copies) })}>
              ×{line.copies}
            </span>
          )}
          {line.sender && <span className="recap-sender">{line.sender}</span>}
        </span>
      </button>
    </li>
  );
});

function Section({ section, t, selectedId, onOpen }) {
  const [open, setOpen] = useState(false);
  const lines = open ? section.lines : section.lines.slice(0, COLLAPSED);
  const hidden = section.lines.length - lines.length;
  return (
    <section className="recap-section">
      <h4>
        {sectionLabel(section.key, t)}
        <span className="recap-count">{section.count}</span>
      </h4>
      <ul>
        {lines.map((line) => (
          <Line key={line.email_id} line={line} t={t}
                active={line.email_id === selectedId} onOpen={onOpen} />
        ))}
      </ul>
      {(hidden > 0 || open) && (
        <button className="btn ghost recap-expand" onClick={() => setOpen(!open)}>
          {open ? t('recapLess') : t('recapMore', { n: String(hidden) })}
        </button>
      )}
    </section>
  );
}

function Bulk({ bulk, t, selectedId, onOpen }) {
  const [open, setOpen] = useState(null);
  return (
    <section className="recap-section recap-bulk">
      <h4>{t('recapBulk')}<span className="recap-count">{bulk.count}</span></h4>
      <ul>
        {bulk.senders.map((s) => (
          <li key={s.sender} className="recap-bulk-sender">
            <button className="recap-open"
                    onClick={() => setOpen(open === s.sender ? null : s.sender)}>
              <span className="recap-subject">{s.sender}</span>
              <span className="recap-meta"><span className="copies">×{s.count}</span></span>
            </button>
            {open === s.sender && (
              <ul className="recap-nested">
                {s.lines.map((line) => (
                  <Line key={line.email_id} line={line} t={t}
                        active={line.email_id === selectedId} onOpen={onOpen} />
                ))}
              </ul>
            )}
          </li>
        ))}
      </ul>
    </section>
  );
}

export default function Recap({ card, onOpen, onDismiss, selectedId }) {
  const t = useT();
  /* `too_soon` is the app declining to speak rather than the app having nothing
     to say, and the difference matters: a card that reappears empty two minutes
     after it was dismissed is how a summary teaches you to ignore it. */
  if (!card || card.too_soon) return null;
  if (!card.sections.length && !card.bulk) return null;

  const since = card.since ? new Date(card.since) : null;
  const sinceLabel = since
    ? since.toLocaleDateString(undefined, { month: 'short', day: 'numeric' })
    : '';

  return (
    <div className="recap">
      <div className="recap-head">
        <h3>{t('recapTitle')}</h3>
        <span className="recap-window">
          {card.first_run ? t('recapFirstRun')
            : card.clamped ? t('recapClamped', { d: String(card.since ? 7 : 7) })
            : t('recapSince', { d: sinceLabel })}
          {' · '}
          {t('recapWaiting', { n: String(card.total) })}
        </span>
        {/* The only control in the app that moves the window. Not rendering the
            card, not opening mail from it -- dismissing it. Opening one thing
            to check it must not spend the summary. */}
        <button className="btn ghost recap-seen" onClick={onDismiss}>
          {t('recapDismiss')}
        </button>
      </div>

      {card.sections.map((section) => (
        <Section key={section.key} section={section} t={t}
                 selectedId={selectedId} onOpen={onOpen} />
      ))}

      {card.bulk && <Bulk bulk={card.bulk} t={t} selectedId={selectedId} onOpen={onOpen} />}

      {/* A footer, on purpose. What the ranker suppressed is real and the user
          is owed it -- but it is a fact about the app, and this card is about
          the mail. It was the centre of the first draft of this feature and
          that was the wrong object. */}
      {card.hidden?.hidden > 0 && (
        <p className="recap-hidden">
          {t('recapHidden', { n: String(card.hidden.hidden), m: String(card.hidden.explored) })}
        </p>
      )}
    </div>
  );
}
