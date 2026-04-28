import { contextBridge, ipcRenderer } from 'electron';

contextBridge.exposeInMainWorld('loomHost', {
  loadConfig: () => ipcRenderer.invoke('config:load'),
  saveConfig: (config) => ipcRenderer.invoke('config:save', config),
  readAllStatus: (loom) => ipcRenderer.invoke('loom:readAllStatus', loom),
  sendDeclaration: (payload) => ipcRenderer.invoke('loom:sendDeclaration', payload),
  pollRealtimeStatus: (loom) => ipcRenderer.invoke('loom:pollRealtimeStatus', loom),
  remoteStop: (payload) => ipcRenderer.invoke('loom:remoteStop', payload),
  getBackendStatus: () => ipcRenderer.invoke('backend:status'),
  startBackend: () => ipcRenderer.invoke('backend:start'),
  onBackendLog: (callback) => {
    const listener = (_event, lines) => callback(lines);
    ipcRenderer.on('backend:log', listener);
    return () => ipcRenderer.removeListener('backend:log', listener);
  }
});
