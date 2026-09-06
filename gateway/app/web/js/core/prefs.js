// Einstellungen, die sich merken.
//
// Alles hier ist reine Bequemlichkeit: Dichte, Akzentfarbe, Sortierung einer
// Liste, wie viele Zeilen sie zeigt. Nichts davon gehört auf den Server —
// es ist die Vorliebe dieses Menschen an diesem Gerät, nicht ein Zustand des
// Systems. Deshalb localStorage.
//
// Es gibt zwei Ebenen:
//   global(...)  gilt überall — Dichte, Akzent, Bewegung, Startseite
//   modul(id)    gilt für einen Menüpunkt — Sortierung, Ansicht, Filter
//
// Ein Modul fragt nie selbst localStorage ab. Es holt sich seinen Behälter
// und arbeitet damit; wo die Werte liegen, ist von hier aus änderbar.

const SCHLUESSEL = 'mst.prefs.v1';

// Der Auslieferungszustand. Jede Einstellung steht genau einmal hier —
// fehlt ein Wert im Speicher, gilt dieser.
const STANDARD = {
  global: {
    dichte: 'normal',        // kompakt | normal | luftig
    akzent: 'blau',          // blau | violett | tuerkis | bernstein
    bewegung: 'voll',        // voll | reduziert
    startseite: 'overview',
    bestaetigen: true,       // vor zerstörenden Aktionen nachfragen
    hoerprobeLautstaerke: 0.8,
    autoAktualisieren: true, // Listen bei Live-Ereignissen neu zeichnen
  },
  module: {},
};

function lade() {
  try {
    const roh = localStorage.getItem(SCHLUESSEL);
    if (!roh) return structuredClone(STANDARD);
    const gespeichert = JSON.parse(roh);
    return {
      global: { ...STANDARD.global, ...(gespeichert.global || {}) },
      module: gespeichert.module || {},
    };
  } catch {
    // Privates Fenster, volle Platte, kaputter Eintrag — kein Grund, die
    // Oberfläche nicht zu starten.
    return structuredClone(STANDARD);
  }
}

let zustand = lade();
const zuhoerer = new Set();

function sichern() {
  try {
    localStorage.setItem(SCHLUESSEL, JSON.stringify(zustand));
  } catch { /* Nicht schreibbar: die Einstellung gilt dann nur diese Sitzung. */ }
}

function melden(bereich) {
  for (const fn of zuhoerer) {
    try { fn(bereich); } catch (exc) { console.error('Einstellungs-Zuhörer:', exc); }
  }
}

/** Wird bei jeder Änderung gerufen. Gibt die Abmeldefunktion zurück. */
export function onChange(fn) {
  zuhoerer.add(fn);
  return () => zuhoerer.delete(fn);
}

// ------------------------------------------------------------------ global
export const global = () => zustand.global;
export const hole = (name) => zustand.global[name];

export function setze(name, wert) {
  if (zustand.global[name] === wert) return;
  zustand.global[name] = wert;
  sichern();
  wendeAn();
  melden('global');
}

/** Schreibt die globalen Einstellungen an das <html>-Element, wo das
 *  Stylesheet sie abgreift. Eine Zeile hier ersetzt ein zweites Theme. */
export function wendeAn() {
  const w = document.documentElement;
  w.dataset.dichte = zustand.global.dichte;
  w.dataset.akzent = zustand.global.akzent;
  w.dataset.bewegung = zustand.global.bewegung;
}

export function zuruecksetzen() {
  zustand = structuredClone(STANDARD);
  sichern();
  wendeAn();
  melden('global');
}

// ------------------------------------------------------------------- Modul
/** Einstellungen eines Menüpunkts. `standard` beschreibt, was es gibt und
 *  was gilt, solange nichts gespeichert ist. */
export function modul(id, standard = {}) {
  const gespeichert = zustand.module[id] || {};
  const werte = { ...standard, ...gespeichert };

  return {
    /** Aktueller Wert einer Einstellung. */
    get: (name) => werte[name],
    /** Alle Werte als Objekt — praktisch beim Aufbauen einer Anfrage. */
    alle: () => ({ ...werte }),
    /** Setzt einen Wert und merkt ihn. Gibt zurück, ob er sich geändert hat. */
    set(name, wert) {
      if (werte[name] === wert) return false;
      werte[name] = wert;
      zustand.module[id] = { ...(zustand.module[id] || {}), [name]: wert };
      sichern();
      melden(id);
      return true;
    },
    /** Verwirft die gemerkten Werte dieses Moduls. */
    zuruecksetzen() {
      delete zustand.module[id];
      for (const [k, v] of Object.entries(standard)) werte[k] = v;
      sichern();
      melden(id);
    },
  };
}

// Beim Laden sofort anwenden, damit die Seite nicht erst in der
// Standardansicht erscheint und dann umspringt.
wendeAn();
