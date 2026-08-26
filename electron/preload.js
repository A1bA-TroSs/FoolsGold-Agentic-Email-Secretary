const { contextBridge, ipcRenderer } = require('electron');

// The renderer gets exactly one capability: hand a URL to the real browser.
// No node, no fs, no direct IPC surface beyond this.
contextBridge.exposeInMainWorld('foolsgold', {
  openExternal: (url) => ipcRenderer.invoke('open-external', url),
});
