// Einstiegspunkt: Anmeldung, Navigation aufbauen, Router starten.
// Alles Fachliche liegt in den Modulen unter js/modules/.

import { api, post, onUnauthorized, setAngemeldet } from './core/api.js';
import { $, esc, icon } from './core/dom.js';
import { navGruppen, findeModul, darfSehen } from './core/registry.js';
import * as router from './core/router.js';
import * as bus from './core/bus.js';
import * as player from './core/player.js';
import * as prefs from './core/prefs.js';
import * as shortcuts from './core/shortcuts.js';
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

  // Ohne Route in der Adresse auf die eingestellte Startseite gehen. Wer
  // einen Link geöffnet hat, landet dort, wo der Link hinzeigt.
  const start = prefs.hole('startseite');
  if (!location.hash && start && start !== 'overview'
      && darfSehen(findeModul(start), istAdmin())) {
    location.hash = `#/${start}`;
  }
  await router.zeichne();
}

function baueNavigation() {
  // Gruppiert statt als eine Liste von elf Punkten: die Überschriften sagen,
  // wo etwas hingehört, und man liest sie einmal statt jedes Mal zu suchen.
  $('#nav').innerHTML = navGruppen(istAdmin()).map(([titel, module]) => `
    <div class="nav-group">${esc(titel)}</div>
    ${module.map((m) => `
      <a href="#/${m.id}" data-nav="${m.id}">${icon(m.symbol)}<span>${esc(m.titel)}</span>
        <span class="nav-count" data-zaehler="${m.id}" hidden></span></a>`).join('')}
  `).join('');
}

/** Zahl am Menüpunkt — zeigt offene Arbeit, ohne dass man die Seite öffnet. */
function setzeZaehler(id, anzahl, beschaeftigt = false) {
  const el = document.querySelector(`[data-zaehler="${id}"]`);
  if (!el) return;
  el.hidden = !anzahl;
  el.textContent = anzahl > 99 ? '99+' : String(anzahl);
  el.className = `nav-count${beschaeftigt ? ' nav-count-busy' : ''}`;
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

  // Kurzes Einblenden je Seitenwechsel. Die Klasse muss erst weg und dann
  // wieder dran, sonst läuft die Animation beim zweiten Mal nicht.
  const view = $('#view');
  view.classList.remove('view-in');
  void view.offsetWidth;
  view.classList.add('view-in');
}

// ----------------------------------------------------------- Ereignisstrom
bus.onConnectionChange((verbunden) => {
  const el = $('#sse-state');
  el.textContent = verbunden ? 'live' : 'getrennt';
  el.className = `pill ${verbunden ? 'pill-ok' : 'pill-err'}`;
});

// Offene Arbeit an den Menüpunkten anzeigen. Kommt ohnehin über den Strom —
// dafür braucht es keine eigene Abfrage.
bus.on('state', (stand) => {
  const jobs = stand.jobs || {};
  const laufend = (jobs.running || 0) + (jobs.pending || 0);
  setzeZaehler('jobs', laufend, Boolean(jobs.running));
  const offen = (stand.queue || []).filter(
    (i) => !i.navidrome_id && i.state !== 'ready').length;
  setzeZaehler('queue', offen, offen > 0);
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

// ----------------------------------------------------------- Tastaturkürzel
function zeigeKuerzel() {
  if (state.user) router.gehe('settings');
}
$('#shortcuts').innerHTML = icon('keyboard');
$('#shortcuts').addEventListener('click', zeigeKuerzel);

shortcuts.init({
  hilfe: zeigeKuerzel,
  stopp: () => player.stop(),
  darf: (id) => Boolean(state.user) && darfSehen(findeModul(id), istAdmin()),
});

// ------------------------------------------------------------------ Start
$('#login-mark').innerHTML = icon('note');
prefs.wendeAn();
router.init({ istAdmin, user: () => state.user }, markiereAktiv);

(async function start() {
  try {
    await zeigeApp(await api('/api/auth/me'));
  } catch {
    zeigeAnmeldung();
  }
})();
