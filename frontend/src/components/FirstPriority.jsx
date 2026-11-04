import { useEffect, useState } from 'react';
import { api } from '../lib/api.js';
import { useT } from '../lib/i18n.js';

/* The one-priority question (roadmap F4), for users who never saw the setup
   screen: Outlook, or Apple Mail with access already granted. Shown once, on
   the priority view, only while there are no priorities and it was never
   answered or skipped. Skippable (Danny, 2026-11-02). */
export default function FirstPriority({ settings, onSettings }) {
  const t = useT();
  const [show, setShow] = useState(false);
  const [topic, setTopic] = useState('');
  const [busy, setBusy] = useState(false);
  const asked = settings?.setup_priority_asked === '1';

  useEffect(() => {
    if (!settings || asked) { setShow(false); return; }
    let live = true;
    api.priorities().then((r) => {
      if (!live) return;
      if ((r.items || []).length) close();   // already has one: never ask
      else setShow(true);
    }).catch(() => {});
    return () => { live = false; };
  }, [settings, asked]); // eslint-disable-line react-hooks/exhaustive-deps

  async function close() {
    setShow(false);
    try { onSettings(await api.patchSettings({ setup_priority_asked: '1' })); } catch { /* shown again next time */ }
  }

  async function save() {
    if (!topic.trim()) return;
    setBusy(true);
    try {
      await api.addPriority({ topic: topic.trim(), status: 'active', weight: 10 });
      await close();
    } finally {
      setBusy(false);
    }
  }

  if (!show) return null;
  return (
    <div className="banner" role="region" aria-label={t('setupPriorityLabel')}
         style={{ display: 'block', margin: '8px 12px' }}>
      <label htmlFor="first-topic" style={{ display: 'block', fontWeight: 600, marginBottom: 6 }}>
        {t('setupPriorityLabel')}
      </label>
      <div style={{ display: 'flex', gap: 8 }}>
        <input id="first-topic" className="input" style={{ flex: 1 }} maxLength={80}
               placeholder={t('addPriorityPlaceholder')} value={topic}
               onChange={(e) => setTopic(e.target.value)}
               onKeyDown={(e) => e.key === 'Enter' && save()} />
        <button className="btn primary" onClick={save} disabled={busy || !topic.trim()}>{t('save')}</button>
        <button className="btn ghost" onClick={close}>{t('setupPrioritySkip')}</button>
      </div>
      <p className="hint" style={{ marginTop: 4 }}>{t('setupPriorityHelp')}</p>
    </div>
  );
}
