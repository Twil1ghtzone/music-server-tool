// Playlist: Bild, Ersteller, Titelliste, am Stück laden.

import { get, post } from '../core/api.js';
import { esc, icon, num, duration, failure, skeleton, empty } from '../core/dom.js';
import { hero, trackZeile, zurueck } from '../core/catalog-ui.js';
import * as player from '../core/player.js';
import { ok, fail } from '../core/toast.js';
import { bindeKatalogKlicks } from './_katalog-klicks.js';

export const meta = { id: 'playlist', titel: 'Playlist' };

export async function mount(wurzel, ctx) {
  wurzel.innerHTML = `${zurueck('#/search', 'Zurück zur Suche')}${skeleton(8)}`;

  let daten;
  try {
    daten = await get(`/api/catalog/playlist/${encodeURIComponent(ctx.param)}`);
  } catch (exc) {
    wurzel.innerHTML = `${zurueck('#/search', 'Zurück zur Suche')}
      ${failure('Playlist konnte nicht geladen werden.', exc.message)}`;
    return;
  }

  const titel = daten.tracklist || [];

  wurzel.innerHTML = `
    ${zurueck('#/search', 'Zurück zur Suche')}
    ${hero({
      md5: daten.md5_image, bildart: 'playlist',
      kind: 'Playlist', titel: daten.title,
      meta: `${daten.creator ? `von ${esc(daten.creator)} · ` : ''}${
        num(daten.tracks || titel.length)} Titel${
        daten.duration ? ` · ${duration(daten.duration)}` : ''}`,
      aktionen: `<button type="button" class="btn btn-go btn-lg" data-release="playlist">
          ${icon('download')} Playlist laden
        </button>`,
    })}

    ${daten.description
      ? `<div class="card"><p class="muted small">${esc(daten.description)}</p></div>` : ''}

    <div class="card card-flush">
      <h3>Titel</h3>
      <div class="tracklist">
        ${titel.length
          ? titel.map((t, i) => trackZeile(t, { nummer: i + 1, laeuft: player.laeuft() })).join('')
          : empty('Diese Playlist enthält keine abrufbaren Titel.')}
      </div>
    </div>`;

  wurzel.addEventListener('click', async (e) => {
    const alles = e.target.closest('[data-release]');
    if (!alles) return;
    const anzahl = daten.tracks || titel.length;
    if (anzahl > 50 && !confirm(`Die Playlist hat ${num(anzahl)} Titel.\n\n`
        + 'Das kann lange dauern und viel Platz belegen. Trotzdem laden?')) return;
    alles.disabled = true;
    try {
      const r = await post('/api/download/release', { kind: 'playlist', provider_id: ctx.param });
      ok(`„${r.label}“ eingeplant (${r.tracks || anzahl} Titel)`);
      alles.textContent = 'Eingeplant';
    } catch (exc) { fail(exc.message); alles.disabled = false; }
  });

  return bindeKatalogKlicks(wurzel);
}
