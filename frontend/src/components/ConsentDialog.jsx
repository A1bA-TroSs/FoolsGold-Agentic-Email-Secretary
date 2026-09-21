import { useEffect, useMemo, useRef } from 'react';
import { useT } from '../lib/i18n.js';

/* Permission before any mail leaves this Mac for a cloud AI.

   Apple 5.1.2(i) asks for two things: say *where* personal data goes,
   "including with third-party AI", and get explicit permission first. Korea's
   PIPA asks for the same thing in more detail -- recipient, country, items,
   purpose, retention -- and adds two things people usually leave out: that the
   user may refuse, and what refusing costs. Every row below is one of those.

   Two design choices are deliberate:

   * Focus starts on "Don't allow". Enter must never be the way a mailbox gets
     shared; the yes takes a deliberate click.
   * Both answers look like answers. No greyed-out link for "no", no
     "Maybe later" that quietly re-asks forever. Declining is remembered for
     the session, and Settings says what state things are in.

   The backend enforces the gate (llm/registry.get_provider); this dialog is
   how a person answers it, not what makes it true. */
export default function ConsentDialog({ open, status, lang, busy, onAllow, onDecline }) {
  const t = useT();
  const declineRef = useRef(null);

  useEffect(() => {
    if (!open) return undefined;
    declineRef.current?.focus();
    const onKey = (e) => { if (e.key === 'Escape') onDecline(); };
    document.addEventListener('keydown', onKey);
    return () => document.removeEventListener('keydown', onKey);
  }, [open, onDecline]);

  // The backend sends an ISO code; the reader's language names the country.
  const country = useMemo(() => {
    if (!status?.country) return t('consentCountryUnknown');
    try {
      return new Intl.DisplayNames([lang || 'en'], { type: 'region' }).of(status.country);
    } catch {
      return status.country;
    }
  }, [status?.country, lang, t]);

  if (!open || !status) return null;
  const recipient = status.recipient;

  return (
    <div className="modal-scrim">
      <div className="modal consent" role="alertdialog" aria-modal="true"
           aria-labelledby="consent-title" aria-describedby="consent-intro">
        <h4 id="consent-title">{t('consentTitle', { recipient })}</h4>
        <p id="consent-intro">{t('consentIntro')}</p>

        <dl className="consent-terms">
          <dt>{t('consentRecipient')}</dt>
          <dd>{recipient}</dd>
          <dt>{t('consentWhere')}</dt>
          <dd>{country}</dd>
          <dt>{t('consentItems')}</dt>
          <dd>{t('consentItemsBody')}</dd>
          <dt>{t('consentPurpose')}</dt>
          <dd>{t('consentPurposeBody')}</dd>
          <dt>{t('consentRetention')}</dt>
          <dd>{t('consentRetentionBody', { recipient })}</dd>
          <dt>{t('consentRefuse')}</dt>
          <dd>{t('consentRefuseBody')}</dd>
        </dl>

        {status.policy_url && (
          <p className="consent-policy">
            <a href={status.policy_url} target="_blank" rel="noreferrer">
              {t('consentPolicy', { recipient })}
            </a>
          </p>
        )}

        <div className="actions">
          <button ref={declineRef} className="btn" onClick={onDecline} disabled={busy}>
            {t('consentDecline')}
          </button>
          <button className="btn primary" onClick={onAllow} disabled={busy}>
            {busy ? t('consentAllowing') : t('consentAllow')}
          </button>
        </div>
      </div>
    </div>
  );
}
