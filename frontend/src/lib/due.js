/* How a due date is described, in one place.

   The priority list and the briefing each grew their own copy of this, and
   they drifted: the list stopped showing a chip once a deadline was three
   weeks past, while the briefing happily announced "496d overdue" and marked
   it urgent. A deadline that old is not a reminder, it is history.

   So there is one rule and one function. Past the horizon a deadline stops
   being spoken about at all -- it stays on the calendar, on the day it was
   due, because that is a record of what happened; it just no longer claims
   anything about today. */

// A month. After that, on any ordinary reading, the thing is over: either it
// was done, or it was missed and the world moved on. Shouting about it daily
// only teaches you to ignore the app.
export const FORGET_AFTER_DAYS = 30;

export function daysUntil(deadline, now = new Date()) {
  if (!deadline) return null;
  const today = new Date(now);
  today.setHours(0, 0, 0, 0);
  const due = new Date(`${deadline}T00:00:00`);
  if (Number.isNaN(due.getTime())) return null;
  return Math.round((due - today) / 86400000);
}

export function isForgotten(deadline, now = new Date()) {
  const days = daysUntil(deadline, now);
  return days !== null && days < -FORGET_AFTER_DAYS;
}

/**
 * @returns {{label: string, urgent: boolean} | null} null when there is nothing
 *          worth saying -- no date, or one too old to mean anything.
 */
export function dueChip(deadline, t, now = new Date()) {
  const days = daysUntil(deadline, now);
  if (days === null) return null;
  if (days < -FORGET_AFTER_DAYS) return null;
  if (days < 0) return { label: t('overdueDays', { n: -days }), urgent: true };
  if (days === 0) return { label: t('dueToday'), urgent: true };
  if (days === 1) return { label: t('dueTomorrow'), urgent: true };
  if (days <= 7) return { label: t('dueInDays', { n: days }), urgent: false };
  return { label: t('dueOn', { date: deadline.slice(5) }), urgent: false };
}
