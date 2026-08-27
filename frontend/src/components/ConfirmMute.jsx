import { useEffect, useRef } from 'react';
import { useT } from '../lib/i18n.js';
import { MuteIcon } from './Icons.jsx';

/* Muting is destructive and permanent-feeling, so it asks first.

   The dialog leads with the *sender*, not the message, and names how many
   emails it already covers -- because "mute" otherwise reads as "hide this
   one", and the user only discovers the real scope when a month of mail has
   quietly stopped being ranked. */
export default function ConfirmMute({ open, sender, address, count, onCancel, onConfirm, busy }) {
  const t = useT();
  const confirmRef = useRef(null);

  useEffect(() => {
    if (!open) return undefined;
    confirmRef.current?.focus();
    const onKey = (e) => { if (e.key === 'Escape') onCancel(); };
    document.addEventListener('keydown', onKey);
    return () => document.removeEventListener('keydown', onKey);
  }, [open, onCancel]);

  if (!open) return null;

  return (
    <div className="modal-scrim" onMouseDown={(e) => { if (e.target === e.currentTarget) onCancel(); }}>
      <div className="modal" role="alertdialog" aria-modal="true" aria-labelledby="mute-title">
        <h4 id="mute-title">{t('muteConfirmTitle')}</h4>
        <p>{t('muteConfirmBody')}</p>

        <div className="who">
          <MuteIcon />
          <div style={{ minWidth: 0 }}>
            <b>{sender || address}</b><br />
            <span>{address}</span>
          </div>
        </div>

        <p>
          {count > 0
            ? <>{t('muteAffects')} <span className="count">
                {t(count === 1 ? 'muteCountOne' : 'muteCount', { n: count })}
              </span></>
            : t('muteAffectsNone')}
        </p>
        <p>{t('muteReversible')}</p>

        <div className="actions">
          <button className="btn" onClick={onCancel} disabled={busy}>{t('cancel')}</button>
          <button ref={confirmRef} className="btn danger" onClick={onConfirm} disabled={busy}>
            {busy ? t('muting') : t('muteConfirmAction')}
          </button>
        </div>
      </div>
    </div>
  );
}
