/* AgentDesk office: a pixel-art trading floor that acts out what the engine is doing.
   World is drawn on a 192x120 grid at 2x (384x240 canvas); text is drawn at 1x in Silkscreen. */
(function () {
  const S = 2, W = 192, H = 120;
  const FONT = '8px "Silkscreen", monospace';

  const PAL = {
    wall: '#1b2130', wallTrim: '#252d40', floorA: '#2b2622', floorB: '#302a25', rug: '#3a2f4a', rugEdge: '#4b3d60',
    desk: '#6b4f3a', deskTop: '#8a6a4f', deskEdge: '#4d382a', metal: '#3b4252', screen: '#0c1118',
    skin: ['#f1c7a3', '#d9a07a', '#a86f4c', '#7a4a2e', '#e8b894', '#c68860', '#f3d2b4'],
    led: '#f0b44c', ledBg: '#0b0d12', up: '#26a69a', dn: '#ef5350', white: '#f4f1ea', ink: '#11131a',
  };
  const CREW = {
    agent: { name: 'AGENT', shirt: '#2b3243', trim: '#f0b44c', hair: '#2a1d14', skin: 1 },
    macro: { name: 'MACRO', shirt: '#3987e5', hair: '#1c1c1c', skin: 2, seat: [30, 64], face: 'L', desk: [6, 58] },
    rates: { name: 'RATES', shirt: '#199e70', hair: '#8a5a2b', skin: 0, seat: [30, 94], face: 'L', desk: [6, 88] },
    fed: { name: 'FED', shirt: '#9085e9', hair: '#c9c2b5', skin: 4, seat: [162, 64], face: 'R', desk: [168, 58] },
    vol: { name: 'VOL', shirt: '#c98500', hair: '#2a1d14', skin: 3, seat: [162, 94], face: 'R', desk: [168, 88] },
    quant: { name: 'QUANT', shirt: '#d55181', hair: '#3b2a55', skin: 6, seat: [70, 110], face: 'L', desk: [46, 104] },
    risk: { name: 'RISK', shirt: '#e66767', hair: '#5a3a1a', skin: 5, seat: [122, 110], face: 'R', desk: [128, 104] },
    tape: { name: 'TAPE', shirt: '#4fb3bf', hair: '#1c1c1c', skin: 3, seat: [108, 110], face: 'L', desk: [84, 104] },
  };
  const AGENT_SEAT = [96, 78];
  const HUDDLE = [[80, 84], [112, 84], [76, 98], [116, 98], [88, 102], [104, 102], [96, 106]];

  function ctTime(ts) {
    return new Intl.DateTimeFormat('en-US', { timeZone: 'America/Chicago', hour: '2-digit', minute: '2-digit', hour12: false }).format(new Date(ts * 1000));
  }

  class Office {
    constructor(canvas, caption, status) {
      this.cv = canvas; this.cap = caption; this.status = status;
      this.cx = canvas.getContext('2d');
      canvas.width = W * S; canvas.height = H * S;
      this.cx.imageSmoothingEnabled = false;
      this.t = 0; this.last = performance.now();
      this.market = { price: null, prev: null, pnl: 0, closes: [], ts: Date.now() / 1000, open: false, pos: null, halted: false };
      this.agentAct = 'offline'; this.agentText = ''; this.actSince = 0;
      this.queue = []; this.current = null; this.particles = []; this.flash = null; this.ticker = 0;
      this.actors = {};
      for (const k of Object.keys(CREW)) {
        const c = CREW[k];
        const seat = k === 'agent' ? AGENT_SEAT : c.seat;
        this.actors[k] = { key: k, x: seat[0], y: seat[1], home: seat.slice(), path: [], state: 'sit', dir: k === 'agent' ? 'U' : c.face, bubble: null, bob: Math.random() * 6 };
      }
      this.reduced = window.matchMedia && matchMedia('(prefers-reduced-motion: reduce)').matches;
      const loop = (now) => { this.step(Math.min(0.1, (now - this.last) / 1000)); this.last = now; this.draw(); requestAnimationFrame(loop); };
      requestAnimationFrame(loop);
    }

    /* ------------------------------------------------ inputs from the dashboard */
    setMarket(m) { Object.assign(this.market, m); }
    onAgent(act, text) {
      if (act === this.agentAct && text === this.agentText) return;
      const prev = this.agentAct;
      this.agentAct = act; this.agentText = text || ''; this.actSince = this.t;
      if (act === 'celebrating') this.burst('#26a69a', '$', 14);
      if (act === 'frustrated') this.burst('#8a93a6', 'rain', 10);
      if (act === 'typing') this.flash = { color: /SELL/.test(text) ? PAL.dn : PAL.up, until: this.t + 0.6 };
      if (!this.current) this.say('agent', text, act === 'typing' ? 1.8 : 2.6, act === 'typing' || act === 'celebrating' || act === 'frustrated' || act === 'alarm');
      if (act === 'alarm') this.callRisk('KILL SWITCH HIT');
    }
    onCrew(e) { this.queue.push(e); }
    busy() { return !!this.current || this.queue.length > 0 || Object.values(this.actors).some(a => a.path.length); }

    callRisk(text) { this.queue.push({ desk: 'risk', phase: 'walk' }, { desk: 'risk', phase: 'say', who: 'risk', text }, { desk: 'risk', phase: 'done' }); }

    /* ------------------------------------------------ choreography */
    say(who, text, secs = 2.6, bubble = true, to = null) {
      if (!text) return;
      const a = this.actors[who];
      if (bubble && a) {
        for (const o of Object.values(this.actors)) o.bubble = null;      // one speaker at a time
        a.bubble = { text: this.short(text), until: this.t + secs };
      }
      const name = (CREW[who] ? CREW[who].name : who.toUpperCase()) + (to && to !== 'all' && CREW[to] ? ' → ' + CREW[to].name : '');
      this.cap.innerHTML = '';
      const b = document.createElement('b'); b.textContent = name; b.style.color = CREW[who] ? (CREW[who].trim || CREW[who].shirt) : '';
      const s = document.createElement('span'); s.textContent = ' ' + text;
      this.cap.append(b, s);
    }
    short(t) { t = String(t).replace(/\s+/g, ' ').trim(); return t.length > 30 ? t.slice(0, 29) + '…' : t; }

    walkTo(a, tx, ty) {
      const aisleY = 84;
      a.path = [[a.x, aisleY], [tx, aisleY], [tx, ty]];
      a.state = 'walk';
    }
    consultSpot(desk) { return CREW[desk].face === 'L' ? [82, 80] : [110, 80]; }

    face(a, other) {
      if (!a || !other || a === other) return;
      if (a.key === 'agent' || a.state !== 'sit') a.dir = other.x >= a.x ? 'R' : 'L';
    }

    runQueue() {
      if (this.current) {
        const c = this.current;
        if (c.phase === 'walk' && c.desks.every((d) => this.actors[d].path.length === 0)) { this.current = null; }
        else if ((c.phase === 'say' || c.phase === 'done') && this.t >= c.until) {
          if (c.phase === 'done') {
            const a = this.actors[c.desk];
            if (a.state !== 'sit') { this.walkTo(a, a.home[0], a.home[1]); a.returning = true; }
          }
          this.current = null;
        }
        return;
      }
      const e = this.queue.shift();
      if (!e) return;
      if (e.phase === 'walk') {
        const group = [e];
        while (this.queue.length && this.queue[0].phase === 'walk') group.push(this.queue.shift());
        for (const g of group) {
          const a = this.actors[g.desk];
          const [x, y] = g.spot != null ? HUDDLE[g.spot % HUDDLE.length] : this.consultSpot(g.desk);
          this.walkTo(a, x, y); a.returning = false;
        }
        this.actors.agent.dir = 'D';
        this.current = { phase: 'walk', desks: group.map((g) => g.desk) };
      } else if (e.phase === 'say') {
        const who = e.who === 'agent' ? 'agent' : e.desk;
        const sp = this.actors[who];
        const target = e.to && this.actors[e.to] ? this.actors[e.to] : (e.to === 'agent' ? this.actors.agent : null);
        this.face(sp, target || this.actors.agent);
        if (target) this.face(target, sp);
        const dur = Math.max(1.8, Math.min(4.2, 1.2 + String(e.text || '').length / 22));
        this.say(who, e.text, dur, true, e.to);
        this.current = { ...e, until: this.t + dur };
      } else if (e.phase === 'done') {
        this.current = { ...e, until: this.t + 0.25 };
      }
    }

    burst(color, kind, n) {
      if (this.reduced) return;
      for (let i = 0; i < n; i++) {
        this.particles.push({ x: 96 + (Math.random() - 0.5) * 16, y: 60, vx: (Math.random() - 0.5) * 30, vy: -20 - Math.random() * 30, life: 1.4 + Math.random(), kind, color });
      }
    }

    /* ------------------------------------------------ simulation */
    step(dt) {
      this.t += dt;
      this.ticker += dt * 18;
      this.runQueue();
      for (const a of Object.values(this.actors)) {
        if (!a.path.length) continue;
        const [tx, ty] = a.path[0];
        const dx = tx - a.x, dy = ty - a.y, d = Math.hypot(dx, dy), sp = 46 * dt;
        if (d <= sp) { a.x = tx; a.y = ty; a.path.shift(); } else { a.x += dx / d * sp; a.y += dy / d * sp; }
        a.dir = Math.abs(dx) > Math.abs(dy) ? (dx > 0 ? 'R' : 'L') : (dy > 0 ? 'D' : 'U');
        if (!a.path.length) {
          if (a.returning) {
            a.state = 'sit'; a.dir = CREW[a.key].face; a.returning = false;
            if (Object.values(this.actors).every((o) => o.key === 'agent' || o.state === 'sit')) this.actors.agent.dir = 'U';
          }
          else { a.state = 'stand'; a.dir = CREW[a.key].face === 'L' ? 'R' : 'L'; }
        }
      }
      for (const p of this.particles) { p.x += p.vx * dt; p.y += p.vy * dt; p.vy += (p.kind === 'rain' ? 10 : 40) * dt; p.life -= dt; }
      this.particles = this.particles.filter(p => p.life > 0);
      for (const a of Object.values(this.actors)) if (a.bubble && this.t > a.bubble.until) a.bubble = null;
    }

    /* ------------------------------------------------ drawing */
    r(x, y, w, h, c) { this.cx.fillStyle = c; this.cx.fillRect(Math.round(x) * S, Math.round(y) * S, Math.round(w) * S, Math.round(h) * S); }

    draw() {
      const cx = this.cx, m = this.market;
      const hour = parseInt(ctTime(m.ts).slice(0, 2), 10), min = parseInt(ctTime(m.ts).slice(3), 10);
      const tod = hour + min / 60;
      this.drawRoom(tod);
      this.drawWindow(tod);
      this.drawTicker();
      this.drawClock(hour, min);
      this.drawAlarm();
      for (const k of ['macro', 'fed', 'rates', 'vol', 'quant', 'risk', 'tape']) this.drawStation(k);
      this.drawBattlestation();
      const people = Object.values(this.actors).filter(a => !(a.key === 'agent' && this.agentAct === 'offline'));
      people.sort((a, b) => a.y - b.y);
      for (const a of people) this.drawPerson(a);
      this.drawChairBack();
      this.drawProps();
      this.drawParticles();
      if (!m.open) this.drawNight(tod);
      for (const a of people) this.drawBubble(a);
      if (this.agentAct === 'offline') this.drawSleepSign();
    }

    drawRoom(tod) {
      this.r(0, 0, W, 46, PAL.wall);
      this.r(0, 44, W, 2, PAL.wallTrim);
      for (let y = 46; y < H; y += 4) for (let x = 0; x < W; x += 16) this.r(x + ((y / 4) % 2) * 8, y, 16, 4, ((x / 16 + y / 4) % 2) ? PAL.floorA : PAL.floorB);
      for (let y = 46; y < H; y += 4) this.r(0, y, W, 1, 'rgba(0,0,0,.18)');
      this.r(66, 66, 60, 28, PAL.rugEdge); this.r(68, 68, 56, 24, PAL.rug);
    }

    drawWindow(tod) {
      const x0 = 56, y0 = 10, w = 80, h = 26;
      const sky = tod < 7.5 ? ['#1b1d3a', '#44335e'] : tod < 8.6 ? ['#40427a', '#e08a5c'] : tod < 14.5 ? ['#4f8fd0', '#9cc7ec'] : tod < 16 ? ['#5a4c8a', '#e89266'] : ['#0e1026', '#1d2144'];
      const g = this.cx.createLinearGradient(0, y0 * S, 0, (y0 + h) * S);
      g.addColorStop(0, sky[0]); g.addColorStop(1, sky[1]);
      this.cx.fillStyle = g; this.cx.fillRect(x0 * S, y0 * S, w * S, h * S);
      const night = tod < 7.8 || tod >= 15.5;
      const bld = [[0, 14, 8], [8, 9, 6], [14, 18, 7], [21, 6, 9], [30, 12, 6], [36, 20, 5], [41, 11, 8], [49, 16, 6], [55, 8, 7], [62, 13, 9], [71, 17, 9]];
      for (const [bx, bh, bw] of bld) {
        this.r(x0 + bx, y0 + h - bh, bw, bh, night ? '#12142a' : '#2c3a55');
        for (let yy = y0 + h - bh + 2; yy < y0 + h - 1; yy += 3) for (let xx = x0 + bx + 1; xx < x0 + bx + bw - 1; xx += 2)
          if (((xx * 7 + yy * 13) % 5) < (night ? 2 : 1)) this.r(xx, yy, 1, 1, night ? '#f0d27a' : '#7c93b8');
      }
      this.r(x0 - 2, y0 - 2, w + 4, 2, '#3a4358'); this.r(x0 - 2, y0 + h, w + 4, 3, '#3a4358');
      this.r(x0 - 2, y0, 2, h, '#3a4358'); this.r(x0 + w, y0, 2, h, '#3a4358'); this.r(x0 + w / 2 - 1, y0, 2, h, '#3a4358');
    }

    drawTicker() {
      const cx = this.cx, m = this.market;
      this.r(0, 0, W, 7, PAL.ledBg);
      const chg = m.price && m.prev ? m.price - m.prev : 0;
      const pnl = m.pnl || 0;
      const txt = `SPY ${m.price ? m.price.toFixed(2) : '---'} ${chg >= 0 ? '▲' : '▼'}${Math.abs(chg).toFixed(2)}   P&L ${pnl >= 0 ? '+' : '-'}$${Math.abs(pnl).toFixed(0)}   ${m.pos ? m.pos : 'FLAT'}   ${m.halted ? 'HALTED' : ''}   `;
      cx.font = FONT; cx.textBaseline = 'top';
      const tw = cx.measureText(txt).width;
      const off = (this.ticker * S) % tw;
      cx.save(); cx.beginPath(); cx.rect(0, 0, W * S, 7 * S); cx.clip();
      for (let x = -off; x < W * S; x += tw) {
        cx.fillStyle = PAL.led; cx.fillText(txt, x, 3);
      }
      cx.restore();
    }

    drawClock(h, mi) {
      const cx = 168, cy = 22;
      this.r(cx - 7, cy - 7, 14, 14, '#d8d2c4'); this.r(cx - 6, cy - 6, 12, 12, '#f4f1ea');
      const a1 = ((h % 12) + mi / 60) / 12 * Math.PI * 2, a2 = mi / 60 * Math.PI * 2;
      for (let i = 0; i < 4; i++) this.r(cx + Math.round(Math.sin(a1) * i), cy - Math.round(Math.cos(a1) * i), 1, 1, '#11131a');
      for (let i = 0; i < 6; i++) this.r(cx + Math.round(Math.sin(a2) * i), cy - Math.round(Math.cos(a2) * i), 1, 1, '#b8453f');
      this.cx.font = FONT; this.cx.fillStyle = '#8a93a6'; this.cx.fillText('CT', (cx - 5) * S, (cy + 9) * S);
      // coffee machine on the left wall
      this.r(10, 30, 12, 14, '#2f3647'); this.r(12, 32, 8, 5, '#11131a'); this.r(14, 39, 4, 3, '#e9e2d0');
      if (!this.reduced && (this.t * 2) % 2 < 1) this.r(15, 37, 1, 1, '#aab');
    }

    drawAlarm() {
      if (!(this.agentAct === 'alarm' || this.agentAct === 'halted')) return;
      const on = this.reduced || Math.floor(this.t * 4) % 2 === 0;
      this.r(144, 12, 8, 6, on ? '#ff4a4a' : '#6a1d1d');
      if (on && this.agentAct === 'alarm') { this.cx.fillStyle = 'rgba(255,40,40,.10)'; this.cx.fillRect(0, 0, W * S, H * S); }
    }

    drawScreen(x, y, w, h, series, color) {
      this.r(x - 1, y - 1, w + 2, h + 2, PAL.metal);
      this.r(x, y, w, h, PAL.screen);
      if (series && series.length > 1) {
        const lo = Math.min(...series), hi = Math.max(...series), n = series.length;
        let px = null;
        for (let i = 0; i < w; i++) {
          const v = series[Math.floor(i / w * n)];
          const yy = y + h - 2 - Math.round((v - lo) / (hi - lo || 1) * (h - 4));
          this.r(x + i, yy, 1, 1, color || (series[n - 1] >= series[0] ? PAL.up : PAL.dn));
          if (px !== null && Math.abs(yy - px) > 1) this.r(x + i, Math.min(yy, px), 1, Math.abs(yy - px), color || (series[n - 1] >= series[0] ? PAL.up : PAL.dn));
          px = yy;
        }
      }
    }

    drawBattlestation() {
      const m = this.market, c = m.closes || [];
      const fl = this.flash && this.t < this.flash.until ? this.flash.color : null;
      // monitors
      this.drawScreen(72, 44, 15, 10, c.slice(-60));
      this.drawScreen(89, 42, 15, 12, c.slice(-20));
      this.r(106, 44, 15, 10, PAL.metal); this.r(107, 45, 13, 8, PAL.screen);
      if (m.posPct != null) {
        const col = m.posPct >= 0 ? PAL.up : PAL.dn;
        const bar = Math.max(1, Math.min(12, Math.round(Math.abs(m.posPct) / 4)));
        this.r(108, 49, bar, 3, col);
        this.cx.font = FONT; this.cx.fillStyle = col; this.cx.fillText((m.posPct >= 0 ? '+' : '') + m.posPct.toFixed(0) + '%', 107 * S + 1, 45 * S);
      } else {
        this.cx.font = FONT; this.cx.fillStyle = '#6b7382'; this.cx.fillText('FLAT', 108 * S, 46 * S);
      }
      if (fl) { this.cx.fillStyle = fl + '55'; this.cx.fillRect(71 * S, 41 * S, 51 * S, 15 * S); }
      for (const x of [79, 96, 113]) this.r(x, 54, 1, 3, PAL.metal);
      // desk
      this.r(68, 57, 56, 4, PAL.deskTop); this.r(68, 61, 56, 6, PAL.desk); this.r(68, 66, 56, 1, PAL.deskEdge);
      this.r(90, 58, 12, 2, '#1b1f29');       // keyboard
      if (this.agentAct === 'typing' && !this.reduced && Math.floor(this.t * 12) % 2) this.r(91 + Math.floor(Math.random() * 10), 58, 1, 1, '#fff');
      this.r(114, 57, 3, 3, '#e9e2d0'); if (!this.reduced && Math.floor(this.t * 2) % 2) this.r(115, 55, 1, 1, '#9aa');   // mug
    }

    drawChairBack() {
      const a = this.actors.agent;
      if (this.agentAct === 'offline') { this.r(91, 70, 10, 10, '#232838'); this.r(92, 80, 8, 2, '#1a1e2a'); return; }
      this.r(a.x - 5, a.y - 5, 10, 6, '#232838');
    }

    drawStation(k) {
      const c = CREW[k], [dx, dy] = c.desk;
      this.r(dx, dy, 18, 3, PAL.deskTop); this.r(dx, dy + 3, 18, 5, PAL.desk); this.r(dx, dy + 7, 18, 1, PAL.deskEdge);
      const sx = c.face === 'L' ? dx + 2 : dx + 6;
      this.r(sx, dy - 8, 10, 7, PAL.metal); this.r(sx + 1, dy - 7, 8, 5, PAL.screen);
      const blink = Math.floor(this.t * 1.5 + dx) % 3;
      if (k === 'tape') {          // live depth: green bid bar vs red ask bar
        const imb = this.market.imb == null ? 0 : this.market.imb;
        const g = Math.max(1, Math.round(4 + imb * 3)), rd = Math.max(1, Math.round(4 - imb * 3));
        this.r(sx + 1, dy - 6, g, 1, '#26a69a'); this.r(sx + 1, dy - 4, rd, 1, '#ef5350');
      } else this.r(sx + 2, dy - 5 + blink % 2, 5, 1, c.shirt);
      if (k === 'risk') this.r(dx + 13, dy - 2, 3, 2, '#c0392b');
      if (k === 'quant') {   // whiteboard on the back wall
        this.r(27, 14, 22, 15, '#9aa3b2'); this.r(28, 15, 20, 13, '#e9e6de');
        this.r(30, 17, 8, 1, '#556'); this.r(30, 20, 14, 1, '#556'); this.r(30, 23, 6, 1, '#b8453f'); this.r(38, 23, 1, 3, '#2a78d6'); this.r(41, 21, 1, 5, '#2a78d6'); this.r(44, 18, 1, 8, '#2a78d6');
      }
      this.cx.font = FONT; this.cx.fillStyle = '#7f889a';
      this.cx.fillText(c.name, dx * S, (dy + 9) * S);
    }

    drawPerson(a) {
      const c = CREW[a.key], skin = PAL.skin[c.skin];
      const x = Math.round(a.x), y = Math.round(a.y);
      const act = a.key === 'agent' ? this.agentAct : null;
      const walking = a.path.length > 0;
      const step = walking ? Math.floor(this.t * 8) % 2 : 0;
      let jump = 0;
      if (act === 'celebrating' && !this.reduced && this.t - this.actSince < 3) jump = Math.abs(Math.sin(this.t * 10)) * 2;
      const Y = y - jump;
      if (a.state === 'sit' && a.key !== 'agent') {
        // side-view sitting crew
        const f = c.face === 'L' ? -1 : 1;
        this.r(x - 3, Y - 4, 6, 2, '#1d2230');                 // seat
        this.r(x - 3, Y - 12, 6, 8, c.shirt);                  // torso
        this.r(x - 3, Y - 17, 6, 5, skin);                     // head
        this.r(x - 3, Y - 18, 6, 2, c.hair); this.r(x + (f < 0 ? 1 : -3), Y - 17, 2, 3, c.hair);
        this.r(x + (f < 0 ? -3 : 2), Y - 15, 1, 1, PAL.ink);   // eye
        const typing = Math.floor(this.t * 6 + a.bob) % 4 === 0 ? 1 : 0;
        this.r(x + f * 3 + (f < 0 ? -4 : 0), Y - 9 - typing, 4, 2, c.shirt);   // arm to desk
        this.r(x + f * 7 + (f < 0 ? -1 : 0), Y - 9 - typing, 1, 2, skin);
        this.r(x - 3, Y - 4, 6, 3, '#2a2f3d');                 // legs
        this.r(x - 1, Y - 2, 2, 5, '#2a2f3d');
        return;
      }
      if (a.key === 'agent' && !walking && a.dir === 'U') {
        // back view at the desk
        const arm = act === 'typing' && !this.reduced ? Math.floor(this.t * 14) % 2 : 0;
        const slump = act === 'frustrated' ? 2 : 0;
        const up = act === 'celebrating' && this.t - this.actSince < 2.5;
        this.r(x - 4, Y - 12 + slump, 8, 9, c.shirt);
        this.r(x - 1, Y - 12 + slump, 2, 6, c.trim);
        this.r(x - 3, Y - 18 + slump, 6, 6, c.hair);
        this.r(x - 3, Y - 13 + slump, 6, 1, skin);
        if (up) { this.r(x - 6, Y - 20, 2, 8, c.shirt); this.r(x + 4, Y - 20, 2, 8, c.shirt); this.r(x - 6, Y - 22, 2, 2, skin); this.r(x + 4, Y - 22, 2, 2, skin); }
        else if (act === 'frustrated') { this.r(x - 5, Y - 18, 2, 5, c.shirt); this.r(x + 3, Y - 18, 2, 5, c.shirt); this.r(x - 4, Y - 19, 8, 1, skin); }
        else { this.r(x - 6, Y - 11 - arm, 2, 5, c.shirt); this.r(x + 4, Y - 11 - (1 - arm) * (act === 'typing' ? 1 : 0), 2, 5, c.shirt); }
        if (act === 'coffee') { this.r(x + 5, Y - 13, 3, 3, '#e9e2d0'); if (!this.reduced && Math.floor(this.t * 2) % 2) this.r(x + 6, Y - 15, 1, 1, '#bbb'); }
        return;
      }
      // standing / walking (front or side)
      const side = a.dir === 'L' || a.dir === 'R';
      const f = a.dir === 'L' ? -1 : 1;
      this.r(x - 2, Y - 3, 2, 3 - step, '#2a2f3d'); this.r(x, Y - 3, 2, 2 + step, '#2a2f3d');   // legs
      this.r(x - 3, Y - 11, 6, 8, c.shirt);
      if (a.key === 'agent') this.r(x - 1, Y - 11, 2, 6, c.trim);
      this.r(x - 3, Y - 16, 6, 5, skin);
      this.r(x - 3, Y - 17, 6, 2, c.hair);
      if (side) { this.r(x + (f < 0 ? 1 : -3), Y - 16, 2, 3, c.hair); this.r(x + (f < 0 ? -3 : 2), Y - 14, 1, 1, PAL.ink); }
      else { this.r(x - 2, Y - 14, 1, 1, PAL.ink); this.r(x + 1, Y - 14, 1, 1, PAL.ink); }
      const talking = a.bubble && Math.floor(this.t * 6) % 2;
      if (!side && talking) this.r(x - 1, Y - 12, 2, 1, '#7a3b30');
      this.r(x - 4, Y - 10 + step, 1, 5, c.shirt); this.r(x + 3, Y - 10 + (1 - step), 1, 5, c.shirt);
      if (a.key === 'quant' && a.state === 'stand') this.r(x + 3 * f, Y - 8, 3, 4, '#e9e6de');   // clipboard
    }

    drawProps() {
      // plants
      for (const [x, y] of [[2, 112], [184, 112], [48, 50], [140, 50]]) {
        this.r(x, y - 4, 6, 5, '#7a4e32'); this.r(x - 1, y - 10, 8, 6, '#2f7d4f'); this.r(x + 1, y - 13, 4, 3, '#3a9660');
      }
      // water cooler
      this.r(150, 36, 7, 10, '#c7d0dc'); this.r(151, 30, 5, 6, '#7fb4e6');
    }

    drawParticles() {
      const cx = this.cx;
      for (const p of this.particles) {
        if (p.kind === '$') { cx.font = FONT; cx.fillStyle = p.color; cx.fillText('$', p.x * S, p.y * S); }
        else { this.r(p.x, p.y, 1, 2, p.color); }
      }
      if (this.agentAct === 'frustrated' && this.t - this.actSince < 3) {
        const a = this.actors.agent;
        this.r(a.x - 6, a.y - 30, 12, 4, '#6b7382'); this.r(a.x - 4, a.y - 32, 8, 2, '#6b7382');
        if (!this.reduced && Math.floor(this.t * 8) % 2) this.r(a.x - 3 + Math.floor(Math.random() * 7), a.y - 25, 1, 2, '#7fb4e6');
      }
    }

    drawNight(tod) {
      this.cx.fillStyle = tod < 8.5 ? 'rgba(10,12,30,.28)' : 'rgba(6,8,20,.52)';
      this.cx.fillRect(0, 8 * S, W * S, (H - 8) * S);
      const g = this.cx.createRadialGradient(96 * S, 60 * S, 4, 96 * S, 60 * S, 46 * S);
      g.addColorStop(0, 'rgba(255,214,140,.22)'); g.addColorStop(1, 'rgba(255,214,140,0)');
      this.cx.fillStyle = g; this.cx.fillRect(40 * S, 20 * S, 112 * S, 80 * S);
    }

    drawSleepSign() {
      this.cx.font = FONT; this.cx.fillStyle = '#9aa3b2';
      const z = Math.floor(this.t) % 3;
      this.cx.fillText('Z'.repeat(z + 1), 104 * S, 64 * S);
      this.cx.fillText('BACK AT 8:15', 72 * S, 86 * S);
    }

    drawBubble(a) {
      if (!a.bubble) return;
      const cx = this.cx; cx.font = FONT;
      const tw = Math.ceil(cx.measureText(a.bubble.text).width) + 8;
      let bx = a.x * S - tw / 2, by = (a.y - 34) * S;
      if (a.key === 'agent' && a.dir === 'U') by = (a.y - 40) * S;
      bx = Math.max(2, Math.min(W * S - tw - 2, bx));
      by = Math.max(16, by);
      cx.fillStyle = PAL.ink; cx.fillRect(bx - 1, by - 1, tw + 2, 16);
      cx.fillStyle = PAL.white; cx.fillRect(bx, by, tw, 14);
      const tx = Math.max(bx + 4, Math.min(bx + tw - 8, a.x * S - 2));
      cx.fillRect(tx, by + 14, 4, 3); cx.fillRect(tx + 1, by + 17, 2, 2);
      cx.fillStyle = PAL.ink; cx.textBaseline = 'top'; cx.fillText(a.bubble.text, bx + 4, by + 3);
    }
  }

  window.Office = Office;
})();
