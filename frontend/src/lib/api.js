// Every call goes to the local FastAPI process. In dev, Vite proxies /api to it.
const BASE = import.meta.env.DEV ? '' : 'http://127.0.0.1:8765';

async function request(path, options = {}) {
  const res = await fetch(BASE + path, {
    headers: { 'Content-Type': 'application/json' },
    ...options,
  });
  const text = await res.text();
  const data = text ? JSON.parse(text) : null;
  if (!res.ok) {
    const err = new Error(data?.detail || `Request failed (${res.status})`);
    err.status = res.status;
    throw err;
  }
  return data;
}

export const api = {
  health: () => request('/api/health'),

  sources: () => request('/api/sources'),

  authStatus: () => request('/api/auth/status'),
  login: (email) => request('/api/auth/login', { method: 'POST', body: JSON.stringify({ email }) }),
  logout: () => request('/api/auth/logout', { method: 'POST' }),

  listMail: ({ bucket, search, sort = 'priority', muted = false } = {}) => {
    const q = new URLSearchParams({ sort });
    if (bucket) q.set('bucket', bucket);
    if (search) q.set('search', search);
    if (muted) q.set('muted', 'true');
    return request(`/api/mail?${q}`);
  },

  mutedSenders: () => request('/api/mail/senders/muted'),
  highlightedSenders: () => request('/api/mail/senders/highlighted'),
  highlightSender: (address, color) =>
    request('/api/mail/senders/highlight', { method: 'POST', body: JSON.stringify({ address, color }) }),
  unhighlightSender: (address) =>
    request(`/api/mail/senders/highlight?address=${encodeURIComponent(address)}`, { method: 'DELETE' }),
  muteImpact: (address) => request(`/api/mail/senders/impact?address=${encodeURIComponent(address)}`),
  muteSender: (address) =>
    request('/api/mail/senders/mute', { method: 'POST', body: JSON.stringify({ address }) }),
  unmuteSender: (address) =>
    request(`/api/mail/senders/mute?address=${encodeURIComponent(address)}`, { method: 'DELETE' }),
  getMail: (id) => request(`/api/mail/${encodeURIComponent(id)}`),
  setFeedback: (id, verdict, snoozeHours) =>
    request(`/api/mail/${encodeURIComponent(id)}/feedback`, {
      method: 'POST',
      body: JSON.stringify({ verdict, snooze_hours: snoozeHours ?? null }),
    }),
  clearFeedback: (id) =>
    request(`/api/mail/${encodeURIComponent(id)}/feedback`, { method: 'DELETE' }),
  sync: () => request('/api/mail/sync', { method: 'POST' }),
  rescan: () => request('/api/mail/rescan', { method: 'POST' }),
  digest: (force = false) => request(`/api/mail/digest/today?force=${force}`),

  getSettings: () => request('/api/settings'),
  patchSettings: (values) => request('/api/settings', { method: 'PATCH', body: JSON.stringify({ values }) }),
  testProvider: () => request('/api/settings/test-provider', { method: 'POST' }),
  copilotStatus: () => request('/api/settings/copilot-status'),

  calendarMonth: (year, month, weekStartsOn = 0) =>
    request(`/api/calendar/month?year=${year}&month=${month}&week_starts_on=${weekStartsOn}`),
  calendarAgenda: (day) => request(`/api/calendar/agenda${day ? `?day=${day}` : ''}`),
  addTask: (body) => request('/api/calendar/tasks', { method: 'POST', body: JSON.stringify(body) }),
  patchTask: (id, body) =>
    request(`/api/calendar/tasks/${id}`, { method: 'PATCH', body: JSON.stringify(body) }),
  deleteTask: (id) => request(`/api/calendar/tasks/${id}`, { method: 'DELETE' }),
  setEntryDone: (kind, id, done) =>
    request(`/api/calendar/entries/${kind}/${encodeURIComponent(id)}/done`, {
      method: 'POST', body: JSON.stringify({ done }),
    }),
  removeEntry: (kind, id) =>
    request(`/api/calendar/entries/${kind}/${encodeURIComponent(id)}`, { method: 'DELETE' }),
  removedEntries: () => request('/api/calendar/removed'),
  restoreEntry: (kind, id) =>
    request(`/api/calendar/entries/${kind}/${encodeURIComponent(id)}/restore`, { method: 'POST' }),
  purgeEntry: (kind, id) =>
    request(`/api/calendar/removed/${kind}/${encodeURIComponent(id)}`, { method: 'DELETE' }),

  // --- adaptive ranking -------------------------------------------------
  ranking: () => request('/api/ranking'),
  startLearning: () => request('/api/ranking/epoch', { method: 'POST' }),
  setActionVolume: (targetPerDay) =>
    request('/api/ranking/volume', {
      method: 'POST', body: JSON.stringify({ target_per_day: targetPerDay }),
    }),
  resetLearning: () => request('/api/ranking/reset', { method: 'POST' }),
  explainRanking: (id) => request(`/api/ranking/explain/${encodeURIComponent(id)}`),

  priorities: () => request('/api/priorities'),
  addPriority: (body) => request('/api/priorities', { method: 'POST', body: JSON.stringify(body) }),
  patchPriority: (id, body) => request(`/api/priorities/${id}`, { method: 'PATCH', body: JSON.stringify(body) }),
  deletePriority: (id) => request(`/api/priorities/${id}`, { method: 'DELETE' }),
  suggestions: () => request('/api/priorities/suggestions'),
};

// Electron exposes shell.openExternal; in a plain browser fall back to a tab.
export function openExternal(url) {
  if (window.foolsgold?.openExternal) window.foolsgold.openExternal(url);
  else window.open(url, '_blank', 'noopener');
}
