// Gemeinsame Klickbehandlung der Katalogseiten: Hörprobe und Einzeldownload.
//
// Der Unterstrich im Namen sagt: kein Modul im Sinne der Registry, sondern
// ein Baustein, den die Katalogseiten teilen. Ohne ihn stünde dieselbe
// Behandlung dreimal in album.js, artist.js und playlist.js.

import { post } from '../core/api.js';
import { $$, icon } from '../core/dom.js';
import * as player from '../core/player.js';
import { ok, fail } from '../core/toast.js';

/** Hängt die Behandlung an und gibt die Aufräumfunktion zurück. */
export function bindeKatalogKlicks(wurzel) {
  async function beiKlick(event) {
    const probe = event.target.closest('[data-preview]');
    const laden = event.target.closest('[data-download]');

    if (probe) {
      const zeile = probe.closest('[data-track]');
      player.play({
        id: probe.dataset.preview,
        title: zeile?.querySelector('.track-title')?.textContent?.trim() || 'Titel',
        artist: zeile?.querySelector('.item-sub')?.textContent?.trim() || '',
      });
      return;
    }

    if (!laden) return;
    laden.disabled = true;
    try {
      await post('/api/download', { provider_id: laden.dataset.download });
      laden.outerHTML = '<span class="pill pill-busy">eingeplant</span>';
      ok('Download eingeplant');
    } catch (exc) {
      fail(exc.message);
      laden.disabled = false;
    }
  }

  wurzel.addEventListener('click', beiKlick);

  // Beim Wechsel des laufenden Titels nur die Symbole tauschen - neu
  // zeichnen würde die Scrollposition in einer langen Titelliste verlieren.
  const abPlayer = player.onChange(() => {
    for (const el of $$('[data-preview]', wurzel)) {
      const spielt = String(player.laeuft()) === el.dataset.preview;
      el.innerHTML = icon(spielt ? 'pause' : 'play');
      el.closest('.track')?.classList.toggle('is-playing', spielt);
    }
  });

  return () => {
    wurzel.removeEventListener('click', beiKlick);
    abPlayer();
  };
}
