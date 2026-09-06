// Klappmenü für Einstellungen — bedienbar mit Maus und Tastatur.
//
// Es gibt genau eines gleichzeitig. Ein zweites öffnen schließt das erste;
// Escape, ein Klick daneben oder der Verlust des Fokus schließen es auch.
// Ohne diese drei Wege bleibt so ein Ding irgendwann offen stehen und
// verdeckt, was darunter liegt.

import { icon } from './dom.js';

let offen = null;   // { wrap, knopf, panel, schliessen }

function schliesseOffenes() {
  if (offen) offen.schliessen();
}

// Einmal global, nicht je Menü: sonst hängen bei zehn Modulwechseln zehn
// Zuhörer am Dokument.
document.addEventListener('keydown', (e) => {
  if (e.key === 'Escape' && offen) {
    const knopf = offen.knopf;
    schliesseOffenes();
    knopf.focus();
  }
});
document.addEventListener('pointerdown', (e) => {
  if (offen && !offen.wrap.contains(e.target)) schliesseOffenes();
});
document.addEventListener('focusin', (e) => {
  if (offen && !offen.wrap.contains(e.target)) schliesseOffenes();
});

/**
 * Baut einen Knopf mit angehängtem Klappfeld.
 *
 * @param {object} o
 * @param {string} o.label      Barrierefreier Name des Knopfs
 * @param {string} [o.text]     Sichtbare Beschriftung; ohne wird es ein Symbolknopf
 * @param {string} [o.symbol]   Name aus icon()
 * @param {string|Node} o.inhalt  Inhalt des Klappfelds
 * @param {boolean} [o.links]   Feld linksbündig statt rechtsbündig ausrichten
 * @returns {HTMLElement} das Hüllelement, direkt einsetzbar
 */
export function klappmenue({ label, text = '', symbol = 'settings', inhalt, links = false }) {
  const wrap = document.createElement('div');
  wrap.className = 'menu-wrap';

  const knopf = document.createElement('button');
  knopf.type = 'button';
  knopf.className = `btn btn-sm${text ? '' : ' btn-icon'}`;
  knopf.setAttribute('aria-expanded', 'false');
  knopf.setAttribute('aria-haspopup', 'true');
  knopf.setAttribute('aria-label', label);
  knopf.title = label;
  knopf.innerHTML = symbol ? icon(symbol) : '';
  if (text) knopf.insertAdjacentHTML('beforeend', `<span>${text}</span>`);

  const panel = document.createElement('div');
  panel.className = `menu${links ? ' menu-left' : ''}`;
  panel.hidden = true;
  if (typeof inhalt === 'string') panel.innerHTML = inhalt;
  else if (inhalt) panel.append(inhalt);

  function schliessen() {
    panel.hidden = true;
    knopf.setAttribute('aria-expanded', 'false');
    if (offen && offen.panel === panel) offen = null;
  }

  function oeffnen() {
    schliesseOffenes();
    panel.hidden = false;
    knopf.setAttribute('aria-expanded', 'true');
    offen = { wrap, knopf, panel, schliessen };
    // Direkt zur ersten Einstellung springen, damit die Tastatur weiterkommt.
    panel.querySelector('input, select, button, a')?.focus();
  }

  knopf.addEventListener('click', () => (panel.hidden ? oeffnen() : schliessen()));

  wrap.append(knopf, panel);
  return wrap;
}

/** Zeile im Klappfeld: Beschriftung links, Bedienelement rechts. */
export function einstellung(beschriftung, hinweis, element, alsLabel = true) {
  const zeile = document.createElement(alsLabel ? 'label' : 'div');
  zeile.className = 'setting';
  const text = document.createElement('span');
  text.className = 'setting-label';
  text.innerHTML = `<span>${beschriftung}</span>${
    hinweis ? `<span class="setting-hint">${hinweis}</span>` : ''}`;
  zeile.append(text, element);
  return zeile;
}

/** Auswahlfeld aus [wert, beschriftung]-Paaren. */
export function auswahl(werte, aktuell, beiAenderung) {
  const el = document.createElement('select');
  el.innerHTML = werte
    .map(([w, b]) => `<option value="${w}"${w === aktuell ? ' selected' : ''}>${b}</option>`)
    .join('');
  el.addEventListener('change', () => beiAenderung(el.value));
  return el;
}

/** Schiebeschalter. */
export function schalter(an, beiAenderung) {
  const el = document.createElement('input');
  el.type = 'checkbox';
  el.className = 'switch';
  el.checked = Boolean(an);
  el.addEventListener('change', () => beiAenderung(el.checked));
  return el;
}

/**
 * Segmentwahl: zwei bis vier Möglichkeiten nebeneinander, genau eine gilt.
 * @param {Array} werte  [wert, beschriftung, symbol?]
 */
export function segmente(werte, aktuell, beiAenderung, { label = '' } = {}) {
  const gruppe = document.createElement('div');
  gruppe.className = 'segmented';
  gruppe.setAttribute('role', 'group');
  if (label) gruppe.setAttribute('aria-label', label);

  gruppe.innerHTML = werte.map(([w, b, s]) => `
    <button type="button" data-wert="${w}" aria-pressed="${w === aktuell}"
            ${s && !b ? `aria-label="${b || w}" title="${b || w}"` : ''}>
      ${s ? icon(s) : ''}${b ? `<span>${b}</span>` : ''}
    </button>`).join('');

  gruppe.addEventListener('click', (e) => {
    const knopf = e.target.closest('button[data-wert]');
    if (!knopf || knopf.getAttribute('aria-pressed') === 'true') return;
    for (const b of gruppe.querySelectorAll('button')) {
      b.setAttribute('aria-pressed', String(b === knopf));
    }
    beiAenderung(knopf.dataset.wert);
  });
  return gruppe;
}
