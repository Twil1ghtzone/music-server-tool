// Jobs: was der Worker abarbeitet, mit Fortschritt und Fehlergrund.

import { get, post, q } from '../core/api.js';
import { $, esc, empty, failure, skeleton, relativeTime, num, zustandPill, balkenFuellen } from '../core/dom.js';
import * as bus from '../core/bus.js';
import * as prefs from '../core/prefs.js';
import { werkzeugleiste, anwenden } from '../core/toolbar.js';
import { ok, fail } from '../core/toast.js';

export const meta = { id: 'jobs', titel: 'Jobs' };

// Die Typnamen kommen englisch aus der Datenbank. Hier stehen sie einmal
// lesbar — ein unbekannter Typ wird durchgereicht statt verschluckt.
const TYP = {
  download_track: 'Titel herunterladen',
  download_release: 'Veröffentlichung am Stück',
  import_staging: 'Staging importieren',
  navidrome_scan: 'Navidrome-Scan',
  library_scan: 'Bibliothek indizieren',
  hash_files: 'Prüfsummen bilden',
  fingerprint: 'Fingerabdrücke berechnen',
  find_dupes: 'Duplikate suchen',
  apply_dupes: 'Duplikate in Quarantäne',
};

export async function mount(wurzel, ctx) {
  const admin = ctx.istAdmin();
  let jobs = [];

  wurzel.innerHTML = `
    <div class="page-head">
      <div>
        <h2>Jobs</h2>
        <p class="lede">Downloads, Importe, Scans und Duplikatläufe. Ein fehlgeschlagener
          Job nennt oben in seiner Zeile den Grund — meistens steht dort schon,
          was zu tun ist.</p>
      </div>
    </div>
    <div class="tiles" id="job-tiles"></div>
    <div id="job-leiste"></div>
    <div class="card card-flush"><div id="job-list" class="list">${skeleton(6)}</div></div>`;

  const leiste = werkzeugleiste({
    id: 'jobs',
    standard: { q: '', zustand: 'all', sort: 'id', richtung: 'ab', technisch: false, grenze: 200 },
    suche: { platzhalter: 'Nach Art oder Meldung filtern…', label: 'Jobs filtern' },
    filter: [{
      name: 'zustand',
      label: 'Nach Zustand filtern',
      werte: [['all', 'Alle'], ['active', 'Aktiv'], ['failed', 'Fehlgeschlagen'], ['done', 'Erledigt']],
    }],
    sortierung: [['id', 'Nummer'], ['updated_at', 'Zuletzt geändert'], ['type', 'Art']],
    zusatz: [
      { name: 'technisch', art: 'schalter', label: 'Technische Namen zeigen',
        hinweis: 'download_release statt „Album am Stück“' },
      { name: 'grenze', art: 'auswahl', zahl: true, label: 'Wie viele laden',
        werte: [[50, '50'], [200, '200'], [500, '500']] },
    ],
    beiAenderung: (w, geaendert) => {
      // Zustand und Anzahl beantwortet der Server, alles andere hier.
      if (geaendert === 'zustand' || geaendert === 'grenze' || geaendert === 'reset') lade();
      else zeichne();
    },
  });
  $('#job-leiste').replaceWith(leiste.el);

  function name(j) {
    return leiste.werte().technisch ? j.type : (TYP[j.type] || j.type);
  }

  function zeichne(neue) {
    if (neue) jobs = neue;
    const el = $('#job-list');
    if (!el) return;

    const w = leiste.werte();
    const zeilen = anwenden(
      jobs.map((j) => ({ ...j, _name: name(j) })), w,
      {
        felder: ['_name', 'type', 'detail', 'last_error'],
        sortierer: {
          id: (j) => j.id,
          updated_at: (j) => j.updated_at || '',
          type: (j) => j._name || '',
        },
      });

    leiste.setzeZaehler(zeilen.length === jobs.length
      ? `${jobs.length}` : `${zeilen.length} von ${jobs.length}`);

    if (!jobs.length) {
      el.innerHTML = empty('Keine Jobs für diesen Filter.',
        'Der Worker hatte für diesen Zustand nichts zu tun.');
      return;
    }
    if (!zeilen.length) {
      el.innerHTML = empty('Kein Job passt zur Suche.', 'Leere das Suchfeld in der Leiste oben.');
      return;
    }

    el.innerHTML = zeilen.map((j) => {
      const prozent = Math.round((j.progress || 0) * 100);
      const wiederholbar = admin && ['failed', 'cancelled'].includes(j.state);
      const abbrechbar = admin && j.state === 'pending';
      return `
        <div class="item">
          <div class="item-main">
            <div class="item-title"><span class="faint num">#${j.id}</span> ${esc(j._name)}</div>
            <div class="item-sub">${esc(j.detail || j.last_error || '—')} · ${esc(relativeTime(j.updated_at))}${
              j.attempts > 1 ? ` · Versuch ${j.attempts}/${j.max_attempts}` : ''}</div>
          </div>
          <div class="item-side">
            ${j.state === 'running'
              ? `<div class="bar" data-anteil="${prozent}"><span></span></div>
                 <span class="tiny faint num">${prozent}&nbsp;%</span>` : ''}
            ${zustandPill(j.state)}
            ${wiederholbar ? `<button type="button" class="btn btn-sm" data-retry="${j.id}">Wiederholen</button>` : ''}
            ${abbrechbar ? `<button type="button" class="btn btn-sm btn-ghost" data-cancel="${j.id}">Abbrechen</button>` : ''}
          </div>
        </div>`;
    }).join('');
    balkenFuellen(el);
  }

  function zeichneKacheln(stats) {
    if (!$('#job-tiles')) return;
    $('#job-tiles').innerHTML = ['pending', 'running', 'done', 'failed']
      .map((k) => `<div class="tile ${k === 'failed' && stats[k] ? 'tile-err' : ''}${
        k === 'running' && stats[k] ? ' tile-ok' : ''}">
        <div class="tile-label">${{ pending: 'Wartend', running: 'Läuft',
                                    done: 'Erledigt', failed: 'Fehlgeschlagen' }[k]}</div>
        <div class="tile-value">${num(stats[k])}</div>
      </div>`).join('');
  }

  async function lade() {
    try {
      const w = leiste.werte();
      const d = await get(`/api/jobs${q({ state: w.zustand, limit: w.grenze })}`);
      zeichne(d.jobs);
      zeichneKacheln(d.stats || {});
    } catch (exc) {
      if ($('#job-list')) $('#job-list').innerHTML = failure('Jobs nicht abrufbar.', exc.message);
    }
  }
  await lade();

  // Nur bei „aktiv" live nachziehen - sonst springt die Liste unter der Hand.
  const abState = bus.on('state', (s) => {
    zeichneKacheln(s.jobs || {});
    if (leiste.werte().zustand === 'active' && prefs.hole('autoAktualisieren')) zeichne(s.active);
  });

  wurzel.addEventListener('click', async (event) => {
    const erneut = event.target.closest('[data-retry]');
    const abbruch = event.target.closest('[data-cancel]');
    const knopf = erneut || abbruch;
    if (!knopf) return;
    knopf.disabled = true;
    try {
      const id = erneut ? erneut.dataset.retry : abbruch.dataset.cancel;
      await post(`/api/jobs/${id}/${erneut ? 'retry' : 'cancel'}`);
      ok(erneut ? 'Job neu eingeplant' : 'Job abgebrochen');
      await lade();
    } catch (exc) { fail(exc.message); knopf.disabled = false; }
  });

  return () => abState();
}
