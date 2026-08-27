import { useEffect, useState } from 'react';
import { api } from '../lib/api.js';
import { LANGUAGES, useT } from '../lib/i18n.js';

const SOURCE_KEYS = ['user_address', 'applemail_root', 'applemail_inbox_only'];
const AI_KEYS = ['llm_provider', 'copilot_model', 'anthropic_api_key',
                 'anthropic_model', 'openai_api_key', 'openai_model'];
const SYNC_KEYS = ['sync_days', 'sync_max_messages', 'classify_batch_size'];
const NOTIFY_KEYS = ['notify_enabled', 'notify_morning', 'notify_evening'];

const THEMES = [
  { id: 'gold', key: 'themeGold', colors: ['#FAF6EC', '#F0E6C8', '#C9A227', '#3A2E1F'] },
  { id: 'dark', key: 'themeDark', colors: ['#1C1B18', '#26241E', '#D4AF37', '#F1E8CC'] },
  { id: 'green', key: 'themeGreen', colors: ['#F1F6EE', '#DCE8D3', '#4C7A3D', '#2B3D24'] },
  { id: 'purple', key: 'themePurple', colors: ['#F5F0F8', '#E4D6EC', '#7E4F9E', '#3C2C4A'] },
];

export default function Settings({
  settings, onSettings, theme, onTheme, source, onSourceChange, onSignOut, lang, onLanguage,
}) {
  const t = useT();
  const [priorities, setPriorities] = useState([]);
  const [suggestions, setSuggestions] = useState(null);
  const [newTopic, setNewTopic] = useState('');
  const [test, setTest] = useState(null);
  const [testing, setTesting] = useState(false);
  const [copilot, setCopilot] = useState(null);
  const [rescan, setRescan] = useState(null);
  const [rescanning, setRescanning] = useState(false);
  const [draft, setDraft] = useState({});
  const [sourceInfo, setSourceInfo] = useState(null);

  useEffect(() => { api.sources().then(setSourceInfo).catch(() => {}); }, [settings]);

  useEffect(() => { api.priorities().then((r) => setPriorities(r.items)); }, []);
  useEffect(() => { setDraft({}); }, [settings]);

  const value = (key) => (draft[key] !== undefined ? draft[key] : settings?.[key] ?? '');
  const edit = (key, v) => setDraft((d) => ({ ...d, [key]: v }));

  /* Every section shares one `draft`, so a Save button must ask whether *its
     own* fields are dirty. Testing the whole draft lit up all four Saves the
     moment you typed in any one field -- each still saved only its own keys, so
     nothing was lost, but the buttons were lying about what was pending. */
  const dirty = (keys) => keys.some((k) => draft[k] !== undefined);

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
          <h3>{t('mailSource')}</h3>
          <p className="hint">{t('mailSourceHelp')}</p>

          <div className="field">
            <select className="input" value={value('mail_source')}
                    onChange={async (e) => {
                      onSettings(await api.patchSettings({ mail_source: e.target.value }));
                      onSourceChange?.();
                    }}>
              <option value="applemail">{t('sourceAppleMail')}</option>
              <option value="graph">{t('sourceOutlook')}</option>
            </select>
          </div>

          {sourceInfo && (
            <div style={{ fontSize: 12, color: 'var(--muted)', marginBottom: 12, lineHeight: 1.6 }}>
              {Object.entries(sourceInfo.statuses).map(([name, st]) => (
                <div key={name}>
                  <strong style={{ color: st.ready ? 'var(--accent-strong)' : 'var(--muted)' }}>
                    {name === 'applemail' ? 'Apple Mail' : 'Outlook'}:
                  </strong>{' '}
                  {st.ready ? `${t('ready')}${st.account ? ` — ${st.account}` : ''}` : st.detail}
                </div>
              ))}
            </div>
          )}

          <div className="field">
            <label>{t('yourEmail')}</label>
            <input className="input" type="email" placeholder="you@outlook.com"
                   value={value('user_address')} onChange={(e) => edit('user_address', e.target.value)} />
            <p className="hint" style={{ marginTop: 4 }}>{t('yourEmailHelp')}</p>
          </div>

          {value('mail_source') === 'applemail' && (
            <>
              <div className="field">
                <label>{t('mailStorePath')}</label>
                <input className="input" placeholder="~/Library/Mail/V10"
                       value={value('applemail_root')}
                       onChange={(e) => edit('applemail_root', e.target.value)} />
              </div>
              <div className="field">
                <label>
                  <input type="checkbox" style={{ marginRight: 6 }}
                         checked={value('applemail_inbox_only') !== 'false'}
                         onChange={(e) => edit('applemail_inbox_only', e.target.checked ? 'true' : 'false')} />
                  {t('inboxOnly')}
                </label>
              </div>
            </>
          )}

          <div style={{ display: 'flex', gap: 8 }}>
            <button className="btn primary"
                    onClick={() => save(SOURCE_KEYS)}
                    disabled={!dirty(SOURCE_KEYS)}>{t('save')}</button>
            {source?.account && value('mail_source') === 'graph' && (
              <button className="btn" onClick={onSignOut}>{t('signOutOutlook')}</button>
            )}
          </div>
        </section>

        {value('mail_source') === 'graph' && (
        <section>
          <h3>{t('msRegistration')}</h3>
          <p className="hint">{t('msRegistrationHelp')}</p>
          <div className="field">
            <input className="input" placeholder="00000000-0000-0000-0000-000000000000"
                   value={value('entra_client_id')} onChange={(e) => edit('entra_client_id', e.target.value)} />
          </div>
          <button className="btn primary" onClick={() => save(['entra_client_id'])}
                  disabled={draft.entra_client_id === undefined}>{t('saveClientId')}</button>
        </section>
        )}

        <section>
          <h3>{t('language')}</h3>
          <p className="hint">{t('languageHelp')}</p>
          <div className="lang-grid">
            {LANGUAGES.map((l) => (
              <button key={l.id} className={`chip lang-chip ${lang === l.id ? 'active' : ''}`}
                      onClick={() => onLanguage(l.id)}>
                {l.native}
              </button>
            ))}
          </div>
        </section>

        <section>
          <h3>{t('theme')}</h3>
          <p className="hint">{t('themeHelp')}</p>
          <div className="theme-grid">
            {THEMES.map((th) => (
              /* `th`, not `t` -- `t` is the translator in this scope, and
                 shadowing it here is what emptied these swatches. */
              <button key={th.id} className={`theme-swatch ${theme === th.id ? 'active' : ''}`}
                      onClick={() => onTheme(th.id)}>
                <span className="bar">
                  {th.colors.map((c) => <span key={c} style={{ background: c }} />)}
                </span>
                <span className="theme-name">{t(th.key)}</span>
              </button>
            ))}
          </div>
        </section>

        <section>
          <h3>{t('aiProvider')}</h3>
          <p className="hint">{t('aiProviderHelp')}</p>
          <div className="field">
            <label>{t('provider')}</label>
            <select className="input" value={provider} onChange={(e) => edit('llm_provider', e.target.value)}>
              <option value="copilot">GitHub Copilot (SDK)</option>
              <option value="anthropic">Anthropic (Claude)</option>
              <option value="openai">OpenAI</option>
              <option value="none">{t('providerNone')}</option>
            </select>
          </div>

          {provider === 'copilot' && (
            <div className="field">
              <label>{t('model')}</label>
              <input className="input" value={value('copilot_model')}
                     onChange={(e) => edit('copilot_model', e.target.value)} placeholder="auto" />
              <p className="hint" style={{ marginTop: 6 }}>
                {copilot
                  ? (copilot.signed_in
                      ? `Copilot signed in${copilot.detail ? ` — ${copilot.detail}` : ''}`
                      : `Not signed in: ${copilot.detail || 'run `copilot` once in a terminal to authorise.'}`)
                  : <button className="btn ghost" style={{ padding: '2px 6px' }}
                            onClick={async () => setCopilot(await api.copilotStatus())}>{t('checkCopilot')}</button>}
              </p>
            </div>
          )}

          {provider === 'anthropic' && (
            <>
              <div className="field">
                <label>{t('apiKey')} {settings?.anthropic_api_key_set && <span style={{ color: 'var(--muted)' }}>{t('savedNote')}</span>}</label>
                <input className="input" type="password" placeholder="sk-ant-…"
                       value={draft.anthropic_api_key ?? ''} onChange={(e) => edit('anthropic_api_key', e.target.value)} />
              </div>
              <div className="field">
                <label>{t('model')}</label>
                <input className="input" value={value('anthropic_model')}
                       onChange={(e) => edit('anthropic_model', e.target.value)} />
              </div>
            </>
          )}

          {provider === 'openai' && (
            <>
              <div className="field">
                <label>{t('apiKey')} {settings?.openai_api_key_set && <span style={{ color: 'var(--muted)' }}>{t('savedNote')}</span>}</label>
                <input className="input" type="password" placeholder="sk-…"
                       value={draft.openai_api_key ?? ''} onChange={(e) => edit('openai_api_key', e.target.value)} />
              </div>
              <div className="field">
                <label>{t('model')}</label>
                <input className="input" value={value('openai_model')}
                       onChange={(e) => edit('openai_model', e.target.value)} />
              </div>
            </>
          )}

          <div style={{ display: 'flex', gap: 8, alignItems: 'center' }}>
            <button className="btn primary" onClick={() => save(AI_KEYS)}
                    disabled={!dirty(AI_KEYS)}>{t('save')}</button>
            <button className="btn" onClick={runTest} disabled={testing}>
              {testing ? <><span className="spin" /> {t('testing')}</> : t('testConnection')}
            </button>
            {test && (
              <span style={{ fontSize: 12, color: test.ok ? 'var(--accent-strong)' : '#B4483C' }}>
                {test.ok ? `${t('working')} (${test.provider})` : test.detail}
              </span>
            )}
          </div>
        </section>

        <section>
          <h3>{t('activePriorities')}</h3>
          <p className="hint">{t('activePrioritiesHelp')}</p>

          <div style={{ display: 'flex', gap: 6, marginBottom: 12 }}>
            <input className="input" placeholder={t('addPriorityPlaceholder')} value={newTopic}
                   onChange={(e) => setNewTopic(e.target.value)}
                   onKeyDown={(e) => e.key === 'Enter' && addTopic(newTopic)} />
            <button className="btn primary" onClick={() => addTopic(newTopic)}>{t('add')}</button>
          </div>

          {priorities.map((p) => (
            <div key={p.id} className={`prio-row ${p.status}`}>
              <span className="topic">{p.topic}</span>
              <select className="input" style={{ width: 130 }} value={p.status}
                      onChange={(e) => setStatus(p.id, e.target.value)}>
                <option value="active">{t('statusActive')}</option>
                <option value="low_care">{t('statusLowCare')}</option>
                <option value="dismissed">{t('statusDismissed')}</option>
              </select>
              <button className="btn ghost" onClick={() => remove(p.id)} title={t('remove')}>×</button>
            </div>
          ))}
          {!priorities.length && <p className="hint">{t('noPriorities')}</p>}

          <div style={{ marginTop: 16 }}>
            <button className="btn" onClick={async () => setSuggestions((await api.suggestions()).items)}>
              {t('suggestTopics')}
            </button>
            {suggestions && (
              <div style={{ marginTop: 10 }}>
                {suggestions.length === 0 && <p className="hint">{t('noSuggestions')}</p>}
                {suggestions.map((s) => (
                  <div key={s.topic} className="prio-row">
                    <span className="topic">{s.topic}</span>
                    <span style={{ fontSize: 11.5, color: 'var(--muted)' }}>{s.kind} · {s.count}×</span>
                    <button className="btn" onClick={() => addTopic(s.topic)}>{t('promote')}</button>
                    <button className="btn ghost"
                            onClick={() => setSuggestions((x) => x.filter((y) => y.topic !== s.topic))}>{t('ignore')}</button>
                  </div>
                ))}
              </div>
            )}
          </div>
        </section>

        <section>
          <h3>{t('rescanTitle')}</h3>
          <p className="hint">{t('rescanHelp')}</p>
          <button className="btn" disabled={rescanning}
                  onClick={async () => {
                    setRescanning(true); setRescan(null);
                    try {
                      const r = await api.rescan();
                      setRescan({ ok: true, detail: t('rescanDone', { n: r.classified }) });
                    } catch (e) {
                      setRescan({ ok: false, detail: e.message });
                    } finally {
                      setRescanning(false);
                    }
                  }}>
            {rescanning ? <><span className="spin" /> {t('rescanRunning')}</> : t('rescanAction')}
          </button>
          {rescan && (
            <p style={{ fontSize: 12, marginTop: 8,
                        color: rescan.ok ? 'var(--accent-strong)' : 'var(--danger)' }}>
              {rescan.detail}
            </p>
          )}
        </section>

        <section>
          <h3>{t('notifyTitle')}</h3>
          <p className="hint">{t('notifyHelp')}</p>
          <div className="field row">
            <label className="check">
              <input type="checkbox" checked={value('notify_enabled') === 'true'}
                     onChange={(e) => edit('notify_enabled', e.target.checked ? 'true' : 'false')} />
              {t('notifyEnabled')}
            </label>
          </div>
          <div className="field">
            <label>{t('notifyMorning')}</label>
            <input className="input" type="time" value={value('notify_morning')}
                   disabled={value('notify_enabled') !== 'true'}
                   onChange={(e) => edit('notify_morning', e.target.value)} />
          </div>
          <div className="field">
            <label>{t('notifyEvening')}</label>
            <input className="input" type="time" value={value('notify_evening')}
                   disabled={value('notify_enabled') !== 'true'}
                   onChange={(e) => edit('notify_evening', e.target.value)} />
          </div>
          <button className="btn primary" disabled={!dirty(NOTIFY_KEYS)}
                  onClick={() => save(NOTIFY_KEYS)}>{t('save')}</button>
        </section>

        <section>
          <h3>{t('sync')}</h3>
          <div className="field">
            <label>{t('daysToKeep')}</label>
            <input className="input" type="number" min="1" max="365" value={value('sync_days')}
                   onChange={(e) => edit('sync_days', e.target.value)} />
          </div>
          <div className="field">
            <label>{t('maxMessages')}</label>
            <input className="input" type="number" min="10" max="2000" value={value('sync_max_messages')}
                   onChange={(e) => edit('sync_max_messages', e.target.value)} />
          </div>
          <div className="field">
            <label>{t('batchSize')}</label>
            <input className="input" type="number" min="1" max="25" value={value('classify_batch_size')}
                   onChange={(e) => edit('classify_batch_size', e.target.value)} />
          </div>
          <button className="btn primary" disabled={!dirty(SYNC_KEYS)}
                  onClick={() => save(SYNC_KEYS)}>{t('save')}</button>
        </section>

      </div>
    </div>
  );
}
