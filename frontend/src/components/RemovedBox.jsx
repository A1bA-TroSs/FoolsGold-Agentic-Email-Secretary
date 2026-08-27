import { useState } from 'react';
import { useT } from '../lib/i18n.js';
import { TrashIcon, UndoIcon } from './Icons.jsx';

/* Everything taken off the calendar, and the way back.

   Deleting a due date is a judgement -- "this is not really mine to do" -- and
   judgements get revised. It sits under the muted senders because the two
   boxes answer the same question: what have I suppressed, and how do I undo
   it? The difference is scope. Muting is about a correspondent and is undone
   by address; this is about single dates.

   Only hand-written tasks can be destroyed for good. An email's entry is a
   tombstone over a real message, so "delete forever" would mean deleting the
   tombstone -- which puts the date back. There is nothing here to destroy. */

function whenText(iso, t) {
  if (!iso) return '';
  const then = new Date(iso);
  if (Number.isNaN(then.getTime())) return '';
  const days = Math.floor((Date.now() - then.getTime()) / 86400000);
  if (days <= 0) return t('removedToday');
  if (days === 1) return t('removedYesterday');
  return t('removedDaysAgo', { n: days });
}

export default function RemovedBox({ items, onRestore, onPurge, busyKeys }) {
  const t = useT();
  const [confirm, setConfirm] = useState(null);

  return (
    <div className="removed-box">
      <h3>{t('removedTitle')}</h3>
      <p className="hint">{t('removedHelp')}</p>

      {!items.length ? (
        <p className="removed-empty">{t('removedEmpty')}</p>
      ) : (
        <ul className="removed-list">
          {items.map((it) => (
            <li key={it.key} className={busyKeys?.has(it.key) ? 'busy' : ''}>
              <span className="who">
                <b>{it.title}</b>
                <span>
                  <span className={`tag ${it.email_id ? 'fyi' : 'task'}`}>
                    {it.email_id ? t('calFromMail') : t('calOwnTask')}
                  </span>
                  {it.due && <> {t('removedWasDue', { date: it.due })}</>}
                  {it.removed_at && <> · {whenText(it.removed_at, t)}</>}
                </span>
              </span>

              {confirm === it.key ? (
                <span className="removed-confirm">
                  <button className="btn danger sm"
                          onClick={() => { setConfirm(null); onPurge(it); }}>
                    {t('removedPurge')}
                  </button>
                  <button className="btn ghost sm" onClick={() => setConfirm(null)}>
                    {t('cancel')}
                  </button>
                </span>
              ) : (
                <span className="removed-actions">
                  <button className="btn sm" onClick={() => onRestore(it)}>
                    <UndoIcon /> {t('removedRestore')}
                  </button>
                  {it.can_purge && (
                    <button className="icon-btn danger" onClick={() => setConfirm(it.key)}
                            title={t('removedPurgeTitle')} aria-label={`${t('removedPurgeTitle')}: ${it.title}`}>
                      <TrashIcon />
                    </button>
                  )}
                </span>
              )}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
