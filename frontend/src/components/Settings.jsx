import { useEffect, useState } from 'react';
import { api } from '../lib/api.js';

const THEMES = [
  { id: 'gold', label: 'Gold & Ivory', colors: ['#FAF6EC', '#F0E6C8', '#C9A227', '#3A2E1F'] },
  { id: 'dark', label: 'Dark', colors: ['#1C1B18', '#26241E', '#D4AF37', '#F1E8CC'] },
  { id: 'green', label: 'Green', colors: ['#F1F6EE', '#DCE8D3', '#4C7A3D', '#2B3D24'] },
  { id: 'purple', label: 'Purple', colors: ['#F5F0F8', '#E4D6EC', '#7E4F9E', '#3C2C4A'] },
];

export default function Settings({ settings, onSettings, theme, onTheme, source, onSourceChange, onSignOut }) {
  const [priorities, setPriorities] = useState([]);
  const [suggestions, setSuggestions] = useState(null);
  const [newTopic, setNewTopic] = useState('');
  const [test, setTest] = useState(null);
  const [testing, setTesting] = useState(false);
  const [copilot, setCopilot] = useState(null);
  const [draft, setDraft] = useState({});
  const [sourceInfo, setSourceInfo] = useState(null);

  useEffect(() => { api.sources().then(setSourceInfo).catch(() => {}); }, [settings]);

  useEffect(() => { api.priorities().then((r) => setPriorities(r.items)); }, []);
  useEffect(() => { setDraft({}); }, [settings]);

  const value = (key) => (draft[key] !== undefined ? draft[key] : settings?.[key] ?? '');
  const edit = (key, v) => setDraft((d) => ({ ...d, [key]: v }));

  async function save(keys) {
    const patch = {};
    for (const k of keys) if (draft[k] !== undefined) patch[k] = draft[k];
    if (Object.keys(patch).length) onSettings(await api.patchSettings(patch));
  }

  async function addTopic(topic) {
    if (!topic.trim()) return;
    const r = await api.addPriority({ topic: topic.trim(), status: 'active', weight: 10 });
    setPriorities(r.items);
    setNewTopic('');
    setSuggestions((s) => s?.filter((x) => x.topic !== topic));
  }

  async function setStatus(id, status) { setPriorities((await api.patchPriority(id, { status })).items); }
  async function remove(id) { setPriorities((await api.deletePriority(id)).items); }

  async function runTest() {
    setTesting(true);
    try { setTest(await api.testProvider()); } finally { setTesting(false); }
  }

  const provider = value('llm_provider');

  return (
    <div className="scroll">
      <div className="settings">

        <section>
          <h3>Mail source</h3>
          <p className="hint">
            Where your mail is read from. <strong>Apple Mail</strong> reads the messages already
            on this Mac &mdash; no sign-in, no app registration, nothing leaves the machine, and
            no school IT policy can block it. <strong>Outlook</strong> talks to Microsoft
            directly, which is fresher but needs an Entra app registration you create yourself.
          </p>

          <div className="field">
            <select className="input" value={value('mail_source')}
                    onChange={async (e) => {
                      onSettings(await api.patchSettings({ mail_source: e.target.value }));
                      onSourceChange?.();
                    }}>
              <option value="applemail">Apple Mail on this Mac</option>
              <option value="graph">Outlook (Microsoft account)</option>
            </select>
          </div>

          {sourceInfo && (
            <div style={{ fontSize: 12, color: 'var(--muted)', marginBottom: 12, lineHeight: 1.6 }}>
              {Object.entries(sourceInfo.statuses).map(([name, st]) => (
                <div key={name}>
                  <strong style={{ color: st.ready ? 'var(--accent-strong)' : 'var(--muted)' }}>
                    {name === 'applemail' ? 'Apple Mail' : 'Outlook'}:
                  </strong>{' '}
                  {st.ready ? `ready${st.account ? ` — ${st.account}` : ''}` : st.detail}
                </div>
              ))}
            </div>
          )}

          <div className="field">
            <label>Your email address</label>
            <input className="input" type="email" placeholder="you@outlook.com"
                   value={value('user_address')} onChange={(e) => edit('user_address', e.target.value)} />
            <p className="hint" style={{ marginTop: 4 }}>
              Used to tell mail addressed <em>to</em> you from mail you&rsquo;re only copied on &mdash;
              the single strongest ranking signal there is. Outlook fills this in at sign-in.
            </p>
          </div>

          {value('mail_source') === 'applemail' && (
            <>
              <div className="field">
                <label>Mail store path (leave blank to auto-detect)</label>
                <input className="input" placeholder="~/Library/Mail/V10"
                       value={value('applemail_root')}
                       onChange={(e) => edit('applemail_root', e.target.value)} />
              </div>
              <div className="field">
                <label>
                  <input type="checkbox" style={{ marginRight: 6 }}
                         checked={value('applemail_inbox_only') !== 'false'}
                         onChange={(e) => edit('applemail_inbox_only', e.target.checked ? 'true' : 'false')} />
                  Inbox only (Trash, Junk, Sent and Drafts are always excluded)
                </label>
              </div>
            </>
          )}

          <div style={{ display: 'flex', gap: 8 }}>
            <button className="btn primary"
                    onClick={() => save(['user_address', 'applemail_root', 'applemail_inbox_only'])}
                    disabled={!Object.keys(draft).length}>Save</button>
            {source?.account && value('mail_source') === 'graph' && (
              <button className="btn" onClick={onSignOut}>Sign out of Outlook</button>
            )}
          </div>
        </section>

        {value('mail_source') === 'graph' && (
        <section>
          <h3>Microsoft app registration</h3>
          <p className="hint">
            The Application (client) ID from your own Entra app registration. One-time setup &mdash;
            the walkthrough is in <code>docs/ENTRA_SETUP.md</code>. There is no client secret:
            desktop apps use PKCE instead.
          </p>
          <div className="field">
            <input className="input" placeholder="00000000-0000-0000-0000-000000000000"
                   value={value('entra_client_id')} onChange={(e) => edit('entra_client_id', e.target.value)} />
          </div>
          <button className="btn primary" onClick={() => save(['entra_client_id'])}
                  disabled={draft.entra_client_id === undefined}>Save client ID</button>
        </section>
        )}

        <section>
          <h3>Theme</h3>
          <p className="hint">Gold &amp; Ivory is the default, drawn from the app icon.</p>
          <div className="theme-grid">
            {THEMES.map((t) => (
              <button key={t.id} className={`theme-swatch ${theme === t.id ? 'active' : ''}`}
                      onClick={() => onTheme(t.id)}>
                <div className="bar">{t.colors.map((c) => <span key={c} style={{ background: c }} />)}</div>
                {t.label}
              </button>
            ))}
          </div>
        </section>

        <section>
          <h3>AI provider</h3>
          <p className="hint">
            Copilot signs in with GitHub&rsquo;s device flow &mdash; no API key to paste, and the token
            lives in your keychain, not in this app. It bills in <em>premium requests</em>, so
            Fool&rsquo;s Gold batches emails and caches every result.
          </p>
          <div className="field">
            <label>Provider</label>
            <select className="input" value={provider} onChange={(e) => edit('llm_provider', e.target.value)}>
              <option value="copilot">GitHub Copilot (SDK)</option>
              <option value="anthropic">Anthropic (Claude)</option>
              <option value="openai">OpenAI</option>
              <option value="none">None — structural signals only</option>
            </select>
          </div>

          {provider === 'copilot' && (
            <div className="field">
              <label>Model</label>
              <input className="input" value={value('copilot_model')}
                     onChange={(e) => edit('copilot_model', e.target.value)} placeholder="auto" />
              <p className="hint" style={{ marginTop: 6 }}>
                {copilot
                  ? (copilot.signed_in
                      ? `Copilot signed in${copilot.detail ? ` — ${copilot.detail}` : ''}`
                      : `Not signed in: ${copilot.detail || 'run `copilot` once in a terminal to authorise.'}`)
                  : <button className="btn ghost" style={{ padding: '2px 6px' }}
                            onClick={async () => setCopilot(await api.copilotStatus())}>Check Copilot sign-in</button>}
              </p>
            </div>
          )}

          {provider === 'anthropic' && (
            <>
              <div className="field">
                <label>API key {settings?.anthropic_api_key_set && <span style={{ color: 'var(--muted)' }}>(saved)</span>}</label>
                <input className="input" type="password" placeholder="sk-ant-…"
                       value={draft.anthropic_api_key ?? ''} onChange={(e) => edit('anthropic_api_key', e.target.value)} />
              </div>
              <div className="field">
                <label>Model</label>
                <input className="input" value={value('anthropic_model')}
                       onChange={(e) => edit('anthropic_model', e.target.value)} />
              </div>
            </>
          )}

          {provider === 'openai' && (
            <>
              <div className="field">
                <label>API key {settings?.openai_api_key_set && <span style={{ color: 'var(--muted)' }}>(saved)</span>}</label>
                <input className="input" type="password" placeholder="sk-…"
                       value={draft.openai_api_key ?? ''} onChange={(e) => edit('openai_api_key', e.target.value)} />
              </div>
              <div className="field">
                <label>Model</label>
                <input className="input" value={value('openai_model')}
                       onChange={(e) => edit('openai_model', e.target.value)} />
              </div>
            </>
          )}

          <div style={{ display: 'flex', gap: 8, alignItems: 'center' }}>
            <button className="btn primary" onClick={() => save([
              'llm_provider', 'copilot_model', 'anthropic_api_key', 'anthropic_model',
              'openai_api_key', 'openai_model',
            ])} disabled={!Object.keys(draft).length}>Save</button>
            <button className="btn" onClick={runTest} disabled={testing}>
              {testing ? <><span className="spin" /> Testing…</> : 'Test connection'}
            </button>
            {test && (
              <span style={{ fontSize: 12, color: test.ok ? 'var(--accent-strong)' : '#B4483C' }}>
                {test.ok ? `Working (${test.provider})` : test.detail}
              </span>
            )}
          </div>
        </section>

        <section>
          <h3>Active priorities</h3>
          <p className="hint">
            What you are working on <em>right now</em>. Mail touching these rises; everything is
            re-scored the moment you change this list, with no new AI calls. Demote a topic when a
            course ends rather than deleting it &mdash; that keeps its history out of your way.
          </p>

          <div style={{ display: 'flex', gap: 6, marginBottom: 12 }}>
            <input className="input" placeholder="e.g. CS3103 final project" value={newTopic}
                   onChange={(e) => setNewTopic(e.target.value)}
                   onKeyDown={(e) => e.key === 'Enter' && addTopic(newTopic)} />
            <button className="btn primary" onClick={() => addTopic(newTopic)}>Add</button>
          </div>

          {priorities.map((p) => (
            <div key={p.id} className={`prio-row ${p.status}`}>
              <span className="topic">{p.topic}</span>
              <select className="input" style={{ width: 130 }} value={p.status}
                      onChange={(e) => setStatus(p.id, e.target.value)}>
                <option value="active">Active</option>
                <option value="low_care">Low care</option>
                <option value="dismissed">Dismissed</option>
              </select>
              <button className="btn ghost" onClick={() => remove(p.id)} title="Remove">×</button>
            </div>
          ))}
          {!priorities.length && <p className="hint">No priorities yet — add one above, or see what turns up below.</p>}

          <div style={{ marginTop: 16 }}>
            <button className="btn" onClick={async () => setSuggestions((await api.suggestions()).items)}>
              Suggest topics from my recent mail
            </button>
            {suggestions && (
              <div style={{ marginTop: 10 }}>
                {suggestions.length === 0 && <p className="hint">Nothing recurring enough to suggest yet.</p>}
                {suggestions.map((s) => (
                  <div key={s.topic} className="prio-row">
                    <span className="topic">{s.topic}</span>
                    <span style={{ fontSize: 11.5, color: 'var(--muted)' }}>{s.kind} · {s.count}×</span>
                    <button className="btn" onClick={() => addTopic(s.topic)}>Promote</button>
                    <button className="btn ghost"
                            onClick={() => setSuggestions((x) => x.filter((y) => y.topic !== s.topic))}>Ignore</button>
                  </div>
                ))}
              </div>
            )}
          </div>
        </section>

        <section>
          <h3>Sync</h3>
          <div className="field">
            <label>Days of mail to keep</label>
            <input className="input" type="number" min="1" max="365" value={value('sync_days')}
                   onChange={(e) => edit('sync_days', e.target.value)} />
          </div>
          <div className="field">
            <label>Maximum messages per sync</label>
            <input className="input" type="number" min="10" max="2000" value={value('sync_max_messages')}
                   onChange={(e) => edit('sync_max_messages', e.target.value)} />
          </div>
          <div className="field">
            <label>Emails per AI request (higher = fewer premium requests)</label>
            <input className="input" type="number" min="1" max="25" value={value('classify_batch_size')}
                   onChange={(e) => edit('classify_batch_size', e.target.value)} />
          </div>
          <button className="btn primary" disabled={!Object.keys(draft).length}
                  onClick={() => save(['sync_days', 'sync_max_messages', 'classify_batch_size'])}>Save</button>
        </section>

      </div>
    </div>
  );
}
