// Bibliothek: eigener Index über die Dateien auf der Platte.

import { get, post } from '../core/api.js';
import { $, esc, icon, bytes, duration, num, tile, empty, failure, skeleton } from '../core/dom.js';
import { klappmenue, einstellung, auswahl, schalter } from '../core/menu.js';
import * as prefs from '../core/prefs.js';
import { ok, fail } from '../core/toast.js';

export const meta = { id: 'library', titel: 'Bibliothek' };

export async function mount(wurzel) {
  wurzel.innerHTML = `
    <div class="page-head">
      <div>
        <h2>Bibliothek</h2>
        <p class="lede">Eigener Index über <code>/music</code>. Rein lesend — verändert keine Datei.</p>
      </div>
      <div class="page-actions" id="lib-aktionen">
        <button type="button" class="btn" data-do="scan">${icon('refresh')} Neu indexieren</button>
        <button type="button" class="btn" data-do="fp">${icon('bolt')} Fingerprints berechnen</button>
      </div>
    </div>
    <div class="tiles" id="lib-tiles">${skeleton(1)}</div>
    <div class="split">
      <div class="card card-flush"><h3>Formate</h3><div id="lib-formats" class="list"></div></div>
      <div class="card card-flush"><h3>Metadaten-Mängel</h3><div id="lib-issues" class="list"></div></div>
    </div>`;

  // --- Einstellungen dieser Seite ------------------------------------------
  // Ein Index-Lauf dauert; wer ihn anstößt, will sehen, wie er vorankommt,
  // ohne von Hand nachzuladen.
  const p = prefs.modul('library', { takt: 0, formateLeer: false });
  let zeitgeber = null;

  function stelleTakt() {
    clearInterval(zeitgeber);
    const sek = p.get('takt');
    if (sek > 0) zeitgeber = setInterval(() => lade(), sek * 1000);
  }

  const feld = document.createElement('div');
  feld.innerHTML = '<h4>Ansicht anpassen</h4>';
  feld.append(einstellung('Selbst nachladen', 'Wie oft die Zahlen neu geholt werden',
    auswahl([[0, 'aus'], [10, 'alle 10 s'], [30, 'alle 30 s'], [60, 'jede Minute']],
      p.get('takt'), (w) => { p.set('takt', Number(w)); stelleTakt(); })));
  feld.append(einstellung('Formate ohne Dateien zeigen', 'Auch Endungen mit null Treffern',
    schalter(p.get('formateLeer'), (an) => { p.set('formateLeer', an); lade(); })));
  $('#lib-aktionen').append(klappmenue({ label: 'Einstellungen dieser Seite', inhalt: feld }));

  async function lade() {
    try {
      const [stats, issues] = await Promise.all([
        get('/api/library/stats'),
        get('/api/library/issues'),
      ]);

      // Zwischen Anfrage und Antwort kann die Seite gewechselt worden sein.
      if (!$('#lib-tiles')) return;

      $('#lib-tiles').innerHTML = [
        tile('Dateien', num(stats.files), bytes(stats.bytes)),
        tile('Spielzeit', duration(stats.seconds)),
        tile('Noch nicht analysiert', num(stats.unanalysed), '', stats.unanalysed ? 'warn' : 'ok'),
        tile('Fingerprints', num(stats.fingerprinted)),
        tile('Ohne Cover', num(stats.without_cover), '', stats.without_cover ? 'warn' : 'ok'),
        tile('Fehlend', num(stats.missing), 'Datei weg, Index behalten'),
      ].join('');

      const formate = (stats.formats || [])
        .filter((f) => p.get('formateLeer') || f.n > 0);
      $('#lib-formats').innerHTML = formate.length
        ? formate.map((f) => `
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
      if (!$('#lib-tiles')) return;
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

  stelleTakt();
  // Ohne das läuft der Zeitgeber nach dem Wechsel auf eine andere Seite weiter.
  return () => clearInterval(zeitgeber);
}
