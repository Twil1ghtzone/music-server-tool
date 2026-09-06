// Benutzer: Dashboard-Konten anlegen, Rolle setzen, Passwort erzeugen.

import { get, post, patch, del } from '../core/api.js';
import { $, esc, icon, empty, failure, skeleton, relativeTime } from '../core/dom.js';
import { werkzeugleiste, anwenden } from '../core/toolbar.js';
import * as prefs from '../core/prefs.js';
import { ok, fail } from '../core/toast.js';

export const meta = { id: 'users', titel: 'Benutzer' };

export async function mount(wurzel) {
  wurzel.innerHTML = `
    <div class="page-head">
      <div>
        <h2>Benutzer</h2>
        <p class="lede">Konten für dieses Dashboard.</p>
      </div>
    </div>

    <div class="notice">${icon('info')}<div>
      Musik-Clients melden sich weiterhin mit ihren <strong>Navidrome</strong>-Konten an —
      daran ändert sich nichts. Wer nur Musik hören soll, braucht hier kein Konto.
    </div></div>

    <div class="split">
      <form class="card" id="u-create">
        <h3>Neuen Benutzer anlegen</h3>
        <div class="stack">
          <label for="u-name">Benutzername
            <input id="u-name" name="username" pattern="[A-Za-z0-9._@\\-]+" required
                   autocomplete="off" spellcheck="false">
          </label>
          <label for="u-pass">Passwort — leer lassen, dann wird eins erzeugt
            <input id="u-pass" name="password" type="password" minlength="10"
                   autocomplete="new-password" placeholder="wird erzeugt">
          </label>
          <label for="u-role">Rolle
            <select id="u-role" name="role">
              <option value="user">Benutzer — suchen und herunterladen</option>
              <option value="admin">Administrator — darf alles</option>
            </select>
          </label>
          <button class="btn btn-primary" type="submit">Anlegen</button>
        </div>
        <div id="u-secret"></div>
      </form>

      <div class="card card-flush">
        <h3>Vorhandene Benutzer</h3>
        <div id="u-leiste"></div>
        <div id="u-list" class="list">${skeleton(3)}</div>
      </div>
    </div>`;

  // Ein erzeugtes Passwort erscheint genau einmal — danach liegt nur noch
  // der Hash in der Datenbank. Entsprechend auffällig zeigen.
  function zeigePasswort(name, passwort) {
    $('#u-secret').innerHTML = `
      <div class="notice mt-4">${icon('warn')}<div>
        Passwort für <strong>${esc(name)}</strong> — wird nur jetzt angezeigt:
        <code class="mono break secret">${esc(passwort)}</code>
        Steht auch im Log: <code>docker logs music-gateway-api | grep Passwort</code>
      </div></div>`;
  }

  let alle = [];

  const leiste = werkzeugleiste({
    id: 'users',
    standard: { q: '', sort: 'username', richtung: 'auf', rolle: 'alle', details: true },
    suche: { platzhalter: 'Benutzer filtern…', label: 'Benutzer filtern' },
    filter: [{ name: 'rolle', label: 'Nach Rolle filtern', werte: [
      ['alle', 'Alle Rollen'], ['admin', 'Nur Administratoren'], ['user', 'Nur Benutzer'],
    ] }],
    sortierung: [['username', 'Name'], ['last_login_at', 'Letzte Anmeldung'], ['sessions', 'Sitzungen']],
    zusatz: [
      { name: 'details', art: 'schalter', label: 'Zweite Zeile zeigen',
        hinweis: 'Letzte Anmeldung, offene Sitzungen, Zwei-Faktor' },
    ],
    beiAenderung: () => zeichne(),
  });
  $('#u-leiste').replaceWith(leiste.el);
  leiste.el.classList.add('pad-body');

  function zeichne() {
    const el = $('#u-list');
    if (!el) return;
    const w = leiste.werte();
    let users = w.rolle === 'alle' ? alle : alle.filter((u) => u.role === w.rolle);
    users = anwenden(users, w, {
      felder: ['username'],
      sortierer: {
        username: (u) => u.username || '',
        last_login_at: (u) => u.last_login_at || '',
        sessions: (u) => u.sessions || 0,
      },
    });
    leiste.setzeZaehler(users.length === alle.length
      ? `${alle.length}` : `${users.length} von ${alle.length}`);

    el.innerHTML = users.length ? users.map((u) => `
        <div class="item">
          <div class="item-main">
            <div class="item-title">${esc(u.username)}${
              u.self ? ' <span class="faint tiny">(du)</span>' : ''}</div>
            ${w.details ? `<div class="item-sub">${
              u.last_login_at ? `zuletzt ${esc(relativeTime(u.last_login_at))}` : 'nie angemeldet'
            } · ${u.sessions} offene Sitzung(en)${u.totp_enabled ? ' · 2FA' : ''}</div>` : ''}
          </div>
          <div class="item-side">
            <label class="sr-only" for="role-${u.id}">Rolle von ${esc(u.username)}</label>
            <select id="role-${u.id}" data-role="${u.id}" ${u.self ? 'disabled' : ''}>
              <option value="user" ${u.role === 'user' ? 'selected' : ''}>Benutzer</option>
              <option value="admin" ${u.role === 'admin' ? 'selected' : ''}>Administrator</option>
            </select>
            <button type="button" class="btn btn-sm" data-reset="${u.id}"
                    data-name="${esc(u.username)}">Passwort</button>
            ${u.self ? '' : `<button type="button" class="btn btn-sm btn-danger"
              data-del="${u.id}" data-name="${esc(u.username)}">Löschen</button>`}
          </div>
        </div>`).join('')
      : empty(alle.length ? 'Kein Benutzer passt zum Filter.' : 'Keine Benutzer.',
              alle.length ? 'Setze Suche und Rollenfilter oben zurück.' : '');
  }

  async function lade() {
    try {
      alle = (await get('/api/users')).users || [];
      zeichne();
    } catch (exc) {
      if ($('#u-list')) $('#u-list').innerHTML = failure('Benutzer nicht abrufbar.', exc.message);
    }
  }
  await lade();

  $('#u-create').addEventListener('submit', async (e) => {
    e.preventDefault();
    const knopf = e.target.querySelector('button[type=submit]');
    knopf.disabled = true;
    const daten = new FormData(e.target);
    const passwort = (daten.get('password') || '').trim();
    try {
      const r = await post('/api/users', {
        username: daten.get('username'),
        password: passwort || null,
        role: daten.get('role'),
      });
      e.target.reset();
      if (r.password) zeigePasswort(r.username, r.password);
      else $('#u-secret').innerHTML = '';
      ok('Benutzer angelegt');
      await lade();
    } catch (exc) { fail(exc.message); } finally { knopf.disabled = false; }
  });

  $('#u-list').addEventListener('change', async (e) => {
    const auswahl = e.target.closest('[data-role]');
    if (!auswahl) return;
    try {
      await patch(`/api/users/${auswahl.dataset.role}`, { role: auswahl.value });
      ok('Rolle geändert');
    } catch (exc) { fail(exc.message); }
    await lade();
  });

  $('#u-list').addEventListener('click', async (e) => {
    const neu = e.target.closest('[data-reset]');
    const weg = e.target.closest('[data-del]');
    if (!neu && !weg) return;
    try {
      if (neu) {
        if (!confirm(`Neues Passwort für ${neu.dataset.name} erzeugen?\n\n`
          + 'Es wird einmal angezeigt, und alle offenen Sitzungen dieses Benutzers werden beendet.')) return;
        const r = await patch(`/api/users/${neu.dataset.reset}`, { generate_password: true });
        await lade();
        if (r.password) zeigePasswort(neu.dataset.name, r.password);
        ok('Passwort erzeugt. Offene Sitzungen wurden beendet.');
      } else {
        // Ein Benutzer verschwindet endgültig — hier wird auch dann gefragt,
        // wenn Rückfragen abgeschaltet sind.
        if (!confirm(`${weg.dataset.name} wirklich löschen?`)) return;
        await del(`/api/users/${weg.dataset.del}`);
        ok('Benutzer gelöscht');
        await lade();
      }
    } catch (exc) { fail(exc.message); }
  });
}
