const { contextBridge, ipcRenderer } = require('electron');

contextBridge.exposeInMainWorld('auavApi', {
  onBackendReady: (cb) => {
    const listener = (_event, payload) => cb(payload);
    ipcRenderer.on('backend:ready', listener);
    return () => ipcRenderer.removeListener('backend:ready', listener);
  },
  getState: () => ipcRenderer.invoke('backend:state'),
  getFullState: () => ipcRenderer.invoke('backend:state-full'),
  chat: (req) => ipcRenderer.invoke('backend:chat', req),
  command: (req) => ipcRenderer.invoke('backend:command', req),
  reset: () => ipcRenderer.invoke('backend:reset'),
  discoverModels: (req) => ipcRenderer.invoke('backend:discover-models', req),
  listSaves: () => ipcRenderer.invoke('backend:list-saves'),
  saveSnapshot: (req) => ipcRenderer.invoke('backend:save', req),
  loadSnapshot: (req) => ipcRenderer.invoke('backend:load-save', req),
  compareSnapshots: (req) => ipcRenderer.invoke('backend:compare-saves', req),
  exportCfd: (req) => ipcRenderer.invoke('backend:export-cfd', req),
});

