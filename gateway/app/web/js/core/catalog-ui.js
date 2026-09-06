// Bausteine, die Suche, Interpret, Album und Playlist gemeinsam nutzen.
// Liegt im Kern und nicht in einem Modul, damit keins vom anderen abhängt.

import { esc, icon, duration, zustandPill } from './dom.js';

/** Cover-Adresse über den eigenen Server. Ohne Prüfsumme kein Bild —
 *  dann bleibt die Fläche leer statt ein kaputtes Bild zu zeigen. */
export function coverUrl(md5, art = 'cover', groesse = 250) {
  if (!md5) return null;
  return `/api/catalog/cover/${art}/${md5}?s=${groesse}`;
}

/** Bildfläche mit fester Größe — verhindert das Springen des Layouts
 *  beim Nachladen. */
export function coverImg(md5, art, alt, groesse = 250) {
  const url = coverUrl(md5, art, groesse);
  if (!url) return `<div class="cover-art" aria-hidden="true"></div>`;
  return `<div class="cover-art">
    <img src="${esc(url)}" alt="${esc(alt || '')}"
         width="${groesse}" height="${groesse}" loading="lazy" decoding="async">
  </div>`;
}

/** Karte für Album, Interpret oder Playlist. Ein Verweis, kein div mit
 *  onclick — damit Mittelklick, Tastatur und Vorschau funktionieren. */
export function karte({ href, md5, art, titel, sub, rund = false }) {
  return `<a class="cover-card ${rund ? 'cover-round' : ''}" href="${esc(href)}">
    ${coverImg(md5, art, titel)}
    <span class="cover-title">${esc(titel)}</span>
    ${sub ? `<span class="cover-sub">${esc(sub)}</span>` : ''}
  </a>`;
}

/** Zustandsplakette und Aktion für einen Katalogtitel.
 *  Alles, was noch nicht wirklich in der Bibliothek liegt, behält einen
 *  Knopf — sonst hängt ein Eintrag ohne Ausweg fest. */
export function trackAktion(track) {
  const bekannt = track.known;
  if (bekannt?.navidrome_id) return '<span class="pill pill-ok">vorhanden</span>';

  const zustand = bekannt && bekannt.state !== 'virtual' ? bekannt.state : null;
  const plakette = zustand ? zustandPill(zustand) : '';
  const beschriftung = zustand ? 'Erneut' : 'Laden';
  const klasse = zustand ? 'btn btn-sm' : 'btn btn-sm btn-go';
  return `${plakette}<button type="button" class="${klasse}"
    data-download="${esc(track.provider_id)}">${beschriftung}</button>`;
}

/**
 * Zeile einer Titelliste, mit Hörprobe und Ladeknopf.
 *
 * `cover: true` blendet das Albumbild vor der Zeile ein. In einer Albumliste
 * wäre es überflüssig — dort ist es für alle Titel dasselbe und steht schon
 * im Kopf. In einer Trefferliste dagegen ist es das, woran man einen Titel
 * erkennt, lange bevor man den Namen gelesen hat.
 */
export function trackZeile(track, { nummer = null, laeuft = null, cover = false } = {}) {
  const spielt = laeuft && String(laeuft) === String(track.provider_id);
  const bild = cover ? coverUrl(track.md5_image, 'cover', 120) : null;
  return `
    <div class="track ${cover ? 'has-art' : ''} ${spielt ? 'is-playing' : ''}"
         data-track="${esc(track.provider_id)}">
      <span class="track-no">${nummer ?? track.track_no ?? ''}</span>
      ${cover ? `<span class="track-art">${bild
        ? `<img src="${esc(bild)}" alt="" width="44" height="44" loading="lazy" decoding="async">`
        : ''}</span>` : ''}
      <div class="track-main">
        <div class="item-title track-title">${esc(track.title)}</div>
        <div class="item-sub">${esc(track.artist)}${
          track.album ? ` · ${esc(track.album)}` : ''}</div>
      </div>
      <span class="track-time">${duration(track.duration)}</span>
      <span class="track-actions">
        <button type="button" class="btn btn-sm btn-ghost btn-icon"
                data-preview="${esc(track.provider_id)}"
                aria-label="${spielt ? 'Hörprobe beenden' : `Hörprobe von „${esc(track.title)}“`}"
                title="Hörprobe, 30 s">${icon(spielt ? 'pause' : 'play')}</button>
        ${trackAktion(track)}
      </span>
    </div>`;
}

/** Kopfbereich einer Detailseite. */
export function hero({ art, md5, bildart, kind, titel, meta, aktionen = '', rund = false }) {
  return `
    <header class="hero ${rund ? 'hero-round' : ''}">
      <div class="hero-art">${coverUrl(md5, bildart, 500)
        ? `<img src="${esc(coverUrl(md5, bildart, 500))}" alt="" width="500" height="500" decoding="async">`
        : ''}</div>
      <div class="hero-body">
        <div class="hero-kind">${esc(kind)}</div>
        <h2 class="hero-title">${esc(titel)}</h2>
        <p class="hero-meta">${meta}</p>
        <div class="btn-row mt-3">${aktionen}</div>
      </div>
    </header>`;
}

export const zurueck = (href, text) =>
  `<a class="crumb" href="${esc(href)}">${icon('back')}<span>${esc(text)}</span></a>`;
