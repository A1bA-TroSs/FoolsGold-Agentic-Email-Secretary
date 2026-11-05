/* Fools Gold desktop shell.
 *
 * A real window, not a browser tab: fixed default size, its own icon, hidden
 * title bar in the Obsidian style. It owns the Python backend's lifetime, so
 * quitting the app actually stops the process holding your mail cache.
 */
const { app, BrowserWindow, Notification, shell, ipcMain, dialog } = require('electron');
const { spawn } = require('child_process');
const path = require('path');
const http = require('http');
const fs = require('fs');
const { decideStart, describeExit } = require('./backend_guard.js');

const PORT = process.env.FOOLSGOLD_PORT || '8765';
const BACKEND_URL = `http://127.0.0.1:${PORT}`;
const DEV = process.env.FOOLSGOLD_DEV === '1';
const VITE_URL = 'http://localhost:5173';

let backend = null;
// Kept so the failure dialog can say what the process actually printed.
// Without it the only copy of the real reason is in a terminal the user
// may never have had.
let backendStderr = '';
// True when the backend on the port is one we did not start. Nothing may
// SIGTERM it on quit: it is not ours to end.
let adopted = false;
let win = null;
let reminderTimer = null;
const remindersSent = new Map();   // slot name -> the date it last fired on

// --------------------------------------------------------------------------

function backendDir() {
  // Packaged builds carry backend/ in resources; in dev it sits beside us.
  const packaged = path.join(process.resourcesPath || '', 'backend');
  return app.isPackaged && fs.existsSync(packaged) ? packaged : path.join(__dirname, '..', 'backend');
}

/* What to run, and where the UI is, in a packaged app.

   `npm run dist` had never been run, and reading what it would have produced
   found two things that would each have shipped a broken app on its own:

   1. `backend/.venv` went into the bundle whole, and its python is a symlink to
      `/opt/homebrew/opt/python@3.13/bin/python3.13`. **The app would have run
      on the developer's Mac and on no other.** A packaged build now runs a
      frozen backend (PyInstaller, scripts/build_backend.sh) that carries its
      own interpreter and is signed with everything else.

   2. The UI lives inside app.asar, which the Python server cannot read, so the
      window it opens would have been a 404. It is copied out as an extra
      resource and the backend is told where.

   Development is unchanged: `npm start` still runs `python -m app.main` from
   the venv. */
function frozenBackend() {
  if (!app.isPackaged) return null;
  const bin = path.join(process.resourcesPath, 'backend', 'foolsgold-backend', 'foolsgold-backend');
  return fs.existsSync(bin) ? bin : null;
}

function uiDir() {
  if (!app.isPackaged) return undefined;          // dev: the backend finds it itself
  const dir = path.join(process.resourcesPath, 'ui');
  return fs.existsSync(dir) ? dir : undefined;
}

function pythonBin() {
  if (process.env.FOOLSGOLD_PYTHON) return process.env.FOOLSGOLD_PYTHON;
  const venv = path.join(backendDir(), '.venv', 'bin', 'python');
  if (fs.existsSync(venv)) return venv;

  // Deliberately NOT falling back to `python3`. On macOS that is 3.9, which is
  // too old for this backend, and the failure would surface as an unrelated
  // syntax error deep in FastAPI rather than "you skipped setup".
  return null;
}

function startBackend() {
  const cwd = backendDir();
  const frozen = frozenBackend();
  const python = frozen ? null : pythonBin();
  if (frozen === null && python === null) {
    dialog.showErrorBox(
      'Fools Gold is not set up yet',
      'No Python environment found at backend/.venv.\n\n' +
      'Run this once from the repo root:\n\n' +
      '    ./scripts/setup.sh\n\n' +
      'It needs Python 3.11 or newer — macOS ships 3.9, which is too old. ' +
      'The script checks for you and says what to install.'
    );
    app.quit();
    return;
  }
  backendStderr = '';
  const env = { ...process.env, FOOLSGOLD_PORT: PORT, PYTHONUNBUFFERED: '1' };
  const ui = uiDir();
  if (ui) env.FOOLSGOLD_UI_DIR = ui;
  backend = frozen
    ? spawn(frozen, [], { cwd: path.dirname(frozen), env, stdio: ['ignore', 'pipe', 'pipe'] })
    : spawn(python, ['-m', 'app.main'], { cwd, env, stdio: ['ignore', 'pipe', 'pipe'] });
  backend.stdout.on('data', (d) => process.stdout.write(`[backend] ${d}`));
  backend.stderr.on('data', (d) => {
    process.stderr.write(`[backend] ${d}`);
    // Bounded: a backend that fails in a loop must not grow the main process.
    backendStderr = (backendStderr + d).slice(-8000);
  });
  // Without this an ENOENT (the venv python vanished between the fs.existsSync
  // above and the spawn) is an unhandled 'error' event, which takes down the
  // main process rather than showing a dialog.
  backend.on('error', (err) => {
    backend = null;
    if (!app.isQuitting) {
      dialog.showErrorBox('Fools Gold could not start its backend', String(err.message));
    }
  });
  backend.on('exit', (code) => {
    backend = null;
    if (code && code !== 0 && !app.isQuitting) {
      const { title, body } = describeExit({ code, stderr: backendStderr, port: PORT });
      dialog.showErrorBox(title, body);
    }
  });
}

/* What is already on the port, if anything.
 *
 * Resolves to the parsed /api/health body, `{}` when something answered but
 * not with our document, or null when nothing is there. It never rejects:
 * "nothing is listening" is the normal case, not an error.
 */
function probeBackend(timeoutMs = 1200) {
  return new Promise((resolve) => {
    const req = http.get(`${BACKEND_URL}/api/health`, (res) => {
      let raw = '';
      res.setEncoding('utf8');
      res.on('data', (chunk) => { raw += chunk; });
      res.on('end', () => {
        if (res.statusCode !== 200) return resolve({});
        try { resolve(JSON.parse(raw)); } catch { resolve({}); }
      });
    });
    req.setTimeout(timeoutMs, () => { req.destroy(); resolve(null); });
    req.on('error', () => resolve(null));
  });
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

/* End a backend of ours that nothing owns, and wait for the port to free.
 *
 * Only ever called with a pid this app read out of its OWN health document, so
 * it cannot be some unrelated process that happens to hold the port -- that
 * case is refused, not killed. SIGTERM first because the backend closes the
 * database on the way out; SIGKILL only if it will not go.
 */
async function reclaimPort(pid) {
  // The pid came from a health response a moment ago, so in principle the
  // process could have exited and the number been reused in between. The
  // consequence is bounded: the port is re-probed after every signal, and if
  // it is still answering we stop and say so rather than signalling again.
  const gone = async () => (await probeBackend(600)) === null;
  for (const signal of ['SIGTERM', 'SIGKILL']) {
    try { process.kill(pid, signal); } catch { return await gone(); }
    for (let i = 0; i < 12; i += 1) {
      await sleep(250);
      if (await gone()) return true;
    }
  }
  return false;
}

/* Decide, then act. Returns false when the app must not continue.
 *
 * The old code spawned unconditionally, which is why a leftover backend
 * produced an error dialog on top of a working window -- the window being
 * served by the leftover.
 */
async function ensureBackend() {
  const decision = decideStart(await probeBackend());

  if (decision.action === 'refuse') {
    dialog.showErrorBox(
      `Port ${PORT} is already in use`,
      `Something other than Fools Gold is listening on 127.0.0.1:${PORT}, so the ` +
      `backend cannot start.\n\nFind it with:\n\n    lsof -i tcp:${PORT}\n\n` +
      `Quit that program, or set FOOLSGOLD_PORT to a free port and launch again.`
    );
    return false;
  }

  if (decision.action === 'adopt') {
    // Running, ours, and unkillable from here. The app works -- but against
    // code this build did not start, which is the one thing that must never
    // be silent.
    adopted = true;
    dialog.showErrorBox(
      'An older Fools Gold backend is already running',
      `A Fools Gold backend from an earlier build is on 127.0.0.1:${PORT}, and it ` +
      `cannot be ended automatically because it does not report its process id.\n\n` +
      `The app will use it, so what you see may not include recent changes. To ` +
      `switch to this build, quit Fools Gold, run:\n\n` +
      `    lsof -ti tcp:${PORT} | xargs kill\n\nand open it again.`
    );
    return true;
  }

  if (decision.action === 'replace') {
    const freed = await reclaimPort(decision.pid);
    if (!freed) {
      dialog.showErrorBox(
        'A previous Fools Gold backend will not stop',
        `A Fools Gold backend (pid ${decision.pid}) is holding 127.0.0.1:${PORT} and ` +
        `did not stop when asked.\n\nEnd it by hand:\n\n    kill -9 ${decision.pid}\n\n` +
        `then open Fools Gold again.`
      );
      return false;
    }
  }

  startBackend();
  return backend !== null;
}

function waitForBackend(timeoutMs = 30000) {
  const deadline = Date.now() + timeoutMs;
  return new Promise((resolve, reject) => {
    const probe = () => {
      http.get(`${BACKEND_URL}/api/health`, (res) => {
        res.resume();
        res.statusCode === 200 ? resolve() : retry();
      }).on('error', retry);
    };
    const retry = () => {
      if (Date.now() > deadline) reject(new Error('Backend did not start in time.'));
      else setTimeout(probe, 400);
    };
    probe();
  });
}

// --------------------------------------------------------------------------
// Daily checklist reminders
//
// A ticker, not two setTimeouts. A timeout scheduled twelve hours out does not
// survive the laptop being shut, and macOS gives no guarantee about when a
// slept timer fires -- so instead we wake every minute, ask what time it is,
// and decide. Each slot is allowed to fire once per calendar day, tracked by
// date so that a fire at 09:00 cannot repeat at 09:01.
// --------------------------------------------------------------------------

const REMINDER_TICK_MS = 60 * 1000;

const { dueSlots, reminderBody } = require('./reminders.js');

function getJson(pathname) {
  return new Promise((resolve, reject) => {
    http.get(`${BACKEND_URL}${pathname}`, (res) => {
      let body = '';
      res.setEncoding('utf8');
      res.on('data', (chunk) => { body += chunk; });
      res.on('end', () => {
        if (res.statusCode !== 200) return reject(new Error(`HTTP ${res.statusCode}`));
        try { resolve(JSON.parse(body)); } catch (err) { reject(err); }
      });
    }).on('error', reject);
  });
}

async function maybeRemind() {
  if (!Notification.isSupported()) return;

  let settings;
  try {
    settings = await getJson('/api/settings');
  } catch {
    return;                       // backend still starting, or already stopped
  }

  // dueSlots claims each slot as it returns it, so a slow agenda request
  // cannot let the next tick fire the same reminder twice.
  const slots = dueSlots(settings, new Date(), remindersSent);
  if (!slots.length) return;

  let agenda;
  try {
    agenda = await getJson('/api/calendar/agenda');
  } catch {
    return;                       // next reminder will try again
  }
  const body = reminderBody(agenda);
  if (!body) return;              // say nothing rather than "nothing to do"

  for (const slot of slots) {
    const notification = new Notification({
      title: slot === 'morning' ? 'Today in Fools Gold' : 'Still on your list',
      body,
      silent: false,
    });
    notification.on('click', () => {
      if (win) { if (win.isMinimized()) win.restore(); win.show(); win.focus(); }
    });
    notification.show();
  }
}

function startReminders() {
  if (reminderTimer) return;
  reminderTimer = setInterval(() => { maybeRemind().catch(() => {}); }, REMINDER_TICK_MS);
  maybeRemind().catch(() => {});
}

// --------------------------------------------------------------------------

function createWindow() {
  win = new BrowserWindow({
    width: 1120,
    height: 760,
    minWidth: 820,
    minHeight: 520,
    show: false,
    title: "Fools Gold",
    icon: path.join(__dirname, '..', 'assets', 'logo.png'),
    backgroundColor: '#FAF6EC',          // ivory, so first paint is never white flash
    // Obsidian-style chrome. The traffic lights are positioned to sit inside the
    // app's own 38px title strip; previously they floated over the icon rail and
    // the list header, covering the logo and colliding with the view title.
    titleBarStyle: 'hiddenInset',
    trafficLightPosition: { x: 13, y: 12 },
    webPreferences: {
      preload: path.join(__dirname, 'preload.js'),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
    },
  });

  win.once('ready-to-show', () => win.show());
  win.loadURL(DEV ? VITE_URL : BACKEND_URL);
  if (DEV) win.webContents.openDevTools({ mode: 'detach' });

  // Anything that wants a new window -- Microsoft's consent page, "Open in
  // Outlook" -- goes to the system browser instead. The app window only ever
  // renders our own UI.
  win.webContents.setWindowOpenHandler(({ url }) => {
    openSafely(url);
    return { action: 'deny' };
  });
  win.webContents.on('will-navigate', (event, url) => {
    if (!url.startsWith(BACKEND_URL) && !url.startsWith(VITE_URL)) {
      event.preventDefault();
      openSafely(url);
    }
  });

  win.on('closed', () => { win = null; });
}

// --------------------------------------------------------------------------

/* Every path that can reach the operating system, in one place.

   `shell.openExternal` is a documented remote-code-execution class, not a
   theoretical one: Electron's own security checklist says "when openExternal
   is used with untrusted content, it can be leveraged to execute arbitrary
   commands", and there are CVEs (Jitsi Meet CVE-2020-25019 among them) that
   are precisely this call reached from message content. The published advice
   is exact: limit the scheme to http, https and mailto.

   Until now both callers below handed it the raw string. One of them is the
   window-open handler, which is what an anchor inside an email body reaches
   when it is clicked. So a link in a stranger's email could name any scheme
   any installed application had registered, and this app would ask macOS to
   open it.

   An allowlist and never a denylist -- the interesting schemes are the ones
   nobody thought of. The URL is re-serialised from the parsed object rather
   than passed through, so a string that parses one way here and another way
   downstream cannot carry a payload between the two. */
const OPENABLE = new Set(['http:', 'https:', 'mailto:']);

function openSafely(raw) {
  if (typeof raw !== 'string' || raw.length > 4096) return false;
  // Control characters and newlines are how a single "URL" becomes two
  // arguments somewhere further down.
  if (/[\u0000-\u001f\u007f]/.test(raw)) return false;
  let url;
  try {
    url = new URL(raw);
  } catch {
    return false;
  }
  if (!OPENABLE.has(url.protocol)) return false;
  shell.openExternal(url.href);
  return true;
}

ipcMain.handle('open-external', (_event, url) => openSafely(url));

/* First-run setup for the Apple Mail source.

   Full Disk Access is granted to FoolsGold.app, and macOS applies it only to
   processes started AFTER the grant. Closing the window is not enough -- on
   macOS the app keeps running in the Dock, and so does its backend, still
   without access. That is how "I gave it Full Disk Access and it still says
   blocked" happens. So the setup screen gets a real restart: relaunch, then
   quit, which also ends the backend (see the 'quit' handler). */
const FULL_DISK_ACCESS_PANE =
  'x-apple.systempreferences:com.apple.preference.security?Privacy_AllFiles';
ipcMain.handle('open-full-disk-access', () => shell.openExternal(FULL_DISK_ACCESS_PANE));
ipcMain.handle('relaunch-app', () => { app.relaunch(); app.quit(); });

/* Run from /Applications, not from the disk image or Downloads.

   An app opened straight from the DMG -- or from Downloads with the quarantine
   flag still on -- runs from a read-only or randomised ("translocated") path.
   Full Disk Access then attaches to a copy the user will not find again, and
   the grant appears not to work. Offer the move once, the way most Mac apps
   downloaded from the web do. */
const MOVE_TEXT = {
  en: ['Move FoolsGold to your Applications folder?',
       'FoolsGold is running from the download. Moving it to Applications lets macOS keep its permissions (such as Full Disk Access) and lets you open it from Launchpad.',
       'Move to Applications', 'Not now'],
  ko: ['FoolsGold를 응용 프로그램 폴더로 옮길까요?',
       '지금 FoolsGold가 다운로드한 위치에서 실행되고 있습니다. 응용 프로그램 폴더로 옮겨야 macOS가 권한(전체 디스크 접근 등)을 유지하고, Launchpad에서도 열 수 있습니다.',
       '응용 프로그램으로 이동', '나중에'],
  zh: ['要将 FoolsGold 移到“应用程序”文件夹吗？',
       'FoolsGold 正从下载位置运行。移到“应用程序”后，macOS 才能保留它的权限（例如完全磁盘访问权限），也可以从启动台打开。',
       '移到“应用程序”', '以后再说'],
  ja: ['FoolsGold を「アプリケーション」フォルダに移動しますか？',
       'FoolsGold はダウンロードした場所から実行されています。「アプリケーション」に移動すると、macOS が権限（フルディスクアクセスなど）を保持でき、Launchpad からも開けます。',
       '「アプリケーション」に移動', '今はしない'],
};

function offerMoveToApplications() {
  if (!app.isPackaged || process.platform !== 'darwin') return false;
  try { if (app.isInApplicationsFolder()) return false; } catch { return false; }
  const lang = (app.getLocale() || 'en').slice(0, 2);
  const [message, detail, move, later] = MOVE_TEXT[lang] || MOVE_TEXT.en;
  const choice = dialog.showMessageBoxSync({
    type: 'question', message, detail, buttons: [move, later],
    defaultId: 0, cancelId: 1,
  });
  if (choice !== 0) return false;
  try {
    // Replaces an older copy already in /Applications (that is how an update
    // from a newer DMG lands); never touches a copy that is running.
    return app.moveToApplicationsFolder({
      conflictHandler: (type) => type !== 'existsAndRunning',
    });
  } catch (err) {
    dialog.showErrorBox('FoolsGold', String(err.message || err));
    return false;
  }
}

/* One app, one backend.
 *
 * A second copy of the app used to spawn a second backend, which died on bind
 * and produced the "exited with code 3" dialog -- over a window the FIRST
 * copy's backend was serving perfectly well. The lock has to be taken before
 * anything else, and a second launch must raise the existing window rather
 * than touching the port at all.
 */
if (!app.requestSingleInstanceLock()) {
  app.quit();
} else {

app.on('second-instance', () => {
  if (!win) return;
  if (win.isMinimized()) win.restore();
  win.focus();
});

app.whenReady().then(async () => {
  // Before the backend starts: a successful move quits and relaunches from
  // /Applications, and a backend started from the old path would be orphaned.
  if (offerMoveToApplications()) return;
  if (!(await ensureBackend())) { app.quit(); return; }
  try {
    /* A packaged build's first launch is slow: macOS checks every library in
       the frozen backend the first time it loads (the build's smoke test saw
       startup land past 20 s on a binary that was fine). 30 s is plenty for
       `python -m app.main` in development and too tight for that, so the
       frozen backend gets 90 s. The build script prints the real cold and warm
       start times -- if the cold one approaches this, raise it here. */
    await waitForBackend(frozenBackend() ? 90000 : 30000);
  } catch (err) {
    dialog.showErrorBox('Fools Gold could not start', String(err.message));
  }
  createWindow();
  startReminders();

  app.on('activate', () => {
    if (BrowserWindow.getAllWindows().length === 0) createWindow();
  });
});

app.on('window-all-closed', () => { if (process.platform !== 'darwin') app.quit(); });

app.on('before-quit', () => { app.isQuitting = true; });

app.on('quit', () => {
  if (reminderTimer) { clearInterval(reminderTimer); reminderTimer = null; }
  // `adopted` is deliberately not killed here: it is not ours to end, and the
  // user was told so when the app started.
  if (backend) { backend.kill('SIGTERM'); backend = null; }
});

}    // end of the single-instance branch
