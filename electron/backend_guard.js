/* Deciding what to do about the port, and saying what actually went wrong.
 *
 * Both halves are pure, and they are here rather than in main.js for the same
 * reason reminders.js is: a decision that can only be exercised by launching
 * the app is a decision nobody tests.
 *
 * ## What this exists to stop
 *
 * The shell spawned the backend unconditionally. If anything already held
 * 127.0.0.1:8765 the new process died on bind -- and under uvicorn 0.52 that
 * is **exit code 3**, not the 1 that older versions returned -- while
 * `waitForBackend()` cheerfully succeeded, because the thing already on the
 * port answered `/api/health`. So the window loaded and worked, an error
 * dialog appeared over the top of it, and every request went to a backend
 * **this build did not start and whose code may be months old**.
 *
 * That is the worst shape a bug can have: the app is running, it looks fine,
 * and the code it is running is not the code on disk. A fix can be written,
 * built, shipped and confirmed against a process that never loaded it.
 */

const APP_NAME = 'Fools Gold';

/** Is this `/api/health` body one of ours? */
function isOurHealth(body) {
  return Boolean(body) && typeof body === 'object' && body.app === APP_NAME;
}

/* What to do, given whatever answered on the port.
 *
 *   null  -- nothing is listening, or it did not answer HTTP
 *   {...} -- the parsed /api/health body
 *   {}    -- something answered but not with our health document
 *
 * Four outcomes, and the interesting one is `adopt`. A backend of ours that
 * reports no pid is one built before this change: we cannot end it, so the
 * choice is between refusing to run and running against code we did not start.
 * It adopts **and says so**, because the failure to avoid is not "the app did
 * not open", it is "the app opened and was silently a version behind".
 */
function decideStart(body) {
  if (body === null || body === undefined) return { action: 'spawn' };
  if (!isOurHealth(body)) return { action: 'refuse', reason: 'foreign' };
  const pid = body.pid;
  if (!Number.isInteger(pid) || pid <= 1) return { action: 'adopt', reason: 'no-pid' };
  if (pid === process.pid) return { action: 'adopt', reason: 'self' };
  return { action: 'replace', pid };
}

/* The dialog text.
 *
 * The old message named one cause -- "the environment is stale or incomplete,
 * run setup.sh" -- for every possible failure. It was wrong here in the way
 * that costs the most: it is confident, actionable, and it sends you to a
 * command that cannot fix what happened. **A hardcoded diagnosis is wrong for
 * every cause but one**, and the user has no way to tell which case they are
 * in, because the real output only exists in a terminal they may not have
 * launched from.
 *
 * So: read what the process actually said, and only offer setup.sh when the
 * output looks like a missing dependency.
 */
function describeExit({ code, stderr = '', port = '8765' }) {
  const tail = lastLines(stderr, 12);
  const said = (re) => re.test(stderr);

  if (said(/address already in use/i) || said(/errno 48|errno 98/i)) {
    return {
      title: 'Another program is using port ' + port,
      body:
        `The backend could not start because something is already listening on ` +
        `127.0.0.1:${port}.\n\n` +
        `Usually that is a Fools Gold backend left behind by a previous run. End it:\n\n` +
        `    lsof -ti tcp:${port} | xargs kill\n\n` +
        `then open Fools Gold again.` + withTail(tail),
    };
  }

  if (said(/ModuleNotFoundError|ImportError|No module named/i)) {
    return {
      title: 'The Python environment is incomplete',
      body:
        `The backend could not import something it needs. Rebuild the environment:\n\n` +
        `    ./scripts/setup.sh` + withTail(tail),
    };
  }

  if (code === 3) {
    return {
      title: 'Fools Gold backend stopped',
      body:
        `The backend loaded but never began serving (exit code 3).\n\n` +
        `That code means the server failed during startup — most often the port ` +
        `was taken, or the database could not be opened.` + withTail(tail),
    };
  }

  return {
    title: 'Fools Gold backend stopped',
    body: `The Python backend exited with code ${code}.` + withTail(tail),
  };
}

function withTail(tail) {
  // Naming the absence matters: "no output" and "output you cannot see" are
  // different problems, and the old dialog made them look the same.
  return tail
    ? `\n\nWhat it said:\n\n${tail}`
    : `\n\nThe backend printed nothing before exiting.`;
}

function lastLines(text, n) {
  return String(text || '')
    .split('\n')
    .filter((line) => line.trim() !== '')
    .slice(-n)
    .join('\n')
    .trim();
}

module.exports = { decideStart, describeExit, isOurHealth, lastLines, APP_NAME };
