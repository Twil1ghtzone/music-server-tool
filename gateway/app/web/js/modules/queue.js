// Warteschlange: was on demand angefordert wurde und wie weit es ist.

import { get, post, del } from '../core/api.js';
import { $, esc, icon, empty, failure, skeleton, relativeTime, zustandPill } from '../core/dom.js';
import * as bus from '../core/bus.js';
import * as prefs from '../core/prefs.js';
import { werkzeugleiste, anwenden } from '../core/toolbar.js';
import { ok, fail } from '../core/toast.js';

export const meta = { id: 'queue', titel: 'Warteschlange' };

export async function mount(wurzel, ctx) {
  const admin = ctx.istAdmin();
  let alle = [];

  wurzel.innerHTML = `
    <div class="page-head">
      <div>
        <h2>Warteschlange</h2>
        <p class="lede">Titel, die über die Suche oder einen Musik-Client angefordert wurden.
          Fertige verschwinden nicht — sie bleiben als Nachweis stehen, dass sie
          angekommen sind.</p>
      </div>
      ${admin ? `<div class="page-actions">
        <button type="button" class="btn" data-do="clear">Fehlgeschlagene entfernen</button>
        <button type="button" class="btn" data-do="staging">${icon('download')} Staging importieren</button>
      </div>` : ''}
    </div>
    <div id="q-leiste"></div>
    <div class="card card-flush"><div id="q-list" class="list">${skeleton(4)}</div></div>`;

  // --- Werkzeugleiste ------------------------------------------------------
  // Filter, Sortierung und Ansicht merkt sich die Seite. Wer hier meistens
  // nach Fehlern sucht, findet sie beim nächsten Mal schon gefiltert vor.
  const leiste = werkzeugleiste({
    id: 'queue',
    standard: { q: '', sort: 'updated_at', richtung: 'ab', zustand: 'alle', kompakt: false },
    suche: { platzhalter: 'Titel, Interpret oder Album filtern…', label: 'Warteschlange filtern' },
    filter: [{
      name: 'zustand',
      label: 'Nach Zustand filtern',
      werte: [
        ['alle', 'Alle Zustände'],
        ['offen', 'Nur offene'],
        ['failed', 'Nur fehlgeschlagene'],
        ['ready', 'Nur fertige'],
      ],
    }],
    sortierung: [
      ['updated_at', 'Zuletzt geändert'],
      ['title', 'Titel'],
      ['artist', 'Interpret'],
      ['play_requests', 'Abrufe'],
    ],
    zusatz: [
      { name: 'kompakt', art: 'schalter', label: 'Nur eine Zeile je Titel',
        hinweis: 'Blendet Album, Fehlergrund und Zeitpunkt aus' },
    ],
    beiAenderung: () => zeichne(alle),
  });
  $('#q-leiste').replaceWith(leiste.el);

  // --- Zeichnen ------------------------------------------------------------
  function zeichne(items) {
    alle = items || alle;
    const el = $('#q-list');
    if (!el) return;

    const w = leiste.werte();
    let zeilen = alle;

    if (w.zustand === 'offen') zeilen = zeilen.filter((i) => !i.navidrome_id && i.state !== 'ready');
    else if (w.zustand !== 'alle') zeilen = zeilen.filter((i) => i.state === w.zustand);

    zeilen = anwenden(zeilen, w, {
      felder: ['title', 'artist', 'album'],
      sortierer: {
        updated_at: (i) => i.updated_at || '',
        title: (i) => i.title || '',
        artist: (i) => i.artist || '',
        play_requests: (i) => i.play_requests || 0,
      },
    });

    leiste.setzeZaehler(zeilen.length === alle.length
      ? `${alle.length}` : `${zeilen.length} von ${alle.length}`);

    if (!alle.length) {
      el.innerHTML = empty('Nichts in der Warteschlange.',
        'Suche einen Titel, den es lokal noch nicht gibt, und fordere ihn an.');
      return;
    }
    if (!zeilen.length) {
      el.innerHTML = empty('Kein Eintrag passt zum Filter.',
        'Setze den Filter in der Leiste oben zurück, um wieder alles zu sehen.');
      return;
    }

    el.innerHTML = zeilen.map((i) => {
      const fertig = Boolean(i.navidrome_id) || i.state === 'ready';
      return `
      <div class="item">
        <div class="item-main">
          <div class="item-title">${esc(i.artist)} — ${esc(i.title)}</div>
          ${w.kompakt ? '' : `<div class="item-sub">${esc(i.album || '')}${
            i.error ? ` · ${esc(i.error)}` : ''} · ${esc(relativeTime(i.updated_at))}</div>`}
        </div>
        <div class="item-side">
          ${i.play_requests ? `<span class="faint tiny num" title="So oft abgerufen">${i.play_requests}×</span>` : ''}
          ${zustandPill(i.state)}
          ${!fertig && i.provider_id
            ? `<button type="button" class="btn btn-sm" data-retry="${esc(i.provider_id)}">Erneut</button>` : ''}
          ${admin && !fertig
            ? `<button type="button" class="btn btn-sm btn-ghost btn-icon" data-forget="${esc(i.id)}"
                 aria-label="„${esc(i.title)}“ aus der Liste entfernen" title="Aus der Liste entfernen"
                 >${icon('close')}</button>` : ''}
        </div>
      </div>`;
    }).join('');
  }

  async function lade() {
    try { zeichne((await get('/api/queue?limit=200')).items); }
    catch (exc) {
      if ($('#q-list')) $('#q-list').innerHTML = failure('Warteschlange nicht abrufbar.', exc.message);
    }
  }
  await lade();

  // Die Momentaufnahme aus dem Ereignisstrom hält die Liste aktuell, ohne
  // dass hier ein eigener Zeitgeber laufen muss. Wer das nicht will, schaltet
  // es unter Einstellungen ab — sonst springt die Liste unter der Hand weg,
  // während man eine Zeile liest.
  const abState = bus.on('state', (s) => {
    if (prefs.hole('autoAktualisieren')) zeichne(s.queue);
  });

  wurzel.addEventListener('click', async (event) => {
    const erneut = event.target.closest('[data-retry]');
    const weg = event.target.closest('[data-forget]');
    const tun = event.target.closest('[data-do]');
    const knopf = erneut || weg || tun;
    if (!knopf) return;
    knopf.disabled = true;
    try {
      if (erneut) {
        await post('/api/download', { provider_id: erneut.dataset.retry });
        ok('Erneut angefordert');
        await lade();
      } else if (weg) {
        await del(`/api/queue/${encodeURIComponent(weg.dataset.forget)}`);
        weg.closest('.item')?.remove();
      } else if (tun.dataset.do === 'clear') {
        const r = await post('/api/queue/clear-failed');
        ok(`${r.removed} Eintrag/Einträge entfernt`);
        await lade();
      } else {
        await post('/api/import-staging');
        ok('Import eingeplant');
      }
    } catch (exc) { fail(exc.message); } finally { knopf.disabled = false; }
  });

  return () => abState();
}
