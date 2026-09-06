// Übersicht: Zustand des Stacks auf einen Blick.

import { get, post } from '../core/api.js';
import { $, esc, icon, bytes, duration, num, tile, skeleton, failure, empty, zustandPill, stufePill, balkenFuellen } from '../core/dom.js';
import * as bus from '../core/bus.js';
import { ok, fail } from '../core/toast.js';

export const meta = { id: 'overview', titel: 'Übersicht' };

export async function mount(wurzel, ctx) {
  wurzel.innerHTML = `
    <div class="page-head">
      <div>
        <h2>Übersicht</h2>
        <p class="lede">Zustand von Navidrome, Deemix und der Bibliothek.</p>
      </div>
      ${ctx.istAdmin() ? `<div class="page-actions">
        <button type="button" class="btn" data-do="scan-nd">Navidrome-Scan</button>
        <button type="button" class="btn" data-do="scan-lib">Bibliothek indexieren</button>
      </div>` : ''}
    </div>
    <div id="ov-hints"></div>
    <div class="tiles" id="ov-tiles">${skeleton(1)}</div>
    <div class="split">
      <div class="card card-flush">
        <h3>Aktive Jobs</h3>
        <div id="ov-jobs" class="list"></div>
      </div>
      <div class="card">
        <h3>Ereignisse</h3>
        <div id="ov-events" class="list" aria-live="polite"></div>
      </div>
    </div>
    <div class="card">
      <h3>Zuletzt hinzugefügt</h3>
      <div id="ov-albums"></div>
    </div>`;

  async function ladeStatus() {
    try {
      const s = await get('/api/status');
      zeichneKacheln(s);
      zeichneHinweise(s);
    } catch (exc) {
      if (!$('#ov-tiles')) return;
      $('#ov-tiles').innerHTML = '';
      $('#ov-hints').innerHTML = failure('Status nicht abrufbar.', exc.message);
    }
  }

  function zeichneKacheln(s) {
    const nd = s.navidrome || {};
    const lib = s.library || {};
    const jobs = s.jobs || {};
    const w = s.worker || {};
    const v = s.virtual || {};
    const disk = s.storage?.music || {};

    if (!$('#ov-tiles')) return;
    $('#ov-tiles').innerHTML = [
      tile('Navidrome',
           nd.online ? (nd.authenticated ? 'online' : 'ohne Zugang') : 'offline',
           nd.serverVersion || nd.note || nd.error || '',
           nd.online ? (nd.authenticated ? 'ok' : 'warn') : 'err'),
      tile('Titel indexiert', num(lib.files), `${bytes(lib.bytes)} · ${duration(lib.seconds)}`),
      tile('Jobs aktiv', num((jobs.pending || 0) + (jobs.running || 0)),
           w.alive ? `${num(jobs.failed)} fehlgeschlagen` : 'Worker antwortet nicht',
           w.alive ? (jobs.failed ? 'warn' : '') : 'err'),
      tile('On-Demand geladen', num(v.ready),
           `${num(v.active)} unterwegs · ${num(v.failed)} Fehler`,
           v.failed ? 'warn' : 'ok'),
      tile('Speicher frei', bytes(disk.free), `${disk.percent ?? '?'} % belegt`,
           (disk.percent ?? 0) > 90 ? 'err' : ''),
      tile('Duplikate offen', num(s.duplicates?.groups),
           `${bytes(s.duplicates?.wasted)} belegt`, s.duplicates?.groups ? 'warn' : 'ok'),
    ].join('');
  }

  function zeichneHinweise(s) {
    const nd = s.navidrome || {};
    const w = s.worker || {};
    const hinweise = [];

    if (!w.alive) {
      hinweise.push(`<div class="notice notice-err">${icon('warn')}<div>
        <strong>Der Worker antwortet nicht.</strong> Downloads, Import und Scans bleiben
        liegen — angeforderte Titel hängen dann auf „queued“.
        ${w.ever_seen ? `Letztes Lebenszeichen: vor ${Math.round((w.age || 0) / 60)} Minuten.`
                      : 'Er hat sich noch nie gemeldet.'}
        Prüfen mit <code>docker logs music-gateway-worker</code>.
      </div></div>`);
    }
    if (nd.online && !nd.authenticated) {
      hinweise.push(`<div class="notice">${icon('info')}<div>
        Kein Navidrome-Zugang — importierte Titel lassen sich nicht auf ihre ID auflösen.
        <a href="#/diagnostics">Unter Diagnose eintragen</a> oder einmal mit einem
        Musik-Client auf Port 8080 anmelden.
      </div></div>`);
    }
    $('#ov-hints').innerHTML = hinweise.join('');
  }

  function zeichneJobs(liste) {
    const el = $('#ov-jobs');
    if (!el) return;
    el.innerHTML = (liste || []).length
      ? liste.map((j) => `
          <div class="item">
            <div class="item-main">
              <div class="item-title">#${j.id} ${esc(j.type)}</div>
              <div class="item-sub">${esc(j.detail || j.last_error || '—')}</div>
            </div>
            <div class="item-side">
              ${j.state === 'running'
                ? `<div class="bar" data-anteil="${Math.round((j.progress || 0) * 100)}"><span></span></div>` : ''}
              ${zustandPill(j.state)}
            </div>
          </div>`).join('')
      : empty('Nichts in Arbeit.', 'Hier erscheinen Downloads, Importe und Scans, während sie laufen.');
    balkenFuellen(el);
  }

  function zeichneEreignis(e) {
    const el = $('#ov-events');
    if (!el) return;
    const zeit = (e.ts || '').slice(11, 19);
    el.insertAdjacentHTML('afterbegin', `
      <div class="item">
        <div class="item-main">
          <div class="item-title">${esc(e.message)}</div>
          <div class="item-sub"><time>${esc(zeit)}</time> · ${esc(e.category)}</div>
        </div>
        <div class="item-side">${stufePill(e.level)}</div>
      </div>`);
    while (el.children.length > 40) el.lastElementChild.remove();
  }

  // --- Daten holen -------------------------------------------------------
  await ladeStatus();

  get('/api/jobs?state=active&limit=20').then((d) => zeichneJobs(d.jobs)).catch(() => {});

  get('/api/recent').then((d) => {
    $('#ov-events').innerHTML = '';
    (d.events || []).forEach(zeichneEreignis);
    const alben = d.albums || [];
    if (!$('#ov-albums')) return;
    $('#ov-albums').innerHTML = alben.length
      ? `<div class="grid-cards">${alben.map((a) => `
          <div class="cover-card">
            <div class="cover-art"></div>
            <div class="cover-title">${esc(a.name)}</div>
            <div class="cover-sub">${esc(a.artist)}</div>
          </div>`).join('')}</div>`
      : empty('Noch keine Alben.', 'Sobald Navidrome erreichbar ist und Titel indexiert sind, stehen sie hier.');
  }).catch((exc) => {
    if ($('#ov-albums')) $('#ov-albums').innerHTML = failure('Alben nicht abrufbar.', exc.message);
  });

  // --- Live bleiben ------------------------------------------------------
  const abLog = bus.on('log', zeichneEreignis);
  const abState = bus.on('state', (s) => zeichneJobs(s.active));
  const takt = setInterval(ladeStatus, 20000);

  wurzel.addEventListener('click', async (event) => {
    const knopf = event.target.closest('[data-do]');
    if (!knopf) return;
    knopf.disabled = true;
    try {
      if (knopf.dataset.do === 'scan-nd') {
        await post('/api/scan', { full: false });
        ok('Navidrome-Scan eingeplant');
      } else {
        await post('/api/library/scan');
        ok('Bibliotheks-Scan eingeplant');
      }
    } catch (exc) { fail(exc.message); } finally { knopf.disabled = false; }
  });

  // Aufräumen: ohne das laufen Zeitgeber und Zuhörer nach dem Wechsel weiter.
  return () => { abLog(); abState(); clearInterval(takt); };
}
