import { useState } from 'react';
import { useT } from '../lib/i18n.js';
import HighlightPicker from './HighlightPicker.jsx';

/* Every sender you have coloured, and the way to change or clear it.

   It sits with the muted box because the two are the same kind of decision
   about the same kind of thing -- a correspondent, not a message -- pointing
   in opposite directions. Whatever you do here is instantly true in the
   mailbox and on the calendar, because all three read one table. */
export default function HighlightBox({ items, onHighlight }) {
  const t = useT();
  const [pickerFor, setPickerFor] = useState(null);

  return (
    <div className="hl-box">
      <h3>{t('highlightTitle')}</h3>
      <p className="hint">{t('highlightHelp')}</p>

      {!items.length ? (
        <p className="empty-line">{t('highlightEmpty')}</p>
      ) : (
        <ul className="hl-list">
          {items.map((s) => (
            <li key={s.address} className={`hl-${s.color}`}>
              <span className="dot" />
              <span className="who">
                <b>{s.display_name || s.address}</b>
                <span>
                  {s.address}
                  {s.message_count ? ` · ${t(s.message_count === 1 ? 'muteCountOne' : 'muteCount', { n: s.message_count })}` : ''}
                </span>
              </span>
              <button className={`icon-btn on hl-${s.color}`}
                      onClick={() => setPickerFor(pickerFor === s.address ? null : s.address)}
                      title={t('highlightChange')} aria-label={`${t('highlightChange')}: ${s.address}`}>
                <span className="dot" />
              </button>
              {pickerFor === s.address && (
                <HighlightPicker
                  current={s.color}
                  label={`${t('highlightSender')}: ${s.address}`}
                  onPick={(c) => { setPickerFor(null); onHighlight(s.address, c); }}
                  onClear={() => { setPickerFor(null); onHighlight(s.address, null); }}
                  onClose={() => setPickerFor(null)}
                />
              )}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
