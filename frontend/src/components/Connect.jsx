import { useState } from 'react';
import { api } from '../lib/api.js';
import Login from './Login.jsx';

/* The screen shown before any mail is available. Which of the two sources is
   selected decides what "getting connected" even means:
     - Apple Mail: nothing to sign into. Grant a macOS permission, tell us your
       address, done.
     - Outlook:    a Microsoft app registration and an OAuth round trip. */
export default function Connect({ source, settings, onSettings, onReady, onOpenSettings }) {
  const [address, setAddress] = useState(settings?.user_address || '');
  const [saving, setSaving] = useState(false);
  const [checking, setChecking] = useState(false);
  const [error, setError] = useState('');

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

  async function recheck() {
    setChecking(true);
    setError('');
    try {
      const health = await api.health();
      if (health.source?.ready) onReady();
      else setError(health.source?.detail || 'Still not ready.');
    } catch (e) {
      setError(e.message);
    } finally {
      setChecking(false);
    }
  }

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
    return <Login configured onSignedIn={onReady} picker={picker} />;
  }

  if (source.name === 'graph') {
    return (
      <div className="centered">
        <div className="card">
          <img src="./logo.png" alt="Fools Gold" />
          <h2>Connect Outlook</h2>
          {picker}
          <p className="sub">
            Outlook needs a Microsoft app registration first &mdash; that&rsquo;s an Entra
            client ID you create once under your own account. If your university blocks
            that page, switch to <strong>Apple Mail</strong> above; it needs no registration
            at all.
          </p>
          <p className="sub" style={{ color: 'var(--accent-strong)' }}>{source.detail}</p>
          <button className="btn primary" style={{ width: '100%' }} onClick={onOpenSettings}>
            Open Settings
          </button>
        </div>
      </div>
    );
  }

  // Apple Mail.
  return (
    <div className="centered">
      <div className="card" style={{ maxWidth: 440 }}>
        <img src="./logo.png" alt="Fools Gold" />
        <h2>Read from Apple Mail</h2>
        {picker}
        <p className="sub">
          No sign-in, no app registration. Apple Mail has already downloaded your
          messages &mdash; Fools Gold reads them straight off this Mac, and nothing
          leaves it.
        </p>

        <ol style={{ textAlign: 'left', fontSize: 12.5, color: 'var(--muted)',
                     lineHeight: 1.65, paddingLeft: 18, margin: '0 0 18px' }}>
          <li>Add your Outlook account to <strong>Apple Mail</strong> and let it finish
              downloading. macOS handles the Microsoft login for you.</li>
          <li>Give Fools Gold <strong>Full Disk Access</strong> in System Settings →
              Privacy &amp; Security. macOS protects <code>~/Library/Mail</code>, so this is
              required. Restart the app afterwards.</li>
          <li>Type your own address below &mdash; it&rsquo;s how we tell mail addressed
              <em> to</em> you from mail you&rsquo;re merely copied on.</li>
        </ol>

        <div className="field">
          <label htmlFor="addr">Your email address</label>
          <input id="addr" className="input" type="email" placeholder="you@outlook.com"
                 value={address} onChange={(e) => setAddress(e.target.value)}
                 onKeyDown={(e) => e.key === 'Enter' && saveAndCheck()} />
        </div>

        <div style={{ display: 'flex', gap: 8 }}>
          <button className="btn primary" style={{ flex: 1 }}
                  onClick={saveAndCheck} disabled={saving || !address.trim()}>
            {saving ? <><span className="spin" /> Saving…</> : 'Save and continue'}
          </button>
          <button className="btn" onClick={recheck} disabled={checking}>
            {checking ? <span className="spin" /> : 'Re-check'}
          </button>
        </div>

        {source.detail && !source.ready && (
          <p className="sub" style={{ marginTop: 14, marginBottom: 0 }}>{source.detail}</p>
        )}
        {error && <p className="sub" style={{ marginTop: 10, marginBottom: 0, color: '#B4483C' }}>{error}</p>}
      </div>
    </div>
  );
}
