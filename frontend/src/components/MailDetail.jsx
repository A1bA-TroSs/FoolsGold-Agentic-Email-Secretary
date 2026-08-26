import { useEffect, useState } from 'react';
import { api, openExternal } from '../lib/api.js';

export default function MailDetail({ emailId }) {
  const [mail, setMail] = useState(null);
  const [error, setError] = useState('');

  useEffect(() => {
    if (!emailId) { setMail(null); return; }
    let cancelled = false;
    setMail(null); setError('');
    api.getMail(emailId)
      .then((m) => { if (!cancelled) setMail(m); })
      .catch((e) => { if (!cancelled) setError(e.message); });
    return () => { cancelled = true; };
  }, [emailId]);

  if (!emailId) return <div className="empty">Select an email to read it.</div>;
  if (error) return <div className="empty">{error}</div>;
  if (!mail) return <div className="empty"><span className="spin" /></div>;

  const names = (list) => (list || []).map((r) => r.name || r.address).join(', ');

  return (
    <div className="scroll">
      <div className="detail-head">
        <h2>{mail.subject}</h2>
        <div className="detail-meta">
          <span><strong>{mail.from_name || mail.from_address}</strong> &lt;{mail.from_address}&gt;</span>
          <span>{new Date(mail.received_at).toLocaleString()}</span>
        </div>
        <div className="detail-meta" style={{ marginTop: 4 }}>
          {mail.to_recipients?.length ? <span>To: {names(mail.to_recipients)}</span> : null}
          {mail.cc_recipients?.length ? <span>Cc: {names(mail.cc_recipients)}</span> : null}
        </div>
        <div style={{ marginTop: 10, display: 'flex', gap: 6, alignItems: 'center', flexWrap: 'wrap' }}>
          {mail.bucket && <span className={`tag ${mail.bucket}`}>{mail.bucket}</span>}
          {mail.deadline && <span className="tag due">due {mail.deadline}</span>}
          {mail.web_link && (
            <button className="btn ghost" style={{ marginLeft: 'auto' }}
                    onClick={() => openExternal(mail.web_link)}>
              Open in Outlook ↗
            </button>
          )}
        </div>

        {(mail.rationale || mail.matched?.length > 0) && (
          <div className="why">
            <strong>Why this ranking:</strong> {mail.rationale}
            {mail.matched?.length > 0 && <> &middot; matches <strong>{mail.matched.join(', ')}</strong></>}
            {mail.source === 'structural' && <> &middot; <em>no AI used</em></>}
          </div>
        )}
      </div>

      <div className="detail-body">
        {mail.body_html ? (
          /* Sandboxed iframe: remote mail HTML must never run script or reach
             the network from inside the app window. */
          <iframe
            title="email body"
            sandbox=""
            srcDoc={`<style>body{font:14px -apple-system,Segoe UI,sans-serif;color:#222;margin:0;padding:4px}
                     img{max-width:100%;height:auto}a{color:#0b62c4}</style>${mail.body_html}`}
            onLoad={(e) => {
              const doc = e.target.contentDocument;
              if (doc) e.target.style.height = `${doc.body.scrollHeight + 30}px`;
            }}
          />
        ) : (
          <pre>{mail.body_text || mail.body_preview}</pre>
        )}
      </div>
    </div>
  );
}
