// Einstiegspunkt: Anmeldung, Navigation aufbauen, Router starten.
// Alles Fachliche liegt in den Modulen unter js/modules/.

import { api, post, onUnauthorized, setAngemeldet } from './core/api.js';
import { $, esc, icon } from './core/dom.js';
import { navModule, findeModul } from './core/registry.js';
import * as router from './core/router.js';
import * as bus from './core/bus.js';
import * as player from './core/player.js';
import { fail } from './core/toast.js';

const state = { user: null };
const istAdmin = () => state.user?.role === 'admin';

// --------------------------------------------------------------- Anmeldung
function zeigeAnmeldung(meldung = null) {
  state.user = null;
  setAngemeldet(false);
  bus.disconnect();
  player.stop();
  $('#app').hidden = true;
  $('#login').hidden = false;

  // Nur setzen, nie löschen: eine Seite feuert mehrere Anfragen parallel,
  // und der zweite 401 würde sonst die Meldung des ersten wegräumen.
  if (meldung) {
    const box = $('#login-error');
    box.textContent = meldung;
    box.hidden = false;
  }
}
onUnauthorized(zeigeAnmeldung);

async function zeigeApp(user) {
  state.user = user;
  setAngemeldet(true);
  $('#login').hidden = true;
  $('#app').hidden = false;
  $('#brand-user').textContent = istAdmin()
    ? `${user.username} · Administrator` : user.username;
  $('.brand-mark').innerHTML = icon('note');

  baueNavigation();
  bus.connect();
  await router.zeichne();
}

function baueNavigation() {
  $('#nav').innerHTML = navModule(istAdmin()).map((m) => `
    <a href="#/${m.id}" data-nav="${m.id}">${icon(m.symbol)}<span>${esc(m.titel)}</span></a>
  `).join('');
}

function markiereAktiv(id) {
  for (const a of document.querySelectorAll('#nav a')) {
    // Detailseiten (Interpret, Album, …) gehören zur Suche.
    const gehoert = a.dataset.nav === id
      || (a.dataset.nav === 'search' && ['artist', 'album', 'playlist'].includes(id));
    if (gehoert) a.setAttribute('aria-current', 'page');
    else a.removeAttribute('aria-current');
  }
  const modul = findeModul(id);
  if (modul) document.title = `${modul.titel} · music-server-tool`;
}

// ----------------------------------------------------------- Ereignisstrom
bus.onConnectionChange((verbunden) => {
  const el = $('#sse-state');
  el.textContent = verbunden ? 'live' : 'getrennt';
  el.className = `pill ${verbunden ? 'pill-ok' : 'pill-err'}`;
});

// Fehler aus dem Protokoll immer sichtbar machen, egal welches Modul läuft.
bus.on('log', (eintrag) => {
  if (eintrag.level === 'error') fail(eintrag.message);
});

// ------------------------------------------------------------- Formulare
$('#login-form').addEventListener('submit', async (event) => {
  event.preventDefault();
  const form = event.target;
  const knopf = $('#login-submit');
  const box = $('#login-error');
  box.hidden = true;
  knopf.disabled = true;
  knopf.textContent = 'Anmelden…';

  const daten = new FormData(form);
  try {
    const user = await post('/api/auth/login', {
      username: daten.get('username'),
      password: daten.get('password'),
      totp: daten.get('totp') || null,
    });
    form.reset();
    await zeigeApp(user);
  } catch (exc) {
    box.textContent = exc.message;
    box.hidden = false;
    // Verlangt der Server einen Code, blenden wir das Feld nach und setzen
    // den Fokus hinein - sonst sucht man es.
    if (/Authenticator/i.test(exc.message)) {
      $('#totp-field').hidden = false;
      $('#login-totp').focus();
    } else {
      $('#login-pass').focus();
    }
  } finally {
    knopf.disabled = false;
    knopf.textContent = 'Anmelden';
  }
});

$('#logout').addEventListener('click', async () => {
  try { await post('/api/auth/logout'); } catch { /* egal */ }
  zeigeAnmeldung();
});

// ------------------------------------------------------------------ Start
router.init({ istAdmin, user: () => state.user }, markiereAktiv);

(async function start() {
  try {
    await zeigeApp(await api('/api/auth/me'));
  } catch {
    zeigeAnmeldung();
  }
})();
