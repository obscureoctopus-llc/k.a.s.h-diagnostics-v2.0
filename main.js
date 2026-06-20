// K.A.S.H. Diagnostics — Electron main process.
// Spawns the Python OBD-II engine and bridges it to the renderer via IPC.
const { app, BrowserWindow, ipcMain } = require('electron');
const { spawn } = require('child_process');
const path = require('path');
const fs = require('fs');

const PY_SCRIPT = path.join(__dirname, 'kash_diagnostics.py');

function resolvePython() {
  const venvWin = path.join(__dirname, 'venv', 'Scripts', 'python.exe');
  const venvNix = path.join(__dirname, 'venv', 'bin', 'python');
  if (fs.existsSync(venvWin)) return venvWin;
  if (fs.existsSync(venvNix)) return venvNix;
  return process.platform === 'win32' ? 'python.exe' : 'python3';
}

let pyProc = null;
let nextReqId = 1;
const pending = new Map();
let stdoutBuf = '';

function startPython() {
  if (pyProc) return;
  pyProc = spawn(resolvePython(), [PY_SCRIPT, '--serve'], {
    cwd: __dirname,
    stdio: ['pipe', 'pipe', 'pipe'],
    windowsHide: true,
  });
  pyProc.stdout.setEncoding('utf8');
  pyProc.stdout.on('data', (chunk) => {
    stdoutBuf += chunk;
    let idx;
    while ((idx = stdoutBuf.indexOf('\n')) !== -1) {
      const line = stdoutBuf.slice(0, idx).trim();
      stdoutBuf = stdoutBuf.slice(idx + 1);
      if (!line) continue;
      let msg;
      try { msg = JSON.parse(line); } catch (_) { continue; }
      const p = pending.get(msg.id);
      if (!p) continue;
      pending.delete(msg.id);
      msg.ok ? p.resolve(msg.result) : p.reject(new Error(msg.error || 'backend error'));
    }
  });
  pyProc.stderr.setEncoding('utf8');
  pyProc.stderr.on('data', (d) => process.stderr.write(`[py] ${d}`));
  pyProc.on('exit', (code) => {
    console.error(`[py] exited code=${code}`);
    pyProc = null;
    for (const { reject } of pending.values()) reject(new Error('Python exited'));
    pending.clear();
  });
}

// Per-command timeouts (ms).
// connect can probe multiple ports x 3 baud rates with ATZ timeouts, so give it 60s.
const CMD_TIMEOUTS = {
  connect:      60000,
  scan:         20000,
  clear_dtcs:   10000,
  live_metrics:  5000,
  get_pid:       5000,
};
const DEFAULT_TIMEOUT = 10000;

function pyCall(cmd, args = {}) {
  return new Promise((resolve, reject) => {
    if (!pyProc) { try { startPython(); } catch (e) { return reject(e); } }
    const id = nextReqId++;
    pending.set(id, { resolve, reject });
    try { pyProc.stdin.write(JSON.stringify({ id, cmd, args }) + '\n'); }
    catch (e) { pending.delete(id); return reject(e); }
    const ms = CMD_TIMEOUTS[cmd] ?? DEFAULT_TIMEOUT;
    setTimeout(() => {
      if (pending.has(id)) { pending.delete(id); reject(new Error(`timeout: ${cmd}`)); }
    }, ms);
  });
}

ipcMain.handle('kash:listPorts',   ()         => pyCall('list_ports'));
ipcMain.handle('kash:connect',     (_e, args) => pyCall('connect', args || {}));
ipcMain.handle('kash:disconnect',  ()         => pyCall('disconnect'));
ipcMain.handle('kash:status',      ()         => pyCall('status'));
ipcMain.handle('kash:scan',        ()         => pyCall('scan'));
ipcMain.handle('kash:clearDTCs',   ()         => pyCall('clear_dtcs'));
ipcMain.handle('kash:liveMetrics', ()         => pyCall('live_metrics'));
ipcMain.handle('kash:getPID',      (_e, pid)  => pyCall('get_pid', { pid }));
ipcMain.handle('kash:lookupDTC',   (_e, code) => pyCall('lookup_dtc', { code }));
ipcMain.handle('kash:ping',        ()         => pyCall('ping'));

function createWindow() {
  const win = new BrowserWindow({
    width: 1400, height: 900, backgroundColor: '#06080A',
    webPreferences: {
      preload: path.join(__dirname, 'preload.js'),
      contextIsolation: true, nodeIntegration: false, sandbox: false,
    },
  });
  win.loadFile(path.join(__dirname, 'index.html'));
}

app.whenReady().then(() => { startPython(); createWindow(); });
app.on('window-all-closed', () => {
  if (pyProc) { try { pyProc.kill(); } catch (_) {} pyProc = null; }
  if (process.platform !== 'darwin') app.quit();
});
