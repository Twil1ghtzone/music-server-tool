// Protokoll: alle Ereignisse, filterbar, optional mitlaufend.

import { get, q } from '../core/api.js';
import { $, esc, icon, empty, failure, skeleton, stufePill, relativeTime } from '../core/dom.js';
import * as bus from '../core/bus.js';
import { werkzeugleiste } from '../core/toolbar.js';
import { fail, ok } from '../core/toast.js';

export const meta = { id: 'logs', titel: 'Protokoll' };

export async function mount(wurzel) {
  let eintraege = [];

  wurzel.innerHTML = `
    <div class="page-head">
      <div>
        <h2>Protokoll</h2>
        <p class="lede">Jedes Ereignis des Gateways. Auch unerwartete Fehler landen hier,
          nicht nur im Container-Log — wenn etwas nicht geht, steht der Grund
          fast immer auf dieser Seite.</p>
      </div>
      <div class="page-actions" id="l-aktionen"></div>
    </div>

    <div id="l-leiste"></div>
    <div class="card card-flush"><div id="l-list" class="list" aria-live="polite">${skeleton(8)}</div></div>`;

  // --- Kopieren ------------------------------------------------------------
  // Wer hier etwas findet, will es meistens irgendwo hinschicken. Ohne den
  // Knopf markiert man 300 Zeilen mit der Maus.
  const kopieren = document.createElement('button');
  kopieren.type = 'button';
  kopieren.className = 'btn';
  kopieren.innerHTML = `${icon('logs')} Sichtbares kopieren`;
  kopieren.addEventListener('click', async () => {
    const text = eintraege
      .map((e) => `${(e.ts || '').replace('T', ' ').slice(0, 19)}  ${e.level.toUpperCase()}  `
        + `[${e.category}] ${e.message}`)
      .join('\n');
    try {
      await navigator.clipboard.writeText(text);
      ok(`${eintraege.length} Zeile(n) in der Zwischenablage`);
    } catch { fail('Der Browser hat den Zugriff auf die Zwischenablage abgelehnt.'); }
  });
  $('#l-aktionen').append(kopieren);

  // --- Werkzeugleiste ------------------------------------------------------
  const leiste = werkzeugleiste({
    id: 'logs',
    standard: {
      q: '', level: 'all', category: 'all', grenze: 300,
      mitlaufen: true, zeitRelativ: false, daten: true,
    },
    suche: { platzhalter: 'Volltext im Protokoll…', label: 'Protokoll durchsuchen' },
    filter: [
      { name: 'level', label: 'Nach Stufe filtern', werte: [
        ['all', 'Alle Stufen'], ['error', 'Nur Fehler'],
        ['warn', 'Nur Auffälliges'], ['info', 'Nur Info'],
      ] },
      { name: 'category', label: 'Nach Bereich filtern', werte: [['all', 'Alle Bereiche']] },
    ],
    zusatz: [
      { name: 'mitlaufen', art: 'schalter', label: 'Mitlaufen',
        hinweis: 'Neue Einträge oben einfügen, während sie entstehen' },
      { name: 'zeitRelativ', art: 'schalter', label: 'Zeit relativ zeigen',
        hinweis: '„vor 3 Minuten“ statt Zeitstempel' },
      { name: 'daten', art: 'schalter', label: 'Zusatzdaten zeigen',
        hinweis: 'Der technische Anhang eines Eintrags' },
      { name: 'grenze', art: 'auswahl', zahl: true, label: 'Wie viele laden',
        werte: [[100, '100'], [300, '300'], [1000, '1000']] },
    ],
    beiAenderung: (w, geaendert) => {
      // Alles außer den reinen Anzeigeschaltern kommt vom Server.
      if (['zeitRelativ', 'daten', 'mitlaufen'].includes(geaendert)) zeichne();
      else lade();
    },
  });
  $('#l-leiste').replaceWith(leiste.el);

  // Die Bereichsliste kennt erst der Server. Das Auswahlfeld steht schon —
  // hier werden nur seine Einträge nachgereicht.
  const bereichFeld = leiste.el.querySelector('select[aria-label="Nach Bereich filtern"]');

  function zeile(e) {
    const w = leiste.werte();
    const zeit = w.zeitRelativ
      ? relativeTime(e.ts)
      : (e.ts || '').replace('T', ' ').slice(0, 19);
    return `<div class="item">
      <div class="item-main">
        <div class="item-title">${esc(e.message)}</div>
        <div class="item-sub"><time class="mono">${esc(zeit)}</time>${
          w.daten && e.data && e.data !== 'null'
            ? ` · <span class="mono break">${esc(e.data)}</span>` : ''}</div>
      </div>
      <div class="item-side">
        <span class="pill">${esc(e.category)}</span>
        ${stufePill(e.level)}
      </div>
    </div>`;
  }

  function zeichne() {
    const el = $('#l-list');
    if (!el) return;
    leiste.setzeZaehler(`${eintraege.length}`);
    el.innerHTML = eintraege.length
      ? eintraege.map(zeile).join('')
      : empty('Keine Einträge für diesen Filter.',
              'Stelle Stufe und Bereich in der Leiste oben auf „Alle“.');
  }

  async function lade() {
    const w = leiste.werte();
    const el = $('#l-list');
    if (!el) return;
    el.innerHTML = skeleton(8);
    try {
      const d = await get(`/api/logs${q({
        level: w.level, category: w.category, q: w.q, limit: w.grenze,
      })}`);

      // Bereichsliste nachziehen, ohne die Auswahl zu verlieren.
      if (bereichFeld) {
        const gewaehlt = bereichFeld.value;
        bereichFeld.innerHTML = '<option value="all">Alle Bereiche</option>'
          + (d.categories || []).map((c) => `<option value="${esc(c)}">${esc(c)}</option>`).join('');
        bereichFeld.value = gewaehlt;
      }

      eintraege = d.entries || [];
      zeichne();
    } catch (exc) {
      if ($('#l-list')) $('#l-list').innerHTML = failure('Protokoll nicht abrufbar.', exc.message);
    }
  }
  await lade();

  const abLog = bus.on('log', (e) => {
    if (!leiste.werte().mitlaufen) return;
    eintraege.unshift(e);
    // Der Puffer wächst sonst über die Sitzung ins Unbegrenzte.
    if (eintraege.length > 1000) eintraege.length = 1000;
    const el = $('#l-list');
    if (!el) return;
    el.insertAdjacentHTML('afterbegin', zeile(e));
    while (el.children.length > 1000) el.lastElementChild.remove();
    leiste.setzeZaehler(`${eintraege.length}`);
  });

  return () => abLog();
}
