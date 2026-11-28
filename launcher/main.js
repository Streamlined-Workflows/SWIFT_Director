/* SWIFT Director Launcher: the desktop window. No sign-in, no set-up steps, nothing sent anywhere.
 *
 * What this process does for the page (renderer/app.js), and nothing else:
 *   tailscale      ask this computer's Tailscale which devices are on the tailnet
 *   find-server    look for SWIFT Director on those devices (port 8190) and read its list of services
 *   manifest       read that list from a known device:  GET http://<device>:8190/api/launcher
 *   probe          can this computer reach a port on that device? (a plain connection attempt, nothing is sent)
 *   open           open http://<device>:<port> in the default browser
 *   settings       remember the chosen device and any ports added by hand (a small JSON file in the app's data folder)
 *
 * Addresses: only Tailscale addresses (100.64.0.0/10), *.ts.net names and this computer itself are accepted, because
 * SWIFT Director and ComfyUI have no login and are meant to be reached over Tailscale only.
 */
const { app, BrowserWindow, ipcMain, shell, session } = require('electron');
const { execFile } = require('node:child_process');
const fs = require('node:fs');
const net = require('node:net');
const path = require('node:path');

const DIRECTOR_PORT = 8190;
const ADMIN = 'https://login.tailscale.com/admin/machines';
const DOWNLOAD = 'https://tailscale.com/download';
let win = null;

/* ---------- settings ---------- */
const settingsFile = () => path.join(app.getPath('userData'), 'settings.json');
function readSettings() {
  try { const s = JSON.parse(fs.readFileSync(settingsFile(), 'utf8')); return { server: s.server || null, custom: Array.isArray(s.custom) ? s.custom : [] }; }
  catch { return { server: null, custom: [] }; }
}
function writeSettings(s) { fs.mkdirSync(path.dirname(settingsFile()), { recursive: true }); fs.writeFileSync(settingsFile(), JSON.stringify(s, null, 2)); return s; }

/* ---------- which addresses are allowed ---------- */
function tailscaleHost(host) {
  const h = String(host || '').trim().toLowerCase();
  if (!h || h.length > 253 || !/^[a-z0-9.-]+$/.test(h)) return false;
  if (h === 'localhost' || h === '127.0.0.1') return true;
  if (h.endsWith('.ts.net')) return true;
  const m = /^(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})$/.exec(h);
  return !!m && +m[1] === 100 && +m[2] >= 64 && +m[2] <= 127 && +m[3] <= 255 && +m[4] <= 255;      // 100.64.0.0/10
}
const port = p => { const n = Number(p); return Number.isInteger(n) && n >= 1 && n <= 65535 ? n : null; };
const text = (v, max) => String(v == null ? '' : v).replace(/[\u0000-\u001f]/g, ' ').trim().slice(0, max);

/* ---------- Tailscale ---------- */
function tailscaleCommands() {
  if (process.env.SWIFT_TAILSCALE_CMD) return [process.env.SWIFT_TAILSCALE_CMD];
  const list = ['tailscale'];
  if (process.platform === 'win32') list.push(path.join(process.env.ProgramFiles || 'C:\\Program Files', 'Tailscale', 'tailscale.exe'), path.join(process.env['ProgramFiles(x86)'] || 'C:\\Program Files (x86)', 'Tailscale', 'tailscale.exe'));
  if (process.platform === 'darwin') list.push('/Applications/Tailscale.app/Contents/MacOS/Tailscale');
  return list;
}
const run = (cmd, args) => new Promise(done => execFile(cmd, args, { timeout: 8000, windowsHide: true, maxBuffer: 8 * 1024 * 1024 }, (err, out) => done(err ? { err } : { out })));
async function tailscale() {
  for (const cmd of tailscaleCommands()) {
    const r = await run(cmd, ['status', '--json']);
    if (r.err && r.err.code === 'ENOENT') continue;
    let j = null; try { j = JSON.parse(r.out || (r.err && r.err.stdout) || ''); } catch { /* not JSON */ }
    if (!j) return { installed: true, running: false, devices: [], error: 'Tailscale is installed but did not answer. Open Tailscale and sign in.' };
    const device = (d, self) => ({ name: text(d.HostName, 80), dns: text(d.DNSName, 200).replace(/\.$/, ''), ip: (d.TailscaleIPs || []).find(a => /^\d+\.\d+\.\d+\.\d+$/.test(a)) || '', os: text(d.OS, 30), online: self ? true : !!d.Online, self });
    const devices = [j.Self ? device(j.Self, true) : null, ...Object.values(j.Peer || {}).map(d => device(d, false))].filter(d => d && d.ip && tailscaleHost(d.ip));
    const running = j.BackendState === 'Running';
    return { installed: true, running, state: text(j.BackendState, 40), tailnet: text(j.CurrentTailnet && j.CurrentTailnet.Name, 120), devices,
      error: running ? '' : 'Tailscale is not connected on this computer. Open Tailscale and connect.' };
  }
  return { installed: false, running: false, devices: [], error: 'Tailscale was not found on this computer.' };
}

/* ---------- looking at a device ---------- */
function probe(host, p, wait = 1500) {
  return new Promise(done => {
    if (!tailscaleHost(host) || !port(p)) return done(false);
    const s = net.connect({ host, port: p });
    const end = ok => { s.destroy(); done(ok); };
    s.setTimeout(wait, () => end(false)); s.once('connect', () => end(true)); s.once('error', () => end(false));
  });
}
async function manifest(host) {
  if (!tailscaleHost(host)) return null;
  const stop = new AbortController(), timer = setTimeout(() => stop.abort(), 3000);
  try {
    const r = await fetch(`http://${host}:${DIRECTOR_PORT}/api/launcher`, { signal: stop.signal, redirect: 'error' });
    if (!r.ok) return null;
    const j = await r.json();
    if (!j || j.app !== 'SWIFT Director' || !Array.isArray(j.services)) return null;
    return { app: 'SWIFT Director', version: text(j.version, 40), services: j.services.slice(0, 40).map(s => ({
      id: text(s.id, 40).replace(/[^a-z0-9-]/gi, ''), name: text(s.name, 60), port: port(s.port), path: /^\/[\w\-./?=&%]*$/.test(s.path || '') ? s.path : '/',
      reach: s.reach === 'server' ? 'server' : 'tailscale', summary: text(s.summary, 300), about: (Array.isArray(s.about) ? s.about : []).slice(0, 12).map(a => text(a, 400)) })).filter(s => s.id && s.name && s.port) };
  } catch { return null; } finally { clearTimeout(timer); }
}
async function findServer(devices) {
  const online = (devices || []).filter(d => d && d.online && tailscaleHost(d.ip)).slice(0, 60);
  const found = await Promise.all(online.map(async d => (await probe(d.ip, DIRECTOR_PORT)) ? { device: d, manifest: await manifest(d.ip) } : null));
  return found.filter(Boolean).sort((a, b) => (b.manifest ? 1 : 0) - (a.manifest ? 1 : 0))[0] || null;
}

/* ---------- the window ---------- */
function createWindow() {
  win = new BrowserWindow({
    width: 1180, height: 760, minWidth: 980, minHeight: 620, frame: false, backgroundColor: '#000000', show: false,
    title: 'SWIFT Director Launcher', icon: path.join(__dirname, 'build', 'icon.png'),
    webPreferences: { preload: path.join(__dirname, 'preload.js'), contextIsolation: true, sandbox: true, nodeIntegration: false, webSecurity: true, devTools: !app.isPackaged },
  });
  win.removeMenu();
  win.once('ready-to-show', () => win.show());
  win.webContents.setWindowOpenHandler(() => ({ action: 'deny' }));          // nothing opens inside the launcher
  win.webContents.on('will-navigate', ev => ev.preventDefault());
  win.on('maximize', () => win.webContents.send('window', { maximized: true }));
  win.on('unmaximize', () => win.webContents.send('window', { maximized: false }));
  win.loadFile(path.join(__dirname, 'renderer', 'index.html'));
}

app.whenReady().then(() => {
  session.defaultSession.setPermissionRequestHandler((_wc, _perm, answer) => answer(false));
  session.defaultSession.webRequest.onBeforeRequest((details, next) => next({ cancel: !/^(file|devtools):/.test(details.url) }));   // the page itself fetches nothing
  const mine = ev => ev.senderFrame && ev.senderFrame.url.startsWith('file://');
  const on = (name, fn) => ipcMain.handle(name, (ev, ...args) => { if (!mine(ev)) throw new Error('refused'); return fn(...args); });

  on('state', () => ({ settings: readSettings(), version: app.getVersion(), platform: process.platform, directorPort: DIRECTOR_PORT }));
  on('tailscale', () => tailscale());
  on('find-server', devices => findServer(devices));
  on('manifest', host => manifest(host));
  on('probe', async (host, ports) => { const out = {}; await Promise.all((Array.isArray(ports) ? ports : []).slice(0, 40).map(async p => { if (port(p)) out[p] = await probe(host, port(p)); })); return out; });
  on('open', (host, p, where) => {
    if (!tailscaleHost(host) || !port(p)) return { ok: false, error: 'Only Tailscale addresses can be opened from here.' };
    const tail = /^\/[\w\-./?=&%]*$/.test(where || '') ? where : '/';
    shell.openExternal(`http://${host}:${port(p)}${tail}`); return { ok: true };
  });
  on('open-tailscale', which => { shell.openExternal(which === 'download' ? DOWNLOAD : ADMIN); return { ok: true }; });
  on('save', patch => {
    const s = readSettings(), p = patch && typeof patch === 'object' ? patch : {};
    if ('server' in p) {
      if (p.server && !tailscaleHost(p.server.host)) return { ...s, rejected: true };        // not a Tailscale address: nothing is changed
      s.server = p.server ? { host: text(p.server.host, 253).toLowerCase(), name: text(p.server.name, 80) } : null;
    }
    if (Array.isArray(p.custom)) s.custom = p.custom.slice(0, 30).map(c => ({ id: 'custom-' + text(c.id, 40).replace(/^custom-/, '').replace(/[^a-z0-9]/gi, ''), name: text(c.name, 40), port: port(c.port), path: /^\/[\w\-./?=&%]*$/.test(c.path || '') ? c.path : '/', summary: text(c.summary, 300) })).filter(c => c.name && c.port);
    return writeSettings(s);
  });
  on('window', what => { if (!win) return; if (what === 'minimize') win.minimize(); else if (what === 'maximize') win.isMaximized() ? win.unmaximize() : win.maximize(); else if (what === 'close') win.close(); });

  createWindow();
});
app.on('window-all-closed', () => app.quit());
