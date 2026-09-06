// Protokoll: alle Ereignisse, filterbar, optional mitlaufend.

import { get, q } from '../core/api.js';
import { $, esc, empty, failure, skeleton, stufePill } from '../core/dom.js';
import * as bus from '../core/bus.js';
import { fail } from '../core/toast.js';

export const meta = { id: 'logs', titel: 'Protokoll' };


export async function mount(wurzel) {
  wurzel.innerHTML = `
    <div class="page-head">
      <div>
        <h2>Protokoll</h2>
        <p class="lede">Auch unerwartete Fehler landen hier, nicht nur im Container-Log.</p>
      </div>
      <div class="page-actions">
        <label class="inline"><input type="checkbox" id="l-follow" checked> mitlaufen</label>
        <button type="button" class="btn" id="l-reload">Neu laden</button>
      </div>
    </div>

    <form class="card row" id="l-filter">
      <label class="sr-only" for="l-level">Stufe</label>
      <select id="l-level" name="level">
        <option value="all">Alle Stufen</option>
        <option value="warn">Nur Auffälliges</option>
        <option value="error">Nur Fehler</option>
        <option value="info">Nur Info</option>
      </select>
      <label class="sr-only" for="l-cat">Bereich</label>
      <select id="l-cat" name="category"><option value="all">Alle Bereiche</option></select>
      <label class="sr-only" for="l-q">Volltextsuche</label>
      <input class="grow" id="l-q" name="q" type="search" spellcheck="false" placeholder="Volltextsuche…">
      <button class="btn btn-primary" type="submit">Filtern</button>
    </form>

    <div class="card card-flush"><div id="l-list" class="list" aria-live="polite"></div></div>`;

  const zeile = (e) => {
    const zeit = (e.ts || '').replace('T', ' ').slice(0, 19);
    return `<div class="item">
      <div class="item-main">
        <div class="item-title">${esc(e.message)}</div>
        <div class="item-sub"><time class="mono">${esc(zeit)}</time>${
          e.data && e.data !== 'null' ? ` · <span class="mono break">${esc(e.data)}</span>` : ''}</div>
      </div>
      <div class="item-side">
        <span class="pill">${esc(e.category)}</span>
        ${stufePill(e.level)}
      </div>
    </div>`;
  };

  async function lade() {
    const daten = new FormData($('#l-filter'));
    const el = $('#l-list');
    el.innerHTML = skeleton(8);
    try {
      const d = await get(`/api/logs${q({
        level: daten.get('level') || 'all',
        category: daten.get('category') || 'all',
        q: daten.get('q') || '',
        limit: 500,
      })}`);

      // Bereichsliste nachziehen, ohne die Auswahl zu verlieren.
      const auswahl = $('#l-cat');
      const gewaehlt = auswahl.value;
      auswahl.innerHTML = '<option value="all">Alle Bereiche</option>'
        + (d.categories || []).map((c) => `<option value="${esc(c)}">${esc(c)}</option>`).join('');
      auswahl.value = gewaehlt;

      el.innerHTML = (d.entries || []).length
        ? d.entries.map(zeile).join('')
        : empty('Keine Einträge für diesen Filter.');
    } catch (exc) {
      el.innerHTML = failure('Protokoll nicht abrufbar.', exc.message);
    }
  }
  await lade();

  $('#l-filter').addEventListener('submit', (e) => { e.preventDefault(); lade(); });
  $('#l-reload').addEventListener('click', () => lade().catch((exc) => fail(exc.message)));

  const abLog = bus.on('log', (e) => {
    if (!$('#l-follow')?.checked) return;
    const el = $('#l-list');
    el.insertAdjacentHTML('afterbegin', zeile(e));
    while (el.children.length > 600) el.lastElementChild.remove();
  });

  return () => abLog();
}
