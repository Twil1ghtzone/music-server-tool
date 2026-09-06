// Jobs: was der Worker abarbeitet, mit Fortschritt und Fehlergrund.

import { get, post, q } from '../core/api.js';
import { $, esc, empty, failure, skeleton, relativeTime, num, zustandPill, balkenFuellen } from '../core/dom.js';
import * as bus from '../core/bus.js';
import { ok, fail } from '../core/toast.js';

export const meta = { id: 'jobs', titel: 'Jobs' };

export async function mount(wurzel, ctx) {
  const admin = ctx.istAdmin();
  let filter = 'all';

  wurzel.innerHTML = `
    <div class="page-head">
      <div>
        <h2>Jobs</h2>
        <p class="lede">Downloads, Importe, Scans und Duplikatläufe.</p>
      </div>
      <div class="page-actions">
        <label class="sr-only" for="job-filter">Nach Zustand filtern</label>
        <select id="job-filter">
          <option value="all">Alle</option>
          <option value="active">Aktiv</option>
          <option value="failed">Fehlgeschlagen</option>
          <option value="done">Erledigt</option>
        </select>
      </div>
    </div>
    <div class="tiles" id="job-tiles"></div>
    <div class="card card-flush"><div id="job-list" class="list">${skeleton(6)}</div></div>`;

  function zeichne(jobs) {
    const el = $('#job-list');
    if (!el) return;
    if (!jobs?.length) {
      el.innerHTML = empty('Keine Jobs für diesen Filter.');
      return;
    }
    el.innerHTML = jobs.map((j) => {
      const prozent = Math.round((j.progress || 0) * 100);
      const wiederholbar = admin && ['failed', 'cancelled'].includes(j.state);
      const abbrechbar = admin && j.state === 'pending';
      return `
        <div class="item">
          <div class="item-main">
            <div class="item-title">#${j.id} ${esc(j.type)}</div>
            <div class="item-sub">${esc(j.detail || j.last_error || '—')} · ${esc(relativeTime(j.updated_at))}${
              j.attempts > 1 ? ` · Versuch ${j.attempts}/${j.max_attempts}` : ''}</div>
          </div>
          <div class="item-side">
            ${j.state === 'running'
              ? `<div class="bar" data-anteil="${prozent}"><span></span></div>
                 <span class="tiny faint num">${prozent}&nbsp;%</span>` : ''}
            ${zustandPill(j.state)}
            ${wiederholbar ? `<button type="button" class="btn btn-sm" data-retry="${j.id}">Wiederholen</button>` : ''}
            ${abbrechbar ? `<button type="button" class="btn btn-sm btn-ghost" data-cancel="${j.id}">Abbrechen</button>` : ''}
          </div>
        </div>`;
    }).join('');
    balkenFuellen(el);
  }

  function zeichneKacheln(stats) {
    $('#job-tiles').innerHTML = ['pending', 'running', 'done', 'failed']
      .map((k) => `<div class="tile ${k === 'failed' && stats[k] ? 'tile-err' : ''}">
        <div class="tile-label">${{ pending: 'Wartend', running: 'Läuft',
                                    done: 'Erledigt', failed: 'Fehlgeschlagen' }[k]}</div>
        <div class="tile-value">${num(stats[k])}</div>
      </div>`).join('');
  }

  async function lade() {
    try {
      const d = await get(`/api/jobs${q({ state: filter, limit: 200 })}`);
      zeichne(d.jobs);
      zeichneKacheln(d.stats || {});
    } catch (exc) {
      $('#job-list').innerHTML = failure('Jobs nicht abrufbar.', exc.message);
    }
  }
  await lade();

  $('#job-filter').addEventListener('change', (e) => { filter = e.target.value; lade(); });

  // Nur bei „aktiv" live nachziehen - sonst springt die Liste unter der Hand.
  const abState = bus.on('state', (s) => {
    zeichneKacheln(s.jobs || {});
    if (filter === 'active') zeichne(s.active);
  });

  wurzel.addEventListener('click', async (event) => {
    const erneut = event.target.closest('[data-retry]');
    const abbruch = event.target.closest('[data-cancel]');
    const knopf = erneut || abbruch;
    if (!knopf) return;
    knopf.disabled = true;
    try {
      const id = erneut ? erneut.dataset.retry : abbruch.dataset.cancel;
      await post(`/api/jobs/${id}/${erneut ? 'retry' : 'cancel'}`);
      ok(erneut ? 'Job neu eingeplant' : 'Job abgebrochen');
      await lade();
    } catch (exc) { fail(exc.message); knopf.disabled = false; }
  });

  return () => abState();
}
