// Routing über den Hash-Teil der Adresse.
//
// Warum Hash und nicht History-API: der Gateway liefert die Oberfläche als
// statische Dateien aus. Echte Pfade müssten serverseitig alle auf
// index.html zeigen — dieselbe Route wie der Subsonic-Proxy. Der Hash hält
// beide Welten sauber getrennt.
//
// Der entscheidende Teil ist `abbauen`: jedes Modul gibt beim Einhängen eine
// Aufräumfunktion zurück, die beim Wechsel läuft. Ohne das sammeln sich
// Zeitgeber und Ereignis-Zuhörer über die Sitzung an.

import { findeModul, darfSehen } from './registry.js';
import { failure } from './dom.js';

const geladen = new Map();      // id -> Modul-Namensraum
let abbauen = null;             // Aufräumfunktion des aktiven Moduls
let aktiveId = null;
let ctx = null;
let beiWechsel = () => {};

export function init(kontext, onWechsel) {
  ctx = kontext;
  beiWechsel = onWechsel || (() => {});
  window.addEventListener('hashchange', () => zeichne());
}

/** "#/album/302127" -> { id: "album", param: "302127" } */
export function aktuelleRoute() {
  const roh = (location.hash || '').replace(/^#\/?/, '');
  const [id, param] = roh.split('/');
  return { id: id || 'overview', param: param ? decodeURIComponent(param) : null };
}

export function gehe(id, param) {
  const ziel = param ? `#/${id}/${encodeURIComponent(param)}` : `#/${id}`;
  if (location.hash === ziel) zeichne();      // erneut zeichnen bei gleicher Route
  else location.hash = ziel;
}

export async function zeichne() {
  const { id, param } = aktuelleRoute();
  const modul = findeModul(id);
  const wurzel = document.getElementById('view');

  if (!darfSehen(modul, ctx.istAdmin())) {
    // Unbekannt oder nicht erlaubt: zurück auf die Übersicht, statt eine
    // leere Seite zu zeigen.
    if (id !== 'overview') return gehe('overview');
    return;
  }

  // Erst das alte Modul sauber abbauen, dann das neue einhängen.
  if (abbauen) {
    try { abbauen(); } catch (exc) { console.error('Aufräumen fehlgeschlagen:', exc); }
    abbauen = null;
  }
  aktiveId = id;
  beiWechsel(id, modul);
  wurzel.innerHTML = '';
  wurzel.scrollTop = 0;

  try {
    if (!geladen.has(id)) geladen.set(id, await import(`../modules/${id}.js`));
    const mod = geladen.get(id);
    const ergebnis = await mod.mount(wurzel, { ...ctx, param });
    // Nur übernehmen, wenn inzwischen nicht schon weitergeklickt wurde.
    if (aktiveId === id) abbauen = typeof ergebnis === 'function' ? ergebnis : null;
  } catch (exc) {
    console.error(`Modul "${id}" konnte nicht geladen werden:`, exc);
    wurzel.innerHTML = failure(`Die Seite „${modul.titel}“ konnte nicht geladen werden.`,
                               exc.message);
  }
}

export const aktivesModul = () => aktiveId;
