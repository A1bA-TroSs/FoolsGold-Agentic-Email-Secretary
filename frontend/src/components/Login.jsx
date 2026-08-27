import { useState } from 'react';
import { api, openExternal } from '../lib/api.js';

/* Two-step sign-in, matching how Outlook actually works:
   type the address -> we hand Microsoft the login_hint -> the real Microsoft
   consent page opens in the system browser -> it redirects back to our
   loopback listener. We poll for the result rather than trying to detect it. */
export default function Login({ onSignedIn, configured, picker = null }) {
  const [email, setEmail] = useState('');
  const [phase, setPhase] = useState('idle');     // idle | opening | waiting
  const [error, setError] = useState('');

  async function start(e) {
    e.preventDefault();
    setError('');
    setPhase('opening');
    try {
      const { auth_url } = await api.login(email.trim());
      openExternal(auth_url);
      setPhase('waiting');
      poll();
    } catch (err) {
      setError(err.message);
      setPhase('idle');
    }
  }

  function poll() {
    const started = Date.now();
    const timer = setInterval(async () => {
      if (Date.now() - started > 5 * 60 * 1000) {
        clearInterval(timer);
        setPhase('idle');
        setError('Sign-in timed out. Try again.');
        return;
      }
      try {
        const status = await api.authStatus();
        if (status.signed_in) {
          clearInterval(timer);
          onSignedIn(status);
        }
      } catch { /* backend momentarily busy; keep polling */ }
    }, 2000);
  }

  return (
    <div className="centered">
      <div className="card">
        <img src="./logo.png" alt="Fools Gold" />
        <h2>Fools Gold</h2>
        {picker}
        <p className="sub">
          Connect your Outlook mailbox. Fools Gold reads your mail and never writes to it &mdash;
          everything stays on this Mac.
        </p>

        {!configured && (
          <p className="sub" style={{ color: 'var(--accent-strong)' }}>
            First set your Microsoft client ID in Settings &mdash; see docs/ENTRA_SETUP.md.
          </p>
        )}

        <form onSubmit={start}>
          <div className="field">
            <label htmlFor="email">Outlook address</label>
            <input
              id="email" className="input" type="email" autoFocus
              placeholder="you@outlook.com" value={email}
              onChange={(e) => setEmail(e.target.value)}
              disabled={phase !== 'idle'}
            />
          </div>
          <button className="btn primary" style={{ width: '100%' }}
                  disabled={!configured || phase !== 'idle' || !email.trim()}>
            {phase === 'idle' && 'Sign in with Microsoft'}
            {phase === 'opening' && 'Opening Microsoft…'}
            {phase === 'waiting' && <><span className="spin" /> &nbsp;Waiting for sign-in…</>}
          </button>
        </form>

        {phase === 'waiting' && (
          <p className="sub" style={{ marginTop: 14, marginBottom: 0 }}>
            Finish signing in the browser window that just opened, then come back here.
          </p>
        )}
        {error && <p className="sub" style={{ marginTop: 14, marginBottom: 0, color: '#B4483C' }}>{error}</p>}
      </div>
    </div>
  );
}
