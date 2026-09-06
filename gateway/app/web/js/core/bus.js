// Ein einziger Ereignisstrom für die ganze Oberfläche.
//
// Vorher öffnete das Dashboard eine EventSource und schrieb aus dem
// Empfangs-Handler direkt in verschiedene Seiten. Jetzt gibt es genau eine
// Verbindung, und Module melden sich für das an, was sie brauchen. Beim
// Modulwechsel wird abgemeldet — sonst laufen nach ein paar Wechseln
// mehrere Zuhörer parallel und schreiben in längst ersetzte Elemente.

const zuhoerer = new Map();   // art -> Set<fn>
let strom = null;
let zustandGeaendert = () => {};

export function onConnectionChange(handler) { zustandGeaendert = handler; }

export function on(art, handler) {
  if (!zuhoerer.has(art)) zuhoerer.set(art, new Set());
  zuhoerer.get(art).add(handler);
  return () => off(art, handler);          // Abmeldefunktion zurückgeben
}

export function off(art, handler) {
  zuhoerer.get(art)?.delete(handler);
}

function verteile(art, daten) {
  for (const handler of zuhoerer.get(art) || []) {
    try {
      handler(daten);
    } catch (exc) {
      // Ein kaputter Zuhörer darf die anderen nicht mitreißen.
      console.error(`Ereignis-Zuhörer für "${art}" fehlgeschlagen:`, exc);
    }
  }
}

export function connect() {
  disconnect();
  const es = new EventSource('/api/events');
  strom = es;

  es.onopen = () => zustandGeaendert(true);
  es.onerror = () => zustandGeaendert(false);
  // Der Server sendet zwei Arten: einzelne Protokollzeilen und in größeren
  // Abständen eine Momentaufnahme von Jobs und Warteschlange.
  es.addEventListener('log', (e) => verteile('log', JSON.parse(e.data)));
  es.addEventListener('state', (e) => verteile('state', JSON.parse(e.data)));
}

export function disconnect() {
  if (strom) { strom.close(); strom = null; }
  zustandGeaendert(false);
}
