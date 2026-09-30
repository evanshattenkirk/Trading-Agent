/* AgentDesk dashboard. Consumes engine events from the local websocket, replays a recorded session, or (review)
   shows the last saved session read-only while the engine is off. */
(function () {
  const CFG = window.AGENTDESK || { source: 'ws' };
  const READ_ONLY = CFG.source === 'replay' || CFG.source === 'review';
  const $ = (id) => document.getElementById(id);
  const TF_SEC = { '1m': 60, '5m': 300, '15m': 900 };
  const DESK_ORDER = ['macro', 'rates', 'fed', 'vol', 'quant', 'risk', 'tape', 'ops', 'earnings', 'postmortem'];
  const DESK_META = {
    macro: ['Macro', '#3987e5'], rates: ['Rates', '#199e70'], fed: ['Fed Watch', '#9085e9'],
    vol: ['Vol', '#c98500'], quant: ['Quant', '#d55181'], risk: ['Risk', '#e66767'], tape: ['Tape (L2)', '#4fb3bf'],
    ops: ['Ops', '#7a8aa0'], earnings: ['Earnings', '#b0623a'], postmortem: ['Post-mortem', '#b8a05a'],
  };
  const ACT_LABEL = {
    watching: 'WATCHING THE TAPE', typing: 'PLACING ORDER', thinking: 'PICKING A STRIKE', consulting: 'IN A HUDDLE',
    celebrating: 'BOOKED A WIN', frustrated: 'TOOK A LOSS', coffee: 'PRE-MARKET PREP', offline: 'OFF THE CLOCK',
    paused: 'PAUSED', halted: 'DONE FOR THE DAY', alarm: 'KILL SWITCH', arriving: 'BOOTING UP', briefing: 'BRIEFING',
  };

  /* ------------------------------------------------------------------ state */
  const S = {
    mode: 'sim', symbol: 'SPY', ts: 0, price: null, vwap: null, prevClose: null,
    bars: { '144t': [], '1m': [], '5m': [], '15m': [] }, signal: null, levels: [],
    positions: new Map(), targets: new Map(), closed: [], skips: [], risk: null,
    crew: { briefs: {}, offline: true, directive: null }, agent: { activity: 'offline', text: '' },
    marks: [], feed: [], config: null, tf: '1m', bulk: false, l2: null, conviction: null, proposals: new Map(),
    books: [], account: null, combos: new Map(), bookClosed: [], activeBook: 'ALL',
  };

  /* ------------------------------------------------------------------ format */
  const ctFmt = new Intl.DateTimeFormat('en-US', { timeZone: 'America/Chicago', hour: '2-digit', minute: '2-digit', hour12: false });
  const ctFmtS = new Intl.DateTimeFormat('en-US', { timeZone: 'America/Chicago', hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false });
  const hm = (ts) => (ts ? ctFmt.format(new Date(ts * 1000)) : '—');
  const hms = (ts) => (ts ? ctFmtS.format(new Date(ts * 1000)) : '—');
  const money = (x, dec = 0) => (x == null ? '—' : (x >= 0 ? '+$' : '−$') + Math.abs(x).toFixed(dec));
  const px = (x, d = 2) => (x == null ? '—' : Number(x).toFixed(d));
  const cls = (x) => (x > 0 ? 'pos' : x < 0 ? 'neg' : '');
  const esc = (s) => String(s ?? '').replace(/[&<>"]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
  const css = (v) => getComputedStyle(document.documentElement).getPropertyValue(v).trim();
  const ctMinutes = (ts) => { const [h, m] = hm(ts).split(':').map(Number); return h * 60 + m; };
  const isRTH = (ts) => { const m = ctMinutes(ts); return m >= 510 && m < 900; };

  /* ------------------------------------------------------------------ chart */
  const LC = window.LightweightCharts;
  let chart, cS, vS, mS, sS, hS, rS, markersApi, levelLines = [];
  function buildChart() {
    const el = $('chart');
    chart = LC.createChart(el, {
      autoSize: true,
      layout: { background: { color: css('--panel') }, textColor: css('--ink-2'), fontFamily: 'JetBrains Mono, monospace', fontSize: 11,
        panes: { separatorColor: css('--line'), separatorHoverColor: css('--line-2') }, attributionLogo: true },
      grid: { vertLines: { color: css('--line') + '66' }, horzLines: { color: css('--line') + '66' } },
      rightPriceScale: { borderColor: css('--line') },
      timeScale: { borderColor: css('--line'), timeVisible: true, secondsVisible: false, rightOffset: 4, barSpacing: 7,
        tickMarkFormatter: (t) => labelFor(t, false) },
      localization: { timeFormatter: (t) => labelFor(t, true), priceFormatter: (p) => p.toFixed(2) },
      crosshair: { mode: 0 },
    });
    cS = chart.addSeries(LC.CandlestickSeries, { upColor: css('--up'), downColor: css('--dn'), wickUpColor: css('--up'), wickDownColor: css('--dn'), borderVisible: false, priceLineColor: css('--ink-3') }, 0);
    vS = chart.addSeries(LC.LineSeries, { color: css('--vwap'), lineWidth: 1, lineStyle: 2, priceLineVisible: false, lastValueVisible: false, crosshairMarkerVisible: false }, 0);
    hS = chart.addSeries(LC.HistogramSeries, { priceLineVisible: false, lastValueVisible: false, priceFormat: { type: 'price', precision: 3, minMove: 0.001 } }, 1);
    mS = chart.addSeries(LC.LineSeries, { color: css('--macd'), lineWidth: 2, priceLineVisible: false, lastValueVisible: false, priceFormat: { type: 'price', precision: 3, minMove: 0.001 } }, 1);
    sS = chart.addSeries(LC.LineSeries, { color: css('--sig'), lineWidth: 2, priceLineVisible: false, lastValueVisible: false, priceFormat: { type: 'price', precision: 3, minMove: 0.001 } }, 1);
    rS = chart.addSeries(LC.LineSeries, { color: css('--rsi'), lineWidth: 2, priceLineVisible: false, lastValueVisible: true, priceFormat: { type: 'price', precision: 1, minMove: 0.1 } }, 2);
    for (const [v, t] of [[70, '70'], [30, '30']]) rS.createPriceLine({ price: v, color: css('--ink-3'), lineWidth: 1, lineStyle: 1, axisLabelVisible: true, title: t });
    const panes = chart.panes();
    panes[0].setStretchFactor(3); panes[1].setStretchFactor(1.15); panes[2].setStretchFactor(0.9);
    markersApi = LC.createSeriesMarkers(cS, []);
  }
  function labelFor(t, withSec) {
    if (S.tf === '144t') {
      const b = S.bars['144t'][t - S.bars['144t'][0]?.i] || S.bars['144t'].find((x) => x.i === t);
      return b ? (withSec ? hms(b.end) : hm(b.end)) : '';
    }
    return withSec ? hms(t) : hm(t);
  }
  const barTime = (b) => (b.tf === '144t' ? b.i : b.t);
  function chartBars(tf) {
    // one bar per time, ascending: the chart library rejects anything else, and a rejected setData left the old
    // timeframe on screen with no new candles (Evan, 2026-09-29: 144t then back to 1m)
    const by = new Map();
    for (const b of S.bars[tf]) { const t = barTime(b); if (typeof t === 'number' && isFinite(t)) by.set(t, b); }
    return [...by.values()].sort((a, b) => barTime(a) - barTime(b));
  }
  function safeChart(fn) {
    try { fn(); } catch (err) { console.warn('chart update rejected; redrawing', err); mark('chart'); }
  }
  function pushBarToChart(b) {
    const t = barTime(b);
    cS.update({ time: t, open: b.o, high: b.h, low: b.l, close: b.c });
    if (b.vw != null) vS.update({ time: t, value: b.vw });
    if (b.macd != null && b.sig != null) {
      mS.update({ time: t, value: b.macd }); sS.update({ time: t, value: b.sig });
      const h = b.macd - b.sig;
      hS.update({ time: t, value: h, color: h >= 0 ? css('--up') + '99' : css('--dn') + '99' });
    }
    if (b.rsi != null) rS.update({ time: t, value: b.rsi });
  }
  function redrawChart() {
    const bars = chartBars(S.tf);
    const c = [], v = [], m = [], s = [], h = [], r = [];
    const up = css('--up') + '99', dn = css('--dn') + '99';
    for (const b of bars) {
      const t = barTime(b);
      c.push({ time: t, open: b.o, high: b.h, low: b.l, close: b.c });
      if (b.vw != null) v.push({ time: t, value: b.vw });
      if (b.macd != null && b.sig != null) { m.push({ time: t, value: b.macd }); s.push({ time: t, value: b.sig }); h.push({ time: t, value: b.macd - b.sig, color: b.macd >= b.sig ? up : dn }); }
      if (b.rsi != null) r.push({ time: t, value: b.rsi });
    }
    cS.setData(c); vS.setData(S.tf === '15m' ? [] : v); mS.setData(m); sS.setData(s); hS.setData(h); rS.setData(r);
    drawMarkers(); drawLevels();
    const n = c.length;
    if (n) chart.timeScale().setVisibleLogicalRange({ from: Math.max(0, n - (S.tf === '144t' ? 140 : 110)), to: n + 4 });
  }
  function barForTs(ts) {
    const bars = S.bars[S.tf];
    if (!bars.length) return null;
    if (S.tf === '144t') {
      let lo = 0, hi = bars.length - 1;
      if (bars[hi].end < ts) return null;
      while (lo < hi) { const mid = (lo + hi) >> 1; if (bars[mid].end >= ts) hi = mid; else lo = mid + 1; }
      return bars[lo];
    }
    const start = Math.floor(ts / TF_SEC[S.tf]) * TF_SEC[S.tf];
    for (let i = bars.length - 1; i >= 0; i--) { if (bars[i].t === start) return bars[i]; if (bars[i].t < start) break; }
    return null;
  }
  function drawMarkers() {
    const out = [];
    for (const mk of S.marks) {
      const b = barForTs(mk.ts);
      if (!b) continue;
      out.push({ time: barTime(b), position: mk.buy ? 'belowBar' : 'aboveBar', shape: mk.buy ? 'arrowUp' : 'arrowDown',
        color: mk.buy ? css('--up') : (mk.pnl >= 0 ? css('--accent') : css('--dn')), text: mk.text, size: 1 });
    }
    out.sort((a, b) => a.time - b.time);
    markersApi.setMarkers(out);
  }
  function drawLevels() {
    for (const l of levelLines) cS.removePriceLine(l);
    levelLines = [];
    const keep = new Set(['PDH', 'PDL', 'PDC', 'ORH', 'ORL']);
    for (const l of S.levels) {
      if (!keep.has(l.name)) continue;
      levelLines.push(cS.createPriceLine({ price: l.px, color: css('--ink-3'), lineWidth: 1, lineStyle: 3, axisLabelVisible: false, title: l.name }));
    }
  }

  /* ------------------------------------------------------------------ event reducer */
  let dirty = new Set(['all']);
  const mark = (...k) => k.forEach((x) => dirty.add(x));

  function loadSnapshot(sn) {
    S.mode = sn.mode; S.symbol = sn.symbol; S.ts = sn.ts; S.price = sn.price; S.vwap = sn.vwap;
    for (const tf of Object.keys(S.bars)) S.bars[tf] = (sn.bars?.[tf] || []).map((b) => ({ ...b }));
    S.signal = sn.signal; S.levels = sn.levels || []; setPrev();
    S.positions = new Map((sn.positions || []).map((p) => [p.pos.id, p.pos]));
    S.targets = new Map((sn.positions || []).map((p) => [p.pos.id, p.targets]));
    S.closed = sn.closed || []; S.skips = sn.skips || []; S.risk = sn.risk; S.agent = sn.agent || S.agent;
    S.crew = sn.crew && sn.crew.briefs ? sn.crew : S.crew; S.config = sn.config;
    S.conviction = sn.crew?.conviction?.checks?.length ? sn.crew.conviction : null;
    S.proposals = new Map((sn.crew?.proposals || []).map((p) => [p.id, p]));
    S.l2 = sn.l2 && sn.l2.book ? { book: sn.l2.book, mode: sn.l2.mode } : (sn.l2 ? { book: null, mode: sn.l2.mode } : null);
    const bk = sn.books;
    S.books = bk ? bk.books.map(({ open, closed, ...x }) => x) : [];
    S.account = bk?.account || null;
    S.combos = new Map((bk?.books || []).flatMap((b) => b.open || []).map((p) => [p.id, p]));
    S.bookClosed = (bk?.books || []).flatMap((b) => (b.closed || []).map((p) => ({ ...p, net: p.total_pnl ?? p.pnl })));
    S.marks = [];
    for (const p of [...S.closed, ...S.positions.values()]) for (const f of p.fills || []) addMark(f.side === 'buy', f.ts, f.qty, p, f.px);
    for (const p of [...S.bookClosed, ...S.combos.values()]) for (const f of p.fills || []) addComboMark(p, f.ts, f.side === 'open');
    if (office) office.onAgent(S.agent.activity, S.agent.text);
    mark('all', 'chart');
  }
  function setPrev() { const pdc = S.levels.find((l) => l.name === 'PDC'); if (pdc) S.prevClose = pdc.px; }
  function addMark(buy, ts, qty, pos, fillPx) {
    const strike = (pos.contract || '').split(' ')[1] || '';
    const text = 'A·' + (buy ? `B${qty} ${strike}` : `S${qty} ${fillPx != null && pos.entry ? ((fillPx / pos.entry - 1) * 100).toFixed(0) + '%' : ''}`);
    S.marks.push({ ts, buy, text, pnl: fillPx != null ? fillPx - pos.entry : 0 });
  }
  function addComboMark(p, ts, open) {
    // a credit structure opens with a sale (arrow down) and closes with a purchase (arrow up)
    S.marks.push({ ts, buy: p.credit === false ? open : !open, text: `${p.book} ${open ? 'open' : 'close'}`, pnl: open ? 0 : (p.total_pnl || 0) });
  }
  function feedPush(ts, type, text, klass) {
    S.feed.unshift({ ts, type, text, klass: klass || type });
    if (S.feed.length > 400) S.feed.length = 400;
    mark('feed');
  }

  function apply(e) {
    if (e.ts) S.ts = Math.max(S.ts, e.ts);
    switch (e.type) {
      case 'snapshot': loadSnapshot(e); break;
      case 'batch': e.events.forEach((x) => { try { apply(x); } catch (err) { console.warn('event skipped', x.type, err); mark('chart'); } }); break;
      case 'tick': S.price = e.price; S.vwap = e.vwap ?? S.vwap; safeChart(() => liveCandle(e)); mark('header'); break;
      case 'bar': {
        const b = { ...e.bar, vw: e.vwap };
        const arr = S.bars[b.tf];
        const t = barTime(b);
        if (!arr || typeof t !== 'number') break;
        let k = arr.length - 1;                 // keep the list in time order: a late bar goes in its place
        while (k >= 0 && barTime(arr[k]) > t) k--;
        if (k >= 0 && barTime(arr[k]) === t) arr[k] = b; else arr.splice(k + 1, 0, b);
        if (arr.length > 4000) arr.shift();
        S.price = b.c; S.vwap = e.vwap ?? S.vwap;
        if (b.tf === S.tf && !S.bulk) {
          if (barTime(arr[arr.length - 1]) === t) safeChart(() => pushBarToChart(b)); else mark('chart');
          const lm = S.marks[S.marks.length - 1];
          if (lm && lm.ts >= b.t - 1) safeChart(drawMarkers);
        }
        mark('header');
        break;
      }
      case 'signal': S.signal = { snap: e.snap, ok: e.ok, passed: e.passed, failed: e.failed }; if (e.levels) { S.levels = e.levels; setPrev(); if (!S.bulk) drawLevels(); } mark('signal'); break;
      case 'cross': break;
      case 'order': {
        const side = e.side === 'buy' ? 'buy' : 'sell';
        feedPush(e.ts, side, `${e.side.toUpperCase()} ${e.qty}× ${e.contract} lim ${px(e.limit)} → ${e.status}${e.filled ? ` ${e.filled}@${px(e.price)}` : ''}${e.review ? ' (reviewed by Robinhood)' : ''}`);
        break;
      }
      case 'position': {
        S.positions.set(e.pos.id, e.pos); S.targets.set(e.pos.id, e.targets);
        if (e.event === 'open') { addMark(true, e.ts, e.pos.qty_initial, e.pos); if (!S.bulk) drawMarkers(); feedPush(e.ts, 'buy', `Opened ${e.pos.qty}× ${e.pos.contract} @ ${px(e.pos.entry)} · ${e.pos.setup} · strike ${e.pos.strike_reason}`); }
        mark('pos', 'office');
        break;
      }
      case 'fill': {
        const p = S.positions.get(e.pos_id);
        if (p) { addMark(false, e.ts, e.qty, p, e.px); if (!S.bulk) drawMarkers(); }
        feedPush(e.ts, 'sell', `Sold ${e.qty} @ ${px(e.px)} · ${e.why} · ${money(e.pnl)}`);
        break;
      }
      case 'trade_closed': S.positions.delete(e.pos.id); S.closed.push({ ...e.pos, net: e.net }); S.risk = e.risk; mark('pos', 'trades', 'risk', 'header'); break;
      case 'risk': S.risk = e.risk; mark('risk', 'header'); break;
      case 'skip': S.skips.push(e); feedPush(e.ts, 'skip', `Passed on ${e.setup} (${e.tf} cross): ${e.why}`); mark('pos'); break;
      case 'agent': S.agent = { activity: e.activity, text: e.text, ts: e.ts }; if (office && !S.bulk) office.onAgent(e.activity, e.text); mark('office'); break;
      case 'crew':
        if (e.phase === 'done' && e.brief) { S.crew.briefs[e.desk] = e.brief; mark('crew'); }
        if (e.phase === 'say') {
          const nm = (k) => (k === 'agent' ? 'Agent' : k === 'all' ? 'all' : DESK_META[k] ? DESK_META[k][0] : k);
          const from = e.who === 'agent' ? 'agent' : e.desk;
          feedPush(e.ts, 'crew', `${nm(from)}${e.to && e.to !== 'agent' || from === 'agent' && e.to ? ' → ' + nm(e.to) : ''}: ${e.text}`);
        }
        if (e.phase === 'say' && e.desk === 'tape') { S.crew.briefs.tape = { headline: e.text, ts: e.ts, slot: 'book', bias: '' }; mark('crew'); }
        if (office && !S.bulk) office.onCrew(e);
        break;
      case 'conviction': S.conviction = { mult: e.mult, checks: e.checks, setup: e.setup, ts: e.ts }; mark('risk'); break;
      case 'proposal': S.proposals.set(e.item.id, e.item); feedPush(e.ts, 'crew', `Proposal (${e.item.scope}, ${e.item.status}): ${e.item.title}`); mark('props'); break;
      case 'l2': S.l2 = { book: e.book, mode: e.mode, gate_ok: e.gate_ok, gate_why: e.gate_why }; mark('l2'); break;
      case 'directive': S.crew.directive = e.directive; S.risk = e.risk || S.risk; mark('crew', 'risk'); break;
      case 'log': feedPush(e.ts, 'log', e.msg); break;
      case 'books': S.books = e.books; S.account = e.account; mark('books', 'header'); break;
      case 'book_position':
        S.combos.set(e.pos.id, { ...(S.combos.get(e.pos.id) || {}), ...e.pos });
        if (e.event === 'open') {
          addComboMark(e.pos, e.ts, true); if (!S.bulk) drawMarkers();
          feedPush(e.ts, 'buy', `Book ${e.pos.book} opened ${e.pos.qty}× ${e.pos.contract} for ${px(e.pos.entry)} ${e.pos.credit ? 'credit' : 'debit'}${e.size_note ? ' · ' + e.size_note : ''}`);
        }
        mark('pos', 'office', 'header'); break;
      case 'book_closed':
        S.combos.delete(e.pos.id); S.bookClosed.push({ ...e.pos, net: e.net }); addComboMark(e.pos, e.ts, false); if (!S.bulk) drawMarkers();
        feedPush(e.ts, 'sell', `Book ${e.pos.book} closed ${e.pos.contract} · ${e.pos.exit_reason} · ${money(e.net)}`);
        mark('pos', 'trades', 'header'); break;
      case 'book_order': {
        // combo books send open/close with mid and natural; book F sends buy/sell with a symbol and no mid
        const quote = e.mid != null || e.natural != null ? ` (mid ${px(e.mid)}, natural ${px(e.natural)})` : '';
        feedPush(e.ts, e.action === 'open' || e.action === 'buy' ? 'buy' : 'sell', `Book ${e.book} ${e.action.toUpperCase()} ${e.qty}×${e.symbol ? ' ' + e.symbol : ''} lim ${px(e.limit)}${quote} → ${e.status}${e.filled ? ` @ ${px(e.price)}` : ''}${e.review ? ' (reviewed by Robinhood)' : ''}`);
        break;
      }
      case 'f_position':          // book F holds shares: its positions ride with the combos for the strip, position card and trades
        if (e.event === 'closed') {
          S.combos.delete(e.pos.id); S.bookClosed.push({ ...e.pos, net: e.pos.pnl });
          feedPush(e.ts, 'sell', `Book ${e.pos.book} closed ${e.pos.symbol} · ${e.pos.exit_reason} · ${money(e.pos.pnl, 2)}`);
          mark('pos', 'trades', 'header');
        } else {
          S.combos.set(e.pos.id, e.pos);
          if (e.event === 'open') feedPush(e.ts, 'buy', `Book ${e.pos.book} bought ${e.pos.qty} ${e.pos.symbol} @ ${px(e.pos.entry)} · stop ${px(e.pos.stop)}`);
          mark('pos', 'office', 'header');
        }
        break;
      case 'f_positions': for (const p of e.positions || []) S.combos.set(p.id, p); mark('pos', 'header'); break;
      case 'book_skip': feedPush(e.ts, 'skip', `Book ${e.book} passed: ${e.why}`); mark('pos'); break;
      case 'session': for (const tf of Object.keys(S.bars)) S.bars[tf] = []; S.closed = []; S.bookClosed = []; S.combos = new Map(); S.marks = []; mark('all', 'chart'); break;
      case 'final': break;
    }
  }

  function liveCandle(e) {
    const sec = TF_SEC[S.tf];
    if (!sec || S.bulk || !e.price) return;
    const arr = S.bars[S.tf], start = Math.floor(e.ts / sec) * sec;
    const last = arr[arr.length - 1];
    if (last && start <= last.t) return;
    cS.update({ time: start, open: last ? last.c : e.price, high: Math.max(e.price, last ? last.c : e.price), low: Math.min(e.price, last ? last.c : e.price), close: e.price });
  }

  /* ------------------------------------------------------------------ books */
  const aOpenPnl = () => [...S.positions.values()].reduce((a, p) => a + (p.unrealized || 0), 0);
  const combosOf = (k) => [...S.combos.values()].filter((p) => p.book === k);
  const isShares = (p) => p.book === 'F1' || p.book === 'F';     // F: journal rows from before F1
  const pnlPct = (p) => (p.pnl_pct != null ? Number(p.pnl_pct) : p.entry && p.mark != null ? (p.mark / p.entry - 1) * 100 : 0);
  const posName = (p) => (isShares(p) ? `${p.qty}× ${p.symbol}` : p.setup);
  function bookPnl(k) {
    if (k === 'A') return (S.risk?.day_pnl || 0) + aOpenPnl();
    const b = S.books.find((x) => x.book === k);
    return (b?.day_pnl || 0) + combosOf(k).reduce((a, p) => a + (p.unrealized || 0) - (p.fees || 0), 0);
  }
  function bookWL(k) {
    if (k === 'A') return [S.risk?.wins || 0, S.risk?.losses || 0];
    const b = S.books.find((x) => x.book === k);
    return [b?.wins || 0, b?.losses || 0];
  }
  function selectedBooks() {
    if (!S.books.length) return ['A'];
    return S.activeBook === 'ALL' ? S.books.map((b) => b.book) : [S.activeBook];
  }
  function renderBooks() {
    const el = $('books');
    el.hidden = !S.books.length;
    if (!S.books.length) return;
    const total = S.books.reduce((a, b) => a + bookPnl(b.book), 0);
    const chips = [['ALL', `<b>ALL</b><span class="num ${cls(total)}">${money(total)}</span>`, false]].concat(S.books.map((b) => {
      const v = bookPnl(b.book);
      const halted = b.book === 'A' ? S.risk?.halted : b.halted;
      const [n, mx] = b.book === 'A' && S.risk ? [S.risk.trades, S.risk.max_trades] : [b.trades, b.max_trades];
      return [b.book, `<b>${b.book}</b><span class="num ${cls(v)}">${money(v)}</span><small>${n}/${mx}</small>${halted ? '<i class="hd" title="halted"></i>' : ''}`, halted];
    }));
    const keys = chips.map((c) => c[0]).join(',');
    if (el.dataset.keys !== keys) {       // build the buttons once; later renders update them in place so clicks land
      el.dataset.keys = keys;
      el.innerHTML = chips.map(([k]) => `<button type="button" role="tab" data-book="${k}"></button>`).join('');
      el.onclick = (ev) => { const x = ev.target.closest('[data-book]'); if (x) { S.activeBook = x.dataset.book; mark('all'); } };
    }
    chips.forEach(([k, html, halted], i) => {
      const btn = el.children[i];
      if (btn.innerHTML !== html) btn.innerHTML = html;
      btn.setAttribute('aria-selected', String(S.activeBook === k));
      btn.classList.toggle('halted', !!halted);
    });
  }

  /* ------------------------------------------------------------------ renderers */
  function renderHeader() {
    $('mode').textContent = CFG.source === 'replay' ? 'DEMO REPLAY' : CFG.source === 'review' ? `REVIEW · ${S.mode.toUpperCase()}` : S.mode.toUpperCase();
    $('mode').className = 'pill mode ' + (CFG.source === 'ws' ? S.mode : CFG.source);
    $('s-px').textContent = px(S.price);
    const chg = S.price && S.prevClose ? S.price - S.prevClose : null;
    $('s-chg').textContent = chg == null ? '' : `${chg >= 0 ? '+' : '−'}${Math.abs(chg).toFixed(2)} (${(chg / S.prevClose * 100).toFixed(2)}%)`;
    $('s-chg').className = 'd num ' + cls(chg);
    $('s-vwap').textContent = px(S.vwap);
    const sel = selectedBooks();
    const pnl = sel.reduce((a, k) => a + bookPnl(k), 0);
    $('s-pnl').textContent = money(pnl); $('s-pnl').className = 'v num ' + cls(pnl);
    const wl = sel.reduce((a, k) => { const w = bookWL(k); return [a[0] + w[0], a[1] + w[1]]; }, [0, 0]);
    $('s-wl').textContent = `${wl[0]} / ${wl[1]}`;
    $('s-clock').textContent = hms(S.ts);
    const paused = S.risk?.paused;
    $('btn-pause').textContent = paused ? 'Resume entries' : 'Pause entries';
  }

  const ROLE = { '15m': 'Filter', '5m': 'Filter', '1m': 'Trigger', '144t': 'Trigger' };
  function renderSignal() {
    const sig = S.signal;
    const tb = $('matrix');
    if (!sig || !sig.snap) { tb.innerHTML = '<tr><td colspan="5" class="role">Waiting for the first bars…</td></tr>'; return; }
    const win = S.config?.strategy?.confirm_window_sec || 120;
    tb.innerHTML = ['15m', '5m', '1m', '144t'].map((tf) => {
      const s = sig.snap[tf] || {};
      const st = !s.ready ? 'na' : s.bull ? 'bull' : 'bear';
      const fresh = s.cross_up_ts && S.ts - s.cross_up_ts <= win && ROLE[tf] === 'Trigger' ? '<span class="fresh">fresh ↑</span>' : '';
      const r = s.rsi;
      const rsi = r == null ? '—' : `<span class="rsi-cell"><span class="rsi-bar"><i style="left:calc(${Math.max(0, Math.min(100, r))}% - 1px)"></i></span><span class="num">${r.toFixed(0)}</span></span>`;
      return `<tr><td>${tf}</td><td class="role">${ROLE[tf]}</td>
        <td><span class="state ${st}"><span class="dot"></span>${st === 'na' ? 'warming' : st === 'bull' ? 'Above' : 'Below'}</span>${fresh}</td>
        <td class="r num">${s.hist == null ? '—' : (s.hist >= 0 ? '+' : '') + s.hist.toFixed(3)}</td><td class="r">${rsi}</td></tr>`;
    }).join('');
    const gate = $('gate');
    const failed = sig.failed || [];
    if (S.risk?.halted) { gate.textContent = 'Halted'; gate.className = 'pill bad'; }
    else if (sig.ok) { gate.textContent = 'Armed'; gate.className = 'pill good'; }
    else if (failed.length === 1 && /fresh/.test(failed[0])) { gate.textContent = 'Set up · waiting for cross'; gate.className = 'pill acc'; }
    else { gate.textContent = `Blocked · ${failed.length}`; gate.className = 'pill bad'; }
    $('conds').innerHTML = failed.map((f) => `<li class="no">${esc(f)}</li>`).join('') + (sig.passed || []).map((p) => `<li class="ok">${esc(p)}</li>`).join('');
  }

  function renderPosition() {
    const act = S.activeBook;
    if (act !== 'ALL' && act !== 'A') return renderCombo(combosOf(act)[0], act);
    const combos = [...S.combos.values()];
    if (act === 'ALL' && !S.positions.size && combos.length) return renderCombo(combos[0], combos[0].book);
    renderAPosition(act === 'ALL' ? combos.map((c) => (isShares(c)
      ? `<p class="note">Book ${esc(c.book)} · ${esc(posName(c))} · entry ${px(c.entry)} · mark ${px(c.mark)} · <span class="${cls(c.unrealized)}">${money(c.unrealized)}</span></p>`
      : `<p class="note">Book ${esc(c.book)} · ${esc(c.setup)} · ${c.credit ? 'credit' : 'debit'} ${px(c.entry)} · mark ${px(c.mark)} · <span class="${cls(c.total_pnl)}">${money(c.total_pnl)}</span></p>`)).join('') : '');
  }

  function renderCombo(p, k) {
    const body = $('pos-body'), pill = $('pos-setup');
    const b = S.books.find((x) => x.book === k) || {};
    if (!p) {
      pill.textContent = `Book ${k} · Flat`; pill.className = 'pill';
      const last = S.bookClosed.filter((x) => x.book === k).slice(-1)[0];
      body.innerHTML = `<p class="note">Book ${esc(k)} (${esc(b.name || '')}) has no open position.${b.halted ? ` <b>Halted: ${esc(b.halt_reason)}</b>.` : ''}${b.blocked ? ` <b>${esc(b.blocked)}</b>.` : ''}</p>`
        + (b.last_skip ? `<p class="note">Last pass <b>${hm(b.last_skip.ts)}</b>: ${esc(b.last_skip.why)}</p>` : '')
        + (last ? `<p class="note">Last trade: <b>${esc(last.contract ?? last.symbol)}</b> <span class="${cls(last.net)}">${money(last.net)}</span> · ${esc(last.exit_reason)}</p>` : '');
      return;
    }
    if (isShares(p)) return renderShares(p);
    pill.textContent = `Book ${p.book} · ${p.setup}`; pill.className = 'pill acc';
    const legs = (p.legs || []).map((l) => `<tr><td>${l.side === 'sell' ? 'Short' : 'Long'}</td><td class="r num">${l.ratio > 1 ? l.ratio + '× ' : ''}${Number(l.strike).toFixed(0)}</td><td>${l.right}</td></tr>`).join('');
    const priced = p.stop > 0;
    body.innerHTML = `
      <div class="pos-title"><span class="c">${p.qty}× ${esc(p.setup)}</span><span class="p ${cls(p.pnl_pct)}">${p.pnl_pct >= 0 ? '+' : ''}${Number(p.pnl_pct).toFixed(1)}%</span></div>
      <table class="legs" aria-label="Legs"><tbody>${legs}</tbody></table>
      <div class="kv">
        <div><span class="k">${p.credit ? 'Credit' : 'Debit'}</span><span class="v">${px(p.entry)}</span></div>
        <div><span class="k">Mark</span><span class="v">${px(p.mark)}</span></div>
        <div><span class="k">Max loss</span><span class="v">${money(-p.max_loss)}</span></div>
        ${priced ? `<div><span class="k">Take profit</span><span class="v pos">${px(p.target)}</span></div>
        <div><span class="k">Stop</span><span class="v neg">${px(p.stop)}</span></div>` : ''}
        <div><span class="k">P&amp;L</span><span class="v ${cls(p.total_pnl)}">${money(p.total_pnl)}</span></div>
      </div>
      <p class="note">${esc(p.strike_reason)}.</p>
      ${p.meta?.plan ? `<p class="note">Exit plan${priced ? '' : ' (exits on SPY)'}: <b>${esc(p.meta.plan)}</b></p>` : ''}`;
  }

  function renderShares(p) {        // book F: long whole shares, one card per name; the rest are listed underneath
    const body = $('pos-body'), pill = $('pos-setup');
    const others = combosOf(p.book).filter((x) => x.id !== p.id);
    const pct = pnlPct(p);
    pill.textContent = `Book ${p.book} · ${combosOf(p.book).length} open`; pill.className = 'pill acc';
    body.innerHTML = `
      <div class="pos-title"><span class="c">${esc(posName(p))}</span><span class="p ${cls(pct)}">${pct >= 0 ? '+' : ''}${pct.toFixed(1)}%</span></div>
      <div class="kv">
        <div><span class="k">Entry</span><span class="v">${px(p.entry)}</span></div>
        <div><span class="k">Mark</span><span class="v">${px(p.mark)}</span></div>
        <div><span class="k">Stop</span><span class="v neg">${px(p.stop)}</span></div>
        <div><span class="k">Risk</span><span class="v">${money(-p.risk, 2)}</span></div>
        <div><span class="k">R</span><span class="v ${cls(p.r)}">${p.r == null ? '—' : (p.r >= 0 ? '+' : '') + Number(p.r).toFixed(2)}</span></div>
        <div><span class="k">P&amp;L</span><span class="v ${cls(p.unrealized)}">${money(p.unrealized, 2)}</span></div>
      </div>
      <p class="note">Long shares, paper only. Exits at the stop or at the day's exit time.</p>
      ${others.map((x) => `<p class="note">Also open: <b>${esc(posName(x))}</b> · <span class="${cls(x.unrealized)}">${money(x.unrealized, 2)}</span></p>`).join('')}`;
  }

  function renderAPosition(extra) {
    const body = $('pos-body'), pill = $('pos-setup');
    const p = [...S.positions.values()][0];
    if (!p) {
      pill.textContent = 'Flat'; pill.className = 'pill';
      const sk = S.skips[S.skips.length - 1];
      const lastT = S.closed[S.closed.length - 1];
      body.innerHTML = `<p class="note">No open position. The engine enters on a fresh 1m or 144t MACD cross with the 15m and 5m filters above signal and RSI inside 30–70.</p>` +
        (sk ? `<p class="note">Last pass <b>${hm(sk.ts)}</b>: ${esc(sk.why)}</p>` : '') +
        (lastT ? `<p class="note">Last trade: <b>${esc(lastT.contract)}</b> <span class="${cls(lastT.net)}">${money(lastT.net)}</span> · ${esc(lastT.exit_reason)}</p>` : '') + extra;
      return;
    }
    const tg = S.targets.get(p.id) || [];
    pill.textContent = p.setup + (p.ripping ? ' · RIPPING' : ''); pill.className = 'pill ' + (p.ripping ? 'good' : 'acc');
    const pnl = p.realized + p.unrealized;
    const lo = Math.min(p.stop, p.mark, p.entry) * 0.97, hi = Math.max(...tg, p.peak, p.mark) * 1.03;
    const pos = (v) => ((v - lo) / (hi - lo) * 100).toFixed(1) + '%';
    const fillL = Math.min(p.mark, p.entry), fillR = Math.max(p.mark, p.entry);
    const ex = S.config?.exits?.[p.setup === 'SWING' ? 'swing' : 'scalp'];
    body.innerHTML = `
      <div class="pos-title"><span class="c">${p.qty}× ${esc(p.contract)}</span><span class="p ${cls(p.pnl_pct)}">${p.pnl_pct >= 0 ? '+' : ''}${p.pnl_pct.toFixed(1)}%</span></div>
      <div class="ladder" aria-label="Premium ladder: stop, entry, targets, current mark">
        <div class="track"></div>
        <div class="fill" style="left:${pos(fillL)};width:calc(${pos(fillR)} - ${pos(fillL)});background:${p.mark >= p.entry ? 'var(--up)' : 'var(--dn)'};opacity:.55"></div>
        <div class="tick stop" style="left:${pos(p.stop)}"></div><div class="lbl" style="left:${pos(p.stop)}">${px(p.stop)}</div>
        <div class="tick entry" style="left:${pos(p.entry)}"></div>
        ${tg.map((t, i) => `<div class="tick tgt" style="left:${pos(t)}"></div><div class="lbl" style="left:${pos(t)}">T${i + 1}</div>`).join('')}
        <div class="mark" style="left:${pos(p.mark)}"></div>
      </div>
      <div class="kv">
        <div><span class="k">Qty</span><span class="v">${p.qty}/${p.qty_initial}</span></div>
        <div><span class="k">Entry</span><span class="v">${px(p.entry)}</span></div>
        <div><span class="k">Mark</span><span class="v">${px(p.mark)}</span></div>
        <div><span class="k">Stop</span><span class="v neg">${px(p.stop)}</span></div>
        <div><span class="k">Peak</span><span class="v">${px(p.peak)}</span></div>
        <div><span class="k">P&amp;L</span><span class="v ${cls(pnl)}">${money(pnl)}</span></div>
      </div>
      <p class="note">Strike ${esc(p.strike_reason)}.</p>
      ${ex ? `<p class="note">Exit plan: <b>${ex.exit_on_cross_back}</b> cross-back · scale ${ex.scale_outs.map((s) => `${Math.round(s.fraction * 100)}% at +${Math.round(s.at * 100)}%`).join(', ')} · runner trails ${Math.round(ex.runner_trail_pct * 100)}% off peak · time stop ${ex.time_stop_min}m${p.ripping ? ' · <b>runner holding for the 5m cross</b>' : ''}</p>` : ''}${extra}`;
  }

  function renderRisk() {
    const r = S.risk, body = $('risk-body'), pill = $('risk-state');
    if (!r) { body.innerHTML = ''; return; }
    const now = S.ts;
    const cooling = r.cooldown_until && r.cooldown_until > now;
    const inBlack = (r.blackouts || []).find((b) => b.start <= now && now < b.end);
    if (r.halted) { pill.textContent = 'Halted'; pill.className = 'pill bad'; }
    else if (r.paused) { pill.textContent = 'Paused'; pill.className = 'pill warn'; }
    else if (cooling) { pill.textContent = 'Cooldown'; pill.className = 'pill warn'; }
    else if (inBlack) { pill.textContent = 'Blackout'; pill.className = 'pill warn'; }
    else { pill.textContent = 'Normal'; pill.className = 'pill good'; }
    const used = Math.max(0, -r.day_pnl) / r.max_daily_loss;
    body.innerHTML = `
      <div class="kv">
        <div><span class="k">Realized</span><span class="v ${cls(r.day_pnl)}">${money(r.day_pnl)}</span></div>
        <div><span class="k">Trades</span><span class="v">${r.trades}/${r.max_trades}</span></div>
        <div><span class="k">Size</span><span class="v">${Math.round(r.size_mult * 100)}%</span></div>
      </div>
      <div><div class="meter" role="meter" aria-valuemin="0" aria-valuemax="${r.max_daily_loss}" aria-valuenow="${Math.max(0, -r.day_pnl).toFixed(0)}" aria-label="Daily loss used"><i style="width:${Math.min(100, used * 100).toFixed(1)}%;background:${used > 0.75 ? 'var(--dn)' : used > 0.4 ? 'var(--warn)' : 'var(--ink-3)'}"></i></div>
      <p class="note" style="margin-top:4px">Loss limit used: <b>$${Math.max(0, -r.day_pnl).toFixed(0)}</b> of $${r.max_daily_loss}${r.peak_day_pnl > 0 ? ` · peak day +$${r.peak_day_pnl.toFixed(0)}` : ''}</p></div>
      ${r.halted ? `<p class="note"><b>${esc(r.halt_reason)}</b>. No new entries until restart.</p>` : ''}
      ${cooling ? `<p class="note">${r.loss_streak} losses in a row: cooling off until <b>${hm(r.cooldown_until)}</b>.</p>` : ''}
      ${(r.blackouts || []).length ? `<ul class="bl">${r.blackouts.map((b) => `<li>Blackout <b>${hm(b.start)}–${hm(b.end)}</b> ${esc(b.name)}</li>`).join('')}</ul>` : ''}
      ${gateHtml()}`;
  }

  function renderBook() {
    const body = $('book-body'), pill = $('book-state');
    const L = S.l2;
    if (!L || !L.book) { pill.textContent = L ? (L.mode || 'off').toUpperCase() : 'Off'; pill.className = 'pill'; return; }
    const b = L.book;
    const mode = (L.mode || 'observe').toUpperCase();
    if (L.gate_ok === false) { pill.textContent = `${mode} · against`; pill.className = 'pill warn'; }
    else { pill.textContent = `${mode} · ok`; pill.className = 'pill good'; }
    const asks = (b.asks || []).slice(0, 5), bids = (b.bids || []).slice(0, 5);
    const mx = Math.max(1, ...asks.map((x) => x[1]), ...bids.map((x) => x[1]));
    const wallA = b.ask_wall ? b.ask_wall[0] : null, wallB = b.bid_wall ? b.bid_wall[0] : null;
    const row = (lv, side) => `<span class="px ${side}">${lv[0].toFixed(2)}</span><span class="bar ${side}${(side === 'ask' ? wallA : wallB) === lv[0] ? ' wall' : ''}" style="width:${Math.max(3, lv[1] / mx * 100).toFixed(0)}%"></span><span class="sz">${lv[1] >= 1000 ? (lv[1] / 1000).toFixed(1) + 'k' : lv[1].toFixed(0)}</span>`;
    const imb = b.imbalance || 0;
    const left = imb >= 0 ? 50 : 50 + imb * 50, width = Math.abs(imb) * 50;
    body.innerHTML = `
      <div class="ladder2">${asks.slice().reverse().map((x) => row(x, 'ask')).join('')}
        <span class="mid">spread ${((b.ask - b.bid) * 100).toFixed(0)}c · mid ${b.mid.toFixed(3)}</span>
        ${bids.map((x) => row(x, 'bid')).join('')}</div>
      <div class="kv">
        <div><span class="k">Imbalance</span><span class="v ${cls(imb)}">${imb >= 0 ? '+' : ''}${imb.toFixed(2)}</span></div>
        <div><span class="k">Microprice</span><span class="v ${cls(b.micro_edge_c)}">${b.micro_edge_c >= 0 ? '+' : ''}${b.micro_edge_c.toFixed(1)}c</span></div>
        <div><span class="k">Ask wall</span><span class="v">${b.ask_wall ? b.ask_wall[0].toFixed(2) : '—'}</span></div>
      </div>
      <div class="imb" aria-label="Near-book imbalance, bids minus asks"><i style="left:${left}%;width:${width}%;background:${imb >= 0 ? 'var(--up)' : 'var(--dn)'}"></i><b></b></div>
      <p class="note">${L.gate_ok === false ? `Book leans against a call entry: <b>${esc(L.gate_why)}</b>. ` : ''}${mode === 'OBSERVE' ? 'Observe mode: logged on every entry, never blocks.' : mode === 'ENFORCE' ? 'Enforce mode: can veto call entries.' : ''}</p>`;
  }

  function gateHtml() {
    const c = S.conviction;
    if (!c) return '<p class="note">Size-up gate: checked on each entry. All items must pass for up to 125% size.</p>';
    const pass = c.checks.filter((x) => x.ok).length;
    return `<p class="note"><b>Size-up gate</b> at ${hm(c.ts)} (${esc(c.setup)} entry): ${c.mult > 1 ? `<span class="pos">PASSED, size ${Math.round(c.mult * 100)}%</span>` : `${pass}/${c.checks.length} checks, no size-up`}</p>
      <ul class="gate">${c.checks.map((x) => `<li class="${x.ok ? 'ok' : 'no'}"><b>${esc(x.name)}</b><span>${esc(x.detail || '')}</span></li>`).join('')}</ul>`;
  }

  function renderProps() {
    const items = [...S.proposals.values()].sort((a, b) => (a.status === 'pending' ? -1 : 0) - (b.status === 'pending' ? -1 : 0) || b.ts - a.ts);
    const pend = items.filter((p) => p.status === 'pending').length;
    $('props-state').textContent = pend ? `${pend} awaiting you` : items.length ? 'Up to date' : 'None';
    $('props-state').className = 'pill ' + (pend ? 'acc' : '');
    const demo = READ_ONLY;
    const scopeLbl = { trade: 'Next trade', day: 'Today', standing: 'Standing change', new_strategy: 'New strategy' };
    $('props').innerHTML = items.slice(0, 8).map((p) => `<li class="${p.status === 'pending' ? 'pending' : ''}">
      <div class="ph"><span class="pt">${esc(p.title)}</span><span class="pill ${p.status === 'applied' ? 'good' : p.status === 'pending' ? 'acc' : ''}">${scopeLbl[p.scope] || p.scope}</span></div>
      <span class="pm">${esc(DESK_META[p.desk] ? DESK_META[p.desk][0] : p.desk)} · ${hm(p.ts)} · ${esc(p.status)}</span>
      ${p.rationale ? `<span class="pm">${esc(p.rationale)}</span>` : ''}
      ${Object.keys(p.params || {}).length ? `<span class="pm">${Object.entries(p.params).map(([k, v]) => `<code>${esc(k)} = ${esc(JSON.stringify(v))}</code>`).join(' ')}</span>` : ''}
      ${p.spec ? `<span class="pm"><b>Rules:</b> ${esc(p.spec)}</span>` : ''}
      ${p.evidence ? `<span class="pm"><b>Evidence:</b> ${esc(p.evidence)}</span>` : ''}
      ${p.status === 'pending' ? `<div class="pa"><button class="btn" data-approve="${p.id}" ${demo ? `disabled title="${CFG.source === 'review' ? 'Approvals work while the engine runs' : 'Approvals work in the local app'}"` : ''}>Approve</button><button class="btn" data-reject="${p.id}" ${demo ? 'disabled' : ''}>Reject</button></div>` : ''}
    </li>`).join('') || '<li class="pm">Desks can pitch per-trade or per-day tweaks (applied within set limits), standing changes and new strategies (both wait for you).</li>';
    $('props').querySelectorAll('[data-approve]').forEach((b) => b.onclick = () => post(`/api/proposals/${b.dataset.approve}/approve`));
    $('props').querySelectorAll('[data-reject]').forEach((b) => b.onclick = () => post(`/api/proposals/${b.dataset.reject}/reject`));
  }

  function renderCrew() {
    const ul = $('crew');
    $('crew-mode').textContent = S.crew.offline === false ? 'live research' : CFG.source === 'replay' || S.mode === 'sim' ? 'simulated' : 'offline';
    $('crew-mode').className = 'pill ' + (S.crew.offline === false ? 'acc' : 'muted');
    ul.innerHTML = DESK_ORDER.map((k) => {
      const b = S.crew.briefs[k];
      const [name, color] = DESK_META[k];
      const bias = b?.bias || '';
      const bc = bias === 'bullish' ? 'good' : bias === 'bearish' ? 'bad' : '';
      return `<li><span class="sq" style="background:${color}"></span><span class="who">${name}</span>
        <span class="hl">${b ? esc(b.headline) : '<span style="color:var(--ink-3)">No brief yet</span>'}${b ? `<small>${hm(b.ts)} · ${esc(b.slot || '')}${b.size_multiplier != null && k !== 'tape' ? ` · <span class="vote">vote ${b.size_multiplier.toFixed(2)}×</span>` : ''}</small>` : ''}</span>
        ${bias ? `<span class="pill bias ${bc}">${bias}</span>` : '<span></span>'}</li>`;
    }).join('');
  }

  function renderTrades() {
    const act = S.activeBook;
    const rows = [...S.closed.map((p) => ({ ...p, book: 'A' })), ...S.bookClosed]
      .filter((p) => act === 'ALL' || p.book === act)
      .sort((a, b) => (b.closed_ts || 0) - (a.closed_ts || 0));
    $('trades-empty').hidden = rows.length > 0;
    $('trades').innerHTML = rows.map((p) => `<tr>
      <td><b>${esc(p.book)}</b></td><td class="num">${hm(p.opened_ts)}</td><td class="num">${esc(p.contract ?? p.symbol ?? '')}</td><td>${esc(p.setup ?? (p.symbol ? 'SHARES' : ''))}</td>
      <td class="r">${p.qty_initial ?? p.qty ?? ''}</td><td class="r">${px(p.entry)}</td><td class="r">${px(p.peak)}</td>
      <td class="r ${cls(p.net ?? p.total_pnl ?? p.pnl)}">${money(p.net ?? p.total_pnl ?? p.pnl)}</td><td>${esc(p.exit_reason)}</td><td class="wrap">${esc(p.strike_reason ?? '')}</td></tr>`).join('');
  }

  function renderFeed() {
    $('feed').innerHTML = S.feed.slice(0, 200).map((f) => `<li><span class="t">${hms(f.ts)}</span><span class="ty ${f.klass}">${f.type}</span><span>${esc(f.text)}</span></li>`).join('');
  }

  function renderOffice() {
    if (!office) return;
    const act = S.activeBook;
    const aPos = act === 'ALL' || act === 'A' ? [...S.positions.values()][0] : null;
    const p = aPos || (act === 'ALL' ? [...S.combos.values()][0] : combosOf(act)[0]);
    const lbl = !p ? null : aPos ? `${S.books.length ? 'A ' : ''}${p.qty}X ${p.contract.split(' ')[1]}` : `${p.book} ${isShares(p) ? `${p.qty}X ${p.symbol}` : `${p.qty}X ${p.setup}`}`;
    const pct = p ? pnlPct(p) : null;
    office.setMarket({
      price: S.price, prev: S.prevClose, pnl: selectedBooks().reduce((a, k) => a + bookPnl(k), 0), ts: S.ts || Date.now() / 1000, open: isRTH(S.ts),
      closes: S.bars['1m'].slice(-60).map((b) => b.c), pos: p ? `${lbl} ${pct >= 0 ? '+' : ''}${pct.toFixed(0)}%` : null,
      posPct: pct, halted: !!S.risk?.halted, imb: S.l2?.book?.imbalance ?? null,
    });
    $('office-status').textContent = office.current || office.queue.length ? 'IN A HUDDLE' : (ACT_LABEL[S.agent.activity] || String(S.agent.activity || '').toUpperCase());
  }

  let lastPanels = 0;
  function render(now) {
    renderOffice();
    if (now - lastPanels < 120 && !dirty.has('all') && !dirty.has('chart')) return;
    lastPanels = now;
    const all = dirty.has('all');
    if (dirty.has('chart') && !S.bulk) { dirty.delete('chart'); try { redrawChart(); } catch (err) { console.error('chart redraw failed', err); } }
    if (all || dirty.has('header')) renderHeader();
    if (all || dirty.has('books') || dirty.has('header')) renderBooks();
    if (all || dirty.has('signal')) renderSignal();
    if (all || dirty.has('pos') || dirty.has('header')) renderPosition();
    if (all || dirty.has('risk') || dirty.has('header')) renderRisk();
    if (all || dirty.has('crew')) renderCrew();
    if (all || dirty.has('l2')) renderBook();
    if (all || dirty.has('props')) renderProps();
    if (all || dirty.has('trades')) renderTrades();
    if (all || dirty.has('feed')) renderFeed();
    dirty = new Set();
  }

  /* ------------------------------------------------------------------ sources */
  function connectWS() {
    let backoff = 1000;
    const open = () => {
      const ws = new WebSocket((location.protocol === 'https:' ? 'wss://' : 'ws://') + location.host + '/ws');
      ws.onopen = () => { banner(null); backoff = 1000; };
      ws.onmessage = (m) => { apply(JSON.parse(m.data)); };
      ws.onclose = () => { banner(`<b>Disconnected from the engine.</b> Retrying in ${Math.round(backoff / 1000)}s…`); setTimeout(open, backoff); backoff = Math.min(15000, backoff * 2); };
    };
    open();
  }

  class Replay {
    constructor(events) {
      this.ev = events; this.speed = 60; this.playing = true; this.reset();
      $('rp-play').onclick = () => { this.playing = !this.playing; $('rp-play').textContent = this.playing ? 'Pause' : 'Play'; };
      $('rp-speed').onchange = (e) => { this.speed = Number(e.target.value); };
      $('rp-restart').onclick = () => this.restart();
    }
    reset() { this.i = 0; this.clock = this.ev[0].ts; }
    restart() {
      // ts too: the clock only moves forward, so a kept end-of-day time froze the header clock after a restart
      Object.assign(S, { ts: 0, bars: { '144t': [], '1m': [], '5m': [], '15m': [] }, positions: new Map(), targets: new Map(), closed: [], skips: [], marks: [], feed: [], crew: { briefs: {}, offline: true, directive: null }, signal: null, risk: null, l2: null, conviction: null, proposals: new Map(), books: [], account: null, combos: new Map(), bookClosed: [] });
      if (office) office.queue.length = 0;     // don't replay the rest of a huddle from later in the day
      this.holding = false;
      this.reset(); this.seek(this.startTs, true); this.playing = true; $('rp-play').textContent = 'Pause';
    }
    seek(ts, animateLast) {
      S.bulk = true;
      while (this.i < this.ev.length && this.ev[this.i].ts <= ts) apply(this.ev[this.i++]);
      S.bulk = false; this.clock = ts;
      if (office) office.onAgent(S.agent.activity, S.agent.text);
      mark('all', 'chart');
    }
    step(dt) {
      if (!this.playing || this.i >= this.ev.length) return;
      if (this.holding && office.busy()) return;
      this.holding = false;
      const next = this.ev[this.i].ts;
      const idle = !isRTH(this.clock) && S.positions.size === 0;
      this.clock += dt * this.speed * (idle ? 6 : 1);
      if (idle && next - this.clock > 600) this.clock = next - 5;      // skip long dead air
      while (this.i < this.ev.length && this.ev[this.i].ts <= this.clock) {
        const e = this.ev[this.i++];
        apply(e);
        if (e.type === 'crew' && e.phase === 'walk') this.holding = true;
      }
      S.ts = Math.max(S.ts, this.clock);
      mark('header');
    }
  }

  /* review: the last saved session (python -m agentdesk review), read-only */
  const dayFmt = new Intl.DateTimeFormat('en-US', { timeZone: 'UTC', weekday: 'short', month: 'short', day: 'numeric' });
  const dayName = (d) => dayFmt.format(new Date(d + 'T12:00:00Z'));
  async function loadReview(day) {
    let r;
    try { r = await fetch('/api/review' + (day ? '?day=' + encodeURIComponent(day) : ''), { cache: 'no-store' }); }
    catch (e) { banner('<b>Could not reach the review server.</b>'); return; }
    if (!r.ok) {
      banner('<b>The engine is off</b> and no session has been saved yet. The live view starts at 08:10 CT on the next trading day; after that, this page shows the day read-only.');
      return;
    }
    const data = await r.json();
    Object.assign(S, { ts: 0, bars: { '144t': [], '1m': [], '5m': [], '15m': [] }, positions: new Map(), targets: new Map(), closed: [], skips: [], marks: [], feed: [], crew: { briefs: {}, offline: true, directive: null }, signal: null, risk: null, l2: null, conviction: null, proposals: new Map(), books: [], account: null, combos: new Map(), bookClosed: [] });
    S.bulk = true;             // the saved feed events rebuild the Activity tab; the snapshot then sets the state
    for (const e of data.events || []) { try { apply(e); } catch (err) { console.warn('saved event skipped', e.type, err); } }
    S.bulk = false;
    loadSnapshot({ ...data.snapshot, type: 'snapshot' });
    S.ts = data.snapshot.ts || S.ts;
    S.agent = { activity: 'offline', text: 'Engine off' };
    if (office) { office.queue.length = 0; office.onAgent('offline', 'Engine off'); }
    window.__reviewSnapshot = data.snapshot;          // book_f.js reads it once mounted, or now if it already is
    if (window.AgentDeskBookF) window.AgentDeskBookF.fromSnapshot(data.snapshot);
    const sel = $('rv-day');
    sel.innerHTML = (data.days || []).map((d) => `<option value="${esc(d)}"${d === data.day ? ' selected' : ''}>${esc(dayName(d))}</option>`).join('');
    sel.onchange = () => loadReview(sel.value);
    const latest = data.days && data.day === data.days[0];
    banner(`<b>Review.</b> The engine is off, so this is the ${esc(dayName(data.day))} session as saved at ${hm(data.saved_ts)} CT, read-only. ${latest ? 'The live view comes back at 08:10 CT on the next trading day.' : 'Pick another day above.'}`);
    mark('all', 'chart');
  }
  function watchForEngine() {    // when the engine takes the port back (08:10 CT), switch to the live page
    setInterval(async () => {
      try { const t = await (await fetch('/config.js', { cache: 'no-store' })).text(); if (!t.includes("'review'")) location.reload(); }
      catch (e) { /* in between servers */ }
    }, 30000);
  }

  function banner(html) { const b = $('banner'); if (!html) { b.hidden = true; return; } b.innerHTML = html; b.hidden = false; }

  /* ------------------------------------------------------------------ controls */
  function confirmBar(text, onYes) {
    $('confirm-text').textContent = text; $('confirm').hidden = false;
    $('confirm-yes').onclick = () => { $('confirm').hidden = true; onYes(); };
    $('confirm-no').onclick = () => { $('confirm').hidden = true; };
    $('confirm-no').focus();
  }
  // control token: arrives once in the terminal link (#token=...), then kept for this dashboard origin only
  function token() {
    try {
      const m = location.hash.match(/token=([\w-]+)/);
      if (m) { localStorage.setItem('agentdesk-token', m[1]); history.replaceState(null, '', location.pathname + location.search); return m[1]; }
      return localStorage.getItem('agentdesk-token') || '';
    } catch (e) { return ''; }
  }
  token();
  window.addEventListener('hashchange', token);
  async function post(path) {
    try { const r = await fetch(path, { method: 'POST', headers: { 'X-AgentDesk-Token': token() } }); const j = await r.json(); if (!j.ok) banner(`<b>${esc(j.error || 'Request failed')}</b>`); }
    catch (e) { banner('<b>Could not reach the engine.</b>'); }
  }
  function wireControls() {
    $('btn-pause').onclick = () => post(S.risk?.paused ? '/api/resume' : '/api/pause');
    $('btn-flatten').onclick = () => confirmBar('Sell every open contract at the bid now?', () => post('/api/flatten'));
    $('btn-kill').onclick = () => confirmBar('Kill switch: flatten everything, cancel orders and stop trading for the day?', () => post('/api/kill'));
    document.querySelectorAll('.tf').forEach((b) => b.onclick = () => {
      document.querySelectorAll('.tf').forEach((x) => x.classList.toggle('on', x === b));
      S.tf = b.dataset.tf; chart.timeScale().applyOptions({ secondsVisible: S.tf === '144t' }); safeChart(redrawChart);
    });
    document.querySelectorAll('.tab').forEach((b) => b.onclick = () => {
      document.querySelectorAll('.tab').forEach((x) => x.classList.toggle('on', x === b));
      $('tab-trades').hidden = b.dataset.tab !== 'trades'; $('tab-activity').hidden = b.dataset.tab !== 'activity';
    });
    $('office-toggle').onclick = (e) => {
      const box = e.target.closest('.office'); const min = box.classList.toggle('min');
      e.target.textContent = min ? 'Show' : 'Hide'; e.target.setAttribute('aria-expanded', String(!min));
    };
  }

  /* ------------------------------------------------------------------ boot */
  let office = null, replay = null;
  async function boot() {
    try { await document.fonts.load('8px "Silkscreen"'); } catch (e) { /* fallback font is fine */ }
    buildChart();
    office = new window.Office($('office-canvas'), $('office-cap'), $('office-status'));
    wireControls();
    if (CFG.source === 'replay' && window.AGENTDESK_DEMO) {
      $('live').hidden = true; $('replay').hidden = false;
      const evs = window.AGENTDESK_DEMO;
      const fin = evs.find((e) => e.type === 'final');
      if (fin) S.config = fin.snapshot.config;
      S.mode = 'sim';
      banner('<b>Demo replay.</b> A recorded simulated session: synthetic SPY prices, paper fills, offline research crew. On your machine the same screen runs against the live engine.');
      replay = new Replay(evs);
      replay.startTs = CFG.startTs || evs[0].ts;
      replay.seek(replay.startTs);
    } else if (CFG.source === 'review') {
      $('live').hidden = true; $('review').hidden = false;
      await loadReview(null);
      watchForEngine();
    } else {
      connectWS();
    }
    let last = performance.now();
    const loop = (now) => {
      const dt = Math.min(0.25, (now - last) / 1000); last = now;
      if (replay) replay.step(dt);
      render(now);
      requestAnimationFrame(loop);
    };
    requestAnimationFrame(loop);
  }
  boot();
})();
