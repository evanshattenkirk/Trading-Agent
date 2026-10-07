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

  // The 144t bar a fill at `ts` belongs to: the first bar that ends at or after it. None when the fill is older than
  // the first bar kept (the 4000-bar window rolls past the morning) or newer than the last, so no marker lands on a
  // wrong bar.
  function bar144(bars, ts) {
    if (!bars || !bars.length) return null;
    let lo = 0, hi = bars.length - 1;
    if (bars[hi].end < ts || ts < bars[0].t) return null;
    while (lo < hi) { const mid = (lo + hi) >> 1; if (bars[mid].end >= ts) hi = mid; else lo = mid + 1; }
    return bars[lo];
  }

  // Book A's exit plan: the one the engine built for this position (a next-trade tweak gives it its own), else today's
  // config (a saved session from before positions carried their plan).
  function exitPlan(pos, exits) {
    if (pos && pos.plan) return pos.plan;
    const x = exits && exits[pos && pos.setup === 'SWING' ? 'swing' : 'scalp'];
    return x ? { stop_pct: exits.stop_loss_pct, trail_pct: x.runner_trail_pct, exit_on_cross_back: x.exit_on_cross_back,
      scale_outs: x.scale_outs || [], time_stop_min: x.time_stop_min } : null;
  }
  const pct = (x) => Math.round(x * 100);
  const exitPlanText = (pl) => [
    pl.stop_pct != null ? `stop −${pct(pl.stop_pct)}%` : null,
    pl.exit_on_cross_back ? `${pl.exit_on_cross_back} cross-back` : null,
    (pl.scale_outs || []).length ? 'scale ' + pl.scale_outs.map((s) => `${pct(s.fraction)}% at +${pct(s.at)}%`).join(', ') : null,
    pl.trail_pct != null ? `runner trails ${pct(pl.trail_pct)}% off peak` : null,
    pl.time_stop_min != null ? `time stop ${pl.time_stop_min}m` : null,
  ].filter(Boolean).join(' · ');

  const rsiBand = (strategy) => { const r = (strategy && strategy.rsi) || {}; return `${r.lower ?? 30}–${r.upper ?? 70}`; };

  // The after-hours review server's /config.js (python -m agentdesk review); the engine's says source 'ws'.
  const isReviewConfig = (text) => /source:\s*['"]review['"]/.test(text || '');

  const api = { riskTotals, liveBlackouts, onChart, openLabel, bar144, exitPlan, exitPlanText, rsiBand, isReviewConfig };
  if (typeof module === 'object' && module.exports) module.exports = api;
  else root.AgentPanel = api;
})(this);
