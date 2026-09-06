// Einziger Weg zum Server. Kuemmert sich um CSRF, Sitzungsverlust und
// lesbare Fehlermeldungen - damit kein Modul das selbst nachbauen muss.

let beiSitzungsverlust = () => {};

/** Wird von main.js einmal gesetzt: was passieren soll, wenn eine Antwort
 *  401 liefert. */
export function onUnauthorized(handler) {
  beiSitzungsverlust = handler;
}

function csrfToken() {
  const treffer = document.cookie.match(/(?:^|;\s*)mst_csrf=([^;]+)/);
  return treffer ? decodeURIComponent(treffer[1]) : null;
}

let angemeldet = false;
export const setAngemeldet = (wert) => { angemeldet = wert; };

export async function api(pfad, optionen = {}) {
  const opts = { credentials: 'same-origin', headers: {}, ...optionen };
  if (opts.body !== undefined && !(opts.body instanceof FormData)) {
    opts.headers['Content-Type'] = 'application/json';
    opts.body = JSON.stringify(opts.body);
  }
  const token = csrfToken();
  if (token) opts.headers['X-CSRF-Token'] = token;

  let antwort;
  try {
    antwort = await fetch(pfad, opts);
  } catch (exc) {
    // Netzfehler haben keine Antwort - ohne diesen Zweig sieht man nur
    // "Failed to fetch" und weiß nicht, ob der Server oder das WLAN weg ist.
    throw new Error(`Server nicht erreichbar (${exc.message})`);
  }

  if (antwort.status === 401) {
    // Beim Start ist ein 401 normal. Während einer laufenden Sitzung heißt
    // er: das Cookie kommt nicht zurück - und das muss man sehen.
    beiSitzungsverlust(angemeldet
      ? 'Sitzung wurde nicht angenommen. Das Anmelde-Cookie kommt nicht zurück — '
        + 'meist ein Cache-Problem (Strg+Shift+R) oder ein Browser, der Cookies blockiert.'
      : null);
    throw new Error('Nicht angemeldet');
  }

  const text = await antwort.text();
  const daten = text ? JSON.parse(text) : null;
  if (!antwort.ok) throw new Error(daten?.detail || `HTTP ${antwort.status}`);
  return daten;
}

export const get = (pfad) => api(pfad);
export const post = (pfad, body) => api(pfad, { method: 'POST', body });
export const patch = (pfad, body) => api(pfad, { method: 'PATCH', body });
export const del = (pfad) => api(pfad, { method: 'DELETE' });

/** Query-String aus einem Objekt, leere Werte fallen weg. */
export function q(werte) {
  const p = new URLSearchParams();
  for (const [k, v] of Object.entries(werte)) {
    if (v !== undefined && v !== null && v !== '') p.set(k, String(v));
  }
  const s = p.toString();
  return s ? `?${s}` : '';
}
