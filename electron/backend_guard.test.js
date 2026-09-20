/* Tests for the port decision and the failure message.
 *
 * Both were written after a real report: "The Python backend exited with code
 * 3", shown on top of a window that was working, with a suggested fix
 * (`setup.sh`) that could not have helped. Every case below is one of the
 * things that actually happened or was one step away from happening.
 */
const test = require('node:test');
const assert = require('node:assert');
const { decideStart, describeExit, isOurHealth, lastLines } = require('./backend_guard.js');

// ---------------------------------------------------------------- decideStart

test('nothing on the port: start one', () => {
  assert.deepStrictEqual(decideStart(null), { action: 'spawn' });
  assert.deepStrictEqual(decideStart(undefined), { action: 'spawn' });
});

test('our backend, reporting its pid: replace it', () => {
  assert.deepStrictEqual(
    decideStart({ app: 'Fools Gold', version: '1.0.0-dev', pid: 4242 }),
    { action: 'replace', pid: 4242 },
  );
});

test('replacing, not adopting, is the point', () => {
  // Adopting a leftover backend is how a build gets confirmed against code it
  // never loaded. The only reason to adopt is being unable to end it.
  const d = decideStart({ app: 'Fools Gold', pid: 900 });
  assert.strictEqual(d.action, 'replace');
});

test('our backend without a pid: adopt, because it cannot be ended', () => {
  const d = decideStart({ app: 'Fools Gold', version: '1.0.0-dev' });
  assert.strictEqual(d.action, 'adopt');
  assert.strictEqual(d.reason, 'no-pid');
});

test('a pid that is not a real pid is not trusted', () => {
  for (const pid of [0, 1, -5, 1.5, '4242', null]) {
    assert.strictEqual(decideStart({ app: 'Fools Gold', pid }).action, 'adopt',
      `pid ${JSON.stringify(pid)} should not be signalled`);
  }
});

test('someone else on the port: refuse, never kill', () => {
  // The pid in a foreign health document is an arbitrary number from an
  // arbitrary program. Signalling it would be the app killing a stranger.
  assert.deepStrictEqual(decideStart({ app: 'Grafana', pid: 77 }),
    { action: 'refuse', reason: 'foreign' });
  assert.deepStrictEqual(decideStart({}), { action: 'refuse', reason: 'foreign' });
  assert.deepStrictEqual(decideStart('<html>'), { action: 'refuse', reason: 'foreign' });
});

test('isOurHealth goes by the app name, not by answering 200', () => {
  assert.ok(isOurHealth({ app: 'Fools Gold' }));
  assert.ok(!isOurHealth({ status: 'ok' }));
  assert.ok(!isOurHealth(null));
});

// --------------------------------------------------------------- describeExit

test('address already in use is named, with the way out', () => {
  const { title, body } = describeExit({
    code: 3,
    port: '8765',
    stderr: "ERROR:    [Errno 48] error while attempting to bind on address ('127.0.0.1', 8765): address already in use",
  });
  assert.match(title, /port 8765/);
  assert.match(body, /lsof -ti tcp:8765/);
  // The old message sent every failure here. It must not appear for this one.
  assert.doesNotMatch(body, /setup\.sh/);
});

test('exit 3 is address-in-use under uvicorn 0.52 and exit 1 under 0.46', () => {
  // Which is why the message is chosen by what the process SAID, not by the
  // code: the same failure changed its number when a dependency was upgraded.
  const three = describeExit({ code: 3, stderr: 'address already in use' });
  const one = describeExit({ code: 1, stderr: 'address already in use' });
  assert.strictEqual(three.title, one.title);
});

test('a missing dependency is the one case that earns setup.sh', () => {
  const { title, body } = describeExit({
    code: 1, stderr: "ModuleNotFoundError: No module named 'uvicorn'",
  });
  assert.match(title, /environment is incomplete/i);
  assert.match(body, /setup\.sh/);
});

test('an unexplained exit 3 says what the code means and shows the output', () => {
  const { body } = describeExit({ code: 3, stderr: 'sqlite3.OperationalError: database is locked' });
  assert.match(body, /never began serving/);
  assert.match(body, /database is locked/);
  assert.doesNotMatch(body, /setup\.sh/);
});

test('silence is reported as silence', () => {
  // "no output" and "output you cannot see" were indistinguishable before.
  const { body } = describeExit({ code: 9, stderr: '' });
  assert.match(body, /printed nothing/);
});

test('the tail is bounded and blank lines are dropped', () => {
  const noisy = Array.from({ length: 40 }, (_, i) => `line ${i}`).join('\n\n');
  const out = lastLines(noisy, 12);
  assert.strictEqual(out.split('\n').length, 12);
  assert.match(out, /line 39$/);
});
