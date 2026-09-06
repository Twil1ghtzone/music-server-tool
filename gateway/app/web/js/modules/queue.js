// Warteschlange: was on demand angefordert wurde und wie weit es ist.

import { get, post, del } from '../core/api.js';
import { $, esc, empty, failure, skeleton, relativeTime, zustandPill } from '../core/dom.js';
import * as bus from '../core/bus.js';
import { ok, fail } from '../core/toast.js';

export const meta = { id: 'queue', titel: 'Warteschlange' };

export async function mount(wurzel, ctx) {
  const admin = ctx.istAdmin();

  wurzel.innerHTML = `
    <div class="page-head">
      <div>
        <h2>Warteschlange</h2>
        <p class="lede">Titel, die über die Suche oder einen Musik-Client angefordert wurden.</p>
      </div>
      ${admin ? `<div class="page-actions">
        <button type="button" class="btn" data-do="clear">Fehlgeschlagene entfernen</button>
        <button type="button" class="btn" data-do="staging">Staging importieren</button>
      </div>` : ''}
    </div>
    <div class="card card-flush"><div id="q-list" class="list">${skeleton(4)}</div></div>`;

  function zeichne(items) {
    const el = $('#q-list');
    if (!el) return;
    if (!items?.length) {
      el.innerHTML = empty('Nichts in der Warteschlange.',
        'Suche einen Titel, den es lokal noch nicht gibt, und fordere ihn an.');
      return;
    }
    el.innerHTML = items.map((i) => {
      const fertig = Boolean(i.navidrome_id) || i.state === 'ready';
      return `
      <div class="item">
        <div class="item-main">
          <div class="item-title">${esc(i.artist)} — ${esc(i.title)}</div>
          <div class="item-sub">${esc(i.album || '')}${
            i.error ? ` · ${esc(i.error)}` : ''} · ${esc(relativeTime(i.updated_at))}</div>
        </div>
        <div class="item-side">
          ${i.play_requests ? `<span class="faint tiny num">${i.play_requests}×</span>` : ''}
          ${zustandPill(i.state)}
          ${!fertig && i.provider_id
            ? `<button type="button" class="btn btn-sm" data-retry="${esc(i.provider_id)}">Erneut</button>` : ''}
          ${admin && !fertig
            ? `<button type="button" class="btn btn-sm btn-ghost btn-icon" data-forget="${esc(i.id)}"
                 aria-label="„${esc(i.title)}“ aus der Liste entfernen" title="Aus der Liste entfernen">
                 <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"
                   stroke-linecap="round" aria-hidden="true"><path d="M18 6 6 18M6 6l12 12"/></svg>
               </button>` : ''}
        </div>
      </div>`;
    }).join('');
  }

  async function lade() {
    try { zeichne((await get('/api/queue?limit=200')).items); }
    catch (exc) { $('#q-list').innerHTML = failure('Warteschlange nicht abrufbar.', exc.message); }
  }
  await lade();

  // Die Momentaufnahme aus dem Ereignisstrom hält die Liste aktuell, ohne
  // dass hier ein eigener Zeitgeber laufen muss.
  const abState = bus.on('state', (s) => zeichne(s.queue));

  wurzel.addEventListener('click', async (event) => {
    const erneut = event.target.closest('[data-retry]');
    const weg = event.target.closest('[data-forget]');
    const tun = event.target.closest('[data-do]');
    const knopf = erneut || weg || tun;
    if (!knopf) return;
    knopf.disabled = true;
    try {
      if (erneut) {
        await post('/api/download', { provider_id: erneut.dataset.retry });
        ok('Erneut angefordert');
        await lade();
      } else if (weg) {
        await del(`/api/queue/${encodeURIComponent(weg.dataset.forget)}`);
        weg.closest('.item')?.remove();
      } else if (tun.dataset.do === 'clear') {
        const r = await post('/api/queue/clear-failed');
        ok(`${r.removed} Eintrag/Einträge entfernt`);
        await lade();
      } else {
        await post('/api/import-staging');
        ok('Import eingeplant');
      }
    } catch (exc) { fail(exc.message); } finally { knopf.disabled = false; }
  });

  return () => abState();
}
