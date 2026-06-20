const { contextBridge, ipcRenderer } = require('electron');

const call = (ch, ...args) => ipcRenderer.invoke(ch, ...args);

contextBridge.exposeInMainWorld('api', {
  listPorts:   ()     => call('kash:listPorts'),
  connect:     (port) => call('kash:connect', { port }),
  disconnect:  ()     => call('kash:disconnect'),
  status:      ()     => call('kash:status'),
  scan:        ()     => call('kash:scan'),
  clearDTCs:   ()     => call('kash:clearDTCs'),
  liveMetrics: ()     => call('kash:liveMetrics'),
  getPID:      (pid)  => call('kash:getPID', pid),
  lookupDTC:   (code) => call('kash:lookupDTC', code),
  ping:        ()     => call('kash:ping'),
});
