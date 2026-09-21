import { useEffect, useRef, useState } from 'react';
import { api } from '../lib/api.js';
import { useT } from '../lib/i18n.js';
import { CloseIcon, SendIcon } from './Icons.jsx';

/* The compose pane, and the approval step in front of sending.

   Two screens, deliberately. Writing and approving are different acts, and
   collapsing them into one button is how a half-finished message goes out: the
   draft view is editable and cannot send, the review view can send and cannot
   be edited. To change a word you go back, which re-renders the message on the
   server and produces a new token -- so what is approved is always what was
   just read.

   The backend enforces the same thing structurally: `POST /{token}/send` takes
   a token and has no request body, so nothing here can send anything the
   server did not render and hand back. This component cannot weaken that; it
   only makes it visible. */

const WRITE = 'write';
const REVIEW = 'review';
const DONE = 'done';

function Field({ label, value, onChange, placeholder, autoFocus }) {
  return (
    <label className="compose-field">
      <span>{label}</span>
      <input value={value} onChange={(e) => onChange(e.target.value)}
             placeholder={placeholder} autoFocus={autoFocus} spellCheck={false} />
    </label>
  );
}

export default function Compose({ action, emailId, onClose }) {
  const t = useT();
  const [stage, setStage] = useState(WRITE);
  const [to, setTo] = useState('');
  const [cc, setCc] = useState('');
  const [bcc, setBcc] = useState('');
  const [showCopies, setShowCopies] = useState(false);
  const [subject, setSubject] = useState('');
  const [text, setText] = useState('');
  const [draft, setDraft] = useState(null);
  const [outcome, setOutcome] = useState(null);
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const body = useRef(null);

  const isReply = action === 'reply' || action === 'reply_all';

  useEffect(() => { body.current?.focus(); }, []);

  /* Escape closes, but never from the review screen without a decision: a
     message you meant to send and a message you meant to discard look
     identical once the window is gone. */
  useEffect(() => {
    const onKey = (e) => {
      if (e.key === 'Escape' && stage !== REVIEW) onClose();
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [stage, onClose]);

  async function review() {
    setBusy(true); setError('');
    try {
      const made = await api.draft({
        action, email_id: emailId, subject, text,
        to: to ? [to] : [], cc: cc ? [cc] : [], bcc: bcc ? [bcc] : [],
      });
      setDraft(made);
      setStage(REVIEW);
    } catch (e) {
      setError(e.message);
    } finally {
      setBusy(false);
    }
  }

  async function confirm() {
    setBusy(true); setError('');
    try {
      const result = await api.sendDraft(draft.token);
      setOutcome(result.outcome);
      setStage(DONE);
    } catch (e) {
      setError(e.message);
    } finally {
      setBusy(false);
    }
  }

  const transport = draft?.transport;
  const verb = transport?.verb === 'saveDraft' ? t('composeSaveDraft') : t('composeSend');

  return (
    <div className="compose-scrim" onMouseDown={(e) => {
      /* Only the write stage closes on a click outside. Losing a reviewed
         message to a stray click is a worse failure than one extra click. */
      if (e.target === e.currentTarget && stage === WRITE) onClose();
    }}>
      <div className="compose" role="dialog" aria-label={t(`compose_${action}`)}>
        <header className="compose-head">
          <h3>{t(`compose_${action}`)}</h3>
          <button className="btn ghost icon" onClick={onClose} aria-label={t('close')}>
            <CloseIcon />
          </button>
        </header>

        {stage === WRITE && (
          <>
            <div className="compose-fields">
              <Field label={t('toLabel')} value={to} onChange={setTo}
                     placeholder={isReply ? t('composeDerived') : 'name@example.com'}
                     autoFocus={!isReply} />
              {showCopies ? (
                <>
                  <Field label={t('ccLabel')} value={cc} onChange={setCc} placeholder="" />
                  <Field label={t('bccLabel')} value={bcc} onChange={setBcc} placeholder="" />
                </>
              ) : (
                <button className="btn ghost compose-more" onClick={() => setShowCopies(true)}>
                  {t('composeAddCopies')}
                </button>
              )}
              {action !== 'reply' && action !== 'reply_all' && (
                <Field label={t('subjectLabel')} value={subject} onChange={setSubject} placeholder="" />
              )}
            </div>
            <textarea ref={body} className="compose-body" value={text} rows={12}
                      onChange={(e) => setText(e.target.value)}
                      placeholder={t('composePlaceholder')} />
            {error && <p className="compose-error">{error}</p>}
            <footer className="compose-foot">
              <span className="compose-note">{t('composeReviewNote')}</span>
              <button className="btn ghost" onClick={onClose}>{t('cancel')}</button>
              <button className="btn primary" disabled={busy} onClick={review}>
                {busy ? t('composeChecking') : t('composeReview')}
              </button>
            </footer>
          </>
        )}

        {stage === REVIEW && draft && (
          <>
            {/* Exactly what the server built. Not a re-render of the fields
                above: the point of this screen is to show the message as it
                will exist, including the headers the app filled in. */}
            <div className="compose-review">
              {/* `data-field` so the render check can assert on the To line
                  specifically. Reading these positionally passed a mutation
                  that showed the empty form field instead of the derived
                  recipient -- the From line below still had an address in it,
                  and a check that only asked "is there an address anywhere"
                  could not tell the difference. */}
              <dl>
                <dt>{t('fromLabel')}</dt><dd data-field="from">{draft.summary.from}</dd>
                <dt>{t('toLabel')}</dt><dd data-field="to">{draft.summary.to || '—'}</dd>
                {draft.summary.cc && (
                  <><dt>{t('ccLabel')}</dt><dd data-field="cc">{draft.summary.cc}</dd></>)}
                {draft.summary.bcc && (
                  <><dt>{t('bccLabel')}</dt>
                    <dd data-field="bcc">{draft.summary.bcc} <em>{t('composeBccNote')}</em></dd></>)}
                <dt>{t('subjectLabel')}</dt><dd data-field="subject">{draft.summary.subject}</dd>
                {draft.summary.in_reply_to && (
                  <><dt>{t('composeThread')}</dt>
                    <dd><em>{t('composeThreadNote')}</em></dd></>)}
              </dl>
              <pre className="compose-preview">{draft.summary.text}</pre>
            </div>
            {transport && !transport.ready && (
              <p className="compose-error">{transport.detail}</p>
            )}
            {transport?.mode === 'hands_off' && transport.ready && (
              /* Said before the click, not after. A button marked "Send" on a
                 transport that only files a draft is a lie the user finds out
                 about from the recipient who never got it. */
              <p className="compose-note handoff">{t('composeHandoffNote')}</p>
            )}
            {error && <p className="compose-error">{error}</p>}
            <footer className="compose-foot">
              <button className="btn ghost" onClick={() => setStage(WRITE)}>
                {t('composeBack')}
              </button>
              <button className="btn primary" disabled={busy || !transport?.ready}
                      onClick={confirm}>
                <SendIcon /> {busy ? t('composeSending') : verb}
              </button>
            </footer>
          </>
        )}

        {stage === DONE && outcome && (
          <div className="compose-done">
            <p className="compose-done-line">
              {outcome.delivered ? t('composeDelivered') : t('composeHandedOff', { arg: outcome.handoff })}
            </p>
            {outcome.refused?.length > 0 && (
              <p className="compose-error">{t('composeRefused', { arg: outcome.refused.join(', ') })}</p>
            )}
            {/* Only when there is something the headline does not already say.
                The drafts transport's detail is the same instruction as the
                translated line above, in English -- shown twice, once in the
                wrong language. A failed copy to Sent is different: the mail
                went, the copy did not, and the server's words are the
                diagnosis. */}
            {outcome.sent_copy === 'failed' && outcome.detail && (
              <p className="compose-note">{outcome.detail}</p>
            )}
            <footer className="compose-foot">
              <button className="btn primary" onClick={onClose}>{t('close')}</button>
            </footer>
          </div>
        )}
      </div>
    </div>
  );
}
