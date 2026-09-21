import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { api } from './lib/api.js';
import { useEvent } from './lib/useEvent.js';
import { reuseUnchanged } from './lib/reconcile.js';
import { I18nContext, translator } from './lib/i18n.js';
import Connect from './components/Connect.jsx';
import Digest from './components/Digest.jsx';
import MailDetail from './components/MailDetail.jsx';
import MailList from './components/MailList.jsx';
import ConfirmMute from './components/ConfirmMute.jsx';
import ConsentDialog from './components/ConsentDialog.jsx';
import MutedBox from './components/MutedBox.jsx';
import RemovedBox from './components/RemovedBox.jsx';
import HighlightBox from './components/HighlightBox.jsx';
import Settings from './components/Settings.jsx';
import Calendar, { DayPanel, useCalendar, todayIso } from './components/Calendar.jsx';
import Splash from './components/Splash.jsx';
import LogoMenu from './components/LogoMenu.jsx';
import { SkeletonList, Sweep, SyncStep, ThinkingNote } from './components/Loading.jsx';
import { AgendaIcon, ArchiveIcon, BackIcon, CalendarIcon, CheckIcon, ChevronDownIcon, GearIcon, InboxIcon, IrrelevantIcon, MuteIcon, RefreshIcon, UndoIcon } from './components/Icons.jsx';

/* Views are a flat list including Settings, not a modal on top of everything
   else. Settings used to be a boolean overlay, which meant clicking a sidebar
   icon while it was open did nothing until you closed it -- the sidebar looked
   broken. Now every rail button is just a route. */
/* Order runs from "what do I do right now" outwards to "what have I put
   aside": the ranked inbox, then everything, then the month, then the things
   silenced or removed.

   There is no separate Today view. There was, and it showed the same briefing
   Priority already shows -- a rail button whose entire job was to move the
   checklist from the right-hand pane into the left-hand one. Two doors into one
   room is a question the user answers every time they look at the rail. So
   Priority absorbed it, and took the agenda icon with it: what the button opens
   onto is a checklist for today, which is what that icon has always said. */
const VIEWS = [
  { id: 'priority', key: 'viewPriority', hintKey: 'viewPriorityHint', short: 'railPriority', icon: AgendaIcon },
  { id: 'all',      key: 'viewAll',      hintKey: 'viewAllHint',      short: 'railAll',      icon: InboxIcon },
  { id: 'calendar', key: 'viewCalendar', hintKey: 'viewCalendarHint', short: 'railCalendar', icon: CalendarIcon },
];

/* The three lists of decisions already made, folded into one group.

   They arrived one at a time -- done, then dismissed, then muted was already
   there -- and by the third the rail was six items deep before Settings, with
   the three you look at least often sitting in the middle of the three you
   look at constantly. They are also the same KIND of thing: none of them is a
   place you read mail, all of them are places you go to check or reverse
   something you did. A group says that; six equal buttons said the opposite.

   Collapsed by default, and opened automatically when one of them is the
   current view -- otherwise clicking into a list would leave the thing you are
   looking at hidden inside a closed folder. */
const PUT_AWAY = [
  { id: 'done',      key: 'viewDone',      hintKey: 'viewDoneHint',      short: 'railDone',      icon: CheckIcon },
  { id: 'dismissed', key: 'viewDismissed', hintKey: 'viewDismissedHint', short: 'railDismissed', icon: IrrelevantIcon },
  { id: 'muted',     key: 'viewMuted',     hintKey: 'viewMutedHint',     short: 'railMuted',     icon: MuteIcon },
];
const PUT_AWAY_IDS = PUT_AWAY.map((v) => v.id);
const PUT_AWAY_KEY = 'foolsgold.putAwayOpen';

/* A date for a banner, not for a row. Deliberately separate from the list's
   `when()`: that one says "09:14" for today, which is exactly wrong in a
   sentence whose whole point is that the date is days old. */
const shortDate = (iso) => {
  if (!iso) return '';
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? String(iso).slice(0, 10)
    : d.toLocaleDateString(undefined, { month: 'short', day: 'numeric' });
};

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

/* A notice that can be sent away.

   Every one of these is true and worth saying once. Said on every render of
   every session, with no way to acknowledge it, they become a permanent strip
   of yellow the eye stops reading -- and then the one that matters is the one
   nobody sees either.

   So each announces itself when the app opens and carries a way to close it.
   Dismissals are held in memory, not on disk, which is the whole design in one
   line: a fresh launch says it again, because the condition is still true and
   this is the moment the user is deciding what to look at. And a notice whose
   condition goes away is forgotten, so if it comes back it is new news and
   says so.

   `onDismiss` is not optional-by-omission: a notice with no way out is the
   thing this exists to stop, so passing none is a mistake, not a mode. */
function Notice({ id, onDismiss, dismissLabel, children }) {
  return (
    <div className="banner">
      <span className="dot" />
      <div className="banner-text">{children}</div>
      <button type="button" className="banner-x"
              onClick={() => onDismiss(id)}
              title={dismissLabel} aria-label={dismissLabel}>
        <svg viewBox="0 0 16 16" aria-hidden="true" width="11" height="11">
          <path d="M3.5 3.5 12.5 12.5M12.5 3.5 3.5 12.5" stroke="currentColor"
                strokeWidth="1.7" strokeLinecap="round" fill="none" />
        </svg>
      </button>
    </div>
  );
}

export default function App() {
  const [booting, setBooting] = useState(true);
  const [source, setSource] = useState({ name: 'applemail', label: '', ready: false });
  const [view, setView] = useState('priority');
  /* Folded by default, and the choice is remembered -- it describes how this
     person likes their window, not anything about the account.
     `try` because a private window can make localStorage throw on read. */
  const [putAwayOpen, setPutAwayOpen] = useState(() => {
    try { return localStorage.getItem(PUT_AWAY_KEY) === '1'; } catch { return false; }
  });
  useEffect(() => {
    try { localStorage.setItem(PUT_AWAY_KEY, putAwayOpen ? '1' : '0'); } catch { /* private window */ }
  }, [putAwayOpen]);
  /* Opened whenever one of its lists becomes the current view. Without this,
     arriving at Dismissed from a keyboard shortcut or a restored session would
     show the list with its own folder shut above it. */
  useEffect(() => {
    if (PUT_AWAY_IDS.includes(view)) setPutAwayOpen(true);
  }, [view]);
  const [settings, setSettings] = useState(null);
  /* Cloud-AI permission for the provider saved in Settings. The backend refuses
     to send without it; this is how the user is asked. A "no" is remembered for
     the session per (provider, recipient, disclosure version), so the dialog
     does not nag -- Settings keeps a way back to it. */
  const [consent, setConsent] = useState(null);
  const [consentOpen, setConsentOpen] = useState(false);
  const [consentBusy, setConsentBusy] = useState(false);
  const declinedConsent = useRef(new Set());
  const [theme, setTheme] = useState('gold');
  const [lang, setLang] = useState('en');
  const t = useMemo(() => translator(lang), [lang]);

  const [mail, setMail] = useState([]);
  const [counts, setCounts] = useState({});
  const [aiStatus, setAiStatus] = useState({ available: true, off: false, detail: '' });
  /* What the last sync actually did. The app reads Apple Mail's files rather
     than the mail server, so it is exactly as fresh as Apple Mail is -- and
     until this existed, a source that had stopped producing looked identical
     to a quiet week. */
  const [syncState, setSyncState] = useState(null);
  const [selectedId, setSelectedId] = useState(null);
  const [cursorId, setCursorId] = useState(null);
  const [bucketFilter, setBucketFilter] = useState(null);
  const [search, setSearch] = useState('');
  const [listLoading, setListLoading] = useState(true);
  const [syncing, setSyncing] = useState(false);
  const [digest, setDigestState] = useState(null);
  /* The briefing is one entry per actionable email, so on a full mailbox it is
     the same size as the list and re-renders for the same reason. */
  const setDigest = useCallback((next) => setDigestState((prev) => {
    if (!next || !prev) return next;
    const items = reuseUnchanged(prev.items, next.items);
    return items === prev.items ? { ...next, items: prev.items } : { ...next, items };
  }), []);
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
  /* Ids the user has closed during this run of the app. A Set in state,
     deliberately not persisted -- see the Notice comment. */
  const [dismissed, setDismissed] = useState(() => new Set());
  const dismissNotice = useCallback((id) => {
    setDismissed((prev) => new Set(prev).add(id));
  }, []);

  const inSettings = view === 'settings';

  const consentKey = (c) => `${c.provider}|${c.recipient}|${c.version}`;

  const checkConsent = useCallback(async () => {
    try {
      const c = await api.aiConsent();
      setConsent(c);
      if (c.required && !c.granted && !declinedConsent.current.has(consentKey(c))) {
        setConsentOpen(true);
      }
    } catch { /* backend without the endpoint, or offline -- the gate still holds server-side */ }
  }, []);

  // Asked when the saved provider (or the server it points at) changes, not
  // while it is being typed -- Settings saves, then this runs.
  useEffect(() => {
    if (!booting && settings) checkConsent();
  }, [booting, settings?.llm_provider, settings?.openai_base_url, checkConsent]); // eslint-disable-line react-hooks/exhaustive-deps

  async function allowConsent() {
    if (!consent) return;
    setConsentBusy(true);
    try {
      setConsent(await api.grantConsent(consent.provider));
      setConsentOpen(false);
      sync();            // rank with the AI the user just allowed, now
    } catch (e) {
      setBanner(e.message);
    } finally {
      setConsentBusy(false);
    }
  }

  const declineConsent = useCallback(() => {
    setConsent((c) => { if (c) declinedConsent.current.add(consentKey(c)); return c; });
    setConsentOpen(false);
  }, []);

  async function withdrawConsent() {
    if (!consent) return;
    setConsent(await api.withdrawConsent(consent.provider));
    declinedConsent.current.add(consentKey(consent));
  }

  // ---- boot ---------------------------------------------------------------
  useEffect(() => {
    (async () => {
      try {
        setBootDetail('Waking the backend');
        const [health, cfg] = await Promise.all([api.health(), api.getSettings()]);
        setSource(health.source || { name: 'applemail', ready: false });
        setSettings(cfg);
        applyTheme(cfg.theme || 'gold');
        /* A fresh install speaks the Mac's language. Only on a genuinely new
           setup (no address saved, never chosen by hand), so nobody who picked
           English on purpose is switched. */
        let startLang = cfg.ui_language || 'en';
        try {
          const sys = (navigator.language || 'en').slice(0, 2);
          if (!cfg.user_address && startLang === 'en' && ['ko', 'zh', 'ja'].includes(sys)
              && !localStorage.getItem('foolsgold.langChosen')) {
            startLang = sys;
            api.patchSettings({ ui_language: sys }).then(setSettings).catch(() => {});
          }
        } catch { /* storage unavailable: keep the saved language */ }
        setLang(startLang);
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
    try { localStorage.setItem('foolsgold.langChosen', '1'); } catch { /* private mode */ }
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
        bucket: (view === 'muted' || view === 'done' || view === 'dismissed') ? null : bucketFilter,
        search: search.trim() || undefined,
        sort: view === 'all' ? 'date' : (view === 'done' || view === 'dismissed') ? 'decided' : 'priority',
        muted: view === 'muted',
        // Sorted by the decision, newest first: the tick you want back is the
        // one you just made.
        verdict: view === 'done' ? 'done' : view === 'dismissed' ? 'not_relevant' : undefined,
      });
      /* Not `setMail(data.items)`. Reusing the row objects that did not
         change is what lets the memoised rows stay put through a reload --
         see lib/reconcile.js. */
      setMail((prev) => reuseUnchanged(prev, data.items));
      setCounts(data.counts || {});
      setAiStatus(data.ai || { available: true });
      setSyncState(data.sync || null);
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

  /* Keep the open window in step with the mailbox.

     The backend has synced every five minutes since it was written, and the
     window never showed the result: `loadMail` ran on a user action and on
     nothing else. So the app could pull twelve new emails an hour and display
     none of them until someone pressed refresh -- which is the difference
     between an inbox and a screenshot of one.

     Two triggers, because they answer different questions. The interval keeps
     a window left open on a second monitor current. The focus handler covers
     the far more common case: the laptop was shut, the poller never ran, and
     the first thing the user does on coming back is look at it. Polling on an
     interval alone would leave that first look stale for up to five minutes.

     `document.hidden` is checked so a background window does no work at all --
     a hidden tab refetching every ninety seconds is battery spent on pixels
     nobody is looking at. */
  useEffect(() => {
    if (!source.ready) return undefined;
    const tick = () => { if (!document.hidden) loadMail(); };
    const timer = setInterval(tick, 90_000);
    window.addEventListener('focus', tick);
    document.addEventListener('visibilitychange', tick);
    return () => {
      clearInterval(timer);
      window.removeEventListener('focus', tick);
      document.removeEventListener('visibilitychange', tick);
    };
  }, [source.ready, loadMail]);

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

  /* What the list shows.

     The priority view used to hide `noise`, which made it a second opinion
     about what deserved to exist rather than an ordering of what does. Two
     things went wrong with that. The briefing beside it answers "what needs me
     today" and was answering it from the same narrowed set, so the two panes
     said nearly the same thing in different words. And a ranked list that also
     hides things asks the user to trust a judgement they cannot check --
     suppression is the self-concealing case, which is the whole reason the
     exploration quota had to exist.

     Now the division of labour is clean: the BRIEFING is the short answer,
     scoped to the deadline window, and the PRIORITY LIST is every email you
     have not already dealt with, in rank order, with the bucket chips there to
     narrow it when you want that. Nothing is hidden by the app; things are
     only hidden by you, and everything you hid is in Put away.

     Worth noting what this costs: the explored badge meant "the ranker
     suppressed this and is showing it anyway". In a list that suppresses
     nothing it is no longer a gap in a one-way door -- it still marks a row the
     ranker is unsure about, which is useful, but the door it was propping open
     is now simply not shut. The badge stays; its justification changed. */
  const visible = useMemo(() => {
    // The put-away lists are made of dismissed mail by definition, so the
    // filter below -- which exists to hide dismissed mail -- would empty them.
    if (view === 'all' || view === 'done' || view === 'dismissed') return mail;
    return mail.filter((m) =>
      // The row being animated out has to stay mounted until its exit finishes.
      // Filtering on the optimistic verdict alone unmounted it on the same tick
      // the checkbox was ticked, so the completion animation never played --
      // the row simply blinked out of existence.
      m.id === leaving?.id
      || !DISMISSED.has(m.verdict));
  }, [mail, view, leaving]);

  /* `useEvent`, not `useCallback`. This handler reads `mail`, and `mail` is
     replaced by the very click it is handling -- so under `useCallback` it was
     reborn at the exact moment the memoised rows needed it to hold still, and
     all 187 re-rendered underneath the completion animation. */
  const applyFeedback = useEvent(async (id, verdict) => {
    const previous = mail.find((m) => m.id === id)?.verdict ?? null;

    // Completing and dismissing get different exits on purpose. Ticking
    // something off should feel like an accomplishment -- the tick draws, the
    // subject strikes through, the row washes gold and slides away to the
    // right. Muting is a rejection, so it desaturates and shrinks away to the
    // left. Two different feelings for two different meanings.
    const kind = verdict === 'done' ? 'done'
      : verdict === 'not_important' || verdict === 'snoozed' ? 'banish'
      // Un-ticking inside the completed box means the row leaves that box, and
      // it should leave the way anything neutral leaves -- not with the gold
      // wash of completion, and not with the grey collapse of a rejection.
      : verdict === 'not_relevant' ? 'banish'
      : (verdict === null && (view === 'done' || view === 'dismissed')) ? 'restore'
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
      const settle = kind === 'done' ? 800 : kind === 'banish' ? 480 : kind === 'restore' ? 360 : 0;
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
  });

  /* Muting is a decision about a correspondent, so it confirms first and then
     burns the row away rather than sliding it out like a completed task. */
  const askMute = useEvent(async (email) => {
    const address = email.from_address;
    if (!address) return;
    let count = 0;
    try { count = (await api.muteImpact(address)).message_count; } catch { /* offline */ }
    setMuteTarget({ email, count });
  });

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
  const setHighlight = useEvent(async (address, color) => {
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
  });

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
      /* A dialog owns the keyboard. Without this, Enter on a dialog's focused
         button opened a mail row behind it instead of pressing the button --
         found by render check 22, where Enter on "Don't allow" left the
         consent dialog open (and ConfirmMute had the same hole). */
      if (document.querySelector('.modal-scrim')) return;
      if (tag === 'BUTTON' && (e.key === 'Enter' || e.key === ' ')) return;
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
        case 'i': return applyFeedback(cursorId || ids[0], 'not_relevant');
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

  /* Every notice whose condition holds right now, dismissed or not.

     Dismissed ones stay in this list on purpose: it is what the pruning below
     is measured against, and dropping them here would make a closed notice
     look like a condition that had gone away -- so it would come straight
     back on the next render. */
  const liveNotices = useMemo(() => {
    const out = [];
    if (banner) out.push({ id: 'banner', body: banner });
    if (source.ready && source.needs_setup) {
      out.push({
        id: 'needs-address',
        body: (<>
          {t('addAddressHint')}
          <button className="btn ghost" style={{ marginLeft: 'auto', padding: '1px 8px' }}
                  onClick={() => setView('settings')}>{t('openSettings')}</button>
        </>),
      });
    }
    if (!aiStatus.available && !aiStatus.off) {
      /* The reason in the reader's language when the app recognises it, and
         the raw message only when it does not.

         This banner read "AI를 쓸 수 없어 구조적 신호만으로 순위를 매기고
         있습니다. (Ollama has no model called 'qwen3.5:9b'. Pull it first:
         ollama pull qwen3.5:9b)" — a Korean sentence with an English one
         bolted into the middle of it. Third occurrence of the same rule after
         the briefing note and the ranking reason: the backend names what went
         wrong, this file decides how it reads. A failure with no key is one
         nobody anticipated, and showing its message verbatim is the right
         answer for that. */
      const why = aiStatus.detail_key
        ? t(aiStatus.detail_key, aiStatus.detail_vars || {})
        : (aiStatus.detail || '').slice(0, 110);
      out.push({
        id: 'ai-unavailable',
        body: `${t('aiUnavailable')}${why ? ` (${why})` : ''}`,
      });
    }
    if (syncState?.state === 'failing') {
      out.push({
        id: 'sync-failing',
        body: `${t('syncFailing', { arg: String(syncState.consecutive_failures || 1) })}`
          + (syncState.error ? ` — ${syncState.error.slice(0, 120)}` : ''),
      });
    }
    if (syncState?.state === 'frozen') {
      out.push({
        id: 'sync-frozen',
        body: t('syncFrozen', {
          date: shortDate(syncState.newest_received),
          hours: String(Math.round(syncState.frozen_hours || 0)),
          checks: String(syncState.frozen_checks || 0),
        }),
      });
    }
    return out;
  }, [banner, source.ready, source.needs_setup, aiStatus.available, aiStatus.off,
      aiStatus.detail, aiStatus.detail_key, aiStatus.detail_vars, syncState, t]);

  /* Forget a dismissal once its notice stops applying.

     Keyed on the id and nothing else, so the numbers inside `syncFrozen` --
     which tick up on every poll -- do not resurrect a notice the user closed
     two minutes ago. The condition changing back is news; the same condition
     counting higher is not. */
  useEffect(() => {
    setDismissed((prev) => {
      if (prev.size === 0) return prev;
      const live = new Set(liveNotices.map((n) => n.id));
      const next = new Set([...prev].filter((id) => live.has(id)));
      return next.size === prev.size ? prev : next;
    });
  }, [liveNotices]);

  const notices = liveNotices.filter((n) => !dismissed.has(n.id));

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
                putAwayOpen={putAwayOpen} setPutAwayOpen={setPutAwayOpen}
                logoOpen={logoOpen} onLogo={() => setLogoOpen((v) => !v)} />
          <div className="pane-detail">
            {banner && <div className="banner"><span className="dot" />{banner}</div>}
            <Connect source={source} settings={settings} onSettings={setSettings}
                     lang={lang} onLanguage={chooseLanguage}
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
        right={!inSettings && syncing ? <SyncStep /> : null}
      />
      {syncing && <Sweep />}

      <div className="app">
        <Rail view={view} setView={setView} counts={counts} t={t}
              putAwayOpen={putAwayOpen} setPutAwayOpen={setPutAwayOpen}
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
                        consent={consent} onReviewConsent={() => setConsentOpen(true)}
                        onWithdrawConsent={withdrawConsent}
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
                {view !== 'calendar' && <input id="mail-search" className="input" placeholder={t('searchPlaceholder')}
                       value={search} onChange={(e) => setSearch(e.target.value)} />}
                <button className="btn ghost" onClick={sync} disabled={syncing}
                        title={`${t('syncTitle')} (r)`} aria-label={t('syncTitle')}>
                  <RefreshIcon spinning={syncing} />
                </button>
              </div>

              {view !== 'muted' && view !== 'calendar' && view !== 'done' && view !== 'dismissed' && <div className="filters">
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
                                  doneView={view === 'done'}
                                  dismissedView={view === 'dismissed'}
                                  onSelect={openEmail} onFeedback={applyFeedback} onMute={askMute}
                                  onHighlight={setHighlight} />}
                  </>
                )}
              </div>

              {view !== 'calendar' && <div className="keyhint">
                <span><kbd>↑</kbd><kbd>↓</kbd> {t('keyMove')}</span>
                <span><kbd>↵</kbd> {t('keyOpen')}</span>
                <span><kbd>p</kbd> {t('keyPin')}</span>
                <span><kbd>e</kbd> {t('keyDone')}</span>
                <span><kbd>i</kbd> {t('keyDismissed')}</span>
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
              {/* Each of these used to be an unconditional strip. The content
                  is unchanged; what is new is that they are a list, so they
                  can be dismissed one at a time and pruned when they stop
                  being true.

                  Two different sentences for the mailbox on purpose.
                  `failing` means this app is erroring and names the error.
                  `frozen` means this app is working perfectly and the thing it
                  reads has stopped -- not the user's fault, not a bug they can
                  report, and it needs the one instruction that actually fixes
                  it. "Something went wrong" for both would be worse than
                  silence, because it would send them looking in the wrong
                  place. */}
              {notices.map(({ id, body }) => (
                <Notice key={id} id={id} onDismiss={dismissNotice} dismissLabel={t('dismiss')}>
                  {body}
                </Notice>
              ))}

              {selectedId ? (
                <>
                  {/* The way back. Reading an email is a detour from a list, a
                      briefing or a day on the calendar, and until this existed
                      the only way out of the detour was to start another one. */}
                  <div className="detail-back">
                    <button className="btn ghost" onClick={closeEmail}>
                      <BackIcon />
                      {t(view === 'calendar' ? 'backToCalendar'
                        : view === 'priority' ? 'backToChecklist' : 'backToList')}
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
              ) : (
                <div className="scroll">
                  <Digest
                    digest={digest} loading={digestLoading} onRefresh={refreshDigest}
                    selectedId={selectedId} checkedIds={doneIds}
                    onOpen={openEmail} onDone={applyFeedback}
                    onOpenSettings={() => setView('settings')}
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

      <ConsentDialog
        open={consentOpen}
        status={consent}
        lang={lang}
        busy={consentBusy}
        onAllow={allowConsent}
        onDecline={declineConsent}
      />

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

function Rail({ view, setView, counts, logoOpen, onLogo, t, putAwayOpen, setPutAwayOpen }) {
  return (
    <nav className="rail">
      <img className={`logo brand-mark ${logoOpen ? 'open' : ''}`} src="./logo.png"
           alt="Fools Gold — status" title="Status and sync"
           role="button" tabIndex={0}
           onClick={onLogo}
           onKeyDown={(e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); onLogo(); } }} />
      {/* Labels, because three unlabelled glyphs in a column are a guessing
          game -- a sun in particular read as a brightness control. */}
      {VIEWS.map(({ id, key, hintKey, short, icon: Icon }) => (
        <button key={id} className={`rail-item ${view === id ? 'active' : ''}`}
                data-view={id}
                title={`${t(key)} — ${t(hintKey)}`}
                onClick={() => setView(id)}>
          <span className="rail-glyph">
            <Icon />
            {id === 'priority' && counts.action ? <span className="badge" /> : null}
          </span>
          <span className="rail-label">{t(short)}</span>
        </button>
      ))}

      {/* Decisions already made. One folder, three lists. */}
      <button className={`rail-item rail-group ${putAwayOpen ? 'open' : ''} ${
                !putAwayOpen && PUT_AWAY_IDS.includes(view) ? 'active' : ''}`}
              data-view="put-away"
              aria-expanded={putAwayOpen}
              title={t('viewPutAwayHint')}
              onClick={() => setPutAwayOpen((v) => !v)}>
        <span className="rail-glyph"><ArchiveIcon /><ChevronDownIcon /></span>
        <span className="rail-label">{t('railPutAway')}</span>
      </button>
      {putAwayOpen && PUT_AWAY.map(({ id, key, hintKey, short, icon: Icon }) => (
        <button key={id} className={`rail-item rail-child ${view === id ? 'active' : ''}`}
                data-view={id}
                title={`${t(key)} — ${t(hintKey)}`}
                onClick={() => setView(id)}>
          <span className="rail-glyph"><Icon /></span>
          <span className="rail-label">{t(short)}</span>
        </button>
      ))}

      <div className="spacer" />
      <button className={`rail-item ${view === 'settings' ? 'active' : ''}`} title={t('viewSettings')}
              data-view="settings"
              onClick={() => setView('settings')}>
        <span className="rail-glyph"><GearIcon /></span>
        <span className="rail-label">{t('railSettings')}</span>
      </button>
    </nav>
  );
}
