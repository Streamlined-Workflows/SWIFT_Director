/* The only doorway between the page and the computer. Each function asks the main process (main.js) for one thing. */
const { contextBridge, ipcRenderer } = require('electron');
const ask = name => (...args) => ipcRenderer.invoke(name, ...args);
contextBridge.exposeInMainWorld('launcher', {
  state: ask('state'), tailscale: ask('tailscale'), findServer: ask('find-server'), manifest: ask('manifest'), probe: ask('probe'),
  open: ask('open'), openTailscale: ask('open-tailscale'), save: ask('save'), window: ask('window'),
  onWindow: fn => ipcRenderer.on('window', (_ev, data) => fn(data)),
});
