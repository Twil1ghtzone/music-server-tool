// Duplikate: Vorschläge prüfen, Behalten-Auswahl setzen, in Quarantäne legen.

import { get, post } from '../core/api.js';
import { $, $$, esc, bytes, num, tile, icon, empty, failure, skeleton } from '../core/dom.js';
import { ok, fail } from '../core/toast.js';

export const meta = { id: 'dupes', titel: 'Duplikate' };

export async function mount(wurzel) {
  const ausgewaehlt = new Set();

  wurzel.innerHTML = `
    <div class="page-head">
      <div>
        <h2>Duplikate</h2>
        <p class="lede">Drei Stufen: gleiche Bytes, gleiche Musik trotz anderer Tags, gleiche Aufnahme.</p>
      </div>
      <div class="page-actions">
        <button type="button" class="btn" data-do="find">Suchen (Hash)</button>
        <button type="button" class="btn" data-do="find-acoustic">Suchen (inkl. akustisch)</button>
        <button type="button" class="btn btn-danger" id="d-apply" disabled>Auswahl in Quarantäne</button>
      </div>
    </div>

    <div class="notice">${icon('info')}<div>
      Es wird nichts gelöscht. Nicht behaltene Dateien wandern in den
      Quarantäne-Ordner und lassen sich von dort zurückholen.
    </div></div>

    <div class="tiles" id="d-summary"></div>
    <div id="d-list">${skeleton(3)}</div>`;

  function knopfStand() {
    const k = $('#d-apply');
    k.disabled = ausgewaehlt.size === 0;
    k.textContent = ausgewaehlt.size
      ? `${ausgewaehlt.size} Gruppe(n) in Quarantäne` : 'Auswahl in Quarantäne';
  }

  async function lade() {
    try {
      const d = await get('/api/library/dupes?state=open&limit=200');
      const s = d.summary || {};
      ausgewaehlt.clear();
      knopfStand();

      $('#d-summary').innerHTML = [
        tile('Gruppen', num(s.groups)),
        tile('Betroffene Dateien', num(s.files)),
        tile('Rückgewinnbar', bytes(s.wasted), '', s.wasted ? 'warn' : 'ok'),
        ...(s.by_kind || []).map((k) => tile(k.kind, num(k.n), bytes(k.wasted))),
      ].join('');

      const gruppen = d.groups || [];
      $('#d-list').innerHTML = gruppen.length
        ? gruppen.map((g) => `
            <div class="card card-flush" data-group="${g.id}">
              <div class="item item-head">
                <label class="inline">
                  <input type="checkbox" data-select="${g.id}">
                  <span class="sr-only">Gruppe ${g.id} auswählen</span>
                </label>
                <div class="item-main">
                  <div class="item-title">
                    <span class="pill">${esc(g.kind)}</span>
                    ${num(g.files)} Dateien · ${bytes(g.wasted)} rückgewinnbar
                  </div>
                </div>
                <div class="item-side">
                  <button type="button" class="btn btn-sm btn-ghost" data-ignore="${g.id}">Ignorieren</button>
                </div>
              </div>
              ${(g.members || []).map((m) => `
                <div class="item ${m.id === g.keeper_id ? 'item-keeper' : ''}">
                  <label class="inline">
                    <input type="radio" name="keeper-${g.id}" value="${m.id}"
                           data-keeper="${g.id}" ${m.id === g.keeper_id ? 'checked' : ''}>
                    <span class="sr-only">Diese Datei behalten</span>
                  </label>
                  <div class="item-main">
                    <div class="item-title mono tiny break">${esc(m.path)}</div>
                    <div class="item-sub">${esc(m.artist || '')} — ${esc(m.title || '')}</div>
                  </div>
                  <div class="item-side">
                    <span class="pill">${esc(m.ext || '')}</span>
                    ${m.bitrate ? `<span class="faint tiny num">${Math.round(m.bitrate / 1000)}&nbsp;kbit/s</span>` : ''}
                    <span class="faint tiny">${bytes(m.size)}</span>
                    ${m.similarity != null
                      ? `<span class="pill pill-busy num">${Math.round(m.similarity * 100)}&nbsp;%</span>` : ''}
                  </div>
                </div>`).join('')}
            </div>`).join('')
        : empty('Keine offenen Duplikate.',
                'Starte oben eine Suche. Der Index muss dafür aufgebaut sein.');
    } catch (exc) {
      $('#d-list').innerHTML = failure('Duplikate nicht abrufbar.', exc.message);
    }
  }
  await lade();

  wurzel.addEventListener('change', async (e) => {
    const auswahl = e.target.closest('[data-select]');
    if (auswahl) {
      const id = Number(auswahl.dataset.select);
      auswahl.checked ? ausgewaehlt.add(id) : ausgewaehlt.delete(id);
      knopfStand();
      return;
    }
    const keeper = e.target.closest('[data-keeper]');
    if (!keeper) return;
    try {
      await post(`/api/library/dupes/${keeper.dataset.keeper}/keeper`,
                 { media_file_id: Number(keeper.value) });
      ok('Behalten-Auswahl gespeichert');
    } catch (exc) { fail(exc.message); await lade(); }
  });

  wurzel.addEventListener('click', async (e) => {
    const ignorieren = e.target.closest('[data-ignore]');
    const tun = e.target.closest('[data-do]');
    const anwenden = e.target.closest('#d-apply');

    if (ignorieren) {
      try {
        await post(`/api/library/dupes/${ignorieren.dataset.ignore}/ignore`);
        ignorieren.closest('[data-group]')?.remove();
      } catch (exc) { fail(exc.message); }
      return;
    }
    if (tun) {
      tun.disabled = true;
      try {
        await post(`/api/library/dupes/find${tun.dataset.do === 'find-acoustic' ? '?acoustic=true' : ''}`);
        ok('Suche eingeplant — das Ergebnis erscheint, sobald der Job durch ist.');
      } catch (exc) { fail(exc.message); } finally { tun.disabled = false; }
      return;
    }
    if (anwenden && ausgewaehlt.size) {
      if (!confirm(`${ausgewaehlt.size} Gruppe(n) bereinigen?\n\n`
        + 'Die nicht behaltenen Dateien werden in den Quarantäne-Ordner verschoben, nicht gelöscht.')) return;
      anwenden.disabled = true;
      try {
        const r = await post('/api/library/dupes/apply', { groups: [...ausgewaehlt] });
        ok(`Eingeplant. Quarantäne: ${r.quarantine}`);
        setTimeout(lade, 1500);
      } catch (exc) { fail(exc.message); anwenden.disabled = false; }
    }
  });
}
