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
    // Ein 401 beim Start ist normal (noch nicht angemeldet). Ein 401, während
    // eine Sitzung läuft, heißt: das Cookie kommt nicht zurück. Das früher
    // stumm zu behandeln sah aus wie ein hängendes Login-Fenster.
    showLogin(state.user
      ? 'Sitzung wurde nicht angenommen. Das Anmelde-Cookie kommt nicht zurück — '
        + 'meist ein Cache-Problem (Strg+Shift+R) oder ein Browser, der Cookies blockiert.'
      : null);
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

// Ansichten, die nur Administratoren offenstehen.
const ADMIN_VIEWS = new Set(['library', 'dupes', 'tags', 'logs', 'diagnostics', 'users']);

const STATE_PILL = {
  pending: '', running: 'running', done: 'ok', failed: 'err', cancelled: '',
  queued: '', downloading: 'running', importing: 'running', ready: 'ok', virtual: '',
};

// ------------------------------------------------------------------ Login
function showLogin(message = null) {
  state.user = null;
  if (state.stream) { state.stream.close(); state.stream = null; }
  $('#app').hidden = true;
  $('#login').hidden = false;
  // Nur setzen, nie löschen: eine Ansicht feuert mehrere Anfragen parallel,
  // und der zweite 401 würde sonst die Meldung des ersten wieder wegräumen.
  // Geleert wird ausschließlich beim nächsten Anmeldeversuch.
  if (message) {
    const box = $('#login-error');
    box.textContent = message;
    box.hidden = false;
  }
}

async function showApp(user) {
  state.user = user;
  $('#login').hidden = true;
  $('#app').hidden = false;
  $('#brand-status').textContent = user.role === 'admin'
    ? `${user.username} · Administrator` : user.username;

  // Was ein Benutzer ohnehin nicht darf, wird gar nicht erst angeboten.
  // Die Endpunkte lehnen es unabhängig davon ab — das hier ist reine
  // Aufgeräumtheit, kein Sicherheitsmerkmal.
  const admin = user.role === 'admin';
  $$('#nav button[data-admin]').forEach((b) => { b.hidden = !admin; });
  $$('[data-action="scan-navidrome"], [data-action="scan-library"], '
     + '[data-action="clear-failed"], [data-action="import-staging"]')
    .forEach((b) => { b.hidden = !admin; });

  if (!admin && ADMIN_VIEWS.has(state.view)) state.view = 'overview';
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
    logs: loadLogs,
    users: loadUsers,
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

// Verweise aus Hinweistexten heraus, etwa "Unter Diagnose eintragen".
document.addEventListener('click', (event) => {
  const link = event.target.closest('[data-view-link]');
  if (!link) return;
  event.preventDefault();
  switchView(link.dataset.viewLink);
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
    // Auf der Protokollseite live oben anhängen, solange "mitlaufen" an ist.
    if (state.view === 'logs' && $('#log-follow')?.checked) {
      const box = $('#log-entries');
      box.insertAdjacentHTML('afterbegin', logRow(item));
      while (box.children.length > 600) box.lastChild.remove();
    }
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

  // Ohne Navidrome-Zugang laeuft der Import nur halb - das gehoert nach oben,
  // nicht in eine Kachel, die man fuer Deko halten kann.
  $('#overview-hint').innerHTML = (nd.online && !nd.authenticated)
    ? `<p class="notice">Kein Navidrome-Zugang. Importierte Titel lassen sich
         dann nicht auf ihre ID auflösen.
         <a href="#" data-view-link="diagnostics">Unter Diagnose eintragen</a> —
         oder einmal mit einem Musik-Client auf Port 8080 anmelden.</p>`
    : '';

  $('#tiles').innerHTML = [
    tile('Navidrome',
         nd.online ? (nd.authenticated ? 'online' : 'ohne Zugang') : 'offline',
         nd.serverVersion || nd.note || nd.error || '',
         nd.online ? (nd.authenticated ? 'ok' : 'warn') : 'err'),
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
        ${item.state === 'failed' && item.provider_id
          ? `<button class="tiny" data-download="${esc(item.provider_id)}">Erneut</button>` : ''}
        ${state.user?.role === 'admin'
          ? `<button class="tiny ghost" data-forget="${esc(item.id)}"
               title="Aus der Liste entfernen">✕</button>` : ''}
      </div>
    </div>`).join('');
}

async function loadQueue() {
  const data = await api('/api/queue?limit=200');
  renderQueue(data.items);
}

$('#queue-list').addEventListener('click', async (event) => {
  const button = event.target.closest('[data-forget]');
  if (!button) return;
  button.disabled = true;
  try {
    await api(`/api/queue/${encodeURIComponent(button.dataset.forget)}`, { method: 'DELETE' });
    button.closest('.item').remove();
  } catch (exc) {
    button.disabled = false;
    toast(exc.message, 'err');
  }
});

// ----------------------------------------------------------------- Suche
$('#search-form').addEventListener('submit', async (event) => {
  event.preventDefault();
  const query = new FormData(event.target).get('q');
  $('#search-catalog').innerHTML = '<div class="item"><div class="main">Suche läuft…</div></div>';
  try {
    const data = await api(`/api/search?q=${encodeURIComponent(query)}`);

    const hinweis = data.corrected
      ? `<div class="item"><div class="main"><div class="sub">
           Keine Treffer für „${esc(query)}“ — zeige „${esc(data.corrected)}“
         </div></div></div>`
      : '';

    $('#search-local').innerHTML = hinweis + (data.local || []).map((song) => `
      <div class="item"><div class="main">
        <div class="title">${esc(song.artist)} — ${esc(song.title)}</div>
        <div class="sub">${esc(song.album || '')} · ${esc(song.suffix || '')} ${song.bitRate || ''} kbit/s</div>
      </div><div class="side"><span class="pill ok">lokal</span></div></div>`).join('');

    $('#search-catalog').innerHTML = (data.catalog || []).map((track) => {
      const known = track.known;
      // Ein fehlgeschlagener Titel muss erneut anstoßbar sein und seinen
      // Grund zeigen - sonst steht dort nur ein Zustand ohne Erklärung.
      let badge;
      if (known?.navidrome_id) {
        badge = '<span class="pill ok">vorhanden</span>';
      } else if (known && known.state === 'failed') {
        badge = `<span class="pill err">fehlgeschlagen</span>
                 <button class="tiny" data-download="${esc(track.provider_id)}">Erneut</button>`;
      } else if (known && known.state !== 'virtual') {
        badge = `<span class="pill running">${esc(known.state)}</span>`;
      } else {
        badge = `<button class="tiny primary" data-download="${esc(track.provider_id)}">Laden</button>`;
      }
      const grund = known?.error ? ` · ${esc(known.error)}` : '';
      return `<div class="item"><div class="main">
        <div class="title">${esc(track.artist)} — ${esc(track.title)}</div>
        <div class="sub">${esc(track.album || '')} · ${duration(track.duration)}${grund}</div>
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

// ------------------------------------------------------------- Protokoll
function logRow(item) {
  const zeit = (item.ts || '').replace('T', ' ').slice(0, 19);
  const daten = item.data && item.data !== 'null'
    ? `<div class="log-data">${esc(item.data)}</div>` : '';
  return `<div class="log-line lvl-${esc(item.level)}">
    <span class="ts">${esc(zeit)}</span>
    <span class="pill">${esc(item.category)}</span>
    <span class="log-msg">${esc(item.message)}</span>
    ${daten}
  </div>`;
}

async function loadLogs() {
  const form = $('#log-filter');
  const data = new FormData(form);
  const params = new URLSearchParams({
    level: data.get('level') || 'all',
    category: data.get('category') || 'all',
    q: data.get('q') || '',
    limit: '500',
  });
  const result = await api(`/api/logs?${params}`);

  // Bereichsliste nachziehen, ohne die Auswahl zu verlieren.
  const select = form.querySelector('[name=category]');
  const gewaehlt = select.value;
  select.innerHTML = '<option value="all">alle Bereiche</option>'
    + (result.categories || []).map((c) => `<option value="${esc(c)}">${esc(c)}</option>`).join('');
  select.value = gewaehlt;

  $('#log-entries').innerHTML = (result.entries || []).map(logRow).join('')
    || '<p class="muted">Keine Einträge für diesen Filter.</p>';
}

$('#log-filter').addEventListener('submit', (event) => {
  event.preventDefault();
  loadLogs().catch((exc) => toast(exc.message, 'err'));
});

// -------------------------------------------------------------- Diagnose
const CHECK_PILL = { ok: 'ok', warn: 'warn', fail: 'err' };
const CHECK_LABEL = { ok: 'ok', warn: 'hinweis', fail: 'fehler' };

async function loadDiagnostics() {
  // Die Prüfung fragt echte Dienste ab und braucht ein paar Sekunden. Ohne
  // Platzhalter sieht die leere Seite so lange aus wie ein Fehler.
  $('#diagnostics-body').innerHTML =
    '<div class="card"><p class="muted">Prüfe Pfade, Rechte und Dienste…</p></div>';
  const [data, pre] = await Promise.all([api('/api/diagnostics'), api('/api/preflight')]);
  const flag = (value) => value
    ? '<span class="pill ok">ok</span>' : '<span class="pill err">fehlt</span>';

  const readiness = pre.ready
    ? `<p class="notice">Startprüfung bestanden — bereit für den ersten Download.
       ${pre.counts.warn} Hinweis(e).</p>`
    : `<p class="notice" style="border-color:var(--err);background:#2a1618;color:#ffb4ae">
       ${pre.counts.fail} Prüfung(en) fehlgeschlagen. Vor dem ersten Download beheben —
       sonst laufen Downloads ins Leere oder landen an der falschen Stelle.</p>`;

  const cred = await api('/api/navidrome/credentials');
  const quelle = { env: 'aus der Umgebung (NAVIDROME_PASSWORD)',
                   manual: 'hier hinterlegt',
                   borrowed: 'von einem angemeldeten Musik-Client übernommen' };

  const zugang = cred.configured
    ? `<p>Verbunden als <strong>${esc(cred.username)}</strong> —
         ${esc(quelle[cred.source] || 'vorhanden')}.</p>
       ${cred.editable
         ? '<button id="cred-clear" class="ghost">Zugang entfernen</button>'
         : '<p class="muted">In der Umgebung gesetzt — hier nicht änderbar.</p>'}`
    : `<p class="muted">Der Gateway braucht einen Navidrome-Zugang, um importierte
         Titel auf ihre ID aufzulösen und Scans anzustoßen. Es genügt ein
         normaler Navidrome-Benutzer; für den Scan-Anstoß ein Administrator.</p>
       <form id="cred-form" class="row">
         <input name="username" placeholder="Navidrome-Benutzer" autocomplete="off" required>
         <input name="password" type="password" placeholder="Passwort"
                autocomplete="off" required>
         <button class="primary" type="submit">Verbinden</button>
       </form>
       <p class="muted" style="margin-top:.6rem">
         Das Passwort wird nicht gespeichert — daraus entsteht einmalig ein
         Subsonic-Token, und nur das liegt in der Datenbank.
       </p>`;

  const arl = await api('/api/deemix/arl');
  const deezer = arl.configured
    ? `<p>ARL hinterlegt <span class="muted mono">${esc(arl.hint || '')}</span> —
         der Gateway meldet sich damit selbst bei Deemix an.</p>
       <button id="arl-clear" class="ghost">ARL entfernen</button>`
    : `<p class="muted">Deemix hält die Deezer-Anmeldung <strong>pro Browser-Sitzung</strong>.
         Dass die Deemix-Oberfläche angemeldet ist, hilft dem Gateway nicht — er ist
         ein eigener Client. Trag den ARL hier ein, dann meldet er sich selbst an.</p>
       <form id="arl-form" class="row">
         <input name="arl" type="password" placeholder="ARL (nur Hex-Zeichen)"
                autocomplete="off" required>
         <button class="primary" type="submit">Anmelden</button>
       </form>
       <p class="muted" style="margin-top:.6rem">
         Wird vor dem Speichern gegen Deezer geprüft und danach nie wieder
         ausgegeben — im Dashboard stehen nur die letzten Zeichen.
       </p>`;

  $('#diagnostics-body').innerHTML = `
    ${readiness}
    <div class="card">
      <h3>Navidrome-Zugang</h3>
      ${zugang}
    </div>
    <div class="card">
      <h3>Deemix-Anmeldung (Deezer-ARL)</h3>
      ${deezer}
    </div>
    <div class="card">
      <h3>Zugriffe von Musik-Clients</h3>
      <p class="muted">Die letzten Anfragen, die tatsächlich hier ankommen.
        Bleibt die Liste leer, während du im Client suchst, zeigt der Client
        nicht auf den Gateway.</p>
      <button data-action="client-activity">Aktualisieren</button>
      <div id="client-activity" class="list" style="margin-top:.8rem"></div>
    </div>
    <div class="card">
      <h3>Was ein Musik-Client sieht</h3>
      <p class="muted">Fragt den eigenen Subsonic-Endpunkt genauso ab wie
        Substreamer. Kommen hier Titel mit Marker zurück, liegt ein Problem
        im Client — meist zeigt er auf Port 4533 statt 8080.
        <strong>Navidromes eigene Weboberfläche kann sie nie zeigen</strong>,
        die läuft am Gateway vorbei.</p>
      <button data-action="client-test">Test ausführen</button>
      <div id="client-test-result" style="margin-top:.8rem"></div>
    </div>
    <div class="card">
      <h3>Startprüfung</h3>
      <div class="list">
        ${pre.checks.map((check) => `
          <div class="item">
            <div class="main">
              <div class="title">${esc(check.name)}</div>
              <div class="sub">${esc(check.detail)}</div>
            </div>
            <div class="side">
              <span class="pill ${CHECK_PILL[check.status]}">${CHECK_LABEL[check.status]}</span>
            </div>
          </div>`).join('')}
      </div>
    </div>
    <div class="tiles">
      ${tile('Navidrome',
             data.navidrome.online
               ? (data.navidrome.authenticated ? 'online' : 'ohne Zugang') : 'offline',
             data.navidrome.serverVersion || data.navidrome.note
               || data.navidrome.error || '',
             data.navidrome.online
               ? (data.navidrome.authenticated ? 'ok' : 'warn') : 'err')}
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

// Formular und Knopf entstehen erst beim Rendern der Diagnose-Seite,
// deshalb delegiert am Dokument statt direkt gebunden.
document.addEventListener('submit', async (event) => {
  const form = event.target.closest('#cred-form');
  if (!form) return;
  event.preventDefault();
  const data = new FormData(form);
  const button = form.querySelector('button');
  button.disabled = true;
  try {
    await api('/api/navidrome/credentials', {
      method: 'POST',
      body: { username: data.get('username'), password: data.get('password') },
    });
    toast('Mit Navidrome verbunden', 'ok');
    await loadDiagnostics();
  } catch (exc) {
    toast(exc.message, 'err');
    button.disabled = false;
  }
});

document.addEventListener('submit', async (event) => {
  const form = event.target.closest('#arl-form');
  if (!form) return;
  event.preventDefault();
  const button = form.querySelector('button');
  button.disabled = true;
  try {
    const info = await api('/api/deemix/arl', {
      method: 'POST',
      body: { arl: new FormData(form).get('arl') },
    });
    toast(`Bei Deemix angemeldet als ${info.user || 'unbekannt'}`, 'ok');
    await loadDiagnostics();
  } catch (exc) {
    toast(exc.message, 'err');
    button.disabled = false;
  }
});

document.addEventListener('click', async (event) => {
  const navidrome = event.target.closest('#cred-clear');
  const deemixArl = event.target.closest('#arl-clear');
  if (!navidrome && !deemixArl) return;
  try {
    await api(navidrome ? '/api/navidrome/credentials' : '/api/deemix/arl',
              { method: 'DELETE' });
    toast('Entfernt', 'ok');
    await loadDiagnostics();
  } catch (exc) { toast(exc.message, 'err'); }
});

// -------------------------------------------------------------- Benutzer
const ROLLE = { admin: 'Administrator', user: 'Benutzer' };

async function loadUsers() {
  const { users } = await api('/api/users');
  $('#user-list').innerHTML = users.map((u) => `
    <div class="item">
      <div class="main">
        <div class="title">${esc(u.username)}${u.self ? ' <span class="muted">(du)</span>' : ''}</div>
        <div class="sub">seit ${esc((u.created_at || '').slice(0, 10))}
          · ${u.last_login_at ? 'zuletzt ' + esc(u.last_login_at.slice(0, 16)) : 'nie angemeldet'}
          · ${u.sessions} offene Sitzung(en)${u.totp_enabled ? ' · 2FA' : ''}</div>
      </div>
      <div class="side">
        <select data-role="${u.id}" ${u.self ? 'disabled' : ''}>
          <option value="user" ${u.role === 'user' ? 'selected' : ''}>Benutzer</option>
          <option value="admin" ${u.role === 'admin' ? 'selected' : ''}>Administrator</option>
        </select>
        <button class="tiny" data-reset="${u.id}" data-name="${esc(u.username)}">Passwort</button>
        ${u.self ? '' : `<button class="tiny danger" data-deluser="${u.id}"
                          data-name="${esc(u.username)}">Löschen</button>`}
      </div>
    </div>`).join('');
}

$('#user-create').addEventListener('submit', async (event) => {
  event.preventDefault();
  const data = new FormData(event.target);
  try {
    await api('/api/users', {
      method: 'POST',
      body: {
        username: data.get('username'),
        password: data.get('password'),
        role: data.get('role'),
      },
    });
    event.target.reset();
    toast('Benutzer angelegt', 'ok');
    await loadUsers();
  } catch (exc) { toast(exc.message, 'err'); }
});

$('#user-list').addEventListener('change', async (event) => {
  const select = event.target.closest('[data-role]');
  if (!select) return;
  try {
    await api(`/api/users/${select.dataset.role}`, {
      method: 'PATCH', body: { role: select.value },
    });
    toast('Rolle geändert', 'ok');
    await loadUsers();
  } catch (exc) {
    toast(exc.message, 'err');
    await loadUsers();
  }
});

$('#user-list').addEventListener('click', async (event) => {
  const reset = event.target.closest('[data-reset]');
  const remove = event.target.closest('[data-deluser]');
  try {
    if (reset) {
      const pass = prompt(`Neues Passwort für ${reset.dataset.name} (min. 10 Zeichen):`);
      if (!pass) return;
      await api(`/api/users/${reset.dataset.reset}`, {
        method: 'PATCH', body: { password: pass },
      });
      toast('Passwort gesetzt. Offene Sitzungen wurden beendet.', 'ok');
      await loadUsers();
    } else if (remove) {
      if (!confirm(`${remove.dataset.name} wirklich löschen?`)) return;
      await api(`/api/users/${remove.dataset.deluser}`, { method: 'DELETE' });
      toast('Benutzer gelöscht', 'ok');
      await loadUsers();
    }
  } catch (exc) { toast(exc.message, 'err'); }
});

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
  // Aktionen dürfen eine eigene Rückmeldung liefern; sonst gilt die Vorgabe.
  'clear-failed': async () => {
    const result = await api('/api/queue/clear-failed', { method: 'POST' });
    await loadQueue();
    return `${result.removed} Eintrag/Einträge entfernt`;
  },
  'find-dupes': () => api('/api/library/dupes/find', { method: 'POST' }),
  'find-dupes-acoustic': () => api('/api/library/dupes/find?acoustic=true', { method: 'POST' }),
  'diagnostics': async () => { await loadDiagnostics(); return 'Diagnose aktualisiert'; },
  'logs-refresh': async () => { await loadLogs(); return 'Protokoll aktualisiert'; },
  'client-activity': async () => {
    const { requests } = await api('/api/client-activity');
    $('#client-activity').innerHTML = requests.length
      ? requests.map((r) => `
          <div class="item">
            <div class="main">
              <div class="title">${esc(r.endpoint)}${r.query ? ` — „${esc(r.query)}“` : ''}</div>
              <div class="sub">${esc(r.ts)} · ${esc(r.detail || '')}</div>
            </div>
            <div class="side">
              <span class="pill">${esc(r.client)}</span>
              <span class="muted">${esc(r.user)}</span>
            </div>
          </div>`).join('')
      : `<div class="item"><div class="main"><div class="sub">Noch kein
           Musik-Client hier angekommen. Such einmal im Client und lade dann
           neu — bleibt es leer, zeigt er auf den falschen Port.</div></div></div>`;
    return `${requests.length} Zugriff(e)`;
  },
  'client-test': async () => {
    const box = $('#client-test-result');
    box.innerHTML = '<p class="muted">Frage den eigenen Subsonic-Endpunkt ab…</p>';
    try {
      const r = await api('/api/client-test?q=Mark%20Forster');
      box.innerHTML = r.virtual > 0
        ? `<p class="notice">Der Gateway liefert <strong>${r.local} lokale</strong> und
             <strong>${r.virtual} noch nicht geladene</strong> Titel für „${esc(r.query)}“.
             Ein Musik-Client auf Port 8080 sieht genau das.
             ${r.beispiele.length ? 'Zum Beispiel: ' + esc(r.beispiele.join(', ')) : ''}</p>`
        : `<p class="notice" style="border-color:var(--warn);background:#2a2413;color:#ffd88a">
             Der Gateway liefert keine noch nicht geladenen Titel. Prüfe unter
             Diagnose, ob der Katalog (Deezer) erreichbar ist.</p>`;
      return 'Test ausgeführt';
    } catch (exc) {
      box.innerHTML = `<p class="notice" style="border-color:var(--err);background:#2a1618;color:#ffb4ae">
                         ${esc(exc.message)}</p>`;
      throw exc;
    }
  },
};

document.addEventListener('click', async (event) => {
  const button = event.target.closest('[data-action]');
  if (!button) return;
  const action = ACTIONS[button.dataset.action];
  if (!action) return;
  button.disabled = true;
  try {
    const message = await action();
    toast(typeof message === 'string' ? message : 'Auftrag eingeplant', 'ok');
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
