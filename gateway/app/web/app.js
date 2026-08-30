// music-server-tool - Dashboard
// Vanilla ES-Module, kein Build-Schritt. Der Zustand ist klein genug, dass ein
// Framework hier mehr Ballast als Nutzen waere.

const $  = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];

const state = {
  user: null,
  csrf: null,
  view: 'overview',
  stream: null,
  dupes: [],
  selectedGroups: new Set(),
};

// ------------------------------------------------------------------- API
function csrfToken() {
  const match = document.cookie.match(/(?:^|;\s*)mst_csrf=([^;]+)/);
  return match ? decodeURIComponent(match[1]) : null;
}

async function api(path, options = {}) {
  const opts = { credentials: 'same-origin', headers: {}, ...options };
  if (opts.body !== undefined && !(opts.body instanceof FormData)) {
    opts.headers['Content-Type'] = 'application/json';
    opts.body = JSON.stringify(opts.body);
  }
  const token = csrfToken();
  if (token) opts.headers['X-CSRF-Token'] = token;

  const response = await fetch(path, opts);
  if (response.status === 401) {
    showLogin();
    throw new Error('Nicht angemeldet');
  }
  const text = await response.text();
  const data = text ? JSON.parse(text) : null;
  if (!response.ok) throw new Error(data?.detail || `HTTP ${response.status}`);
  return data;
}

// ---------------------------------------------------------------- Helfer
const esc = (value) => String(value ?? '').replace(/[&<>"']/g,
  (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

function bytes(value) {
  const n = Number(value || 0);
  if (!n) return '0 B';
  const units = ['B', 'KB', 'MB', 'GB', 'TB'];
  const i = Math.min(Math.floor(Math.log(n) / Math.log(1024)), units.length - 1);
  return `${(n / 1024 ** i).toFixed(i ? 1 : 0)} ${units[i]}`;
}

function duration(seconds) {
  const s = Math.round(Number(seconds) || 0);
  if (s >= 3600) return `${Math.floor(s / 3600)} h ${Math.floor((s % 3600) / 60)} min`;
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}`;
}

function toast(message, kind = '') {
  const node = document.createElement('div');
  node.className = `toast ${kind}`;
  node.textContent = message;
  $('#toasts').append(node);
  setTimeout(() => node.remove(), 5200);
}

function tile(label, value, sub = '', kind = '') {
  return `<div class="tile ${kind}"><div class="label">${esc(label)}</div>
          <div class="value">${esc(value)}</div>
          ${sub ? `<div class="sub">${esc(sub)}</div>` : ''}</div>`;
}

const STATE_PILL = {
  pending: '', running: 'running', done: 'ok', failed: 'err', cancelled: '',
  queued: '', downloading: 'running', importing: 'running', ready: 'ok', virtual: '',
};

// ------------------------------------------------------------------ Login
function showLogin() {
  state.user = null;
  if (state.stream) { state.stream.close(); state.stream = null; }
  $('#app').hidden = true;
  $('#login').hidden = false;
}

async function showApp(user) {
  state.user = user;
  $('#login').hidden = true;
  $('#app').hidden = false;
  $('#brand-status').textContent = user.username;
  connectStream();
  await switchView(state.view);
}

$('#login-form').addEventListener('submit', async (event) => {
  event.preventDefault();
  const form = new FormData(event.target);
  const error = $('#login-error');
  error.hidden = true;
  try {
    const user = await api('/api/auth/login', {
      method: 'POST',
      body: {
        username: form.get('username'),
        password: form.get('password'),
        totp: form.get('totp') || null,
      },
    });
    event.target.reset();
    await showApp(user);
  } catch (exc) {
    error.textContent = exc.message;
    error.hidden = false;
    // Fordert der Server einen Code an, blenden wir das Feld nach.
    if (/Authenticator/i.test(exc.message)) $('#totp-field').hidden = false;
  }
});

$('#logout').addEventListener('click', async () => {
  try { await api('/api/auth/logout', { method: 'POST' }); } catch { /* egal */ }
  showLogin();
});

// ----------------------------------------------------------------- Router
async function switchView(view) {
  state.view = view;
  $$('#nav button').forEach((b) => b.classList.toggle('active', b.dataset.view === view));
  $$('main > section').forEach((s) => { s.hidden = s.dataset.view !== view; });
  const loaders = {
    overview: loadOverview,
    queue: loadQueue,
    jobs: loadJobs,
    library: loadLibrary,
    dupes: loadDupes,
    diagnostics: loadDiagnostics,
    account: loadAccount,
  };
  if (loaders[view]) {
    try { await loaders[view](); } catch (exc) { toast(exc.message, 'err'); }
  }
}

$('#nav').addEventListener('click', (event) => {
  const button = event.target.closest('button[data-view]');
  if (button) switchView(button.dataset.view);
});

// --------------------------------------------------------------- SSE-Live
function connectStream() {
  if (state.stream) state.stream.close();
  const stream = new EventSource('/api/events');
  state.stream = stream;

  stream.onopen = () => {
    $('#sse-state').textContent = 'live';
    $('#sse-state').className = 'pill ok';
  };
  stream.onerror = () => {
    $('#sse-state').textContent = 'getrennt';
    $('#sse-state').className = 'pill err';
  };
  stream.addEventListener('log', (event) => {
    const item = JSON.parse(event.data);
    appendLog(item);
    if (item.level === 'error') toast(item.message, 'err');
  });
  stream.addEventListener('state', (event) => {
    const snapshot = JSON.parse(event.data);
    if (state.view === 'overview') renderJobList($('#overview-jobs'), snapshot.active);
    if (state.view === 'queue') renderQueue(snapshot.queue);
  });
}

function appendLog(item) {
  const box = $('#overview-events');
  if (!box) return;
  const line = document.createElement('div');
  const time = (item.ts || '').slice(11, 19);
  line.innerHTML = `<span class="ts">${esc(time)}</span>
                    <span class="lvl-${esc(item.level)}">${esc(item.message)}</span>`;
  box.append(line);
  while (box.children.length > 200) box.firstChild.remove();
  box.scrollTop = box.scrollHeight;
}

// -------------------------------------------------------------- Übersicht
async function loadOverview() {
  const [status, recent] = await Promise.all([api('/api/status'), api('/api/recent')]);

  const nd = status.navidrome || {};
  const lib = status.library || {};
  const disk = status.storage?.music || {};
  const jobs = status.jobs || {};
  const virtual = status.virtual || {};

  $('#tiles').innerHTML = [
    tile('Navidrome', nd.online ? 'online' : 'offline',
         nd.serverVersion || nd.error || '', nd.online ? 'ok' : 'err'),
    tile('Titel indexiert', (lib.files ?? 0).toLocaleString('de-DE'),
         `${bytes(lib.bytes)} · ${duration(lib.seconds)}`),
    tile('Jobs aktiv', (jobs.pending || 0) + (jobs.running || 0),
         `${jobs.failed || 0} fehlgeschlagen`, jobs.failed ? 'warn' : ''),
    tile('On-Demand geladen', virtual.ready ?? 0,
         `${virtual.active ?? 0} unterwegs · ${virtual.failed ?? 0} Fehler`,
         virtual.failed ? 'warn' : 'ok'),
    tile('Speicher frei', bytes(disk.free), `${disk.percent ?? '?'} % belegt`,
         (disk.percent ?? 0) > 90 ? 'err' : ''),
    tile('Duplikate offen', status.duplicates?.groups ?? 0,
         `${bytes(status.duplicates?.wasted)} belegt`,
         status.duplicates?.groups ? 'warn' : 'ok'),
  ].join('');

  const active = await api('/api/jobs?state=active&limit=20');
  renderJobList($('#overview-jobs'), active.jobs);

  $('#overview-events').innerHTML = '';
  (recent.events || []).forEach(appendLog);

  $('#recent-albums').innerHTML = (recent.albums || []).map((album) => `
    <div class="album">
      <div class="name">${esc(album.name)}</div>
      <div class="artist">${esc(album.artist)}</div>
    </div>`).join('') || '<p class="muted">Noch keine Alben.</p>';
}

// ------------------------------------------------------------------ Jobs
function renderJobList(target, jobs) {
  if (!target) return;
  target.innerHTML = (jobs || []).map((job) => {
    const pill = STATE_PILL[job.state] ?? '';
    const progress = Math.round((job.progress || 0) * 100);
    const controls = job.state === 'failed' || job.state === 'cancelled'
      ? `<button class="tiny" data-retry="${job.id}">Wiederholen</button>`
      : job.state === 'pending'
        ? `<button class="tiny" data-cancel="${job.id}">Abbrechen</button>` : '';
    return `<div class="item">
      <div class="main">
        <div class="title">#${job.id} ${esc(job.type)}</div>
        <div class="sub">${esc(job.detail || job.last_error || '—')}</div>
      </div>
      <div class="side">
        ${job.state === 'running' ? `<div class="bar"><span style="width:${progress}%"></span></div>` : ''}
        <span class="pill ${pill}">${esc(job.state)}</span>
        ${controls}
      </div></div>`;
  }).join('');
}

async function loadJobs() {
  const filter = $('#job-filter').value;
  const data = await api(`/api/jobs?state=${encodeURIComponent(filter)}&limit=200`);
  renderJobList($('#jobs-list'), data.jobs);
}

$('#job-filter').addEventListener('change', loadJobs);

document.addEventListener('click', async (event) => {
  const retry = event.target.closest('[data-retry]');
  const cancel = event.target.closest('[data-cancel]');
  try {
    if (retry) {
      await api(`/api/jobs/${retry.dataset.retry}/retry`, { method: 'POST' });
      toast('Job neu eingeplant', 'ok');
      await loadJobs();
    } else if (cancel) {
      await api(`/api/jobs/${cancel.dataset.cancel}/cancel`, { method: 'POST' });
      toast('Job abgebrochen', 'ok');
      await loadJobs();
    }
  } catch (exc) { toast(exc.message, 'err'); }
});

// --------------------------------------------------------- Warteschlange
function renderQueue(items) {
  const target = $('#queue-list');
  if (!target) return;
  target.innerHTML = (items || []).map((item) => `
    <div class="item">
      <div class="main">
        <div class="title">${esc(item.artist)} — ${esc(item.title)}</div>
        <div class="sub">${esc(item.album || '')} ${item.error ? '· ' + esc(item.error) : ''}</div>
      </div>
      <div class="side">
        <span class="muted">${item.play_requests}×</span>
        <span class="pill ${STATE_PILL[item.state] ?? ''}">${esc(item.state)}</span>
      </div>
    </div>`).join('');
}

async function loadQueue() {
  const data = await api('/api/queue?limit=200');
  renderQueue(data.items);
}

// ----------------------------------------------------------------- Suche
$('#search-form').addEventListener('submit', async (event) => {
  event.preventDefault();
  const query = new FormData(event.target).get('q');
  $('#search-catalog').innerHTML = '<div class="item"><div class="main">Suche läuft…</div></div>';
  try {
    const data = await api(`/api/search?q=${encodeURIComponent(query)}`);

    $('#search-local').innerHTML = (data.local || []).map((song) => `
      <div class="item"><div class="main">
        <div class="title">${esc(song.artist)} — ${esc(song.title)}</div>
        <div class="sub">${esc(song.album || '')} · ${esc(song.suffix || '')} ${song.bitRate || ''} kbit/s</div>
      </div><div class="side"><span class="pill ok">lokal</span></div></div>`).join('');

    $('#search-catalog').innerHTML = (data.catalog || []).map((track) => {
      const known = track.known;
      const badge = known?.navidrome_id
        ? '<span class="pill ok">vorhanden</span>'
        : known ? `<span class="pill running">${esc(known.state)}</span>`
                : `<button class="tiny primary" data-download="${esc(track.provider_id)}">Laden</button>`;
      return `<div class="item"><div class="main">
        <div class="title">${esc(track.artist)} — ${esc(track.title)}</div>
        <div class="sub">${esc(track.album || '')} · ${duration(track.duration)}</div>
      </div><div class="side">${badge}</div></div>`;
    }).join('');
  } catch (exc) { toast(exc.message, 'err'); }
});

document.addEventListener('click', async (event) => {
  const button = event.target.closest('[data-download]');
  if (!button) return;
  button.disabled = true;
  try {
    await api('/api/download', { method: 'POST', body: { provider_id: button.dataset.download } });
    button.outerHTML = '<span class="pill running">eingeplant</span>';
    toast('Download eingeplant', 'ok');
  } catch (exc) {
    button.disabled = false;
    toast(exc.message, 'err');
  }
});

// ------------------------------------------------------------ Bibliothek
async function loadLibrary() {
  const [stats, issues] = await Promise.all([
    api('/api/library/stats'),
    api('/api/library/issues'),
  ]);

  $('#library-tiles').innerHTML = [
    tile('Dateien', (stats.files ?? 0).toLocaleString('de-DE'), bytes(stats.bytes)),
    tile('Spielzeit', duration(stats.seconds)),
    tile('Noch nicht analysiert', stats.unanalysed ?? 0, '', stats.unanalysed ? 'warn' : 'ok'),
    tile('Fingerprints', stats.fingerprinted ?? 0),
    tile('Ohne Cover', stats.without_cover ?? 0, '', stats.without_cover ? 'warn' : 'ok'),
    tile('Fehlend', stats.missing ?? 0, 'Datei weg, Index behalten'),
  ].join('');

  $('#library-formats').innerHTML = (stats.formats || []).map((row) => `
    <div class="item"><div class="main"><div class="title">${esc(row.ext || '?')}</div></div>
    <div class="side"><span class="muted">${row.n.toLocaleString('de-DE')}</span>
    <span class="pill">${bytes(row.bytes)}</span></div></div>`).join('');

  $('#library-issues').innerHTML = (issues.by_issue || []).map((row) => `
    <div class="item"><div class="main"><div class="title">${esc(row.issue)}</div></div>
    <div class="side"><span class="pill warn">${row.count}</span></div></div>`).join('');
}

// -------------------------------------------------------------- Duplikate
async function loadDupes() {
  const data = await api('/api/library/dupes?state=open&limit=200');
  state.dupes = data.groups || [];
  state.selectedGroups.clear();
  updateApplyButton();

  const summary = data.summary || {};
  $('#dupes-summary').innerHTML = [
    tile('Gruppen', summary.groups ?? 0),
    tile('Betroffene Dateien', summary.files ?? 0),
    tile('Rückgewinnbar', bytes(summary.wasted), '', summary.wasted ? 'warn' : 'ok'),
    ...(summary.by_kind || []).map((k) => tile(k.kind, k.n, bytes(k.wasted))),
  ].join('');

  $('#dupes-list').innerHTML = state.dupes.map((group) => `
    <div class="dupe" data-group="${group.id}">
      <div class="dupe-head">
        <input type="checkbox" data-select="${group.id}" style="width:auto">
        <span class="pill">${esc(group.kind)}</span>
        <span class="grow">${group.files} Dateien · ${bytes(group.wasted)} rückgewinnbar</span>
        <button class="tiny" data-ignore="${group.id}">Ignorieren</button>
      </div>
      ${(group.members || []).map((member) => `
        <div class="dupe-member ${member.id === group.keeper_id ? 'keeper' : ''}">
          <input type="radio" name="keeper-${group.id}" data-keeper="${group.id}"
                 value="${member.id}" ${member.id === group.keeper_id ? 'checked' : ''}
                 style="width:auto">
          <span class="path" title="${esc(member.path)}">${esc(member.path)}</span>
          <span class="pill">${esc(member.ext || '')}</span>
          <span class="muted">${member.bitrate ? Math.round(member.bitrate / 1000) + ' kbit/s' : ''}</span>
          <span class="muted">${bytes(member.size)}</span>
          ${member.similarity != null
            ? `<span class="pill running">${Math.round(member.similarity * 100)} %</span>` : ''}
        </div>`).join('')}
    </div>`).join('') || '<p class="muted">Keine offenen Duplikate. Erst eine Suche starten.</p>';
}

function updateApplyButton() {
  const button = $('#apply-dupes');
  button.disabled = state.selectedGroups.size === 0;
  button.textContent = state.selectedGroups.size
    ? `${state.selectedGroups.size} Gruppe(n) in Quarantäne`
    : 'Auswahl in Quarantäne';
}

$('#dupes-list').addEventListener('change', async (event) => {
  const select = event.target.closest('[data-select]');
  if (select) {
    const id = Number(select.dataset.select);
    select.checked ? state.selectedGroups.add(id) : state.selectedGroups.delete(id);
    updateApplyButton();
    return;
  }
  const keeper = event.target.closest('[data-keeper]');
  if (keeper) {
    try {
      await api(`/api/library/dupes/${keeper.dataset.keeper}/keeper`, {
        method: 'POST', body: { media_file_id: Number(keeper.value) },
      });
      toast('Behalten-Auswahl gespeichert', 'ok');
      $$(`.dupe[data-group="${keeper.dataset.keeper}"] .dupe-member`)
        .forEach((row) => row.classList.toggle('keeper', row.contains(keeper)));
    } catch (exc) { toast(exc.message, 'err'); }
  }
});

$('#dupes-list').addEventListener('click', async (event) => {
  const ignore = event.target.closest('[data-ignore]');
  if (!ignore) return;
  try {
    await api(`/api/library/dupes/${ignore.dataset.ignore}/ignore`, { method: 'POST' });
    ignore.closest('.dupe').remove();
  } catch (exc) { toast(exc.message, 'err'); }
});

$('#apply-dupes').addEventListener('click', async () => {
  const groups = [...state.selectedGroups];
  if (!groups.length) return;
  if (!confirm(`${groups.length} Gruppe(n) bereinigen?\n\n` +
               'Die nicht behaltenen Dateien werden in den Quarantäne-Ordner ' +
               'verschoben, nicht gelöscht.')) return;
  try {
    const result = await api('/api/library/dupes/apply', { method: 'POST', body: { groups } });
    toast(`Eingeplant. Quarantäne: ${result.quarantine}`, 'ok');
    setTimeout(loadDupes, 1500);
  } catch (exc) { toast(exc.message, 'err'); }
});

// ---------------------------------------------------------- Tag-Werkstatt
$('#tag-search').addEventListener('submit', async (event) => {
  event.preventDefault();
  const form = new FormData(event.target);
  const params = new URLSearchParams({
    q: form.get('q') || '',
    issues_only: form.get('issues_only') ? 'true' : 'false',
    limit: '200',
  });
  try {
    const data = await api(`/api/library/files?${params}`);
    $('#tag-files').innerHTML = (data.files || []).map((file) => `
      <div class="item" data-file="${file.id}">
        <div class="main">
          <div class="title">${esc(file.artist || '—')} — ${esc(file.title || file.path.split('/').pop())}</div>
          <div class="sub">${esc(file.album || '')} · ${esc(file.tag_issues || 'ok')}</div>
        </div>
        <div class="side">
          <button class="tiny" data-edit="${file.id}">Bearbeiten</button>
        </div>
      </div>`).join('');
    if (!data.files?.length) toast('Keine Treffer — evtl. zuerst indexieren.');
  } catch (exc) { toast(exc.message, 'err'); }
});

$('#tag-files').addEventListener('click', async (event) => {
  const button = event.target.closest('[data-edit]');
  if (!button) return;
  const row = button.closest('.item');
  if (row.querySelector('form')) { row.querySelector('form').remove(); return; }

  const form = document.createElement('form');
  form.className = 'row';
  form.style.width = '100%';
  form.innerHTML = `
    <input name="title" placeholder="Titel">
    <input name="artist" placeholder="Interpret">
    <input name="album" placeholder="Album">
    <input name="album_artist" placeholder="Album-Interpret">
    <input name="year" placeholder="Jahr" inputmode="numeric" style="max-width:90px">
    <input name="track_no" placeholder="Nr." inputmode="numeric" style="max-width:70px">
    <button class="primary tiny">Speichern</button>`;
  form.addEventListener('submit', async (submitEvent) => {
    submitEvent.preventDefault();
    const data = new FormData(form);
    const body = {};
    for (const [key, value] of data.entries()) {
      if (value !== '') body[key] = ['year', 'track_no'].includes(key) ? Number(value) : value;
    }
    try {
      await api(`/api/library/files/${button.dataset.edit}/tags`, { method: 'PATCH', body });
      toast('Tags gespeichert', 'ok');
      form.remove();
    } catch (exc) { toast(exc.message, 'err'); }
  });
  row.after(form);
});

// -------------------------------------------------------------- Diagnose
async function loadDiagnostics() {
  const data = await api('/api/diagnostics');
  const flag = (value) => value
    ? '<span class="pill ok">ok</span>' : '<span class="pill err">fehlt</span>';

  $('#diagnostics-body').innerHTML = `
    <div class="tiles">
      ${tile('Navidrome', data.navidrome.online ? 'online' : 'offline',
             data.navidrome.serverVersion || data.navidrome.error || '',
             data.navidrome.online ? 'ok' : 'err')}
      ${tile('Deezer-Katalog', data.deezer.reachable ? 'erreichbar' : 'blockiert', '',
             data.deezer.reachable ? 'ok' : 'err')}
      ${tile('Deemix', data.deemix.reachable ? 'erreichbar' : 'offline',
             data.deemix.known_transport ? data.deemix.known_transport.join(' ') : 'Transport unbekannt',
             data.deemix.reachable ? 'ok' : 'err')}
      ${tile('Staging-Dateien', data.staging_files, 'warten auf Import',
             data.staging_files ? 'warn' : 'ok')}
    </div>
    <div class="split">
      <div class="card">
        <h3>Werkzeuge</h3>
        <div class="list">
          ${Object.entries(data.tools).map(([name, ok]) => `
            <div class="item"><div class="main"><div class="title">${esc(name)}</div></div>
            <div class="side">${flag(ok)}</div></div>`).join('')}
        </div>
      </div>
      <div class="card">
        <h3>Pfade</h3>
        <div class="list">
          ${Object.entries(data.paths).map(([name, info]) => `
            <div class="item"><div class="main">
              <div class="title">${esc(name)}</div>
              <div class="sub mono">${esc(info.path)}</div>
            </div><div class="side">${flag(info.exists)}</div></div>`).join('')}
        </div>
      </div>
    </div>
    <div class="card">
      <h3>Deemix-Endpunkte</h3>
      <p class="muted">Der Gateway merkt sich den ersten Endpunkt, der eine
        Anfrage annimmt. Antwortet keiner, ist meist der ARL abgelaufen oder
        der Container noch nicht bereit.</p>
      <table>
        <tr><th>Pfad</th><th>Status</th><th>Antwort</th></tr>
        ${data.deemix.endpoints.map((row) => `
          <tr><td class="mono">${esc(row.path)}</td>
              <td>${row.status ?? '—'}</td>
              <td class="mono">${esc((row.preview || row.error || '').slice(0, 80))}</td></tr>`).join('')}
      </table>
    </div>`;
}

// ----------------------------------------------------------------- Konto
async function loadAccount() {
  const user = await api('/api/auth/me');
  $('#totp-state').textContent = user.totp_enabled
    ? 'Zwei-Faktor ist aktiv.'
    : 'Zwei-Faktor ist nicht eingerichtet.';
  $('#totp-off').hidden = !user.totp_enabled;
}

$('#password-form').addEventListener('submit', async (event) => {
  event.preventDefault();
  const form = new FormData(event.target);
  try {
    await api('/api/auth/password', {
      method: 'POST',
      body: { current: form.get('current'), new: form.get('new') },
    });
    event.target.reset();
    toast('Passwort geändert. Andere Sitzungen wurden beendet.', 'ok');
  } catch (exc) { toast(exc.message, 'err'); }
});

$('#totp-start').addEventListener('click', async () => {
  try {
    const data = await api('/api/auth/totp/setup', { method: 'POST' });
    $('#totp-secret').textContent = data.secret;
    $('#totp-setup').hidden = false;
  } catch (exc) { toast(exc.message, 'err'); }
});

$('#totp-enable-form').addEventListener('submit', async (event) => {
  event.preventDefault();
  const code = new FormData(event.target).get('code');
  try {
    await api('/api/auth/totp/enable', { method: 'POST', body: { code } });
    $('#totp-setup').hidden = true;
    toast('Zwei-Faktor aktiviert', 'ok');
    await loadAccount();
  } catch (exc) { toast(exc.message, 'err'); }
});

$('#totp-off').addEventListener('click', async () => {
  const code = prompt('Aktuellen Code zur Bestätigung eingeben:');
  if (!code) return;
  try {
    await api('/api/auth/totp/disable', { method: 'POST', body: { code } });
    toast('Zwei-Faktor deaktiviert', 'ok');
    await loadAccount();
  } catch (exc) { toast(exc.message, 'err'); }
});

// --------------------------------------------------------------- Aktionen
const ACTIONS = {
  'scan-navidrome': () => api('/api/scan', { method: 'POST', body: { full: false } }),
  'scan-library': () => api('/api/library/scan', { method: 'POST' }),
  'fingerprint': () => api('/api/library/fingerprint', { method: 'POST' }),
  'import-staging': () => api('/api/import-staging', { method: 'POST' }),
  'find-dupes': () => api('/api/library/dupes/find', { method: 'POST' }),
  'find-dupes-acoustic': () => api('/api/library/dupes/find?acoustic=true', { method: 'POST' }),
  'diagnostics': () => loadDiagnostics(),
};

document.addEventListener('click', async (event) => {
  const button = event.target.closest('[data-action]');
  if (!button) return;
  const action = ACTIONS[button.dataset.action];
  if (!action) return;
  button.disabled = true;
  try {
    await action();
    toast('Auftrag eingeplant', 'ok');
  } catch (exc) {
    toast(exc.message, 'err');
  } finally {
    button.disabled = false;
  }
});

// ------------------------------------------------------------------ Start
(async function boot() {
  try {
    const user = await api('/api/auth/me');
    await showApp(user);
  } catch {
    showLogin();
  }
})();
