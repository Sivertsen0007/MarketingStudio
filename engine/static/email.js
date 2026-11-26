// Email area: campaigns from your email platform (Email API or MCP). Grid, editor with live preview, checks, and the user's own buttons
// (Send test, Schedule, Send now, Cancel schedule). Claude never presses these; there is no MCP tool for them.
(() => {
const A = (k, q) => "/api/area/email/" + k + (q ? "?" + qs(q) : "");
const PROG = "/api/progress?key=email";
const USER = "user-pressed";
const E = {state:null, rows:[], counts:{}, filter: localGet("email.filter") || "all", search:"", view: localGet("email.view") || "Data",
           sort:null, cur:null, aud:null, device: localGet("email.device") || "desktop", el:null, prevT:null, prevN:0};

if (!document.getElementById("em-css")){
  const st = document.createElement("style"); st.id = "em-css";
  st.textContent = `
.em-root{display:flex;flex-direction:column;flex:1;min-height:0;background:var(--bg)}
.em-head{display:flex;align-items:flex-end;gap:12px;padding:22px 28px 14px;flex-wrap:wrap}
.em-head .ttl{flex:1;min-width:240px}
.em-head h1{margin:0;font-size:24px;font-weight:600;letter-spacing:-.3px;line-height:1.2;color:var(--text)}
.em-head p{margin:4px 0 0;font-size:13.5px;font-weight:500;color:var(--muted)}
.em-head .acts{display:flex;gap:8px;align-items:center}
.em-banner .link{min-height:32px;padding:0 6px;display:inline-flex;align-items:center}
.em-banner{display:flex;gap:10px;align-items:center;margin:0 28px 14px;padding:10px 14px;border-radius:var(--r-md);background:var(--warn-bg);color:var(--text2);font-size:13px;font-weight:500;flex-wrap:wrap}
.em-banner b{color:var(--warn);font-weight:600}
.em-banner.ok{background:var(--ok-bg)} .em-banner.ok b{color:var(--ok)}
.em-banner.info{background:var(--accent-bg)} .em-banner.info b{color:var(--accent)}
.em-banner.load{background:transparent;color:var(--muted);padding:22px 0 0}
.em-note{color:var(--muted);font-size:12px}
.em-need,.em-ro{margin:0 28px 12px;padding:9px 14px;border-radius:var(--r-md);font-size:12.5px;font-weight:500}
.em-need{background:var(--accent-bg);color:var(--text2)}
.em-ro{background:var(--panel);box-shadow:var(--sh-1);color:var(--muted)}
.em-spacer{flex:1}
.em-root .toolbar.em-bar{background:transparent;border:0;box-shadow:none;padding:0 28px 12px;gap:8px}
.em-search{width:240px}
.pill.em-draft i{background:var(--faint)}.pill.em-scheduled i{background:var(--accent)}.pill.em-sending i{background:var(--c4)}
.pill.em-sent i{background:var(--ok)}.pill.em-failed i{background:var(--bad)}.pill.em-failed{color:var(--bad)}.pill.em-cancelled{color:var(--muted)}
.em-card{margin:0 28px 24px;border-radius:var(--r-lg);background:var(--panel);box-shadow:var(--sh-card);overflow:auto;flex:1;min-height:0}
.em-card.grid-wrap{flex:0 1 auto}
.em-grid{width:100%}
.em-grid th{height:40px;background:var(--head);color:var(--muted);font-size:11.5px;font-weight:600;border-right:0;border-bottom:1px solid var(--line2)}
.em-grid td{height:44px;border-right:0;color:var(--text2)}
.em-grid th:first-child,.em-grid td:first-child{padding-left:20px} .em-grid th:last-child,.em-grid td:last-child{padding-right:20px}
.em-grid td:first-child{color:var(--text);font-weight:600}
.em-grid tbody tr{cursor:pointer;transition:background var(--t) var(--ease)}
.em-grid tbody tr:last-child td{border-bottom:0}
.em-grid .chip{font-size:11px;font-weight:600;padding:2px 7px;border-radius:var(--r-sm);background:var(--sunken);color:var(--muted);margin-left:4px}
.em-grid .chip.draft{background:var(--draft-bg);color:var(--draft)}
.em-ed{flex:1;min-height:0;display:grid;grid-template-columns:minmax(380px,44%) 1fr;gap:12px;padding:0 28px 20px}
.em-left,.em-right{display:flex;flex-direction:column;min-height:0;background:var(--panel);border-radius:var(--r-lg);box-shadow:var(--sh-card);overflow:hidden}
.em-fields{padding:18px 20px 6px;overflow:auto;flex:none;max-height:52%}
.em-fields .row{grid-template-columns:120px 1fr;margin-bottom:10px}
.em-fields .row>label{font-size:12.5px;font-weight:600;color:var(--muted)}
.em-fields input,.em-fields select{width:100%}
.em-fields input[readonly]{background:var(--head);color:var(--text2);box-shadow:none}
.em-fields .chg input,.em-fields .chg select{background:var(--draft-bg);border-color:var(--draft)}
.em-cnt{font-size:11px;font-weight:600;color:var(--faint);margin-left:6px}.em-cnt.over{color:var(--bad)}
.em-code{flex:1;min-height:0;display:flex;flex-direction:column;padding:8px 20px 18px;border-top:1px solid var(--line2)}
.em-code .h{display:flex;justify-content:space-between;align-items:center;font-size:11.5px;font-weight:600;color:var(--muted);margin:6px 0 8px}
.em-code textarea{flex:1;min-height:160px;resize:none;font:12px/1.6 var(--mono);padding:12px 14px;border:1px solid var(--line);border-radius:var(--r);background:var(--head);color:var(--text2);white-space:pre;overflow:auto;tab-size:2;box-shadow:none}
.em-code textarea:focus{background:var(--panel)}
.em-code.chg textarea{border-color:var(--draft);background:var(--draft-bg)}
.em-right{background:var(--panel)}
.em-pbar{display:flex;gap:8px;align-items:center;padding:12px 16px;border-bottom:1px solid var(--line2);font-size:12px}
.em-pbar .src{color:var(--muted);font-weight:500;margin-left:4px}
.em-seg{display:inline-flex;gap:2px;padding:3px;border-radius:var(--r-md);background:var(--sunken)}
.em-root .fpills button{min-height:32px}
.em-root button.danger,.em-root .menu button.danger{color:var(--bad-text, #c8303a)}
.em-seg button{height:32px;padding:0 14px;border-radius:var(--r-sm);background:transparent;box-shadow:none;color:var(--muted);font-size:12.5px}
.em-seg button:hover{background:transparent;color:var(--text)}
.em-seg button.on{background:var(--panel);color:var(--text);box-shadow:var(--sh-1)}
.em-pbar>button{height:32px;padding:0 12px;font-size:12.5px}
.em-frame{flex:1;min-height:0;overflow:auto;display:flex;justify-content:center;padding:20px;background:var(--sunken)}
.em-frame iframe{border:0;background:var(--panel);height:100%;min-height:400px;width:100%;max-width:760px;box-shadow:var(--sh-card);border-radius:var(--r-md);transition:width .25s var(--ease)}
.em-frame.mobile iframe{width:375px;border-radius:var(--r-xl);box-shadow:0 0 0 8px var(--ink),var(--sh-pop)}
.em-checks{flex:none;max-height:34%;overflow:auto;padding:12px 16px;border-top:1px solid var(--line2);font-size:13px}
.em-checks .hint{margin:0;font-size:12.5px}
.em-checks .p{padding:7px 10px;border-radius:var(--r-sm);margin:4px 0;color:var(--text2);word-break:break-word;font-size:12.5px;background:var(--head)}
.em-checks .p.error{background:var(--bad-bg)} .em-checks .p.error b{color:var(--bad)}
.em-checks .p.warn{background:var(--warn-bg)} .em-checks .p.warn b{color:var(--warn)}
.em-checks .okline{color:var(--ok);font-weight:600;padding:2px 0 4px}
.em-root .toolbar.em-etb{background:transparent;border:0;box-shadow:none;padding:20px 28px 14px;gap:8px}
.em-etb .t{font-size:18px;font-weight:600;color:var(--text);max-width:28vw;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;margin-left:4px}
.em-etb .sep{width:1px;align-self:stretch;background:var(--line);margin:4px 2px}
.em-etb .hint{margin:0;font-size:12px}
.em-back{display:inline-flex;align-items:center;gap:6px}
.em-when{font-size:13px;color:var(--muted);margin-top:4px}
.em-root .prob{margin:16px 28px;padding:10px 14px;border-radius:var(--r);background:var(--bad-bg);color:var(--text);font-size:13px}
@media (max-width:900px){.em-ed{grid-template-columns:1fr}.em-right{min-height:480px}}
`;
  document.head.appendChild(st);
}

// ---- formatting ----
const MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December"];
// The send zone: the shared posting zone (Setup > Email > Send timezone, also SoMe's posting zone), default Europe/London
const sendTz = () => (E.state && E.state.send_tz) || "Europe/London";
const tzName = () => sendTz().split("/").pop().replace(/_/g, " ");  // "Europe/London" -> "London"
function zoneParts(d){
  let f;
  try { f = new Intl.DateTimeFormat("en-GB", {timeZone:sendTz(), year:"numeric", month:"2-digit", day:"2-digit", hour:"2-digit", minute:"2-digit", hourCycle:"h23"}) }
  catch(e) { f = new Intl.DateTimeFormat("en-GB", {timeZone:"Europe/London", year:"numeric", month:"2-digit", day:"2-digit", hour:"2-digit", minute:"2-digit", hourCycle:"h23"}) }
  const o = {}; for (const x of f.formatToParts(d)) o[x.type] = x.value;
  return {Y:+o.year, M:+o.month, D:+o.day, h:+o.hour, m:+o.minute};
}
const pad = n => String(n).padStart(2, "0");
function fmtDate(iso, long){
  if (!iso) return "";
  const p = zoneParts(new Date(iso));
  return `${p.D} ${long ? MONTHS[p.M - 1] : MONTHS[p.M - 1].slice(0, 3)} ${p.Y}, ${pad(p.h)}:${pad(p.m)}`;
}
function fmtLocal(d){
  return `${d.getDate()} ${MONTHS[d.getMonth()]} ${d.getFullYear()}, ${pad(d.getHours())}:${pad(d.getMinutes())}`;
}
// "YYYY-MM-DDTHH:MM" as wall time in the send zone -> Date
function zoneToDate(v){
  const [ds, ts] = String(v).split("T"); if (!ds || !ts) return null;
  const [Y, M, D] = ds.split("-").map(Number), [h, m] = ts.split(":").map(Number);
  const want = Date.UTC(Y, M - 1, D, h, m); let g = want;
  for (let i = 0; i < 3; i++){ const p = zoneParts(new Date(g)); g += want - Date.UTC(p.Y, p.M - 1, p.D, p.h, p.m) }
  return new Date(g);
}
const num = v => v == null || v === "" ? "" : Number(v).toLocaleString("en-GB");
const pct = v => v == null ? "" : Number(v).toFixed(1) + "%";
const statusPill = s => s ? `<span class="pill em-${esc(s)}"><i></i>${esc(s[0].toUpperCase() + s.slice(1))}</span>` : "";
const probsHTML = probs => !probs ? "" : !probs.filter(p => p.level !== "info").length ? `<div class="okline">All checks passed.</div>` + probs.map(p => `<div class="p info">${esc(p.message)}</div>`).join("")
  : probs.map(p => `<div class="p ${esc(p.level)}">${p.level === "info" ? "" : `<b>${p.level === "error" ? "Error" : "Warning"}:</b> `}${esc(p.message)}</div>`).join("");

const COLS = {
  name:{label:"Name"}, status:{label:"Status", fmt:statusPill}, audience:{label:"List or segment", fmt:(v, r) => v ? `${esc(v)} <span class="chip">${r.audience_kind}</span>` : ""},
  audience_count:{label:"Audience", num:true, fmt:num}, scheduled_for:{label:"Scheduled for", fmt:v => esc(fmtDate(v))}, sent_at:{label:"Sent at", fmt:v => esc(fmtDate(v))},
  sent:{label:"Sent", num:true, fmt:num}, opens:{label:"Opens", num:true, fmt:num}, clicks:{label:"Clicks", num:true, fmt:num}, bounces:{label:"Bounces", num:true, fmt:num},
  unsubscribes:{label:"Unsubscribes", num:true, fmt:num}, open_rate:{label:"Open rate", num:true, fmt:pct}, click_rate:{label:"Click rate", num:true, fmt:pct},
  subject:{label:"Subject"}, preview_text:{label:"Preview text"}, from_name:{label:"From name"},
  draft:{label:"Studio draft", fmt:v => v ? `<span class="chip draft">draft</span>` : ""},
};
const VIEWS = {
  Data:["name", "status", "audience", "audience_count", "scheduled_for", "sent_at", "sent", "opens", "clicks", "bounces", "unsubscribes", "open_rate", "click_rate"],
  Edit:["name", "status", "draft", "subject", "audience", "audience_count", "scheduled_for", "sent_at"],
};
const FILTERS = [["all", "All"], ["draft", "Drafts"], ["scheduled", "Scheduled"], ["sending", "Sending"], ["sent", "Sent"], ["cancelled", "Cancelled"], ["failed", "Failed"]];

function banner(){
  const s = E.state || {};
  const note = s.api_note ? `<span class="em-note">${esc(s.api_note)}</span>` : "";
  if (s.source === "mcp") return `<div class="em-banner info" id="emBanner"><b>${esc(s.source_label)}</b><span>Reads through the MCP. You can create new drafts; changing HTML, tests, scheduling and sending need the Email API.</span>${note}<span class="em-spacer"></span><button class="link" data-em="connect">Connection</button></div>`;
  if (s.source === "api") return `<div class="em-banner ok" id="emBanner"><b>${esc(s.source_label)}</b><span>${esc(s.platform || "")}</span><span class="em-spacer"></span><button class="link" data-em="connect">Connection</button></div>`;
  return `<div class="em-banner" id="emBanner"><b>Demo data: no email platform connected.</b><span>Nothing here is real, and nothing is sent.</span>${note}<span class="em-spacer"></span><button class="link" data-em="connect">Connect</button></div>`;
}
const can = k => !!(E.state && E.state.can && E.state.can[k]);
const PN = () => (E.state && E.state.platform) || "your email platform";
const needAttr = k => can(k) ? "" : `disabled title="${esc((E.state || {}).needs_api || "")}"`;

// The area page element is shared by every area: after an await, only draw if Email is still the open area.
const gone = () => typeof S !== "undefined" && S.area && S.area !== "email";
// ---- campaigns list ----
async function renderList(){
  const el = E.el; E.cur = null;
  el.innerHTML = `<div class="em-root"><div class="em-banner load">Loading ...</div></div>`;
  try { E.state = await api(A("state")) } catch(e) { if (!gone()) el.innerHTML = `<div class="em-root"><div class="prob">${esc(e.message)}</div></div>`; return }
  if (gone()) return;
  el.innerHTML = `<div class="em-root"><div class="em-head"><div class="ttl"><h1>Email</h1><p>Campaigns from your email platform. Test, schedule and send are your own buttons.</p></div>
    <div class="acts"><button data-em="refresh">Refresh</button><button data-em="new" class="primary">New campaign</button></div></div>
    ${banner()}
    <div class="toolbar em-bar">
      <div class="fpills" id="emPills"></div>
      <input id="emSearch" class="em-search" placeholder="Search name, subject, list" value="${esc(E.search)}">
      <select id="emView" title="Columns">${Object.keys(VIEWS).map(v => `<option value="${v}" ${v === E.view ? "selected" : ""}>View: ${v}</option>`).join("")}</select>
    </div>
    <div class="grid-wrap em-card" id="emGrid"><div class="empty">Loading campaigns ...</div></div></div>`;
  el.onclick = onClick;
  el.oncontextmenu = onRowMenu;
  $("#emSearch").oninput = e => { E.search = e.target.value; clearTimeout(E._st); E._st = setTimeout(loadGrid, 250) };
  $("#emView").onchange = e => { E.view = e.target.value; localSet("email.view", E.view); drawGrid() };
  await loadGrid();
}
async function loadGrid(fresh){
  try {
    const r = await api(A("campaigns", {search:E.search, fresh: fresh === true ? 1 : null}));
    E.rows = r.campaigns; E.counts = r.counts;
  } catch(e) { const g = $("#emGrid"); if (g) g.innerHTML = `<div class="empty">${esc(e.message)}</div>`; return }
  drawGrid();
}
function drawGrid(){
  if (!$("#emGrid")) return;
  $("#emPills").innerHTML = FILTERS.filter(([k]) => k === "all" || E.counts[k]).map(([k, l]) =>
    `<button data-emf="${k}" class="${E.filter === k ? "on" : ""}">${l}<span class="n">${E.counts[k] || 0}</span></button>`).join("");
  let rows = E.rows.filter(r => E.filter === "all" || r.status === E.filter);
  if (E.sort){ const {k, d} = E.sort; rows = [...rows].sort((a, b) => { const x = a[k] ?? "", y = b[k] ?? ""; return (x > y ? 1 : x < y ? -1 : 0) * d }) }
  const cols = VIEWS[E.view] || VIEWS.Data;
  if (!rows.length){ $("#emGrid").innerHTML = `<div class="empty">No campaigns here.<br><button data-em="new" style="margin-top:10px">New campaign</button></div>`; return }
  $("#emGrid").innerHTML = `<table class="em-grid"><thead><tr>${cols.map(c => `<th data-ems="${c}" class="${E.sort && E.sort.k === c ? "sorted" : ""}">${COLS[c].label}${E.sort && E.sort.k === c ? (E.sort.d > 0 ? " ▴" : " ▾") : ""}</th>`).join("")}</tr></thead>
    <tbody>${rows.map(r => `<tr data-emid="${esc(r.id)}">${cols.map(c => `<td class="${COLS[c].num ? "num" : ""}" title="${COLS[c].fmt ? "" : esc(r[c])}">${COLS[c].fmt ? COLS[c].fmt(r[c], r) : esc(r[c])}</td>`).join("")}</tr>`).join("")}</tbody></table>`;
}

function onClick(e){
  const b = e.target.closest("[data-em]"), f = e.target.closest("[data-emf]"), s = e.target.closest("[data-ems]"), row = e.target.closest("[data-emid]");
  const dv = e.target.closest("[data-emdev]");
  if (f){ E.filter = f.dataset.emf; localSet("email.filter", E.filter); return drawGrid() }
  if (s){ const k = s.dataset.ems; E.sort = E.sort && E.sort.k === k ? {k, d:-E.sort.d} : {k, d:COLS[k].num ? -1 : 1}; return drawGrid() }
  if (dv){ E.device = dv.dataset.emdev; localSet("email.device", E.device); return setDevice() }
  if (row && !E.cur) return openEditor(row.dataset.emid);
  if (!b) return;
  const act = {connect:connectDialog, refresh:async () => { await dataRefresh("email", null, {reload:false}); loadGrid() }, newdraft:createFromEditor, new:newDialog, back:goBack, check:runChecks, save:() => saveDraft(false), discard:discardDraft,
               platform:saveToPlatform, file:saveHtmlDialog, test:testDialog, schedule:scheduleDialog, now:sendNowDialog, cancel:cancelDialog, prev:() => preview(true)}[b.dataset.em];
  if (act) act();
}

// ---- right-click on a campaign row: Open / Delete draft ----
(function(){
  if (document.getElementById("emMenuCss")) return;
  const st = document.createElement("style"); st.id = "emMenuCss";
  st.textContent = `.em-menu{position:fixed;z-index:60;min-width:200px;background:var(--panel);border-radius:var(--r-lg);box-shadow:var(--sh-pop);padding:6px;font-size:13px}
  .em-menu button{display:flex;align-items:center;width:100%;height:32px;text-align:left;border:0;border-radius:var(--r-sm);background:none;box-shadow:none;padding:0 10px;color:var(--text);font-weight:500}
  .em-menu button:hover:not(:disabled){background:var(--hover)}
  .em-menu button.danger{color:var(--bad)} .em-menu button:disabled{color:var(--faint);cursor:default}
  .em-menu .why{padding:2px 10px 6px;color:var(--muted);font-size:11px;font-weight:500;max-width:240px}`;
  document.head.appendChild(st);
})();
function closeRowMenu(){ const m = document.getElementById("emMenu"); if (m) m.remove(); document.removeEventListener("mousedown", outsideMenu, true) }
function outsideMenu(e){ if (!e.target.closest("#emMenu")) closeRowMenu() }
function onRowMenu(e){
  const tr = e.target.closest("tr[data-emid]"); if (!tr || E.cur) return;
  e.preventDefault(); closeRowMenu();
  const r = (E.rows || []).find(x => String(x.id) === tr.dataset.emid); if (!r) return;
  const isDraft = r.status === "draft", okDel = isDraft && can("delete");
  const why = !isDraft ? "Only drafts can be deleted." : !can("delete") ? "Deleting needs the Email API connection." : "";
  document.body.insertAdjacentHTML("beforeend", `<div class="em-menu" id="emMenu" role="menu">
    <button data-m="open" role="menuitem">Open</button>
    <button data-m="del" class="danger" role="menuitem" ${okDel ? "" : "disabled"}>Delete draft</button>
    ${why ? `<div class="why">${esc(why)}</div>` : ""}</div>`);
  const m = document.getElementById("emMenu");
  m.style.left = Math.min(e.clientX, innerWidth - m.offsetWidth - 8) + "px";
  m.style.top = Math.min(e.clientY, innerHeight - m.offsetHeight - 8) + "px";
  m.onclick = ev => { const b = ev.target.closest("[data-m]"); if (!b || b.disabled) return; closeRowMenu();
    if (b.dataset.m === "open") openEditor(r.id); else deleteDialog(r) };
  setTimeout(() => document.addEventListener("mousedown", outsideMenu, true), 0);
}
document.addEventListener("keydown", e => { if (e.key === "Escape") closeRowMenu() });
window.addEventListener("blur", closeRowMenu);
function deleteDialog(r){
  showDialog("Delete draft", `<p>Delete <b>${esc(r.name)}</b>${E.state.demo ? " from the demo data" : " on " + esc(PN())}?</p>
    <p class="hint">The draft and any Studio edits to it are removed. This can't be undone. Sent and scheduled campaigns can never be deleted from here.</p>`,
    [["Keep it", () => dlg.close()], ["Delete draft", async () => {
      try { await api(A("delete"), {id:r.id, confirm:USER}); dlg.close(); toast(`Deleted "${r.name}"`); await loadGrid(true) }
      catch(err) { toast(err.message) }
    }, "danger"]]);
}

// ---- editor ----
const FIELDS = ["name", "subject", "preview_text", "from_name"];
function audValue(c){ return c.segment_id ? "s:" + c.segment_id : c.list_id ? "l:" + c.list_id : "" }
function audOptions(sel){
  const a = E.aud || {lists:[], segments:[]};
  return `<option value="">Pick a list or segment</option>`
    + `<optgroup label="Lists">${a.lists.map(x => `<option value="l:${esc(x.id)}" ${sel === "l:" + x.id ? "selected" : ""}>${esc(x.name)} (${num(x.count)})</option>`).join("")}</optgroup>`
    + `<optgroup label="Segments">${a.segments.map(x => `<option value="s:${esc(x.id)}" ${sel === "s:" + x.id ? "selected" : ""}>${esc(x.name)} (${num(x.count)})</option>`).join("")}</optgroup>`;
}
async function openEditor(id){
  const el = E.el;
  el.innerHTML = `<div class="em-root"><div class="em-banner load">Opening ...</div></div>`;
  try {
    const [c, aud] = await Promise.all([api(A("campaign", {id})), E.aud ? Promise.resolve(E.aud) : api(A("audiences"))]);
    E.cur = c; E.aud = aud;
  } catch(e) { if (gone()) return; toast(e.message); return renderList() }
  if (gone()) return;
  if (window.TeamReviews) TeamReviews.ownerNote([`email/${id}`, "email"]); // [team reviews] "Owned by ..."
  drawEditor();
}
function drawEditor(){
  const {campaign:c, original:o, draft:d} = E.cur;
  const editable = o.status === "draft";
  const ro = editable ? "" : "readonly";
  E.el.innerHTML = `<div class="em-root">
    <div class="toolbar em-etb">
      <button data-em="back" class="em-back">&larr; Back to campaigns</button>
      <span class="t" title="${esc(c.name)}">${esc(c.name)}</span>${statusPill(o.status)}
      <span id="emDraftChip">${d ? `<span class="chip draft" title="Edits saved in Studio, not yet on ${esc(PN())} (by ${esc(d.by)})">Studio draft</span>` : ""}</span>
      <span class="em-spacer"></span>
      <button data-em="check" title="Run every check, links included">Check</button>
      ${editable ? `<span id="emAuto" class="hint" title="Edits are kept as a Studio draft on their own, about a second after you stop typing">Edits save on their own</span>
      ${d ? `<button data-em="discard" class="danger" title="Throw away the Studio draft and go back to what ${esc(PN())} holds">Discard draft</button>` : ""}
      ${can("edit") ? `<button data-em="platform" class="primary" title="Write the Studio draft to ${esc(PN())} (drafts only)" ${d ? "" : "disabled"}>Save to platform</button>`
        : `<button data-em="platform" class="primary" ${needAttr("edit")}>Save to platform</button>`}` : ""}
      ${!can("edit") && can("create") ? `<button data-em="newdraft" title="Create a NEW draft on ${esc(PN())} with this subject, list and HTML. Nothing is sent.">Create draft on platform</button>` : ""}
      <button data-em="file" title="Write the HTML to the campaign folder in AI-Hub">Save HTML to folder</button>
      <span class="sep"></span>
      ${editable ? (can("test") ? `<button data-em="test" title="Always to ${esc(E.state.test_address)}">Send test</button>` : `<button data-em="test" ${needAttr("test")}>Send test</button>`)
        + (can("schedule") ? `<button data-em="schedule">Schedule</button><button data-em="now" class="live">Send now</button>`
          : `<button data-em="schedule" ${needAttr("schedule")}>Schedule</button><button data-em="now" class="live" ${needAttr("schedule")}>Send now</button>`) : ""}
      ${o.status === "scheduled" ? `<button data-em="cancel" class="danger" ${needAttr("schedule")}>Cancel schedule</button>` : ""}
    </div>
    ${banner()}
    ${can("edit") ? "" : `<div class="em-need">${esc(E.state.needs_api)}: Save to platform, Send test, Schedule, Send now and Cancel schedule. Your edits stay as a Studio draft, and Create draft on platform makes a new draft from them.</div>`}
    ${editable ? "" : `<div class="em-ro">${o.status === "scheduled" ? `Scheduled for ${esc(fmtDate(o.scheduled_for, true))} (${esc(tzName())}). Cancel the schedule to edit it again.` : o.status === "sent" ? `Sent ${esc(fmtDate(o.sent_at, true))}. A sent email can't be changed or unsent.` : `This campaign is ${esc(o.status)}. Only drafts can be changed.`}</div>`}
    <div class="em-ed" data-team-coll="email" data-team-id="${esc(o.id)}" data-team-label="${esc(c.name)}" data-team-bar="before">
      <div class="em-left">
        <div class="em-fields">
          <div class="row" data-f="name"><label>Name</label><input id="em_name" value="${esc(c.name)}" ${ro}></div>
          <div class="row" data-f="subject"><label>Subject <span class="em-cnt" id="emSubjN"></span></label><input id="em_subject" value="${esc(c.subject)}" ${ro}></div>
          <div class="row" data-f="preview_text"><label>Preview text</label><input id="em_preview_text" value="${esc(c.preview_text)}" ${ro}></div>
          <div class="row" data-f="from_name"><label>From name</label><input id="em_from_name" value="${esc(c.from_name)}" ${ro}></div>
          <div class="row"><label>From</label><input value="${esc(E.state.from_address || c.from_address || "")}" readonly title="${E.state.from_address ? "Fixed in Setup: always " + esc(E.state.from_address) : "Set by the platform"}"></div>
          <div class="row"><label>Reply-to</label><input value="${esc(E.state.from_address || c.reply_to || "")}" readonly title="${E.state.from_address ? "Fixed in Setup: always " + esc(E.state.from_address) : "Set by the platform"}"></div>
          <div class="row" data-f="aud"><label>Send to</label><select id="em_aud" ${editable ? "" : "disabled"}>${audOptions(audValue(c))}</select></div>
          ${c.scheduled_for ? `<div class="row"><label>Scheduled for</label><span>${esc(fmtDate(c.scheduled_for, true))} (${esc(tzName())})</span></div>` : ""}
        </div>
        <div class="em-code" id="emCodeBox"><div class="h"><span>HTML</span><span id="emHtmlN"></span></div><textarea id="em_html" spellcheck="false" ${ro}>${esc(c.html)}</textarea></div>
      </div>
      <div class="em-right">
        <div class="em-pbar"><span class="em-seg"><button data-emdev="desktop">Desktop</button><button data-emdev="mobile">Mobile</button></span><button data-em="prev" title="Preview again">Refresh</button><span class="src" id="emSrc"></span></div>
        <div class="em-frame" id="emFrame"><iframe id="emIframe" sandbox="" title="Email preview"></iframe></div>
        <div class="em-checks" id="emChecks"><span class="hint">Press Check to run every check (links too). Send, Schedule and Send test run them as well.</span></div>
      </div>
    </div></div>`;
  E.el.onclick = onClick;
  for (const f of [...FIELDS, "html"]) $("#em_" + f).addEventListener("input", onEdit);
  $("#em_aud").addEventListener("change", onEdit);
  $("#em_html").addEventListener("keydown", e => {
    if (e.key === "Tab" && !e.target.readOnly){ e.preventDefault(); const t = e.target, s = t.selectionStart; t.setRangeText("  ", s, t.selectionEnd, "end"); onEdit() }
  });
  setDevice(); onEdit(true); preview(true);
}
function formValues(){
  const v = {}; for (const f of [...FIELDS, "html"]) v[f] = $("#em_" + f).value;
  const a = $("#em_aud").value; v.list_id = a.startsWith("l:") ? a.slice(2) : null; v.segment_id = a.startsWith("s:") ? a.slice(2) : null;
  return v;
}
// fields that differ from what the platform holds
function diffFromOriginal(){
  const v = formValues(), o = E.cur.original, out = {};
  for (const f of [...FIELDS, "html"]) if ((v[f] || "") !== (o[f] || "")) out[f] = v[f];
  if ((v.list_id || null) !== (o.list_id || null) || (v.segment_id || null) !== (o.segment_id || null)){ out.list_id = v.list_id; out.segment_id = v.segment_id }
  return out;
}
function isDirty(){
  if (!E.cur || E.cur.original.status !== "draft") return false;
  const v = formValues(), c = E.cur.campaign;
  return [...FIELDS, "html"].some(f => (v[f] || "") !== (c[f] || "")) || (v.list_id || null) !== (c.list_id || null) || (v.segment_id || null) !== (c.segment_id || null);
}
function onEdit(first){
  if (!E.cur) return;
  const dif = diffFromOriginal(), subj = $("#em_subject").value.trim();
  for (const f of FIELDS) $(`[data-f="${f}"]`).classList.toggle("chg", f in dif);
  $(`[data-f="aud"]`).classList.toggle("chg", "list_id" in dif);
  $("#emCodeBox").classList.toggle("chg", "html" in dif);
  const n = $("#emSubjN"); n.textContent = subj.length + "/60"; n.classList.toggle("over", subj.length > 60 || !subj.length);
  $("#emHtmlN").textContent = $("#em_html").value.length.toLocaleString("en-GB") + " characters" + ("html" in dif ? " · changed" : "");
  if (first !== true){ clearTimeout(E.prevT); E.prevT = setTimeout(() => preview(false), 500); scheduleAutoSave() }
}
function setDevice(){
  const fr = $("#emFrame"); if (!fr) return;
  fr.classList.toggle("mobile", E.device === "mobile");
  document.querySelectorAll("[data-emdev]").forEach(b => b.classList.toggle("on", b.dataset.emdev === E.device));
}
async function preview(){
  if (!E.cur) return;
  const n = ++E.prevN;
  try {
    const r = await api(A("preview"), {id:E.cur.original.id, html:$("#em_html").value});
    if (n !== E.prevN || !$("#emIframe")) return;
    $("#emIframe").srcdoc = r.html; $("#emSrc").textContent = r.source || "";
  } catch(e) { if ($("#emSrc")) $("#emSrc").textContent = "Preview failed: " + e.message }
}
async function runChecks(){
  const box = $("#emChecks"); box.innerHTML = `<div class="prog-h"><span class="spin"></span>Checking, links included ...</div>`;
  try {
    const r = await api(A("checks"), {id:E.cur.original.id, fields:{...E.cur.draft ? E.cur.draft.fields : {}, ...formValues()}});
    box.innerHTML = probsHTML(r.problems);
    return r;
  } catch(e) { box.innerHTML = `<div class="p error">${esc(e.message)}</div>` }
}
async function reloadCurrent(){
  const id = E.cur && E.cur.original.id; if (!id) return;
  try { E.cur = await api(A("campaign", {id})); drawEditor() } catch(e) { toast(e.message) }
}
// ---- autosave: keep the Studio draft in step with the editor, without redrawing it ----
function autoTxt(t){ const x = document.getElementById("emAuto"); if (x) x.textContent = t }
function scheduleAutoSave(){
  if (!E.cur || E.cur.original.status !== "draft") return;
  clearTimeout(E.autoT); autoTxt("Saving ..."); E.autoT = setTimeout(autoSave, 1000);
}
function autoSave(){
  clearTimeout(E.autoT); E.autoT = null;
  if (!E.cur || E.cur.original.status !== "draft" || !document.getElementById("em_html")) return E.autoChain || Promise.resolve(true);
  const cur = E.cur, fields = diffFromOriginal(), vals = formValues();
  E.autoChain = (E.autoChain || Promise.resolve()).then(async () => {
    try {
      await api(A("save_draft"), {id:cur.original.id, fields, replace:true});
      const has = Object.keys(fields).length > 0;
      cur.draft = has ? {...(cur.draft || {}), fields, by:"you"} : null;
      cur.campaign = {...cur.campaign, ...vals};
      if (E.cur === cur){
        autoTxt(has ? "Saved as Studio draft" : "Same as " + PN());
        const chip = document.getElementById("emDraftChip"); if (chip) chip.innerHTML = has ? `<span class="chip draft" title="Edits saved in Studio, not yet on ${esc(PN())} (by you)">Studio draft</span>` : "";
        const m = document.querySelector('[data-em="platform"]'); if (m && can("edit")) m.disabled = !has;
      }
      return true;
    } catch(e) { autoTxt("Not saved: " + e.message); toast(e.message); return false }
  });
  return E.autoChain;
}
async function saveDraft(quiet){
  if (!E.cur || E.cur.original.status !== "draft") return true;
  const fields = diffFromOriginal();
  try {
    await api(A("save_draft"), {id:E.cur.original.id, fields, replace:true});
    if (!quiet) toast(Object.keys(fields).length ? "Saved as a draft in Studio. Press Save to platform when it is ready." : "Same as " + PN() + ": no draft kept.");
    await reloadCurrent(); return true;
  } catch(e) { toast(e.message); return false }
}
async function discardDraft(){
  try { await api(A("discard_draft"), {id:E.cur.original.id}); toast("Studio draft discarded"); reloadCurrent() } catch(e) { toast(e.message) }
}
async function saveToPlatform(){
  if (E.autoT) await autoSave();
  if (isDirty() && !(await saveDraft(true))) return;
  try { const r = await api(A("save_to_platform"), {id:E.cur.original.id}); toast(r.demo ? "Saved to the demo campaign." : "Saved to " + PN() + " as a draft."); reloadCurrent() }
  catch(e) { toast(e.message) }
}
async function goBack(){
  if (E.autoT) await autoSave();
  renderList();
}

// ---- dialogs ----
async function saveHtmlDialog(){
  let f = {folders:[], root:""}; try { f = await api(A("folders")) } catch(e) {}
  const slug = s => String(s || "").toLowerCase().normalize("NFKD").replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "");
  showDialog("Save HTML to the campaign folder", `<p class="hint">Writes the HTML in the editor to ${esc(f.root)}/&lt;folder&gt;/&lt;file&gt;.html. A file with the same name is replaced.</p>
    <div class="row"><label>Folder</label><select id="emFolder"><option value="">New folder ...</option>${f.folders.map(x => `<option>${esc(x)}</option>`).join("")}</select></div>
    <div class="row" id="emNewRow"><label>New folder</label><input id="emNewFolder" placeholder="black-november-2026"></div>
    <div class="row"><label>File name</label><input id="emSlug" value="${esc(slug(E.cur.campaign.name))}"></div>`,
    [["Save", async () => {
      const folder = $("#emFolder").value || $("#emNewFolder").value;
      try { const r = await api(A("save_html"), {folder, slug:$("#emSlug").value, html:$("#em_html").value}); dlg.close(); toast((r.replaced ? "Replaced " : "Saved ") + r.path) }
      catch(e) { toast(e.message) }
    }, "primary"]]);
  $("#emFolder").onchange = e => { $("#emNewRow").style.display = e.target.value ? "none" : "" };
}
async function connectDialog(){
  let s = {}; try { s = await api(A("settings")) } catch(e) {}
  const ph = set => set ? "Saved (hidden). Paste a new one to replace it" : "";
  const any = s.token_set || s.mcp_token_set;
  showDialog("Connect your email platform", `<p class="hint">Connect through the <b>Email API</b> (full: edit, test, schedule, send) and/or an <b>MCP server</b> (read, plus new drafts). Tokens go into the macOS Keychain and are never shown again. The README describes both.</p>
    <div class="row"><label>Platform name</label><input id="emName" value="${esc(s.name && s.name !== "your email platform" ? s.name : "")}" placeholder="Shown in buttons, e.g. Acme Mail"></div>
    <div class="row"><label>Email API URL</label><input id="emApiUrl" value="${esc(s.api_url || "")}" placeholder="https://mail.example.com/api/studio/email"></div>
    <div class="row"><label>Email API token</label><input id="emTok" type="password" autocomplete="off" placeholder="${ph(s.token_set)}"></div>
    <div class="row"><label>MCP URL</label><input id="emMcpUrl" value="${esc(s.mcp_url || "")}" placeholder="https://mail.example.com/api/mcp"></div>
    <div class="row"><label>MCP token</label><input id="emMcpTok" type="password" autocomplete="off" placeholder="${ph(s.mcp_token_set)}"></div>
    <label style="display:flex;gap:8px;align-items:center;margin-bottom:8px"><input type="checkbox" id="emSame" ${s.same_token ? "checked" : ""}> Use the same token for both</label>
    <p class="hint" id="emConnMsg">${any ? `Saved: ${[s.token_set && "Email API token", s.mcp_token_set && "MCP token"].filter(Boolean).join(" and ")}.` : "Nothing connected yet: the Email area shows demo data."}</p>`,
    [...(any ? [["Disconnect", async () => { try { await api(A("connect"), {disconnect:true}); dlg.close(); toast("Disconnected: showing demo data"); E.aud = null; renderList() } catch(e) { toast(e.message) } }, "danger"]] : []),
     ["Test connection", async () => { $("#emConnMsg").textContent = "Testing ..."; try { const r = await api(A("test_connection"), {}); $("#emConnMsg").textContent = r.message } catch(e) { $("#emConnMsg").textContent = e.message } }],
     ["Save", async () => {
       try {
         await api(A("connect"), {name:$("#emName").value, api_url:$("#emApiUrl").value, token:$("#emTok").value, mcp_url:$("#emMcpUrl").value, mcp_token:$("#emMcpTok").value, same_token:$("#emSame").checked});
         $("#emTok").value = ""; $("#emMcpTok").value = "";
         $("#emConnMsg").textContent = "Saved. Testing ...";
         const r = await api(A("test_connection"), {}); dlg.close(); toast(r.message); E.aud = null; renderList();
       } catch(e) { $("#emConnMsg").textContent = e.message }
     }, "primary"]]);
}
function listOnlyOptions(sel){
  const a = E.aud || {lists:[]};
  return `<option value="">Pick a list</option>` + a.lists.map(x => `<option value="l:${esc(x.id)}" ${sel === "l:" + x.id ? "selected" : ""}>${esc(x.name)} (${num(x.count)})</option>`).join("");
}
async function createDraft(fields){
  const r = await api(A("new_campaign"), {...fields, confirm:USER});
  dlg.close(); toast(E.state.demo ? "Draft created in the demo data" : "Draft created on " + PN() + ". Nothing was sent."); openEditor(r.campaign.id);
}
async function newDialog(){
  try { if (!E.aud) E.aud = await api(A("audiences")) } catch(e) {}
  const segOk = can("segments_on_create");
  showDialog("New campaign", `<p class="hint">Creates a draft${E.state && E.state.demo ? " in the demo data" : " on " + esc(PN())}. Nothing is sent.${E.state.from_address ? " From and reply-to are always " + esc(E.state.from_address) + "." : ""}${segOk ? "" : " Through the MCP a new draft can only go to a list; set a segment on the platform."}</p>
    <div class="row"><label>Name</label><input id="emNewName" placeholder="December newsletter"></div>
    <div class="row"><label>Subject</label><input id="emNewSubj"></div>
    <div class="row"><label>Send to</label><select id="emNewAud">${segOk ? audOptions("") : listOnlyOptions("")}</select></div>`,
    [["Create draft", async () => {
      const a = $("#emNewAud").value;
      try { await createDraft({name:$("#emNewName").value, subject:$("#emNewSubj").value, list_id:a.startsWith("l:") ? a.slice(2) : null, segment_id:a.startsWith("s:") ? a.slice(2) : null}) }
      catch(e) { toast(e.message) }
    }, "primary"]]);
  setTimeout(() => $("#emNewName") && $("#emNewName").focus(), 50);
}
// MCP mode: the edits can't be written into the existing campaign, so make a new draft from them (the user's button)
async function createFromEditor(){
  const v = formValues(), aud = $("#em_aud").value;
  showDialog("Create draft on platform", `<p>Creates a <b>new draft</b> on ${esc(PN())} from what is in the editor: subject, HTML and list. The original campaign is not changed, and nothing is sent.</p>
    <div class="row"><label>Name</label><input id="emCopyName" value="${esc(v.name + " (from Studio)")}"></div>
    <div class="row"><label>Send to</label><select id="emCopyAud">${listOnlyOptions(aud.startsWith("l:") ? aud : "")}</select></div>
    <p class="hint">Checks run first (links are not checked here). Through the MCP a new draft can only go to a list.</p>`,
    [["Create draft", async () => {
      const a = $("#emCopyAud").value;
      try { await createDraft({name:$("#emCopyName").value, subject:v.subject, html:v.html, from_name:v.from_name, list_id:a ? a.slice(2) : null}) }
      catch(e) { toast(e.message) }
    }, "primary"]]);
}
function audLabel(){
  const v = $("#em_aud").value, a = E.aud || {lists:[], segments:[]};
  const x = v.startsWith("l:") ? a.lists.find(l => "l:" + l.id === v) : v.startsWith("s:") ? a.segments.find(s => "s:" + s.id === v) : null;
  return x ? `${x.name} (${num(x.count)} people)` : "no list or segment picked";
}
async function beforeAction(){ if (E.autoT) await autoSave(); if (isDirty()) return saveDraft(true); return true }
async function runAction(title, key, extra){
  const id = E.cur.original.id;
  try {
    const r = await withProgress(title, A(key), {id, confirm:USER, ...(extra || {}), ...(key !== "test_send" && window.TeamReviews ? TeamReviews.extra() : {})}, PROG);
    const list = await lastProgress(PROG);
    showDialog(r.ok ? title + ": done" : title + ": stopped", `${list}<p>${esc(r.message || "")}</p>${r.problems && r.problems.length ? probsHTML(r.problems) : ""}`, [["Close", () => dlg.close(), "primary"]]);
    if (r.problems && $("#emChecks")) $("#emChecks").innerHTML = probsHTML(r.problems);
  } catch(e) { dlg.close(); toast(e.message) }
  await reloadCurrent();
}
async function testDialog(){
  if (!(await beforeAction())) return;
  const list = (E.state && E.state.test_addresses) || [];
  const where = list.length > 1
    ? `<div class="row"><label>Send to</label><select id="emTestTo">${list.map(a => `<option value="${esc(a)}">${esc(a)}</option>`).join("")}</select></div>`
    : `<p>Sends one test email <b>to ${esc(list[0] || "the platform's test inbox")}</b>.</p>`;
  showDialog("Send a test", `${where}
    <p class="hint">Checks run first. ${E.cur.draft ? "Your Studio draft is saved to " + esc(PN()) + " first, so the test shows your edits." : ""}${E.state.demo ? " Demo data: nothing is really sent." : ""}${list.length ? "" : " Add your own test addresses in Setup > Email."}</p>`,
    [["Send test", () => runAction("Send test", "test_send", list.length > 1 ? {to: $("#emTestTo").value} : (list[0] ? {to: list[0]} : {})), "primary"]]);
  const sel = $("#emTestTo");
  if (sel){ const last = localGet("email.testTo"); if (last && list.includes(last)) sel.value = last; sel.onchange = () => localSet("email.testTo", sel.value) }
}
function defaultWhen(){
  const p = zoneParts(new Date(Date.now() + 86400000));
  return `${p.Y}-${pad(p.M)}-${pad(p.D)}T09:00`;
}
// [team reviews] in a team: what needs approval, and the one-line why, in the Schedule / Send now dialogs
const teamGate = async () => window.TeamReviews ? TeamReviews.gate("email", "email", String(E.cur.original.id)) : null;
function teamWire(t, extraOk){
  if (!window.TeamReviews) return;
  const b = $("#dlgF .live") || [...document.querySelectorAll("#dlgF button")].pop();
  const upd = () => { b.disabled = !TeamReviews.ready(t) || (t && t.required && !t.count) || (extraOk && !extraOk()) };
  TeamReviews.wire($("#dlgB"), null, upd); upd(); return upd;
}
async function scheduleDialog(){
  if (!(await beforeAction())) return;
  const t = await teamGate();
  showDialog("Schedule", `<p>Sends to <b>${esc(audLabel())}</b> at the time you pick.</p>
    <div class="row"><label>Date and time (${esc(tzName())})</label><input id="emWhen" type="datetime-local" value="${defaultWhen()}"></div>
    <div class="em-when" id="emWhenLocal"></div>
    <p class="hint">Checks run first. You can cancel the schedule until it starts sending.${E.state.demo ? " Demo data: nothing will really go out." : ""}</p>${window.TeamReviews ? TeamReviews.section(t) : ""}`,
    [["Schedule", () => {
      const v = $("#emWhen").value, d = zoneToDate(v);
      if (!d || d.getTime() < Date.now() + 120000) return toast("Pick a time at least a few minutes from now");
      runAction("Schedule", "schedule", {when:v, tz:sendTz()});
    }, "primary"]]);
  const upd = () => { const d = zoneToDate($("#emWhen").value); $("#emWhenLocal").textContent = d ? `${tzName()}: ${fmtDate(d.toISOString(), true)} · Your time: ${fmtLocal(d)}` : "" };
  $("#emWhen").oninput = upd; upd();
  teamWire(t);
}
async function sendNowDialog(){
  if (!(await beforeAction())) return;
  const t = await teamGate();
  showDialog("Send now", `<p>This sends the email to <b>${esc(audLabel())}</b> now.</p>
    <p><b>A sent email can't be unsent.</b> Checks run first; if any fail, nothing goes out.${E.state.demo ? " Demo data: nothing is really sent." : ""}</p>
    <label style="display:flex;gap:8px;align-items:center"><input type="checkbox" id="emSure"> I understand it can't be unsent</label>${window.TeamReviews ? TeamReviews.section(t) : ""}`,
    [["Send now", () => runAction("Send now", "send_now"), "live"]]);
  const btn = $("#dlgF .live"); btn.disabled = true;
  const upd = teamWire(t, () => $("#emSure").checked);
  $("#emSure").onchange = e => { if (upd) upd(); else btn.disabled = !e.target.checked };
}
function cancelDialog(){
  showDialog("Cancel the schedule", `<p>The campaign goes back to draft and will not go out at ${esc(fmtDate(E.cur.original.scheduled_for, true))} (${esc(tzName())}).</p>`,
    [["Cancel schedule", () => runAction("Cancel schedule", "cancel"), "danger"]]);
}

window.AREAS = window.AREAS || {};
window.AREAS.email = {label:"Email", render(el){ E.el = el; E.cur = null; E.ready = renderList() },
  open(coll, id){ return (E.ready || Promise.resolve()).then(() => openEditor(id)) }};  // [team live] jump to a campaign
})();
