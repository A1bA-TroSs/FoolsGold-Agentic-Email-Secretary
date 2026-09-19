import { memo } from 'react';
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

const PROVIDER_NAMES = {
  copilot: 'GitHub Copilot',
  anthropic: 'Claude',
  openai: 'OpenAI',
  ollama: 'Ollama',
};

/* One briefing row, memoised -- the same reasoning as the mail row.

   On a full mailbox this list is as long as the priority list, and it reloads
   from the same click: ticking an email refetches the digest 800ms later, at
   the exact moment the completion animation is finishing. Reused item objects
   (lib/reconcile.js) plus this memo mean that reload re-renders the rows that
   actually changed and nothing else. */
const AgendaItem = memo(function AgendaItem({ it, t, delay, checked, active, onOpen, onDone }) {
  const due = dueChip(it.deadline, t);
  return (
    <li
      className={['agenda-item', checked ? 'checked' : '', active ? 'active' : '']
        .filter(Boolean).join(' ')}
      style={{ animationDelay: `${delay}ms` }}
    >
      <label className="row-check" onClick={(e) => e.stopPropagation()}
             title={checked ? t('markNotDone') : t('markHandled')}>
        <input
          type="checkbox"
          checked={checked}
          aria-label={`${t('markHandled')}: ${it.subject}`}
          onChange={() => onDone(it.email_id, checked ? null : 'done')}
        />
        <span className="box"><Tick spark on={checked} /></span>
        <span className="ripple" />
      </label>

      <button className="agenda-open" onClick={() => onOpen(it.email_id)}>
        <span className="agenda-note">
          {it.note_key ? t(it.note_key, it.note_vars) : (it.note || it.subject)}
        </span>
        <span className="agenda-meta">
          {due && (<span className={`tag ${due.urgent ? 'due' : 'fyi'}`}>{due.label}</span>)}
          <span className="agenda-subject">{it.subject}</span>
          {it.sender && <span className="agenda-sender">· {it.sender}</span>}
        </span>
      </button>
    </li>
  );
});

export default function Digest({ digest, loading, onRefresh, onOpen, onDone, selectedId, checkedIds, onOpenSettings }) {
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
              {items.map((it, i) => (
                <AgendaItem
                  key={it.email_id}
                  it={it}
                  t={t}
                  delay={Math.min(i, 7) * 45}
                  checked={(checkedIds?.has(it.email_id) ?? false) || it.verdict === 'done'}
                  active={it.email_id === selectedId}
                  onOpen={onOpen}
                  onDone={onDone}
                />
              ))}
            </ul>
          ) : (
            <p className="digest-empty">{t('digestEmpty')}</p>
          )}
        </>
      )}

      {digest?.model && digest.model !== 'none' && (
        <div className="digest-foot">
          {/* `digest.model` is "provider:model" -- an internal identifier.
              "via copilot:auto" reads as a hardcoded string because "auto" is
              not a model name; it is the app's own placeholder leaking into the
              UI. Show the provider, which is the part that is both true and
              meaningful to the reader. */}
          {/* Not a bug report, an answer.

              "briefing written by GitHub Copilot" was read as an error by a
              user who believed he had stopped using Copilot. The string was
              true -- his provider setting said copilot and Copilot really did
              write it. The defect was that this footnote was the ONLY place in
              the app where the active provider appeared, so the first time he
              learned where his mail was going was from a caption.

              For a product whose argument is that mail never leaves the
              machine, "which provider is running" is not a footnote. A cloud
              provider now says so, and says where to change it. */}
          {digest.model === 'structural'
            ? t('digestNoAi')
            : (() => {
                const id = digest.model.split(':')[0];
                const name = PROVIDER_NAMES[id] || id;
                return id === 'ollama'
                  ? t('digestViaLocal', { model: name })
                  : (
                    <>
                      {t('digestViaCloud', { model: name })}
                      {onOpenSettings && (
                        <button className="btn ghost" style={{ marginLeft: 8, padding: '1px 8px' }}
                                onClick={onOpenSettings}>{t('openSettings')}</button>
                      )}
                    </>
                  );
              })()}
        </div>
      )}
    </div>
  );
}
