const { contextBridge, ipcRenderer } = require('electron');

// The renderer gets three narrow capabilities: hand a URL to the real browser,
// open the Full Disk Access pane, and restart the app.
// No node, no fs, no direct IPC surface beyond this.
contextBridge.exposeInMainWorld('foolsgold', {
  openExternal: (url) => ipcRenderer.invoke('open-external', url),
  // First-run setup on macOS: open the one Settings pane the Apple Mail source
  // needs, and restart so macOS applies the permission (it never does to a
  // running process). Fixed targets -- the renderer passes nothing.
  openFullDiskAccess: () => ipcRenderer.invoke('open-full-disk-access'),
  relaunch: () => ipcRenderer.invoke('relaunch-app'),
});
