// Diagnose: Zugänge einrichten und prüfen, ob alles zusammenpasst.

import { get, post, del } from '../core/api.js';
import { $, esc, icon, tile, skeleton, failure } from '../core/dom.js';
import { ok, fail } from '../core/toast.js';

export const meta = { id: 'diagnostics', titel: 'Diagnose' };

const STUFE = { ok: ['pill-ok', 'ok'], warn: ['pill-warn', 'hinweis'], fail: ['pill-err', 'fehler'] };
const QUELLE = {
  env: 'aus der Umgebung (NAVIDROME_PASSWORD)',
  manual: 'hier hinterlegt',
  borrowed: 'von einem angemeldeten Musik-Client übernommen',
};

export async function mount(wurzel) {
  wurzel.innerHTML = `
    <div class="page-head">
      <div>
        <h2>Diagnose</h2>
        <p class="lede">Zugänge, Startprüfung und was ein Musik-Client tatsächlich sieht.</p>
      </div>
      <div class="page-actions">
        <button type="button" class="btn" id="d-reload">${icon('refresh')} Neu prüfen</button>
      </div>
    </div>
    <div id="d-body">${skeleton(6)}</div>`;

  async function lade() {
    const el = $('#d-body');
    el.innerHTML = skeleton(6);
    try {
      const [diag, pre, cred, arl] = await Promise.all([
        get('/api/diagnostics'), get('/api/preflight'),
        get('/api/navidrome/credentials'), get('/api/deemix/arl'),
      ]);
      el.innerHTML = zeichne(diag, pre, cred, arl);
    } catch (exc) {
      el.innerHTML = failure('Diagnose nicht abrufbar.', exc.message);
    }
  }

  function zeichne(d, pre, cred, arl) {
    const bereit = pre.ready
      ? `<div class="notice">${icon('info')}<div>Startprüfung bestanden — bereit für den ersten Download.
           ${pre.counts.warn} Hinweis(e).</div></div>`
      : `<div class="notice notice-err">${icon('warn')}<div>
           <strong>${pre.counts.fail} Prüfung(en) fehlgeschlagen.</strong> Vor dem ersten
           Download beheben — sonst laufen Downloads ins Leere oder landen falsch.</div></div>`;

    const zugang = cred.configured
      ? `<p>Verbunden als <strong>${esc(cred.username)}</strong> — ${esc(QUELLE[cred.source] || 'vorhanden')}.</p>
         ${cred.editable ? '<button type="button" class="btn btn-ghost" id="cred-clear">Zugang entfernen</button>'
                         : '<p class="muted small">In der Umgebung gesetzt — hier nicht änderbar.</p>'}`
      : `<p class="muted">Der Gateway braucht einen Navidrome-Zugang, um importierte Titel
           auf ihre ID aufzulösen und Scans anzustoßen.</p>
         <form class="row" id="cred-form">
           <label class="sr-only" for="c-user">Navidrome-Benutzer</label>
           <input id="c-user" name="username" placeholder="Navidrome-Benutzer" autocomplete="off"
                  spellcheck="false" required>
           <label class="sr-only" for="c-pass">Passwort</label>
           <input id="c-pass" name="password" type="password" placeholder="Passwort"
                  autocomplete="off" required>
           <button class="btn btn-primary" type="submit">Verbinden</button>
         </form>
         <p class="muted tiny mt-2">Das Passwort wird nicht gespeichert —
           daraus entsteht einmalig ein Subsonic-Token, und nur das liegt in der Datenbank.</p>`;

    const deemix = arl.configured
      ? `<p>ARL hinterlegt <span class="mono faint">${esc(arl.hint || '')}</span> —
           der Gateway meldet sich damit selbst bei Deemix an.</p>
         <div class="btn-row">
           <button type="button" class="btn" id="arl-copy">${icon('logs')} ARL kopieren</button>
           <button type="button" class="btn btn-ghost" id="arl-clear">ARL entfernen</button>
         </div>
         <div id="arl-out"></div>
         <p class="muted tiny mt-2">Der Wert ist ein Zugang zu deinem ganzen
           Deezer-Konto. Das Abrufen steht im Protokoll.</p>`
      : `<p class="muted">Deemix hält die Deezer-Anmeldung <strong>pro Browser-Sitzung</strong>.
           Dass die Deemix-Oberfläche angemeldet ist, hilft dem Gateway nicht — er ist ein
           eigener Client. Trag den ARL hier ein, dann meldet er sich selbst an.</p>
         <form class="row" id="arl-form">
           <label class="sr-only" for="a-arl">Deezer-ARL</label>
           <input class="grow" id="a-arl" name="arl" type="password" spellcheck="false"
                  placeholder="ARL (nur Hex-Zeichen)" autocomplete="off" required>
           <button class="btn btn-primary" type="submit">Anmelden</button>
         </form>`;

    return `
      ${bereit}

      <div class="card"><h3>Navidrome-Zugang</h3>${zugang}</div>
      <div class="card"><h3>Deemix-Anmeldung (Deezer-ARL)</h3>${deemix}</div>

      <div class="card">
        <h3>Zugriffe von Musik-Clients</h3>
        <p class="muted small">Die letzten Anfragen, die tatsächlich hier ankommen. Bleibt die
          Liste leer, während du im Client suchst, zeigt der Client nicht auf den Gateway.</p>
        <button type="button" class="btn" id="act-load">Aktualisieren</button>
        <div id="act-list" class="list mt-3"></div>
      </div>

      <div class="card">
        <h3>Was ein Musik-Client sieht</h3>
        <p class="muted small">Fragt den eigenen Subsonic-Endpunkt genauso ab wie Substreamer.
          <strong>Navidromes eigene Weboberfläche kann ergänzte Titel nie zeigen</strong> —
          sie läuft am Gateway vorbei.</p>
        <button type="button" class="btn" id="ct-run">Test ausführen</button>
        <div id="ct-out" class="mt-3"></div>
      </div>

      <div class="card card-flush">
        <h3>Startprüfung</h3>
        <div class="list">${pre.checks.map((c) => `
          <div class="item">
            <div class="item-main">
              <div class="item-title">${esc(c.name)}</div>
              <div class="item-sub">${esc(c.detail)}</div>
            </div>
            <div class="item-side">
              <span class="pill ${STUFE[c.status][0]}">${STUFE[c.status][1]}</span>
            </div>
          </div>`).join('')}</div>
      </div>

      <div class="split">
        <div class="card card-flush"><h3>Werkzeuge</h3><div class="list">
          ${Object.entries(d.tools).map(([n, gut]) => `
            <div class="item"><div class="item-main"><div class="item-title mono">${esc(n)}</div></div>
            <div class="item-side"><span class="pill ${gut ? 'pill-ok' : 'pill-err'}">${
              gut ? 'ok' : 'fehlt'}</span></div></div>`).join('')}
        </div></div>
        <div class="card card-flush"><h3>Pfade</h3><div class="list">
          ${Object.entries(d.paths).map(([n, i]) => `
            <div class="item"><div class="item-main">
              <div class="item-title">${esc(n)}</div>
              <div class="item-sub mono break">${esc(i.path)}</div>
            </div>
            <div class="item-side"><span class="pill ${i.exists ? 'pill-ok' : 'pill-err'}">${
              i.exists ? 'ok' : 'fehlt'}</span></div></div>`).join('')}
        </div></div>
      </div>

      <div class="card">
        <h3>Deemix-Endpunkte</h3>
        <p class="muted small">Der Gateway merkt sich den ersten Endpunkt, der eine Anfrage
          annimmt. Antwortet keiner, ist meist der ARL abgelaufen.</p>
        <div class="table-wrap"><table>
          <thead><tr><th>Pfad</th><th>Status</th><th>Antwort</th></tr></thead>
          <tbody>${d.deemix.endpoints.map((r) => `<tr>
            <td class="mono">${esc(r.path)}</td>
            <td class="num">${r.status ?? '—'}</td>
            <td class="mono tiny break">${esc((r.preview || r.error || '').slice(0, 80))}</td>
          </tr>`).join('')}</tbody>
        </table></div>
      </div>`;
  }

  await lade();

  wurzel.addEventListener('submit', async (e) => {
    const cred = e.target.closest('#cred-form');
    const arl = e.target.closest('#arl-form');
    if (!cred && !arl) return;
    e.preventDefault();
    const form = cred || arl;
    const knopf = form.querySelector('button');
    knopf.disabled = true;
    const daten = new FormData(form);
    try {
      if (cred) {
        await post('/api/navidrome/credentials',
                   { username: daten.get('username'), password: daten.get('password') });
        ok('Mit Navidrome verbunden');
      } else {
        const info = await post('/api/deemix/arl', { arl: daten.get('arl') });
        ok(`Bei Deemix angemeldet als ${info.user || 'unbekannt'}`);
      }
      await lade();
    } catch (exc) { fail(exc.message); knopf.disabled = false; }
  });

  wurzel.addEventListener('click', async (e) => {
    if (e.target.closest('#d-reload')) return lade();

    const credWeg = e.target.closest('#cred-clear');
    const arlWeg = e.target.closest('#arl-clear');
    if (credWeg || arlWeg) {
      try {
        await del(credWeg ? '/api/navidrome/credentials' : '/api/deemix/arl');
        ok('Entfernt');
        await lade();
      } catch (exc) { fail(exc.message); }
      return;
    }

    if (e.target.closest('#arl-copy')) {
      try {
        const { arl } = await post('/api/deemix/arl/reveal');
        // Die Zwischenablage kann verweigert werden — dann wird der Wert
        // angezeigt, statt so zu tun, als sei etwas passiert.
        try {
          await navigator.clipboard.writeText(arl);
          ok('ARL in der Zwischenablage');
          $('#arl-out').innerHTML = '';
        } catch {
          $('#arl-out').innerHTML = `<div class="notice mt-3">${icon('warn')}<div>
            Der Browser hat die Zwischenablage verweigert. Hier ist der Wert
            zum Markieren:<code class="mono break secret">${esc(arl)}</code></div></div>`;
        }
      } catch (exc) { fail(exc.message); }
      return;
    }

    if (e.target.closest('#act-load')) {
      const el = $('#act-list');
      el.innerHTML = skeleton(3);
      try {
        const { requests } = await get('/api/client-activity');
        el.innerHTML = requests.length
          ? requests.map((r) => `
              <div class="item">
                <div class="item-main">
                  <div class="item-title">${esc(r.endpoint)}${
                    r.query ? ` — „${esc(r.query)}“` : ''}</div>
                  <div class="item-sub"><time class="mono">${esc(r.ts)}</time> · ${esc(r.detail || '')}</div>
                </div>
                <div class="item-side">
                  <span class="pill">${esc(r.client)}</span>
                  <span class="faint tiny">${esc(r.user)}</span>
                </div>
              </div>`).join('')
          : `<div class="empty"><p>Noch kein Musik-Client hier angekommen. Such einmal im
               Client und lade dann neu — bleibt es leer, zeigt er auf den falschen Port.</p></div>`;
      } catch (exc) { el.innerHTML = failure('Nicht abrufbar.', exc.message); }
      return;
    }

    if (e.target.closest('#ct-run')) {
      const el = $('#ct-out');
      el.innerHTML = '<p class="muted small">Frage den eigenen Subsonic-Endpunkt ab…</p>';
      try {
        const r = await get('/api/client-test?q=Mark%20Forster');
        el.innerHTML = r.virtual > 0
          ? `<div class="notice">${icon('info')}<div>Der Gateway liefert
               <strong>${r.local} lokale</strong> und <strong>${r.virtual} noch nicht
               geladene</strong> Titel für „${esc(r.query)}“. Ein Musik-Client auf Port 8080
               sieht genau das.${r.beispiele.length
                 ? ` Zum Beispiel: ${esc(r.beispiele.join(', '))}` : ''}</div></div>`
          : `<div class="notice notice-warn">${icon('warn')}<div>Der Gateway liefert keine
               ergänzten Titel. Prüfe oben, ob der Katalog erreichbar ist.</div></div>`;
      } catch (exc) {
        el.innerHTML = `<div class="notice notice-err">${icon('warn')}<div>${esc(exc.message)}</div></div>`;
      }
    }
  });
}
