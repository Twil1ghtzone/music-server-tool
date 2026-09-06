// Suche über die Bibliothek und den Katalog, in vier Arten.

import { get, post, q } from '../core/api.js';
import { $, $$, esc, empty, failure, skeleton, icon } from '../core/dom.js';
import { karte, trackZeile } from '../core/catalog-ui.js';
import * as player from '../core/player.js';
import { ok, fail } from '../core/toast.js';

export const meta = { id: 'search', titel: 'Suche & Download' };

const ARTEN = [
  { id: 'track', titel: 'Titel' },
  { id: 'album', titel: 'Alben' },
  { id: 'artist', titel: 'Interpreten' },
  { id: 'playlist', titel: 'Playlists' },
];

// Der Suchbegriff überlebt einen Modulwechsel — sonst tippt man ihn nach
// jedem Abstecher auf eine Albumseite neu.
let letzterBegriff = '';
let letzteArt = 'track';

export async function mount(wurzel) {
  wurzel.innerHTML = `
    <div class="page-head">
      <div>
        <h2>Suche &amp; Download</h2>
        <p class="lede">Was lokal fehlt, lässt sich direkt aus dem Katalog holen.</p>
      </div>
    </div>

    <form class="card row" id="s-form" role="search">
      <label class="sr-only" for="s-q">Suchbegriff</label>
      <input class="grow" id="s-q" name="q" type="search" spellcheck="false"
             placeholder="Interpret, Titel, Album…" value="${esc(letzterBegriff)}" required>
      <button class="btn btn-primary" type="submit">Suchen</button>
    </form>

    <div class="btn-row" role="tablist" aria-label="Art der Treffer" id="s-tabs">
      ${ARTEN.map((a) => `<button type="button" class="btn btn-sm" role="tab"
        aria-selected="${a.id === letzteArt}" data-kind="${a.id}">${a.titel}</button>`).join('')}
    </div>

    <div id="s-local"></div>
    <div class="card card-flush">
      <h3 id="s-catalog-head">Im Katalog</h3>
      <div id="s-catalog" class="pad-body"></div>
    </div>`;

  markiereTab();

  async function sucheTitel(begriff) {
    const ziel = $('#s-catalog');
    const lokal = $('#s-local');
    ziel.innerHTML = skeleton(6);
    lokal.innerHTML = '';

    try {
      const d = await get(`/api/search${q({ q: begriff })}`);

      lokal.innerHTML = `<div class="card card-flush">
        <h3>In der Bibliothek${d.corrected ? ` — zeige „${esc(d.corrected)}“` : ''}</h3>
        <div class="list">${(d.local || []).length
          ? d.local.map((s) => `
              <div class="item">
                <div class="item-main">
                  <div class="item-title">${esc(s.artist)} — ${esc(s.title)}</div>
                  <div class="item-sub">${esc(s.album || '')} · ${esc(s.suffix || '')}${
                    s.bitRate ? ` ${s.bitRate}&nbsp;kbit/s` : ''}</div>
                </div>
                <div class="item-side"><span class="pill pill-ok">lokal</span></div>
              </div>`).join('')
          : empty('Nichts in der Bibliothek gefunden.',
                  d.corrected ? '' : 'Vielleicht liegt der Titel unter anderer Schreibweise vor.')
        }</div></div>`;

      ziel.innerHTML = (d.catalog || []).length
        ? `<div class="tracklist">${d.catalog.map((t) =>
            trackZeile(t, { laeuft: player.laeuft() })).join('')}</div>`
        : empty('Keine Katalogtreffer.',
                'Prüfe unter Diagnose, ob der Katalog erreichbar ist.');
    } catch (exc) {
      ziel.innerHTML = failure('Suche fehlgeschlagen.', exc.message);
    }
  }

  async function sucheKatalog(begriff, art) {
    const ziel = $('#s-catalog');
    $('#s-local').innerHTML = '';
    ziel.innerHTML = skeleton(4);
    try {
      const d = await get(`/api/catalog/search${q({ q: begriff, kind: art, limit: 40 })}`);
      const treffer = d.results || [];
      if (!treffer.length) { ziel.innerHTML = empty('Keine Treffer.'); return; }

      ziel.innerHTML = `<div class="grid-cards">${treffer.map((e) => {
        if (art === 'album') {
          return karte({ href: `#/album/${e.id}`, md5: e.md5_image, art: 'cover',
                         titel: e.title, sub: `${e.artist}${e.year ? ` · ${e.year}` : ''}` });
        }
        if (art === 'artist') {
          return karte({ href: `#/artist/${e.id}`, md5: e.md5_image, art: 'artist',
                         titel: e.name, sub: e.albums ? `${e.albums} Alben` : '', rund: true });
        }
        return karte({ href: `#/playlist/${e.id}`, md5: e.md5_image, art: 'playlist',
                       titel: e.title, sub: `${e.tracks || 0} Titel` });
      }).join('')}</div>`;
    } catch (exc) {
      ziel.innerHTML = failure('Katalogsuche fehlgeschlagen.', exc.message);
    }
  }

  function markiereTab() {
    for (const b of $$('#s-tabs [data-kind]')) {
      const aktiv = b.dataset.kind === letzteArt;
      b.setAttribute('aria-selected', String(aktiv));
      b.classList.toggle('btn-primary', aktiv);
    }
    $('#s-catalog-head').textContent =
      letzteArt === 'track' ? 'Im Katalog'
        : `Im Katalog — ${ARTEN.find((a) => a.id === letzteArt).titel}`;
  }

  async function suchen() {
    const begriff = $('#s-q').value.trim();
    if (!begriff) return;
    letzterBegriff = begriff;
    if (letzteArt === 'track') await sucheTitel(begriff);
    else await sucheKatalog(begriff, letzteArt);
  }

  $('#s-form').addEventListener('submit', (e) => { e.preventDefault(); suchen(); });

  $('#s-tabs').addEventListener('click', (e) => {
    const tab = e.target.closest('[data-kind]');
    if (!tab) return;
    letzteArt = tab.dataset.kind;
    markiereTab();
    if ($('#s-q').value.trim()) suchen();
  });

  wurzel.addEventListener('click', async (e) => {
    const probe = e.target.closest('[data-preview]');
    const laden = e.target.closest('[data-download]');
    if (probe) {
      const zeile = probe.closest('[data-track]');
      player.play({
        id: probe.dataset.preview,
        title: zeile?.querySelector('.track-title')?.textContent || 'Titel',
        artist: zeile?.querySelector('.item-sub')?.textContent || '',
      });
      return;
    }
    if (!laden) return;
    laden.disabled = true;
    try {
      await post('/api/download', { provider_id: laden.dataset.download });
      laden.outerHTML = '<span class="pill pill-busy">eingeplant</span>';
      ok('Download eingeplant');
    } catch (exc) { fail(exc.message); laden.disabled = false; }
  });

  // Beim Zurückkommen den letzten Treffer wiederherstellen.
  if (letzterBegriff) await suchen();
  else $('#s-catalog').innerHTML = empty('Noch nichts gesucht.',
    'Tippe einen Interpreten, Titel oder ein Album ein.');

  const abPlayer = player.onChange(() => {
    // Nur die Symbole tauschen statt neu zu zeichnen - sonst verliert man
    // die Scrollposition mitten in einer langen Liste.
    for (const el of $$('[data-preview]')) {
      const spielt = String(player.laeuft()) === el.dataset.preview;
      el.innerHTML = icon(spielt ? 'pause' : 'play');
      el.closest('.track')?.classList.toggle('is-playing', spielt);
    }
  });

  return () => abPlayer();
}
