/* node --test electron/reminders.test.js */
const test = require('node:test');
const assert = require('node:assert');
const { dueSlots, parseClock, localDateKey, reminderBody, GRACE_MINUTES } = require('./reminders.js');

const ON = { notify_enabled: 'true', notify_morning: '09:00', notify_evening: '21:00' };
const at = (h, m = 0, day = 27) => new Date(2026, 7, day, h, m, 0);

test('parseClock reads a normal time', () => {
  assert.equal(parseClock('09:00', 0), 540);
  assert.equal(parseClock('21:30', 0), 1290);
  assert.equal(parseClock('7:05', 0), 425);
  assert.equal(parseClock('00:00', 999), 0);
  assert.equal(parseClock('23:59', 0), 1439);
});

test('parseClock falls back rather than throwing on rubbish', () => {
  for (const bad of ['', null, undefined, 'nine', '9', '25:00', '09:60', '09:0', {}]) {
    assert.equal(parseClock(bad, 540), 540, `input: ${JSON.stringify(bad)}`);
  }
});

test('localDateKey uses the local day, not UTC', () => {
  // 23:30 local on the 27th is already the 28th in UTC east of Greenwich, and
  // still the 27th west of it. The key must say 27 either way.
  assert.equal(localDateKey(new Date(2026, 7, 27, 23, 30)), '2026-08-27');
  assert.equal(localDateKey(new Date(2026, 7, 27, 0, 15)), '2026-08-27');
  assert.equal(localDateKey(new Date(2026, 0, 5, 12)), '2026-01-05');
});

test('nothing fires before its time', () => {
  assert.deepEqual(dueSlots(ON, at(8, 59), new Map()), []);
});

test('the morning slot fires at its minute', () => {
  assert.deepEqual(dueSlots(ON, at(9, 0), new Map()), ['morning']);
});

test('a slot fires once per day, not once per tick', () => {
  const sent = new Map();
  assert.deepEqual(dueSlots(ON, at(9, 0), sent), ['morning']);
  assert.deepEqual(dueSlots(ON, at(9, 1), sent), []);
  assert.deepEqual(dueSlots(ON, at(9, 30), sent), []);
});

test('the next day starts clean', () => {
  const sent = new Map();
  dueSlots(ON, at(9, 0, 27), sent);
  assert.deepEqual(dueSlots(ON, at(9, 0, 28), sent), ['morning']);
});

test('waking up late still delivers, within the grace window', () => {
  const sent = new Map();
  assert.deepEqual(dueSlots(ON, at(12, 30), sent), ['morning'], 'three and a half hours late');
});

test('waking up much later delivers nothing but still spends the slot', () => {
  const sent = new Map();
  assert.deepEqual(dueSlots(ON, at(20, 0), sent), [], 'eleven hours late is noise');
  assert.equal(sent.get('morning'), '2026-08-27', 'and must not ambush later');
});

test('the grace boundary is inclusive on the useful side', () => {
  const edge = 9 * 60 + GRACE_MINUTES;
  assert.deepEqual(dueSlots(ON, at(Math.floor(edge / 60), edge % 60), new Map()), ['morning']);
  assert.deepEqual(dueSlots(ON, at(Math.floor((edge + 1) / 60), (edge + 1) % 60), new Map()), []);
});

test('opening the app at night fires the evening slot and skips the stale morning one', () => {
  const sent = new Map();
  assert.deepEqual(dueSlots(ON, at(21, 5), sent), ['evening']);
  assert.equal(sent.get('morning'), '2026-08-27');
});

test('reminders can be turned off', () => {
  assert.deepEqual(dueSlots({ ...ON, notify_enabled: 'false' }, at(9, 0), new Map()), []);
  assert.deepEqual(dueSlots({}, at(9, 0), new Map()), []);
  assert.deepEqual(dueSlots(null, at(9, 0), new Map()), []);
});

test('custom times are honoured', () => {
  const custom = { ...ON, notify_morning: '06:45', notify_evening: '22:15' };
  assert.deepEqual(dueSlots(custom, at(6, 45), new Map()), ['morning']);
  assert.deepEqual(dueSlots(custom, at(6, 44), new Map()), []);
  assert.deepEqual(dueSlots(custom, at(22, 15), new Map()), ['evening']);
});

test('two slots at the same minute both fire', () => {
  const same = { ...ON, notify_morning: '09:00', notify_evening: '09:00' };
  assert.deepEqual(dueSlots(same, at(9, 0), new Map()), ['morning', 'evening']);
});

test('an empty day produces no notification at all', () => {
  assert.equal(reminderBody({ open: [], overdue: [], done: [] }), null);
  assert.equal(reminderBody({}), null);
  assert.equal(reminderBody(null), null);
});

test('the body lists the day and counts the rest', () => {
  const body = reminderBody({
    open: [1, 2, 3, 4, 5, 6].map((n) => ({ title: `Task ${n}` })),
    overdue: [{ title: 'Old' }],
  });
  assert.match(body, /• Task 1/);
  assert.match(body, /• Task 4/);
  assert.doesNotMatch(body, /• Task 5\n/);
  assert.match(body, /and 2 more/);
  assert.match(body, /1 overdue/);
});

test('a day with only overdue work still says something', () => {
  const body = reminderBody({ open: [], overdue: [{ title: 'Old' }, { title: 'Older' }] });
  assert.equal(body, '2 overdue');
});
