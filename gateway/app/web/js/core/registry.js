// Was es an Modulen gibt. Eine neue Seite hinzufügen heißt: eine Datei
// unter js/modules/ anlegen und hier eine Zeile ergänzen — sonst nichts.
//
// `nav: false` heißt: erreichbar über eine Route, aber kein eigener
// Menüpunkt (die Detailseiten von Interpret, Album und Playlist).
//
// `gruppe` ordnet die Menüpunkte in drei Blöcke. Elf Einträge in einer
// ununterbrochenen Liste liest niemand — man sucht jedes Mal neu. Mit
// Zwischenüberschriften weiß man nach einmal Hinsehen, wo etwas steht.

export const GRUPPEN = [
  ['musik',  'Musik'],
  ['pflege', 'Bibliothek'],
  ['system', 'System'],
];

export const MODULE = [
  { id: 'overview',    titel: 'Übersicht',        symbol: 'overview',    gruppe: 'musik',  admin: false },
  { id: 'search',      titel: 'Suche & Download', symbol: 'search',      gruppe: 'musik',  admin: false },
  { id: 'queue',       titel: 'Warteschlange',    symbol: 'queue',       gruppe: 'musik',  admin: false },
  { id: 'jobs',        titel: 'Jobs',             symbol: 'jobs',        gruppe: 'musik',  admin: false },

  { id: 'mediathek',   titel: 'Mediathek',        symbol: 'library',     gruppe: 'pflege', admin: false },
  { id: 'library',     titel: 'Dateien',          symbol: 'diagnostics', gruppe: 'pflege', admin: true },
  { id: 'dupes',       titel: 'Duplikate',        symbol: 'dupes',       gruppe: 'pflege', admin: true },
  { id: 'tags',        titel: 'Tag-Werkstatt',    symbol: 'tags',        gruppe: 'pflege', admin: true },

  { id: 'logs',        titel: 'Protokoll',        symbol: 'logs',        gruppe: 'system', admin: true },
  { id: 'diagnostics', titel: 'Diagnose',         symbol: 'diagnostics', gruppe: 'system', admin: true },
  { id: 'users',       titel: 'Benutzer',         symbol: 'users',       gruppe: 'system', admin: true },
  { id: 'settings',    titel: 'Einstellungen',    symbol: 'sliders',     gruppe: 'system', admin: false },
  { id: 'account',     titel: 'Konto',            symbol: 'account',     gruppe: 'system', admin: false },

  // Detailseiten des Katalogs - über die Suche erreichbar, nicht im Menü.
  { id: 'artist',   titel: 'Interpret', admin: false, nav: false },
  { id: 'album',    titel: 'Album',     admin: false, nav: false },
  { id: 'playlist', titel: 'Playlist',  admin: false, nav: false },
];

const NACH_ID = new Map(MODULE.map((m) => [m.id, m]));

export const findeModul = (id) => NACH_ID.get(id);
export const navModule = (istAdmin) =>
  MODULE.filter((m) => m.nav !== false && (istAdmin || !m.admin));
export const darfSehen = (modul, istAdmin) => Boolean(modul) && (istAdmin || !modul.admin);

/** Menüpunkte nach Gruppen, leere Gruppen fallen weg. */
export function navGruppen(istAdmin) {
  const sichtbar = navModule(istAdmin);
  return GRUPPEN
    .map(([id, titel]) => [titel, sichtbar.filter((m) => m.gruppe === id)])
    .filter(([, module]) => module.length > 0);
}
