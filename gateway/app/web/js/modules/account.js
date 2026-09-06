// Konto: eigenes Passwort und Zwei-Faktor.

import { get, post } from '../core/api.js';
import { $, esc, icon, failure, skeleton } from '../core/dom.js';
import { ok, fail } from '../core/toast.js';

export const meta = { id: 'account', titel: 'Konto' };

export async function mount(wurzel, ctx) {
  wurzel.innerHTML = `
    <div class="page-head"><div><h2>Konto</h2>
      <p class="lede">Gilt für dieses Dashboard, nicht für deinen Navidrome-Zugang.</p></div></div>
    <div class="split" id="acc-body">${skeleton(4)}</div>`;

  let ich;
  try {
    ich = await get('/api/auth/me');
  } catch (exc) {
    if ($('#acc-body')) $('#acc-body').innerHTML = failure('Konto nicht abrufbar.', exc.message);
    return;
  }
  if (!$('#acc-body')) return;

  $('#acc-body').innerHTML = `
    <div class="card">
      <form id="pw-form" class="stack">
        <h3>Passwort ändern</h3>
        <label for="pw-cur">Aktuelles Passwort
          <input id="pw-cur" name="current" type="password" autocomplete="current-password" required>
        </label>
        <label for="pw-new">Neues Passwort (mindestens 10 Zeichen)
          <input id="pw-new" name="new" type="password" minlength="10"
                 autocomplete="new-password" required>
        </label>
        <button class="btn btn-primary" type="submit">Ändern</button>
      </form>

      <hr>

      <h3>Passwort vergessen</h3>
      <p class="muted small">Erzeugt ein neues Passwort, ohne das alte zu kennen. Es wird hier
        einmal angezeigt und steht zusätzlich im Log. Alle offenen Sitzungen werden beendet —
        du musst dich danach neu anmelden.</p>
      <button type="button" class="btn btn-danger" id="pw-reset">Neues Passwort erzeugen</button>
      <div id="pw-out"></div>
    </div>

    <div class="card">
      <h3>Zwei-Faktor-Authentifizierung</h3>
      <p class="muted small" id="totp-state">${ich.totp_enabled
        ? 'Zwei-Faktor ist aktiv.' : 'Zwei-Faktor ist nicht eingerichtet.'}</p>

      <div id="totp-setup" hidden>
        <p class="muted small">Geheimnis in der Authenticator-App eintragen:</p>
        <code class="mono break secret" id="totp-secret"></code>
        <form class="row" id="totp-on">
          <label class="sr-only" for="totp-code">Code aus der App</label>
          <input class="grow" id="totp-code" name="code" inputmode="numeric" spellcheck="false"
                 placeholder="6-stelliger Code" required>
          <button class="btn btn-primary" type="submit">Aktivieren</button>
        </form>
      </div>

      <div class="btn-row mt-3">
        <button type="button" class="btn" id="totp-start">Einrichten</button>
        <button type="button" class="btn btn-ghost" id="totp-off" ${
          ich.totp_enabled ? '' : 'hidden'}>Deaktivieren</button>
      </div>
    </div>`;

  function zeigePasswort(name, passwort) {
    $('#pw-out').innerHTML = `
      <div class="notice mt-4">${icon('warn')}<div>
        Neues Passwort für <strong>${esc(name)}</strong> — wird nur jetzt angezeigt:
        <code class="mono break secret">${esc(passwort)}</code>
        Jetzt notieren und danach neu anmelden.
      </div></div>`;
  }

  $('#pw-form').addEventListener('submit', async (e) => {
    e.preventDefault();
    const knopf = e.target.querySelector('button');
    knopf.disabled = true;
    const daten = new FormData(e.target);
    try {
      await post('/api/auth/password', { current: daten.get('current'), new: daten.get('new') });
      e.target.reset();
      ok('Passwort geändert. Andere Sitzungen wurden beendet.');
    } catch (exc) { fail(exc.message); } finally { knopf.disabled = false; }
  });

  $('#pw-reset').addEventListener('click', async () => {
    const totp = ich.totp_enabled
      ? prompt('Zwei-Faktor ist aktiv. Code aus der Authenticator-App:') : null;
    if (ich.totp_enabled && !totp) return;
    if (!confirm('Neues Passwort erzeugen?\n\nAlle offenen Sitzungen werden beendet — '
                 + 'du musst dich danach neu anmelden.')) return;
    try {
      const r = await post('/api/auth/password/reset', { totp });
      zeigePasswort(r.username, r.password);
      ok('Neues Passwort erzeugt — jetzt notieren.');
    } catch (exc) { fail(exc.message); }
  });

  $('#totp-start').addEventListener('click', async () => {
    try {
      const d = await post('/api/auth/totp/setup');
      $('#totp-secret').textContent = d.secret;
      $('#totp-setup').hidden = false;
      $('#totp-code').focus();
    } catch (exc) { fail(exc.message); }
  });

  $('#totp-on').addEventListener('submit', async (e) => {
    e.preventDefault();
    try {
      await post('/api/auth/totp/enable', { code: new FormData(e.target).get('code') });
      $('#totp-setup').hidden = true;
      $('#totp-state').textContent = 'Zwei-Faktor ist aktiv.';
      $('#totp-off').hidden = false;
      ich.totp_enabled = true;
      ok('Zwei-Faktor aktiviert');
    } catch (exc) { fail(exc.message); }
  });

  $('#totp-off').addEventListener('click', async () => {
    const code = prompt('Aktuellen Code zur Bestätigung eingeben:');
    if (!code) return;
    try {
      await post('/api/auth/totp/disable', { code });
      $('#totp-state').textContent = 'Zwei-Faktor ist nicht eingerichtet.';
      $('#totp-off').hidden = true;
      ich.totp_enabled = false;
      ok('Zwei-Faktor deaktiviert');
    } catch (exc) { fail(exc.message); }
  });
}
