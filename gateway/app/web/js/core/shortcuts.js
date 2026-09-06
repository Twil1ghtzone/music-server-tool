// Tastaturkürzel.
//
// Bewusst wenige und alle nach demselben Muster: "g" gefolgt von einem
// Buchstaben springt irgendwohin ("go"), "/" springt in das Suchfeld der
// aktuellen Seite, "?" zeigt die Liste. Wer sie nicht kennt, merkt nichts
// davon — sie stehen unter Einstellungen, und nichts hier verstellt eine
// Taste, die man beim Tippen braucht.

import * as router from './router.js';

// "g" dann Buchstabe. Die Umlaute stehen mit drin, weil "ü" für Übersicht
// die naheliegende Taste auf einer deutschen Tastatur ist.
const SPRUNG = {
  'ü': 'overview', u: 'overview',
  s: 'search',
  w: 'queue',
  j: 'jobs',
  b: 'library',
  p: 'logs',
  d: 'diagnostics',
  e: 'settings',
  k: 'account',
};

let wartetAufZiel = false;
let zeitgeber = null;

/** Tippt der Mensch gerade irgendwo hinein? Dann gehören die Tasten dorthin. */
function imFeld(ziel) {
  if (!ziel) return false;
  if (ziel.isContentEditable) return true;
  return ['INPUT', 'TEXTAREA', 'SELECT'].includes(ziel.tagName);
}

/**
 * @param {object} o
 * @param {() => void} o.hilfe        zeigt die Kürzelliste
 * @param {() => boolean} o.stopp     beendet eine laufende Hörprobe; true, wenn eine lief
 * @param {(id:string) => boolean} o.darf  darf diese Seite überhaupt geöffnet werden?
 */
export function init({ hilfe, stopp, darf }) {
  document.addEventListener('keydown', (e) => {
    if (e.ctrlKey || e.metaKey || e.altKey) return;

    // Escape beendet die Hörprobe — auch aus einem Feld heraus, weil es die
    // eine Taste ist, die überall "hör auf" heißt.
    if (e.key === 'Escape') { stopp(); return; }
    if (imFeld(e.target)) return;

    if (wartetAufZiel) {
      wartetAufZiel = false;
      clearTimeout(zeitgeber);
      const ziel = SPRUNG[e.key.toLowerCase()];
      if (ziel && darf(ziel)) { e.preventDefault(); router.gehe(ziel); }
      return;
    }

    if (e.key === 'g') {
      // Eine Sekunde auf den zweiten Anschlag warten, dann vergessen —
      // sonst schluckt ein vergessenes "g" den nächsten Tastendruck.
      wartetAufZiel = true;
      clearTimeout(zeitgeber);
      zeitgeber = setTimeout(() => { wartetAufZiel = false; }, 1200);
      return;
    }

    if (e.key === '/') {
      const feld = document.querySelector('#view input[type="search"], #view input[type="text"]');
      if (feld) { e.preventDefault(); feld.focus(); feld.select?.(); }
      return;
    }

    if (e.key === '?') { e.preventDefault(); hilfe(); return; }

    if (e.key === ' ' && stopp()) e.preventDefault();
  });
}
