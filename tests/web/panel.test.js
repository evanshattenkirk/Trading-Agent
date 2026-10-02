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
