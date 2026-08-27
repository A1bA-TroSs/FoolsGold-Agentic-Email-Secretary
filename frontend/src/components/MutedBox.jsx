import { useT } from '../lib/i18n.js';
import { UndoIcon } from './Icons.jsx';

/* The muted box. Muting silently and permanently is how a filter becomes a
   thing you distrust -- everything muted stays visible here, with a one-click
   way back. */
export default function MutedBox({ senders, onUnmute }) {
  const t = useT();

  return (
    <div>
      <div className="muted-head">
        <h4>{t('mutedTitle')}</h4>
        <p>{t('mutedHelp')}</p>
      </div>

      {senders.length === 0 ? (
        <div className="empty">{t('mutedEmpty')}</div>
      ) : (
        senders.map((sndr) => (
          <div className="muted-sender" key={sndr.address}>
            <div className="who">
              <b>{sndr.display_name || sndr.address}</b>
              <span>
                {sndr.address}
                {sndr.message_count ? ` · ${t('muteCount', { n: sndr.message_count })}` : ''}
              </span>
            </div>
            <button className="btn" onClick={() => onUnmute(sndr.address)}>
              <UndoIcon /> {t('unmute')}
            </button>
          </div>
        ))
      )}
    </div>
  );
}
