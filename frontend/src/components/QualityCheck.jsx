import { useCallback, useEffect, useState } from 'react';
import { api } from '../lib/api.js';
import { useT } from '../lib/i18n.js';

/* Tester quality check (roadmap A5), in Settings.

   Label about 120 of your own emails, see how Fools Gold did beside Apple
   Mail's Priority mark, and save a numbers-only score card to send back.

   Three rules from the old terminal scripts hold here too:
   - The app's own answer is never on screen while labelling (anchoring).
   - One key per question, and every email is saved as it is answered, so
     120 can be done in several sittings.
   - Numbers are shown per language, never only as one average. */

const STEPS = ['bucket', 'addressed', 'dated', 'date'];
const KEYS = {
  bucket: { a: 'action', f: 'fyi', n: 'noise' },
  addressed: { y: true, n: false },
  dated: { y: true, n: false },
};

function pct(v) {
  return v === null || v === undefined ? '–' : `${Math.round(v * 100)}%`;
}

export default function QualityCheck() {
  const t = useT();
  const [state, setState] = useState(null);     // {item, labelled, total}
  const [labelling, setLabelling] = useState(false);
  const [answer, setAnswer] = useState({});
  const [step, setStep] = useState('bucket');
  const [date, setDate] = useState('');
  const [score, setScore] = useState(null);
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    try { setState(await api.qualityNext()); } catch (e) { setError(e.message); }
  }, []);
  useEffect(() => { load(); }, [load]);

  async function start() {
    setBusy(true); setError('');
    try {
      await api.qualitySample();
      const s = await api.qualityNext();
      setState(s);
      setLabelling(Boolean(s.item));
      reset();
    } catch (e) { setError(e.message); } finally { setBusy(false); }
  }

  function reset() { setAnswer({}); setStep('bucket'); setDate(''); }

  const save = useCallback(async (a, deadline) => {
    if (!state?.item) return;
    setBusy(true); setError('');
    try {
      await api.qualityLabel({ email_id: state.item.email_id, bucket: a.bucket,
                               addressed: a.addressed, deadline: deadline || null });
      const s = await api.qualityNext();
      setState(s);
      reset();
      if (!s.item) { setLabelling(false); setScore(await api.qualityScore()); }
    } catch (e) { setError(e.message); } finally { setBusy(false); }
  }, [state]);

  const choose = useCallback((which, value) => {
    const a = { ...answer, [which]: value };
    setAnswer(a);
    if (which === 'bucket') setStep('addressed');
    else if (which === 'addressed') setStep('dated');
    else if (which === 'dated') {
      if (value) setStep('date');
      else save(a, null);
    }
  }, [answer, save]);

  // Single keys while labelling. Ignored while typing the date.
  useEffect(() => {
    if (!labelling || step === 'date' || busy) return undefined;
    function onKey(e) {
      if (e.metaKey || e.ctrlKey || e.altKey) return;
      const tag = (e.target?.tagName || '').toLowerCase();
      if (tag === 'input' || tag === 'textarea' || tag === 'select') return;
      const k = e.key.toLowerCase();
      if (k === 'escape') { setLabelling(false); return; }
      const map = KEYS[step];
      if (map && k in map) { e.preventDefault(); choose(step, map[k]); }
    }
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [labelling, step, busy, choose]);

  async function showScore() {
    setError('');
    try { setScore(await api.qualityScore()); } catch (e) { setError(e.message); }
  }

  async function exportCard() {
    setError('');
    try {
      const card = await api.qualityExport();
      const blob = new Blob([JSON.stringify(card, null, 2)], { type: 'application/json' });
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = `foolsgold-quality-${card.app_version}.json`;
      document.body.appendChild(a);
      a.click();
      a.remove();
      setTimeout(() => URL.revokeObjectURL(url), 1000);
    } catch (e) { setError(e.message); }
  }

  const total = state?.total || 0;
  const done = state?.labelled || 0;
  const item = state?.item;

  if (labelling && item) {
    const options = {
      bucket: [['a', 'action', t('qcActed')], ['f', 'fyi', t('qcFyi')], ['n', 'noise', t('qcNoise')]],
      addressed: [['y', true, t('qcYes')], ['n', false, t('qcNo')]],
      dated: [['y', true, t('qcYes')], ['n', false, t('qcNo')]],
    }[step];
    return (
      <div className="field">
        <p className="hint">{t('qcProgress', { done, total })} · {t('qcKeysHint')}</p>
        <progress style={{ width: '100%' }} max={total || 1} value={done} />
        <div style={{ border: '1px solid var(--border)', borderRadius: 8, padding: 12, margin: '10px 0',
                      maxHeight: 280, overflow: 'auto' }}>
          <strong>{item.subject || t('qcNoSubject')}</strong>
          <p className="hint" style={{ margin: '4px 0 8px' }}>
            {item.from_name} &lt;{item.from_address}&gt; · {(item.received_at || '').slice(0, 10)}
          </p>
          <div style={{ whiteSpace: 'pre-wrap', fontSize: 13 }}>{item.body}</div>
        </div>
        <label>{t({ bucket: 'qcQBucket', addressed: 'qcQAddressed', dated: 'qcQDated', date: 'qcQDate' }[step])}</label>
        {step === 'date' ? (
          <div style={{ display: 'flex', gap: 8 }}>
            <input className="input" type="date" value={date} autoFocus
                   onChange={(e) => setDate(e.target.value)}
                   onKeyDown={(e) => { if (e.key === 'Enter') save(answer, date); }} />
            <button className="btn primary" disabled={busy} onClick={() => save(answer, date)}>{t('qcSave')}</button>
          </div>
        ) : (
          <div className="lang-grid">
            {options.map(([key, value, label]) => (
              <button key={key} type="button" className="chip lang-chip" disabled={busy}
                      onClick={() => choose(step, value)}>
                <kbd>{key.toUpperCase()}</kbd> {label}
              </button>
            ))}
          </div>
        )}
        <div style={{ marginTop: 10 }}>
          <button className="btn ghost" onClick={() => setLabelling(false)}>{t('qcPause')}</button>
        </div>
        {error && <p className="compose-error">{error}</p>}
      </div>
    );
  }

  return (
    <div className="field">
      {total > 0 && <p className="hint">{t('qcProgress', { done, total })}</p>}
      <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap' }}>
        {item ? (
          <button className="btn primary" onClick={() => { reset(); setLabelling(true); }}>
            {done ? t('qcResume') : t('qcStart')}
          </button>
        ) : (
          <button className="btn primary" disabled={busy} onClick={start}>
            {total ? t('qcMore') : t('qcStart')}
          </button>
        )}
        {done > 0 && <button className="btn" onClick={showScore}>{t('qcShowScore')}</button>}
        {done > 0 && <button className="btn" onClick={exportCard}>{t('qcExport')}</button>}
      </div>
      {done > 0 && <p className="hint" style={{ marginTop: 6 }}>{t('qcExportHelp')}</p>}
      {score && (
        <>
          <table className="qc-table" style={{ width: '100%', fontSize: 13, marginTop: 10 }}>
            <thead>
              <tr>
                <th style={{ textAlign: 'left' }}>{t('qcLang')}</th>
                <th>{t('qcColN')}</th>
                <th>{t('qcColRecall')}</th>
                <th>{t('qcColPrecision')}</th>
                <th>{t('qcColFloor')}</th>
                <th>{t('qcColApple')}</th>
              </tr>
            </thead>
            <tbody>
              {Object.entries(score.languages).map(([lang, d]) => (
                <tr key={lang}>
                  <td>{t(`qcLang_${lang}`)}</td>
                  <td style={{ textAlign: 'center' }}>{d.n} ({d.acted})</td>
                  <td style={{ textAlign: 'center' }}>{pct(d.fg_act_recall)}</td>
                  <td style={{ textAlign: 'center' }}>{pct(d.fg_act_precision)}</td>
                  <td style={{ textAlign: 'center' }}>{pct(d.floor_act_recall)}</td>
                  <td style={{ textAlign: 'center' }}>{pct(d.apple_act_recall)}</td>
                </tr>
              ))}
            </tbody>
          </table>
          <p className="hint" style={{ marginTop: 6 }}>{t('qcScoreHelp')}</p>
          {!score.apple.measured && <p className="hint">{t('qcAppleNotMeasured')}</p>}
        </>
      )}
      {error && <p className="compose-error">{error}</p>}
    </div>
  );
}
