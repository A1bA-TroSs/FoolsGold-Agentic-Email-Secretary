import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { api } from './lib/api.js';
import Connect from './components/Connect.jsx';
import Digest from './components/Digest.jsx';
import MailDetail from './components/MailDetail.jsx';
import MailList from './components/MailList.jsx';
import Settings from './components/Settings.jsx';
import Splash from './components/Splash.jsx';
import LogoMenu from './components/LogoMenu.jsx';
import { SkeletonList, Sweep, SyncingNote, ThinkingNote } from './components/Loading.jsx';
import { GearIcon, InboxIcon, RefreshIcon, StarIcon, SunIcon, UndoIcon } from './components/Icons.jsx';

/* Views are a flat list including Settings, not a modal on top of everything
   else. Settings used to be a boolean overlay, which meant clicking a sidebar
   icon while it was open did nothing until you closed it -- the sidebar looked
   broken. Now every rail button is just a route. */
const VIEWS = [
  { id: 'priority', label: 'Priority inbox', icon: StarIcon, hint: 'What needs you, ranked' },
  { id: 'all',      label: 'All mail',       icon: InboxIcon, hint: 'Everything, newest first' },
  { id: 'today',    label: "Today's digest", icon: SunIcon,  hint: 'One briefing for today' },
];

export default function App() {
  const [booting, setBooting] = useState(true);
  const [source, setSource] = useState({ name: 'applemail', label: '', ready: false });
  const [view, setView] = useState('priority');
  const [settings, setSettings] = useState(null);
  const [theme, setTheme] = useState('gold');

  const [mail, setMail] = useState([]);
  const [counts, setCounts] = useState({});
  const [aiStatus, setAiStatus] = useState({ available: true, off: false, detail: '' });
  const [selectedId, setSelectedId] = useState(null);
  const [cursorId, setCursorId] = useState(null);
  const [bucketFilter, setBucketFilter] = useState(null);
  const [search, setSearch] = useState('');
  const [listLoading, setListLoading] = useState(true);
  const [syncing, setSyncing] = useState(false);
  const [digest, setDigest] = useState(null);
  const [digestLoading, setDigestLoading] = useState(false);
  const [banner, setBanner] = useState('');
  const [leavingId, setLeavingId] = useState(null);
  const [undo, setUndo] = useState(null);
  const undoTimer = useRef(null);
  const [logoOpen, setLogoOpen] = useState(false);
  const [lastSync, setLastSync] = useState(null);
  const [bootDetail, setBootDetail] = useState('');
  const [firstPaint, setFirstPaint] = useState(false);

  const inSettings = view === 'settings';

  // ---- boot ---------------------------------------------------------------
  useEffect(() => {
    (async () => {
      try {
        setBootDetail('Waking the backend');
        const [health, cfg] = await Promise.all([api.health(), api.getSettings()]);
        setSource(health.source || { name: 'applemail', ready: false });
        setSettings(cfg);
        applyTheme(cfg.theme || 'gold');
        setBootDetail(health.source?.ready ? 'Ranking what arrived' : 'Finding your mailbox');
      } catch (e) {
        setBanner(`Backend not reachable: ${e.message}`);
        setBootDetail('');
      } finally {
        // Hold the splash for a beat so it never flashes; a splash that appears
        // and vanishes inside 100ms is more jarring than no splash at all.
        setTimeout(() => setBooting(false), 420);
      }
    })();
  }, []);

  function applyTheme(next) {
    setTheme(next);
    document.documentElement.setAttribute('data-theme', next);
  }

  async function chooseTheme(next) {
    applyTheme(next);
    setSettings(await api.patchSettings({ theme: next }));
  }

  // ---- data ---------------------------------------------------------------
  const loadMail = useCallback(async () => {
    if (!source.ready) return;
    setListLoading(true);
    try {
      const data = await api.listMail({
        bucket: bucketFilter,
        search: search.trim() || undefined,
        sort: view === 'all' ? 'date' : 'priority',
      });
      setMail(data.items);
      setCounts(data.counts || {});
      setAiStatus(data.ai || { available: true });
      setFirstPaint(true);
    } catch (e) {
      setBanner(e.message);
    } finally {
      setListLoading(false);
    }
  }, [source.ready, bucketFilter, search, view]);

  useEffect(() => { loadMail(); }, [loadMail]);

  useEffect(() => {
    if (!source.ready) return;
    setDigestLoading(true);
    api.digest(false).then(setDigest).catch(() => {}).finally(() => setDigestLoading(false));
  }, [source.ready]);

  async function refreshDigest() {
    setDigestLoading(true);
    try { setDigest(await api.digest(true)); } finally { setDigestLoading(false); }
  }

  async function sync() {
    setSyncing(true);
    setBanner('');
    try {
      await api.sync();
      setLastSync(new Date().toISOString());
      await loadMail();
    } catch (e) {
      setBanner(e.status === 401 ? 'Your Outlook session expired — sign in again.' : e.message);
      if (e.status === 401 || e.status === 428) {
        try { setSource((await api.health()).source); } catch { /* offline */ }
      }
    } finally {
      setSyncing(false);
    }
  }

  async function refreshSource() {
    const health = await api.health();
    setSource(health.source || {});
    setSettings(await api.getSettings());
    setView('priority');
    if (health.source?.ready) sync();
  }

  async function signOut() {
    await api.logout();
    setMail([]); setSelectedId(null); setView('priority');
    try { setSource((await api.health()).source); } catch { /* offline */ }
  }

  // ---- feedback -----------------------------------------------------------
  const visible = useMemo(() => {
    if (view === 'all') return mail;
    return mail.filter((m) => m.bucket !== 'noise' && m.verdict !== 'done' && m.verdict !== 'snoozed');
  }, [mail, view]);

  const applyFeedback = useCallback(async (id, verdict) => {
    const previous = mail.find((m) => m.id === id)?.verdict ?? null;
    const disappears = view !== 'all' && (verdict === 'done' || verdict === 'snoozed');

    // Optimistic: the row responds immediately, then the server re-scores.
    if (disappears) setLeavingId(id);
    setMail((rows) => rows.map((m) => (m.id === id ? { ...m, verdict } : m)));

    clearTimeout(undoTimer.current);
    setUndo({ id, previous, verdict });
    undoTimer.current = setTimeout(() => setUndo(null), 6000);

    try {
      if (verdict) await api.setFeedback(id, verdict, verdict === 'snoozed' ? 24 : null);
      else await api.clearFeedback(id);
      setTimeout(() => { setLeavingId(null); loadMail(); }, disappears ? 280 : 0);
    } catch (e) {
      setBanner(e.message);
      setLeavingId(null);
      loadMail();
    }
  }, [mail, view, loadMail]);

  async function undoFeedback() {
    if (!undo) return;
    const { id, previous } = undo;
    setUndo(null);
    await applyFeedback(id, previous);
  }

  // ---- keyboard -----------------------------------------------------------
  useEffect(() => {
    if (inSettings || !source.ready) return undefined;

    function onKey(e) {
      const tag = document.activeElement?.tagName;
      if (tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT') {
        if (e.key === 'Escape') document.activeElement.blur();
        return;
      }
      const ids = visible.map((m) => m.id);
      if (!ids.length) return;
      const at = Math.max(0, ids.indexOf(cursorId));

      const move = (delta) => {
        e.preventDefault();
        const next = ids[Math.min(ids.length - 1, Math.max(0, at + delta))];
        setCursorId(next);
      };

      switch (e.key) {
        case 'j': case 'ArrowDown': return move(1);
        case 'k': case 'ArrowUp': return move(-1);
        case 'Enter': case 'o': e.preventDefault(); return setSelectedId(cursorId || ids[0]);
        case 'p': return applyFeedback(cursorId || ids[0], 'pinned');
        case 'e': return applyFeedback(cursorId || ids[0], 'done');
        case 'x': return applyFeedback(cursorId || ids[0], 'not_important');
        case 's': return applyFeedback(cursorId || ids[0], 'snoozed');
        case 'u': return undoFeedback();
        case '/': e.preventDefault(); return document.getElementById('mail-search')?.focus();
        case 'r': return sync();
        default: return undefined;
      }
    }
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  });

  useEffect(() => {
    if (!cursorId && visible.length) setCursorId(visible[0].id);
  }, [visible, cursorId]);

  // ---- render -------------------------------------------------------------
  const starting = booting || (source.ready && !firstPaint);

  if (booting) {
    return (
      <div className="shell">
        <div className="titlebar" />
        <Splash done={false} detail={bootDetail} />
      </div>
    );
  }

  const currentView = VIEWS.find((v) => v.id === view);

  if (!source.ready && !inSettings) {
    return (
      <div className="shell">
        <Titlebar title="Fool&rsquo;s Gold" sub="Not connected yet" />
        <div className="app">
          <Rail view={view} setView={setView} counts={counts}
                logoOpen={logoOpen} onLogo={() => setLogoOpen((v) => !v)} />
          <div className="pane-detail">
            {banner && <div className="banner"><span className="dot" />{banner}</div>}
            <Connect source={source} settings={settings} onSettings={setSettings}
                     onReady={refreshSource} onOpenSettings={() => setView('settings')} />
          </div>
        </div>
      </div>
    );
  }

  return (
    <div className="shell">
      <Splash done={!starting} detail={bootDetail} />
      <Titlebar
        title={inSettings ? 'Settings' : currentView?.label}
        sub={inSettings ? 'Preferences and ranking inputs' : source.account || source.label}
        right={!inSettings && syncing ? <SyncingNote>Syncing…</SyncingNote> : null}
      />
      {syncing && <Sweep />}

      <div className="app">
        <Rail view={view} setView={setView} counts={counts}
              logoOpen={logoOpen} onLogo={() => setLogoOpen((v) => !v)} />
        <LogoMenu
          open={logoOpen} onClose={() => setLogoOpen(false)}
          counts={counts} source={source} ai={aiStatus} lastSync={lastSync}
          syncing={syncing} version={settings?.version}
          onSync={() => { setLogoOpen(false); sync(); }}
        />

        {inSettings ? (
          <div className="pane-detail view-enter">
            <div className="settings-wrap">
              <Settings settings={settings} onSettings={setSettings} theme={theme} onTheme={chooseTheme}
                        source={source} onSourceChange={refreshSource} onSignOut={signOut} />
            </div>
          </div>
        ) : (
          <>
            <div className="pane-list view-enter" key={view}>
              <div className="topbar">
                <input id="mail-search" className="input" placeholder="Search mail…  /"
                       value={search} onChange={(e) => setSearch(e.target.value)} />
                <button className="btn ghost" onClick={sync} disabled={syncing}
                        title="Sync (r)" aria-label="Sync">
                  <RefreshIcon spinning={syncing} />
                </button>
              </div>

              <div className="filters">
                {[
                  { id: null, label: `All ${visible.length ? `(${visible.length})` : ''}` },
                  { id: 'action', label: `Action ${counts.action ? `(${counts.action})` : ''}` },
                  { id: 'fyi', label: `FYI ${counts.fyi ? `(${counts.fyi})` : ''}` },
                  { id: 'noise', label: `Noise ${counts.noise ? `(${counts.noise})` : ''}` },
                ].map((f) => (
                  <button key={f.label} className={`chip ${bucketFilter === f.id ? 'active' : ''}`}
                          onClick={() => setBucketFilter(f.id)}>{f.label}</button>
                ))}
              </div>

              <div className="scroll">
                {listLoading && !mail.length
                  ? <SkeletonList />
                  : <MailList items={visible} selectedId={selectedId} cursorId={cursorId}
                              leavingId={leavingId} onSelect={setSelectedId} onFeedback={applyFeedback} />}
              </div>

              <div className="keyhint">
                <span><kbd>j</kbd><kbd>k</kbd> move</span>
                <span><kbd>↵</kbd> open</span>
                <span><kbd>p</kbd> pin</span>
                <span><kbd>e</kbd> done</span>
                <span><kbd>x</kbd> mute</span>
                <span><kbd>s</kbd> snooze</span>
                <span><kbd>u</kbd> undo</span>
                <span><kbd>/</kbd> search</span>
                <span><kbd>r</kbd> sync</span>
              </div>
            </div>

            <div className="pane-detail">
              {banner && <div className="banner"><span className="dot" />{banner}</div>}
              {source.ready && source.needs_setup && (
                <div className="banner">
                  <span className="dot" />
                  Add your own email address in Settings — it&rsquo;s how Fool&rsquo;s Gold tells mail
                  addressed to you from mail you&rsquo;re only copied on.
                  <button className="btn ghost" style={{ marginLeft: 'auto', padding: '1px 8px' }}
                          onClick={() => setView('settings')}>Open Settings</button>
                </div>
              )}
              {!aiStatus.available && !aiStatus.off && (
                <div className="banner">
                  <span className="dot" />
                  AI unavailable — ranking on structural signals only.
                  {aiStatus.detail ? ` (${aiStatus.detail.slice(0, 110)})` : ''}
                </div>
              )}

              {view === 'today' || !selectedId ? (
                <div className="scroll">
                  <Digest digest={digest} loading={digestLoading} onRefresh={refreshDigest} />
                  {!selectedId && view !== 'today' && (
                    <div className="empty">
                      {listLoading ? <ThinkingNote /> : 'Select an email to read it.'}
                    </div>
                  )}
                </div>
              ) : (
                <MailDetail emailId={selectedId} />
              )}

              {undo && (
                <div className="toast">
                  <span>
                    {undo.verdict ? `Marked ${undo.verdict.replace('_', ' ')}` : 'Cleared'} — ranking updated
                  </span>
                  <button className="btn" onClick={undoFeedback}><UndoIcon /> Undo</button>
                </div>
              )}
            </div>
          </>
        )}
      </div>
    </div>
  );
}

function Titlebar({ title, sub, right }) {
  return (
    <div className="titlebar">
      <span className="title" dangerouslySetInnerHTML={{ __html: title || '' }} />
      {sub && <span className="sub">{sub}</span>}
      <div className="right">{right}</div>
    </div>
  );
}

function Rail({ view, setView, counts, logoOpen, onLogo }) {
  return (
    <nav className="rail">
      <img className={`logo ${logoOpen ? 'open' : ''}`} src="./logo.png"
           alt="Fool's Gold — status" title="Status and sync"
           role="button" tabIndex={0}
           onClick={onLogo}
           onKeyDown={(e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); onLogo(); } }} />
      {VIEWS.map(({ id, label, icon: Icon, hint }) => (
        <button key={id} className={view === id ? 'active' : ''} title={`${label} — ${hint}`}
                onClick={() => setView(id)}>
          <Icon />
          {id === 'priority' && counts.action ? <span className="badge" /> : null}
        </button>
      ))}
      <div className="spacer" />
      <button className={view === 'settings' ? 'active' : ''} title="Settings"
              onClick={() => setView('settings')}>
        <GearIcon />
      </button>
    </nav>
  );
}
