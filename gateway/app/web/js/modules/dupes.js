// Duplikate: finden, anhören, entscheiden, in Quarantäne legen.
//
// Der Scanner schlägt vor, der Mensch entscheidet — außer in den Fällen, die
// keine zwei Lesarten haben ("Song (2).flac" neben "Song.flac"). Die trägt
// der Server als „eindeutig" markiert aus, und die lassen sich hier mit
// einem Griff über alle Seiten hinweg auswählen.
//
// Warum Cover und Abspielknopf: zwei Dateien mit gleicher Dauer und gleichem
// Format sind auf dem Papier nicht zu unterscheiden. Man muss hineinhören
// können, sonst ist jede Entscheidung geraten.

import { get, post, q } from '../core/api.js';
import { $, $$, esc, bytes, num, tile, icon, empty, failure, skeleton, relativeTime } from '../core/dom.js';
import { werkzeugleiste } from '../core/toolbar.js';
import * as player from '../core/player.js';
import * as bus from '../core/bus.js';
import * as prefs from '../core/prefs.js';
import { ok, fail } from '../core/toast.js';

export const meta = { id: 'dupes', titel: 'Duplikate' };

const ART = {
  exact: ['Identisch', 'Byte für Byte dieselbe Datei'],
  audio: ['Gleiche Musik', 'Gleicher Ton, andere Tags oder Cover'],
  acoustic: ['Gleiche Aufnahme', 'Dieselbe Aufnahme in anderer Kodierung'],
};

const SCHUTZ = {
  playlist: 'In einer Playlist',
  favorit: 'Favorit',
  bewertet: 'Bewertet',
};

export async function mount(wurzel, ctx) {
  const ausgewaehlt = new Set();
  let seite = { groups: [], total: 0, offset: 0, limit: 25 };
  let zusammenfassung = {};
  let laeuftScan = false;
  let darfAnwenden = true;

  wurzel.innerHTML = `
    <div class="page-head">
      <div>
        <h2>Duplikate</h2>
        <p class="lede">Drei Stufen: Byte für Byte identisch, gleicher Ton trotz anderer
          Tags, gleiche Aufnahme in anderer Kodierung. Jeder Lauf holt zuerst
          Playlists, Favoriten und Bewertungen aus Navidrome — was dort hängt,
          wird nie zum Entfernen ausgewählt. Es wird nichts gelöscht:
          Ausgewähltes wandert in die Quarantäne und bleibt dort eine Frist lang liegen.</p>
      </div>
      <div class="page-actions" id="d-aktionen"></div>
    </div>

    <div id="d-sperre"></div>
    <div id="d-scan"></div>
    <div class="tiles" id="d-summary">${skeleton(1)}</div>
    <div id="d-leiste"></div>
    <div id="d-list">${skeleton(3)}</div>
    <div id="d-seiten"></div>
    <div id="d-quarantaene"></div>`;

  // --------------------------------------------------------------- Kopfzeile
  // Kein eigener Abgleich-Knopf: jeder Lauf holt zuerst Playlists, Favoriten
  // und Bewertungen aus Navidrome. Ein Vorschlag auf veralteten Markierungen
  // wäre gefährlich, also ist das kein Extraschritt, sondern Teil der Suche.
  $('#d-aktionen').innerHTML = `
    <button type="button" class="btn" data-do="find">${icon('search')} Schnell suchen</button>
    <button type="button" class="btn btn-primary" data-do="find-acoustic">${icon('bolt')} Gründlich suchen</button>`;

  // ------------------------------------------------------------ Werkzeuge
  const leiste = werkzeugleiste({
    id: 'dupes',
    standard: { q: '', art: 'alle', nurAuto: false, proSeite: 25, pfade: false },
    suche: { platzhalter: 'Nach Titel, Interpret oder Pfad filtern…', label: 'Duplikate filtern' },
    filter: [{ name: 'art', label: 'Nach Art filtern', werte: [
      ['alle', 'Alle Arten'], ['exact', 'Identisch'],
      ['audio', 'Gleiche Musik'], ['acoustic', 'Gleiche Aufnahme'],
    ] }],
    zusatz: [
      { name: 'nurAuto', art: 'schalter', label: 'Nur eindeutige Fälle',
        hinweis: 'Blendet alles aus, was eine Entscheidung braucht' },
      { name: 'pfade', art: 'schalter', label: 'Vollständige Pfade zeigen',
        hinweis: 'Ausgeschaltet erscheint nur der Ordner' },
      { name: 'proSeite', art: 'auswahl', zahl: true, label: 'Gruppen je Seite',
        werte: [[10, '10'], [25, '25'], [50, '50'], [100, '100']] },
    ],
    beiAenderung: (w, geaendert) => {
      if (geaendert === 'pfade' || geaendert === 'q') zeichne();
      else lade(0);
    },
  });
  $('#d-leiste').replaceWith(leiste.el);

  const alleKnopf = document.createElement('button');
  alleKnopf.type = 'button';
  alleKnopf.className = 'btn btn-sm';
  alleKnopf.addEventListener('click', waehleEindeutige);

  const anwendenKnopf = document.createElement('button');
  anwendenKnopf.type = 'button';
  anwendenKnopf.className = 'btn btn-sm btn-danger';
  anwendenKnopf.disabled = true;
  anwendenKnopf.addEventListener('click', anwenden);

  const rechts = leiste.el.querySelector('.toolbar-right');
  rechts.append(alleKnopf, anwendenKnopf);

  function knopfStand() {
    anwendenKnopf.disabled = ausgewaehlt.size === 0 || !darfAnwenden;
    anwendenKnopf.textContent = ausgewaehlt.size
      ? `${ausgewaehlt.size} in Quarantäne` : 'Nichts ausgewählt';
    const auto = zusammenfassung.auto || 0;
    alleKnopf.textContent = auto ? `${num(auto)} eindeutige wählen` : 'Keine eindeutigen';
    alleKnopf.disabled = !auto;
  }

  // ------------------------------------------------------- Scan-Anzeige
  // Ein Duplikatlauf über eine große Bibliothek dauert. Ohne Anzeige sieht
  // es aus, als sei nichts passiert — und man klickt ein zweites Mal.
  function zeichneScan(job) {
    const el = $('#d-scan');
    if (!el) return;
    if (!job) { el.innerHTML = ''; laeuftScan = false; return; }
    const neu = !laeuftScan;
    laeuftScan = true;
    const prozent = Math.round((job.progress || 0) * 100);

    if (neu) {
      el.innerHTML = `
        <div class="scan">
          <div class="scan-wave" aria-hidden="true">
            ${Array.from({ length: 32 }, (_, i) => `<span data-i="${i}"></span>`).join('')}
          </div>
          <div class="scan-body">
            <div class="scan-title">Suche läuft</div>
            <div class="scan-detail"></div>
          </div>
          <div class="scan-percent num"></div>
        </div>`;
      // Höhe und Verzögerung je Balken über die CSSOM: ein style-Attribut im
      // Markup würde die Content-Security-Policy verwerfen.
      for (const balken of el.querySelectorAll('.scan-wave span')) {
        const i = Number(balken.dataset.i);
        balken.style.animationDelay = `${i * 55}ms`;
        balken.style.height = `${25 + Math.abs(Math.sin(i * 0.8)) * 65}%`;
      }
    }
    const detail = el.querySelector('.scan-detail');
    if (detail) detail.textContent = job.detail || 'Der Worker hat übernommen';
    const anteil = el.querySelector('.scan-percent');
    if (anteil) anteil.textContent = `${prozent} %`;
  }

  // --------------------------------------------------------------- Laden
  async function lade(offset = seite.offset) {
    const el = $('#d-list');
    if (!el) return;
    el.innerHTML = skeleton(3);
    const w = leiste.werte();
    try {
      const d = await get(`/api/library/dupes${q({
        state: 'open', kind: w.art, auto: w.nurAuto || undefined,
        limit: w.proSeite, offset,
      })}`);
      if (!$('#d-list')) return;
      seite = d;
      zusammenfassung = d.summary || {};
      zeichneSperre(d.apply_allowed !== false);
      zeichneKacheln();
      zeichne();
      zeichneSeiten();
      await ladeQuarantaene();
    } catch (exc) {
      if ($('#d-list')) $('#d-list').innerHTML = failure('Duplikate nicht abrufbar.', exc.message);
    }
  }

  // Der Schutzschalter steht im Auslieferungszustand auf "aus": suchen und
  // anzeigen ja, Dateien anfassen nein. Das ist richtig so - aber es muss
  // hier stehen, sonst drueckt man auf "in Quarantäne" und bekommt eine
  // Fehlermeldung, die aussieht wie ein Defekt.
  function zeichneSperre(erlaubt) {
    darfAnwenden = erlaubt;
    const el = $('#d-sperre');
    if (!el) return;
    el.innerHTML = erlaubt ? '' : `
      <div class="notice notice-warn">${icon('warn')}<div>
        <strong>Bereinigen ist gesperrt.</strong> Suchen und Ansehen geht, das
        Verschieben in die Quarantäne nicht. Das ist der Auslieferungszustand —
        so kann ein erster Lauf nichts anfassen, bevor du die Vorschläge gesehen
        hast. Zum Freischalten in der <code class="mono">docker-compose.yml</code>
        <code class="mono break">GATEWAY_ALLOW_DEDUPE_APPLY: "true"</code> setzen
        und den Stack neu starten.
      </div></div>`;
    anwendenKnopf.title = erlaubt ? '' : 'Gesperrt — siehe Hinweis oben';
  }

  function zeichneKacheln() {
    const el = $('#d-summary');
    if (!el) return;
    const s = zusammenfassung;
    el.innerHTML = [
      tile('Gruppen', num(s.groups), `${num(s.files)} Dateien`),
      tile('Rückgewinnbar', bytes(s.wasted), '', s.wasted ? 'warn' : 'ok'),
      tile('Eindeutig', num(s.auto), bytes(s.auto_wasted), s.auto ? 'ok' : ''),
      tile('In Quarantäne', num(s.quarantine), bytes(s.quarantine_bytes)),
    ].join('');
  }

  // -------------------------------------------------------------- Zeichnen
  function mitglied(m, gruppe) {
    const istKeeper = m.id === gruppe.keeper_id;
    const spielt = String(player.laeuft()) === `lib-${m.id}`;
    const schutz = m.protected ? (SCHUTZ[m.protect_why] || 'Geschützt') : null;
    const w = leiste.werte();

    return `
      <div class="dupe-row ${istKeeper ? 'is-keeper' : ''} ${spielt ? 'is-playing' : ''}">
        <label class="inline dupe-pick">
          <input type="radio" name="keeper-${gruppe.id}" value="${m.id}"
                 data-keeper="${gruppe.id}" ${istKeeper ? 'checked' : ''}>
          <span class="sr-only">„${esc(m.name)}“ behalten</span>
        </label>

        <div class="dupe-art">
          ${m.has_cover
            ? `<img src="/api/library/files/${m.id}/cover?s=96" alt=""
                    width="48" height="48" loading="lazy" decoding="async">`
            : `<span class="dupe-art-leer">${icon('note')}</span>`}
          <button type="button" class="dupe-play" data-play="${m.id}"
                  aria-label="${spielt ? 'Anhalten' : `„${esc(m.title || m.name)}“ anhören`}"
                  title="${spielt ? 'Anhalten' : 'Anhören'}">${icon(spielt ? 'pause' : 'play')}</button>
        </div>

        <div class="dupe-main">
          <div class="dupe-title">${esc(m.title || m.name)}${
            m.copy_no ? ` <span class="pill pill-warn">Kopie ${m.copy_no}</span>` : ''}</div>
          <div class="dupe-sub mono">${esc(w.pfade ? m.path : m.folder)}</div>
        </div>

        <div class="dupe-fakten">
          <span class="pill">${esc((m.ext || '').replace('.', '').toUpperCase())}</span>
          ${m.bitrate ? `<span class="faint tiny num">${Math.round(m.bitrate / 1000)}&nbsp;kbit/s</span>` : ''}
          <span class="faint tiny num">${bytes(m.size)}</span>
          ${m.similarity != null
            ? `<span class="pill pill-busy num" title="Akustische Übereinstimmung"
                >${Math.round(m.similarity * 100)}&nbsp;%</span>` : ''}
        </div>

        <div class="dupe-status">
          ${schutz ? `<span class="pill pill-ok"
              title="Navidrome hängt Nutzerdaten daran — wird nie entfernt"
              >${icon('check')} ${esc(schutz)}</span>` : ''}
          ${istKeeper
            ? '<span class="pill pill-ok">bleibt</span>'
            : '<span class="pill pill-err">wird entfernt</span>'}
        </div>
      </div>`;
  }

  function zeichne() {
    const el = $('#d-list');
    if (!el) return;
    const w = leiste.werte();

    // Der Suchbegriff trifft die Dateien einer Gruppe, nicht die Gruppe
    // selbst — nach einer Gruppennummer würde niemand suchen.
    const suche = (w.q || '').toLowerCase();
    const sichtbar = suche
      ? seite.groups.filter((g) => (g.members || []).some((m) =>
          [m.path, m.title, m.artist, m.album].some(
            (f) => String(f || '').toLowerCase().includes(suche))))
      : seite.groups;

    leiste.setzeZaehler(seite.total
      ? `${seite.offset + 1}–${Math.min(seite.offset + sichtbar.length, seite.total)} von ${num(seite.total)}`
      : '');

    if (!seite.total) {
      el.innerHTML = empty('Keine offenen Duplikate.',
        laeuftScan ? 'Der Lauf ist noch unterwegs.'
                   : 'Starte oben eine Suche. Der Index muss dafür aufgebaut sein.');
      knopfStand();
      return;
    }
    if (!sichtbar.length) {
      el.innerHTML = empty('Auf dieser Seite passt nichts zur Suche.',
        'Der Filter wirkt nur auf die geladene Seite — blättere weiter oder leere das Feld.');
      knopfStand();
      return;
    }

    el.innerHTML = sichtbar.map((g) => {
      const [name, erklaerung] = ART[g.kind] || [g.kind, ''];
      return `
      <div class="card card-flush dupe-group ${ausgewaehlt.has(g.id) ? 'is-picked' : ''}"
           data-group="${g.id}">
        <div class="dupe-head">
          <label class="inline">
            <input type="checkbox" data-select="${g.id}" ${ausgewaehlt.has(g.id) ? 'checked' : ''}>
            <span class="sr-only">Gruppe ${g.id} zum Bereinigen auswählen</span>
          </label>
          <div class="dupe-head-main">
            <div class="dupe-head-title">
              <span class="pill ${g.kind === 'acoustic' ? 'pill-busy' : ''}">${esc(name)}</span>
              ${g.auto_ok
                ? `<span class="pill pill-ok" title="${esc(g.auto_why || '')}">${icon('check')} eindeutig</span>`
                : `<span class="pill" title="${esc(g.auto_why || '')}">Entscheidung nötig</span>`}
              <strong>${num(g.files)} Dateien</strong>
              <span class="faint">${bytes(g.wasted)} rückgewinnbar</span>
            </div>
            <div class="dupe-head-sub">${esc(g.auto_why || erklaerung)}</div>
          </div>
          <button type="button" class="btn btn-sm btn-ghost" data-ignore="${g.id}">Ignorieren</button>
        </div>
        <div class="dupe-rows">${(g.members || []).map((m) => mitglied(m, g)).join('')}</div>
      </div>`;
    }).join('');
    knopfStand();
  }

  // --------------------------------------------------------------- Seiten
  function zeichneSeiten() {
    const el = $('#d-seiten');
    if (!el) return;
    const proSeite = seite.limit || 25;
    const seitenZahl = Math.ceil(seite.total / proSeite);
    if (seitenZahl <= 1) { el.innerHTML = ''; return; }

    const aktuell = Math.floor(seite.offset / proSeite);
    // Höchstens sieben Knöpfe: die aktuelle Seite mit Nachbarn.
    const von = Math.max(0, Math.min(aktuell - 3, seitenZahl - 7));
    const bis = Math.min(seitenZahl, von + 7);

    el.innerHTML = `
      <nav class="pager" aria-label="Seiten">
        <button type="button" class="btn btn-sm" data-seite="${aktuell - 1}"
                ${aktuell === 0 ? 'disabled' : ''}>Zurück</button>
        ${von > 0 ? '<span class="pager-luecke">…</span>' : ''}
        ${Array.from({ length: bis - von }, (_, i) => von + i).map((n) => `
          <button type="button" class="btn btn-sm ${n === aktuell ? 'btn-primary' : ''}"
                  data-seite="${n}" ${n === aktuell ? 'aria-current="page"' : ''}>${n + 1}</button>`).join('')}
        ${bis < seitenZahl ? '<span class="pager-luecke">…</span>' : ''}
        <button type="button" class="btn btn-sm" data-seite="${aktuell + 1}"
                ${aktuell >= seitenZahl - 1 ? 'disabled' : ''}>Weiter</button>
        <span class="faint tiny">Seite ${aktuell + 1} von ${seitenZahl}</span>
      </nav>`;
  }

  // ----------------------------------------------------------- Quarantäne
  async function ladeQuarantaene() {
    const el = $('#d-quarantaene');
    if (!el) return;
    let d;
    try {
      d = await get('/api/library/quarantine?state=held&limit=50');
    } catch (exc) {
      el.innerHTML = failure('Quarantäne nicht abrufbar.', exc.message);
      return;
    }
    if (!$('#d-quarantaene')) return;

    el.innerHTML = `
      <div class="card">
        <h3>Quarantäne</h3>
        <p class="muted small">Entferntes wird nicht gelöscht, sondern hierher verschoben —
          unter seinem Originalpfad, damit Zurückholen ein Griff ist. Nach Ablauf der
          Frist verschwindet es von selbst.</p>

        <div class="row mt-3">
          <label class="inline" for="q-tage">Frist
            <input id="q-tage" type="number" min="1" max="365" value="${d.days}"
                   class="field-narrow" aria-label="Aufbewahrung in Tagen">
            Tage
          </label>
          <button type="button" class="btn btn-sm" data-do="frist">Frist speichern</button>
          <span class="grow"></span>
          <span class="faint tiny">${num(d.total)} Datei(en) · ${bytes(d.bytes)}</span>
          ${d.total ? '<button type="button" class="btn btn-sm btn-ghost" data-do="purge">Fällige jetzt löschen</button>' : ''}
        </div>

        <div class="list mt-3">${d.items.length ? d.items.map((i) => `
          <div class="item">
            <div class="item-main">
              <div class="item-title">${esc(i.name)}</div>
              <div class="item-sub mono">${esc(i.folder)}</div>
            </div>
            <div class="item-side">
              <span class="faint tiny num">${bytes(i.size)}</span>
              <span class="pill ${i.days_left <= 3 ? 'pill-warn' : ''}"
                    title="Verschoben ${esc(relativeTime(i.moved_at))}"
                >noch ${num(Math.max(0, i.days_left))} Tag(e)</span>
              <button type="button" class="btn btn-sm" data-restore="${i.id}">Zurückholen</button>
            </div>
          </div>`).join('') : empty('Die Quarantäne ist leer.')}</div>
      </div>`;
  }

  // ------------------------------------------------------------- Aktionen
  async function waehleEindeutige() {
    try {
      const { groups, count } = await get('/api/library/dupes/auto');
      for (const id of groups) ausgewaehlt.add(id);
      zeichne();
      ok(`${count} eindeutige Gruppe(n) ausgewählt — auch auf den anderen Seiten.`);
    } catch (exc) { fail(exc.message); }
  }

  async function anwenden() {
    if (!ausgewaehlt.size) return;
    const tage = zusammenfassung.quarantine_days || 21;
    if (prefs.hole('bestaetigen')
        && !confirm(`${ausgewaehlt.size} Gruppe(n) bereinigen?\n\n`
          + 'Die nicht behaltenen Dateien wandern in die Quarantäne und werden dort '
          + `nach ${tage} Tagen endgültig gelöscht. Bis dahin lassen sie sich `
          + 'jederzeit zurückholen.')) return;
    anwendenKnopf.disabled = true;
    try {
      const r = await post('/api/library/dupes/apply', { groups: [...ausgewaehlt] });
      ok(`Eingeplant. Quarantäne: ${r.quarantine}`);
      ausgewaehlt.clear();
      setTimeout(() => lade(0), 1500);
    } catch (exc) { fail(exc.message); knopfStand(); }
  }

  await lade(0);

  // Ein Cover, das Navidrome nicht hat, wuerde als kaputtes Bild stehen
  // bleiben - mit Rahmen und Fragezeichen, mitten in der Liste. Der
  // Rueckfall raeumt es weg und setzt das Notenzeichen an seine Stelle.
  // "error" steigt nicht auf, deshalb in der Erfassungsphase.
  function coverFehlt(e) {
    const bild = e.target;
    if (bild.tagName !== 'IMG' || !bild.closest('.dupe-art')) return;
    bild.replaceWith(Object.assign(document.createElement('span'), {
      className: 'dupe-art-leer', innerHTML: icon('note'),
    }));
  }
  wurzel.addEventListener('error', coverFehlt, true);

  // -------------------------------------------------------- Ereignisse
  wurzel.addEventListener('change', async (e) => {
    const auswahl = e.target.closest('[data-select]');
    if (auswahl) {
      const id = Number(auswahl.dataset.select);
      if (auswahl.checked) ausgewaehlt.add(id); else ausgewaehlt.delete(id);
      auswahl.closest('.dupe-group')?.classList.toggle('is-picked', auswahl.checked);
      knopfStand();
      return;
    }
    const keeper = e.target.closest('[data-keeper]');
    if (!keeper) return;
    try {
      await post(`/api/library/dupes/${keeper.dataset.keeper}/keeper`,
                 { media_file_id: Number(keeper.value) });
      // Die Gruppe neu zeichnen, damit „bleibt" und „wird entfernt" wandern.
      const gruppe = seite.groups.find((g) => g.id === Number(keeper.dataset.keeper));
      if (gruppe) { gruppe.keeper_id = Number(keeper.value); zeichne(); }
      ok('Auswahl gespeichert');
    } catch (exc) { fail(exc.message); await lade(); }
  });

  wurzel.addEventListener('click', async (e) => {
    const abspielen = e.target.closest('[data-play]');
    if (abspielen) {
      const id = abspielen.dataset.play;
      const zeile = abspielen.closest('.dupe-row');
      player.play({
        id: `lib-${id}`,
        src: `/api/library/files/${id}/stream`,
        title: zeile?.querySelector('.dupe-title')?.textContent.trim() || 'Datei',
        artist: zeile?.querySelector('.dupe-sub')?.textContent.trim() || '',
        cover: `/api/library/files/${id}/cover?s=96`,
      });
      return;
    }

    const seitenKnopf = e.target.closest('[data-seite]');
    if (seitenKnopf) {
      await lade(Number(seitenKnopf.dataset.seite) * (seite.limit || 25));
      $('#d-list')?.scrollIntoView({ behavior: 'smooth', block: 'start' });
      return;
    }

    const zurueck = e.target.closest('[data-restore]');
    if (zurueck) {
      zurueck.disabled = true;
      try {
        await post(`/api/library/quarantine/${zurueck.dataset.restore}/restore`);
        ok('Zurückgeholt');
        await ladeQuarantaene();
      } catch (exc) { fail(exc.message); zurueck.disabled = false; }
      return;
    }

    const ignorieren = e.target.closest('[data-ignore]');
    if (ignorieren) {
      try {
        await post(`/api/library/dupes/${ignorieren.dataset.ignore}/ignore`);
        ausgewaehlt.delete(Number(ignorieren.dataset.ignore));
        ignorieren.closest('[data-group]')?.remove();
        knopfStand();
      } catch (exc) { fail(exc.message); }
      return;
    }

    const tun = e.target.closest('[data-do]');
    if (!tun) return;
    const was = tun.dataset.do;
    tun.disabled = true;
    try {
      if (was === 'find' || was === 'find-acoustic') {
        await post(`/api/library/dupes/find${was === 'find-acoustic' ? '?acoustic=true' : ''}`);
        ok(was === 'find-acoustic'
          ? 'Gründliche Suche läuft — erst der Abgleich mit Navidrome, dann auch '
            + 'Aufnahmen in anderer Kodierung.'
          : 'Suche läuft — Playlists, Favoriten und Bewertungen werden dabei geholt.');
      } else if (was === 'frist') {
        const r = await post('/api/library/quarantine/days', { days: Number($('#q-tage').value) });
        ok(`Frist auf ${r.days} Tage gesetzt. Gilt für alles, was ab jetzt entfernt wird.`);
        await lade();
      } else if (was === 'purge') {
        await post('/api/library/quarantine/purge');
        ok('Aufräumen eingeplant — es verschwindet nur, was seine Frist hinter sich hat.');
      }
    } catch (exc) { fail(exc.message); } finally { tun.disabled = false; }
  });

  // Der Lauf meldet sich über den Ereignisstrom. Ist er durch, wird die
  // Liste einmal neu geholt — ohne dass jemand neu laden muss.
  const abState = bus.on('state', (s) => {
    const lauf = (s.active || []).find(
      (j) => ['find_dupes', 'sync_protection', 'apply_dupes'].includes(j.type));
    const liefVorher = laeuftScan;
    zeichneScan(lauf);
    if (liefVorher && !lauf && prefs.hole('autoAktualisieren')) lade();
  });

  const abPlayer = player.onChange(() => {
    for (const el of $$('[data-play]')) {
      const spielt = String(player.laeuft()) === `lib-${el.dataset.play}`;
      el.innerHTML = icon(spielt ? 'pause' : 'play');
      el.closest('.dupe-row')?.classList.toggle('is-playing', spielt);
    }
  });

  return () => { abState(); abPlayer(); player.stop(); };
}
