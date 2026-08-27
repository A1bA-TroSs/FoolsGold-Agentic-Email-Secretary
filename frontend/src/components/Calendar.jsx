import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { api } from '../lib/api.js';
import { useT } from '../lib/i18n.js';
import { ChevronLeftIcon, ChevronRightIcon, HighlightIcon, MuteIcon, PlusIcon, TrashIcon } from './Icons.jsx';
import HighlightPicker from './HighlightPicker.jsx';
import { Tick } from './Tick.jsx';

/* The month grid.

   Everything about which day is which comes from the backend, which builds the
   grid with real date arithmetic. The alternative -- doing the maths here from
   a JS Date -- is where calendars traditionally go wrong: getMonth() is
   zero-based, month lengths vary, and `new Date(y, m, d)` silently rolls over.
   Here the frontend only ever reads ISO strings it was handed. */

const ISO = /^\d{4}-\d{2}-\d{2}$/;

export function todayIso() {
  // Local calendar day, not UTC. Anyone east of Greenwich in the evening gets
  // tomorrow's date out of toISOString(), which is exactly the kind of
  // off-by-one that makes a planner feel broken.
  const d = new Date();
  const pad = (n) => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
}

function dayNumber(iso) {
  return Number(iso.slice(8, 10));
}

function monthOf(iso) {
  return Number(iso.slice(5, 7));
}

/* Who is filling the calendar.

   Promotional mail is the worst offender for spurious deadlines -- "offer ends
   Friday" is a real date in a message that is not a real commitment -- and it
   arrives in volume, so muting it one row at a time is a chore. This lists the
   senders behind the month's email entries, busiest first, with a mute beside
   each. One pass here clears what would otherwise be a dozen clicks. */
export function sendersInMonth(month) {
  if (!month?.days) return [];
  const byAddress = new Map();
  for (const items of Object.values(month.days)) {
    for (const item of items) {
      // Extracted to-dos count towards their sender too -- muting has to be
      // able to see everything that sender is putting on the calendar.
      if (!item.sender_address) continue;
      const found = byAddress.get(item.sender_address);
      if (found) found.count += 1;
      else byAddress.set(item.sender_address, {
        address: item.sender_address, name: item.sender, count: 1,
        highlight: item.highlight || null,
      });
    }
  }
  return [...byAddress.values()].sort((a, b) => b.count - a.count || a.address.localeCompare(b.address));
}

function SenderList({ senders, onMute, onHighlight }) {
  const t = useT();
  const [open, setOpen] = useState(false);
  const [pickerFor, setPickerFor] = useState(null);
  const box = useRef(null);

  /* The list sits under a six-row grid, so on a short window expanding it
     opens something you cannot see. Bring it into view. */
  useEffect(() => {
    if (open) box.current?.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
  }, [open]);

  if (!senders.length) return null;

  return (
    <div className="cal-senders" ref={box}>
      <button className="cal-senders-toggle" onClick={() => setOpen((v) => !v)}
              aria-expanded={open}>
        {t('calSenders', { n: senders.length })}
        <span className={`caret ${open ? 'open' : ''}`}>▾</span>
      </button>
      {open && (
        <>
          <p className="cal-senders-hint">{t('calSendersHint')}</p>
          <ul className="cal-sender-list">
            {senders.map((s) => (
              <li key={s.address} className={s.highlight ? `hl hl-${s.highlight}` : ''}>
                <span className="who">
                  <b>{s.name || s.address}</b>
                  <span>{s.address}</span>
                </span>
                <span className="count">
                  {t(s.count === 1 ? 'calSenderCountOne' : 'calSenderCount', { n: s.count })}
                </span>
                <span className="sender-actions">
                  <button className={`icon-btn ${s.highlight ? `on hl-${s.highlight}` : ''}`}
                          onClick={() => setPickerFor(pickerFor === s.address ? null : s.address)}
                          title={t('highlightSender')}
                          aria-label={`${t('highlightSender')}: ${s.address}`}>
                    <HighlightIcon />
                  </button>
                  <button className="icon-btn danger" onClick={() => onMute(s)}
                          title={t('muteSenderAction')} aria-label={`${t('muteSenderAction')}: ${s.address}`}>
                    <MuteIcon />
                  </button>
                </span>
                {pickerFor === s.address && (
                  <HighlightPicker
                    current={s.highlight}
                    label={`${t('highlightSender')}: ${s.address}`}
                    onPick={(c) => { setPickerFor(null); onHighlight(s.address, c); }}
                    onClear={() => { setPickerFor(null); onHighlight(s.address, null); }}
                    onClose={() => setPickerFor(null)}
                  />
                )}
              </li>
            ))}
          </ul>
        </>
      )}
    </div>
  );
}

export default function Calendar({
  month, loading, onMove, onToday, onPickDay, selectedDay, onQuickAdd, onMuteSender, onHighlightSender,
}) {
  const t = useT();
  const weekdays = t('weekdaysShort');           // array, Monday-first
  const monthNames = t('monthNames');

  if (!month) {
    return <div className="cal-empty">{loading ? t('calLoading') : t('calEmpty')}</div>;
  }

  // Word order differs: "August 2026" but "2026년 8월". The whole title is
  // a translated pattern rather than a concatenation.
  const title = t('calMonthTitle', { month: monthNames[month.month - 1], year: month.year });

  return (
    <div className={`cal ${loading ? 'busy' : ''}`}>
      <div className="cal-head">
        <button className="btn ghost icon" onClick={() => onMove(-1)} aria-label={t('calPrev')}>
          <ChevronLeftIcon />
        </button>
        <h2 className="cal-title">{title}</h2>
        <button className="btn ghost icon" onClick={() => onMove(1)} aria-label={t('calNext')}>
          <ChevronRightIcon />
        </button>
        <span className="spacer" />
        <button className="btn ghost" onClick={onToday}>{t('calToday')}</button>
      </div>

      <div className="cal-weekdays" role="row">
        {weekdays.map((w, i) => (
          <span key={w} className={i >= 5 ? 'weekend' : ''} role="columnheader">{w}</span>
        ))}
      </div>

      <div className="cal-grid" role="grid">
        {month.weeks.map((week) => week.map((iso) => {
          const items = month.days[iso] || [];
          const open = items.filter((i) => !i.done);
          const outside = monthOf(iso) !== month.month;
          const isToday = iso === month.today;
          return (
            <button
              key={iso} role="gridcell"
              className={[
                'cal-day',
                outside ? 'outside' : '',
                isToday ? 'today' : '',
                iso === selectedDay ? 'picked' : '',
                open.length ? 'has-open' : '',
              ].filter(Boolean).join(' ')}
              onClick={() => onPickDay(iso)}
              aria-label={`${iso}${items.length ? ` — ${items.length}` : ''}`}
            >
              <span className="cal-daynum">{dayNumber(iso)}</span>
              <span className="cal-chips">
                {items.slice(0, 3).map((it) => (
                  <span key={it.key}
                        className={[
                          'cal-chip', it.kind,
                          it.done ? 'done' : '',
                          it.highlight ? `hl-${it.highlight}` : '',
                        ].filter(Boolean).join(' ')}
                        title={it.title}>
                    {it.title}
                  </span>
                ))}
                {items.length > 3 && (
                  <span className="cal-more">{t('calMore', { n: items.length - 3 })}</span>
                )}
              </span>
            </button>
          );
        }))}
      </div>

      <div className="cal-foot">
        <SenderList senders={sendersInMonth(month)} onMute={onMuteSender}
                    onHighlight={onHighlightSender} />
        <button className="btn" onClick={() => onQuickAdd(selectedDay || month.today)}>
          <PlusIcon /> {t('calAddTask')}
        </button>
      </div>
    </div>
  );
}

/* ------------------------------------------------------------------ day -- */

function AddTask({ day, onAdd, onCancel }) {
  const t = useT();
  const [title, setTitle] = useState('');
  const [due, setDue] = useState(day);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const field = useRef(null);

  useEffect(() => { field.current?.focus(); }, []);
  useEffect(() => { setDue(day); }, [day]);

  async function submit(e) {
    e.preventDefault();
    const clean = title.trim();
    if (!clean) { setError(t('calTitleRequired')); return; }
    if (!ISO.test(due)) { setError(t('calDateRequired')); return; }
    setBusy(true); setError('');
    try {
      await onAdd({ title: clean, due_date: due });
      setTitle('');
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <form className="task-add" onSubmit={submit}>
      <input ref={field} className="input" value={title} placeholder={t('calTaskPlaceholder')}
             onChange={(e) => setTitle(e.target.value)} maxLength={300} />
      <input className="input date" type="date" value={due}
             onChange={(e) => setDue(e.target.value)} />
      <button className="btn primary" type="submit" disabled={busy}>{t('calAdd')}</button>
      <button className="btn ghost" type="button" onClick={onCancel}>{t('cancel')}</button>
      {error && <p className="task-error">{error}</p>}
    </form>
  );
}

export function DayPanel({
  day, items, onToggle, onDelete, onMute, onHighlight, onOpenEmail, onAdd,
  adding, onStartAdd, onStopAdd, busyKeys, burningId,
}) {
  const t = useT();
  const [pickerFor, setPickerFor] = useState(null);
  const monthNames = t('monthNames');
  const weekdaysLong = t('weekdaysLong');
  const [confirm, setConfirm] = useState(null);

  const heading = useMemo(() => {
    if (!ISO.test(day || '')) return '';
    const [y, m, d] = day.split('-').map(Number);
    // Weekday from a UTC-noon Date so a timezone offset can never shift it.
    const weekday = new Date(Date.UTC(y, m - 1, d, 12)).getUTCDay();
    return t('calDayHeading', {
      weekday: weekdaysLong[(weekday + 6) % 7],   // JS Sunday=0 -> Monday-first
      day: d,
      month: monthNames[m - 1],
      year: y,
    });
  }, [day, t, monthNames, weekdaysLong]);

  const open = items.filter((i) => !i.done);
  const done = items.filter((i) => i.done);

  return (
    <div className="day-panel">
      <div className="day-head">
        <h2>{heading}</h2>
        <p className="day-sub">
          {open.length
            ? t('calOpenCount', { n: open.length })
            : items.length ? t('calAllDone') : t('calNothingDue')}
        </p>
      </div>

      {adding
        ? <AddTask day={day} onAdd={onAdd} onCancel={onStopAdd} />
        : <button className="btn add-row" onClick={onStartAdd}><PlusIcon /> {t('calAddTask')}</button>}

      <ul className="day-list">
        {[...open, ...done].map((it) => (
          <li key={it.key}
              className={[
                'day-item',
                it.highlight ? `hl hl-${it.highlight}` : '',
                it.done ? 'done' : '',
                busyKeys.has(it.key) ? 'busy' : '',
                it.email_id && it.email_id === burningId ? 'burning' : '',
              ].filter(Boolean).join(' ')}>
            <label className="row-check" title={it.done ? t('markNotDone') : t('markHandled')}>
              <input type="checkbox" checked={it.done}
                     aria-label={`${it.done ? t('markNotDone') : t('markHandled')}: ${it.title}`}
                     onChange={() => onToggle(it, !it.done)} />
              <span className="box"><Tick /></span>
              <span className="ripple" />
            </label>

            <div className="day-body">
              {/* What matters is whether there is a message behind this, not
                  whether the row is a task or a bare deadline. A to-do read out
                  of an email is still a to-do -- it just has somewhere to go
                  back to when you cannot remember the details. */}
              {it.email_id ? (
                <button className="day-title link" onClick={() => onOpenEmail(it.email_id)}>
                  {it.title}
                </button>
              ) : (
                <span className="day-title">{it.title}</span>
              )}
              <span className="day-meta">
                <span className={`tag ${it.email_id ? 'fyi' : 'task'}`}>
                  {it.email_id ? t('calFromMail') : t('calOwnTask')}
                </span>
                {it.sender && <span className="day-sender">· {it.sender}</span>}
              </span>
            </div>

            {confirm === it.key ? (
              <span className="day-confirm">
                <button className="btn danger sm" onClick={() => { setConfirm(null); onDelete(it); }}>
                  {t('calDelete')}
                </button>
                <button className="btn ghost sm" onClick={() => setConfirm(null)}>{t('cancel')}</button>
              </span>
            ) : (
              <span className="day-actions">
                {/* Removing one date and silencing a correspondent are different
                    sizes of decision, so they are different buttons. Mute has
                    its own confirmation dialog and does not need this one. */}
                {it.sender_address && (
                  <button className={`icon-btn ${it.highlight ? `on hl-${it.highlight}` : ''}`}
                          onClick={() => setPickerFor(pickerFor === it.key ? null : it.key)}
                          title={t('highlightSender')}
                          aria-label={`${t('highlightSender')}: ${it.sender || it.sender_address}`}>
                    <HighlightIcon />
                  </button>
                )}
                {it.sender_address && (
                  <button className="icon-btn danger" onClick={() => onMute(it)}
                          title={t('muteSenderAction')}
                          aria-label={`${t('muteSenderAction')}: ${it.sender || it.sender_address}`}>
                    <MuteIcon />
                  </button>
                )}
                <button className="icon-btn danger" onClick={() => setConfirm(it.key)}
                        title={it.email_id ? t('calRemoveFromCalendar') : t('calDeleteTask')}
                        aria-label={it.email_id ? t('calRemoveFromCalendar') : t('calDeleteTask')}>
                  <TrashIcon />
                </button>
              </span>
            )}

            {pickerFor === it.key && (
              <HighlightPicker
                current={it.highlight}
                label={`${t('highlightSender')}: ${it.sender || it.sender_address}`}
                onPick={(c) => { setPickerFor(null); onHighlight(it.sender_address, c); }}
                onClear={() => { setPickerFor(null); onHighlight(it.sender_address, null); }}
                onClose={() => setPickerFor(null)}
              />
            )}
          </li>
        ))}
      </ul>

      {!items.length && !adding && <p className="empty soft">{t('calNothingDue')}</p>}
      {confirm && items.find((i) => i.key === confirm)?.email_id && (
        <p className="day-hint">{t('calRemoveHint')}</p>
      )}
    </div>
  );
}

/* Shared state for the calendar view: which month is loaded, which day is
   picked, and the optimistic tick that has to feel instant. */
export function useCalendar(refreshMail, enabled = true) {
  const start = todayIso();
  const [cursor, setCursor] = useState({ year: Number(start.slice(0, 4)), month: Number(start.slice(5, 7)) });
  const [month, setMonth] = useState(null);
  const [loading, setLoading] = useState(false);
  const [day, setDay] = useState(start);
  const [busyKeys, setBusyKeys] = useState(new Set());
  const [error, setError] = useState('');

  const load = useCallback(async (year, monthNumber) => {
    setLoading(true);
    try {
      setMonth(await api.calendarMonth(year, monthNumber));
      setError('');
    } catch (e) {
      setError(e.message);
    } finally {
      setLoading(false);
    }
  }, []);

  // Nothing is fetched until the calendar is actually on screen, so the four
  // other views cost nothing extra.
  useEffect(() => { if (enabled) load(cursor.year, cursor.month); }, [enabled, cursor, load]);

  const move = (delta) => setCursor(({ year, month: m }) => {
    const index = year * 12 + (m - 1) + delta;
    return { year: Math.floor(index / 12), month: (index % 12) + 1 };
  });

  const goToday = () => {
    const iso = todayIso();
    setCursor({ year: Number(iso.slice(0, 4)), month: Number(iso.slice(5, 7)) });
    setDay(iso);
  };

  /* Jumping to a day in a neighbouring month should page the grid there too,
     otherwise picking "1 Sep" from August's trailing row selects a day you can
     no longer see. */
  const pickDay = (iso) => {
    setDay(iso);
    const y = Number(iso.slice(0, 4));
    const m = Number(iso.slice(5, 7));
    if (y !== cursor.year || m !== cursor.month) setCursor({ year: y, month: m });
  };

  const mark = (key, on) => setBusyKeys((prev) => {
    const next = new Set(prev);
    if (on) next.add(key); else next.delete(key);
    return next;
  });

  const items = month?.days?.[day] || [];

  async function run(key, fn) {
    mark(key, true);
    try {
      await fn();
      await load(cursor.year, cursor.month);
      refreshMail?.();
      setError('');
    } catch (e) {
      setError(e.message);
    } finally {
      mark(key, false);
    }
  }

  return {
    month, loading, error, day, items, busyKeys, cursor,
    move, goToday, pickDay, reload: () => load(cursor.year, cursor.month),
    toggle: (entry, done) => run(entry.key, () => api.setEntryDone(entry.kind, entry.id, done)),
    remove: (entry) => run(entry.key, () => api.removeEntry(entry.kind, entry.id)),
    addTask: async (body) => {
      await api.addTask(body);
      // A task can be filed on a day outside the month on screen. Follow it
      // there, and reload explicitly when the month has not changed -- setting
      // the cursor to the value it already holds does not re-run the effect.
      const y = Number(body.due_date.slice(0, 4));
      const m = Number(body.due_date.slice(5, 7));
      setDay(body.due_date);
      if (y !== cursor.year || m !== cursor.month) setCursor({ year: y, month: m });
      else await load(cursor.year, cursor.month);
    },
  };
}
