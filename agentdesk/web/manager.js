/* Manager panel (Evan, 2026-10-27): connection and job health from GET /api/health (agentdesk/health.py), polled every
   30 s on the live dashboard and the after-hours review page. Read-only: each red or amber row says what to run in
   Terminal, with a Copy button. Hidden in the demo replay and the simulator (the server answers 404 there). */
(function () {
  const CFG = window.AGENTDESK || { source: 'ws' };
  if (CFG.source !== 'ws' && CFG.source !== 'review') return;
  const P = window.AgentPanel;
  const $ = (id) => document.getElementById(id);
  const esc = (s) => String(s ?? '').replace(/[&<>"]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
  const hms = (ts) => new Date(ts * 1000).toLocaleTimeString('en-US', { timeZone: 'America/Chicago', hour12: false });
  const LABEL = { ok: 'OK', warn: 'Soon', bad: 'Fix now', idle: 'Idle' };
  const btn = $('mgr'), panel = $('manager');
  let rep = null, lastOk = 0, failed = false;

  function render() {
    const pill = P.managerPill(rep);
    btn.hidden = !pill;
    if (!pill) return;
    btn.textContent = pill.text;
    btn.className = failed ? 'pill mgr' : pill.cls;
    btn.title = failed ? `No answer from the dashboard server since ${hms(lastOk)} CT` : pill.title;
    $('mgr-ts').textContent = failed ? `no answer since ${hms(lastOk)} CT` : `updated ${hms(rep.ts)} CT`;
    $('mgr-rows').innerHTML = rep.checks.map((c) => `<li class="${esc(c.state)}">
      <div class="nm"><span>${esc(c.name)}</span><span class="st">${LABEL[c.state] || esc(c.state)}</span></div>
      <div class="dt">${esc(c.detail)}</div>
      ${c.fix ? `<div class="fx">${esc(c.fix)}</div>` : ''}
      ${c.cmd ? `<div class="cmd"><code>${esc(c.cmd)}</code><button class="icon-btn" type="button" data-copy="${esc(c.cmd)}">Copy</button></div>` : ''}
    </li>`).join('');
  }

  async function poll() {
    try {
      const r = await fetch('/api/health', { cache: 'no-store' });
      if (r.status === 404) { rep = null; failed = false; render(); return; }
      if (!r.ok) throw new Error(String(r.status));
      rep = await r.json();
      lastOk = rep.ts;
      failed = false;
    } catch (_) {
      failed = rep !== null;
    }
    render();
  }

  function show(on) {
    panel.hidden = !on || btn.hidden;
    btn.setAttribute('aria-expanded', String(!panel.hidden));
    if (!panel.hidden) $('mgr-close').focus();
  }

  btn.addEventListener('click', () => show(panel.hidden));
  $('mgr-close').addEventListener('click', () => { show(false); btn.focus(); });
  document.addEventListener('keydown', (e) => { if (e.key === 'Escape' && !panel.hidden) { show(false); btn.focus(); } });
  $('mgr-rows').addEventListener('click', async (e) => {
    const b = e.target.closest('[data-copy]');
    if (!b) return;
    try { await navigator.clipboard.writeText(b.dataset.copy); b.textContent = 'Copied'; }
    catch (_) { getSelection().selectAllChildren(b.previousElementSibling); b.textContent = 'Selected'; }
    setTimeout(() => { b.textContent = 'Copy'; }, 1500);
  });

  const fromHash = () => { if (location.hash.includes('manager')) show(true); };
  window.addEventListener('hashchange', fromHash);
  poll().then(fromHash);
  setInterval(poll, 30000);
})();
