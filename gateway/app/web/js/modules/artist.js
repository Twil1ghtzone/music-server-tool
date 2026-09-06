// Interpret: Bild, beliebteste Titel, Diskografie.

import { get, post } from '../core/api.js';
import { esc, icon, num, failure, skeleton, empty } from '../core/dom.js';
import { hero, karte, trackZeile, zurueck } from '../core/catalog-ui.js';
import { werkzeugleiste } from '../core/toolbar.js';
import * as player from '../core/player.js';
import { ok, fail } from '../core/toast.js';
import { bindeKatalogKlicks } from './_katalog-klicks.js';

export const meta = { id: 'artist', titel: 'Interpret' };

export async function mount(wurzel, ctx) {
  wurzel.innerHTML = `${zurueck('#/search', 'Zurück zur Suche')}${skeleton(8)}`;

  let daten;
  try {
    daten = await get(`/api/catalog/artist/${encodeURIComponent(ctx.param)}`);
  } catch (exc) {
    wurzel.innerHTML = `${zurueck('#/search', 'Zurück zur Suche')}
      ${failure('Interpret konnte nicht geladen werden.', exc.message)}`;
    return;
  }

  const a = daten.artist;
  const top = daten.top || [];
  const alben = daten.albums || [];

  wurzel.innerHTML = `
    ${zurueck('#/search', 'Zurück zur Suche')}
    ${hero({
      md5: a.md5_image, bildart: 'artist', rund: true,
      kind: 'Interpret', titel: a.name,
      meta: `${num(a.albums)} Alben${a.fans ? ` · ${num(a.fans)} Fans` : ''}`,
      aktionen: `<button type="button" class="btn btn-go" data-release="artist">
          ${icon('download')} Alles laden
        </button>`,
    })}

    <div class="card card-flush">
      <h3>Beliebteste Titel</h3>
      <div class="tracklist">
        ${top.length
          ? top.slice(0, 15).map((t, i) => trackZeile(t, { nummer: i + 1, laeuft: player.laeuft() })).join('')
          : empty('Keine Titel gefunden.')}
      </div>
    </div>

    <div class="card">
      <h3>Diskografie</h3>
      <div id="a-leiste"></div>
      <div id="a-alben"></div>
    </div>`;

  // Sechzig Alben in einem Raster sind unübersichtlich. Filtern, sortieren
  // und die Ansicht umschalten macht daraus wieder etwas Benutzbares — und
  // die Wahl gilt auch auf der nächsten Interpretenseite.
  const leiste = werkzeugleiste({
    id: 'artist',
    standard: { q: '', sort: 'year', richtung: 'ab', ansicht: 'raster', art: 'alle' },
    suche: { platzhalter: 'Album filtern…', label: 'Diskografie filtern' },
    filter: [{ name: 'art', label: 'Nach Art filtern', werte: [
      ['alle', 'Alle Arten'], ['album', 'Nur Alben'], ['single', 'Nur Singles'], ['ep', 'Nur EPs'],
    ] }],
    sortierung: [['year', 'Jahr'], ['title', 'Titel'], ['tracks', 'Titelzahl']],
    ansicht: [['raster', '', 'grid'], ['liste', '', 'rows']],
    beiAenderung: () => zeichneAlben(),
  });
  document.getElementById('a-leiste').replaceWith(leiste.el);

  function zeichneAlben() {
    const w = leiste.werte();
    let liste = w.art === 'alle' ? alben : alben.filter((al) => al.record_type === w.art);
    const suche = (w.q || '').toLowerCase();
    if (suche) liste = liste.filter((al) => (al.title || '').toLowerCase().includes(suche));

    const richtung = w.richtung === 'ab' ? -1 : 1;
    const schluessel = { year: (al) => al.year || 0, title: (al) => al.title || '',
                         tracks: (al) => al.tracks || 0 }[w.sort];
    liste = [...liste].sort((x, y) => {
      const a1 = schluessel(x); const b1 = schluessel(y);
      return (typeof a1 === 'string' ? a1.localeCompare(b1, 'de') : a1 - b1) * richtung;
    });

    leiste.setzeZaehler(liste.length === alben.length
      ? `${alben.length}` : `${liste.length} von ${alben.length}`);
    document.getElementById('a-alben').innerHTML = liste.length
      ? `<div class="grid-cards${w.ansicht === 'liste' ? ' as-list' : ''}">${liste.map((al) => karte({
          href: `#/album/${al.id}`, md5: al.md5_image, art: 'cover',
          titel: al.title, sub: `${al.year || ''}${al.tracks ? ` · ${al.tracks} Titel` : ''}`,
        })).join('')}</div>`
      : empty(alben.length ? 'Kein Album passt zum Filter.' : 'Keine Alben gefunden.',
              alben.length ? 'Setze Suche und Art oben zurück.' : '');
  }
  zeichneAlben();

  wurzel.addEventListener('click', async (e) => {
    const alles = e.target.closest('[data-release]');
    if (!alles) return;
    // Ein ganzer Interpret können hunderte Titel sein - danach fragen.
    if (!confirm(`Wirklich alle ${num(a.albums)} Alben von „${a.name}“ laden?\n\n`
                 + 'Das kann sehr lange dauern und viel Platz belegen.')) return;
    alles.disabled = true;
    try {
      const r = await post('/api/download/release', { kind: 'artist', provider_id: ctx.param });
      ok(`„${r.label}“ eingeplant`);
      alles.textContent = 'Eingeplant';
    } catch (exc) { fail(exc.message); alles.disabled = false; }
  });

  return bindeKatalogKlicks(wurzel);
}
