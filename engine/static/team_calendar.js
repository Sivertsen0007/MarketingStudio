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
.tc-bar{display:flex;gap:8px;align-items:center;flex-wrap:wrap}
.tc-tabs button{border:0;background:none;padding:6px 10px;border-bottom:2px solid transparent;border-radius:0;color:var(--muted)}
.tc-tabs button.on{color:var(--accent);border-bottom-color:var(--accent)}
.tc-scroll{overflow:auto;flex:1;min-height:0;padding:12px 16px}
.tc-legend{display:flex;gap:10px;flex-wrap:wrap;font-size:12px;color:var(--text2);margin:0 0 10px}
.tc-dot{display:inline-block;width:10px;height:10px;border-radius:50%;vertical-align:-1px;margin-right:4px}
.tc-week{display:grid;grid-template-columns:repeat(7,minmax(0,1fr));gap:6px}
.tc-month{display:grid;grid-template-columns:repeat(7,minmax(0,1fr));gap:4px}
.tc-day{border:1px solid var(--line);border-radius:6px;background:var(--panel);min-height:90px;padding:4px;min-width:0}
.tc-week .tc-day{min-height:260px}
.tc-day.today{border-color:var(--accent)} .tc-day.out{opacity:.55}
.tc-day h4{margin:0 0 4px;font-size:12px;font-weight:500;color:var(--muted)} .tc-day h4 .n{float:right}
.tc-it{display:block;border-left:4px solid #888;background:var(--hover);border-radius:3px;padding:3px 5px;margin-bottom:3px;font-size:12px;line-height:1.3;overflow:hidden;cursor:default}
.tc-it .t{font-weight:500} .tc-it .s{color:var(--muted);font-size:11px}
.tc-it.st-review{outline:1px dashed var(--warn)} .tc-it.st-planned{opacity:.85}
.tc-it.st-published,.tc-it.st-sent{opacity:.6}
.tc-card{border:1px solid var(--line);border-radius:8px;background:var(--panel);padding:10px 12px;margin-bottom:12px}
.tc-card h3{margin:0 0 4px;font-size:14px;font-weight:500;display:flex;gap:8px;align-items:center;flex-wrap:wrap}
.tc-badge{font-size:11px;padding:1px 7px;border-radius:9px;background:var(--line2);color:var(--text2);font-weight:400}
.tc-badge.team{background:var(--accent-bg);color:var(--accent)}
.tc-help{font-size:12px;color:var(--muted);margin:0 0 8px;line-height:1.45}
.tc-rules{width:100%;border-collapse:collapse;font-size:12.5px} .tc-rules td,.tc-rules th{padding:3px 4px;border-bottom:1px solid var(--line2);text-align:left;vertical-align:top}
.tc-rules input[type=text]{width:100%;min-width:0;box-sizing:border-box} .tc-rules td.ar{white-space:nowrap}
.tc-row{display:flex;gap:8px;align-items:center;margin:4px 0;flex-wrap:wrap} .tc-row label{min-width:190px;font-size:13px;color:var(--text2)}
.tc-row input[type=number]{width:80px} .tc-row input[type=text],.tc-row textarea{flex:1;min-width:200px}
.tc-acts{display:flex;gap:8px;margin-top:8px;flex-wrap:wrap;align-items:center}
.tc-lib{width:100%;border-collapse:collapse;font-size:13px} .tc-lib td{padding:5px 6px;border-bottom:1px solid var(--line2);vertical-align:top}
.tc-lib pre{margin:0;white-space:pre-wrap;word-break:break-word;font:12px/1.4 ui-monospace,Menlo,monospace;max-height:120px;overflow:auto}
#tcPick{position:fixed;z-index:60;display:none;font-size:11px;padding:2px 8px;border-radius:10px;border:1px solid var(--line);background:var(--panel);color:var(--accent);box-shadow:0 1px 3px #0003}
#tcPickDlg{border:1px solid var(--line);border-radius:8px;padding:0;width:min(640px,calc(100% - 32px));background:var(--panel);color:var(--text)}
#tcPickDlg::backdrop{background:#0004}
#tcPickDlg .h{display:flex;gap:8px;align-items:center;padding:10px 14px;border-bottom:1px solid var(--line)} #tcPickDlg .b{padding:8px 14px;max-height:60vh;overflow:auto}
#tcPickDlg .it{border:1px solid var(--line2);border-radius:6px;padding:6px 8px;margin-bottom:6px;cursor:pointer} #tcPickDlg .it:hover{border-color:var(--accent)}
@media (max-width:900px){.tc-week{grid-template-columns:1fr}.tc-week .tc-day{min-height:0}.tc-row label{min-width:0}}
`;
if (!document.getElementById("tc-css")){ const st = document.createElement("style"); st.id = "tc-css"; st.textContent = CSS; document.head.appendChild(st) }

const T = {el:null, tab: lget("tab") || "cal", view: lget("view") || "week", anchor: new Date(), data:null, set:null, lib:null, libKind:""};

function render(el){
  T.el = el;
  el.innerHTML = `<div class="toolbar tc-bar">
      <span class="tc-tabs"><button data-tab="cal">Calendar</button><button data-tab="lib">Library</button><button data-tab="set">Team settings</button></span>
      <span id="tcNav" class="tc-bar"><span style="width:10px"></span><button data-v="week">Week</button><button data-v="month">Month</button>
        <button data-go="-1" title="Back">&lsaquo;</button><button data-go="0">Today</button><button data-go="1" title="Forward">&rsaquo;</button><b id="tcTitle"></b></span>
      <span style="flex:1"></span><button id="tcRefresh">Refresh</button></div>
    <div class="tc-scroll" id="tcBody"></div>`;
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
const body = () => T.el.querySelector("#tcBody");

// ---------- calendar ----------
function range(){
  if (T.view === "week"){ const s = monday(T.anchor); return [s, addDays(s, 7)] }
  const first = new Date(T.anchor.getFullYear(), T.anchor.getMonth(), 1), s = monday(first);
  return [s, addDays(s, 42)];
}
async function loadCal(){
  const [s, e] = range();
  T.el.querySelectorAll("[data-v]").forEach(b => b.classList.toggle("on", b.dataset.v === T.view));
  T.el.querySelector("#tcTitle").textContent = T.view === "week" ? `${s.toLocaleDateString(undefined, {day:"numeric", month:"short"})} to ${addDays(e, -1).toLocaleDateString(undefined, {day:"numeric", month:"short", year:"numeric"})}`
    : T.anchor.toLocaleDateString(undefined, {month:"long", year:"numeric"});
  body().innerHTML = `<p class="tc-help">Loading ...</p>`;
  try { T.data = await call(A("calendar", {start: s.toISOString(), end: e.toISOString()})) } catch(err) { body().innerHTML = `<div class="prob">${esc(err.message)}</div>`; return }
  drawCal(s, e);
}
function item(x){
  const o = x.owner || {};
  const tip = `${CH[x.channel] || x.channel} · ${x.status} · ${o.name}${x.audience_name ? " · to " + x.audience_name : ""}${x.link ? " · " + x.link : ""}`;
  return `<span class="tc-it st-${esc(x.status)}" style="border-left-color:${colour(o.colour)}" title="${esc(tip)}">
    <span class="s">${esc(hm(x.when))} ${esc(CH[x.channel] || x.channel)}</span> <span class="t">${esc(x.title)}</span>
    <span class="s">· ${esc(x.status)} · ${esc(o.name)}</span></span>`;
}
function drawCal(s, e){
  const D = T.data, byDay = {};
  for (const x of D.entries) (byDay[ymd(new Date(x.when))] = byDay[ymd(new Date(x.when))] || []).push(x);
  const lim = D.limits || {}, today = ymd(new Date()), month = T.anchor.getMonth();
  let h = `<div class="tc-legend">${(D.people || []).map(p => `<span><i class="tc-dot" style="background:${colour(p.colour)}"></i>${esc(p.name)}</span>`).join("")}
    <span>Dashed: in review. Faded: already out.</span><span>Limits: ${esc(lim.max_per_channel_day)} a day per channel, ${esc(lim.min_gap_hours)} h apart.</span></div>`;
  h += `<div class="${T.view === "week" ? "tc-week" : "tc-month"}">`;
  if (T.view === "month") h += DAYS.map(d => `<div class="tc-help" style="margin:0;text-align:center">${d}</div>`).join("");
  for (let d = new Date(s); d < e; d = addDays(d, 1)){
    const k = ymd(d), list = byDay[k] || [];
    const per = {}; list.forEach(x => per[x.channel] = (per[x.channel] || 0) + 1);
    const over = Object.entries(per).filter(([, n]) => n > (lim.max_per_channel_day || 99)).map(([c]) => CH[c] || c);
    h += `<div class="tc-day ${k === today ? "today" : ""} ${T.view === "month" && d.getMonth() !== month ? "out" : ""}">
      <h4>${T.view === "week" ? DAYS[(d.getDay() + 6) % 7] + " " : ""}${d.getDate()}${over.length ? ` <span class="n" style="color:var(--bad)" title="Over the daily limit">${esc(over.join(", "))}</span>` : list.length ? `<span class="n">${list.length}</span>` : ""}</h4>
      ${list.map(item).join("")}</div>`;
  }
  body().innerHTML = h + `</div>` + (D.entries.length ? "" : `<p class="tc-help" style="margin-top:10px">Nothing with a time in this range. SoMe posts, email campaigns and SEO publishes show here once they have one.</p>`);
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
    <table class="tc-lib">${L.items.map(x => `<tr><td style="width:120px"><span class="tc-badge">${esc(K[x.kind] || x.kind)}</span></td>
      <td>${x.label ? `<b>${esc(x.label)}</b><br>` : ""}<pre>${esc(x.text)}</pre></td><td style="width:140px" class="tc-help">${esc(x.by)}<br>${esc((x.created || "").slice(0, 10))}</td>
      <td style="width:150px"><button data-copy="${esc(x.id)}">Copy</button> ${L.can_add ? `<button class="link" data-del="${esc(x.id)}">Remove</button>` : ""}</td></tr>`).join("")
      || `<tr><td class="tc-help">The library is empty.</td></tr>`}</table>`;
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
  return `<h3>${esc(x.label)} ${t ? `<span class="tc-badge team">Set by your team</span><span class="tc-help" style="margin:0">${esc(t.updated_by)}${t.updated_at ? ", " + esc(new Date(t.updated_at).toLocaleString()) : ""}</span>` : `<span class="tc-badge">This Mac</span>`}</h3>`;
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
