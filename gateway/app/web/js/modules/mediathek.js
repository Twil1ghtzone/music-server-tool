// Mediathek: die Navidrome-Bibliothek, hier sichtbar.
//
// Die Wahrheit bleibt Navidrome — das hier ist keine zweite Bibliothek,
// sondern ein Fenster in die vorhandene. Ohne verbundenes Konto steht an
// dieser Stelle das Anmeldeformular statt einer leeren Seite.
//
// Jeder Dashboard-Benutzer verbindet sein EIGENES Navidrome-Konto. Playlists,
// Favoriten und Bewertungen gehören einem Menschen — ein gemeinsamer Zugang
// hätte geheißen, dass jeder die Favoriten dessen sieht, der sich zuerst
// verbunden hat, und in dessen Namen markiert.

import { get, post, del, q } from '../core/api.js';
import { $, $$, esc, icon, num, duration, empty, failure, skeleton } from '../core/dom.js';
import { werkzeugleiste } from '../core/toolbar.js';
import * as player from '../core/player.js';
import { ok, fail } from '../core/toast.js';

export const meta = { id: 'mediathek', titel: 'Mediathek' };

const SORTIERUNG = [
  ['newest', 'Zuletzt hinzugefügt'],
  ['alphabeticalByName', 'Album A–Z'],
  ['alphabeticalByArtist', 'Interpret A–Z'],
  ['frequent', 'Oft gehört'],
  ['recent', 'Zuletzt gehört'],
  ['starred', 'Favoriten'],
  ['random', 'Zufall'],
];

const cover = (id, groesse = 300) =>
  id ? `/api/mediathek/cover/${encodeURIComponent(id)}?s=${groesse}` : null;

export async function mount(wurzel, ctx) {
  let stand = {};
  let offset = 0;
  let mehr = false;

  wurzel.innerHTML = `
    <div class="page-head">
      <div>
        <h2>Mediathek</h2>
        <p class="lede">Deine Sammlung, wie Navidrome sie kennt. Was hier steht, steht
          auch in jedem Musik-Client — geändert wird es dort, nicht hier.</p>
      </div>
      <div class="page-actions" id="m-aktionen"></div>
    </div>
    <div id="m-zugang"></div>
    <div id="m-leiste"></div>
    <div id="m-inhalt">${skeleton(4)}</div>
    <div id="m-mehr"></div>`;

  // ------------------------------------------------------------ Zugang
  function zeigeAnmeldung(info) {
    $('#m-leiste').innerHTML = '';
    $('#m-inhalt').innerHTML = '';
    $('#m-mehr').innerHTML = '';
    $('#m-aktionen').innerHTML = '';
    $('#m-zugang').innerHTML = `
      <div class="card">
        <h3>Dein Navidrome-Konto verbinden</h3>
        <p class="muted small">Melde dich einmal mit deinem eigenen Navidrome-Zugang an.
          Daraus entsteht ein Subsonic-Token — das Passwort selbst wird nicht gespeichert.
          Der Zugang gilt nur für dich: du siehst deine Playlists und deine Favoriten,
          nicht die eines anderen.
          ${info?.online === false
            ? '<strong class="error">Navidrome antwortet gerade nicht.</strong>' : ''}</p>
        <form class="row mt-3" id="m-login">
          <label class="sr-only" for="m-user">Navidrome-Benutzer</label>
          <input class="grow" id="m-user" name="username" placeholder="Benutzername"
                 autocomplete="username" spellcheck="false" required>
          <label class="sr-only" for="m-pass">Passwort</label>
          <input class="grow" id="m-pass" name="password" type="password"
                 placeholder="Passwort" autocomplete="current-password" required>
          <button class="btn btn-primary" type="submit">Verbinden</button>
        </form>
      </div>`;
  }

  function zeigeVerbunden(info) {
    const server = info.server || {};
    $('#m-zugang').innerHTML = `
      <div class="notice">${icon('check')}<div>
        Verbunden als <strong>${esc(info.username || '—')}</strong>${
          server.version ? ` · Navidrome ${esc(server.version)}` : ''}${
          server.error ? ` — <span class="error">${esc(server.error)}</span>` : ''}
      </div></div>`;
    $('#m-aktionen').innerHTML =
      '<button type="button" class="btn btn-ghost" id="m-trennen">Konto trennen</button>';
  }

  // ------------------------------------------------------------ Inhalt
  const leiste = werkzeugleiste({
    id: 'mediathek',
    standard: { q: '', sort: 'newest', proSeite: 48 },
    suche: { platzhalter: 'In der Mediathek suchen…', label: 'Mediathek durchsuchen' },
    filter: [{ name: 'sort', label: 'Sortierung', werte: SORTIERUNG }],
    zusatz: [
      { name: 'proSeite', art: 'auswahl', zahl: true, label: 'Alben je Seite',
        werte: [[24, '24'], [48, '48'], [96, '96']] },
    ],
    beiAenderung: () => { offset = 0; ladeInhalt(); },
  });

  function albumKarte(a) {
    const bild = cover(a.coverArt || a.id, 300);
    return `
      <a class="cover-card" href="#/mediathek/${encodeURIComponent(a.id)}">
        <div class="cover-art">${bild
          ? `<img src="${esc(bild)}" alt="" width="300" height="300" loading="lazy" decoding="async">`
          : ''}</div>
        <span class="cover-title">${esc(a.name || a.album || '—')}</span>
        <span class="cover-sub">${esc(a.artist || '')}${a.year ? ` · ${a.year}` : ''}</span>
      </a>`;
  }

  async function ladeInhalt() {
    const el = $('#m-inhalt');
    if (!el) return;
    el.innerHTML = skeleton(4);
    const w = leiste.werte();

    // Ein Suchbegriff fragt Navidrome, keine Sortierung: die Suche kennt
    // Titel, Alben und Interpreten, die Albumliste nur Alben.
    if ((w.q || '').trim()) {
      try {
        const d = await get(`/api/mediathek/search${q({ q: w.q, limit: 40 })}`);
        if (!$('#m-inhalt')) return;
        const teile = [];
        if (d.albums?.length) {
          teile.push(`<h3>Alben</h3><div class="grid-cards">${
            d.albums.map(albumKarte).join('')}</div>`);
        }
        if (d.songs?.length) {
          teile.push(`<h3 class="mt-4">Titel</h3><div class="tracklist">${
            d.songs.map((s) => titelZeile(s)).join('')}</div>`);
        }
        el.innerHTML = teile.length ? teile.join('')
          : empty('Nichts gefunden.', 'Navidrome sucht buchstabengetreu — andere Schreibweise probieren.');
        leiste.setzeZaehler(`${(d.albums || []).length} Alben · ${(d.songs || []).length} Titel`);
        $('#m-mehr').innerHTML = '';
      } catch (exc) {
        el.innerHTML = failure('Suche fehlgeschlagen.', exc.message);
      }
      return;
    }

    try {
      const d = await get(`/api/mediathek/albums${q({
        sort: w.sort, limit: w.proSeite, offset })}`);
      if (!$('#m-inhalt')) return;
      mehr = d.more;
      el.innerHTML = d.albums.length
        ? `<div class="grid-cards">${d.albums.map(albumKarte).join('')}</div>`
        : empty('Keine Alben in dieser Ansicht.',
                offset ? 'Blättere zurück.' : 'Navidrome hat noch nichts indiziert.');
      leiste.setzeZaehler(`${offset + 1}–${offset + d.albums.length}`);
      zeichneMehr();
    } catch (exc) {
      el.innerHTML = failure('Mediathek nicht abrufbar.', exc.message);
    }
  }

  function zeichneMehr() {
    const el = $('#m-mehr');
    if (!el) return;
    const proSeite = leiste.werte().proSeite;
    if (!offset && !mehr) { el.innerHTML = ''; return; }
    el.innerHTML = `
      <nav class="pager" aria-label="Seiten">
        <button type="button" class="btn btn-sm" data-blaettern="-1"
                ${offset === 0 ? 'disabled' : ''}>Zurück</button>
        <span class="faint tiny">Ab ${num(offset + 1)}</span>
        <button type="button" class="btn btn-sm" data-blaettern="1"
                ${mehr ? '' : 'disabled'}>Weiter</button>
      </nav>`;
    void proSeite;
  }

  function titelZeile(s) {
    const spielt = String(player.laeuft()) === `nd-${s.id}`;
    return `
      <div class="track has-art ${spielt ? 'is-playing' : ''}" data-nd="${esc(s.id)}">
        <span class="track-no">${s.track || ''}</span>
        <span class="track-art">${s.coverArt
          ? `<img src="${esc(cover(s.coverArt, 96))}" alt="" width="44" height="44" loading="lazy">`
          : ''}</span>
        <div class="track-main">
          <div class="item-title track-title">${esc(s.title || '')}</div>
          <div class="item-sub">${esc(s.artist || '')}${s.album ? ` · ${esc(s.album)}` : ''}</div>
        </div>
        <span class="track-time">${duration(s.duration)}</span>
        <span class="track-actions">
          <button type="button" class="btn btn-sm btn-ghost btn-icon" data-nd-play="${esc(s.id)}"
                  aria-label="${spielt ? 'Anhalten' : `„${esc(s.title || '')}“ abspielen`}"
                  title="${spielt ? 'Anhalten' : 'Abspielen'}">${icon(spielt ? 'pause' : 'play')}</button>
        </span>
      </div>`;
  }

  // --------------------------------------------------------- Albumseite
  async function zeigeAlbum(id) {
    const el = $('#m-inhalt');
    el.innerHTML = skeleton(6);
    $('#m-leiste').innerHTML = '';
    $('#m-mehr').innerHTML = '';
    try {
      const a = await get(`/api/mediathek/album/${encodeURIComponent(id)}`);
      if (!$('#m-inhalt')) return;
      const titel = a.song || [];
      const bild = cover(a.coverArt || a.id, 500);
      el.innerHTML = `
        <a class="crumb" href="#/mediathek">${icon('back')} Zurück zur Mediathek</a>
        <header class="hero">
          <div class="hero-art">${bild
            ? `<img src="${esc(bild)}" alt="" width="500" height="500" decoding="async">` : ''}</div>
          <div class="hero-body">
            <div class="hero-kind">Album</div>
            <h2 class="hero-title">${esc(a.name || '—')}</h2>
            <div class="hero-meta">${esc(a.artist || '')}${a.year ? ` · ${a.year}` : ''} · ${
              num(a.songCount || titel.length)} Titel · ${duration(a.duration)}</div>
          </div>
        </header>
        <div class="card card-flush">
          <h3>Titel</h3>
          <div class="tracklist pad-body">${titel.length
            ? titel.map(titelZeile).join('') : empty('Keine Titel.')}</div>
        </div>`;
    } catch (exc) {
      el.innerHTML = `<a class="crumb" href="#/mediathek">${icon('back')} Zurück</a>
        ${failure('Album nicht abrufbar.', exc.message)}`;
    }
  }

  // ------------------------------------------------------------- Start
  try {
    stand = await get('/api/mediathek/status');
  } catch (exc) {
    $('#m-zugang').innerHTML = failure('Zustand nicht abrufbar.', exc.message);
    return;
  }
  if (!$('#m-zugang')) return;

  if (!stand.configured) {
    zeigeAnmeldung(stand);
  } else {
    zeigeVerbunden(stand);
    $('#m-leiste').replaceWith(leiste.el);
    leiste.el.id = 'm-leiste';
    if (ctx.param) await zeigeAlbum(ctx.param);
    else await ladeInhalt();
  }

  // --------------------------------------------------------- Ereignisse
  wurzel.addEventListener('submit', async (e) => {
    const form = e.target.closest('#m-login');
    if (!form) return;
    e.preventDefault();
    const knopf = form.querySelector('button');
    knopf.disabled = true;
    const daten = new FormData(form);
    try {
      await post('/api/mediathek/connect', {
        username: daten.get('username'), password: daten.get('password'),
      });
      ok('Verbunden — deine Mediathek wird geladen.');
      stand = await get('/api/mediathek/status');
      zeigeVerbunden(stand);
      $('#m-zugang').insertAdjacentElement('afterend', leiste.el);
      leiste.el.id = 'm-leiste';
      await ladeInhalt();
    } catch (exc) { fail(exc.message); knopf.disabled = false; }
  });

  wurzel.addEventListener('click', async (e) => {
    const trennen = e.target.closest('#m-trennen');
    if (trennen) {
      try {
        await del('/api/mediathek/connect');
        ok('Konto getrennt');
        zeigeAnmeldung(await get('/api/mediathek/status'));
      } catch (exc) { fail(exc.message); }
      return;
    }

    const blaettern = e.target.closest('[data-blaettern]');
    if (blaettern) {
      offset = Math.max(0, offset + Number(blaettern.dataset.blaettern) * leiste.werte().proSeite);
      await ladeInhalt();
      wurzel.scrollIntoView({ behavior: 'smooth', block: 'start' });
      return;
    }

    const spielen = e.target.closest('[data-nd-play]');
    if (spielen) {
      const id = spielen.dataset.ndPlay;
      const zeile = spielen.closest('[data-nd]');
      player.play({
        id: `nd-${id}`,
        // Über den Gateway, nicht über /rest/: das Dashboard hat keine
        // Subsonic-Sitzung, der Gateway dagegen seine eigenen Zugangsdaten.
        src: `/api/mediathek/stream/${encodeURIComponent(id)}`,
        title: zeile?.querySelector('.track-title')?.textContent || 'Titel',
        artist: zeile?.querySelector('.item-sub')?.textContent || '',
        cover: cover(id, 96),
      });
    }
  });

  const abPlayer = player.onChange(() => {
    for (const el of $$('[data-nd-play]')) {
      const spielt = String(player.laeuft()) === `nd-${el.dataset.ndPlay}`;
      el.innerHTML = icon(spielt ? 'pause' : 'play');
      el.closest('.track')?.classList.toggle('is-playing', spielt);
    }
  });

  return () => { abPlayer(); player.stop(); };
}
