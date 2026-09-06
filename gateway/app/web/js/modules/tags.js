// Tag-Werkstatt: Metadaten sichten und einzeln korrigieren.

import { get, patch, q } from '../core/api.js';
import { $, esc, empty, failure, skeleton } from '../core/dom.js';
import { ok, fail } from '../core/toast.js';

export const meta = { id: 'tags', titel: 'Tag-Werkstatt' };

const FELDER = [
  ['title', 'Titel'], ['artist', 'Interpret'], ['album', 'Album'],
  ['album_artist', 'Album-Interpret'], ['year', 'Jahr'], ['track_no', 'Nr.'],
];

export async function mount(wurzel) {
  wurzel.innerHTML = `
    <div class="page-head">
      <div>
        <h2>Tag-Werkstatt</h2>
        <p class="lede">Änderungen greifen erst mit <code>GATEWAY_ALLOW_TAG_WRITE=true</code>.</p>
      </div>
    </div>

    <form class="card row" id="t-form">
      <label class="sr-only" for="t-q">Suche</label>
      <input class="grow" id="t-q" name="q" type="search" spellcheck="false"
             placeholder="Titel, Interpret, Album oder Pfad…">
      <label class="inline"><input type="checkbox" name="issues_only"> nur mit Mängeln</label>
      <button class="btn btn-primary" type="submit">Anzeigen</button>
    </form>

    <div class="card card-flush"><div id="t-list" class="list"></div></div>`;

  $('#t-list').innerHTML = empty('Noch nichts geladen.',
    'Suche etwas oder zeige alle Dateien mit Mängeln.');

  $('#t-form').addEventListener('submit', async (e) => {
    e.preventDefault();
    const daten = new FormData(e.target);
    const el = $('#t-list');
    el.innerHTML = skeleton(6);
    try {
      const d = await get(`/api/library/files${q({
        q: daten.get('q') || '',
        issues_only: daten.get('issues_only') ? 'true' : 'false',
        limit: 200,
      })}`);
      el.innerHTML = (d.files || []).length
        ? d.files.map((f) => `
            <div class="item" data-file="${f.id}">
              <div class="item-main">
                <div class="item-title">${esc(f.artist || '—')} — ${esc(f.title || f.path.split(/[\\/]/).pop())}</div>
                <div class="item-sub">${esc(f.album || '')} · ${
                  f.tag_issues ? `<span class="warn">${esc(f.tag_issues)}</span>` : 'ok'}</div>
              </div>
              <div class="item-side">
                <button type="button" class="btn btn-sm" data-edit="${f.id}"
                        aria-expanded="false">Bearbeiten</button>
              </div>
            </div>`).join('')
        : empty('Keine Treffer.', 'Ist der Index schon aufgebaut? Unter Bibliothek → Neu indexieren.');
      if (d.total > (d.files || []).length) {
        el.insertAdjacentHTML('beforeend',
          `<div class="item"><div class="item-main"><div class="item-sub">
            ${d.files.length} von ${d.total} angezeigt — Suche eingrenzen.
          </div></div></div>`);
      }
    } catch (exc) {
      el.innerHTML = failure('Dateien nicht abrufbar.', exc.message);
    }
  });

  $('#t-list').addEventListener('click', (e) => {
    const knopf = e.target.closest('[data-edit]');
    if (!knopf) return;
    const zeile = knopf.closest('.item');
    const vorhanden = zeile.nextElementSibling?.classList.contains('tag-form');
    if (vorhanden) {
      zeile.nextElementSibling.remove();
      knopf.setAttribute('aria-expanded', 'false');
      return;
    }

    const form = document.createElement('form');
    form.className = 'item tag-form';
    form.innerHTML = `
      <div class="item-main row">
        ${FELDER.map(([name, label]) => `
          <label class="grow ${['year', 'track_no'].includes(name) ? 'field-narrow' : 'field-wide'}">
            <span class="tiny">${label}</span>
            <input name="${name}" ${['year', 'track_no'].includes(name) ? 'inputmode="numeric"' : ''}
                   spellcheck="false" autocomplete="off">
          </label>`).join('')}
      </div>
      <div class="item-side"><button class="btn btn-sm btn-primary">Speichern</button></div>`;

    form.addEventListener('submit', async (ev) => {
      ev.preventDefault();
      const knopf2 = form.querySelector('button');
      knopf2.disabled = true;
      const body = {};
      for (const [k, v] of new FormData(form).entries()) {
        if (v !== '') body[k] = ['year', 'track_no'].includes(k) ? Number(v) : v;
      }
      if (!Object.keys(body).length) { knopf2.disabled = false; return; }
      try {
        await patch(`/api/library/files/${knopf.dataset.edit}/tags`, body);
        ok('Tags gespeichert');
        form.remove();
        knopf.setAttribute('aria-expanded', 'false');
      } catch (exc) { fail(exc.message); knopf2.disabled = false; }
    });

    zeile.after(form);
    knopf.setAttribute('aria-expanded', 'true');
    form.querySelector('input')?.focus();
  });
}
