import { RefreshIcon } from './Icons.jsx';
import { ThinkingNote } from './Loading.jsx';
import { useT } from '../lib/i18n.js';
import { dueChip } from '../lib/due.js';

/* Today's briefing, as a checklist.

   It used to be a block of model-written markdown. That read well and did
   nothing: you could not open the email a line referred to, and you could not
   tick it off, so the briefing and the inbox drifted apart the moment you
   started working. Every row here is bound to a real email id -- click it to
   read it, tick it to clear it, and it disappears from tomorrow's briefing
   because the same feedback drives the ranking. */

function Tick() {
  return <svg viewBox="0 0 16 16" aria-hidden="true"><path d="M2.5 8.5 6.2 12 13.5 4" /></svg>;
}

export default function Digest({ digest, loading, onRefresh, onOpen, onDone, selectedId, checkedIds }) {
  const t = useT();
  const items = digest?.items || [];

  /* The structural path sends a translation key, the model sends a finished
     sentence. Render whichever arrived. */
  const say = (obj, literal) =>
    (obj && typeof obj === 'object' && obj.key) ? t(obj.key, obj.vars) : (literal || '');

  return (
    <div className="digest">
      <h3>
        {t('digestTitle')}
        <button className="btn ghost" onClick={onRefresh} disabled={loading}
                title={t('digestRebuild')} aria-label={t('digestRebuild')}>
          <RefreshIcon spinning={loading} />
        </button>
      </h3>

      {loading && !digest ? (
        <ThinkingNote>{t('digestThinking')}</ThinkingNote>
      ) : (
        <>
          {digest?.headline && (
            <p className="digest-headline">
              {typeof digest.headline === 'string' ? digest.headline : say(digest.headline)}
            </p>
          )}

          {items.length > 0 ? (
            <ul className="agenda">
              {items.map((it, i) => {
                const due = dueChip(it.deadline, t);
                /* The row's own verdict is the truth, joined on by the
                   backend. `checkedIds` is the optimistic overlay so the tick
                   draws on the same frame you click, before the refetch. */
                const checked = checkedIds?.has(it.email_id) ?? false
                  ? true
                  : it.verdict === 'done';
                return (
                  <li
                    key={it.email_id}
                    className={[
                      'agenda-item',
                      checked ? 'checked' : '',
                      it.email_id === selectedId ? 'active' : '',
                    ].filter(Boolean).join(' ')}
                    style={{ animationDelay: `${Math.min(i, 7) * 45}ms` }}
                  >
                    <label className="row-check" onClick={(e) => e.stopPropagation()}
                           title={checked ? t('markNotDone') : t('markHandled')}>
                      <input
                        type="checkbox"
                        checked={checked}
                        aria-label={`${t('markHandled')}: ${it.subject}`}
                        onChange={() => onDone(it.email_id, checked ? null : 'done')}
                      />
                      <span className="box"><Tick /></span>
                      <span className="ripple" />
                    </label>

                    <button className="agenda-open" onClick={() => onOpen(it.email_id)}>
                      <span className="agenda-note">
                        {it.note_key ? t(it.note_key, it.note_vars) : (it.note || it.subject)}
                      </span>
                      <span className="agenda-meta">
                        {due && (
                          <span className={`tag ${due.urgent ? 'due' : 'fyi'}`}>{due.label}</span>
                        )}
                        <span className="agenda-subject">{it.subject}</span>
                        {it.sender && <span className="agenda-sender">· {it.sender}</span>}
                      </span>
                    </button>
                  </li>
                );
              })}
            </ul>
          ) : (
            <p className="digest-empty">{t('digestEmpty')}</p>
          )}
        </>
      )}

      {digest?.model && digest.model !== 'none' && (
        <div className="digest-foot">
          {digest.model === 'structural' ? t('digestNoAi') : t('digestVia', { model: digest.model })}
        </div>
      )}
    </div>
  );
}
