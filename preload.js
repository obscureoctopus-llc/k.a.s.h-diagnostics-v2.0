const { contextBridge, ipcRenderer } = require('electron');

contextBridge.exposeInMainWorld('api', {
    connect: () => ipcRenderer.invoke('connect-hardware'),
    getRPM: () => ipcRenderer.invoke('get-rpm')
});
