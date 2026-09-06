// Die Werkzeugleiste über einer Liste: suchen, sortieren, Ansicht wählen,
// Einstellungen dieses Moduls.
//
// Warum das hier liegt und nicht in jedem Modul: elf Module, die sich ihren
// Kopfbereich selbst zusammenbauen, sind elf Gelegenheiten, es anders zu
// machen. Ein Modul beschreibt hier nur noch, welche Felder es braucht;
// Aussehen, Tastaturbedienung und das Merken der Wahl kommen von hier.
//
// Jede Einstellung landet über prefs.modul(id) im Speicher — die Sortierung
// von gestern gilt morgen noch.

import { icon } from './dom.js';
import * as prefs from './prefs.js';
import { klappmenue, einstellung, auswahl, schalter, segmente } from './menu.js';

/**
 * @param {object} o
 * @param {string} o.id                  Modulkennung (auch der Speicherschlüssel)
 * @param {object} [o.standard]          Ausgangswerte der Einstellungen
 * @param {object} [o.suche]             { platzhalter, label } — Suchfeld
 * @param {Array}  [o.sortierung]        [wert, beschriftung] für "Sortieren nach"
 * @param {Array}  [o.ansicht]           [wert, beschriftung, symbol] für Raster/Liste
 * @param {Array}  [o.filter]            [{ name, label, werte:[[w,b]] }]
 * @param {Array}  [o.zusatz]            weitere Einstellungen im Klappfeld
 * @param {Array}  [o.aktionen]          fertige Elemente, rechts vor dem Menü
 * @param {(werte:object, geaendert:string) => void} o.beiAenderung
 * @returns {{ el: HTMLElement, werte: () => object, setzeZaehler: (t:string) => void }}
 */
export function werkzeugleiste(o) {
  const p = prefs.modul(o.id, o.standard || {});
  const leiste = document.createElement('div');
  leiste.className = 'toolbar';

  const melden = (name) => o.beiAenderung?.(p.alle(), name);

  // --- Suche -----------------------------------------------------------
  // Entprellt: bei jedem Tastendruck neu zu filtern lässt lange Listen
  // ruckeln, und bei serverseitiger Suche wäre es eine Anfrage je Zeichen.
  if (o.suche) {
    const feld = document.createElement('div');
    feld.className = 'field-search';
    feld.innerHTML = icon('search');
    const eingabe = document.createElement('input');
    eingabe.type = 'search';
    eingabe.placeholder = o.suche.platzhalter || 'Filtern…';
    eingabe.setAttribute('aria-label', o.suche.label || o.suche.platzhalter || 'Filtern');
    eingabe.spellcheck = false;
    eingabe.autocomplete = 'off';
    eingabe.value = p.get('q') || '';
    let zeitgeber;
    eingabe.addEventListener('input', () => {
      clearTimeout(zeitgeber);
      zeitgeber = setTimeout(() => { p.set('q', eingabe.value.trim()); melden('q'); }, 220);
    });
    feld.append(eingabe);
    leiste.append(feld);
  }

  // --- Filter ------------------------------------------------------------
  for (const f of o.filter || []) {
    const el = auswahl(f.werte, p.get(f.name), (wert) => { p.set(f.name, wert); melden(f.name); });
    el.setAttribute('aria-label', f.label);
    el.title = f.label;
    leiste.append(el);
  }

  // --- Sortierung --------------------------------------------------------
  if (o.sortierung) {
    const el = auswahl(o.sortierung, p.get('sort'), (wert) => { p.set('sort', wert); melden('sort'); });
    el.setAttribute('aria-label', 'Sortieren nach');
    el.title = 'Sortieren nach';
    leiste.append(el);

    // Richtung umkehren. Ein eigener Knopf ist schneller als zwei Einträge
    // je Kriterium im Auswahlfeld.
    const richtung = document.createElement('button');
    richtung.type = 'button';
    richtung.className = 'btn btn-sm btn-icon';
    const zeichne = () => {
      const ab = p.get('richtung') === 'ab';
      richtung.innerHTML = icon(ab ? 'sortDown' : 'sortUp');
      richtung.setAttribute('aria-label', ab ? 'Absteigend sortiert' : 'Aufsteigend sortiert');
      richtung.title = richtung.getAttribute('aria-label');
    };
    zeichne();
    richtung.addEventListener('click', () => {
      p.set('richtung', p.get('richtung') === 'ab' ? 'auf' : 'ab');
      zeichne();
      melden('richtung');
    });
    leiste.append(richtung);
  }

  // --- Ansicht -----------------------------------------------------------
  if (o.ansicht) {
    leiste.append(segmente(o.ansicht, p.get('ansicht'),
      (wert) => { p.set('ansicht', wert); melden('ansicht'); }, { label: 'Ansicht' }));
  }

  // --- Rechter Block -----------------------------------------------------
  const rechts = document.createElement('div');
  rechts.className = 'row toolbar-right';

  const zaehler = document.createElement('span');
  zaehler.className = 'faint tiny num';
  zaehler.setAttribute('aria-live', 'polite');
  rechts.append(zaehler);

  for (const el of o.aktionen || []) rechts.append(el);

  // --- Einstellungen dieses Moduls ---------------------------------------
  if (o.zusatz?.length) {
    const feld = document.createElement('div');
    feld.innerHTML = '<h4>Ansicht anpassen</h4>';

    for (const z of o.zusatz) {
      let element;
      if (z.art === 'schalter') {
        element = schalter(p.get(z.name), (an) => { p.set(z.name, an); melden(z.name); });
      } else if (z.art === 'zahl') {
        element = document.createElement('input');
        element.type = 'number';
        element.min = z.min ?? 1;
        element.max = z.max ?? 1000;
        element.step = z.step ?? 1;
        element.value = p.get(z.name);
        element.addEventListener('change', () => {
          p.set(z.name, Number(element.value)); melden(z.name);
        });
      } else {
        element = auswahl(z.werte, p.get(z.name), (wert) => {
          p.set(z.name, z.zahl ? Number(wert) : wert); melden(z.name);
        });
      }
      feld.append(einstellung(z.label, z.hinweis, element, z.art === 'schalter'));
    }

    const zurueck = document.createElement('button');
    zurueck.type = 'button';
    zurueck.className = 'btn btn-sm btn-ghost';
    zurueck.textContent = 'Auf Standard zurücksetzen';
    zurueck.addEventListener('click', () => { p.zuruecksetzen(); melden('reset'); });
    feld.append(document.createElement('hr'), zurueck);

    rechts.append(klappmenue({ label: 'Einstellungen dieser Seite', inhalt: feld }));
  }

  leiste.append(rechts);

  return {
    el: leiste,
    werte: () => p.alle(),
    /** Zeigt rechts, wie viel gerade zu sehen ist ("12 von 340"). */
    setzeZaehler: (text) => { zaehler.textContent = text || ''; },
  };
}

/**
 * Filtert und sortiert eine Liste nach den Werten der Werkzeugleiste.
 * Damit muss nicht jedes Modul dieselbe Schleife noch einmal schreiben.
 *
 * @param {Array} zeilen
 * @param {object} werte      aus werkzeugleiste().werte()
 * @param {object} o
 * @param {string[]} o.felder Felder, die die Suche durchsucht
 * @param {object} [o.sortierer] { schluessel: (zeile) => vergleichbarer Wert }
 */
export function anwenden(zeilen, werte, { felder = [], sortierer = {} } = {}) {
  let ergebnis = zeilen;

  const q = (werte.q || '').toLowerCase();
  if (q && felder.length) {
    ergebnis = ergebnis.filter((z) =>
      felder.some((f) => String(z[f] ?? '').toLowerCase().includes(q)));
  }

  const schluessel = sortierer[werte.sort];
  if (schluessel) {
    const richtung = werte.richtung === 'ab' ? -1 : 1;
    ergebnis = [...ergebnis].sort((a, b) => {
      const x = schluessel(a); const y = schluessel(b);
      if (x === y) return 0;
      if (x === null || x === undefined) return 1;
      if (y === null || y === undefined) return -1;
      return (typeof x === 'string' ? x.localeCompare(y, 'de') : x - y) * richtung;
    });
  }
  return ergebnis;
}
