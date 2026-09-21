import { useState } from 'react';
import { api, openExternal } from '../lib/api.js';
import { useT } from '../lib/i18n.js';

/* Two-step sign-in, matching how Outlook actually works:
   type the address -> we hand Microsoft the login_hint -> the real Microsoft
   consent page opens in the system browser -> it redirects back to our
   loopback listener. We poll for the result rather than trying to detect it. */
export default function Login({ onSignedIn, configured, picker = null, languages = null }) {
  const t = useT();
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
        setError(t('loginTimedOut'));
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
        {languages}
        <img className="brand-mark" src="./logo.png" alt="FoolsGold" />
        <h2>FoolsGold</h2>
        {picker}
        <p className="sub">{t('loginBody')}</p>

        {!configured && (
          <p className="sub" style={{ color: 'var(--accent-strong)' }}>{t('loginNeedsClientId')}</p>
        )}

        <form onSubmit={start}>
          <div className="field">
            <label htmlFor="email">{t('loginAddressLabel')}</label>
            <input
              id="email" className="input" type="email" autoFocus
              placeholder="you@outlook.com" value={email}
              onChange={(e) => setEmail(e.target.value)}
              disabled={phase !== 'idle'}
            />
          </div>
          <button className="btn primary" style={{ width: '100%' }}
                  disabled={!configured || phase !== 'idle' || !email.trim()}>
            {phase === 'idle' && t('loginButton')}
            {phase === 'opening' && t('loginOpening')}
            {phase === 'waiting' && <><span className="spin" /> &nbsp;{t('loginWaiting')}</>}
          </button>
        </form>

        {phase === 'waiting' && (
          <p className="sub" style={{ marginTop: 14, marginBottom: 0 }}>{t('loginFinishInBrowser')}</p>
        )}
        {error && <p className="sub setup-error">{error}</p>}
      </div>
    </div>
  );
}
