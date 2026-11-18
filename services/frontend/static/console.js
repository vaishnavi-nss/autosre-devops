/* AutoSRE website: roles, sign-in, theme, and the on-call pages. Data comes from /api/state (live) and /api/history (everything the engine and
   dispatcher recorded, restored from the S3 bucket after a lab restart). The pure functions at the top are tested under node. */
(function () {
  'use strict';

  // ------------------------------------------------------------------------------------------------ pure helpers
  const STAGE_TITLES = ['Fault noticed', 'Alerts grouped', 'Root cause diagnosed', 'ML second opinion', 'Dispatcher safety gate', 'Ansible playbook', 'Recovery verified', 'Back to normal'];
  const esc = v => String(v === null || v === undefined ? '-' : v).replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const ms = t => { const v = Date.parse(t); return isNaN(v) ? null : v; };
  const hms = v => (v === null ? '-' : new Date(v).toISOString().slice(11, 19));

  /* The live tracker and the replay share one function: the stage events up to `nowMs` decide each stage's state.
     events: [{stage 1..8, state done|start|stop|warn|na, t, text}], opts: {final: bool, reason: string, alertsQuiet: bool}.
     A stage is done/stop/warn from its own event, active when the stage before it is done and it has not finished, and skipped once the
     incident has ended (or stopped earlier) without ever reaching it. */
  function trackerStages(events, nowMs, opts) {
    opts = opts || {};
    const seen = (events || []).filter(e => e.stage >= 1 && (ms(e.t) === null || ms(e.t) <= nowMs));
    const out = [];
    let blocked = false;                     // an earlier stage stopped the incident
    for (let n = 1; n <= 8; n++) {
      const mine = seen.filter(e => e.stage === n);
      const last = mine[mine.length - 1];
      const prev = out[n - 2];
      const stop = mine.find(e => e.state === 'stop');
      const done = mine.find(e => e.state === 'done' || e.state === 'warn' || e.state === 'na');
      const started = mine.find(e => e.state === 'start');
      let st, text = '', t = null;
      if (blocked) { st = 'skip'; text = 'Not reached'; }
      else if (stop) { st = 'stop'; text = stop.text; t = ms(stop.t); blocked = true; }
      else if (done) { st = done.state === 'na' ? 'skip' : (done.state === 'warn' ? 'warn' : 'done'); text = done.text; t = ms(done.t); }
      else if (started) { st = 'active'; text = started.text; t = ms(started.t); }
      else if ((n === 1 && (seen.length > 0 || opts.live)) || (n > 1 && prev && (prev.state === 'done' || prev.state === 'warn' || prev.state === 'skip' && n === 4))) {
        if (opts.final) { st = 'skip'; text = n === 6 || n === 7 || n === 8 ? (opts.reason || 'Not run') : 'Not reached'; }
        else { st = 'active'; text = n === 6 ? 'Waiting for the playbook to start' : (n === 7 ? 'Waiting for the verification' : 'Waiting'); }
      } else { st = opts.final ? 'skip' : 'wait'; text = opts.final ? 'Not reached' : 'Waiting'; }
      if (n === 8 && st === 'done' && opts.alertsQuiet === false) { st = 'warn'; text = 'Fix verified; alerts are still clearing'; }
      out.push({ n: n, title: STAGE_TITLES[n - 1], state: st, text: text, t: t });
    }
    return out;
  }

  /* Fixed-width time buckets between the first and last incident: days for a short history, weeks for a long one. */
  function buckets(incidents) {
    const ts = incidents.map(i => ms(i.t)).filter(v => v !== null);
    if (!ts.length) return { unit: 'day', rows: [] };
    const lo = Math.min.apply(null, ts), hi = Math.max.apply(null, ts), span = (hi - lo) / 864e5;
    const unit = span > 45 ? 'week' : 'day', size = unit === 'week' ? 7 * 864e5 : 864e5;
    const base = Math.floor(lo / size) * size, n = Math.floor((hi - base) / size) + 1;
    const rows = Array.from({ length: n }, (_, k) => ({ start: base + k * size, auto: 0, esc: 0, dry: 0, other: 0 }));
    incidents.forEach(i => { const v = ms(i.t); if (v === null) return; const b = rows[Math.floor((v - base) / size)];
      if (i.action === 'auto_fixed') b.auto++; else if (i.action === 'escalated' || i.action === 'vetoed') b.esc++; else if (i.action === 'dry_run' || i.action === 'decision') b.dry++; else b.other++; });
    return { unit: unit, rows: rows };
  }

  function variantRows(inc) {
    const v = inc && inc.variants;
    if (!v || !v.rows) return [];
    return v.rows.map(r => ({ label: r.label, active: !!r.active, escalate: r.action === 'escalate',
      decision: r.action === 'act' ? 'Acts: ' + (r.playbook || r.cause || '-') : 'Escalates to a person', why: r.reason || '' }));
  }

  const median = a => { const s = a.filter(x => x !== null && x !== undefined).sort((x, y) => x - y); return s.length ? s[Math.floor(s.length / 2)] : null; };
  const mean = a => { const s = a.filter(x => x !== null && x !== undefined); return s.length ? s.reduce((x, y) => x + y, 0) / s.length : null; };
  const pct = (n, d) => d ? Math.round(100 * n / d) + '%' : '-';
  const dur = s => { if (s === null || s === undefined) return '-'; if (s < 90) return Math.round(s) + ' s'; const m = s / 60; if (m < 90) return Math.round(m) + ' min'; return (m / 60).toFixed(1) + ' h'; };

  if (typeof module !== 'undefined' && module.exports) { module.exports = { trackerStages, buckets, variantRows, esc, median, STAGE_TITLES }; return; }

  // ------------------------------------------------------------------------------------------------ app state
  const $ = id => document.getElementById(id);
  const get = (k, d) => { try { return localStorage.getItem(k) || d; } catch (e) { return d; } };
  const put = (k, v) => { try { localStorage.setItem(k, v); } catch (e) { /* no storage: the choice just is not remembered */ } };
  let ME = get('autosre-user', '');
  let ROLE = get('autosre-role', 'executive');
  let VIEW = 'overview';
  let HIST = { incidents: [], changes: [], runs: [], errors: {} };
  let BUNDLE = { runs: [], changes: [] };
  const F = { range: 9999, q: '', action: '', cause: '', service: '', ml: '', page: 0 };
  const REPLAY = { id: '', playing: false, speed: 10, clock: 0, from: 0, to: 0, timer: null, watch: null };
  const NAVS = {
    executive: [['overview', 'Overview'], ['telemetry', 'Telemetry & Deep-Dive'], ['replay', 'Fault Replay'], ['trends', 'Trends']],
    oncall: [['incidents', 'Incidents'], ['replay', 'Fault Replay'], ['services', 'Services'], ['playbooks', 'Playbooks'], ['settings', 'Settings & changes'], ['runs', 'Validation runs'], ['telemetry', 'Telemetry']],
  };
  const SECTION = { overview: 'view-dashboard', telemetry: 'view-status' };
  const CAUSE = { db_connection_pool_exhaustion: 'DB connection pool exhausted', high_memory_usage: 'High memory usage', db_disk_full: 'Database disk full',
    database_down: 'Database is down', transaction_service_down: 'Transaction service is down', api_gateway_down: 'API gateway is down',
    bad_rollout_transaction_service: 'Bad rollout: transaction service', bad_rollout_api_gateway: 'Bad rollout: API gateway',
    database_unreachable: 'Database unreachable (selector)', transaction_service_unreachable: 'Transaction service unreachable (selector)', api_gateway_unreachable: 'API gateway unreachable (selector)' };
  const cname = c => c ? (CAUSE[c] || String(c).replace(/_/g, ' ')) : 'Unknown cause';
  const ACTION = { auto_fixed: ['Auto-fixed', 'p-auto'], escalated: ['Escalated', 'p-esc'], dry_run: ['Dry run', 'p-dry'], vetoed: ['Held by ML gate', 'p-veto'], skipped: ['Skipped (cooldown)', 'p-dry'], running: ['Fixing now', 'p-run'], decision: ['Decision only', 'p-dry'] };
  const label = i => i.open ? ['Open', 'p-open'] : (ACTION[i.action] || [i.action, 'p-dry']);
  const fmt = v => v === null ? '-' : new Date(v).toLocaleString('en-GB', { timeZone: 'UTC', day: '2-digit', month: 'short', year: 'numeric', hour: '2-digit', minute: '2-digit' }) + ' UTC';
  const ago = v => { if (v === null) return '-'; const s = (Date.now() - v) / 1000; if (s < 90) return 'just now'; const m = s / 60; if (m < 90) return Math.round(m) + ' min ago'; const h = m / 60; if (h < 36) return Math.round(h) + ' h ago'; return Math.round(h / 24) + ' d ago'; };
  const who = a => a ? (a === ME ? a + ' (you)' : a) : null;
  const incs = () => HIST.incidents || [];
  const toast = t => { const e = $('toast'); e.textContent = t; e.style.display = 'block'; clearTimeout(toast.t); toast.t = setTimeout(() => { e.style.display = 'none'; }, 2600); };
  const host = location.hostname;

  // ------------------------------------------------------------------------------------------------ roles, navigation, theme, sign-in
  function buildNav() {
    $('nav').innerHTML = NAVS[ROLE].map(n => '<button class="nav-btn' + (n[0] === VIEW ? ' active' : '') + '" data-go="' + n[0] + '">' + n[1] + '</button>').join('');
    $('nav').querySelectorAll('button').forEach(b => { b.onclick = () => go(b.dataset.go); });
    document.querySelectorAll('#role-seg button').forEach(b => b.classList.toggle('on', b.dataset.role === ROLE));
  }
  function setRole(r, land) {
    ROLE = r; put('autosre-role', r);
    VIEW = land || (r === 'executive' ? 'overview' : 'incidents'); buildNav(); go(VIEW);
  }
  function go(name) {
    if (!NAVS[ROLE].some(n => n[0] === name)) { ROLE = NAVS.executive.some(n => n[0] === name) ? 'executive' : 'oncall'; put('autosre-role', ROLE); }
    VIEW = name; try { sessionStorage.setItem('autosre-view', name); } catch (e) { /* ignore */ }
    document.querySelectorAll('.view-section').forEach(s => s.classList.remove('active'));
    const sec = $(SECTION[name] || 'view-' + name); if (sec) sec.classList.add('active');
    buildNav(); render(); window.scrollTo(0, 0);
  }
  window.go = go;
  function setTheme(t) {
    document.documentElement.setAttribute('data-theme', t); put('autosre-theme', t);
    document.querySelectorAll('#theme-seg button').forEach(b => b.classList.toggle('on', b.dataset.themeSet === t));
  }
  function signIn(name) {
    const n = String(name || '').replace(/[^A-Za-z0-9._ -]/g, '').trim().slice(0, 32); if (!n) return;
    ME = n; put('autosre-user', n); $('login').classList.remove('on'); $('me').textContent = ME; $('whoami').style.display = ''; render();
  }
  function signOut() { ME = ''; try { localStorage.removeItem('autosre-user'); } catch (e) { /* ignore */ } closeDrawer(); $('whoami').style.display = 'none'; $('user').value = ''; $('login').classList.add('on'); $('user').focus(); }

  // ------------------------------------------------------------------------------------------------ data
  async function loadHistory() {
    try {
      HIST = await (await fetch('/api/history')).json();
      if (!document.activeElement || !/INPUT|SELECT/.test(document.activeElement.tagName) || document.activeElement.type === 'range') render();
    } catch (e) { /* the page keeps what it has; the header badges already show connectivity */ }
  }
  async function loadBundle() { try { BUNDLE = await (await fetch('/static/history.json')).json(); } catch (e) { BUNDLE = { runs: [], changes: [] }; } }
  window.onState = function (st) {
    // remember when alerts first appeared, so the tracker can show the fault being noticed before any incident exists
    const names = (st.alerts || []).map(a => a.alertname).sort().join(',');
    if (names && !REPLAY.watch) REPLAY.watch = { names: names, list: st.alerts, since: Date.now() };
    if (!names) REPLAY.watch = null;
    if (VIEW === 'replay' && !REPLAY.playing) renderReplay();
  };

  // ------------------------------------------------------------------------------------------------ pages
  const PAGES = { incidents: pageIncidents, replay: pageReplay, trends: pageTrends, services: pageServices, playbooks: pagePlaybooks, settings: pageSettings, runs: pageRuns };
  function render() {
    const f = PAGES[VIEW]; if (!f) return;
    const sec = $('view-' + VIEW); if (!sec) return;
    sec.innerHTML = '<div class="cx">' + f() + '</div>';
    wire(sec);
    if (VIEW === 'replay') renderReplay();
  }
  function wire(sec) {
    sec.querySelectorAll('[data-id]').forEach(r => { r.onclick = () => openDrawer(r.dataset.id); });
    sec.querySelectorAll('[data-svc]').forEach(b => { b.onclick = () => { F.service = b.dataset.svc; F.page = 0; setRole('oncall', 'incidents'); }; });
    const on = (id, fn) => { const e = $(id); if (e) e.onchange = fn; };
    [['f-action', 'action'], ['f-cause', 'cause'], ['f-service', 'service'], ['f-ml', 'ml']].forEach(([id, k]) => on(id, e => { F[k] = e.target.value; F.page = 0; render(); }));
    on('f-range', e => { F.range = +e.target.value; F.page = 0; render(); });
    if ($('f-q')) $('f-q').oninput = e => { F.q = e.target.value; F.page = 0; render(); $('f-q').focus(); $('f-q').setSelectionRange(F.q.length, F.q.length); };
    if ($('f-clear')) $('f-clear').onclick = () => { Object.assign(F, { action: '', cause: '', service: '', ml: '', q: '', page: 0 }); render(); };
    if ($('prev')) $('prev').onclick = () => { F.page--; render(); };
    if ($('next')) $('next').onclick = () => { F.page++; render(); };
    if ($('export')) $('export').onclick = exportCsv;
  }
  const matches = i => {
    const t = ms(i.t);
    if (F.range < 9999 && t !== null && Date.now() - t > F.range * 864e5) return false;
    if (F.action && (F.action === 'open' ? !i.open : i.action !== F.action)) return false;
    if (F.cause && (i.cause || 'unknown') !== F.cause) return false;
    if (F.service && i.service !== F.service) return false;
    if (F.ml && (i.ml || 'none') !== F.ml) return false;
    if (F.q && ![i.id, i.service, i.cause, cname(i.cause), i.assignee, i.action, i.playbook].join(' ').toLowerCase().includes(F.q.toLowerCase())) return false;
    return true;
  };
  const kpi = (l, v, d) => '<div class="card cx-kpi"><div class="l">' + l + '</div><div class="v">' + v + '</div><div class="d">' + d + '</div></div>';
  const opt = (arr, cur, all) => '<option value="">' + all + '</option>' + arr.map(([v, l]) => '<option value="' + v + '"' + (cur === v ? ' selected' : '') + '>' + l + '</option>').join('');

  function pageIncidents() {
    const all = incs(), list = all.filter(matches), open = all.filter(i => i.open);
    const inr = all.filter(i => F.range >= 9999 || Date.now() - ms(i.t) <= F.range * 864e5);
    const fixed = inr.filter(i => i.action === 'auto_fixed'), esc_ = inr.filter(i => i.action === 'escalated' || i.action === 'vetoed');
    const live = inr.filter(i => i.action !== 'dry_run' && i.action !== 'decision');
    const needs = open.map(i => '<div class="item" data-id="' + i.id + '"><span class="cx-pill p-open">OPEN</span><b class="mono">' + esc(i.id) + '</b><span class="' + (i.sev === 'SEV2' ? 'sev2' : '') + '">' + i.sev + '</span><span>' + esc(i.service) + '</span><span class="why">' + esc(cname(i.cause)) + ': ' + esc(i.reason) + '</span><span class="mute">' + ago(ms(i.received)) + (i.assignee ? ' &middot; ' + esc(who(i.assignee)) + ' investigating' : ' &middot; <b style="color:var(--status-err)">unacknowledged</b>') + '</span></div>').join('');
    const per = 25, pages = Math.max(1, Math.ceil(list.length / per)); F.page = Math.min(F.page, pages - 1);
    const rows = list.slice(F.page * per, F.page * per + per).map(i => { const a = label(i);
      return '<tr class="row" data-id="' + i.id + '"><td title="' + fmt(ms(i.t)) + '">' + ago(ms(i.t)) + '<div class="mute" style="font-size:0.68rem">' + fmt(ms(i.t)).slice(0, 17) + '</div></td><td class="mono">' + esc(i.id) + '</td><td class="' + (i.sev === 'SEV2' ? 'sev2' : '') + '">' + i.sev + '</td><td>' + esc(i.service) + '</td><td>' + esc(cname(i.cause)) + ((i.ambiguous || []).length > 1 ? ' <span class="cx-pill p-veto" title="deciding alerts of two causes fired together">2 causes</span>' : '') + '</td><td>' + (i.confidence === null || i.confidence === undefined ? '-' : Math.round(i.confidence * 100) + '%') + '</td><td>' + esc(i.ml || (i.cause ? 'n/a' : '-')) + '</td><td><span class="cx-pill ' + a[1] + '">' + a[0] + '</span></td><td>' + dur(i.ttr_s) + '</td><td>' + esc(who(i.assignee) || (i.action === 'auto_fixed' ? 'automation' : '-')) + '</td></tr>'; }).join('');
    const err = HIST.errors && Object.keys(HIST.errors).length ? '<div class="cx-note">Some data sources did not answer (' + esc(Object.keys(HIST.errors).join(', ')) + '); the list shows what was available.</div>' : '';
    return '<h1>Incidents</h1><p class="sub">' + list.length + ' of ' + all.length + ' recorded incidents match. Click a row for its timeline, evidence and, with Transparency mode on, what each setting would have decided.</p>' + err +
      (open.length ? '<div class="card cx-need"><h2>Needs you now (' + open.length + ')</h2>' + needs + '</div>' : '') +
      '<div class="cx-kpis">' + kpi('Incidents', inr.length, 'in the selected range') + kpi('Auto-resolved', pct(fixed.length, live.length), fixed.length + ' of ' + live.length + ' with live remediation') +
      kpi('Escalated to a person', pct(esc_.length, live.length), esc_.length + ' needed a human') + kpi('Time to fix (auto)', dur(mean(fixed.map(i => i.ttr_s))), 'first alert to verified recovery') +
      kpi('Time to fix (human)', dur(mean(esc_.map(i => i.ttr_s))), 'first alert to resolved') + kpi('Open now', open.length, 'waiting for a person') + '</div>' +
      '<div class="cx-bar"><select id="f-range"><option value="1"' + (F.range === 1 ? ' selected' : '') + '>Last 24 hours</option><option value="7"' + (F.range === 7 ? ' selected' : '') + '>Last 7 days</option><option value="30"' + (F.range === 30 ? ' selected' : '') + '>Last 30 days</option><option value="9999"' + (F.range === 9999 ? ' selected' : '') + '>All time</option></select>' +
      '<select id="f-action">' + opt([['open', 'Open now'], ['auto_fixed', 'Auto-fixed'], ['escalated', 'Escalated'], ['vetoed', 'Held by ML gate'], ['dry_run', 'Dry run'], ['running', 'Fixing now']], F.action, 'All outcomes') + '</select>' +
      '<select id="f-cause">' + opt(Object.keys(CAUSE).map(k => [k, CAUSE[k]]).concat([['unknown', 'Unknown cause']]), F.cause, 'All causes') + '</select>' +
      '<select id="f-service">' + opt(['api-gateway', 'transaction-service', 'database', 'frontend'].map(s => [s, s]), F.service, 'All services') + '</select>' +
      '<select id="f-ml">' + opt([['agree', 'ML agrees'], ['disagree', 'ML disagrees'], ['unavailable', 'ML unavailable'], ['none', 'ML not consulted']], F.ml, 'Any ML verdict') + '</select>' +
      '<input type="search" id="f-q" placeholder="Search id, service, cause, person" value="' + esc(F.q === '' ? '' : F.q).replace(/^-$/, '') + '"><button class="cx-btn" id="f-clear">Clear</button><div class="cx-grow"></div><button class="cx-btn" id="export">Export CSV</button></div>' +
      '<div class="card cx-tw"><table><thead><tr><th>When</th><th>ID</th><th>Sev</th><th>Service</th><th>Cause</th><th>Conf.</th><th>ML</th><th>Outcome</th><th>Time to fix</th><th>Owner</th></tr></thead><tbody>' + (rows || '<tr><td colspan="10" class="mute">No incidents recorded yet. They appear here as the engine receives them.</td></tr>') + '</tbody></table></div>' +
      '<div class="cx-pager"><button class="cx-btn" id="prev"' + (F.page === 0 ? ' disabled' : '') + '>Previous</button> page ' + (F.page + 1) + ' of ' + pages + ' <button class="cx-btn" id="next"' + (F.page >= pages - 1 ? ' disabled' : '') + '>Next</button></div>';
  }
  function exportCsv() {
    const rows = [['id', 'time', 'severity', 'service', 'cause', 'confidence', 'ml', 'outcome', 'ttr_s']].concat(incs().filter(matches).map(i => [i.id, i.t, i.sev, i.service, i.cause || 'unknown', i.confidence, i.ml, i.action, i.ttr_s]));
    const a = document.createElement('a'); a.href = URL.createObjectURL(new Blob([rows.map(r => r.join(',')).join(String.fromCharCode(10))], { type: 'text/csv' })); a.download = 'autosre-incidents.csv'; a.click();
  }

  // ---- drawer
  function openDrawer(id) {
    const i = incs().find(x => x.id === id); if (!i) return;
    const a = label(i), vr = variantRows(i);
    const tl = (i.events || []).map(e => '<li class="s-' + esc(e.state) + '"><span class="mono mute">' + hms(ms(e.t)) + '</span><span class="dot"></span><span>' + esc(e.text) + '</span></li>').join('');
    const ev = (i.matched || []).map(k => '<tr><td class="mono">' + esc(k) + '</td><td>fired</td></tr>').concat((i.missing || []).map(k => '<tr><td class="mono">' + esc(k) + '</td><td class="mute">did not fire</td></tr>')).join('');
    const vt = vr.map(r => '<tr class="' + (r.active ? 'tm-active' : '') + '"><td>' + esc(r.label) + (r.active ? ' (in use)' : '') + '</td><td class="' + (r.escalate ? 'tm-esc' : 'tm-act') + '">' + esc(r.decision) + '</td><td>' + esc(r.why) + '</td></tr>').join('');
    $('drawer').innerHTML = '<div class="cx-dh"><h2 class="mono">' + esc(i.id) + '</h2><span class="cx-pill ' + a[1] + '">' + a[0] + '</span><span class="' + (i.sev === 'SEV2' ? 'sev2' : '') + '">' + i.sev + '</span><span>' + esc(i.service) + '</span><div class="cx-grow"></div><button class="cx-btn" id="dclose">Close</button></div><div class="cx-db cx">' +
      '<div class="card cx-sec"><h3>Summary</h3><div class="cx-kv"><div>Cause</div><div>' + esc(cname(i.cause)) + '</div><div>First alert</div><div>' + fmt(ms(i.t)) + '</div><div>Incident received</div><div>' + fmt(ms(i.received)) + '</div><div>Confidence</div><div>' + (i.confidence === null || i.confidence === undefined ? '-' : Math.round(i.confidence * 100) + '%') + '</div><div>ML second opinion</div><div>' + esc(i.ml || 'not consulted') + '</div><div>Decision</div><div>' + esc(i.reason || (i.action === 'auto_fixed' ? 'Fixed by ' + i.playbook + ' and verified' : '-')) + '</div><div>Playbook</div><div>' + esc(i.playbook || '-') + (i.duration_s ? ' (' + i.duration_s + ' s)' : '') + '</div><div>Time to fix</div><div>' + dur(i.ttr_s) + '</div><div>Owner</div><div>' + esc(who(i.assignee) || (i.action === 'auto_fixed' ? 'automation' : 'nobody yet')) + '</div>' + (i.note ? '<div>Resolution note</div><div>' + esc(i.note) + '</div>' : '') + '</div>' + (i.explanation ? '<p class="mute" style="margin-top:0.6rem;font-size:0.8rem">' + esc(i.explanation) + '</p>' : '') + '</div>' +
      '<div class="card cx-sec"><h3>Actions</h3><div class="cx-actions">' + (i.open && !i.assignee ? '<button class="cx-btn primary" id="a-ack">Acknowledge</button>' : '') + (i.open ? '<input type="text" id="a-note" placeholder="Resolution note" maxlength="200" style="min-width:200px"><button class="cx-btn" id="a-res">Mark resolved</button>' : '') +
      '<button class="cx-btn" id="a-replay">Replay this incident</button><a class="cx-btn" target="_blank" href="http://' + host + ':3000/d/autosre-main/autosre">Open in Grafana</a><button class="cx-btn" id="a-link">Copy link</button></div></div>' +
      (i.evidence ? evidenceCard(i.evidence) : '') +
      '<div class="card cx-sec"><h3>Timeline</h3><ul class="cx-tl">' + tl + '</ul></div>' +
      '<div class="card cx-sec"><h3>Evidence</h3><p class="mute" style="margin-bottom:0.5rem;font-size:0.8rem">Alerts: ' + (i.alerts || []).map(x => '<span class="mono">' + esc(x.name) + '</span> on ' + esc(x.service)).join(', ') + '</p>' + (ev ? '<table><thead><tr><th>Symptom of the matched cause</th><th>State</th></tr></thead><tbody>' + ev + '</tbody></table>' : '<p class="mute">No known signature matched these alerts.</p>') + '</div>' +
      '<div class="card cx-sec tm-panel"><h3>What each setting would have decided</h3><table><thead><tr><th>Setting</th><th>Decision</th><th>Why</th></tr></thead><tbody>' + (vt || '<tr><td colspan="3" class="mute">Not recorded for this incident.</td></tr>') + '</tbody></table></div></div>';
    $('drawer').classList.add('on'); $('scrim').classList.add('on');
    $('dclose').onclick = closeDrawer;
    const post = async (path, body, ok) => { try { const r = await fetch(path, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(Object.assign({ id: i.id, by: ME }, body)) }); if (!r.ok) throw new Error('HTTP ' + r.status); toast(ok); await loadHistory(); openDrawer(i.id); } catch (e) { toast('Could not record it: ' + e.message); } };
    if ($('a-ack')) $('a-ack').onclick = () => post('/api/ack', {}, 'Acknowledged. You now own ' + i.id + '.');
    if ($('a-res')) $('a-res').onclick = () => post('/api/resolve', { note: $('a-note').value }, 'Marked resolved.');
    $('a-replay').onclick = () => { closeDrawer(); REPLAY.id = i.id; go('replay'); startReplay(); };
    $('a-link').onclick = () => { const u = location.origin + '/#' + i.id; (navigator.clipboard ? navigator.clipboard.writeText(u) : Promise.reject()).then(() => toast('Link copied'), () => toast(u)); };
  }
  // Evidence the dispatcher collected on its own when a person was asked: pod states, recent events and log tails.
  function evidenceCard(e) {
    const d = e.data || {};
    if (!e.data) return '<div class="card cx-sec"><h3>Evidence collected for you</h3><p class="mute">' + (e.status === 'running' ? 'Being collected right now...' : 'Could not be collected: ' + esc(e.reason || e.status)) + '</p></div>';
    const pods = (d.pods || []).map(p => '<tr><td class="mono">' + esc(p.name) + '</td><td>' + esc(p.phase) + '</td><td>' + esc(p.ready ? 'ready' : 'not ready') + '</td><td>' + esc(p.restarts) + '</td><td>' + esc(p.reason || '-') + '</td></tr>').join('');
    const evs = (d.events || []).map(x => '<div class="mono" style="font-size:0.74rem"><span class="mute">' + esc(x.reason) + '</span> ' + esc(x.message) + '</div>').join('');
    const logs = Object.entries(d.logs || {}).map(([k, v]) => '<div style="margin-top:0.5rem"><b class="mono" style="font-size:0.74rem">' + esc(k) + '</b><pre class="rp-log" style="max-height:150px;margin-top:0.2rem">' + esc(v) + '</pre></div>').join('');
    return '<div class="card cx-sec"><h3>Evidence collected for you (read-only, ' + esc(e.service) + ')</h3>' + (d.deployment ? '<p class="mute" style="font-size:0.8rem;margin-bottom:0.4rem">Deployment: ' + esc(d.deployment) + '</p>' : '') +
      (pods ? '<table><thead><tr><th>Pod</th><th>Phase</th><th>Ready</th><th>Restarts</th><th>Waiting / terminated</th></tr></thead><tbody>' + pods + '</tbody></table>' : '<p class="mute">No pod found.</p>') +
      (evs ? '<div style="margin-top:0.6rem"><b style="font-size:0.72rem" class="mute">RECENT EVENTS</b>' + evs + '</div>' : '') + logs + '</div>';
  }
  function closeDrawer() { $('drawer').classList.remove('on'); $('scrim').classList.remove('on'); }

  // ---- fault replay: live tracker + replay
  function currentIncident() {
    const all = incs();
    if (REPLAY.id) return all.find(i => i.id === REPLAY.id) || null;
    return all[0] || null;                                      // newest
  }
  function pageReplay() {
    const opts = incs().slice(0, 100).map(i => '<option value="' + esc(i.id) + '"' + (i.id === REPLAY.id ? ' selected' : '') + '>' + esc(i.id) + ' ' + esc(cname(i.cause)) + ' (' + fmt(ms(i.t)).slice(0, 17) + ')</option>').join('');
    return '<h1>Fault Replay</h1><p class="sub">Follows the newest fault live: each step lights up when the real event happens (alert, engine decision, dispatcher step), with a running timer on the step in progress. Pick a finished incident to replay its recorded timeline at a faster pace.</p>' +
      '<div class="card cx-sec"><div class="rp-head"><span id="rp-badge" class="badge ok">No incident</span><select id="rp-pick"><option value="">Follow the latest (live)</option>' + opts + '</select>' +
      '<button class="cx-btn" id="rp-play">Replay</button><select id="rp-speed"><option value="1"' + (REPLAY.speed === 1 ? ' selected' : '') + '>Real time</option><option value="10"' + (REPLAY.speed === 10 ? ' selected' : '') + '>10x faster</option><option value="30"' + (REPLAY.speed === 30 ? ' selected' : '') + '>30x faster</option></select><span class="mute" id="rp-clock"></span></div>' +
      '<p id="rp-why" class="mute" style="font-size:0.84rem;margin-bottom:0.6rem"></p><div class="rp-progress"><i id="rp-bar" style="width:0%"></i></div><div id="rp-pipe" class="rp-pipe"></div><div id="rp-var" class="tm-panel"></div></div>' +
      '<div class="card cx-sec" style="margin-top:0.9rem"><h3>Activity log</h3><div id="rp-log" class="rp-log">waiting for activity...</div></div>';
  }
  function trackerInput() {
    // what to draw right now: events, the clock, and whether the incident has ended
    const inc = currentIncident(), st = window.STATE || {}, firing = (st.alerts || []).length;
    if (REPLAY.playing && inc) return { inc: inc, events: inc.events, now: REPLAY.clock, final: REPLAY.clock >= REPLAY.to, live: false };
    if (inc) {
      const last = inc.events.reduce((m, e) => Math.max(m, ms(e.t) || 0), 0);
      const running = inc.action === 'running' || (inc.action === 'auto_fixed' && false);
      // a fault that is firing but has no incident newer than the last finished one: show it being noticed, live
      const noticing = !REPLAY.id && firing && REPLAY.watch && REPLAY.watch.since > ms(inc.received) - 1000 && !running;
      if (noticing) return { inc: null, events: [{ stage: 1, state: 'start', t: new Date(REPLAY.watch.since).toISOString(), text: 'Alerts firing: ' + REPLAY.watch.names.replace(/,/g, ', ') + '. Waiting for their hold time, then for Alertmanager to group them.' }], now: Date.now(), final: false, live: true };
      return { inc: inc, events: inc.events, now: running ? Date.now() : Infinity, final: !running, live: running, quiet: !firing };
    }
    if (firing && REPLAY.watch) return { inc: null, events: [{ stage: 1, state: 'start', t: new Date(REPLAY.watch.since).toISOString(), text: 'Alerts firing: ' + REPLAY.watch.names.replace(/,/g, ', ') + '. Waiting for the engine.' }], now: Date.now(), final: false, live: true };
    return { inc: null, events: [], now: Date.now(), final: false, live: false };
  }
  function renderReplay() {
    if (!$('rp-pipe')) return;
    const t = trackerInput(), nowMs = t.now === Infinity ? Date.now() : t.now;
    const stages = trackerStages(t.events, t.now === Infinity ? 8.64e15 : t.now, { final: t.final, live: t.live, reason: t.inc && t.inc.reason, alertsQuiet: t.quiet });
    const startAt = stages.length && stages[0].t !== null ? stages[0].t : null;
    $('rp-pipe').innerHTML = stages.map(s => {
      let when = '';
      if (s.state === 'active') when = '<span class="rp-spin"></span>' + (s.t !== null ? Math.max(0, Math.round((nowMs - s.t) / 1000)) + ' s' : 'now');
      else if (s.state === 'done' || s.state === 'stop' || s.state === 'warn') when = (s.state === 'stop' ? 'STOPPED ' : '') + (s.t !== null ? hms(s.t) + (startAt !== null && s.n > 1 ? ' (+' + Math.max(0, Math.round((s.t - startAt) / 1000)) + ' s)' : '') : '');
      return '<div class="rp-stage ' + s.state + '"><span class="when">' + when + '</span><div class="n">Step ' + s.n + '</div><div class="t">' + esc(s.title) + '</div><div class="d">' + esc(s.text) + '</div></div>';
    }).join('');
    const done = stages.filter(s => s.state === 'done' || s.state === 'warn').length, stopped = stages.some(s => s.state === 'stop');
    $('rp-bar').style.width = Math.round(100 * done / 8) + '%';
    const bad = $('rp-badge');
    const cls = (c, tx) => { bad.className = 'badge ' + c; bad.textContent = tx; };
    if (REPLAY.playing) cls('ok', 'Replaying'); else if (t.live) cls('fail', 'LIVE: fix in progress'); else if (!t.inc) cls('ok', 'No incident');
    else if (stopped) cls('fail', t.inc.action === 'vetoed' || t.inc.action === 'escalated' ? 'Escalated to a person' : 'Stopped'); else if (stages[6].state === 'done') cls('ok', 'Solved and verified'); else cls('ok', 'Decision recorded');
    $('rp-why').textContent = t.inc ? (t.inc.explanation || t.inc.reason || '') : (t.live ? 'A fault is being noticed right now.' : 'Nothing to follow yet. When a fault raises alerts, each step lights up as it happens.');
    $('rp-clock').textContent = REPLAY.playing ? 'virtual time ' + hms(REPLAY.clock) : (t.live ? 'updates every few seconds' : '');
    $('rp-play').textContent = REPLAY.playing ? 'Stop replay' : 'Replay';
    const logs = (t.events || []).filter(e => ms(e.t) === null || ms(e.t) <= (t.now === Infinity ? 8.64e15 : t.now)).map(e => '<div><span class="ts">' + hms(ms(e.t)) + '</span> <span class="k-' + esc(e.state) + '">' + (e.stage ? 'step ' + e.stage + ': ' : '') + esc(e.text) + '</span></div>').join('');
    $('rp-log').innerHTML = logs || 'waiting for activity...';
    if (t.inc && $('rp-var')) { const rows = variantRows(t.inc); $('rp-var').innerHTML = rows.length ? '<div style="font-size:0.72rem;font-weight:700;letter-spacing:0.05em;text-transform:uppercase;color:var(--text-muted);margin-bottom:0.3rem">What each setting would have decided</div><table><thead><tr><th>Setting</th><th>Decision</th><th>Why</th></tr></thead><tbody>' + rows.map(r => '<tr class="' + (r.active ? 'tm-active' : '') + '"><td>' + esc(r.label) + (r.active ? ' (in use)' : '') + '</td><td class="' + (r.escalate ? 'tm-esc' : 'tm-act') + '">' + esc(r.decision) + '</td><td>' + esc(r.why) + '</td></tr>').join('') + '</tbody></table>' : ''; }
    if ($('rp-pick')) $('rp-pick').onchange = e => { stopReplay(); REPLAY.id = e.target.value; renderReplay(); };
    if ($('rp-speed')) $('rp-speed').onchange = e => { REPLAY.speed = +e.target.value; };
    if ($('rp-play')) $('rp-play').onclick = () => { if (REPLAY.playing) { stopReplay(); renderReplay(); } else startReplay(); };
  }
  function startReplay() {
    const inc = currentIncident(); if (!inc || !inc.events.length) { toast('Nothing to replay yet'); return; }
    const times = inc.events.map(e => ms(e.t)).filter(v => v !== null);
    REPLAY.from = Math.min.apply(null, times); REPLAY.to = Math.max.apply(null, times) + 2000; REPLAY.clock = REPLAY.from; REPLAY.playing = true;
    clearInterval(REPLAY.timer);
    REPLAY.timer = setInterval(() => { REPLAY.clock += 100 * REPLAY.speed; if (REPLAY.clock >= REPLAY.to) { REPLAY.clock = REPLAY.to; stopReplay(true); } renderReplay(); }, 100);
    renderReplay();
  }
  function stopReplay(keepEnd) { clearInterval(REPLAY.timer); REPLAY.playing = false; if (!keepEnd) REPLAY.clock = 0; }
  setInterval(() => { if (VIEW === 'replay' && !REPLAY.playing && $('rp-pipe')) renderReplay(); }, 1000);          // the running timers

  // ---- trends, services, playbooks, settings, runs
  const COL = { auto: 'var(--status-ok)', esc: 'var(--status-err)', dry: 'var(--border-strong)', other: 'var(--gold-accent)' };
  function stackChart(b) {
    if (!b.rows.length) return '<p class="mute">No incidents recorded yet.</p>';
    const max = Math.max.apply(null, b.rows.map(r => r.auto + r.esc + r.dry + r.other)) || 1, W = 720, H = 190, L = 30, bw = (W - L) / b.rows.length;
    let g = ''; b.rows.forEach((r, k) => { let y = H - 18; [['dry', COL.dry], ['auto', COL.auto], ['esc', COL.esc], ['other', COL.other]].forEach(([key, c]) => { const h = (r[key] / max) * (H - 30); y -= h; if (r[key]) g += '<rect x="' + (L + k * bw + 1) + '" y="' + y + '" width="' + Math.max(bw - 2, 2) + '" height="' + h + '" fill="' + c + '"><title>' + new Date(r.start).toISOString().slice(0, 10) + ': ' + r[key] + ' ' + key + '</title></rect>'; }); });
    return '<svg viewBox="0 0 ' + W + ' ' + H + '" width="100%"><text x="0" y="12">' + max + '</text><text x="0" y="' + (H - 18) + '">0</text><line class="ax" x1="' + L + '" x2="' + W + '" y1="' + (H - 18) + '" y2="' + (H - 18) + '"/>' + g + '<text x="' + L + '" y="' + (H - 4) + '">' + new Date(b.rows[0].start).toISOString().slice(0, 10) + '</text><text x="' + (W - 60) + '" y="' + (H - 4) + '">' + new Date(b.rows[b.rows.length - 1].start).toISOString().slice(0, 10) + '</text></svg><div class="cx-legend"><span><i style="background:' + COL.auto + '"></i>Auto-fixed</span><span><i style="background:' + COL.esc + '"></i>Escalated</span><span><i style="background:' + COL.dry + '"></i>Decision only / dry run</span><span><i style="background:' + COL.other + '"></i>Other</span></div>';
  }
  function bars(items, unit) {
    if (!items.length) return '<p class="mute">Nothing recorded yet.</p>';
    const max = Math.max.apply(null, items.map(x => x.v)) || 1, W = 720, H = 170, L = 36, bw = (W - L) / items.length;
    return '<svg viewBox="0 0 ' + W + ' ' + H + '" width="100%"><text x="0" y="12">' + Math.round(max) + '</text><line class="ax" x1="' + L + '" x2="' + W + '" y1="' + (H - 20) + '" y2="' + (H - 20) + '"/>' + items.map((x, k) => { const h = (x.v / max) * (H - 40); return '<rect x="' + (L + k * bw + 2) + '" y="' + (H - 20 - h) + '" width="' + Math.max(bw - 4, 3) + '" height="' + h + '" rx="2" fill="var(--navy-mid)"><title>' + esc(x.l) + ': ' + Math.round(x.v) + ' ' + unit + '</title></rect>'; }).join('') + '</svg>';
  }
  function pageTrends() {
    const all = incs(), b = buckets(all);
    const fix = all.filter(i => i.action === 'auto_fixed' && i.ttr_s).reverse().map(i => ({ v: i.ttr_s, l: i.id + ' ' + cname(i.cause) }));
    const causes = Object.keys(CAUSE).concat([null]).map(c => { const set = all.filter(i => (i.cause || null) === c); return [cname(c), set.length, set.filter(i => i.action === 'auto_fixed').length]; });
    const mx = Math.max.apply(null, causes.map(c => c[1]).concat([1]));
    return '<h1>Trends</h1><p class="sub">' + all.length + ' recorded incidents. Bars are per ' + b.unit + '.</p><div class="cx-grid2"><div class="card cx-ch" style="grid-column:1/-1"><h3>Incidents per ' + b.unit + ' by outcome</h3><p>Stacked by what the automation did.</p>' + stackChart(b) + '</div>' +
      '<div class="card cx-ch"><h3>Seconds from first alert to verified recovery</h3><p>Auto-fixed incidents, oldest to newest.</p>' + bars(fix, 's') + '</div>' +
      '<div class="card cx-ch"><h3>By cause</h3><p>Over the whole history.</p>' + causes.map(c => '<div style="margin:0.5rem 0"><div style="display:flex;justify-content:space-between;font-size:0.82rem"><span>' + esc(c[0]) + '</span><span class="mute">' + c[1] + ' incidents, ' + c[2] + ' auto-fixed</span></div><div class="cx-wbar"><i style="width:' + (c[1] / mx * 100) + '%"></i></div></div>').join('') + '</div></div>';
  }
  function pageServices() {
    const dep = (window.STATE && window.STATE.deployments) || [], all = incs();
    const cards = ['frontend', 'api-gateway', 'transaction-service', 'database'].map(s => { const set = all.filter(i => i.service === s), esc_ = set.filter(i => i.action === 'escalated' || i.action === 'vetoed'), open = set.filter(i => i.open).length, d = dep.find(x => x.name === s);
      const top = Object.entries(set.reduce((m, i) => { const k = cname(i.cause); m[k] = (m[k] || 0) + 1; return m; }, {})).sort((a, b) => b[1] - a[1])[0];
      return '<div class="card cx-sec"><h3 style="font-size:0.95rem;text-transform:none;letter-spacing:0;color:var(--title-ink)">' + s + ' ' + (open ? '<span class="cx-pill p-open">' + open + ' open</span>' : '<span class="cx-pill p-auto">no open incidents</span>') + '</h3><div class="cx-kv" style="grid-template-columns:130px 1fr"><div>Replicas ready</div><div>' + (d && !d.error ? d.ready + ' / ' + d.replicas : '-') + '</div><div>Incidents</div><div>' + set.length + '</div><div>Escalated</div><div>' + esc_.length + ' (' + pct(esc_.length, set.length) + ')</div><div>Last incident</div><div>' + (set[0] ? ago(ms(set[0].t)) : '-') + '</div><div>Most common cause</div><div>' + (top ? esc(top[0]) + ' (' + top[1] + ')' : '-') + '</div></div><button class="cx-btn" style="margin-top:0.6rem" data-svc="' + s + '">View its incidents</button></div>'; }).join('');
    return '<h1>Services</h1><p class="sub">The dependency chain: a failure lower in the chain makes everything above it alert, so the engine names the deepest failing service.</p><div class="cx-chain"><span class="node">frontend</span>&rarr;<span class="node">api-gateway</span>&rarr;<span class="node">transaction-service</span>&rarr;<span class="node">database</span></div><div class="cx-cards">' + cards + '</div>';
  }
  const CATALOG = [['restart_service.yml', 'Rolling restart of a service', 'transaction-service'], ['scale_replicas.yml', 'Scale out, optionally replace the pods (memory leak)', 'transaction-service'],
    ['clear_logs.yml', 'Clear old database logs', 'database'], ['restore_service.yml', 'Bring a service with no replica back to a ready replica', 'database, transaction-service, api-gateway'],
    ['rollback_deployment.yml', 'Go back to the previous working revision (kubernetes.core.k8s_rollback)', 'transaction-service, api-gateway'],
    ['fix_service_selector.yml', 'Put a Service selector back so it reaches its pods again', 'database, transaction-service, api-gateway'],
    ['collect_diagnostics.yml', 'Read-only: collect pod states, events and log tails for a person (no approval needed)', 'all four services']];
  window.CX_PLAYBOOKS = CATALOG;
  function pagePlaybooks() {
    const all = incs();
    const rows = CATALOG.map(([p, d, t]) => { const set = all.filter(i => i.playbook === p && i.duration_s); const ok = set.filter(i => i.verified);
      return '<tr><td class="mono">' + p + '</td><td>' + esc(d) + '</td><td>' + esc(t) + '</td><td>' + set.length + '</td><td>' + pct(ok.length, set.length) + '</td><td>' + (set.length ? Math.round(mean(set.map(i => i.duration_s))) + ' s' : '-') + '</td></tr>'; }).join('');
    return '<h1>Playbooks</h1><p class="sub">Only these approved Ansible playbooks can run, only on their allow-listed targets, and every run is checked afterwards by reading the Deployment, not by trusting the playbook.</p><div class="card cx-tw"><table><thead><tr><th>Playbook</th><th>What it does</th><th>Target</th><th>Runs</th><th>Verified recovery</th><th>Avg duration</th></tr></thead><tbody>' + rows + '</tbody></table></div>';
  }
  function pageSettings() {
    const h = window.STATE && window.STATE.dispatcher && window.STATE.dispatcher.health;
    const kv = h ? '<div>Live remediation</div><div><b>' + (h.auto_remediate ? 'ON' : 'off') + '</b></div><div>Confidence threshold</div><div><b>' + esc(h.min_confidence) + '</b></div><div>ML policy</div><div><b>' + esc(h.second_opinion_policy) + '</b></div>' : '<div>Dispatcher</div><div class="mute">not reachable</div>';
    const rows = (HIST.changes || []).concat(BUNDLE.changes || []).sort((a, b) => (b.t || '').localeCompare(a.t || '')).map(e => '<tr><td>' + fmt(ms(e.t)) + '</td><td>' + esc(e.setting) + '</td><td><b>' + esc(e.value) + '</b></td><td>' + esc(e.by) + '</td><td>' + esc(e.reason) + '</td></tr>').join('');
    return '<h1>Settings &amp; changes</h1><p class="sub">Every change to what the automation is allowed to do, who made it and why. The log is kept with the engine and copied to the S3 bucket, so it survives a lab restart.</p><div class="card cx-sec" style="margin-bottom:0.9rem"><h3>In effect now</h3><div class="cx-kv" style="grid-template-columns:200px 1fr">' + kv + '</div></div><div class="card cx-tw"><table><thead><tr><th>When</th><th>Setting</th><th>New value</th><th>By</th><th>Reason</th></tr></thead><tbody>' + (rows || '<tr><td colspan="5" class="mute">No changes recorded.</td></tr>') + '</tbody></table></div>';
  }
  function pageRuns() {
    const live = (HIST.runs || []).map(r => ({ name: (r.name || '') + (r.scenario ? ' ' + r.scenario : '') + (r.variant ? ' (' + r.variant + ')' : ''), when: r.t, mode: r.mode, text: (r.result ? r.result + '. ' : '') + (r.text || '') }));
    const rows = live.concat(BUNDLE.runs || []).map(r => '<tr><td><b>' + esc(r.name) + '</b></td><td>' + esc(r.when && r.when.length > 10 ? fmt(ms(r.when)) : r.when) + '</td><td>' + esc(r.mode) + '</td><td>' + esc(r.text) + '</td></tr>').join('');
    return '<h1>Validation runs</h1><p class="sub">Every fault run and validation session recorded so far, newest first for runs made through the fault console.</p><div class="card cx-tw"><table><thead><tr><th>Run</th><th>When</th><th>Kind</th><th>What it showed</th></tr></thead><tbody>' + (rows || '<tr><td colspan="4" class="mute">No runs recorded yet.</td></tr>') + '</tbody></table></div>';
  }

  // ------------------------------------------------------------------------------------------------ start
  document.querySelectorAll('#role-seg button').forEach(b => { b.onclick = () => setRole(b.dataset.role); });
  document.querySelectorAll('#theme-seg button').forEach(b => { b.onclick = () => setTheme(b.dataset.themeSet); });
  $('signout').onclick = signOut;
  $('loginform').onsubmit = e => { e.preventDefault(); signIn($('user').value); };
  $('scrim').onclick = closeDrawer; document.addEventListener('keydown', e => { if (e.key === 'Escape') closeDrawer(); });
  setTheme(document.documentElement.getAttribute('data-theme') || 'light');
  if (ME) { $('me').textContent = ME; $('whoami').style.display = ''; } else { $('login').classList.add('on'); setTimeout(() => $('user').focus(), 50); }
  let first = 'overview'; try { first = sessionStorage.getItem('autosre-view') || 'overview'; } catch (e) { /* ignore */ }
  if (!NAVS.executive.concat(NAVS.oncall).some(n => n[0] === first)) first = 'overview';
  ROLE = NAVS.oncall.some(n => n[0] === first) && !NAVS.executive.some(n => n[0] === first) ? 'oncall' : (NAVS.executive.some(n => n[0] === first) ? 'executive' : ROLE);
  VIEW = first; buildNav(); go(first);
  loadBundle(); loadHistory(); setInterval(loadHistory, 4000);
  if (location.hash.startsWith('#INC-')) { const id = location.hash.slice(1); setTimeout(() => { go('incidents'); openDrawer(id); }, 800); }
})();
