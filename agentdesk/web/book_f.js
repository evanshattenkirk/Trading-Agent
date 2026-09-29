/* Book F1 (stocks in play, shares) on the dashboard: a side card with the day's scan and positions, and the Tape desk's
   monitor showing the picks. Self-contained: it reads /api/state once and listens on its own /ws connection, so
   app.js and office.js stay unchanged. Every F fill is paper. */
(function () {
  'use strict';
  const $ = (id) => document.getElementById(id);
  const esc = (s) => String(s == null ? '' : s).replace(/[&<>"]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
  const f2 = (x, d = 2) => (x == null || isNaN(x) ? '—' : Number(x).toFixed(d));
  const money = (x) => (x == null ? '—' : (x >= 0 ? '+$' : '-$') + Math.abs(x).toFixed(2));
  const S = { rows: [], book: null, positions: {}, skipped: null };
  window.__fPicks = [];

  function mount() {
    const side = document.querySelector('.side-scroll');
    if (!side || $('f-card')) return !!$('f-card');
    const card = document.createElement('section');
    card.className = 'panel card';
    card.id = 'f-card';
    card.setAttribute('aria-labelledby', 'h-f');
    card.innerHTML =
      '<div class="card-h"><h2 id="h-f">Book F1 · stocks in play</h2><span class="pill" id="f-state">Waiting</span></div>' +
      '<div class="f-strip" id="f-strip"></div>' +
      '<div class="f-scroll"><table class="grid f-grid"><thead><tr><th>Sym</th><th class="r">RVOL5</th><th>1st</th>' +
      '<th class="r">OR hi</th><th class="r">ATR</th><th>Tag</th><th>Status</th><th class="r">R</th></tr></thead>' +
      '<tbody id="f-rows"></tbody></table></div><p class="note" id="f-note">Scan at 09:35 ET. Paper only.</p>';
    const first = side.querySelector('.card');
    side.insertBefore(card, first ? first.nextSibling : null);
    const st = document.createElement('style');
    st.textContent = '.f-strip{display:flex;gap:14px;flex-wrap:wrap;padding:0 12px 6px;font-size:12px;color:var(--ink-2)}' +
      '.f-strip b{font-family:var(--mono);font-weight:500;color:var(--ink)}.f-scroll{overflow-x:auto;padding:0 6px}' +
      '.f-grid{width:100%;font-size:11.5px}.f-grid th,.f-grid td{padding:4px 5px}.f-grid td{font-family:var(--mono);white-space:nowrap}.f-grid td.st{font-family:var(--sans)}' +
      '.f-up{color:var(--up)}.f-dn{color:var(--dn)}.f-dim{color:var(--ink-3)}';
    document.head.appendChild(st);
    return true;
  }

  function tag(n) {
    if (!n) return '<span class="f-dim">…</span>';
    if (n.catalyst === 'unknown') return '<span class="f-dim">n/a</span>';
    return esc(n.catalyst) + (n.priced_in && n.priced_in !== 'unknown' ? ' · ' + esc(n.priced_in) : '');
  }

  function render() {
    if (!mount()) return;
    const b = S.book || {};
    const open = Object.values(S.positions).filter((p) => p.status === 'open');
    const openR = open.reduce((a, p) => a + (p.r || 0), 0);
    const state = $('f-state');
    state.className = 'pill' + (b.halted ? ' bad' : open.length ? ' good' : S.rows.length ? ' acc' : '');
    state.textContent = b.halted ? 'Halted' : open.length ? open.length + ' open' : S.skipped ? 'Skipped' :
      S.rows.some((r) => r.status === 'armed') ? 'Armed' : S.rows.length ? 'Flat' : 'Waiting';
    $('f-strip').innerHTML = '<span>Day <b class="' + ((b.day_pnl || 0) >= 0 ? 'f-up' : 'f-dn') + '">' + money(b.day_pnl || 0) +
      '</b></span><span>Open <b>' + money(b.open_pnl || 0) + '</b></span><span>Open R <b>' + f2(openR) +
      '</b></span><span>Trades <b>' + (b.trades || 0) + '/' + (b.max_trades || 5) + '</b></span>';
    const rows = S.rows.slice(0, 15);
    $('f-rows').innerHTML = rows.map((r) => {
      const p = S.positions[r.symbol];
      const R = p ? p.r : r.r;
      const st = r.status || (r.picked ? 'armed' : '');
      return '<tr><td>' + esc(r.symbol) + (r.ai ? '<span class="f-dim">*</span>' : '') + '</td><td class="r">' + f2(r.rvol5, 1) +
        '</td><td class="' + (r.direction === 'green' ? 'f-up' : r.direction === 'red' ? 'f-dn' : '') + '" title="' + esc(r.direction) + ' first candle">' + (r.direction === 'green' ? '▲' : r.direction === 'red' ? '▼' : '·') +
        '</td><td class="r">' + f2(r.or_high) + '</td><td class="r">' + f2(r.atr) + '</td><td class="st">' + (r.news || r.picked ? tag(r.news) : '') +
        '</td><td class="st">' + esc(st === 'shadow short' ? 'shadow' : st) + '</td><td class="r ' + (R > 0 ? 'f-up' : R < 0 ? 'f-dn' : '') + '">' + f2(R, 1) + '</td></tr>';
    }).join('');
    $('f-note').textContent = S.skipped || (rows.length ? '* AI/memory list. Tag is observe-only. Paper only.' : 'Scan at 09:35 ET. Paper only.');
    window.__fPicks = S.rows.filter((r) => r.picked).map((r) => r.symbol);
  }

  function apply(e) {
    if (e.type === 'f_scan') {
      if (e.skipped) S.skipped = 'Scan skipped: ' + e.skipped;
      if (e.rows) S.rows = e.rows;
      if (e.book) S.book = e.book;
    } else if (e.type === 'f_position' && e.pos) {
      S.positions[e.pos.symbol] = e.pos;
    } else if (e.type === 'f_positions') {
      (e.positions || []).forEach((p) => { S.positions[p.symbol] = p; });
    } else if (e.type === 'session') {
      S.rows = []; S.positions = {}; S.book = null; S.skipped = null;
    } else return false;
    return true;
  }

  function fromSnapshot(snap) {
    const f = snap && snap.books && snap.books.f;
    if (!f) return;
    S.rows = f.scan || [];
    S.book = f.book || null;
    ((f.book && f.book.open) || []).concat((f.book && f.book.closed) || []).forEach((p) => { S.positions[p.symbol] = p; });
    render();
  }

  function connect() {
    if (!/^https?:$/.test(location.protocol)) return;
    fetch('/api/state').then((r) => (r.ok ? r.json() : null)).then(fromSnapshot).catch(() => {});
    let ws;
    try { ws = new WebSocket((location.protocol === 'https:' ? 'wss://' : 'ws://') + location.host + '/ws'); } catch (_) { return; }
    let dirty = false;
    ws.onmessage = (m) => {
      const d = JSON.parse(m.data);
      if (d.type === 'batch') d.events.forEach((e) => { dirty = apply(e) || dirty; });
      else if (d.books !== undefined) fromSnapshot(d);
      if (dirty) { dirty = false; render(); }
    };
    ws.onclose = () => setTimeout(connect, 3000);
  }

  // The Tape desk's monitor cycles through F's picks (office.js is not edited; its drawStation is wrapped).
  function patchOffice() {
    const O = window.Office;
    if (!O || O.prototype.__fPatched) return;
    const orig = O.prototype.drawStation;
    O.prototype.drawStation = function (k) {
      orig.call(this, k);
      const picks = window.__fPicks || [];
      if (k !== 'tape' || !picks.length || !this.cx) return;
      const sym = picks[Math.floor((this.t || 0) / 2) % picks.length];
      this.cx.save();
      this.cx.font = '8px "Silkscreen", monospace';
      this.cx.fillStyle = '#26a69a';
      this.cx.fillText(sym, 86 * 2, 92 * 2);          // just above the Tape desk's screen
      this.cx.restore();
    };
    O.prototype.__fPatched = true;
  }

  function init() { patchOffice(); mount(); render(); connect(); }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init); else init();
})();
