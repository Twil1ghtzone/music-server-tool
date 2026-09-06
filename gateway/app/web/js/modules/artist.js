// Interpret: Bild, beliebteste Titel, Diskografie.

import { get, post } from '../core/api.js';
import { esc, icon, num, failure, skeleton, empty } from '../core/dom.js';
import { hero, karte, trackZeile, zurueck } from '../core/catalog-ui.js';
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
      ${alben.length
        ? `<div class="grid-cards">${alben.map((al) => karte({
            href: `#/album/${al.id}`, md5: al.md5_image, art: 'cover',
            titel: al.title, sub: `${al.year || ''}${al.tracks ? ` · ${al.tracks} Titel` : ''}`,
          })).join('')}</div>`
        : empty('Keine Alben gefunden.')}
    </div>`;

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
