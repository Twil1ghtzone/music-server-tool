// Hörprobe: 30 Sekunden je Titel, wie sie die Deezer-API mitliefert.
//
// Die Datei kommt nicht direkt von Deezer, sondern über den Gateway
// (/api/catalog/preview/…). Damit bleibt die Content-Security-Policy auf
// 'self' — sonst müssten fremde Medienhosts freigegeben werden.
//
// Genau ein Element für die ganze Oberfläche: mehrere gleichzeitig laufende
// Proben wären ein Fehler, kein Merkmal.

import { esc, icon } from './dom.js';

let audio = null;
let leiste = null;
let laufenderTitel = null;
const beobachter = new Set();

function bau() {
  if (leiste) return;
  audio = new Audio();
  audio.preload = 'none';

  leiste = document.createElement('div');
  leiste.className = 'player';
  leiste.hidden = true;
  document.getElementById('view')?.after(leiste);

  audio.addEventListener('ended', () => stop());
  audio.addEventListener('error', () => {
    zeichne({ fehler: 'Hörprobe nicht abspielbar' });
    laufenderTitel = null;
    melde();
  });

  leiste.addEventListener('click', (e) => {
    if (e.target.closest('[data-player-stop]')) stop();
  });
}

function zeichne(state = {}) {
  if (!leiste) return;
  if (state.fehler) {
    leiste.innerHTML = `<div class="player-body"><div class="player-title">${esc(state.fehler)}</div></div>
      <button class="btn btn-icon btn-ghost" data-player-stop aria-label="Schließen">${icon('close')}</button>`;
    leiste.hidden = false;
    return;
  }
  if (!laufenderTitel) { leiste.hidden = true; leiste.innerHTML = ''; return; }

  const t = laufenderTitel;
  leiste.innerHTML = `
    <div class="player-art">${t.cover
      ? `<img src="${esc(t.cover)}" alt="" width="44" height="44" loading="lazy">` : ''}</div>
    <div class="player-body">
      <div class="player-title">${esc(t.title)}</div>
      <div class="player-sub">${esc(t.artist || '')}</div>
    </div>
    <span class="player-note">Hörprobe, 30&nbsp;s</span>
    <button class="btn btn-icon btn-ghost" data-player-stop aria-label="Hörprobe beenden">
      ${icon('close')}
    </button>`;
  leiste.hidden = false;
}

function melde() {
  for (const fn of beobachter) {
    try { fn(laufenderTitel?.id ?? null); } catch { /* egal */ }
  }
}

/** Meldet sich an, wenn sich der laufende Titel ändert. Gibt eine
 *  Abmeldefunktion zurück - Module rufen sie beim Abbauen auf. */
export function onChange(handler) {
  beobachter.add(handler);
  return () => beobachter.delete(handler);
}

export function play(track) {
  bau();
  if (laufenderTitel?.id === track.id) return stop();   // erneuter Klick = anhalten
  laufenderTitel = track;
  audio.src = `/api/catalog/preview/${encodeURIComponent(track.id)}`;
  audio.play().catch(() => {
    // Wird der Aufruf nicht durch eine Nutzeraktion ausgelöst, blockt der
    // Browser. Dann lieber sagen als still scheitern.
    zeichne({ fehler: 'Der Browser hat die Wiedergabe blockiert' });
    laufenderTitel = null;
    melde();
  });
  zeichne();
  melde();
}

export function stop() {
  if (!audio) return;
  audio.pause();
  audio.removeAttribute('src');
  laufenderTitel = null;
  zeichne();
  melde();
}

export const laeuft = () => laufenderTitel?.id ?? null;
