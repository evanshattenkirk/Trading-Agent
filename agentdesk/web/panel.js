/* Pure helpers for the dashboard, kept out of app.js so tests/web can run them under node. */
(function (root) {
  const ctDay = (ts) => new Date(ts * 1000).toLocaleDateString('en-CA', { timeZone: 'America/Chicago' });
  const ctHm = (ts) => new Date(ts * 1000).toLocaleTimeString('en-US', { timeZone: 'America/Chicago', hour: '2-digit', minute: '2-digit', hour12: false });

  // Every book's day combined (Evan, 2026-10-02). Book A's numbers live in the risk state, not its book row.
  // Each loss limit stays per book: A's -$400 halts A only, F1 and F2 have their own (HANDOFF section 9).
  function riskTotals(risk, books) {
    const rows = (books && books.length ? books : [{ book: 'A' }]).map((b) => (b.book === 'A' && risk
      ? { book: 'A', day_pnl: risk.day_pnl || 0, wins: risk.wins || 0, losses: risk.losses || 0, daily_loss: risk.max_daily_loss }
      : b));
    const sum = (k) => rows.reduce((a, b) => a + (b[k] || 0), 0);
    return {
      realized: sum('day_pnl'), wins: sum('wins'), losses: sum('losses'), closed: sum('wins') + sum('losses'),
      limits: rows.filter((b) => b.daily_loss).map((b) => ({ book: b.book, used: Math.max(0, -(b.day_pnl || 0)), max: Math.abs(b.daily_loss) })),
    };
  }

  // A blackout whose window has ended is history; it no longer belongs on the risk panel.
  const liveBlackouts = (blackouts, now) => (blackouts || []).filter((b) => b.end > now);

  // Only trades on the charted underlying get chart markers (no BAC calendar arrows on SPY).
  const onChart = (p, symbol) => new RegExp('^' + symbol + '[0-9 ]').test(p.occ || p.contract || '');

  // A trade opened on an earlier session day (an E calendar) shows its day too.
  const openLabel = (ts, day) => (ts == null ? '' : ctDay(ts) === day ? ctHm(ts)
    : `${new Date(ts * 1000).toLocaleDateString('en-US', { timeZone: 'America/Chicago', weekday: 'short' })} ${ctHm(ts)}`);

  const api = { riskTotals, liveBlackouts, onChart, openLabel };
  if (typeof module === 'object' && module.exports) module.exports = api;
  else root.AgentPanel = api;
})(this);
