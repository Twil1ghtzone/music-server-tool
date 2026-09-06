// Hörprobe: 30 Sekunden je Titel, wie sie die Deezer-API mitliefert.
//
// Die Datei kommt nicht direkt von Deezer, sondern über den Gateway
// (/api/catalog/preview/…). Damit bleibt die Content-Security-Policy auf
// 'self' — sonst müssten fremde Medienhosts freigegeben werden.
//
// Genau ein Element für die ganze Oberfläche: mehrere gleichzeitig laufende
// Proben wären ein Fehler, kein Merkmal.

import { esc, icon } from './dom.js';
import * as prefs from './prefs.js';

let audio = null;
let leiste = null;
let laufenderTitel = null;
const beobachter = new Set();

function bau() {
  if (leiste) return;
  audio = new Audio();
  audio.preload = 'none';
  audio.volume = prefs.hole('hoerprobeLautstaerke') ?? 0.8;
  // Die Lautstärke gilt sofort, nicht erst bei der nächsten Hörprobe.
  prefs.onChange(() => {
    if (audio) audio.volume = prefs.hole('hoerprobeLautstaerke') ?? 0.8;
  });

  leiste = document.createElement('div');
  leiste.className = 'player';
  leiste.hidden = true;
  document.getElementById('view')?.after(leiste);

  audio.addEventListener('ended', () => stop());
  // Der Streifen oben auf der Leiste. 30 Sekunden ohne jede Anzeige fühlen
  // sich länger an, als sie sind — man weiß nicht, ob noch etwas kommt.
  audio.addEventListener('timeupdate', () => {
    const strich = leiste?.querySelector('.player-progress > span');
    if (strich && audio.duration) {
      strich.style.width = `${(audio.currentTime / audio.duration) * 100}%`;
    }
  });
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
    <div class="player-progress"><span></span></div>
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

/** Hält an. Gibt zurück, ob wirklich etwas lief — das Tastenkürzel
 *  entscheidet daran, ob es die Leertaste verbraucht oder durchlässt. */
export function stop() {
  if (!audio || !laufenderTitel) return false;
  audio.pause();
  audio.removeAttribute('src');
  laufenderTitel = null;
  zeichne();
  melde();
  return true;
}

export const laeuft = () => laufenderTitel?.id ?? null;
