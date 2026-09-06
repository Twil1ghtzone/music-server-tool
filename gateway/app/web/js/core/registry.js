// Was es an Modulen gibt. Eine neue Seite hinzufügen heißt: eine Datei
// unter js/modules/ anlegen und hier eine Zeile ergänzen — sonst nichts.
//
// `nav: false` heißt: erreichbar über eine Route, aber kein eigener
// Menüpunkt (die Detailseiten von Interpret, Album und Playlist).

export const MODULE = [
  { id: 'overview',    titel: 'Übersicht',        symbol: 'overview',    admin: false },
  { id: 'search',      titel: 'Suche & Download', symbol: 'search',      admin: false },
  { id: 'queue',       titel: 'Warteschlange',    symbol: 'queue',       admin: false },
  { id: 'jobs',        titel: 'Jobs',             symbol: 'jobs',        admin: false },
  { id: 'library',     titel: 'Bibliothek',       symbol: 'library',     admin: true  },
  { id: 'dupes',       titel: 'Duplikate',        symbol: 'dupes',       admin: true  },
  { id: 'tags',        titel: 'Tag-Werkstatt',    symbol: 'tags',        admin: true  },
  { id: 'logs',        titel: 'Protokoll',        symbol: 'logs',        admin: true  },
  { id: 'diagnostics', titel: 'Diagnose',         symbol: 'diagnostics', admin: true  },
  { id: 'users',       titel: 'Benutzer',         symbol: 'users',       admin: true  },
  { id: 'account',     titel: 'Konto',            symbol: 'account',     admin: false },

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
