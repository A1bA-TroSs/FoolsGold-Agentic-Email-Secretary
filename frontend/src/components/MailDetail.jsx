import { useEffect, useState } from 'react';
import { api, openExternal } from '../lib/api.js';
import { ThinkingNote } from './Loading.jsx';
import { useT } from '../lib/i18n.js';

/* Note on the body frame.

   It is rendered with sandbox="" -- no scripts, no same-origin. That is
   deliberate: mail HTML is arbitrary untrusted markup from strangers, and it
   must not be able to run code, read our storage, or call our local API.

   The cost is that the parent cannot read the frame's contentDocument, so its
   height cannot be measured from its content. Rather than guess a height (which
   is how the reading area ended up as a short box), the frame fills the pane and
   long mail scrolls inside it.
*/

export default function MailDetail({ emailId }) {
  const [mail, setMail] = useState(null);
  const [error, setError] = useState('');
  const t = useT();

  useEffect(() => {
    if (!emailId) { setMail(null); return undefined; }
    let cancelled = false;
    setMail(null); setError('');
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
    <div className="detail-wrap">
      <div className="detail-head">
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

        {(mail.rationale || mail.matched?.length > 0) && (
          <div className="why">
            <strong>{t('whyRanking')}</strong> {mail.rationale}
            {mail.matched?.length > 0 && <> &middot; {t('matches')} <strong>{mail.matched.join(', ')}</strong></>}
            {mail.source === 'structural' && <> &middot; <em>{t('noAiUsed')}</em></>}
          </div>
        )}
      </div>

      <div className="detail-body">
        {mail.body_html ? (
          /* Sandboxed: remote mail HTML must never run script or reach the
             network from inside the app window. */
          <iframe
            title="email body"
            sandbox=""
            srcDoc={`<style>
                       html,body{margin:0;padding:6px 2px}
                       body{font:14px/1.55 -apple-system,Segoe UI,sans-serif;color:#222;
                            word-wrap:break-word;overflow-wrap:anywhere}
                       img{max-width:100%;height:auto}
                       table{max-width:100%}
                       a{color:#0b62c4}
                     </style>${mail.body_html}`}
          />
        ) : (
          <pre>{mail.body_text || mail.body_preview}</pre>
        )}
      </div>
    </div>
  );
}
