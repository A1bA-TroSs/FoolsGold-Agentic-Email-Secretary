import { useEffect, useRef, useState } from 'react';
import { api, openExternal } from '../lib/api.js';
import { useT } from '../lib/i18n.js';

/* Download a local model without a terminal (roadmap F5).

   Shown in Settings under "Local (Ollama)". Nothing here downloads on its
   own: every pull starts from a click. A finished pull does not change the
   model in the box either -- the installed list refreshes and the user clicks
   the one they want, the same "shown, never chosen" rule as before.

   `models` is the /api/ollama/models answer (null while unknown), `typed` the
   name in the model box, `onDone` refreshes the installed list. */
const OLLAMA_DOWNLOAD = 'https://ollama.com/download';

export default function OllamaDownload({ models, typed, onDone }) {
  const t = useT();
  const [pull, setPull] = useState(null);
  const [startError, setStartError] = useState('');
  const timer = useRef(null);

  async function poll() {
    try {
      const s = await api.ollamaPullState();
      setPull(s);
      if (s.running) {
        timer.current = setTimeout(poll, 1000);
      } else if (s.done) {
        onDone?.();
      }
    } catch { /* the next click asks again */ }
  }

  // Once on mount: the offered sizes, and any pull already running (the
  // user may have left Settings and come back mid-download).
  useEffect(() => {
    poll();
    return () => clearTimeout(timer.current);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  async function start(model) {
    setStartError('');
    try {
      await api.ollamaPull(model);
      clearTimeout(timer.current);
      poll();
    } catch (err) {
      setStartError(err.message);
    }
  }

  // Unknown (the model list itself could not be read): offer nothing
  // rather than a download that may have nowhere to go.
  if (models === null || models.reachable == null) return null;

  if (models.reachable === false) {
    return (
      <div className="field">
        <p className="hint">{t('ollamaNotRunning')}</p>
        <button type="button" className="btn sm" onClick={() => openExternal(OLLAMA_DOWNLOAD)}>
          {t('ollamaGet')}
        </button>
      </div>
    );
  }

  const running = pull?.running;
  const installed = models.installed || [];
  const has = (name) => installed.some((m) => m === name || m === `${name}:latest`);
  const offered = (pull?.suggestions || []).filter((s) => !has(s.model));
  const typedName = (typed || '').trim();
  const typedOffered = typedName && !models.matches
    && !offered.some((s) => s.model === typedName) && !has(typedName);

  // The model in the box is installed and nothing is downloading: nothing
  // to offer.
  if (models.matches && !running && !pull?.error && !startError && !pull?.done) return null;
  if (!running && !offered.length && !typedOffered && !pull?.error && !startError && !pull?.done) return null;

  const pct = pull?.total ? Math.floor((pull.completed / pull.total) * 100) : null;
  const errorText = pull?.error
    ? ({ no_server: t('pullNoServer'), disk: t('pullNoDisk'), unknown_model: t('pullUnknownModel', { model: pull.model }) }[pull.error_kind]
       || t('pullFailed', { detail: pull.error }))
    : '';

  return (
    <div className="field">
      <label>{t('downloadModel')}</label>
      {running ? (
        <>
          <p className="hint">{t('pullRunning', { model: pull.model })}</p>
          <progress style={{ width: '100%' }} max={100} value={pct ?? undefined} />
          <p className="hint" style={{ marginTop: 6 }}>
            {pct !== null ? `${pct}% · ` : ''}{t('pullKeepUsing')}
          </p>
        </>
      ) : (
        <>
          <div className="lang-grid">
            {offered.map((s) => (
              <button key={s.model} type="button"
                      className={`chip lang-chip ${s.suggested ? 'active' : ''}`}
                      onClick={() => start(s.model)}>
                {t('downloadAbout', { model: s.model, size: s.size_gb })}
              </button>
            ))}
            {typedOffered && (
              <button type="button" className="chip lang-chip" onClick={() => start(typedName)}>
                {t('downloadTyped', { model: typedName })}
              </button>
            )}
          </div>
          {offered.length > 0 && (
            <p className="hint" style={{ marginTop: 6 }}>
              {offered.map((s) => t(s.suggested ? 'modelFitsSuggested' : s.fits ? 'modelFits' : 'modelTooBig',
                                     { model: s.model, mem: s.min_gb })).join(' ')}
            </p>
          )}
          {pull?.done && <p className="hint">{t('pullDone', { model: pull.model })}</p>}
          {(errorText || startError) && <p className="compose-error">{errorText || startError}</p>}
        </>
      )}
    </div>
  );
}
