import { useCallback, useEffect, useState } from 'react';
import { api } from '../lib/api.js';
import { LANGUAGES, useT } from '../lib/i18n.js';
import './Compose.css';

const SOURCE_KEYS = ['user_address', 'applemail_root', 'applemail_inbox_only'];
const AI_KEYS = ['llm_provider', 'ollama_model', 'ollama_host', 'copilot_model', 'anthropic_api_key',
                 'anthropic_model', 'openai_api_key', 'openai_model', 'openai_base_url'];
const SYNC_KEYS = ['sync_days', 'sync_max_messages', 'classify_batch_size'];
const NOTIFY_KEYS = ['notify_enabled', 'notify_morning', 'notify_evening'];
/* Every key the Sending section can write. `smtp_password` is in here but is
   bound to the *draft* only, never to the saved value -- the server returns a
   saved secret as a mask, and a field initialised from it would save the mask
   as the password. The backend refuses the mask too; this is the first line. */
const SEND_KEYS = ['mail_transport', 'user_name',
                   'imap_host', 'imap_port', 'imap_security', 'imap_username', 'drafts_folder',
                   'smtp_host', 'smtp_port', 'smtp_security', 'smtp_username', 'sent_copy',
                   'smtp_password'];

const THEMES = [
  { id: 'gold', key: 'themeGold', colors: ['#FAF6EC', '#F0E6C8', '#C9A227', '#3A2E1F'] },
  { id: 'dark', key: 'themeDark', colors: ['#1C1B18', '#26241E', '#D4AF37', '#F1E8CC'] },
  { id: 'green', key: 'themeGreen', colors: ['#F1F6EE', '#DCE8D3', '#4C7A3D', '#2B3D24'] },
  { id: 'purple', key: 'themePurple', colors: ['#F5F0F8', '#E4D6EC', '#7E4F9E', '#3C2C4A'] },
];

/* Settings, in drawers rather than one scroll.

   Eleven sections had accumulated, and the shape of the screen said they were
   all equally likely to be wanted -- so finding the sync window meant scrolling
   past the AI provider, the priority list and the whole learning panel. The
   groups below are not a taxonomy of the code; they are a guess at what a
   person is holding in mind when they open this screen: "where does my mail
   come from", "how does it look", "how is it ranked", "what is doing the
   thinking".

   The search box is the escape hatch from a wrong guess, and it deliberately
   IGNORES the tabs: someone typing "ollama" wants the provider section
   wherever it lives, not "no results in View". A grouping you cannot search
   past is worse than no grouping, because it hides things confidently. */
const GROUPS = [
  { id: 'all',  key: 'grpAll' },
  { id: 'mail', key: 'grpMail' },
  { id: 'send', key: 'grpSend' },
  { id: 'view', key: 'grpView' },
  { id: 'rank', key: 'grpRank' },
  { id: 'ai',   key: 'grpAi' },
];

const SEARCHABLE = [
  'mailSource', 'mailSourceHelp', 'applemail', 'mailbox', 'folder',
  'msRegistration', 'language', 'theme', 'aiProvider', 'providerLocal',
  'ollamaModel', 'ollamaHost', 'activePriorities', 'rankTitle', 'rankHelp',
  'deadlineWindow', 'deadlineUrgent', 'rankVolume', 'catTitle',
  'rescoreTitle', 'rescoreHelp', 'rescanTitle', 'notifyTitle', 'sync',
  'daysToKeep', 'maxMessages', 'batchSize',
  'sendTitle', 'sendHelp', 'imapHost', 'draftsFolder', 'appPassword',
];

function Section({ group, titleKey, hintKey, terms = [], tab, q, t, children }) {
  const needle = (q || '').trim().toLowerCase();
  if (needle) {
    /* Searched against what is on SCREEN -- the translated strings -- not
       against the key names. A Korean user typing 동기화 has no reason to know
       the section is called `sync` in the source. */
    const hay = [titleKey, hintKey, ...terms]
      .filter(Boolean).map((k) => t(k)).join(' ').toLowerCase();
    if (!hay.includes(needle)) return null;
  } else if (tab !== 'all' && tab !== group) {
    return null;
  }
  return <section data-group={group} data-section={titleKey}>{children}</section>;
}

export default function Settings({
  settings, onSettings, theme, onTheme, source, onSourceChange, onSignOut, lang, onLanguage,
  consent, onReviewConsent, onWithdrawConsent,
}) {
  const t = useT();
  const [priorities, setPriorities] = useState([]);
  const [suggestions, setSuggestions] = useState(null);
  const [newTopic, setNewTopic] = useState('');
  const [test, setTest] = useState(null);
  // `null` means "not asked yet", which is different from "asked and there
  // are none" -- the second deserves a sentence, the first a button.
  const [models, setModels] = useState(null);
  const [testing, setTesting] = useState(false);
  const [copilot, setCopilot] = useState(null);
  const [rescan, setRescan] = useState(null);
  const [rescanning, setRescanning] = useState(false);
  const [draft, setDraft] = useState({});
  const [sourceInfo, setSourceInfo] = useState(null);
  const [ranking, setRanking] = useState(null);
  const [volume, setVolume] = useState('');
  const [rankMsg, setRankMsg] = useState(null);
  const [cats, setCats] = useState(null);
  const [tab, setTab] = useState('all');
  const [query, setQuery] = useState('');
  const [sendCheck, setSendCheck] = useState(null);
  const [checkingSend, setCheckingSend] = useState(false);
  const [sendAccounts, setSendAccounts] = useState(null);

  useEffect(() => {
    api.ranking().then((r) => {
      setRanking(r);
      setVolume(String(r.target_action_volume ?? 8));
    }).catch(() => {});
    api.rankCategories().then((r) => setCats(r.categories || [])).catch(() => {});
  }, []);

  /* One helper for all three buttons. Each reports its own outcome rather than
     refreshing silently: this panel's whole job is telling you what the ranker
     is doing, so an action that changes something and says nothing would be the
     one place in the app that keeps a secret. */
  async function rankAction(fn, message) {
    try {
      const result = await fn();
      setRanking(await api.ranking());
      setRankMsg({ ok: true, detail: typeof message === 'function' ? message(result) : message });
    } catch (e) {
      setRankMsg({ ok: false, detail: e.message });
    }
  }

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
  /* Asked for once, when the local provider is the one on screen. Not on every
     render: it talks to Ollama, and a settings screen must not poll a model
     server in the background. */
  const loadModels = useCallback(async () => {
    try { setModels(await api.ollamaModels()); } catch { setModels({ installed: [], matches: false }); }
  }, []);
  useEffect(() => {
    if (provider === 'ollama' && models === null) loadModels();
  }, [provider, models, loadModels]);

  const transport = value('mail_transport') || 'auto';
  const manualSend = transport === 'imap_draft' || transport === 'smtp';

  /* Automatic sending has nothing to configure, so what Settings shows for it
     is what it has learned: which addresses it can already send from. */
  useEffect(() => {
    if (transport !== 'auto' || sendAccounts !== null) return;
    api.transports()
      .then((r) => setSendAccounts(r?.statuses?.auto?.accounts || []))
      .catch(() => setSendAccounts([]));
  }, [transport, sendAccounts]);

  /* Save, THEN test. The test runs against saved settings on purpose -- a
     result about the half-typed form would be a result about a configuration
     that will never be used. Saving first makes "it works" true of the thing
     that will actually send. */
  async function checkSending() {
    setCheckingSend(true); setSendCheck(null);
    try {
      await save(SEND_KEYS);
      setDraft((d) => { const n = { ...d }; for (const k of SEND_KEYS) delete n[k]; return n; });
      setSendCheck(await api.testTransport());
    } catch (e) {
      setSendCheck({ ok: false, detail: e.message });
    } finally {
      setCheckingSend(false);
    }
  }

  const needle = query.trim().toLowerCase();

  return (
    <div className="scroll">
      <div className="settings">

        <div className="settings-bar">
          <input className="input settings-search" type="search"
                 placeholder={t('settingsSearch')} value={query}
                 onChange={(e) => setQuery(e.target.value)} />
          <div className="filters settings-tabs">
            {GROUPS.map((g) => (
              <button key={g.id}
                      className={`chip ${!needle && tab === g.id ? 'active' : ''}`}
                      /* Dimmed rather than hidden while searching: the tabs
                         going blank would read as the app breaking, and the
                         user needs them back the moment they clear the box. */
                      disabled={!!needle}
                      onClick={() => setTab(g.id)}>{t(g.key)}</button>
            ))}
          </div>
        </div>

        <Section group="mail" titleKey="mailSource" hintKey="mailSourceHelp" terms={["applemail", "mailbox", "folder"]}
                 tab={tab} q={query} t={t}>
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
              {/* `?? {}`, because `Object.entries(undefined)` throws and React
                  unmounts the tree -- the whole settings screen goes white
                  over one absent field. Same lesson as the `matched` string
                  that cost the entire window. The guard above checked the
                  object and not the field it then indexed. */}
              {Object.entries(sourceInfo.statuses ?? {}).map(([name, st]) => (
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
        </Section>

        <Section group="send" titleKey="sendTitle" hintKey="sendHelp"
                 terms={["imapHost", "draftsFolder", "appPassword"]}
                 tab={tab} q={query} t={t}>
          <h3>{t('sendTitle')}</h3>
          <p className="hint">{t('sendHelp')}</p>

          <div className="field">
            <label>{t('sendMethod')}</label>
            <select className="input" data-key="mail_transport" value={transport}
                    onChange={(e) => { edit('mail_transport', e.target.value); setSendCheck(null); }}>
              <option value="auto">{t('sendAuto')}</option>
              <option value="none">{t('sendNone')}</option>
              <option value="imap_draft">{t('sendDrafts')}</option>
              <option value="smtp">{t('sendSmtp')}</option>
            </select>
            <p className="hint" style={{ marginTop: 4 }}>
              {transport === 'auto' ? t('sendAutoHelp')
                : transport === 'imap_draft' ? t('sendDraftsHelp')
                : transport === 'smtp' ? t('sendSmtpHelp') : t('sendNoneHelp')}
            </p>
          </div>

          {transport === 'auto' && sendAccounts && sendAccounts.length > 0 && (
            <ul className="send-accounts" data-accounts={sendAccounts.length}>
              {sendAccounts.map((a) => (
                <li key={a.address}>
                  <span className="addr">{a.address}</span>
                  <span className="state">
                    {a.oauth_only ? t('sendAccountOauth')
                      : a.ready ? (a.last_mode === 'drafts' ? t('sendAccountDrafts') : t('sendAccountReady'))
                      : t('sendAccountAsk')}
                  </span>
                  {a.ready && (
                    <button className="btn ghost" data-action="forget-password"
                            onClick={async () => {
                              try {
                                const r = await api.forgetCredentials(a.address);
                                setSendAccounts(r.accounts || []);
                              } catch { /* list stays as it was */ }
                            }}>{t('sendForget')}</button>
                  )}
                </li>
              ))}
            </ul>
          )}

          {manualSend && (
            <>
              <div className="field">
                <label>{t('sendFrom')}</label>
                {/* The address lives in the mail-source section and is shown
                    here, not duplicated: two fields for one value is how they
                    come to disagree. */}
                <div className="send-from">
                  <input className="input" data-key="user_name" placeholder={t('sendNamePlaceholder')}
                         value={value('user_name')} onChange={(e) => edit('user_name', e.target.value)} />
                  <span className="send-from-address">
                    {value('user_address') ? `<${value('user_address')}>` : t('sendNoAddress')}
                  </span>
                </div>
              </div>

              <div className="send-grid">
                <div className="field">
                  <label>{t('imapHost')}</label>
                  <input className="input" data-key="imap_host" placeholder="imap.example.com"
                         spellCheck={false} autoCapitalize="off"
                         value={value('imap_host')} onChange={(e) => edit('imap_host', e.target.value.trim())} />
                </div>
                <div className="field">
                  <label>{t('port')}</label>
                  <input className="input" data-key="imap_port" inputMode="numeric"
                         value={value('imap_port')} onChange={(e) => edit('imap_port', e.target.value.replace(/\D/g, ''))} />
                </div>
                <div className="field">
                  <label>{t('security')}</label>
                  <select className="input" data-key="imap_security" value={value('imap_security') || 'ssl'}
                          onChange={(e) => {
                            const v = e.target.value;
                            edit('imap_security', v);
                            /* Only move the port if it is still the other
                               mode's default. A port the user typed is theirs. */
                            const port = value('imap_port');
                            if (v === 'ssl' && (!port || port === '143')) edit('imap_port', '993');
                            if (v !== 'ssl' && (!port || port === '993')) edit('imap_port', '143');
                          }}>
                    <option value="ssl">SSL/TLS</option>
                    <option value="starttls">STARTTLS</option>
                    <option value="plain">{t('securityNone')}</option>
                  </select>
                </div>
              </div>
              {value('imap_security') === 'plain' && (
                <p className="compose-error" style={{ margin: '0 0 12px' }}>{t('securityNoneWarn')}</p>
              )}

              <div className="field">
                <label>{t('username')}</label>
                <input className="input" data-key="imap_username" spellCheck={false} autoCapitalize="off"
                       placeholder={value('user_address') || 'you@example.com'}
                       value={value('imap_username')} onChange={(e) => edit('imap_username', e.target.value.trim())} />
              </div>
              <div className="field">
                <label>
                  {t('appPassword')}{' '}
                  {settings?.smtp_password_set && <span style={{ color: 'var(--muted)' }}>{t('savedNote')}</span>}
                </label>
                <input className="input" data-key="smtp_password" type="password" autoComplete="off"
                       placeholder={settings?.smtp_password_set ? '••••••••' : ''}
                       value={draft.smtp_password ?? ''} onChange={(e) => edit('smtp_password', e.target.value)} />
                <p className="hint" style={{ marginTop: 4 }}>{t('appPasswordHelp')}</p>
              </div>
              <div className="field">
                <label>{t('draftsFolder')}</label>
                <input className="input" data-key="drafts_folder" placeholder={t('draftsFolderAuto')}
                       value={value('drafts_folder')} onChange={(e) => edit('drafts_folder', e.target.value)} />
              </div>

              {transport === 'smtp' && (
                <div className="send-grid">
                  <div className="field">
                    <label>{t('smtpHost')}</label>
                    <input className="input" data-key="smtp_host" placeholder="smtp.example.com"
                           spellCheck={false} autoCapitalize="off"
                           value={value('smtp_host')} onChange={(e) => edit('smtp_host', e.target.value.trim())} />
                  </div>
                  <div className="field">
                    <label>{t('port')}</label>
                    <input className="input" data-key="smtp_port" inputMode="numeric"
                           value={value('smtp_port')} onChange={(e) => edit('smtp_port', e.target.value.replace(/\D/g, ''))} />
                  </div>
                  <div className="field">
                    <label>{t('security')}</label>
                    <select className="input" data-key="smtp_security" value={value('smtp_security') || 'starttls'}
                            onChange={(e) => edit('smtp_security', e.target.value)}>
                      <option value="starttls">STARTTLS</option>
                      <option value="ssl">SSL/TLS</option>
                    </select>
                  </div>
                </div>
              )}
            </>
          )}

          <div style={{ display: 'flex', gap: 8, alignItems: 'center', flexWrap: 'wrap' }}>
            <button className="btn primary" data-action="save-send"
                    onClick={async () => { await save(SEND_KEYS); setSendCheck(null); }}
                    disabled={!dirty(SEND_KEYS)}>{t('save')}</button>
            {manualSend && (
              <button className="btn" data-action="test-send" onClick={checkSending} disabled={checkingSend}>
                {checkingSend ? t('testing') : t('sendTest')}
              </button>
            )}
          </div>
          {sendCheck && (
            /* Success is built here from the folder the server found, in the
               user's language. The server's `detail` is an English sentence
               and it reached a Korean screen verbatim. On failure it is still
               shown -- it quotes what the server said, which is the useful
               part -- but under a translated headline that says what kind of
               thing went wrong. */
            <p className={`send-result ${sendCheck.ok ? 'ok' : 'bad'}`} data-result={sendCheck.ok ? 'ok' : 'bad'}>
              {sendCheck.ok
                ? <>✓ {t('sendTestOk', { arg: sendCheck.folder || '' })}</>
                : <><strong>{t('sendTestFailed')}</strong> {sendCheck.detail}</>}
            </p>
          )}
        </Section>

        {value('mail_source') === 'graph' && (
        <Section group="mail" titleKey="msRegistration"
                 tab={tab} q={query} t={t}>
          <h3>{t('msRegistration')}</h3>
          <p className="hint">{t('msRegistrationHelp')}</p>
          <div className="field">
            <input className="input" placeholder="00000000-0000-0000-0000-000000000000"
                   value={value('entra_client_id')} onChange={(e) => edit('entra_client_id', e.target.value)} />
          </div>
          <button className="btn primary" onClick={() => save(['entra_client_id'])}
                  disabled={draft.entra_client_id === undefined}>{t('saveClientId')}</button>
        </Section>
        )}

        <Section group="view" titleKey="language"
                 tab={tab} q={query} t={t}>
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
        </Section>

        <Section group="view" titleKey="theme"
                 tab={tab} q={query} t={t}>
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
        </Section>

        <Section group="ai" titleKey="aiProvider" terms={["providerLocal", "ollamaModel", "ollamaHost"]}
                 tab={tab} q={query} t={t}>
          <h3>{t('aiProvider')}</h3>
          <p className="hint">{t('aiProviderHelp')}</p>
          <div className="field">
            <label>{t('provider')}</label>
            <select className="input" value={provider} onChange={(e) => edit('llm_provider', e.target.value)}>
              <option value="none">{t('providerNone')}</option>
              {/* Local first, and named for where it runs rather than for whose
                  API it is. For an app that reads the whole mailbox, "never
                  leaves this Mac" is the property worth putting at the top of
                  the list. */}
              <option value="ollama">{t('providerLocal')}</option>
              <option value="anthropic">Anthropic (Claude)</option>
              <option value="openai">{t('providerOpenAI')}</option>
              <option value="copilot">GitHub Copilot</option>
            </select>
            {/* Only cloud providers bill anyone. Under "none" -- and later
                under a local model -- this sentence describes charges that do
                not exist, which is the kind of stray text that makes a settings
                screen feel untrustworthy. */}
            {provider !== 'none' && (
              <p className="hint" style={{ marginTop: 6 }}>{t('providerBilling')}</p>
            )}
            {provider === 'none' && (
              <p className="hint" style={{ marginTop: 6 }}>{t('providerNoneHelp')}</p>
            )}
            {/* Where the cloud-AI permission stands, for the provider that is
                saved -- not the one mid-edit in the box above, which has not
                been asked about yet. */}
            {consent?.required && consent.provider === (settings?.llm_provider || '') && (
              <div className={`consent-status ${consent.granted ? 'ok' : ''}`}>
                <span>
                  {consent.granted
                    ? t('consentStatusGranted', {
                        recipient: consent.recipient,
                        date: new Date(consent.granted_at).toLocaleDateString(lang || 'en'),
                      })
                    : t('consentStatusMissing', { recipient: consent.recipient })}
                </span>
                {consent.granted
                  ? <button className="btn sm" onClick={onWithdrawConsent}>{t('consentWithdraw')}</button>
                  : <button className="btn sm primary" onClick={onReviewConsent}>{t('consentReview')}</button>}
              </div>
            )}
          </div>

          {provider === 'ollama' && (
            <>
              <p className="hint">{t('localHelp')}</p>
              <div className="field">
                <label>{t('ollamaModel')}</label>
                <input className="input" value={value('ollama_model')}
                       onChange={(e) => edit('ollama_model', e.target.value)}
                       placeholder="qwen3.5:9b" />
                <p className="hint" style={{ marginTop: 6 }}>
                  {t('localPull', { model: value('ollama_model') || 'qwen3.5:9b' })}
                </p>
              </div>

              {/* What this Mac actually has.

                  The box above shipped with a default of `qwen3.5:9b`, and
                  someone who had pulled `qwen3.5:4b` -- deliberately, because
                  that is the one that fits a laptop -- was told the app had no
                  model and instructed to download six gigabytes they did not
                  need. A free-text field for a name only the machine knows is
                  a guessing game with the machine.

                  Shown, never chosen: the app does not silently switch the
                  model any more than it silently reaches for a credential.
                  One click is the whole interaction. */}
              <div className="field">
                <label>{t('installedModels')}</label>
                {models === null ? (
                  <button className="btn ghost" style={{ padding: '2px 8px' }}
                          onClick={loadModels}>{t('refreshModels')}</button>
                ) : models.installed.length === 0 ? (
                  <p className="hint">{t('noModelsInstalled')}</p>
                ) : (
                  <>
                    <div className="lang-grid">
                      {models.installed.map((name) => (
                        <button key={name} type="button"
                                className={`chip lang-chip ${name === value('ollama_model') ? 'active' : ''}`}
                                onClick={() => edit('ollama_model', name)}>
                          {name}
                        </button>
                      ))}
                    </div>
                    <p className="hint" style={{ marginTop: 6 }}>
                      {models.matches ? t('installedModelsHelp') : t('modelNotInstalled')}
                    </p>
                  </>
                )}
              </div>
              <div className="field">
                <label>{t('ollamaHost')}</label>
                <input className="input" value={value('ollama_host')}
                       onChange={(e) => edit('ollama_host', e.target.value)}
                       placeholder="http://127.0.0.1:11434" />
              </div>
            </>
          )}

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
                {settings?.anthropic_api_key_set && (
                  <p className="hint" style={{ marginTop: 6 }}>{t('apiKeyClearHint')}</p>
                )}
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
                {settings?.openai_api_key_set && (
                  <p className="hint" style={{ marginTop: 6 }}>{t('apiKeyClearHint')}</p>
                )}
              </div>
              <div className="field">
                <label>{t('model')}</label>
                <input className="input" value={value('openai_model')}
                       onChange={(e) => edit('openai_model', e.target.value)} />
              </div>
              <div className="field">
                <label>{t('endpoint')}</label>
                <input className="input" value={value('openai_base_url')}
                       placeholder="https://api.openai.com/v1"
                       onChange={(e) => edit('openai_base_url', e.target.value)} />
                <p className="hint" style={{ marginTop: 6 }}>{t('endpointHelp')}</p>
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
                {test.ok
                  ? `${t('working')} (${test.provider})`
                  : (test.detail_key ? t(test.detail_key, test.detail_vars || {}) : test.detail)}
              </span>
            )}
          </div>
        </Section>

        <Section group="rank" titleKey="activePriorities"
                 tab={tab} q={query} t={t}>
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
        </Section>

        <Section group="rank" titleKey="rankTitle" hintKey="rankHelp" terms={["deadlineWindow", "deadlineUrgent", "rankVolume", "catTitle"]}
                 tab={tab} q={query} t={t}>
          <h3>{t('rankTitle')}</h3>
          <p className="hint">{t('rankHelp')}</p>

          {ranking && (
            <>
              <p style={{ fontSize: 12.5, marginBottom: 10 }}>
                {ranking.learning.enabled
                  ? t('rankOn', { date: (ranking.learning.epoch_start || '').slice(0, 10) })
                  : t('rankOff')}
              </p>

              {!ranking.learning.enabled && (
                <button className="btn primary"
                        onClick={() => rankAction(api.startLearning, t('rankStarted'))}>
                  {t('rankStart')}
                </button>
              )}

              {/* The look-ahead window. Two knobs, both phrased as questions a
                  person can answer about their own week rather than as model
                  parameters -- "how far ahead do I want to see" and "how many
                  days count as right now". */}
              <div className="field row" style={{ marginTop: 14, alignItems: 'flex-end' }}>
                <label style={{ flex: 1 }}>
                  {t('deadlineWindow')}
                  <input className="input" type="number" min="1" max="120"
                         value={value('deadline_horizon_days')}
                         onChange={(e) => edit('deadline_horizon_days', e.target.value)} />
                </label>
                <label style={{ flex: 1 }}>
                  {t('deadlineUrgent')}
                  <input className="input" type="number" min="0" max="30"
                         value={value('deadline_urgent_days')}
                         onChange={(e) => edit('deadline_urgent_days', e.target.value)} />
                </label>
              </div>
              <p className="hint">{t('deadlineWindowHelp')}</p>
              <p className="hint">{t('deadlineUrgentHelp')}</p>

              <div className="field row" style={{ marginTop: 14, alignItems: 'flex-end' }}>
                <label style={{ flex: 1 }}>
                  {t('rankVolume')}
                  <input className="input" type="number" min="1" max="200" value={volume}
                         onChange={(e) => setVolume(e.target.value)} />
                </label>
                <button className="btn"
                        onClick={() => rankAction(
                          () => api.setActionVolume(Number(volume)),
                          (r) => t('rankVolumeDone', { n: r.sampled }))}>
                  {t('rankVolumeApply')}
                </button>
              </div>
              <p className="hint">{t('rankVolumeHelp')}</p>

              <p style={{ fontSize: 12, color: 'var(--muted)', marginTop: 10 }}>
                {t('rankThresholds', {
                  rel: ranking.thresholds.relevance.toFixed(2),
                  act: ranking.thresholds.action.toFixed(2),
                })}
              </p>

              {/* What the app has concluded about each KIND of mail, in a form
                  that can be disagreed with.

                  This is the lever, not a bigger step size. In a 77-person
                  email-classification study, showing people what the model
                  believed and letting them correct it directly cut the labels
                  needed from 182 to 47 and produced a classifier ~10% more
                  accurate. "Six of your eight competition emails, set aside" is
                  a claim; a weight of -1.4023 is not.

                  `signals` is shown because it is the honest measure of how
                  much to believe the bar beside it. A category corrected twice
                  moves fast and deserves to be read as provisional. */}
              {cats && cats.some((c) => c.dismissed || c.signals) && (
                <div style={{ marginTop: 16 }}>
                  <p style={{ fontSize: 12.5, fontWeight: 600 }}>{t('catTitle')}</p>
                  <div className="cat-rows">
                    {cats.filter((c) => c.dismissed || c.signals).map((c) => (
                      <div className="cat-row" key={c.category}>
                        <span className="cat-name">{t(`cat_${c.category}`)}</span>
                        <span className="cat-count">
                          {c.dismissed}/{c.total} · {c.signals}
                        </span>
                        <span className="cat-bar" title={String(c.weight)}>
                          <i className={c.weight >= 0 ? 'up' : 'down'}
                             style={{ width: `${Math.min(42, Math.abs(c.weight) * 30)}px` }} />
                        </span>
                      </div>
                    ))}
                  </div>
                  <p className="hint">{t('catHelp')}</p>
                </div>
              )}

              {/* Refusals are the diagnosis for "it is not adapting", so they
                  are shown next to the count of what did land, not hidden. */}
              <p style={{ fontSize: 12, marginTop: 6 }}>
                {t('rankApplied', { n: ranking.learning.applied })}
                {ranking.learning.refused > 0 && (
                  <> · {t('rankRefused', { n: ranking.learning.refused })}</>
                )}
              </p>

              {/* Exploration has no other visible surface. A user who never
                  happens to scroll past a badged row cannot distinguish
                  "working, nothing selected right now" from "quietly doing
                  nothing" -- and it was quietly doing nothing, because the mail
                  list filtered out every row it had chosen. */}
              <p style={{ fontSize: 12, marginTop: 4 }}>
                {t('rankExplored', { n: ranking.explored_count ?? 0 })}
              </p>
              <p className="hint">{t('rankExploredHelp')}</p>
              {ranking.learning.refused > 0 && (
                <>
                  <div style={{ display: 'flex', gap: 6, flexWrap: 'wrap', marginTop: 6 }}>
                    {Object.entries(ranking.learning.refused_by_reason).map(([reason, n]) => (
                      <span key={reason} className="tag" title={reason}>{reason} {n}</span>
                    ))}
                  </div>
                  <p className="hint">{t('rankRefusedHelp')}</p>
                </>
              )}

              {Object.keys(ranking.learning.weights || {}).length > 0 && (
                <div style={{ marginTop: 14 }}>
                  <button className="btn ghost"
                          onClick={() => rankAction(api.resetLearning, t('rankResetDone'))}>
                    {t('rankReset')}
                  </button>
                  <p className="hint">{t('rankResetHelp')}</p>
                </div>
              )}

              {rankMsg && (
                <p style={{ fontSize: 12, marginTop: 8,
                            color: rankMsg.ok ? 'var(--accent-strong)' : 'var(--danger)' }}>
                  {rankMsg.detail}
                </p>
              )}
            </>
          )}
        </Section>

        <Section group="rank" titleKey="rescoreTitle" hintKey="rescoreHelp"
                 tab={tab} q={query} t={t}>
          <h3>{t('rescoreTitle')}</h3>
          <p className="hint">{t('rescoreHelp')}</p>
          <button className="btn"
                  onClick={() => rankAction(api.rescore, (r) => t('rescoreDone', { n: r.rescored }))}>
            {t('rescoreAction')}
          </button>
        </Section>

        <Section group="rank" titleKey="rescanTitle"
                 tab={tab} q={query} t={t}>
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
        </Section>

        <Section group="view" titleKey="notifyTitle"
                 tab={tab} q={query} t={t}>
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
        </Section>

        <Section group="mail" titleKey="sync" terms={["daysToKeep", "maxMessages", "batchSize"]}
                 tab={tab} q={query} t={t}>
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
        </Section>

        {needle && !SEARCHABLE.some((k) => t(k).toLowerCase().includes(needle)) && (
          <p className="hint" style={{ padding: '10px 2px' }}>
            {t('settingsNoMatch', { arg: query.trim() })}
          </p>
        )}

      </div>
    </div>
  );
}
