import { useState } from 'react';
import { api, canRelaunch, openFullDiskAccess, relaunchApp } from '../lib/api.js';
import { LANGUAGES, useT } from '../lib/i18n.js';
import Login from './Login.jsx';

/* The screen shown before any mail is available. Which of the two sources is
   selected decides what "getting connected" even means:
     - Apple Mail: nothing to sign into. Grant a macOS permission, tell us your
       address, done.
     - Outlook:    a Microsoft app registration and an OAuth round trip.

   This is the first screen a new user ever sees, so three things it used to
   get wrong are now rules:
   * It speaks the user's language, and lets them change it right here --
     Settings is not reachable until setup is done.
   * It speaks to a user, not a developer. The macOS permission is explained
     as what to click, with buttons that open the pane and restart the app;
     "the app you launched from -- Terminal if you ran npm start" is shown only
     to someone who is in fact running from a terminal.
   * It scrolls. The card is taller than a laptop window once a problem is
     shown, and the fix for the problem was the part cut off. */
export default function Connect({ source, settings, onSettings, onReady, onOpenSettings, lang, onLanguage }) {
  const t = useT();
  const [address, setAddress] = useState(settings?.user_address || '');
  const [saving, setSaving] = useState(false);
  const [checking, setChecking] = useState(false);
  const [error, setError] = useState('');
  const [stillBlocked, setStillBlocked] = useState(false);

  async function switchSource(name) {
    onSettings(await api.patchSettings({ mail_source: name }));
    onReady();
  }

  async function saveAndCheck() {
    setSaving(true);
    setError('');
    try {
      onSettings(await api.patchSettings({ user_address: address.trim() }));
      await recheck();
    } catch (e) {
      setError(e.message);
    } finally {
      setSaving(false);
    }
  }

  /* Re-reads the source and lets App re-render this screen from it. The old
     version copied the source's detail into an error line, so the same
     paragraph appeared twice -- once grey, once red. */
  async function recheck() {
    setChecking(true);
    setError('');
    setStillBlocked(false);
    try {
      const health = await api.health();
      if (health.source?.ready) onReady();
      else { setStillBlocked(true); onReady(); }
    } catch (e) {
      setError(e.message);
    } finally {
      setChecking(false);
    }
  }

  const languages = (
    <div className="setup-langs" role="group" aria-label={t('language')}>
      {LANGUAGES.map((l) => (
        <button key={l.id} type="button"
                className={`chip sm ${l.id === lang ? 'active' : ''}`}
                onClick={() => onLanguage(l.id)}>{l.native}</button>
      ))}
    </div>
  );

  const picker = (
    <div style={{ display: 'flex', gap: 6, justifyContent: 'center', marginBottom: 18 }}>
      <button className={`chip ${source.name === 'applemail' ? 'active' : ''}`}
              onClick={() => switchSource('applemail')}>Apple Mail</button>
      <button className={`chip ${source.name === 'graph' ? 'active' : ''}`}
              onClick={() => switchSource('graph')}>Outlook</button>
    </div>
  );

  // Outlook, already configured, just needs the OAuth round trip.
  if (source.name === 'graph' && source.needs_auth) {
    return <Login configured onSignedIn={onReady} picker={picker} languages={languages} />;
  }

  if (source.name === 'graph') {
    return (
      <div className="centered">
        <div className="card">
          {languages}
          <img className="brand-mark" src="./logo.png" alt="FoolsGold" />
          <h2>{t('setupOutlookTitle')}</h2>
          {picker}
          <p className="sub">{t('setupOutlookBody')}</p>
          {source.detail && (
            <p className="sub" style={{ color: 'var(--accent-strong)' }}>{source.detail}</p>
          )}
          <button className="btn primary" style={{ width: '100%' }} onClick={onOpenSettings}>
            {t('setupOpenSettings')}
          </button>
        </div>
      </div>
    );
  }

  // Apple Mail.
  const needsAccess = !source.ready && source.detail_key === 'fdaNeeded';
  const fromTerminal = source.detail_vars?.launcher === 'terminal';
  const otherProblem = !source.ready && !needsAccess && (source.detail_key || source.detail);

  return (
    <div className="centered">
      <div className="card" style={{ maxWidth: 440 }}>
        {languages}
        <img className="brand-mark" src="./logo.png" alt="FoolsGold" />
        <h2>{t('setupAppleTitle')}</h2>
        {picker}
        <p className="sub">{t('setupAppleBody')}</p>

        <ol className="setup-steps">
          <li>{t('setupStepMail')}</li>
          <li>{t('setupStepAccess')}</li>
          <li>{t('setupStepAddress')}</li>
        </ol>

        {needsAccess && (
          <div className="setup-callout" role="status">
            <b>{t('fdaTitle')}</b>
            <p>{fromTerminal ? t('fdaBodyTerminal') : t('fdaBody')}</p>
            {/* Until the app is signed with a Developer ID, macOS may list it twice:
                once by bundle (the gold icon, which is the one people turn on) and
                once by the executable's path (a plain black icon) -- and the path
                entry is the one it actually checks. Seen on the first DMG run. */}
            {!fromTerminal && <p className="setup-still">{t('fdaTwoEntries')}</p>}
            {stillBlocked && <p className="setup-still">{t('fdaStillBlocked')}</p>}
            {canRelaunch() && (
              <div className="setup-actions">
                <button className="btn" onClick={openFullDiskAccess}>{t('fdaOpenSettings')}</button>
                <button className="btn primary" onClick={relaunchApp}>{t('fdaRestart')}</button>
              </div>
            )}
          </div>
        )}
        {otherProblem && (
          <div className="setup-callout" role="status">
            <p>{source.detail_key ? t(source.detail_key, source.detail_vars || {}) : source.detail}</p>
          </div>
        )}

        <div className="field">
          <label htmlFor="addr">{t('setupAddressLabel')}</label>
          <input id="addr" className="input" type="email" placeholder="you@outlook.com"
                 value={address} onChange={(e) => setAddress(e.target.value)}
                 onKeyDown={(e) => e.key === 'Enter' && saveAndCheck()} />
        </div>

        <div style={{ display: 'flex', gap: 8 }}>
          <button className="btn primary" style={{ flex: 1 }}
                  onClick={saveAndCheck} disabled={saving || !address.trim()}>
            {saving ? <><span className="spin" /> {t('setupSaving')}</> : t('setupSave')}
          </button>
          <button className="btn" onClick={recheck} disabled={checking}>
            {checking ? <span className="spin" /> : t('setupRecheck')}
          </button>
        </div>

        {error && <p className="sub setup-error">{error}</p>}
      </div>
    </div>
  );
}
