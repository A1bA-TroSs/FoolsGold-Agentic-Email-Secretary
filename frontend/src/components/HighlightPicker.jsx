import { useEffect, useRef } from 'react';
import { useT } from '../lib/i18n.js';

/* Six colours, no picker.

   Enough to tell apart at a glance, each nameable in every UI language, and
   each a theme token rather than a hex value -- a colour chosen against the
   ivory theme would be unreadable on the dark one. */
export const HIGHLIGHT_COLORS = ['red', 'orange', 'yellow', 'green', 'blue', 'purple'];

export default function HighlightPicker({ current, onPick, onClear, onClose, label }) {
  const t = useT();
  const box = useRef(null);

  useEffect(() => {
    const onKey = (e) => { if (e.key === 'Escape') onClose(); };
    const onDown = (e) => { if (!box.current?.contains(e.target)) onClose(); };
    document.addEventListener('keydown', onKey);
    document.addEventListener('mousedown', onDown);
    return () => {
      document.removeEventListener('keydown', onKey);
      document.removeEventListener('mousedown', onDown);
    };
  }, [onClose]);

  return (
    <div className="hl-picker" ref={box} role="group"
         aria-label={label || t('highlightSender')}
         onClick={(e) => e.stopPropagation()}>
      {HIGHLIGHT_COLORS.map((c) => (
        <button
          key={c}
          /* `hl-${c}`, not `${c}`. `--hl` is a custom property, so it
             *inherits*: a swatch that never sets it does not fall back to
             nothing, it silently picks up whatever the nearest ancestor is
             using. Inside an un-highlighted row that meant six invisible
             circles; inside a green one it meant six green ones. */
          className={`hl-swatch hl-${c} ${current === c ? 'on' : ''}`}
          title={t(`color_${c}`)}
          aria-label={t(`color_${c}`)}
          aria-pressed={current === c}
          onClick={() => onPick(c)}
        />
      ))}
      {current && (
        <button className="hl-clear" onClick={onClear} title={t('highlightClear')}>
          {t('highlightClear')}
        </button>
      )}
    </div>
  );
}
