// Team notifications (only while this Studio is in a team): the bell in the top bar lists every notification (unread
// first; reviews for you, @mentions, your requests decided, your pending items changed, claims on what you own, pushes
// in areas you follow). Click one to jump to the item; Mark read / Mark all read. The "Notifications" card in the Team
// tab sets what pings you, the channels (bell, email, webhook), their timing, Focus and Away.
// Data from /api/team/notify/* (team_notify.py). Every string from the server is escaped before it is shown.
(function(){
  const TM = window.TeamMarks; if (!TM) return;
  const PRESS = (document.querySelector('meta[name="studio-press"]') || {}).content || "";
  const esc = TM.esc, dot = TM.dot;
  const say = m => (typeof toast === "function" ? toast(m) : alert(m));
  async function get(name, q){
    const r = await fetch("/api/team/notify/" + name + (q ? "?" + new URLSearchParams(q) : ""));
    const j = await r.json(); if (!r.ok) throw new Error(j.error || r.statusText); return j;
  }
  async function post(name, body){
    const r = await fetch("/api/team/notify/" + name, {method:"POST", headers:{"Content-Type":"application/json", "X-Studio-Press":PRESS}, body:JSON.stringify(body || {})});
    const j = await r.json(); if (!r.ok) throw new Error(j.error || r.statusText); return j;
  }
  function when(iso){
    if (!iso) return "";
    const d = new Date(iso); if (isNaN(d)) return "";
    const hm = d.toLocaleTimeString("en-GB", {hour:"2-digit", minute:"2-digit"});
    return d.toDateString() === new Date().toDateString() ? hm : `${d.getDate()} ${d.toLocaleString("en-GB", {month:"short"})} ${hm}`;
  }
  const RULES = {review_requested:"Reviews asked of me", mention:"@mentions of me", review_decided:"My requests approved or sent back",
                 pending_changed:"Someone changes an item I have pending", claim:"Someone claims something I own",
                 alert:"My own Ads alerts (from Alerts on this Mac)"};
  const AREAS = {ads:"Ads", email:"Email", seo:"SEO", social:"SoMe"};
  const TIMING = {instant:"Right away", hourly:"Hourly digest", daily:"Daily digest"};

  document.head.insertAdjacentHTML("beforeend", `<style>
  .tn-pop{position:fixed;z-index:60;width:380px;max-width:calc(100vw - 24px);max-height:70vh;display:none;flex-direction:column;background:var(--panel,#fff);border:1px solid var(--line,#ddd);border-radius:8px;box-shadow:0 8px 28px rgba(0,0,0,.18)}
  .tn-pop.on{display:flex}
  .tn-hd{display:flex;align-items:center;gap:8px;padding:8px 12px;border-bottom:1px solid var(--line2,#eee);font-size:13px}
  .tn-hd b{font-weight:500} .tn-hd .sp{flex:1}
  .tn-list{overflow:auto;flex:1}
  .tn-n{display:grid;grid-template-columns:auto minmax(0,1fr) auto;gap:2px 8px;padding:8px 12px;border-bottom:1px solid var(--line2,#eee);cursor:pointer;font-size:13px;align-items:baseline}
  .tn-n:hover{background:var(--accent-bg,#eef)} .tn-n.unread{border-left:3px solid var(--accent,#1967d2)}
  .tn-n .t{min-width:0;word-break:break-word} .tn-n .w{font-size:12px;color:var(--muted,#777);white-space:nowrap}
  .tn-n .l{grid-column:2/4;font-size:12px;color:var(--muted,#777);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
  .tn-empty{color:var(--muted,#777);font-size:13px;padding:14px 12px}
  .tn-chip{font-size:11px;border-radius:9px;padding:1px 7px;background:var(--accent-bg,#eef);color:var(--accent,#1967d2)}
  .tn-card label{display:flex;gap:6px;align-items:center;font-size:13px;margin:3px 0}
  .tn-card .row{display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin:4px 0;font-size:13px}
  .tn-card h4{font-size:12px;font-weight:500;color:var(--text2,#555);margin:10px 0 4px}
  .tn-card input[type=url]{flex:1;min-width:160px}
  .tn-card .hint{font-size:12px;color:var(--muted,#777)} .tn-card .warn{font-size:12px;color:#c5221f}
  </style>`);

  // ---------- the bell's list ----------
  const N = {list:[], more:false, next:null, open:false};
  const pop = document.createElement("div");
  pop.className = "tn-pop"; pop.id = "tnPop";
  document.body.appendChild(pop);

  async function countUnread(){ return (await get("unread")).unread || 0 }
  TM.countUnread = countUnread;

  async function load(more){
    try {
      const r = await get("list", more && N.next ? {limit:50, before:N.next} : {limit:50});
      N.list = more ? N.list.concat(r.notifications || []) : (r.notifications || []);
      N.more = !!r.more; N.next = r.next_before; TM.unread = r.unread || 0;
    } catch(e) { N.error = e.message }
    draw();
  }
  function draw(){
    if (!N.open) return;
    const unread = N.list.filter(n => !n.read).length;
    const items = N.list.slice().sort((a, b) => (a.read - b.read) || (b.id - a.id));
    pop.innerHTML = `<div class="tn-hd"><b>Notifications</b>${N.focus ? `<span class="tn-chip">Focus</span>` : ""}${N.away ? `<span class="tn-chip">Away</span>` : ""}<span class="sp"></span>
      ${unread ? `<button class="link" data-tnall>Mark all read</button>` : ""}<button class="link" data-tnset>Settings</button></div>
      <div class="tn-list">${N.error ? `<div class="tn-empty">${esc(N.error)}</div>` : items.length ? items.map(n => `<div class="tn-n ${n.read ? "" : "unread"}" data-tn="${esc(n.id)}">
        ${dot((n.who || {}).colour)}<div class="t">${esc(n.text)}</div><span class="w" title="${esc(n.at || "")}">${esc(when(n.at))}</span>
        <div class="l">${esc(n.label || "")}${n.read ? "" : ` · <a href="#" data-tnread="${esc(n.id)}">Mark read</a>`}</div></div>`).join("")
        : `<div class="tn-empty">Nothing yet. Reviews for you, @mentions and the other things you choose in Team &gt; Notifications show here.</div>`}
      ${N.more ? `<div class="tn-empty"><button class="link" data-tnmore>Older</button></div>` : ""}</div>`;
  }
  function place(anchor){
    const r = anchor.getBoundingClientRect();
    pop.style.top = (r.bottom + 6) + "px";
    pop.style.left = Math.max(12, Math.min(r.right - 380, window.innerWidth - 392)) + "px";
  }
  async function toggle(anchor){
    N.open = !N.open;
    pop.classList.toggle("on", N.open);
    if (!N.open) return;
    place(anchor || document.getElementById("tmkBell") || document.body);
    N.error = ""; pop.innerHTML = `<div class="tn-empty">Loading ...</div>`;
    get("prefs").then(r => { N.focus = !!r.prefs.focus.active; N.away = !!r.prefs.away.active; draw() }).catch(() => {});
    load(false);
  }
  function close(){ N.open = false; pop.classList.remove("on") }
  document.addEventListener("mousedown", e => { if (N.open && !pop.contains(e.target) && !e.target.closest("#tmkBell")) close() });
  document.addEventListener("keydown", e => { if (e.key === "Escape" && N.open) close() });

  async function markRead(ids, every){
    try { const r = await post("read", every ? {all:true} : {ids}); TM.unread = r.unread || 0 } catch(e) { return say(e.message) }
    N.list.forEach(n => { if (every || ids.includes(n.id)) n.read = true });
    if (TM.loadUnread) TM.loadUnread();
    draw();
  }
  async function jump(n){
    if (n.coll === "review"){
      const TR = window.TeamReviews;
      if (TR && TR.review) return TR.review(+n.item_id);
      if (TR && TR.open) return TR.open();
      return;
    }
    if (n.coll){
      await TM.open(n.coll, n.item_id);
      if (n.kind === "mention" && TM.openThread) setTimeout(() => TM.openThread(n.coll, n.item_id, n.label), 400);
      return;
    }
    if (n.area && Object.prototype.hasOwnProperty.call(AREAS, n.area) && typeof setArea === "function") setArea(n.area);
  }
  pop.addEventListener("click", async e => {
    if (e.target.closest("[data-tnall]")) return markRead([], true);
    if (e.target.closest("[data-tnset]")){ close(); if (window.TeamTab) window.TeamTab.show("notifications"); return }
    if (e.target.closest("[data-tnmore]")) return load(true);
    const r = e.target.closest("[data-tnread]");
    if (r){ e.preventDefault(); e.stopPropagation(); return markRead([+r.dataset.tnread]) }
    const row = e.target.closest("[data-tn]");
    if (!row) return;
    const n = N.list.find(x => x.id === +row.dataset.tn); if (!n) return;
    if (!n.read) markRead([n.id]);
    close();
    try { await jump(n) } catch(err) { say(err.message) }
  });
  TM.on(kind => { if ((kind === "notify" || kind === "mention") && N.open) load(false) });

  // ---------- settings card (Team tab) ----------
  let CARD = null;
  async function card(el){
    CARD = el; if (!el) return;
    el.innerHTML = `<h3>Notifications</h3><div class="tn-empty">Loading ...</div>`;
    let r; try { r = await get("prefs") } catch(e) { el.innerHTML = `<h3>Notifications</h3><div class="tn-empty">${esc(e.message)}</div>`; return }
    drawCard(el, r);
  }
  const sel = (key, val) => `<select data-k="${esc(key)}">${Object.keys(TIMING).map(t => `<option value="${t}" ${t === val ? "selected" : ""}>${TIMING[t]}</option>`).join("")}</select>`;
  function untilText(s){
    if (!s.active) return "";
    return s.until ? `until ${esc(when(s.until))}` : "until you turn it off";
  }
  function drawCard(el, r){
    const p = r.prefs, srv = r.server || {}, backups = r.backups || [];
    el.innerHTML = `<h3>Notifications</h3><div class="tn-card">
      <h4>Ping me when</h4>
      ${Object.keys(RULES).map(k => `<label><input type="checkbox" data-rule="${k}" ${p.rules[k] ? "checked" : ""}>${RULES[k]}</label>`).join("")}
      <div class="hint">Edits never ping anyone.</div>
      <h4>Follow pushes and publishes in</h4>
      <div class="row">${Object.keys(AREAS).map(a => `<label><input type="checkbox" data-follow="${a}" ${p.follow[a] ? "checked" : ""}>${AREAS[a]}</label>`).join("")}</div>
      <h4>How and when</h4>
      <div class="row">Bell ${sel("bell", p.bell.timing)}<span class="hint">Reviews for you and @mentions are always right away.</span></div>
      <div class="row"><label><input type="checkbox" data-on="email" ${p.email.on ? "checked" : ""} ${srv.email ? "" : "disabled"}>Email</label>${sel("email", p.email.timing)}
        ${srv.email ? `<button class="link" data-test="email">Send a test</button>` : `<span class="hint">The team server has no email settings.</span>`}</div>
      ${p.email.last_error ? `<div class="warn">Email: ${esc(p.email.last_error)} (retrying)</div>` : ""}
      <div class="row"><label><input type="checkbox" data-on="webhook" ${p.webhook.on ? "checked" : ""} ${p.webhook.set ? "" : "disabled"}>Webhook</label>${sel("webhook", p.webhook.timing)}
        ${p.webhook.set ? `<span class="hint">Set (${esc(p.webhook.host)})</span><button class="link" data-test="webhook">Send a test</button><button class="link" data-hookdel>Remove</button>` : ""}</div>
      <div class="row"><input type="url" id="tnHook" placeholder="${p.webhook.set ? "Replace with a new https:// URL" : "https:// webhook URL (Slack, a Telegram bot URL ...)"}"><button data-hookset>Save</button></div>
      ${p.webhook.last_error ? `<div class="warn">Webhook: ${esc(p.webhook.last_error)} (retrying)</div>` : ""}
      <div class="row">Daily digest at <input type="time" id="tnDaily" value="${esc(p.daily_at)}"></div>
      <h4>Focus</h4>
      <div class="row">${p.focus.active ? `<span class="tn-chip">On ${untilText(p.focus)}</span><button data-focusoff>End Focus</button>`
        : `<button data-focus="60">1 hour</button><button data-focus="240">4 hours</button><button data-focus="0">Until I turn it off</button>`}</div>
      <div class="hint">Focus: only the bell counts; email and webhook wait and come as one digest when it ends.</div>
      <h4>Away</h4>
      <div class="row"><label><input type="checkbox" id="tnAway" ${p.away.on ? "checked" : ""}>I'm away</label>
        backup <select id="tnBackup"><option value="">Nobody</option>${backups.map(b => `<option value="${esc(b.id)}" ${b.id === p.away.backup ? "selected" : ""}>${esc(b.name)}${b.may_approve ? "" : " (can't approve)"}</option>`).join("")}</select>
        until <input type="date" id="tnAwayUntil" value="${p.away.until ? esc(String(p.away.until).slice(0, 10)) : ""}"></div>
      ${p.away.active ? `<div class="hint">Away ${untilText(p.away)}.</div>` : ""}
      ${p.away.backup && p.away.backup_ok === false ? `<div class="warn">Your backup can't approve reviews, so reviews are not passed on to them.</div>` : ""}
      <div class="hint">Away: new reviews for you also go to your backup (when they may approve them); you get a digest when you are back.</div>
      <div class="warn" id="tnMsg"></div></div>`;
  }
  async function save(body){
    body.tz_offset = -new Date().getTimezoneOffset();
    try { const r = await post("prefs", body); if (CARD) drawCard(CARD, r) }
    catch(e) { const m = CARD && CARD.querySelector("#tnMsg"); if (m) m.textContent = e.message; else say(e.message) }
  }
  document.addEventListener("change", e => {
    if (!CARD || !CARD.contains(e.target)) return;
    const t = e.target;
    if (t.dataset.rule) return save({rules: {[t.dataset.rule]: t.checked}});
    if (t.dataset.follow) return save({follow: {[t.dataset.follow]: t.checked}});
    if (t.dataset.on) return save({[t.dataset.on]: {on: t.checked}});
    if (t.dataset.k) return save({[t.dataset.k]: {timing: t.value}});
    if (t.id === "tnDaily" && /^\d\d:\d\d$/.test(t.value)) return save({daily_at: t.value});
    if (t.id === "tnAway" || t.id === "tnBackup" || t.id === "tnAwayUntil"){
      const on = CARD.querySelector("#tnAway").checked, b = CARD.querySelector("#tnBackup").value, u = CARD.querySelector("#tnAwayUntil").value;
      return save({away: {on, backup: b ? +b : null, until: u ? new Date(u + "T23:59:59").toISOString() : null}});
    }
  });
  document.addEventListener("click", async e => {
    if (!CARD || !CARD.contains(e.target)) return;
    const t = e.target;
    if (t.dataset.focus !== undefined){ const m = +t.dataset.focus; return save({focus: {on: true, until: m ? new Date(Date.now() + m * 60000).toISOString() : null}}) }
    if (t.closest("[data-focusoff]")) return save({focus: {on: false, until: null}});
    if (t.closest("[data-hookdel]")) return save({webhook: {url: null, on: false}});
    if (t.closest("[data-hookset]")){ const u = CARD.querySelector("#tnHook").value.trim(); if (!u) return; return save({webhook: {url: u, on: true}}) }
    const ts = t.closest("[data-test]");
    if (ts){ try { const r = await post("test", {channel: ts.dataset.test}); say(r.ok ? "Test sent" : "Not sent: " + (r.error || "")) } catch(err) { say(err.message) } }
  });

  // ---------- marketingstudio://open?coll=..&id=.. (the Mac app passes it as #team-open=<query>) ----------
  function openFromHash(){
    const m = /[#&]team-open=([^&]+)/.exec(location.hash);
    if (!m) return;
    history.replaceState(null, "", location.pathname + location.search);
    const q = new URLSearchParams(decodeURIComponent(m[1]));
    const n = {coll: q.get("coll") || "", item_id: q.get("id") || "", area: q.get("area") || "", kind: ""};
    let tries = 0;
    const go = () => { if (TM.st.joined || tries++ > 20) jump(n).catch(err => say(err.message)); else setTimeout(go, 500) };
    setTimeout(go, 300);
  }
  openFromHash();
  window.addEventListener("hashchange", openFromHash);

  window.TeamNotify = {toggle, card, countUnread, reload: () => load(false)};
})();
