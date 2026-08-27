/* Scheduling decisions for the daily checklist reminders.
 *
 * Kept out of main.js and free of any Electron import so it can be tested with
 * plain node. The rules are small but every one of them is a bug if it is
 * wrong: firing twice, firing at 3am because the laptop was shut, or silently
 * never firing because a clock string was malformed.
 */

// If the machine was asleep at the scheduled minute, still deliver -- but only
// while the reminder is plausibly useful. A 9am checklist arriving at 9pm is
// noise, and worse, it buries the 9pm one.
const GRACE_MINUTES = 240;

const DEFAULTS = { morning: 9 * 60, evening: 21 * 60 };

/** "09:30" -> 570. Anything malformed falls back rather than throwing, so a
 *  hand-edited settings row cannot stop reminders entirely. */
function parseClock(value, fallbackMinutes) {
  const match = /^(\d{1,2}):(\d{2})$/.exec(String(value ?? '').trim());
  if (!match) return fallbackMinutes;
  const hours = Number(match[1]);
  const minutes = Number(match[2]);
  if (hours > 23 || minutes > 59) return fallbackMinutes;
  return hours * 60 + minutes;
}

/** Local calendar day. Not toISOString(), which is UTC and would roll the day
 *  over at the wrong moment for everyone not on GMT. */
function localDateKey(now) {
  const pad = (n) => String(n).padStart(2, '0');
  return `${now.getFullYear()}-${pad(now.getMonth() + 1)}-${pad(now.getDate())}`;
}

/**
 * Which slots should fire right now.
 *
 * @param {object} settings  the backend's settings blob
 * @param {Date}   now
 * @param {Map}    sent      slot name -> date key it last fired on (mutated)
 * @returns {string[]} slot names to deliver
 */
function dueSlots(settings, now, sent) {
  if (String(settings?.notify_enabled) !== 'true') return [];

  const today = localDateKey(now);
  const minutesNow = now.getHours() * 60 + now.getMinutes();
  const slots = [
    ['morning', parseClock(settings.notify_morning, DEFAULTS.morning)],
    ['evening', parseClock(settings.notify_evening, DEFAULTS.evening)],
  ];

  const due = [];
  for (const [name, at] of slots) {
    if (sent.get(name) === today) continue;      // already fired today
    const late = minutesNow - at;
    if (late < 0) continue;                      // not yet
    // Claim the slot either way. Too late to be useful still counts as spent,
    // so it cannot ambush the user at an odd hour later in the evening.
    sent.set(name, today);
    if (late <= GRACE_MINUTES) due.push(name);
  }
  return due;
}

/** The notification text, or null when there is nothing worth interrupting for. */
function reminderBody(agenda, max = 4) {
  const open = agenda?.open || [];
  const overdue = agenda?.overdue || [];
  if (!open.length && !overdue.length) return null;

  const lines = open.slice(0, max).map((item) => `• ${item.title}`);
  if (open.length > max) lines.push(`• …and ${open.length - max} more`);
  if (overdue.length) lines.push(`${overdue.length} overdue`);
  return lines.join('\n');
}

module.exports = { GRACE_MINUTES, dueSlots, parseClock, localDateKey, reminderBody };
