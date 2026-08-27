/* Fools Gold desktop shell.
 *
 * A real window, not a browser tab: fixed default size, its own icon, hidden
 * title bar in the Obsidian style. It owns the Python backend's lifetime, so
 * quitting the app actually stops the process holding your mail cache.
 */
const { app, BrowserWindow, shell, ipcMain, dialog } = require('electron');
const { spawn } = require('child_process');
const path = require('path');
const http = require('http');
const fs = require('fs');

const PORT = process.env.FOOLSGOLD_PORT || '8765';
const BACKEND_URL = `http://127.0.0.1:${PORT}`;
const DEV = process.env.FOOLSGOLD_DEV === '1';
const VITE_URL = 'http://localhost:5173';

let backend = null;
let win = null;

// --------------------------------------------------------------------------

function backendDir() {
  // Packaged builds carry backend/ in resources; in dev it sits beside us.
  const packaged = path.join(process.resourcesPath || '', 'backend');
  return app.isPackaged && fs.existsSync(packaged) ? packaged : path.join(__dirname, '..', 'backend');
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
  const python = pythonBin();
  if (python === null) {
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
  backend = spawn(python, ['-m', 'app.main'], {
    cwd,
    env: { ...process.env, FOOLSGOLD_PORT: PORT, PYTHONUNBUFFERED: '1' },
    stdio: ['ignore', 'pipe', 'pipe'],
  });
  backend.stdout.on('data', (d) => process.stdout.write(`[backend] ${d}`));
  backend.stderr.on('data', (d) => process.stderr.write(`[backend] ${d}`));
  backend.on('exit', (code) => {
    backend = null;
    if (code && code !== 0 && !app.isQuitting) {
      dialog.showErrorBox(
        'Fools Gold backend stopped',
        `The Python backend exited with code ${code}.\n\n` +
        `Most often this means the environment is stale or incomplete. Rebuild it:\n\n` +
        `    ./scripts/setup.sh\n\n` +
        `Full output is in the terminal you launched from.`
      );
    }
  });
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
    shell.openExternal(url);
    return { action: 'deny' };
  });
  win.webContents.on('will-navigate', (event, url) => {
    if (!url.startsWith(BACKEND_URL) && !url.startsWith(VITE_URL)) {
      event.preventDefault();
      shell.openExternal(url);
    }
  });

  win.on('closed', () => { win = null; });
}

// --------------------------------------------------------------------------

ipcMain.handle('open-external', (_event, url) => {
  if (typeof url === 'string' && /^https?:\/\//i.test(url)) return shell.openExternal(url);
  return false;
});

app.whenReady().then(async () => {
  startBackend();
  try {
    await waitForBackend();
  } catch (err) {
    dialog.showErrorBox('Fools Gold could not start', String(err.message));
  }
  createWindow();

  app.on('activate', () => {
    if (BrowserWindow.getAllWindows().length === 0) createWindow();
  });
});

app.on('window-all-closed', () => { if (process.platform !== 'darwin') app.quit(); });

app.on('before-quit', () => { app.isQuitting = true; });

app.on('quit', () => {
  if (backend) { backend.kill('SIGTERM'); backend = null; }
});
