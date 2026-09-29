/* Draggable dividers between the chart and the side column, the chart and the trades table, and the side cards and
   the office. Drag, or focus a divider and use the arrow keys (Shift for bigger steps); double-click resets it.
   Sizes are remembered per browser. Self-contained: app.js, office.js and book_f.js stay unchanged. */
(function () {
  'use strict';
  const KEY = 'agentdesk.layout.v1';
  const root = document.documentElement;
  const q = (s) => document.querySelector(s);
  // each divider sizes the pane after it: dragging right/down makes that pane smaller
  const SPEC = {
    side: { prop: '--side-w', axis: 'x', el: () => q('.side'), min: 280, max: () => q('.main').clientWidth - 32 - 12 - 360 },
    log: { prop: '--log-h', axis: 'y', el: () => q('.log-panel'), min: 80, max: () => q('.chart-col').clientHeight - 12 - 150 },
    office: { prop: '--office-h', axis: 'y', el: () => q('.office'), min: 60, max: () => q('.side').clientHeight - 12 - 100 },
  };
  let saved = {};
  try { saved = JSON.parse(localStorage.getItem(KEY) || '{}') || {}; } catch (e) { saved = {}; }

  function store() {
    try { localStorage.setItem(KEY, JSON.stringify(saved)); } catch (e) { /* private window: sizes last this visit only */ }
  }

  function clamp(name, px) {
    const s = SPEC[name];
    return Math.round(Math.max(s.min, Math.min(px, Math.max(s.min, s.max()))));
  }

  function set(name, px, keep) {
    const v = clamp(name, px);
    root.style.setProperty(SPEC[name].prop, v + 'px');
    if (keep) { saved[name] = v; store(); }
    return v;
  }

  function size(name) {
    const r = SPEC[name].el().getBoundingClientRect();
    return SPEC[name].axis === 'x' ? r.width : r.height;
  }

  function reset(name) {
    root.style.removeProperty(SPEC[name].prop);
    delete saved[name];
    store();
  }

  function wire(div) {
    const name = div.dataset.split;
    const s = SPEC[name];
    if (!s) return;
    div.addEventListener('pointerdown', (e) => {
      if (e.button !== 0) return;
      e.preventDefault();
      div.setPointerCapture(e.pointerId);
      const start = s.axis === 'x' ? e.clientX : e.clientY;
      const from = size(name);
      div.classList.add('drag');
      document.body.classList.add('dragging', s.axis === 'x' ? 'v' : 'h');
      const move = (ev) => set(name, from - ((s.axis === 'x' ? ev.clientX : ev.clientY) - start), false);
      const up = (ev) => {
        div.removeEventListener('pointermove', move);
        div.removeEventListener('pointerup', up);
        div.removeEventListener('pointercancel', up);
        div.classList.remove('drag');
        document.body.classList.remove('dragging', 'v', 'h');
        set(name, size(name), true);
      };
      div.addEventListener('pointermove', move);
      div.addEventListener('pointerup', up);
      div.addEventListener('pointercancel', up);
    });
    div.addEventListener('dblclick', () => reset(name));
    div.addEventListener('keydown', (e) => {
      const keys = s.axis === 'x' ? ['ArrowLeft', 'ArrowRight'] : ['ArrowUp', 'ArrowDown'];
      const i = keys.indexOf(e.key);
      if (i < 0) return;
      e.preventDefault();
      const step = e.shiftKey ? 64 : 16;
      set(name, size(name) + (i === 0 ? step : -step), true);   // left/up moves the divider that way: the pane grows
    });
  }

  function refit() {       // a smaller window never leaves a pane wider or taller than the room it has
    Object.keys(saved).forEach((name) => { if (SPEC[name]) set(name, saved[name], false); });
  }

  function init() {
    document.querySelectorAll('.split[data-split]').forEach(wire);
    refit();
    let t = null;
    window.addEventListener('resize', () => { clearTimeout(t); t = setTimeout(refit, 60); });
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
  else init();
})();
