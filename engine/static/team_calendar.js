// Marketing Studio - Calendar area: the shared calendar (week / month, every channel, each owner in their team colour),
// the team library and the team settings. Also, on every page: the "Library" picker for text fields in the Ads, Email,
// SEO and SoMe editors, and a heads-up with the clash warnings before SoMe / email scheduling (warnings only).
// Talks to /api/area/team_calendar/* (areas/team_calendar.py, shared_calendar.py, team_settings.py).
(() => {
const PRESS = (document.querySelector('meta[name="studio-press"]') || {}).content || "";
const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const A = (k, q) => "/api/area/team_calendar/" + k + (q ? "?" + new URLSearchParams(q).toString() : "");
const realFetch = window.fetch.bind(window);
async function call(path, body){
  const r = await realFetch(path, body ? {method:"POST", headers:{"Content-Type":"application/json", "X-Studio-Press":PRESS}, body:JSON.stringify(body)} : {});
  const j = await r.json(); if (!r.ok) throw new Error(j.error || r.statusText); return j;
}
const say = m => (typeof toast === "function" ? toast(m) : alert(m));
const colour = c => /^#[0-9a-fA-F]{6}$/.test(c || "") ? c : "#888888";
const CH = {facebook:"Facebook", instagram:"Instagram", linkedin:"LinkedIn", email:"Email", blog:"Blog"};
const pad = n => String(n).padStart(2, "0");
const ymd = d => `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
const monday = d => { const x = new Date(d.getFullYear(), d.getMonth(), d.getDate()); x.setDate(x.getDate() - ((x.getDay() + 6) % 7)); return x };
const addDays = (d, n) => { const x = new Date(d); x.setDate(x.getDate() + n); return x };
const hm = iso => { const d = new Date(iso); return `${pad(d.getHours())}:${pad(d.getMinutes())}` };
const DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];
const lget = k => { try { return localStorage.getItem("studio.cal." + k) } catch(e) { return null } };
const lset = (k, v) => { try { localStorage.setItem("studio.cal." + k, v) } catch(e) {} };

const CSS = `
.tc-root{display:flex;flex-direction:column;flex:1;min-height:0;background:var(--bg)}
.tc-head{display:flex;align-items:flex-end;gap:12px;padding:22px 28px 14px;flex-wrap:wrap}
.tc-head .ttl{flex:1;min-width:240px}
.tc-head h1{margin:0;font-size:24px;font-weight:600;letter-spacing:-.3px;line-height:1.2;color:var(--text)}
.tc-head p{margin:4px 0 0;font-size:13.5px;font-weight:500;color:var(--muted)}
.tc-root .toolbar.tc-bar{background:transparent;border:0;box-shadow:none;padding:0 28px 14px}
.tc-bar{display:flex;gap:8px;align-items:center;flex-wrap:wrap}
.tc-tabs,.tc-seg{display:inline-flex;gap:2px;padding:3px;border-radius:var(--r-md);background:var(--sunken)}
.tc-tabs button,.tc-seg button{height:32px;padding:0 14px;border:0;border-radius:var(--r-sm);background:transparent;box-shadow:none;color:var(--muted);font-size:13px;font-weight:600}
.tc-tabs button:hover,.tc-seg button:hover{background:transparent;color:var(--text)}
.tc-tabs button.on,.tc-seg button.on{background:var(--panel);color:var(--text);box-shadow:var(--sh-1)}
.tc-nav{width:34px;height:34px;padding:0;font-size:16px;line-height:1}
#tcTitle{font-size:15px;font-weight:600;color:var(--text);margin-left:6px}
.tc-scroll{overflow:auto;flex:1;min-height:0;padding:0 28px 28px}
.tc-legend{display:flex;gap:14px;flex-wrap:wrap;align-items:center;font-size:12px;font-weight:500;color:var(--muted);margin:0 0 12px}
.tc-legend span{display:inline-flex;align-items:center}
.tc-legend .who{height:26px;padding:0 10px;border-radius:var(--r-pill);background:var(--panel);box-shadow:var(--sh-1);color:var(--text2);font-weight:600}
.tc-dot{display:inline-block;width:8px;height:8px;border-radius:50%;margin-right:6px}
.tc-cal{border-radius:var(--r-lg);background:var(--panel);box-shadow:var(--sh-card);overflow:hidden}
.tc-week{display:grid;grid-template-columns:repeat(7,minmax(0,1fr))}
.tc-month{display:grid;grid-template-columns:repeat(7,minmax(0,1fr))}
.tc-dow{height:38px;display:flex;align-items:center;padding:0 12px;font-size:11.5px;font-weight:600;color:var(--muted);background:var(--head);border-bottom:1px solid var(--line2)}
.tc-day{border-right:1px solid var(--line2);border-bottom:1px solid var(--line2);min-height:110px;padding:8px 8px 6px;min-width:0;transition:background var(--t) var(--ease)}
.tc-day:nth-child(7n){border-right:0}
.tc-day:hover{background:var(--hover)}
.tc-week .tc-day{min-height:calc(100vh - 330px);border-bottom:0}
.tc-day.today h4 .d{background:var(--accent);color:var(--ink-text)}
.tc-day.out{background:var(--head)} .tc-day.out h4{color:var(--faint)}
.tc-day h4{display:flex;align-items:center;gap:6px;margin:0 0 6px;font-size:12px;font-weight:600;color:var(--text2)}
.tc-day h4 .d{display:inline-flex;align-items:center;justify-content:center;min-width:22px;height:22px;padding:0 6px;border-radius:var(--r-pill);font-variant-numeric:tabular-nums}
.tc-day h4 .n{margin-left:auto;display:inline-flex;align-items:center;height:18px;padding:0 6px;border-radius:var(--r-pill);background:var(--sunken);color:var(--muted);font-size:10.5px}
.tc-day h4 .n.over{background:var(--bad-bg);color:var(--bad-text, #c8303a)}
.tc-it{--oc:var(--faint);display:block;background:color-mix(in srgb,var(--oc) 10%,var(--panel));box-shadow:inset 3px 0 0 var(--oc);border-radius:var(--r-sm);padding:7px 8px 7px 10px;min-height:32px;box-sizing:border-box;margin-bottom:4px;font-size:12px;line-height:1.35;overflow:hidden;cursor:default;color:var(--text2);transition:background var(--t) var(--ease)}
.tc-it:hover{background:color-mix(in srgb,var(--oc) 17%,var(--panel))}
.tc-it .t{font-weight:600;color:var(--text)} .tc-it .s{color:var(--muted);font-size:11px;font-weight:500}
.tc-it.st-review{box-shadow:inset 3px 0 0 var(--oc),inset 0 0 0 1px var(--warn);background:var(--warn-bg)}
.tc-it.st-planned{opacity:.9}
.tc-it.st-published,.tc-it.st-sent{opacity:.55}
.tc-card{border-radius:var(--r-lg);background:var(--panel);box-shadow:var(--sh-card);padding:18px 20px;margin-bottom:12px}
.tc-card h3{margin:0 0 6px;font-size:16px;font-weight:600;color:var(--text);display:flex;gap:8px;align-items:center;flex-wrap:wrap}
.tc-badge{display:inline-flex;align-items:center;height:22px;font-size:11px;padding:0 8px;border-radius:var(--r-sm);background:var(--sunken);color:var(--text2);font-weight:600}
.tc-badge.team{background:var(--accent-bg);color:var(--accent)}
.tc-help{font-size:12.5px;font-weight:500;color:var(--muted);margin:0 0 10px;line-height:1.55}
.tc-help b{color:var(--text2);font-weight:600}
.tc-root .prob{padding:10px 14px;border-radius:var(--r);background:var(--bad-bg);color:var(--text);font-size:13px}
.tc-rules{width:100%;border-collapse:separate;border-spacing:0;font-size:12.5px;margin:4px 0 8px}
.tc-rules td,.tc-rules th{height:auto;padding:6px;border:0;border-bottom:1px solid var(--line2);text-align:left;vertical-align:middle;white-space:normal;max-width:none;overflow:visible}
.tc-rules th{position:static;background:var(--head);color:var(--muted);font-size:11.5px;font-weight:600;cursor:default;height:34px}
.tc-rules input[type=text]{width:100%;min-width:0;box-sizing:border-box} .tc-rules td.ar{white-space:nowrap}
.tc-rules td.ar label{display:inline-flex;gap:4px;align-items:center;margin-right:8px;font-weight:500}
.tc-row{display:flex;gap:10px;align-items:center;margin:8px 0;flex-wrap:wrap} .tc-row label{min-width:220px;font-size:13px;font-weight:500;color:var(--text2)}
.tc-row input[type=number]{width:90px} .tc-row input[type=text],.tc-row textarea{flex:1;min-width:200px}
.tc-acts{display:flex;gap:8px;margin-top:14px;flex-wrap:wrap;align-items:center}
.tc-libw{border-radius:var(--r-lg);background:var(--panel);box-shadow:var(--sh-card);overflow:hidden}
.tc-lib{width:100%;border-collapse:separate;border-spacing:0;font-size:13px}
.tc-lib td{height:auto;padding:12px 14px;border:0;border-bottom:1px solid var(--line2);vertical-align:top;white-space:normal;max-width:none;overflow:visible;color:var(--text2)}
.tc-lib tr:last-child td{border-bottom:0}
.tc-lib tr:hover td{background:var(--hover)}
.tc-lib td:first-child{padding-left:20px}
.tc-lib b{color:var(--text);font-weight:600}
.tc-lib pre{margin:2px 0 0;white-space:pre-wrap;word-break:break-word;font:12px/1.5 var(--mono);color:var(--text2);max-height:120px;overflow:auto}
.tc-lib button{height:30px;padding:0 12px;font-size:12.5px}
#tcPick{position:fixed;z-index:60;display:none;font-size:11px;font-weight:600;height:22px;line-height:22px;padding:0 9px;border-radius:var(--r-pill);border:0;background:var(--panel);color:var(--accent);box-shadow:var(--sh-pop)}
#tcPickDlg{border:0;border-radius:var(--r-xl);padding:0;width:min(640px,calc(100% - 32px));background:var(--panel);color:var(--text);box-shadow:var(--sh-dlg)}
#tcPickDlg::backdrop{background:rgba(17,19,24,.35);backdrop-filter:blur(2px)}
#tcPickDlg .h{display:flex;gap:8px;align-items:center;padding:18px 22px 12px;font-size:18px;font-weight:600}
#tcPickDlg .h select,#tcPickDlg .h input,#tcPickDlg .h button{font-size:13px}
#tcPickDlg .b{padding:4px 22px 20px;max-height:60vh;overflow:auto}
#tcPickDlg .it{border-radius:var(--r-md);padding:10px 12px;margin-bottom:8px;cursor:pointer;background:var(--panel);box-shadow:var(--sh-1);transition:box-shadow var(--t) var(--ease),background var(--t) var(--ease)}
#tcPickDlg .it:hover{background:var(--accent-bg);box-shadow:0 0 0 1.5px var(--accent)}
@media (max-width:900px){.tc-week{grid-template-columns:1fr}.tc-week .tc-day{min-height:0}.tc-row label{min-width:0}}
`;
if (!document.getElementById("tc-css")){ const st = document.createElement("style"); st.id = "tc-css"; st.textContent = CSS; document.head.appendChild(st) }

const T = {el:null, tab: lget("tab") || "cal", view: lget("view") || "week", anchor: new Date(), data:null, set:null, lib:null, libKind:""};

function render(el){
  T.el = el;
  el.innerHTML = `<div class="tc-root"><div class="tc-head"><div class="ttl"><h1>Calendar</h1><p>Every channel in one place, each owner in their team colour.</p></div>
      <button id="tcRefresh">Refresh</button></div>
    <div class="toolbar tc-bar">
      <span class="tc-tabs"><button data-tab="cal">Calendar</button><button data-tab="lib">Library</button><button data-tab="set">Team settings</button></span>
      <span id="tcNav" class="tc-bar"><span style="width:10px"></span><span class="tc-seg"><button data-v="week">Week</button><button data-v="month">Month</button></span>
        <button data-go="-1" class="tc-nav" title="Previous" aria-label="Previous">&lsaquo;</button><button data-go="0">Today</button><button data-go="1" class="tc-nav" title="Next" aria-label="Next">&rsaquo;</button><b id="tcTitle"></b></span></div>
    <div class="tc-scroll" id="tcBody"></div></div>`;
  el.querySelectorAll("[data-tab]").forEach(b => b.onclick = () => { T.tab = b.dataset.tab; lset("tab", T.tab); draw() });
  el.querySelectorAll("[data-v]").forEach(b => b.onclick = () => { T.view = b.dataset.v; lset("view", T.view); loadCal() });
  el.querySelectorAll("[data-go]").forEach(b => b.onclick = () => {
    const n = +b.dataset.go;
    if (!n) T.anchor = new Date(); else if (T.view === "week") T.anchor = addDays(T.anchor, 7 * n);
    else T.anchor = new Date(T.anchor.getFullYear(), T.anchor.getMonth() + n, 1);
    loadCal();
  });
  el.querySelector("#tcRefresh").onclick = () => T.tab === "cal" ? loadCal() : T.tab === "lib" ? loadLib() : loadSet(true);
  draw();
}
function draw(){
  if (!T.el) return;
  T.el.querySelectorAll("[data-tab]").forEach(b => b.classList.toggle("on", b.dataset.tab === T.tab));
  T.el.querySelector("#tcNav").style.display = T.tab === "cal" ? "" : "none";
  if (T.tab === "cal") loadCal(); else if (T.tab === "lib") loadLib(); else loadSet(false);
}
// After an await the user may have switched to another area (T.el is the shared area page): write into a detached
// stand-in then, instead of throwing "Cannot set properties of null".
const body = () => (T.el && T.el.querySelector("#tcBody")) || document.createElement("div");
// en-GB formatters from static/setup.js, read when used (not at load) with a plain fallback if setup.js is missing
const MON_FB = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
const F = {
  get MON(){ return (window.studioFmt && window.studioFmt.MON) || MON_FB },
  date(v){ if (window.studioFmt) return window.studioFmt.date(v); const d = v instanceof Date ? v : new Date(v); return isNaN(d) ? String(v ?? "") : `${d.getDate()} ${MON_FB[d.getMonth()]} ${d.getFullYear()}` },
  dateTime(v){ if (window.studioFmt) return window.studioFmt.dateTime(v); const d = v instanceof Date ? v : new Date(v); return isNaN(d) ? String(v ?? "") : `${F.date(d)}, ${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}` },
};

// ---------- calendar ----------
function range(){
  if (T.view === "week"){ const s = monday(T.anchor); return [s, addDays(s, 7)] }
  const first = new Date(T.anchor.getFullYear(), T.anchor.getMonth(), 1), s = monday(first);
  return [s, addDays(s, 42)];
}
async function loadCal(){
  const [s, e] = range();
  T.el.querySelectorAll("[data-v]").forEach(b => b.classList.toggle("on", b.dataset.v === T.view));
  const ttl = T.el.querySelector("#tcTitle") || document.createElement("b");
  ttl.textContent = T.view === "week" ? `${s.getDate()} ${F.MON[s.getMonth()]} to ${F.date(addDays(e, -1))}`
    : T.anchor.toLocaleDateString("en-GB", {month:"long", year:"numeric"});
  body().innerHTML = `<p class="tc-help">Loading ...</p>`;
  try { T.data = await call(A("calendar", {start: s.toISOString(), end: e.toISOString()})) } catch(err) { body().innerHTML = `<div class="prob">${esc(err.message)}</div>`; return }
  drawCal(s, e);
}
function item(x){
  const o = x.owner || {};
  const tip = `${CH[x.channel] || x.channel} · ${x.status} · ${o.name}${x.audience_name ? " · to " + x.audience_name : ""}${x.link ? " · " + x.link : ""}`;
  return `<span class="tc-it st-${esc(x.status)}" style="--oc:${colour(o.colour)}" title="${esc(tip)}">
    <span class="s">${esc(hm(x.when))} ${esc(CH[x.channel] || x.channel)}</span> <span class="t">${esc(x.title)}</span>
    <span class="s">· ${esc(x.status)} · ${esc(o.name)}</span></span>`;
}
function drawCal(s, e){
  const D = T.data, byDay = {};
  for (const x of D.entries) (byDay[ymd(new Date(x.when))] = byDay[ymd(new Date(x.when))] || []).push(x);
  const lim = D.limits || {}, today = ymd(new Date()), month = T.anchor.getMonth();
  let h = `<div class="tc-legend">${(D.people || []).map(p => `<span class="who"><i class="tc-dot" style="background:${colour(p.colour)}"></i>${esc(p.name)}</span>`).join("")}
    <span>Dashed: in review. Faded: already out.</span><span>Limits: ${esc(lim.max_per_channel_day)} a day per channel, ${esc(lim.min_gap_hours)} h apart.</span></div>`;
  h += `<div class="tc-cal"><div class="${T.view === "week" ? "tc-week" : "tc-month"}">`;
  if (T.view === "month") h += DAYS.map(d => `<div class="tc-dow">${d}</div>`).join("");
  for (let d = new Date(s); d < e; d = addDays(d, 1)){
    const k = ymd(d), list = byDay[k] || [];
    const per = {}; list.forEach(x => per[x.channel] = (per[x.channel] || 0) + 1);
    const over = Object.entries(per).filter(([, n]) => n > (lim.max_per_channel_day || 99)).map(([c]) => CH[c] || c);
    h += `<div class="tc-day ${k === today ? "today" : ""} ${T.view === "month" && d.getMonth() !== month ? "out" : ""}">
      <h4>${T.view === "week" ? DAYS[(d.getDay() + 6) % 7] + " " : ""}<span class="d">${d.getDate()}</span>${over.length ? ` <span class="n over" title="Over the daily limit">${esc(over.join(", "))}</span>` : list.length ? `<span class="n">${list.length}</span>` : ""}</h4>
      ${list.map(item).join("")}</div>`;
  }
  body().innerHTML = h + `</div></div>` + (D.entries.length ? "" : `<p class="tc-help" style="margin-top:10px">Nothing with a time in this range. SoMe posts, email campaigns and SEO publishes show here once they have one.</p>`);
}

// ---------- library ----------
async function loadLib(){
  body().innerHTML = `<p class="tc-help">Loading ...</p>`;
  try { T.lib = await call(A("library", T.libKind ? {kind:T.libKind} : null)) } catch(err) { body().innerHTML = `<div class="prob">${esc(err.message)}</div>`; return }
  const L = T.lib, K = L.kinds || {};
  body().innerHTML = `<div class="tc-card"><h3>Shared library</h3>
      <p class="tc-help">Approved headlines, calls to action, image URLs, UTM presets and email blocks. Shared with your team like drafts (press Sync). In the Ads, Email, SEO and SoMe editors, click <b>Library</b> next to a text field to insert one.</p>
      <div class="tc-row"><select id="tcLk"><option value="">All kinds</option>${Object.entries(K).map(([k, v]) => `<option value="${esc(k)}" ${k === T.libKind ? "selected" : ""}>${esc(v)}</option>`).join("")}</select></div>
      ${L.can_add ? `<div class="tc-row"><select id="tcNk">${Object.entries(K).map(([k, v]) => `<option value="${esc(k)}">${esc(v)}</option>`).join("")}</select>
        <input type="text" id="tcNl" placeholder="Label (optional)" style="max-width:220px"><textarea id="tcNt" rows="2" placeholder="The text, URL, utm_source=...&amp;utm_medium=... or HTML"></textarea>
        <button id="tcNadd" class="primary">Add</button></div>` : `<p class="tc-help">Viewers can't add to the library.</p>`}</div>
    <div class="tc-libw"><table class="tc-lib">${L.items.map(x => `<tr><td style="width:120px"><span class="tc-badge">${esc(K[x.kind] || x.kind)}</span></td>
      <td>${x.label ? `<b>${esc(x.label)}</b><br>` : ""}<pre>${esc(x.text)}</pre></td><td style="width:140px" class="tc-help">${esc(x.by)}<br>${esc(F.date(x.created))}</td>
      <td style="width:150px"><button data-copy="${esc(x.id)}">Copy</button> ${L.can_add ? `<button class="link" data-del="${esc(x.id)}">Remove</button>` : ""}</td></tr>`).join("")
      || `<tr><td class="tc-help">The library is empty.</td></tr>`}</table></div>`;
  const $ = s => body().querySelector(s);
  $("#tcLk").onchange = e => { T.libKind = e.target.value; loadLib() };
  if ($("#tcNadd")) $("#tcNadd").onclick = async () => {
    try { await call(A("library_add"), {item:{kind:$("#tcNk").value, label:$("#tcNl").value, text:$("#tcNt").value}}); say("Added to the library"); loadLib() } catch(err) { say(err.message) }
  };
  body().querySelectorAll("[data-copy]").forEach(b => b.onclick = async () => {
    const x = L.items.find(i => i.id === b.dataset.copy); try { await navigator.clipboard.writeText(x.text); say("Copied") } catch(err) { say("Copy did not work here") }
  });
  body().querySelectorAll("[data-del]").forEach(b => b.onclick = async () => {
    if (!confirm("Remove this from the library for everyone?")) return;
    try { await call(A("library_delete"), {id:b.dataset.del}); loadLib() } catch(err) { say(err.message) }
  });
}

// ---------- team settings ----------
async function loadSet(fresh){
  body().innerHTML = `<p class="tc-help">Loading ...</p>`;
  try { T.set = await call(A("settings", fresh ? {refresh:"1"} : null)) } catch(err) { body().innerHTML = `<div class="prob">${esc(err.message)}</div>`; return }
  const S = T.set;
  body().innerHTML = `<p class="tc-help">${S.joined ? (S.can_admin ? "You are an owner or admin: settings marked <b>Set by your team</b> apply to everyone, and you change them for everyone. <b>Share with the team</b> makes this Mac's value the team's."
      : "Settings marked <b>Set by your team</b> apply to everyone and are read-only here. The rest are this Mac's own.")
    : "These are this Mac's own settings. When you join a team, the values your team sets replace them."}</p>`
    + Object.values(S.keys).map(card).join("");
  for (const k of Object.keys(S.keys)) wire(k);
}
function head(k){
  const x = T.set.keys[k], t = x.team;
  return `<h3>${esc(x.label)} ${t ? `<span class="tc-badge team">Set by your team</span><span class="tc-help" style="margin:0">${esc(t.updated_by)}${t.updated_at ? ", " + esc(F.dateTime(t.updated_at)) : ""}</span>` : `<span class="tc-badge">This Mac</span>`}</h3>`;
}
function acts(k){
  const x = T.set.keys[k], S = T.set;
  if (!x.editable) return "";
  return `<div class="tc-acts"><button class="primary" data-save="${k}">${x.team ? "Save for the team" : "Save"}</button>
    ${S.can_admin && !x.team ? `<button data-share="${k}">Share with the team</button>` : ""}
    ${S.can_admin && x.team ? `<button class="danger" data-unshare="${k}">Stop sharing (everyone uses their own)</button>` : ""}</div>`;
}
const dis = k => T.set.keys[k].editable ? "" : "disabled";
function card(x){
  const k = x.key, v = x.value || {}, d = dis(k);
  let inner = "";
  if (k === "copy_rules"){
    const areas = T.set.areas;
    inner = `<p class="tc-help">Regular expressions, not case sensitive. <b>Block</b> stops the action in Ads and SoMe and is an error in Email and SEO; <b>warn</b> is shown only. No area ticked = everywhere. Rules run in this order.</p>
      <table class="tc-rules" id="tcRules"><tr><th style="width:80px">Level</th><th>Pattern</th><th>Message</th><th>Areas</th><th></th></tr>
      ${(v.rules || []).map(r => ruleRow(r, areas, d)).join("")}</table>
      ${d ? "" : `<button class="link" id="tcAddRule">+ rule</button>`}`;
  } else if (k === "calendar_limits"){
    const L = [["max_per_channel_day", "Posts per channel per day (max)"], ["min_gap_hours", "Hours between posts on a channel"], ["same_link_days", "Same link: days apart"],
               ["same_image_days", "Same image: days apart"], ["same_topic_days", "Same topic: days apart"], ["email_audience_days", "Emails to one audience: days apart"]];
    inner = `<p class="tc-help">The calendar warns when scheduling breaks one of these. Warnings only: nothing is blocked.</p>`
      + L.map(([f, l]) => `<div class="tc-row"><label>${l}</label><input type="number" min="0" step="1" data-lim="${f}" value="${esc(v[f])}" ${d}></div>`).join("");
  } else if (k === "email_test_addresses"){
    inner = `<div class="tc-row"><label>Addresses (comma separated)</label><input type="text" id="tcMail" value="${esc((v.addresses || []).join(", "))}" ${d}></div>`;
  } else if (k === "seo_sites"){
    inner = `<p class="tc-help">Empty = Studio's built-in sites.</p>
      <div class="tc-row"><label>Live site</label><input type="text" id="tcProd" placeholder="https://www.example.com" value="${esc(v.prod)}" ${d}></div>
      <div class="tc-row"><label>Staging site</label><input type="text" id="tcStg" placeholder="https://staging.example.com" value="${esc(v.staging)}" ${d}></div>
      <div class="tc-row"><label>Sites (one per line: name | note)</label><textarea id="tcSites" rows="2" ${d}>${esc((v.sites || []).map(s => s.name + (s.note ? " | " + s.note : "")).join("\n"))}</textarea></div>`;
  } else if (k === "social"){
    inner = `<div class="tc-row"><label>Late posting (minutes, 0 = never)</label><input type="number" min="0" max="240" id="tcLate" value="${esc(v.late_minutes || 0)}" ${d}></div>`
      + ["facebook", "instagram", "linkedin"].map(c => `<div class="tc-row"><label>${CH[c]} slots</label><input type="text" data-slot="${c}" value="${esc(((v.slots || {})[c] || []).join(", "))}" placeholder="Tue 09:00, Thu 18:00" ${d}></div>`).join("")
      + `<div class="tc-row"><label>UTMs on links</label>${["facebook", "linkedin"].map(c => `<label style="min-width:0"><input type="checkbox" data-utm="${c}" ${(v.utm || {})[c] ? "checked" : ""} ${d}> ${CH[c]}</label>`).join(" ")}</div>
      <div class="tc-row"><label>utm_campaign when a post has no name</label><input type="text" id="tcCamp" value="${esc(v.utm_campaign || "")}" ${d}></div>`;
  }
  return `<div class="tc-card" data-card="${k}">${head(k)}${inner}${acts(k)}</div>`;
}
function ruleRow(r, areas, d){
  return `<tr data-rule><td><select data-f="level" ${d}>${T.set.levels.map(l => `<option ${l === r.level ? "selected" : ""}>${l}</option>`).join("")}</select></td>
    <td><input type="text" data-f="pattern" value="${esc(r.pattern)}" ${d}><input type="hidden" data-f="id" value="${esc(r.id || "")}"><input type="hidden" data-f="extra" value="${esc(JSON.stringify({early:r.early, raw:r.raw, quote:r.quote}))}"></td>
    <td><input type="text" data-f="message" value="${esc(r.message)}" ${d}></td>
    <td class="ar">${areas.map(a => `<label><input type="checkbox" data-area="${a}" ${(r.areas || []).includes(a) ? "checked" : ""} ${d}> ${a}</label>`).join(" ")}</td>
    <td>${d ? "" : `<button class="link" data-rm>Remove</button>`}</td></tr>`;
}
function collect(k){
  const c = body().querySelector(`[data-card="${k}"]`), q = s => c.querySelector(s);
  if (k === "copy_rules") return {rules: [...c.querySelectorAll("[data-rule]")].map(tr => ({...JSON.parse(tr.querySelector('[data-f="extra"]').value || "{}"), id: tr.querySelector('[data-f="id"]').value,
    level: tr.querySelector('[data-f="level"]').value, pattern: tr.querySelector('[data-f="pattern"]').value, message: tr.querySelector('[data-f="message"]').value,
    areas: [...tr.querySelectorAll("[data-area]")].filter(x => x.checked).map(x => x.dataset.area)})).filter(r => r.pattern.trim())};
  if (k === "calendar_limits") return Object.fromEntries([...c.querySelectorAll("[data-lim]")].map(x => [x.dataset.lim, x.value === "" ? null : +x.value]));
  if (k === "email_test_addresses") return {addresses: q("#tcMail").value.split(/[\s,;]+/).filter(Boolean)};
  if (k === "seo_sites") return {prod: q("#tcProd").value, staging: q("#tcStg").value,
    sites: q("#tcSites").value.split("\n").map(l => l.trim()).filter(Boolean).map(l => { const [name, note] = l.split("|").map(s => s.trim()); return {name, note: note || "", on: true} })};
  return {late_minutes: +q("#tcLate").value || 0, slots: Object.fromEntries([...c.querySelectorAll("[data-slot]")].map(x => [x.dataset.slot, x.value])),
    utm: Object.fromEntries([...c.querySelectorAll("[data-utm]")].map(x => [x.dataset.utm, x.checked])), utm_campaign: q("#tcCamp").value};
}
function wire(k){
  const c = body().querySelector(`[data-card="${k}"]`); if (!c) return;
  const save = async (scope, value) => {
    try { T.set = await call(A("settings"), {key:k, value, scope}); say(scope === "team" ? "Saved for the team" : "Saved"); loadSet(false) } catch(err) { say(err.message) }
  };
  const b = s => c.querySelector(s);
  if (b("[data-save]")) b("[data-save]").onclick = () => save(T.set.keys[k].team ? "team" : "local", collect(k));
  if (b("[data-share]")) b("[data-share]").onclick = () => { if (confirm("Share this with the whole team? It then replaces everyone's own value.")) save("team", collect(k)) };
  if (b("[data-unshare]")) b("[data-unshare]").onclick = () => { if (confirm("Stop sharing? Everyone goes back to their own value.")) save("team", null) };
  if (k === "copy_rules"){
    const wireRm = () => c.querySelectorAll("[data-rm]").forEach(x => x.onclick = () => x.closest("tr").remove());
    wireRm();
    if (b("#tcAddRule")) b("#tcAddRule").onclick = () => { b("#tcRules").insertAdjacentHTML("beforeend", ruleRow({level:"warn", pattern:"", message:"", areas:[]}, T.set.areas, "")); wireRm() };
  }
}

window.AREAS = window.AREAS || {};
window.AREAS.team_calendar = {label: "Calendar", render};

// ---------- the Library picker in the editors ----------
const pick = document.createElement("button");
pick.id = "tcPick"; pick.type = "button"; pick.textContent = "Library"; pick.title = "Insert from the shared library";
document.body.appendChild(pick);
document.body.insertAdjacentHTML("beforeend", `<dialog id="tcPickDlg" aria-label="Insert from the library"></dialog>`);
const pdlg = document.getElementById("tcPickDlg");
let field = null, hideT = null;
const EDITORS = "#editBody, #areaPage, #dlg";
function eligible(el){
  if (!el || !(el.tagName === "TEXTAREA" || (el.tagName === "INPUT" && /^(text|url|search|)$/i.test(el.type || "")))) return false;
  if (el.disabled || el.readOnly || el.closest("#tcPickDlg") || el.closest('[data-card], .tc-card, .tc-bar')) return false;
  return !!el.closest(EDITORS);
}
document.addEventListener("focusin", e => {
  if (!eligible(e.target)) return;
  field = e.target; clearTimeout(hideT);
  const r = field.getBoundingClientRect();
  pick.style.top = Math.max(4, r.top - 18) + "px"; pick.style.left = Math.max(4, Math.min(window.innerWidth - 70, r.right - 64)) + "px";
  pick.style.display = "block";
});
document.addEventListener("focusout", e => { if (e.target === field) hideT = setTimeout(() => { pick.style.display = "none" }, 200) });
window.addEventListener("scroll", () => { pick.style.display = "none" }, true);
pick.addEventListener("mousedown", e => e.preventDefault());  // keep the caret in the field
pick.onclick = () => openPicker("");
async function openPicker(kind){
  const target = field; if (!target) return;
  let L; try { L = await call(A("library", kind ? {kind} : null)) } catch(err) { return say(err.message) }
  const K = L.kinds || {};
  pdlg.innerHTML = `<div class="h"><b>Insert from the library</b><select id="tcPk"><option value="">All kinds</option>${Object.entries(K).map(([k, v]) => `<option value="${esc(k)}" ${k === kind ? "selected" : ""}>${esc(v)}</option>`).join("")}</select>
      <input type="search" id="tcPq" placeholder="Search" style="flex:1"><button id="tcPc">Close</button></div>
    <div class="b">${L.items.map(x => `<div class="it" data-id="${esc(x.id)}"><span class="tc-badge">${esc(K[x.kind] || x.kind)}</span> ${x.label ? `<b>${esc(x.label)}</b>` : ""}
      <div style="font-size:12.5px;white-space:pre-wrap;word-break:break-word;max-height:80px;overflow:hidden">${esc(x.text)}</div><div class="tc-help" style="margin:2px 0 0">${esc(x.by)}</div></div>`).join("")
      || `<p class="tc-help">Nothing in the library yet. Add items in Calendar &gt; Library.</p>`}</div>`;
  pdlg.querySelector("#tcPc").onclick = () => pdlg.close();
  pdlg.querySelector("#tcPk").onchange = e => { pdlg.close(); field = target; openPicker(e.target.value) };
  pdlg.querySelector("#tcPq").oninput = e => { const n = e.target.value.toLowerCase(); pdlg.querySelectorAll(".it").forEach(d => d.style.display = d.textContent.toLowerCase().includes(n) ? "" : "none") };
  pdlg.querySelectorAll(".it").forEach(d => d.onclick = () => { const x = L.items.find(i => i.id === d.dataset.id); pdlg.close(); insert(target, x) });
  if (!pdlg.open) pdlg.showModal();
}
function insert(el, x){
  let text = x.text;
  if (x.kind === "utm" && el.tagName === "INPUT" && /^https?:\/\//.test(el.value.trim()) && !/[?&]utm_/.test(el.value)){
    el.value = el.value.trim() + (el.value.includes("?") ? "&" : "?") + text.replace(/^[?&]/, "");
  } else {
    const s = el.selectionStart ?? el.value.length, e = el.selectionEnd ?? el.value.length;
    el.value = el.value.slice(0, s) + text + el.value.slice(e);
    try { el.setSelectionRange(s + text.length, s + text.length) } catch(err) {}
  }
  el.focus();
  el.dispatchEvent(new Event("input", {bubbles:true})); el.dispatchEvent(new Event("change", {bubbles:true}));
}

// ---------- clash heads-up before scheduling (warnings only) ----------
const SCHED = /\/api\/area\/(social|email)\/(schedule|send_now)(\?|$)/;
window.fetch = async function(input, init){
  try {
    const url = typeof input === "string" ? input : (input && input.url) || "";
    const m = SCHED.exec(url);
    if (m && init && (init.method || "").toUpperCase() === "POST" && typeof init.body === "string"){
      const b = JSON.parse(init.body || "{}");
      const q = m[1] === "social" ? {kind:"social", id:b.id, channels:(b.channels || []).join(",")}
        : m[2] === "send_now" ? {kind:"email", id:b.id, now:"1"} : {kind:"email", id:b.id, when:b.when || "", tz:b.tz || ""};
      let warns = [];
      try { warns = (await call(A("clashes", q))).clashes || [] } catch(err) {}
      if (warns.length && !confirm("Heads-up from the shared calendar (warnings only):\n\n" + warns.map(w => "- " + w.message).join("\n") + "\n\nGo ahead anyway?"))
        return new Response(JSON.stringify({ok:false, message:"Not done: you stopped to look at the shared calendar first.", clashes:warns, problems:[]}),
                            {status:200, headers:{"Content-Type":"application/json"}});
    }
  } catch(err) {}
  return realFetch(input, init);
};
})();
