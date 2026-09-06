// Kurzmeldungen. aria-live="polite", damit Screenreader sie mitbekommen,
// ohne den Nutzer zu unterbrechen.

import { esc, icon } from './dom.js';

let behaelter = null;

function box() {
  if (!behaelter) {
    behaelter = document.createElement('div');
    behaelter.className = 'toasts';
    behaelter.setAttribute('aria-live', 'polite');
    behaelter.setAttribute('aria-atomic', 'false');
    document.body.append(behaelter);
  }
  return behaelter;
}

const SYMBOL = { ok: 'info', err: 'warn', '': 'info' };

export function toast(nachricht, art = '') {
  const el = document.createElement('div');
  el.className = `toast ${art ? `toast-${art}` : ''}`;
  el.innerHTML = `${icon(SYMBOL[art] || 'info')}<span>${esc(nachricht)}</span>`;
  box().append(el);
  // Fehler bleiben laenger stehen - sie will man lesen, nicht erahnen.
  setTimeout(() => el.remove(), art === 'err' ? 9000 : 5000);
}

export const ok = (nachricht) => toast(nachricht, 'ok');
export const fail = (nachricht) => toast(nachricht, 'err');
