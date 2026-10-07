// Dashboard helpers from the 2026-10-02 session check (E's BAC calendar opened Wed 2026-09-30 14:45 CT). Run by tests/test_web_panel.py (node --test).
const test = require('node:test');
const assert = require('node:assert');
const P = require('../../agentdesk/web/panel.js');

const RISK = { day_pnl: 0, wins: 0, losses: 0, max_daily_loss: 400 };
const BOOKS = [
  { book: 'A', day_pnl: 0, wins: 0, losses: 0, daily_loss: null },
  { book: 'C', day_pnl: -4.32, wins: 0, losses: 1, daily_loss: null },
  { book: 'F1', day_pnl: -30.27, wins: 0, losses: 2, daily_loss: 75 },
  { book: 'E', day_pnl: -66.48, wins: 0, losses: 1, daily_loss: null },
  { book: 'F2', day_pnl: 0, wins: 0, losses: 0, daily_loss: 300 },
];

test('the risk panel totals every book, with each book loss limit on its own line', () => {
  const t = P.riskTotals(RISK, BOOKS);
  assert.strictEqual(Math.round(t.realized * 100) / 100, -101.07);
  assert.deepStrictEqual([t.wins, t.losses, t.closed], [0, 4, 4]);
  assert.deepStrictEqual(t.limits.map((l) => [l.book, Math.round(l.used), l.max]), [['A', 0, 400], ['F1', 30, 75], ['F2', 0, 300]]);
});

test('book A comes from the risk state when there are no other books', () => {
  const t = P.riskTotals({ ...RISK, day_pnl: -50, losses: 1 }, []);
  assert.deepStrictEqual([t.realized, t.closed, t.limits[0].used], [-50, 1, 50]);
});

test('ended blackouts drop off', () => {
  const bl = [{ start: 100, end: 200, name: 'NFP' }, { start: 300, end: 400, name: 'FOMC' }];
  assert.deepStrictEqual(P.liveBlackouts(bl, 250).map((b) => b.name), ['FOMC']);
});

test('only SPY trades are marked on the SPY chart', () => {
  assert.strictEqual(P.onChart({ occ: 'SPY261002P00772000,SPY261002P00770000' }, 'SPY'), true);
  assert.strictEqual(P.onChart({ contract: 'SPY 770C 10-02' }, 'SPY'), true);
  assert.strictEqual(P.onChart({ occ: 'BAC261009C00055000,BAC261016C00055000' }, 'SPY'), false);
  assert.strictEqual(P.onChart({ symbol: 'MTZ' }, 'SPY'), false);
});

test('a trade opened on an earlier day shows its weekday', () => {
  assert.strictEqual(P.openLabel(1790797500, '2026-10-02'), 'Wed 14:45');
  assert.strictEqual(P.openLabel(1790952000, '2026-10-02'), '09:40');
});

// L16: after a reconnect the 144t window starts mid-afternoon; a morning fill must not land on its first bar.
test('a 144t fill older than the first bar kept gets no bar', () => {
  const bars = [{ t: 100, end: 110 }, { t: 111, end: 120 }, { t: 125, end: 130 }];
  assert.strictEqual(P.bar144(bars, 50), null);                  // before the window: no marker
  assert.strictEqual(P.bar144(bars, 105), bars[0]);
  assert.strictEqual(P.bar144(bars, 120), bars[1]);
  assert.strictEqual(P.bar144(bars, 122), bars[2]);              // between bars: the bar it closed into
  assert.strictEqual(P.bar144(bars, 131), null);                 // after the last bar
  assert.strictEqual(P.bar144([], 105), null);
});

// M18: the card shows the plan the engine uses for this position, else today's config.
const EXITS = {
  stop_loss_pct: 0.35,
  swing: { scale_outs: [{ at: 0.25, fraction: 0.5 }, { at: 0.5, fraction: 0.25 }], runner_trail_pct: 0.25, exit_on_cross_back: '1m', time_stop_min: 20 },
  scalp: { scale_outs: [{ at: 0.15, fraction: 0.5 }], runner_trail_pct: 0.15, exit_on_cross_back: '144t', time_stop_min: 6 },
};

test('the exit plan comes from the position, then from the config', () => {
  const own = { stop_pct: 0.35, trail_pct: 0.1, exit_on_cross_back: '1m', scale_outs: [{ at: 0.2, fraction: 0.5 }], time_stop_min: 15 };
  assert.strictEqual(P.exitPlan({ setup: 'SWING', plan: own }, EXITS), own);
  assert.strictEqual(P.exitPlanText(own), 'stop −35% · 1m cross-back · scale 50% at +20% · runner trails 10% off peak · time stop 15m');
  assert.strictEqual(P.exitPlanText(P.exitPlan({ setup: 'SWING' }, EXITS)),
    'stop −35% · 1m cross-back · scale 50% at +25%, 25% at +50% · runner trails 25% off peak · time stop 20m');
  assert.strictEqual(P.exitPlan({ setup: 'SCALP' }, EXITS).exit_on_cross_back, '144t');
  assert.strictEqual(P.exitPlan({ setup: 'SWING' }, null), null);
  assert.strictEqual(P.exitPlanText({ stop_pct: 0.35, trail_pct: null, scale_outs: [] }), 'stop −35%');   // missing parts left out
});

test('the RSI band comes from the config', () => {
  assert.strictEqual(P.rsiBand({ rsi: { period: 14, lower: 30, upper: 65 } }), '30–65');
  assert.strictEqual(P.rsiBand(null), '30–70');
});

// M17: after 15:10 the review server answers /config.js; a live tab reloads into the review page.
test('the review server is recognised from its config.js', () => {
  assert.strictEqual(P.isReviewConfig("window.AGENTDESK = { source: 'review' };\n"), true);
  assert.strictEqual(P.isReviewConfig("window.AGENTDESK = { source: 'ws' };\n"), false);
  assert.strictEqual(P.isReviewConfig(''), false);
});

test('the recorder pill: ok, down, engine, idle; hidden without a state or on the review page', () => {
  assert.deepStrictEqual(P.recorderPill({ state: 'ok', age: 8 }, 'ws'),
    { text: 'REC', cls: 'pill good', title: 'Quote recorder writing; last quotes 8 s ago' });
  const down = P.recorderPill({ state: 'down', age: 420 }, 'ws');
  assert.strictEqual(down.text, 'REC DOWN');
  assert.strictEqual(down.cls, 'pill bad');
  assert.match(down.title, /last quotes 7 min ago/);
  assert.match(down.title, /recorder\.log/);
  assert.match(P.recorderPill({ state: 'down', age: null }, 'ws').title, /no quotes written yet/);
  assert.strictEqual(P.recorderPill({ state: 'engine', age: 900 }, 'ws').cls, 'pill warn');
  assert.strictEqual(P.recorderPill({ state: 'idle', age: 70000 }, 'ws').cls, 'pill');
  assert.strictEqual(P.recorderPill(null, 'ws'), null);
  assert.strictEqual(P.recorderPill({ state: 'ok', age: 3 }, 'review'), null);     // a saved state isn't live
  assert.strictEqual(P.recorderPill({ state: 'what', age: 3 }, 'ws'), null);
});
