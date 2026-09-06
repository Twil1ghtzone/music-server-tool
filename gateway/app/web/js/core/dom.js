// Kleine Bauhelfer. Absichtlich kein Framework: der Zustand je Modul ist
// klein, und ein Build-Schritt im Container waere reiner Ballast.

export const $ = (sel, root = document) => root.querySelector(sel);
export const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];

/** Text fuer den Einbau in HTML entschaerfen. Jeder Wert aus einer Antwort
 *  muss hier durch - Titel und Pfade enthalten regelmaessig < & ". */
export const esc = (value) =>
  String(value ?? '').replace(/[&<>"']/g, (c) =>
    ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

// ------------------------------------------------------------------ Zahlen
// Intl statt eigener Formatierung: respektiert die Spracheinstellung und
// setzt Tausendertrenner richtig.
const zahl = new Intl.NumberFormat('de-DE');
export const num = (value) => zahl.format(Number(value) || 0);

export function bytes(value) {
  const n = Number(value) || 0;
  if (!n) return '0 B';
  const einheiten = ['B', 'KB', 'MB', 'GB', 'TB'];
  const i = Math.min(Math.floor(Math.log(n) / Math.log(1024)), einheiten.length - 1);
  const wert = n / 1024 ** i;
  // Geschuetztes Leerzeichen: "12 MB" soll nie umbrechen.
  return `${wert.toFixed(i ? 1 : 0)} ${einheiten[i]}`;
}

export function duration(seconds) {
  const s = Math.round(Number(seconds) || 0);
  if (s >= 3600) return `${Math.floor(s / 3600)}:${String(Math.floor((s % 3600) / 60)).padStart(2, '0')}:${String(s % 60).padStart(2, '0')}`;
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}`;
}

// Zustandsnamen kommen aus der Datenbank und sind englisch. Sie stehen an
// vier Stellen in der Oberfläche — deshalb hier einmal übersetzt und nicht
// je Modul neu erfunden.
const ZUSTAND = {
  queued: ['eingereiht', 'pill-busy'],
  pending: ['wartet', 'pill-busy'],
  downloading: ['lädt', 'pill-busy'],
  running: ['läuft', 'pill-busy'],
  importing: ['wird importiert', 'pill-busy'],
  ready: ['fertig', 'pill-ok'],
  done: ['fertig', 'pill-ok'],
  failed: ['fehlgeschlagen', 'pill-err'],
  cancelled: ['abgebrochen', ''],
  virtual: ['nicht geladen', ''],
};

/** Zustand als deutsche Plakette. Unbekannte Zustände werden durchgereicht,
 *  statt sie zu verschlucken — ein neuer Zustand soll sichtbar sein. */
/** Setzt die Breite aller Fortschrittsbalken unterhalb von `wurzel`.
 *  Muss nach dem Einsetzen von HTML aufgerufen werden: die Richtlinie
 *  style-src 'self' verwirft style="…" im Markup, die Zuweisung über
 *  element.style betrifft sie dagegen nicht. */
/**
 * Ein Geheimnis zum Abschreiben — mit Kopierknopf.
 *
 * Der Knopf ist nicht Bequemlichkeit, sondern die Fehlerquelle: ein
 * zwanzigstelliges Passwort tippt man falsch ab, und fünf Fehlversuche
 * sperren den Zugang. Wer kopiert, vertippt sich nicht.
 */
export function geheimnis(wert, beschriftung = 'Kopieren') {
  return `<span class="secret-row">
    <code class="mono break secret" data-geheim>${esc(wert)}</code>
    <button type="button" class="btn btn-sm" data-kopiere="${esc(wert)}"
            title="${esc(beschriftung)}">${icon('logs')} ${esc(beschriftung)}</button>
  </span>`;
}

/**
 * Hängt einen Zuhörer an, der jeden [data-kopiere]-Knopf darunter bedient.
 * Gibt die Abmeldefunktion zurück.
 */
export function kopierKnoepfe(wurzel, beiErfolg = () => {}, beiFehler = () => {}) {
  const handler = async (e) => {
    const knopf = e.target.closest('[data-kopiere]');
    if (!knopf) return;
    try {
      await navigator.clipboard.writeText(knopf.dataset.kopiere);
      const alt = knopf.innerHTML;
      knopf.innerHTML = `${icon('check')} Kopiert`;
      setTimeout(() => { knopf.innerHTML = alt; }, 1600);
      beiErfolg();
    } catch {
      // Ohne sicheren Kontext (http statt https) verweigern manche Browser
      // die Zwischenablage. Dann bleibt das Markieren von Hand.
      beiFehler('Der Browser hat die Zwischenablage verweigert — '
                + 'markiere den Wert und kopiere ihn von Hand.');
    }
  };
  wurzel.addEventListener('click', handler);
  return () => wurzel.removeEventListener('click', handler);
}

export function balkenFuellen(wurzel) {
  for (const balken of (wurzel || document).querySelectorAll('.bar[data-anteil]')) {
    const span = balken.firstElementChild;
    if (span) span.style.width = `${Math.max(0, Math.min(100, Number(balken.dataset.anteil) || 0))}%`;
  }
}

export function zustandPill(state) {
  const [text, klasse] = ZUSTAND[state] || [state || '—', ''];
  return `<span class="pill ${klasse}">${esc(text)}</span>`;
}

// Protokollstufen kommen ebenfalls englisch aus der Datenbank.
const STUFE = {
  error: ['Fehler', 'pill-err'],
  warn: ['Warnung', 'pill-warn'],
  warning: ['Warnung', 'pill-warn'],
  info: ['Info', ''],
  debug: ['Detail', ''],
};

export function stufePill(level) {
  const [text, klasse] = STUFE[level] || [level || '—', ''];
  return `<span class="pill ${klasse}">${esc(text)}</span>`;
}

export function zustandText(state) {
  return (ZUSTAND[state] || [state || '—'])[0];
}

// Einmal bauen, nicht je Zeile: das Protokoll zeigt bis zu 300 Einträge.
const rtf = new Intl.RelativeTimeFormat('de-DE', { numeric: 'auto' });

export function relativeTime(iso) {
  if (!iso) return '—';
  const ts = Date.parse(iso.replace(' ', 'T') + (iso.endsWith('Z') ? '' : 'Z'));
  if (Number.isNaN(ts)) return esc(iso);
  const sek = Math.round((Date.now() - ts) / 1000);
  if (sek < 60) return rtf.format(-sek, 'second');
  if (sek < 3600) return rtf.format(-Math.round(sek / 60), 'minute');
  if (sek < 86400) return rtf.format(-Math.round(sek / 3600), 'hour');
  return rtf.format(-Math.round(sek / 86400), 'day');
}

// ------------------------------------------------------------------ Symbole
// SVG statt Emoji: Emojis werden je nach System anders gezeichnet, lassen
// sich nicht einfaerben und liest ein Screenreader vor.
const PFADE = {
  overview: 'M3 12h4l3 8 4-16 3 8h4',
  search: 'M11 19a8 8 0 1 0 0-16 8 8 0 0 0 0 16Zm10 2-4.35-4.35',
  queue: 'M4 6h11M4 12h11M4 18h7M17 14l4 4-4 4',
  jobs: 'M12 7v5l3 2M21 12a9 9 0 1 1-18 0 9 9 0 0 1 18 0Z',
  library: 'M4 19V5a2 2 0 0 1 2-2h2v18H6a2 2 0 0 1-2-2Zm6-16h2v18h-2V3Zm5.5.4 1.9 17.4',
  dupes: 'M8 8h11v11H8zM5 16H3V5a2 2 0 0 1 2-2h11v2',
  tags: 'M20.6 13.4 12 22l-9-9V3h10l7.6 7.6a2 2 0 0 1 0 2.8ZM7.5 7.5h.01',
  logs: 'M4 4h16v16H4zM8 9h8M8 13h8M8 17h5',
  diagnostics: 'M22 12h-4l-3 9L9 3l-3 9H2',
  users: 'M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2M9 11a4 4 0 1 0 0-8 4 4 0 0 0 0 8Zm13 10v-2a4 4 0 0 0-3-3.9',
  account: 'M20 21v-2a4 4 0 0 0-4-4H8a4 4 0 0 0-4 4v2M12 11a4 4 0 1 0 0-8 4 4 0 0 0 0 8Z',
  play: 'M6 4l14 8-14 8z',
  pause: 'M7 4h4v16H7zM13 4h4v16h-4z',
  download: 'M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4M7 10l5 5 5-5M12 15V3',
  close: 'M18 6 6 18M6 6l12 12',
  back: 'M19 12H5m7-7-7 7 7 7',
  warn: 'M12 9v4m0 4h.01M10.3 3.9 1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0Z',
  info: 'M12 16v-4m0-4h.01M22 12a10 10 0 1 1-20 0 10 10 0 0 1 20 0Z',
  empty: 'M22 12h-6l-2 3h-4l-2-3H2M5.5 5.1 2 12v6a2 2 0 0 0 2 2h16a2 2 0 0 0 2-2v-6l-3.5-6.9A2 2 0 0 0 16.7 4H7.3a2 2 0 0 0-1.8 1.1Z',
  note: 'M9 18V5l12-2v13M9 18a3 3 0 1 1-6 0 3 3 0 0 1 6 0Zm12-2a3 3 0 1 1-6 0 3 3 0 0 1 6 0Z',
  refresh: 'M3 12a9 9 0 0 1 15-6.7L21 8M21 3v5h-5M21 12a9 9 0 0 1-15 6.7L3 16M3 21v-5h5',
  settings: 'M12 15a3 3 0 1 0 0-6 3 3 0 0 0 0 6Zm7.4-3a7.4 7.4 0 0 0-.1-1.1l2-1.5-2-3.4-2.3.9a7.5 7.5 0 0 0-1.9-1.1L14.7 3H10.3l-.4 2.4c-.7.3-1.3.6-1.9 1.1l-2.3-.9-2 3.4 2 1.5a7.4 7.4 0 0 0 0 2.2l-2 1.5 2 3.4 2.3-.9c.6.5 1.2.8 1.9 1.1l.4 2.4h4.4l.4-2.4c.7-.3 1.3-.6 1.9-1.1l2.3.9 2-3.4-2-1.5c.1-.4.1-.7.1-1.1Z',
  sortUp: 'M3 6h13M3 12h9M3 18h5M18 20V8m0 0-3 3m3-3 3 3',
  sortDown: 'M3 6h5M3 12h9M3 18h13M18 4v12m0 0 3-3m-3 3-3-3',
  grid: 'M3 3h7v7H3zM14 3h7v7h-7zM14 14h7v7h-7zM3 14h7v7H3z',
  rows: 'M3 5h18M3 12h18M3 19h18',
  sliders: 'M4 21v-7M4 10V3M12 21v-9M12 8V3M20 21v-5M20 12V3M1 14h6M9 8h6M17 16h6',
  check: 'M20 6 9 17l-5-5',
  keyboard: 'M6 8h.01M10 8h.01M14 8h.01M18 8h.01M8 12h.01M12 12h.01M16 12h.01M7 16h10M2 5h20v14H2z',
  help: 'M12 17h.01M9.1 9a3 3 0 1 1 4.2 2.7c-.8.4-1.3 1.2-1.3 2.1M22 12a10 10 0 1 1-20 0 10 10 0 0 1 20 0Z',
  bolt: 'M13 2 3 14h8l-1 8 10-12h-8z',
};

/** Dekoratives Symbol. Wird von Hilfsmitteln uebersprungen - traegt es
 *  Bedeutung, gehoert daneben Text oder ein aria-label an die Schaltflaeche. */
export function icon(name, klasse = '') {
  const d = PFADE[name] || PFADE.info;
  return `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"
    stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"
    class="${esc(klasse)}"><path d="${d}"/></svg>`;
}

// ------------------------------------------------------------------ Zustaende
// Drei Bausteine, die jede Liste haben sollte. Ohne sie sieht ein Fehler
// aus wie "nichts da" und ein Ladevorgang wie ein kaputtes Modul.

export const skeleton = (zeilen = 5) =>
  `<div class="list" aria-busy="true" aria-live="polite">${
    Array.from({ length: zeilen }, () => '<div class="skeleton skeleton-row"></div>').join('')
  }<span class="sr-only">Wird geladen…</span></div>`;

export const empty = (text, hinweis = '') =>
  `<div class="empty">${icon('empty')}<p>${esc(text)}</p>${
    hinweis ? `<p class="tiny faint">${esc(hinweis)}</p>` : ''}</div>`;

export const failure = (text, hinweis = '') =>
  `<div class="failure" role="alert">${icon('warn')}<p>${esc(text)}</p>${
    hinweis ? `<p class="tiny muted">${esc(hinweis)}</p>` : ''}</div>`;

/** Kachel fuer Kennzahlen. `art` faerbt den Wert: ok, warn, err. */
export const tile = (label, wert, sub = '', art = '') => `
  <div class="tile ${art ? `tile-${art}` : ''}">
    <div class="tile-label">${esc(label)}</div>
    <div class="tile-value">${esc(wert)}</div>
    ${sub ? `<div class="tile-sub">${esc(sub)}</div>` : ''}
  </div>`;

/** Ruft `laden` auf, zeigt solange ein Skelett und faengt Fehler ab, statt
 *  sie in der Konsole verschwinden zu lassen. */
export async function withState(el, laden, zeichnen, { zeilen = 5 } = {}) {
  el.innerHTML = skeleton(zeilen);
  try {
    el.innerHTML = zeichnen(await laden());
  } catch (exc) {
    el.innerHTML = failure('Konnte nicht geladen werden.', exc.message);
  }
}
