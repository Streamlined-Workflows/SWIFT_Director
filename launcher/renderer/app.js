/* SWIFT Director Launcher: the page. One tile per place you can open on the server, a page about each, and a
 * Tailscale page that finds the server. Everything that touches the computer goes through window.launcher (preload.js).
 * Nothing here is built from HTML strings: text from the server or from Tailscale is always placed as text.
 */
(() => {
  'use strict';
  const api = window.launcher;
  const $ = id => document.getElementById(id);
  function h(tag, attrs, ...kids) {
    const el = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs || {})) {
      if (v == null || v === false) continue;
      if (k === 'class') el.className = v; else if (k.startsWith('on')) el.addEventListener(k.slice(2), v); else if (k === 'disabled' || k === 'hidden') el[k] = !!v; else el.setAttribute(k, v === true ? '' : v);
    }
    for (const kid of kids.flat()) if (kid != null && kid !== false) el.append(kid.nodeType ? kid : document.createTextNode(String(kid)));
    return el;
  }
  const SVG = 'http://www.w3.org/2000/svg';
  function icon(paths, cls) { const s = document.createElementNS(SVG, 'svg'); s.setAttribute('viewBox', '0 0 24 24'); s.setAttribute('aria-hidden', 'true'); if (cls) s.setAttribute('class', cls);
    for (const d of paths) { const p = document.createElementNS(SVG, 'path'); p.setAttribute('d', d); s.append(p); } return s; }
  const ICONS = {
    director: ['M3 8h18v11H3z', 'M3 8l2-4 4 4M9 4l4 4M13 4l4 4M17 4l4 4', 'M10 12l5 2.5-5 2.5z'],
    comfyui: ['M4 5h6v5H4z', 'M14 14h6v5h-6z', 'M10 7.5h3a2 2 0 0 1 2 2V14', 'M4 15h4v4H4z', 'M8 17h6'],
    tailscale: ['M6 6h.01M12 6h.01M18 6h.01M6 12h.01M12 12h.01M18 12h.01M6 18h.01M12 18h.01M18 18h.01', 'M6 12h12M12 12v6'],
    server: ['M4 5h16v5H4z', 'M4 14h16v5H4z', 'M8 7.5h.01M8 16.5h.01'],
    add: ['M12 5v14M5 12h14'],
    place: ['M4 5h16v14H4z', 'M4 9h16', 'M7 7h.01'],
    min: ['M5 12h14'], max: ['M6 6h12v12H6z'], restore: ['M8 8h10v10H8z', 'M6 16V6h10'], close: ['M6 6l12 12M18 6L6 18'],
  };

  const DEFAULTS = [
    { id: 'director', name: 'SWIFT Director', port: 8190, path: '/', reach: 'tailscale', summary: 'Your story. Your vision. Directed by you, built by AI. The production studio itself: storyboards, the Asset Generator, audio and 3D.',
      about: ['Storyboard and Assisted Storyboard for music videos and scenes.', 'Asset Generator: images, reference sheets, depth and 3D assets.', 'It has no login. Reach it over Tailscale only.'] },
    { id: 'comfyui', name: 'ComfyUI', port: 8188, path: '/', reach: 'tailscale', summary: 'The node-graph tool that runs the image, video and 3D models on the server. SWIFT Director sends its work here.',
      about: ['Open it to try a workflow by hand, look at the queue, or load a template.', 'A job started here shares the graphics card with SWIFT Director, so one waits for the other.', 'It has no login. Reach it over Tailscale only.'] },
  ];
  const state = { settings: { server: null, custom: [] }, version: '', ts: null, manifest: null, reach: {}, tab: 'director', syncing: false, synced: null, maximized: false, note: '' };

  const server = () => state.settings.server;
  const services = () => [...(state.manifest ? state.manifest.services : DEFAULTS), ...state.settings.custom.map(c => ({ ...c, reach: 'tailscale', about: [], custom: true }))];
  const open = () => services().filter(s => s.reach === 'tailscale');
  const inside = () => services().filter(s => s.reach === 'server');
  const status = s => !server() ? 'none' : state.syncing && !(s.port in state.reach) ? 'wait' : state.reach[s.port] ? 'up' : 'down';
  const address = s => server() ? `http://${server().host}:${s.port}${s.path && s.path !== '/' ? s.path : ''}` : '';
  let toastTimer = null;
  function toast(text) { const t = $('toast'); t.textContent = text; t.hidden = false; $('live').textContent = text; clearTimeout(toastTimer); toastTimer = setTimeout(() => { t.hidden = true; }, 4500); }

  /* ---------- finding the server and what is on it ---------- */
  async function sync(announce) {
    if (state.syncing) return; state.syncing = true; state.reach = {}; draw();
    try {
      state.ts = await api.tailscale();
      const devices = state.ts.devices || [];
      let s = server();
      if (s) {                                             // the saved device may have a new address: follow its name
        const same = devices.find(d => d.ip === s.host || d.dns === s.host) || devices.find(d => d.name && d.name === s.name);
        if (same && same.ip !== s.host && same.dns !== s.host) { state.settings = await api.save({ server: { host: same.ip, name: same.name } }); s = server(); }
      }
      state.manifest = s ? await api.manifest(s.host) : null;
      if (!state.manifest && devices.length) {
        const found = await api.findServer(devices.filter(d => !s || d.ip !== s.host));
        if (found && (found.manifest || !s)) { state.settings = await api.save({ server: { host: found.device.ip, name: found.device.name } }); state.manifest = found.manifest; s = server(); }
      }
      if (s) state.reach = await api.probe(s.host, open().map(x => x.port));
      state.synced = new Date();
      if (announce) toast(!state.ts.installed ? 'Tailscale was not found on this computer.' : !state.ts.running ? 'Tailscale is not connected.' : !s ? 'No device on your tailnet answered as SWIFT Director.' :
        state.manifest ? `Found SWIFT Director on ${s.name || s.host} with ${open().length} place${open().length === 1 ? '' : 's'} to open.` : `${s.name || s.host} did not answer on port 8190. The usual ports are shown.`);
    } catch (e) { toast('Could not sync: ' + (e.message || e)); }
    state.syncing = false;
    if (!['tailscale', 'server', 'add'].includes(state.tab) && !open().some(x => x.id === state.tab)) state.tab = 'director';
    draw();
  }
  async function recheck(quiet) { const s = server(); if (!s) { draw(quiet && ['add', 'tailscale'].includes(state.tab)); return; } state.reach = await api.probe(s.host, open().map(x => x.port)); draw(quiet && ['add', 'tailscale'].includes(state.tab)); }

  /* ---------- drawing ---------- */
  function titlebar() {
    const win = (what, label, paths, cls) => h('button', { class: 'win-btn ' + (cls || ''), type: 'button', 'aria-label': label, title: label, onclick: () => api.window(what) }, icon(paths, 'win-svg'));
    $('titlebar').replaceChildren(
      h('div', { class: 'brand' }, h('img', { class: 'wordmark', src: 'shared/swift-wordmark.svg', alt: 'S.W.I.F.T.' }),
        h('div', { class: 'brand-text' }, h('span', { class: 'app-name' }, 'SWIFT Director Launcher'), h('span', { class: 'app-sub' }, 'Your local studio, over Tailscale'))),
      h('div', { class: 'drag-fill' }),
      h('button', { class: 'btn small sync', type: 'button', disabled: state.syncing, onclick: () => sync(true), title: 'Ask Tailscale for your devices, find SWIFT Director, and check which places answer' }, state.syncing ? 'Syncing…' : 'Sync with Tailscale'),
      h('div', { class: 'win-controls' }, win('minimize', 'Minimize', ICONS.min), win('maximize', state.maximized ? 'Restore' : 'Maximize', state.maximized ? ICONS.restore : ICONS.max), win('close', 'Close', ICONS.close, 'close')));
  }
  function tile(id, label, paths, opts = {}) {
    const selected = state.tab === id;
    return h('button', { class: `tile${selected ? ' selected' : ''}${opts.dim ? ' dim' : ''}${opts.add ? ' add' : ''}`, type: 'button', role: 'tab', 'aria-selected': String(selected), 'data-tab': id, title: opts.title || label,
      onclick: () => { state.tab = id; draw(); $('view').focus(); } },
      h('span', { class: 'tile-icon' }, icon(paths), opts.dot ? h('span', { class: 'dot ' + opts.dot }) : null), h('span', { class: 'tile-label' }, label));
  }
  function rail() {
    const tabs = open().map(s => { const st = status(s); return tile(s.id, s.name, ICONS[s.id] || ICONS.place, { dim: st === 'down' || st === 'none', dot: st === 'up' ? 'up' : st === 'down' ? 'down' : null, title: `${s.name}: ${st === 'up' ? 'answering' : st === 'down' ? 'not answering' : st === 'wait' ? 'checking' : 'no server chosen'}` }); });
    if (inside().length) tabs.push(tile('server', 'On the server', ICONS.server));
    const ts = state.ts;
    tabs.push(tile('tailscale', 'Tailscale', ICONS.tailscale, { dot: !ts ? null : ts.running ? 'up' : 'down' }));
    $('rail').replaceChildren(h('div', { class: 'rail-tabs', role: 'tablist', 'aria-orientation': 'vertical', 'aria-label': 'Places' }, tabs), h('div', { class: 'rail-end' }, tile('add', 'Add a port', ICONS.add, { add: true })));
  }
  const pill = st => h('span', { class: 'pill ' + (st === 'up' ? 'up' : st === 'down' ? 'down' : 'wait') }, st === 'up' ? '● Answering' : st === 'down' ? '✕ Not answering' : st === 'wait' ? '… Checking' : '? No server chosen');

  function servicePage(s) {
    const st = status(s), addr = address(s);
    const actions = h('div', { class: 'hero-actions' },
      h('button', { class: 'btn primary', type: 'button', disabled: !server(), onclick: async () => { const r = await api.open(server().host, s.port, s.path); if (!r.ok) toast(r.error); else toast(`Opening ${s.name} in your browser.`); } }, `Open ${s.name}`),
      h('button', { class: 'btn', type: 'button', disabled: !addr, onclick: async () => { try { await navigator.clipboard.writeText(addr); toast('Address copied.'); } catch { toast(addr); } } }, 'Copy address'),
      h('button', { class: 'btn', type: 'button', disabled: !server() || state.syncing, onclick: async () => { await recheck(); toast(state.reach[s.port] ? `${s.name} is answering.` : `${s.name} is not answering.`); } }, 'Check again'),
      s.custom ? h('button', { class: 'btn', type: 'button', onclick: async () => { state.settings = await api.save({ custom: state.settings.custom.filter(c => c.id !== s.id) }); state.tab = 'director'; draw(); toast('Removed.'); } }, 'Remove this tab') : null);
    const why = st === 'down' ? h('div', { class: 'card' }, h('h2', null, 'Why it might not answer'), h('ul', null,
      h('li', null, 'The server is off, asleep, or not connected to Tailscale. The Tailscale tab shows which devices are online.'),
      h('li', null, `${s.name} is not running on the server, or it listens on a different port than ${s.port}.`),
      h('li', null, 'Tailscale is not connected on this computer.'))) : null;
    return [h('section', { class: 'hero fade' }, h('p', { class: 'micro' }, s.custom ? 'Added by you' : 'On your server'), h('h1', { class: 'hero-name' }, s.name),
        h('div', { class: 'hero-meta' }, pill(st), addr ? h('code', null, addr) : h('span', { class: 'muted' }, 'Choose the server on the Tailscale tab.')),
        s.summary ? h('p', { class: 'hero-desc' }, s.summary) : null, actions),
      h('div', { class: 'body' }, s.about && s.about.length ? h('div', { class: 'card' }, h('h2', null, 'About'), h('ul', null, s.about.map(a => h('li', null, a)))) : null, why,
        h('div', { class: 'card' }, h('h2', null, 'Details'), h('dl', { class: 'facts' }, h('dt', null, 'Port'), h('dd', null, String(s.port)), h('dt', null, 'Opens in'), h('dd', null, 'Your default browser (Chrome or Edge work best)'),
          h('dt', null, 'Listed by'), h('dd', null, s.custom ? 'You, on this computer' : state.manifest ? 'SWIFT Director on the server' : 'The launcher’s built-in list (the server did not answer)'))))];
  }
  function serverPage() {
    return [h('section', { class: 'hero fade' }, h('p', { class: 'micro' }, 'For information'), h('h1', { class: 'hero-name' }, 'On the server'),
        h('p', { class: 'hero-desc' }, 'These run beside SWIFT Director and are used by it. They listen on the server only, so they cannot be opened from this computer, and that is on purpose: they have no login.')),
      h('div', { class: 'body' }, inside().map(s => h('div', { class: 'card' }, h('h2', null, `${s.name} · port ${s.port}`), h('p', null, s.summary), s.about.length ? h('ul', null, s.about.map(a => h('li', null, a))) : null)))];
  }
  function tailscalePage() {
    const ts = state.ts, s = server();
    const line = !ts ? 'Not checked yet.' : !ts.installed ? 'Tailscale was not found on this computer.' : ts.running ? `Connected${ts.tailnet ? ' to ' + ts.tailnet : ''}.` : ts.error;
    const choose = async d => { state.settings = await api.save({ server: { host: d.ip, name: d.name } }); toast(`${d.name || d.ip} is now the server.`); sync(false); };
    const devices = ts && ts.devices.length ? h('ul', { class: 'devices' }, ts.devices.map(d => { const is = s && (s.host === d.ip || s.host === d.dns);
      return h('li', { class: 'device' + (is ? ' server' : '') }, h('span', null, h('strong', null, d.name || d.ip), ' ', h('code', null, d.ip), ' ', h('span', { class: 'muted' }, `${d.os || 'unknown'}${d.self ? ' · this computer' : ''}`)),
        h('span', { class: 'row' }, h('span', { class: 'pill ' + (d.online ? 'up' : 'down') }, d.online ? '● Online' : '✕ Offline'), is ? h('span', { class: 'pill' }, 'The server') : d.self ? null : h('button', { class: 'btn small', type: 'button', onclick: () => choose(d) }, 'Use as the server'))); }))
      : h('p', { class: 'muted' }, 'No devices to show.');
    const manual = h('input', { type: 'text', placeholder: '100.x.y.z or name.tailnet.ts.net', value: s ? s.host : '', 'aria-label': 'Server address', spellcheck: 'false' });
    return [h('section', { class: 'hero fade' }, h('p', { class: 'micro' }, 'How this computer reaches the server'), h('h1', { class: 'hero-name' }, 'Tailscale'),
        h('div', { class: 'hero-meta' }, h('span', { class: 'pill ' + (!ts ? 'wait' : ts.running ? 'up' : 'down') }, !ts ? '… Checking' : ts.running ? '● Connected' : '✕ Not connected'), h('span', null, line)),
        h('p', { class: 'hero-desc' }, 'Tailscale is the private network between your devices. SWIFT Director and ComfyUI have no login, so they are reached only through it and never opened to the internet.'),
        h('div', { class: 'hero-actions' }, h('button', { class: 'btn primary', type: 'button', disabled: state.syncing, onclick: () => sync(true) }, state.syncing ? 'Syncing…' : 'Sync with Tailscale'),
          h('button', { class: 'btn', type: 'button', onclick: () => api.openTailscale('admin') }, 'Open the Tailscale admin console'),
          ts && !ts.installed ? h('button', { class: 'btn', type: 'button', onclick: () => api.openTailscale('download') }, 'Get Tailscale') : null)),
      h('div', { class: 'body' },
        h('div', { class: 'card' }, h('h2', null, 'What Sync does'), h('ul', null, h('li', null, 'Asks Tailscale on this computer for the devices on your tailnet.'), h('li', null, 'Looks for the one answering as SWIFT Director on port 8190 and remembers it.'),
          h('li', null, 'Asks SWIFT Director which places can be opened and on which ports. New ones appear as tabs by themselves.'), h('li', null, 'Checks that each port answers from here. Nothing is sent anywhere else.'))),
        h('div', { class: 'card' }, h('h2', null, 'Your devices'), devices),
        h('div', { class: 'card' }, h('h2', null, 'Set the server by hand'), h('p', { class: 'muted' }, 'Only Tailscale addresses are accepted: 100.64.0.0 to 100.127.255.255, or a name ending in .ts.net.'),
          h('div', { class: 'row' }, manual, h('button', { class: 'btn', type: 'button', onclick: async () => { const host = manual.value.trim().toLowerCase(); const saved = await api.save({ server: host ? { host, name: host } : null });
            if (saved.rejected) return toast('That is not a Tailscale address. Nothing was changed.'); state.settings = saved; toast(host ? 'Server set.' : 'Server cleared.'); sync(false); } }, 'Save'))))];
  }
  function addPage() {
    const f = { name: h('input', { type: 'text', maxlength: '40', placeholder: 'For example: Speech lab' }), port: h('input', { type: 'number', min: '1', max: '65535', placeholder: '8200' }), path: h('input', { type: 'text', placeholder: '/', value: '/' }), summary: h('input', { type: 'text', maxlength: '300', placeholder: 'What it is, in a sentence' }) };
    const add = async () => { const p = Number(f.port.value), name = f.name.value.trim();
      if (!name || !Number.isInteger(p) || p < 1 || p > 65535) return toast('Give it a name and a port between 1 and 65535.');
      if (services().some(s => s.port === p)) return toast(`Port ${p} already has a tab.`);
      const id = String(Date.now()); state.settings = await api.save({ custom: [...state.settings.custom, { id, name, port: p, path: f.path.value.trim() || '/', summary: f.summary.value.trim() }] });
      state.tab = 'custom-' + id; await recheck(); toast(`${name} added.`); };
    return [h('section', { class: 'hero fade' }, h('p', { class: 'micro' }, 'Something new on the server'), h('h1', { class: 'hero-name' }, 'Add a port'),
        h('p', { class: 'hero-desc' }, 'Places that SWIFT Director knows about appear by themselves after a sync. Use this for anything else on the server that you open in a browser. It is saved on this computer only.')),
      h('div', { class: 'body' }, h('div', { class: 'card' }, h('h2', null, 'New tab'), h('div', { class: 'form' }, h('label', null, 'Name', f.name), h('label', null, 'Port', f.port), h('label', null, 'Path (optional)', f.path), h('label', null, 'About', f.summary)),
        h('div', { class: 'row' }, h('button', { class: 'btn primary', type: 'button', onclick: add }, 'Add tab'))))];
  }
  function footer() {
    const s = server(), ts = state.ts;
    $('footer').replaceChildren(h('span', null, s ? `Server: ${s.name || s.host} (${s.host})` : 'Server: not chosen'), h('span', null, !ts ? 'Tailscale: checking' : ts.running ? 'Tailscale: connected' : 'Tailscale: not connected'),
      h('span', null, state.synced ? `Checked at ${state.synced.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}` : ''), h('span', { class: 'grow' }), h('span', null, `No sign-in · nothing leaves your tailnet · v${state.version}`));
  }
  function draw(keepPage) {
    titlebar(); rail(); footer();
    if (keepPage) return;                                  // a timed re-check must not wipe what is being typed
    const s = open().find(x => x.id === state.tab);
    const page = state.tab === 'tailscale' ? tailscalePage() : state.tab === 'server' ? serverPage() : state.tab === 'add' ? addPage() : s ? servicePage(s) : tailscalePage();
    const view = $('view'), top = view.dataset.tab === state.tab ? view.scrollTop : 0;
    view.replaceChildren(...page); view.dataset.tab = state.tab; view.scrollTop = top;
  }

  api.onWindow(w => { state.maximized = !!w.maximized; titlebar(); });
  (async () => { const st = await api.state(); state.settings = st.settings; state.version = st.version; draw(); await sync(false); if (!server()) { state.tab = 'tailscale'; draw(); } setInterval(() => { if (!state.syncing && !document.hidden) recheck(true); }, 60000); })();
  window._state = state;
})();
