// Einstellungen: wie sich die Oberfläche verhält und aussieht.
//
// Alles hier ist Bequemlichkeit und gilt nur für dieses Gerät — es liegt in
// localStorage, nicht auf dem Server. Wer das Dashboard auf dem Rechner und
// auf dem Telefon benutzt, darf beide unterschiedlich einstellen.
//
// Was nicht hierhin gehört: Zugänge (Diagnose), Passwörter (Konto), Rollen
// (Benutzer). Einstellungen, die andere Menschen betreffen, sind keine
// Vorlieben.

import { $, icon, esc } from '../core/dom.js';
import * as prefs from '../core/prefs.js';
import { einstellung, auswahl, schalter, segmente } from '../core/menu.js';
import { navModule } from '../core/registry.js';
import { ok } from '../core/toast.js';

export const meta = { id: 'settings', titel: 'Einstellungen' };

const AKZENTE = [
  ['blau', 'Blau'], ['violett', 'Violett'], ['tuerkis', 'Türkis'], ['bernstein', 'Bernstein'],
];

export async function mount(wurzel, ctx) {
  wurzel.innerHTML = `
    <div class="page-head">
      <div>
        <h2>Einstellungen</h2>
        <p class="lede">Wie sich das Dashboard verhält. Gilt nur für diesen Browser —
          nichts davon wird auf dem Server gespeichert oder für andere sichtbar.</p>
      </div>
      <div class="page-actions">
        <button type="button" class="btn" id="set-reset">${icon('refresh')} Alles zurücksetzen</button>
      </div>
    </div>

    <div class="split">
      <div class="card">
        <h3>Darstellung</h3>
        <div class="stack" id="set-optik"></div>
        <hr>
        <h3>Vorschau</h3>
        <p class="muted small">So sehen Listen mit den aktuellen Einstellungen aus.</p>
        <div class="list mt-3">
          <div class="item">
            <div class="item-main">
              <div class="item-title">Smells Like Teen Spirit</div>
              <div class="item-sub">Nirvana · Nevermind · 5:01</div>
            </div>
            <div class="item-side">
              <span class="pill pill-busy">lädt</span>
              <button type="button" class="btn btn-sm btn-go">Laden</button>
            </div>
          </div>
          <div class="item">
            <div class="item-main">
              <div class="item-title">Come As You Are</div>
              <div class="item-sub">Nirvana · Nevermind · 3:39</div>
            </div>
            <div class="item-side">
              <span class="pill pill-ok">vorhanden</span>
              <button type="button" class="btn btn-sm">Erneut</button>
            </div>
          </div>
        </div>
      </div>

      <div class="card">
        <h3>Verhalten</h3>
        <div class="stack" id="set-verhalten"></div>

        <hr>
        <h3>Tastatur</h3>
        <p class="muted small">Funktioniert überall, außer während du in ein Feld tippst.</p>
        <div class="list mt-3" id="set-tasten"></div>
      </div>
    </div>

    <div class="card">
      <h3>Gemerkte Seiteneinstellungen</h3>
      <p class="muted small">Jede Seite merkt sich ihre Sortierung, Filter und Ansicht
        getrennt — über das Zahnrad rechts in ihrer Werkzeugleiste. Hier lässt sich das
        wieder verwerfen, wenn eine Seite nicht mehr so aussieht, wie du erwartest.</p>
      <div class="btn-row mt-3" id="set-module"></div>
    </div>`;

  const TASTEN = [
    ['/', 'Zum Suchfeld springen'],
    ['g dann ü', 'Zur Übersicht'],
    ['g dann s', 'Zur Suche'],
    ['g dann w', 'Zur Warteschlange'],
    ['g dann j', 'Zu den Jobs'],
    ['Leertaste', 'Laufende Hörprobe beenden'],
    ['Esc', 'Menü oder Hörprobe schließen'],
    ['?', 'Diese Liste zeigen'],
  ];
  $('#set-tasten').innerHTML = TASTEN.map(([taste, was]) => `
    <div class="item">
      <div class="item-main"><div class="item-title">${esc(was)}</div></div>
      <div class="item-side">${taste.split(' dann ')
        .map((t) => `<kbd>${esc(t)}</kbd>`).join('<span class="faint tiny">dann</span>')}</div>
    </div>`).join('');

  // --- Darstellung --------------------------------------------------------
  const optik = $('#set-optik');

  optik.append(einstellung(
    'Dichte', 'Wie viel auf den Bildschirm passt',
    segmente([['kompakt', 'Kompakt'], ['normal', 'Normal'], ['luftig', 'Luftig']],
      prefs.hole('dichte'), (w) => prefs.setze('dichte', w), { label: 'Dichte' }),
    false));

  optik.append(einstellung(
    'Akzentfarbe', 'Navigation und Hauptaktionen',
    segmente(AKZENTE, prefs.hole('akzent'), (w) => prefs.setze('akzent', w),
      { label: 'Akzentfarbe' }),
    false));

  optik.append(einstellung(
    'Bewegung', 'Ein- und Ausblenden, Übergänge',
    segmente([['voll', 'Normal'], ['reduziert', 'Reduziert']],
      prefs.hole('bewegung'), (w) => prefs.setze('bewegung', w), { label: 'Bewegung' }),
    false));

  // --- Verhalten ----------------------------------------------------------
  const verhalten = $('#set-verhalten');

  verhalten.append(einstellung(
    'Startseite', 'Was nach dem Anmelden erscheint',
    auswahl(navModule(ctx.istAdmin()).map((m) => [m.id, m.titel]),
      prefs.hole('startseite'), (w) => prefs.setze('startseite', w)),
    false));

  verhalten.append(einstellung(
    'Listen automatisch aktualisieren',
    'Neue Downloads erscheinen ohne Neuladen',
    schalter(prefs.hole('autoAktualisieren'), (an) => prefs.setze('autoAktualisieren', an))));

  verhalten.append(einstellung(
    'Vor dem Löschen nachfragen',
    'Aus­schalten überspringt jede Rückfrage — auch beim Entfernen von Benutzern',
    schalter(prefs.hole('bestaetigen'), (an) => {
      prefs.setze('bestaetigen', an);
      if (!an) ok('Rückfragen aus. Aktionen wirken jetzt sofort.');
    })));

  const laut = document.createElement('input');
  laut.type = 'range';
  laut.min = '0'; laut.max = '1'; laut.step = '0.05';
  laut.value = String(prefs.hole('hoerprobeLautstaerke'));
  laut.setAttribute('aria-label', 'Lautstärke der Hörproben');
  laut.addEventListener('input', () => prefs.setze('hoerprobeLautstaerke', Number(laut.value)));
  verhalten.append(einstellung('Lautstärke der Hörproben', null, laut, false));

  // --- Gemerkte Seiteneinstellungen ---------------------------------------
  const el = $('#set-module');
  for (const m of navModule(ctx.istAdmin())) {
    const knopf = document.createElement('button');
    knopf.type = 'button';
    knopf.className = 'btn btn-sm';
    knopf.textContent = m.titel;
    knopf.addEventListener('click', () => {
      prefs.modul(m.id).zuruecksetzen();
      ok(`„${m.titel}“ auf Standard zurückgesetzt`);
    });
    el.append(knopf);
  }

  $('#set-reset').addEventListener('click', () => {
    if (prefs.hole('bestaetigen')
        && !confirm('Alle Einstellungen dieses Browsers zurücksetzen?')) return;
    prefs.zuruecksetzen();
    ok('Auf Auslieferungszustand zurückgesetzt');
    // Neu zeichnen, damit die Bedienelemente den neuen Stand zeigen.
    mount(wurzel, ctx);
  });
}
