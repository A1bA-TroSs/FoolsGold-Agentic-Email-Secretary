import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { api, openExternal, API_BASE } from '../lib/api.js';
import { prepareBody, frameDocument } from '../lib/mailBody.js';
import { ThinkingNote } from './Loading.jsx';
import { useT } from '../lib/i18n.js';

/* Note on the body frame.

   Sandboxed, with no `allow-scripts` and no `allow-same-origin`: mail HTML is
   arbitrary untrusted markup from strangers and must not run code, read our
   storage, or call our local API. Those two flags together are the one
   combination that must never appear, because a script inside a same-origin
   sandbox can simply remove the sandbox attribute from its own frame.

   `allow-popups allow-popups-to-escape-sandbox` IS set, and that is the fix
   for links. With a bare `sandbox=""` a click on `<a href>` does nothing at
   all -- no navigation, no popup, no event anywhere the app can see -- which
   is exactly what "the links do not work" looked like. The anchors are
   rewritten to `target="_blank"`, the popup attempt reaches Electron's
   window-open handler, and that handler checks the scheme before asking macOS
   to open anything. See lib/mailBody.js.

   The cost is that the parent cannot read the frame's contentDocument, so its
   height cannot be measured from its content. Rather than guess a height (which
   is how the reading area ended up as a short box), the frame fills the pane and
   long mail scrolls inside it.
*/

/* How much of the reading pane the header keeps.

   The header holds the subject, the recipient list and the reason line, and on
   a departmental mailing list that recipient list is forty addresses. It was
   capped at 46% of the pane, which is a guess that is wrong in both directions:
   too small when you want to read why something ranked, too large when you
   want the mail. So it is the user's to set, the same way the list/detail
   divider already is.

   `null` means "as tall as its content, up to the cap" -- the old behaviour,
   and still the default. A number means the user has an opinion. Double-click
   gives the opinion back. Stored per window in localStorage rather than in
   settings, because it describes this screen, not the account. */
const HEAD_MIN = 70;
const HEAD_MAX_RATIO = 0.8;     // always leave some mail on screen
const HEAD_KEY = 'foolsgold.detailHeadHeight';

function readHeadHeight() {
  try {
    const raw = Number(localStorage.getItem(HEAD_KEY));
    return Number.isFinite(raw) && raw >= HEAD_MIN ? raw : null;
  } catch {
    return null;              // private window, or storage disabled
  }
}

function saveHeadHeight(height) {
  try {
    if (height === null) localStorage.removeItem(HEAD_KEY);
    else localStorage.setItem(HEAD_KEY, String(Math.round(height)));
  } catch { /* not fatal */ }
}

export default function MailDetail({ emailId }) {
  const [mail, setMail] = useState(null);
  /* Per message, and reset when the message changes: agreeing to load one
     sender's images is not agreeing to load the next one's. */
  const [showRemote, setShowRemote] = useState(false);
  const [error, setError] = useState('');
  const [headHeight, setHeadHeight] = useState(readHeadHeight);
  const wrapRef = useRef(null);
  const headRef = useRef(null);
  const t = useT();

  /* Clamped against the pane, not against a constant: the maximum depends on
     how tall the window is, and a stored 400px is wrong the moment the window
     is 300px tall. */
  const clampHead = useCallback((px) => {
    const pane = wrapRef.current?.getBoundingClientRect().height || 0;
    const max = pane ? Math.max(HEAD_MIN, pane * HEAD_MAX_RATIO) : px;
    return Math.min(max, Math.max(HEAD_MIN, Math.round(px)));
  }, []);

  const beginHeadResize = useCallback((event) => {
    if (event.button !== undefined && event.button !== 0) return;
    event.preventDefault();
    const top = headRef.current?.getBoundingClientRect().top ?? 0;
    let latest = null;
    const move = (ev) => {
      latest = clampHead(ev.clientY - top);
      setHeadHeight(latest);
    };
    const stop = () => {
      window.removeEventListener('pointermove', move);
      window.removeEventListener('pointerup', stop);
      window.removeEventListener('pointercancel', stop);
      document.body.classList.remove('resizing-v');
      if (latest !== null) saveHeadHeight(latest);
    };
    // The body frame is sandboxed and swallows pointer events, so without
    // `resizing-v` disabling them the drag dies the moment the cursor crosses
    // into the mail -- which is most of the travel.
    document.body.classList.add('resizing-v');
    window.addEventListener('pointermove', move);
    window.addEventListener('pointerup', stop);
    window.addEventListener('pointercancel', stop);
  }, [clampHead]);

  const nudgeHeadResize = useCallback((event) => {
    const step = event.shiftKey ? 40 : 12;
    const delta = event.key === 'ArrowUp' ? -step : event.key === 'ArrowDown' ? step : 0;
    if (!delta) return;
    event.preventDefault();
    const current = headHeight ?? headRef.current?.getBoundingClientRect().height ?? HEAD_MIN;
    const next = clampHead(current + delta);
    setHeadHeight(next);
    saveHeadHeight(next);
  }, [clampHead, headHeight]);

  const resetHeadResize = useCallback(() => {
    setHeadHeight(null);
    saveHeadHeight(null);
  }, []);

  /* The ranking reason, in the reader's language.

     `rationale` carries one of two shapes. The structural path sends an array
     of `{key, vars}` -- codes, so the sentence is built here; the model path
     sends prose it already wrote in the user's language. Trying to parse is
     what tells them apart, and anything unparsable is shown as-is, which is
     also what makes a row classified before this change render its old English
     sentence instead of nothing. */
  const reason = useMemo(() => {
    const raw = mail?.rationale;
    if (!raw) return '';
    try {
      const codes = JSON.parse(raw);
      if (Array.isArray(codes) && codes.length && codes.every((c) => c && c.key)) {
        return codes.map((c) => t(c.key, c.vars)).join(', ');
      }
    } catch { /* prose, not codes */ }
    return String(raw);
  }, [mail?.rationale, t]);

  /* Parsed once per render: this walks every node in
     the document, and the reading pane re-renders on every list interaction
     behind it. */
  const prepared = useMemo(
    () => (mail?.body_html
      ? prepareBody(mail.body_html, { emailId, apiBase: API_BASE, showRemote })
      : { html: '', remoteBlocked: 0 }),
    [mail?.body_html, emailId, showRemote],
  );

  useEffect(() => {
    if (!emailId) { setMail(null); return undefined; }
    let cancelled = false;
    setMail(null); setError(''); setShowRemote(false);
    api.getMail(emailId)
      .then((m) => { if (!cancelled) setMail(m); })
      .catch((e) => { if (!cancelled) setError(e.message); });
    return () => { cancelled = true; };
  }, [emailId]);

  if (!emailId) return <div className="empty">{t('selectEmail')}</div>;
  if (error) return <div className="empty">{error}</div>;
  if (!mail) return <div className="empty"><ThinkingNote>{t('opening')}</ThinkingNote></div>;

  const names = (list) => (list || []).map((r) => r.name || r.address).join(', ');

  return (
    <div className="detail-wrap" ref={wrapRef}>
      <div className="detail-head" ref={headRef}
           style={headHeight === null ? undefined : { height: `${headHeight}px`, maxHeight: 'none' }}>
        <h2>{mail.subject}</h2>
        <div className="detail-meta">
          <span><strong>{mail.from_name || mail.from_address}</strong> &lt;{mail.from_address}&gt;</span>
          <span>{new Date(mail.received_at).toLocaleString()}</span>
        </div>
        <div className="detail-meta" style={{ marginTop: 4 }}>
          {mail.to_recipients?.length ? <span>{t('toLabel')} {names(mail.to_recipients)}</span> : null}
          {mail.cc_recipients?.length ? <span>{t('ccLabel')} {names(mail.cc_recipients)}</span> : null}
        </div>
        <div style={{ marginTop: 10, display: 'flex', gap: 6, alignItems: 'center', flexWrap: 'wrap' }}>
          {mail.bucket && <span className={`tag ${mail.bucket}`}>{t(`tag${mail.bucket[0].toUpperCase()}${mail.bucket.slice(1)}`)}</span>}
          {mail.deadline && <span className="tag due">{t('dueOn', { date: mail.deadline })}</span>}
          {mail.is_flagged ? <span className="tag due">{t('tagStarred')}</span> : null}
          {mail.is_answered ? <span className="tag noise">{t('tagReplied')}</span> : null}
          {mail.web_link && (
            <button className="btn ghost" style={{ marginLeft: 'auto' }}
                    onClick={() => openExternal(mail.web_link)}>
              {t('openInOutlook')} ↗
            </button>
          )}
        </div>

        {(reason || mail.matched?.length > 0) && (
          /* No "no AI used" marker any more. It said the same thing on every
             row of a mailbox ranked without a model -- which is every row, for
             anyone who has not turned one on -- while the banner above the
             pane and the Settings screen both already say it once. A status
             repeated on every item is not information, it is wallpaper. */
          <div className="why">
            <strong>{t('whyRanking')}</strong> {reason}
            {mail.matched?.length > 0 && <> &middot; {t('matches')} <strong>{mail.matched.join(', ')}</strong></>}
          </div>
        )}
      </div>

      {/* The boundary between what the app says about this mail and the mail
          itself. Same affordances as the list divider: drag, arrow keys,
          double-click to hand the decision back to the layout. */}
      <div
        className="detail-resizer"
        role="separator"
        aria-orientation="horizontal"
        aria-label={t('resizeReadingHint')}
        tabIndex={0}
        title={t('resizeReadingHint')}
        onPointerDown={beginHeadResize}
        onKeyDown={nudgeHeadResize}
        onDoubleClick={resetHeadResize}
      ><span /></div>

      <div className="detail-body">
        {mail.body_html ? (
          <>
            {prepared.remoteBlocked > 0 && !showRemote && (
              /* Named, counted, and one click from being shown. A remote image
                 in mail tells the sender you opened it, roughly when and
                 roughly where -- so it is blocked by default, and saying so is
                 what makes that a choice rather than a silent failure. */
              <div className="remote-bar">
                <span className="dot" />
                {t('remoteBlocked', { arg: String(prepared.remoteBlocked) })}
                <button className="btn ghost" style={{ marginLeft: 'auto', padding: '1px 8px' }}
                        onClick={() => setShowRemote(true)}>{t('remoteShow')}</button>
              </div>
            )}
            <iframe
              title="email body"
              sandbox="allow-popups allow-popups-to-escape-sandbox"
              srcDoc={frameDocument(prepared.html, { apiBase: API_BASE, showRemote })}
            />
          </>
        ) : (
          <pre>{mail.body_text || mail.body_preview}</pre>
        )}
      </div>
    </div>
  );
}
