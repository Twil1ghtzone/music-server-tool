// Album: Cover, Titelliste, Hörprobe, einzeln oder am Stück laden.

import { get, post } from '../core/api.js';
import { $, $$, esc, icon, duration, num, failure, skeleton } from '../core/dom.js';
import { hero, trackZeile, zurueck } from '../core/catalog-ui.js';
import * as player from '../core/player.js';
import { ok, fail } from '../core/toast.js';
import { bindeKatalogKlicks } from './_katalog-klicks.js';

export const meta = { id: 'album', titel: 'Album' };

export async function mount(wurzel, ctx) {
  wurzel.innerHTML = `${zurueck('#/search', 'Zurück zur Suche')}${skeleton(8)}`;

  let daten;
  try {
    daten = await get(`/api/catalog/album/${encodeURIComponent(ctx.param)}`);
  } catch (exc) {
    wurzel.innerHTML = `${zurueck('#/search', 'Zurück zur Suche')}
      ${failure('Album konnte nicht geladen werden.', exc.message)}`;
    return;
  }

  const titel = daten.tracklist || [];
  const gesamt = titel.reduce((s, t) => s + (t.duration || 0), 0);

  wurzel.innerHTML = `
    ${zurueck('#/search', 'Zurück zur Suche')}
    ${hero({
      md5: daten.md5_image, bildart: 'cover',
      kind: daten.record_type === 'single' ? 'Single' : 'Album',
      titel: daten.title,
      meta: `${daten.artist_id
        ? `<a href="#/artist/${esc(daten.artist_id)}">${esc(daten.artist)}</a>`
        : esc(daten.artist)}${daten.year ? ` · ${daten.year}` : ''}
        · ${num(titel.length)} Titel · ${duration(gesamt)}${
        daten.genres?.length ? ` · ${esc(daten.genres.join(', '))}` : ''}`,
      aktionen: `<button type="button" class="btn btn-go btn-lg" data-release="album">
          ${icon('download')} Album laden
        </button>`,
    })}
    <div class="card card-flush">
      <h3>Titel</h3>
      <div class="tracklist" id="a-tracks">
        ${titel.map((t, i) => trackZeile(t, { nummer: i + 1, laeuft: player.laeuft() })).join('')}
      </div>
    </div>`;

  wurzel.addEventListener('click', async (e) => {
    const alles = e.target.closest('[data-release]');
    if (!alles) return;
    alles.disabled = true;
    try {
      const r = await post('/api/download/release', { kind: 'album', provider_id: ctx.param });
      ok(`„${r.label}“ eingeplant (${r.tracks || '?'} Titel)`);
      alles.textContent = 'Eingeplant';
    } catch (exc) { fail(exc.message); alles.disabled = false; }
  });

  return bindeKatalogKlicks(wurzel);
}
