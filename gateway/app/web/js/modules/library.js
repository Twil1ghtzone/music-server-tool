// Bibliothek: eigener Index über die Dateien auf der Platte.

import { get, post } from '../core/api.js';
import { $, esc, bytes, duration, num, tile, empty, failure, skeleton } from '../core/dom.js';
import { ok, fail } from '../core/toast.js';

export const meta = { id: 'library', titel: 'Bibliothek' };

export async function mount(wurzel) {
  wurzel.innerHTML = `
    <div class="page-head">
      <div>
        <h2>Bibliothek</h2>
        <p class="lede">Eigener Index über <code>/music</code>. Rein lesend — verändert keine Datei.</p>
      </div>
      <div class="page-actions">
        <button type="button" class="btn" data-do="scan">Neu indexieren</button>
        <button type="button" class="btn" data-do="fp">Fingerprints berechnen</button>
      </div>
    </div>
    <div class="tiles" id="lib-tiles">${skeleton(1)}</div>
    <div class="split">
      <div class="card card-flush"><h3>Formate</h3><div id="lib-formats" class="list"></div></div>
      <div class="card card-flush"><h3>Metadaten-Mängel</h3><div id="lib-issues" class="list"></div></div>
    </div>`;

  async function lade() {
    try {
      const [stats, issues] = await Promise.all([
        get('/api/library/stats'),
        get('/api/library/issues'),
      ]);

      $('#lib-tiles').innerHTML = [
        tile('Dateien', num(stats.files), bytes(stats.bytes)),
        tile('Spielzeit', duration(stats.seconds)),
        tile('Noch nicht analysiert', num(stats.unanalysed), '', stats.unanalysed ? 'warn' : 'ok'),
        tile('Fingerprints', num(stats.fingerprinted)),
        tile('Ohne Cover', num(stats.without_cover), '', stats.without_cover ? 'warn' : 'ok'),
        tile('Fehlend', num(stats.missing), 'Datei weg, Index behalten'),
      ].join('');

      $('#lib-formats').innerHTML = (stats.formats || []).length
        ? stats.formats.map((f) => `
            <div class="item">
              <div class="item-main"><div class="item-title mono">${esc(f.ext || '?')}</div></div>
              <div class="item-side">
                <span class="faint num">${num(f.n)}</span>
                <span class="pill">${bytes(f.bytes)}</span>
              </div>
            </div>`).join('')
        : empty('Noch nichts indexiert.', 'Starte oben „Neu indexieren“.');

      $('#lib-issues').innerHTML = (issues.by_issue || []).length
        ? issues.by_issue.map((i) => `
            <div class="item">
              <div class="item-main"><div class="item-title">${esc(i.issue)}</div></div>
              <div class="item-side"><span class="pill pill-warn num">${num(i.count)}</span></div>
            </div>`).join('')
        : empty('Keine Mängel gefunden.', 'Oder der Index ist noch leer.');
    } catch (exc) {
      $('#lib-tiles').innerHTML = '';
      $('#lib-formats').innerHTML = failure('Bibliothek nicht abrufbar.', exc.message);
    }
  }
  await lade();

  wurzel.addEventListener('click', async (e) => {
    const knopf = e.target.closest('[data-do]');
    if (!knopf) return;
    knopf.disabled = true;
    try {
      if (knopf.dataset.do === 'scan') { await post('/api/library/scan'); ok('Index-Lauf eingeplant'); }
      else { await post('/api/library/fingerprint'); ok('Fingerprint-Lauf eingeplant'); }
    } catch (exc) { fail(exc.message); } finally { knopf.disabled = false; }
  });
}
