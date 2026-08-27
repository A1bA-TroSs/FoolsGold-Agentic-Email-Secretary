import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { api } from './lib/api.js';
import { I18nContext, translator } from './lib/i18n.js';
import Connect from './components/Connect.jsx';
import Digest from './components/Digest.jsx';
import MailDetail from './components/MailDetail.jsx';
import MailList from './components/MailList.jsx';
import ConfirmMute from './components/ConfirmMute.jsx';
import MutedBox from './components/MutedBox.jsx';
import RemovedBox from './components/RemovedBox.jsx';
import HighlightBox from './components/HighlightBox.jsx';
import Settings from './components/Settings.jsx';
import Calendar, { DayPanel, useCalendar, todayIso } from './components/Calendar.jsx';
import Splash from './components/Splash.jsx';
import LogoMenu from './components/LogoMenu.jsx';
import { SkeletonList, Sweep, SyncingNote, ThinkingNote } from './components/Loading.jsx';
import { AgendaIcon, BackIcon, CalendarIcon, FlagIcon, GearIcon, InboxIcon, MuteIcon, RefreshIcon, UndoIcon } from './components/Icons.jsx';

/* Views are a flat list including Settings, not a modal on top of everything
   else. Settings used to be a boolean overlay, which meant clicking a sidebar
   icon while it was open did nothing until you closed it -- the sidebar looked
   broken. Now every rail button is just a route. */
/* Order runs from "what do I do right now" outwards to "what have I put
   aside": today's checklist, then the ranked inbox, then everything, then the
   month, then the things silenced or removed. */
const VIEWS = [
  { id: 'today',    key: 'viewToday',    hintKey: 'viewTodayHint',    short: 'railToday',    icon: AgendaIcon },
  { id: 'priority', key: 'viewPriority', hintKey: 'viewPriorityHint', short: 'railPriority', icon: FlagIcon },
  { id: 'all',      key: 'viewAll',      hintKey: 'viewAllHint',      short: 'railAll',      icon: InboxIcon },
  { id: 'calendar', key: 'viewCalendar', hintKey: 'viewCalendarHint', short: 'railCalendar', icon: CalendarIcon },
  { id: 'muted',    key: 'viewMuted',    hintKey: 'viewMutedHint',    short: 'railMuted',    icon: MuteIcon },
];

/* The divider between the list and the reading pane is draggable. The chosen
   width lives in localStorage rather than in settings, because it describes
   this window on this screen -- not the account, which syncs nothing anyway. */
const LIST_MIN = 260;
const LIST_MAX = 640;
const LIST_DEFAULT = 340;
const LIST_KEY = 'foolsgold.listWidth';

const clampWidth = (n) => Math.min(LIST_MAX, Math.max(LIST_MIN, Math.round(n)));

function readListWidth() {
  try {
    const raw = Number(localStorage.getItem(LIST_KEY));
    return Number.isFinite(raw) && raw > 0 ? clampWidth(raw) : LIST_DEFAULT;
  } catch {
    return LIST_DEFAULT;      // private window, or storage disabled
  }
}

function saveListWidth(width) {
  try { localStorage.setItem(LIST_KEY, String(width)); } catch { /* not fatal */ }
}

export default function App() {
  const [booting, setBooting] = useState(true);
  const [source, setSource] = useState({ name: 'applemail', label: '', ready: false });
  const [view, setView] = useState('priority');
  const [settings, setSettings] = useState(null);
  const [theme, setTheme] = useState('gold');
  const [lang, setLang] = useState('en');
  const t = useMemo(() => translator(lang), [lang]);

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
  const [leaving, setLeaving] = useState(null);   // { id, kind } while a row plays out
  const [undo, setUndo] = useState(null);
  const undoTimer = useRef(null);
  const [logoOpen, setLogoOpen] = useState(false);
  const [lastSync, setLastSync] = useState(null);
  const [bootDetail, setBootDetail] = useState('');
  const [muteTarget, setMuteTarget] = useState(null);   // { email, count } while confirming
  const [muteBusy, setMuteBusy] = useState(false);
  const [mutedSenders, setMutedSenders] = useState([]);
  const [highlighted, setHighlighted] = useState([]);
  const [removed, setRemoved] = useState([]);
  const [removedBusy, setRemovedBusy] = useState(new Set());
  const [firstPaint, setFirstPaint] = useState(false);
  const [addingTask, setAddingTask] = useState(false);
  const [listWidth, setListWidth] = useState(readListWidth);

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
        setLang(cfg.ui_language || 'en');
        setBootDetail(health.source?.ready ? 'Ranking what arrived' : 'Finding your mailbox');
      } catch (e) {
        setBanner(translator(lang)('backendUnreachable', { detail: e.message }));
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

  async function chooseLanguage(next) {
    setLang(next);
    document.documentElement.setAttribute('lang', next);
    setSettings(await api.patchSettings({ ui_language: next }));
  }

  // ---- data ---------------------------------------------------------------
  const loadMail = useCallback(async () => {
    if (!source.ready) return;
    setListLoading(true);
    try {
      const data = await api.listMail({
        // The muted box hides the bucket chips, so applying a filter the user
        // cannot see (or clear) would silently shrink it.
        bucket: view === 'muted' ? null : bucketFilter,
        search: search.trim() || undefined,
        sort: view === 'all' ? 'date' : 'priority',
        muted: view === 'muted',
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

  // The muted box lists senders, not just their mail.
  const loadMuted = useCallback(async () => {
    try { setMutedSenders((await api.mutedSenders()).items || []); } catch { /* offline */ }
    // Highlights load alongside: they are the same kind of decision about the
    // same kind of thing, and a stale one would contradict the coloured rows.
    try { setHighlighted((await api.highlightedSenders()).items || []); } catch { /* offline */ }
  }, []);
  useEffect(() => { if (source.ready) loadMuted(); }, [source.ready, loadMuted, view]);

  /* What has been taken off the calendar. Loaded with the muted box, which is
     where it lives, and refreshed after a delete so the two views agree. */
  const loadRemoved = useCallback(async () => {
    try { setRemoved((await api.removedEntries()).items || []); } catch { /* offline */ }
  }, []);
  useEffect(() => {
    if (source.ready && (view === 'muted' || view === 'calendar')) loadRemoved();
  }, [source.ready, loadRemoved, view]);

  async function actOnRemoved(entry, run) {
    setRemovedBusy((prev) => new Set(prev).add(entry.key));
    try {
      await run();
      await loadRemoved();
      calendarRef.current?.();
      setBanner('');
    } catch (e) {
      setBanner(e.message);
    } finally {
      setRemovedBusy((prev) => {
        const next = new Set(prev);
        next.delete(entry.key);
        return next;
      });
    }
  }

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

  /* A sync against a small or already-current mailbox can finish in under a
     tenth of a second. Without a floor the progress sweep flashes for two
     frames, so pressing r looks like it did nothing at all -- you cannot tell
     a fast sync from a dead key. Hold the indicator long enough to read. */
  const SYNC_MIN_MS = 550;

  async function sync() {
    setSyncing(true);
    setBanner('');
    const startedAt = Date.now();
    try {
      await api.sync();
      setLastSync(new Date().toISOString());
      await loadMail();
    } catch (e) {
      setBanner(e.status === 401 ? t('sessionExpired') : e.message);
      if (e.status === 401 || e.status === 428) {
        try { setSource((await api.health()).source); } catch { /* offline */ }
      }
    } finally {
      const left = SYNC_MIN_MS - (Date.now() - startedAt);
      if (left > 0) await new Promise((r) => setTimeout(r, left));
      setSyncing(false);
    }
  }

  async function refreshSource() {
    const health = await api.health();
    setSource(health.source || {});
    const cfg = await api.getSettings();
    setSettings(cfg);
    setLang(cfg.ui_language || 'en');
    setView('priority');
    if (health.source?.ready) sync();
  }

  async function signOut() {
    await api.logout();
    setMail([]); setSelectedId(null); setView('priority');
    try { setSource((await api.health()).source); } catch { /* offline */ }
  }

  // ---- feedback -----------------------------------------------------------
  const DISMISSED = new Set(['done', 'snoozed', 'not_important']);

  /* Which briefing rows show as ticked.

     This used to be derived only from the mail list, which is a different
     query from the one that built the briefing -- so a row the list did not
     happen to contain (a muted sender, anything past the 200-row limit) could
     never show a tick no matter how often you clicked it. The verdict now
     comes back on the digest row itself; this set is only the optimistic
     overlay that makes the tick appear on the same frame as the click. */
  const [justDone, setJustDone] = useState(new Set());
  const doneIds = useMemo(() => {
    const ids = new Set(mail.filter((m) => m.verdict === 'done').map((m) => m.id));
    for (const id of justDone) ids.add(id);
    return ids;
  }, [mail, justDone]);

  const visible = useMemo(() => {
    if (view === 'all') return mail;
    return mail.filter((m) =>
      // The row being animated out has to stay mounted until its exit finishes.
      // Filtering on the optimistic verdict alone unmounted it on the same tick
      // the checkbox was ticked, so the completion animation never played --
      // the row simply blinked out of existence.
      m.id === leaving?.id
      // Highlighting is an explicit "I want to spot these". Leaving such a
      // sender filtered out as noise would contradict the instruction the
      // moment it was given -- you colour an address and its mail is nowhere.
      // It does not change the ranking, only whether the row is on screen.
      || (m.highlight && !DISMISSED.has(m.verdict))
      || (m.bucket !== 'noise' && !DISMISSED.has(m.verdict)));
  }, [mail, view, leaving]);

  const applyFeedback = useCallback(async (id, verdict) => {
    const previous = mail.find((m) => m.id === id)?.verdict ?? null;

    // Completing and dismissing get different exits on purpose. Ticking
    // something off should feel like an accomplishment -- the tick draws, the
    // subject strikes through, the row washes gold and slides away to the
    // right. Muting is a rejection, so it desaturates and shrinks away to the
    // left. Two different feelings for two different meanings.
    const kind = verdict === 'done' ? 'done'
      : verdict === 'not_important' || verdict === 'snoozed' ? 'banish'
      : null;
    const exits = view !== 'all' && kind !== null;

    setMail((rows) => rows.map((m) => (m.id === id ? { ...m, verdict } : m)));
    /* The briefing row may not be in `mail` at all, so the optimistic tick has
       to be recorded separately or clicking it looks like nothing happened. */
    setJustDone((prev) => {
      const next = new Set(prev);
      if (verdict === 'done') next.add(id); else next.delete(id);
      return next;
    });
    if (exits) setLeaving({ id, kind });

    clearTimeout(undoTimer.current);
    setUndo({ id, previous, verdict });
    undoTimer.current = setTimeout(() => setUndo(null), 6000);

    try {
      if (verdict) await api.setFeedback(id, verdict, verdict === 'snoozed' ? 24 : null);
      else await api.clearFeedback(id);
      // Hold the list still until the exit animation finishes, or the row is
      // yanked out from under the animation and the gesture reads as a glitch.
      const settle = kind === 'done' ? 800 : kind === 'banish' ? 480 : 0;
      setTimeout(() => {
        setLeaving(null);
        loadMail();
        // Cheap: today's digest is cached, so this only re-reads the verdicts
        // that were just joined onto it.
        api.digest(false).then(setDigest).catch(() => {});
      }, settle);
    } catch (e) {
      setBanner(e.message);
      setLeaving(null);
      loadMail();
    }
  }, [mail, view, loadMail]);

  /* Muting is a decision about a correspondent, so it confirms first and then
     burns the row away rather than sliding it out like a completed task. */
  async function askMute(email) {
    const address = email.from_address;
    if (!address) return;
    let count = 0;
    try { count = (await api.muteImpact(address)).message_count; } catch { /* offline */ }
    setMuteTarget({ email, count });
  }

  async function confirmMute() {
    if (!muteTarget) return;
    const { email } = muteTarget;
    setMuteBusy(true);
    try {
      setMuteTarget(null);
      setLeaving({ id: email.id, kind: 'burn' });
      await api.muteSender(email.from_address);
      setBanner('');
      setTimeout(async () => {
        setLeaving(null);
        await Promise.all([loadMail(), loadMuted()]);
        calendarRef.current?.();      // the muted sender's due dates go too
      }, 1000);                       // the burn runs ~1s; do not yank the row out mid-animation
    } catch (e) {
      setBanner(e.message);
      setLeaving(null);
    } finally {
      setMuteBusy(false);
    }
  }

  async function unmute(address) {
    try {
      await api.unmuteSender(address);
      await Promise.all([loadMail(), loadMuted()]);
      calendarRef.current?.();        // and they come back
    } catch (e) { setBanner(e.message); }
  }

  /* Muting is confirmed and applied by code declared above the calendar hook,
     so it reaches the reload through a ref rather than the binding directly. */
  const calendarRef = useRef(null);

  /* Highlighting is the opposite instruction to muting and, like muting, it is
     about the correspondent -- so one call updates both surfaces at once and
     neither can hold a stale idea of who is coloured. It also un-mutes, which
     is why the mail list has to be reloaded and not merely repainted. */
  async function setHighlight(address, color) {
    if (!address) return;
    try {
      if (color) await api.highlightSender(address, color);
      else await api.unhighlightSender(address);
      await Promise.all([loadMail(), loadMuted()]);
      calendarRef.current?.();
      setBanner('');
    } catch (e) {
      setBanner(e.message);
    }
  }

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
      /* Keys that act on the app rather than on a row work even with nothing
         in the list -- an empty inbox or a search that matched nothing is
         exactly when you want to re-sync or clear the filter, and those used
         to be dead because the row guard below ran first. */
      switch (e.key) {
        case 'r': e.preventDefault(); return sync();
        case 'u': return undoFeedback();
        case 'Escape': return selectedId ? closeEmail() : undefined;
        case '/': e.preventDefault(); return document.getElementById('mail-search')?.focus();
        default: break;
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
        case 'x': {
          const target = visible.find((m) => m.id === (cursorId || ids[0]));
          return target ? askMute(target) : undefined;
        }
        case 's': return applyFeedback(cursorId || ids[0], 'snoozed');
        default: return undefined;
      }
    }
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  });

  useEffect(() => {
    if (!cursorId && visible.length) setCursorId(visible[0].id);
  }, [visible, cursorId]);

  // If the cursor's row disappears (ticked off, muted, filtered away), fall back
  // to the selected mail rather than silently snapping to the top.
  useEffect(() => {
    if (!visible.length) return;
    if (visible.some((m) => m.id === cursorId)) return;
    const fallback = visible.find((m) => m.id === selectedId) || visible[0];
    setCursorId(fallback.id);
  }, [visible, cursorId, selectedId]);

  /* Clicking a row moves the keyboard cursor there too.

     They were separate before: clicking an email set the selection but left the
     cursor wherever it had been (usually the top of the list), so the first
     arrow press after a click jumped back to the top instead of stepping down
     from the mail you were looking at. */
  const openEmail = useCallback((id) => {
    setSelectedId(id);
    setCursorId(id);
  }, []);

  /* Closing an email is a real action, not just "click something else". The
     briefing and the calendar both hand you an email that then fills the
     reading pane, and without a way back the only exit was to pick a different
     email or change view entirely. */
  const closeEmail = useCallback(() => setSelectedId(null), []);

  const calendar = useCalendar(loadMail, view === 'calendar');
  useEffect(() => { calendarRef.current = calendar.reload; }, [calendar.reload]);

  /* The calendar's own entries are not mail rows, so they carry the sender as
     a plain address. Reshape one into what the shared confirm dialog expects
     rather than giving muting a second, subtly different path. */
  const muteFromCalendar = useCallback((entry) => {
    if (!entry?.sender_address) return;
    askMute({
      id: entry.email_id,
      from_address: entry.sender_address,
      from_name: entry.sender || '',
      subject: entry.title,
    });
  }, []);

  useEffect(() => { if (view !== 'calendar') setAddingTask(false); }, [view]);

  // ---- resizable divider --------------------------------------------------
  const listRef = useRef(null);

  const beginResize = useCallback((event) => {
    if (event.button !== undefined && event.button !== 0) return;
    event.preventDefault();
    const left = listRef.current?.getBoundingClientRect().left ?? 0;
    let latest = null;
    const move = (ev) => {
      latest = clampWidth(ev.clientX - left);
      setListWidth(latest);
    };
    const stop = () => {
      window.removeEventListener('pointermove', move);
      window.removeEventListener('pointerup', stop);
      window.removeEventListener('pointercancel', stop);
      document.body.classList.remove('resizing');
      if (latest !== null) saveListWidth(latest);
    };
    document.body.classList.add('resizing');
    window.addEventListener('pointermove', move);
    window.addEventListener('pointerup', stop);
    window.addEventListener('pointercancel', stop);
  }, []);

  const nudgeResize = useCallback((event) => {
    const step = event.shiftKey ? 40 : 12;
    const delta = event.key === 'ArrowLeft' ? -step : event.key === 'ArrowRight' ? step : 0;
    if (!delta) return;
    event.preventDefault();
    setListWidth((current) => {
      const next = clampWidth(current + delta);
      saveListWidth(next);
      return next;
    });
  }, []);

  const resetResize = useCallback(() => {
    setListWidth(LIST_DEFAULT);
    saveListWidth(LIST_DEFAULT);
  }, []);

  // ---- render -------------------------------------------------------------
  const starting = booting || (source.ready && !firstPaint);

  if (booting) {
    return (
      <I18nContext.Provider value={t}>
        <div className="shell">
          <div className="titlebar" />
          <Splash done={false} detail={bootDetail} />
        </div>
      </I18nContext.Provider>
    );
  }

  const currentView = VIEWS.find((v) => v.id === view);

  if (!source.ready && !inSettings) {
    return (
      <I18nContext.Provider value={t}>
      <div className="shell">
        <Titlebar title={t('appName')} sub={t('notConnected')} />
        <div className="app">
          <Rail view={view} setView={setView} counts={counts} t={t}
                logoOpen={logoOpen} onLogo={() => setLogoOpen((v) => !v)} />
          <div className="pane-detail">
            {banner && <div className="banner"><span className="dot" />{banner}</div>}
            <Connect source={source} settings={settings} onSettings={setSettings}
                     onReady={refreshSource} onOpenSettings={() => setView('settings')} />
          </div>
        </div>
      </div>
      </I18nContext.Provider>
    );
  }

  return (
    <I18nContext.Provider value={t}>
    <div className="shell">
      <Splash done={!starting} detail={bootDetail} />
      <Titlebar
        title={inSettings ? t('viewSettings') : t(currentView?.key || 'viewPriority')}
        sub={inSettings ? t('settingsSub') : source.account || source.label}
        right={!inSettings && syncing ? <SyncingNote>Syncing…</SyncingNote> : null}
      />
      {syncing && <Sweep />}

      <div className="app">
        <Rail view={view} setView={setView} counts={counts} t={t}
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
                        source={source} onSourceChange={refreshSource} onSignOut={signOut}
                    lang={lang} onLanguage={chooseLanguage} />
            </div>
          </div>
        ) : (
          <>
            <div className="pane-list view-enter" key={view} ref={listRef}
                 style={{ width: listWidth, flexBasis: listWidth }}>
              {/* In the Today view the briefing IS the list: it lives in the
                  left column so the reading pane is free to show whatever row
                  you click. Previously the briefing filled the reading pane
                  itself, so clicking one of its rows selected an email that
                  had nowhere to appear -- the click looked like it did nothing. */}
              <div className="topbar">
                {view !== 'today' && view !== 'calendar' && <input id="mail-search" className="input" placeholder={t('searchPlaceholder')}
                       value={search} onChange={(e) => setSearch(e.target.value)} />}
                <button className="btn ghost" onClick={sync} disabled={syncing}
                        title={`${t('syncTitle')} (r)`} aria-label={t('syncTitle')}>
                  <RefreshIcon spinning={syncing} />
                </button>
              </div>

              {view !== 'muted' && view !== 'today' && view !== 'calendar' && <div className="filters">
                {[
                  { id: null, label: `${t('filterAll')} ${visible.length ? `(${visible.length})` : ''}` },
                  { id: 'action', label: `${t('filterAction')} ${counts.action ? `(${counts.action})` : ''}` },
                  { id: 'fyi', label: `${t('filterFyi')} ${counts.fyi ? `(${counts.fyi})` : ''}` },
                  { id: 'noise', label: `${t('filterNoise')} ${counts.noise ? `(${counts.noise})` : ''}` },
                ].map((f) => (
                  <button key={f.label} className={`chip ${bucketFilter === f.id ? 'active' : ''}`}
                          onClick={() => setBucketFilter(f.id)}>{f.label}</button>
                ))}
              </div>}

              <div className="scroll">
                {view === 'calendar' ? (
                  <DayPanel
                    day={calendar.day} items={calendar.items} busyKeys={calendar.busyKeys}
                    onToggle={calendar.toggle}
                    onDelete={(entry) => calendar.remove(entry).then(loadRemoved)}
                    onMute={muteFromCalendar}
                    onHighlight={setHighlight}
                    burningId={leaving?.kind === 'burn' ? leaving.id : null}
                    onOpenEmail={openEmail} onAdd={calendar.addTask}
                    adding={addingTask} onStartAdd={() => setAddingTask(true)}
                    onStopAdd={() => setAddingTask(false)}
                  />
                ) : view === 'today' ? (
                  <Digest
                    digest={digest} loading={digestLoading} onRefresh={refreshDigest}
                    selectedId={selectedId} checkedIds={doneIds}
                    onOpen={openEmail} onDone={applyFeedback}
                  />
                ) : (
                  <>
                    {view === 'muted' && <>
                      <MutedBox senders={mutedSenders} onUnmute={unmute} />
                      <HighlightBox items={highlighted} onHighlight={setHighlight} />
                      <RemovedBox
                        items={removed} busyKeys={removedBusy}
                        onRestore={(it) => actOnRemoved(it, () => api.restoreEntry(it.kind, it.id))}
                        onPurge={(it) => actOnRemoved(it, () => api.purgeEntry(it.kind, it.id))}
                      />
                    </>}
                    {listLoading && !mail.length
                      ? <SkeletonList />
                      : <MailList items={visible} selectedId={selectedId} cursorId={cursorId}
                                  leaving={leaving} mutedView={view === 'muted'}
                                  onSelect={openEmail} onFeedback={applyFeedback} onMute={askMute}
                                  onHighlight={setHighlight} />}
                  </>
                )}
              </div>

              {view !== 'today' && view !== 'calendar' && <div className="keyhint">
                <span><kbd>↑</kbd><kbd>↓</kbd> {t('keyMove')}</span>
                <span><kbd>↵</kbd> {t('keyOpen')}</span>
                <span><kbd>p</kbd> {t('keyPin')}</span>
                <span><kbd>e</kbd> {t('keyDone')}</span>
                <span><kbd>x</kbd> {t('keyMute')}</span>
                <span><kbd>s</kbd> {t('keySnooze')}</span>
                <span><kbd>u</kbd> {t('keyUndo')}</span>
                <span><kbd>/</kbd> {t('keySearch')}</span>
                <span><kbd>r</kbd> {t('keySync')}</span>
              </div>}
            </div>

            <div
              className="pane-resizer"
              role="separator"
              aria-orientation="vertical"
              aria-label={t('resizeHandle')}
              aria-valuenow={listWidth}
              aria-valuemin={LIST_MIN}
              aria-valuemax={LIST_MAX}
              tabIndex={0}
              title={t('resizeHint')}
              onPointerDown={beginResize}
              onKeyDown={nudgeResize}
              onDoubleClick={resetResize}
            ><span /></div>

            <div className="pane-detail">
              {banner && <div className="banner"><span className="dot" />{banner}</div>}
              {source.ready && source.needs_setup && (
                <div className="banner">
                  <span className="dot" />
                  {t('addAddressHint')}
                  <button className="btn ghost" style={{ marginLeft: 'auto', padding: '1px 8px' }}
                          onClick={() => setView('settings')}>{t('openSettings')}</button>
                </div>
              )}
              {!aiStatus.available && !aiStatus.off && (
                <div className="banner">
                  <span className="dot" />
                  {t('aiUnavailable')}
                  {aiStatus.detail ? ` (${aiStatus.detail.slice(0, 110)})` : ''}
                </div>
              )}

              {selectedId ? (
                <>
                  {/* The way back. Reading an email is a detour from a list, a
                      briefing or a day on the calendar, and until this existed
                      the only way out of the detour was to start another one. */}
                  <div className="detail-back">
                    <button className="btn ghost" onClick={closeEmail}>
                      <BackIcon />
                      {t(view === 'calendar' ? 'backToCalendar'
                        : view === 'today' ? 'backToChecklist' : 'backToList')}
                    </button>
                    <span className="detail-back-hint"><kbd>esc</kbd></span>
                  </div>
                  <MailDetail emailId={selectedId} />
                </>
              ) : view === 'calendar' ? (
                <div className="scroll cal-scroll">
                  <Calendar
                    month={calendar.month} loading={calendar.loading}
                    selectedDay={calendar.day}
                    onMove={calendar.move} onToday={calendar.goToday}
                    onPickDay={calendar.pickDay}
                    onQuickAdd={(day) => { calendar.pickDay(day); setAddingTask(true); }}
                    onMuteSender={(s) => askMute({ from_address: s.address, from_name: s.name })}
                    onHighlightSender={setHighlight}
                  />
                  {calendar.error && <div className="banner"><span className="dot" />{calendar.error}</div>}
                </div>
              ) : view === 'today' ? (
                <div className="empty">{t('selectEmail')}</div>
              ) : (
                <div className="scroll">
                  <Digest
                    digest={digest} loading={digestLoading} onRefresh={refreshDigest}
                    selectedId={selectedId} checkedIds={doneIds}
                    onOpen={openEmail} onDone={applyFeedback}
                  />
                  <div className="empty">
                    {listLoading ? <ThinkingNote>{t('thinking')}</ThinkingNote> : t('selectEmail')}
                  </div>
                </div>
              )}

              {undo && (
                <div className="toast">
                  <span>
                    {undo.verdict
                      ? t({ done: 'markedDone', pinned: 'markedPinned',
                            not_important: 'markedNotImportant', snoozed: 'markedSnoozed' }[undo.verdict])
                      : t('cleared')}
                  </span>
                  <button className="btn" onClick={undoFeedback}><UndoIcon /> {t('undo')}</button>
                </div>
              )}
            </div>
          </>
        )}
      </div>

      <ConfirmMute
        open={!!muteTarget}
        sender={muteTarget?.email?.from_name}
        address={muteTarget?.email?.from_address}
        count={muteTarget?.count || 0}
        busy={muteBusy}
        onCancel={() => setMuteTarget(null)}
        onConfirm={confirmMute}
      />
    </div>
    </I18nContext.Provider>
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

function Rail({ view, setView, counts, logoOpen, onLogo, t }) {
  return (
    <nav className="rail">
      <img className={`logo ${logoOpen ? 'open' : ''}`} src="./logo.png"
           alt="Fools Gold — status" title="Status and sync"
           role="button" tabIndex={0}
           onClick={onLogo}
           onKeyDown={(e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); onLogo(); } }} />
      {/* Labels, because three unlabelled glyphs in a column are a guessing
          game -- a sun in particular read as a brightness control. */}
      {VIEWS.map(({ id, key, hintKey, short, icon: Icon }) => (
        <button key={id} className={`rail-item ${view === id ? 'active' : ''}`}
                title={`${t(key)} — ${t(hintKey)}`}
                onClick={() => setView(id)}>
          <span className="rail-glyph">
            <Icon />
            {id === 'priority' && counts.action ? <span className="badge" /> : null}
          </span>
          <span className="rail-label">{t(short)}</span>
        </button>
      ))}
      <div className="spacer" />
      <button className={`rail-item ${view === 'settings' ? 'active' : ''}`} title={t('viewSettings')}
              onClick={() => setView('settings')}>
        <span className="rail-glyph"><GearIcon /></span>
        <span className="rail-label">{t('railSettings')}</span>
      </button>
    </nav>
  );
}
