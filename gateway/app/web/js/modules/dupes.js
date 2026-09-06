// Duplikate: Vorschläge prüfen, Behalten-Auswahl setzen, in Quarantäne legen.

import { get, post } from '../core/api.js';
import { $, $$, esc, bytes, num, tile, icon, empty, failure, skeleton } from '../core/dom.js';
import { werkzeugleiste, anwenden as filtern } from '../core/toolbar.js';
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
    <div id="d-leiste"></div>
    <div id="d-list">${skeleton(3)}</div>`;

  let gruppen = [];

  const leiste = werkzeugleiste({
    id: 'dupes',
    standard: { q: '', art: 'alle', sort: 'wasted', richtung: 'ab', pfade: true, alleWaehlen: false },
    suche: { platzhalter: 'Nach Pfad, Titel oder Interpret filtern…', label: 'Duplikate filtern' },
    filter: [{ name: 'art', label: 'Nach Art filtern', werte: [
      ['alle', 'Alle Arten'], ['bytes', 'Gleiche Bytes'],
      ['audio', 'Gleiche Musik'], ['acoustic', 'Gleiche Aufnahme'],
    ] }],
    sortierung: [['wasted', 'Rückgewinnbar'], ['files', 'Dateien'], ['id', 'Nummer']],
    zusatz: [
      { name: 'pfade', art: 'schalter', label: 'Vollständige Pfade zeigen',
        hinweis: 'Aus­geschaltet erscheint nur der Dateiname' },
    ],
    beiAenderung: () => zeichne(),
  });
  $('#d-leiste').replaceWith(leiste.el);

  // Alle sichtbaren Gruppen auf einmal auswählen — bei dreißig Gruppen ist
  // jede einzeln anzuklicken keine Bedienung mehr.
  const alleKnopf = document.createElement('button');
  alleKnopf.type = 'button';
  alleKnopf.className = 'btn btn-sm';
  alleKnopf.textContent = 'Alle sichtbaren wählen';
  alleKnopf.addEventListener('click', () => {
    const kaesten = $$('#d-list [data-select]');
    const allesAn = kaesten.every((k) => k.checked);
    for (const k of kaesten) {
      k.checked = !allesAn;
      const id = Number(k.dataset.select);
      if (k.checked) ausgewaehlt.add(id); else ausgewaehlt.delete(id);
    }
    alleKnopf.textContent = allesAn ? 'Alle sichtbaren wählen' : 'Auswahl aufheben';
    knopfStand();
  });
  leiste.el.querySelector('.toolbar-right').prepend(alleKnopf);

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

      if (!$('#d-summary')) return;
      $('#d-summary').innerHTML = [
        tile('Gruppen', num(s.groups)),
        tile('Betroffene Dateien', num(s.files)),
        tile('Rückgewinnbar', bytes(s.wasted), '', s.wasted ? 'warn' : 'ok'),
        ...(s.by_kind || []).map((k) => tile(k.kind, num(k.n), bytes(k.wasted))),
      ].join('');

      gruppen = d.groups || [];
      zeichne();
    } catch (exc) {
      if ($('#d-list')) $('#d-list').innerHTML = failure('Duplikate nicht abrufbar.', exc.message);
    }
  }

  function zeichne() {
    if (!$('#d-list')) return;
    const w = leiste.werte();
    let sichtbar = w.art === 'alle' ? gruppen : gruppen.filter((g) => g.kind === w.art);

    // Der Suchbegriff trifft die Dateien in einer Gruppe, nicht die Gruppe
    // selbst — sonst könnte man nach nichts Sinnvollem suchen.
    const suche = (w.q || '').toLowerCase();
    if (suche) {
      sichtbar = sichtbar.filter((g) => (g.members || []).some((m) =>
        [m.path, m.title, m.artist].some((f) => String(f || '').toLowerCase().includes(suche))));
    }
    sichtbar = filtern(sichtbar, w, {
      sortierer: { wasted: (g) => g.wasted || 0, files: (g) => g.files || 0, id: (g) => g.id },
    });
    leiste.setzeZaehler(sichtbar.length === gruppen.length
      ? `${gruppen.length}` : `${sichtbar.length} von ${gruppen.length}`);

    $('#d-list').innerHTML = sichtbar.length
        ? sichtbar.map((g) => `
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
                    <div class="item-title mono tiny break">${esc(
                      w.pfade ? m.path : String(m.path || '').split(/[\/]/).pop())}</div>
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
        : empty(gruppen.length ? 'Keine Gruppe passt zum Filter.' : 'Keine offenen Duplikate.',
                gruppen.length
                  ? 'Setze Suche und Art in der Leiste oben zurück.'
                  : 'Starte oben eine Suche. Der Index muss dafür aufgebaut sein.');
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
    const uebernehmen = e.target.closest('#d-apply');

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
    if (uebernehmen && ausgewaehlt.size) {
      if (!confirm(`${ausgewaehlt.size} Gruppe(n) bereinigen?\n\n`
        + 'Die nicht behaltenen Dateien werden in den Quarantäne-Ordner verschoben, nicht gelöscht.')) return;
      uebernehmen.disabled = true;
      try {
        const r = await post('/api/library/dupes/apply', { groups: [...ausgewaehlt] });
        ok(`Eingeplant. Quarantäne: ${r.quarantine}`);
        setTimeout(lade, 1500);
      } catch (exc) { fail(exc.message); uebernehmen.disabled = false; }
    }
  });
}
