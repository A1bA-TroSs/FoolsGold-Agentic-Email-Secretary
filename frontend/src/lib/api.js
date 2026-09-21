// Every call goes to the local FastAPI process. In dev, Vite proxies /api to it.
const BASE = import.meta.env.DEV ? '' : 'http://127.0.0.1:8765';
/* The same origin the app is served from, for subresources the reading pane
   asks for directly (inline images) rather than through `request`. In dev the
   page is on Vite's port and the backend is not, so a bare relative URL would
   404 inside the mail frame. */
export const API_BASE = BASE || 'http://127.0.0.1:8765';

async function request(path, options = {}) {
  const res = await fetch(BASE + path, {
    headers: { 'Content-Type': 'application/json' },
    ...options,
  });
  const text = await res.text();
  const data = text ? JSON.parse(text) : null;
  if (!res.ok) {
    // `detail` is a string for most errors; a few (compose send) return an
    // object the caller needs to act on, so keep it whole on `err.data`.
    const detail = data?.detail;
    const message = typeof detail === 'string' ? detail : detail?.message;
    const err = new Error(message || `Request failed (${res.status})`);
    err.status = res.status;
    err.data = detail && typeof detail === 'object' ? detail : null;
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

  listMail: ({ bucket, search, sort = 'priority', muted = false, verdict } = {}) => {
    const q = new URLSearchParams({ sort });
    if (bucket) q.set('bucket', bucket);
    if (search) q.set('search', search);
    if (muted) q.set('muted', 'true');
    if (verdict) q.set('verdict', verdict);
    return request(`/api/mail?${q}`);
  },

  rankCategories: () => request('/api/ranking/categories'),

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

  // --- writing mail -----------------------------------------------------
  // `sendDraft` takes a token and nothing else, mirroring the endpoint. There
  // is deliberately no way from here to send content that was not first built
  // and returned by `draft`.
  draft: (body) => request('/api/compose/draft', { method: 'POST', body: JSON.stringify(body) }),
  sendDraft: (token) => request(`/api/compose/${encodeURIComponent(token)}/send`, { method: 'POST' }),
  // One-time password for the address a reply is sent from; the backend
  // checks it against that account's own server before keeping it.
  saveCredentials: (address, password) =>
    request('/api/compose/credentials', { method: 'POST', body: JSON.stringify({ address, password }) }),
  forgetCredentials: (address) =>
    request(`/api/compose/credentials/${encodeURIComponent(address)}`, { method: 'DELETE' }),
  getDraft: (token) => request(`/api/compose/${encodeURIComponent(token)}`),

  transports: () => request('/api/transports'),
  // Signs in and finds the mailbox. Sends nothing, writes nothing.
  testTransport: (name) =>
    request('/api/transports/test', { method: 'POST', body: JSON.stringify({ name: name ?? null }) }),

  getSettings: () => request('/api/settings'),
  patchSettings: (values) => request('/api/settings', { method: 'PATCH', body: JSON.stringify({ values }) }),
  testProvider: () => request('/api/settings/test-provider', { method: 'POST' }),
  copilotStatus: () => request('/api/settings/copilot-status'),
  // What Ollama reports it has. Asked for by the Settings screen only.
  ollamaModels: () => request('/api/ollama/models'),
  // Cloud-AI permission. Granting happens only from the consent dialog.
  aiConsent: (provider) =>
    request(`/api/ai/consent${provider ? `?provider=${encodeURIComponent(provider)}` : ''}`),
  grantConsent: (provider) =>
    request('/api/ai/consent', { method: 'POST', body: JSON.stringify({ provider: provider ?? null }) }),
  withdrawConsent: (provider) =>
    request(`/api/ai/consent${provider ? `?provider=${encodeURIComponent(provider)}` : ''}`, { method: 'DELETE' }),

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
  rescore: () => request('/api/ranking/rescore', { method: 'POST' }),
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

/* macOS setup helpers. Only the packaged/Electron app can do these; in a plain
   browser (the render harness, `npm run dev:ui`) the buttons are not shown. */
export function canRelaunch() {
  return typeof window !== 'undefined' && !!window.foolsgold?.relaunch;
}
export function openFullDiskAccess() {
  return window.foolsgold?.openFullDiskAccess?.();
}
export function relaunchApp() {
  return window.foolsgold?.relaunch?.();
}
