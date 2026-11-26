// Marketing Studio - SEO > Topical map: the Koray-style map joined with Search Console + GA4.
// Claude proposes changes over MCP (seo_map_propose); they show here as proposals the user accepts or rejects.
(() => {
const CSS = `
.tm-sub{display:flex;gap:8px;align-items:center;padding:0 28px 12px;flex-wrap:wrap;font-size:13px}
.tm-sub input[type=search]{min-width:220px}
.tm-imp{display:inline-flex;align-items:center;height:34px;padding:0 14px;border-radius:var(--r);background:var(--panel);box-shadow:var(--sh-1);font-weight:600;color:var(--text);cursor:pointer;transition:background var(--t) var(--ease)}
.tm-imp:hover{background:var(--hover)}
.tm-covs{display:flex;gap:12px;padding:0 28px 12px;flex-wrap:wrap}
.tm-covs:empty{display:none}
.tm-cov{flex:1;min-width:240px;max-width:380px;display:grid;grid-template-columns:auto auto 1fr;gap:2px 10px;align-items:baseline;padding:10px 16px 12px;border-radius:var(--r-lg);background:var(--panel);box-shadow:var(--sh-card);font-size:12px;font-weight:500;color:var(--muted)}
.tm-cov b{font-size:20px;font-weight:600;letter-spacing:-.4px;color:var(--text);line-height:1.1}
.tm-bar{grid-column:1/-1;height:6px;border-radius:var(--r-pill);background:var(--accent-soft);overflow:hidden;margin-top:6px}
.tm-bar i{display:block;height:100%;border-radius:var(--r-pill);background:linear-gradient(90deg,var(--accent-soft),var(--accent-2))}
.tm-wrap{flex:1;min-height:0;display:grid;grid-template-columns:minmax(0,1fr) 420px;gap:12px;padding:0 28px 24px}
.tm-wrap.nopanel{grid-template-columns:minmax(0,1fr) 0;gap:0}
.tm-main{overflow:auto;min-width:0;border-radius:var(--r-lg);background:var(--panel);box-shadow:var(--sh-card)}
.tm-side{overflow:auto;padding:18px 20px;border-radius:var(--r-lg);background:var(--panel);box-shadow:var(--sh-card)}
.tm-wrap.nopanel .tm-side{padding:0;box-shadow:none}
.tm-side .tm-shd{display:flex;justify-content:space-between;align-items:center;margin-bottom:12px}
.tm-side .tm-shd b{font-size:16px;font-weight:600;color:var(--text)}
.tm-side .hint{font-size:12px;font-weight:500}
.tm-main table{width:100%}
.tm-main th{height:40px;background:var(--head);color:var(--muted);font-size:11.5px;font-weight:600;border-right:0;border-bottom:1px solid var(--line2);cursor:default}
.tm-main td{height:40px;border-right:0;color:var(--text2)}
.tm-main th:first-child,.tm-main td:first-child{padding-left:18px}
.tm-main td.wrap{white-space:normal;line-height:1.4;max-width:300px;font-size:12px;color:var(--muted)}
.tm-clamp{display:-webkit-box;-webkit-line-clamp:3;-webkit-box-orient:vertical;overflow:hidden;overflow-wrap:break-word}
.tm-legend{display:flex;gap:14px;flex-wrap:wrap;align-items:center;padding:0 28px 10px;font-size:12px;font-weight:500;color:var(--muted)}
.tm-legend span{display:inline-flex;align-items:center;gap:6px}
.tm-sub .fpills button{min-height:32px}
.tm-side button.danger{color:var(--bad-text, #c8303a)}
.tm-main tr.tm-row{cursor:pointer;transition:background var(--t) var(--ease)} .tm-main tr.tm-row:hover td{background:var(--hover)}
.tm-main tr.tm-row.sel td{background:var(--accent-bg)}
.tm-main tr.tm-row.sel td:first-child{box-shadow:inset 3px 0 0 var(--accent)}
.tm-main tr.dim td{opacity:.45}
.tm-t{display:flex;align-items:center;gap:8px;white-space:nowrap;color:var(--text);font-weight:500}
.tm-type{display:inline-flex;align-items:center;justify-content:center;width:20px;height:20px;flex:none;font-size:10px;font-weight:700;border-radius:var(--r-sm);background:var(--sunken);color:var(--muted)}
.tm-type.root{background:var(--accent);color:var(--ink-text)} .tm-type.hub{background:var(--violet-bg);color:var(--violet)} .tm-type.commercial{background:var(--warn-bg);color:var(--c4)}
.tm-st{display:inline-flex;align-items:center;height:22px;font-size:11px;font-weight:600;padding:0 8px;border-radius:var(--r-sm);white-space:nowrap}
.tm-st.gap{background:var(--bad-bg);color:var(--bad-text, #c8303a)} .tm-st.update{background:var(--warn-bg);color:var(--warn)} .tm-st.ok{background:var(--ok-bg);color:var(--ok)}
.tm-st.unknown{background:var(--sunken);color:var(--muted)}
#tmErr:empty{display:none} #tmErr .seo-errs{margin:0 28px 12px}
.tm-sec{font-size:10.5px;font-weight:600;color:var(--muted);text-transform:uppercase;letter-spacing:.06em}
.tm-prop{margin:0 28px 12px;padding:14px 18px;border-radius:var(--r-lg);background:var(--accent-bg);box-shadow:0 0 0 1px var(--accent-soft)}
.tm-prop>b{color:var(--accent);font-weight:600}
.tm-prop .hint{margin:0 0 0 6px;font-size:12px}
.tm-prop .r{display:flex;gap:8px;align-items:center;margin:8px 0 0;flex-wrap:wrap}
.tm-prop .r .s{flex:1;min-width:200px;font-size:13px;color:var(--text2)}
.tm-f{margin-bottom:12px} .tm-f label{display:block;color:var(--muted);font-size:12px;font-weight:600;margin-bottom:5px} .tm-f input,.tm-f textarea,.tm-f select{width:100%;box-sizing:border-box}
.tm-2{display:grid;grid-template-columns:1fr 1fr;gap:10px}
.tm-iss{font-size:12px;font-weight:500;margin:4px 0;padding:7px 10px;border-radius:var(--r-sm);background:var(--warn-bg);color:var(--warn)} .tm-iss.gap{background:var(--bad-bg);color:var(--bad)}
.tm-iss.unknown{background:var(--sunken);color:var(--muted)}
.tm-q{width:100%;font-size:12px;margin:0 0 6px} .tm-q th{height:30px;background:var(--head);color:var(--muted);font-weight:600;border-right:0;font-size:11px}
.tm-q td{height:30px;padding:0 6px;border-right:0;border-bottom:1px solid var(--line2)}
.tm-acts{display:flex;gap:8px;flex-wrap:wrap;margin-top:12px}
.tm-empty{padding:48px 20px;text-align:center;color:var(--muted);font-weight:500;line-height:1.6}
.tm-site{font-size:12.5px;font-weight:500;color:var(--muted);padding:0 28px 12px}
.tm-site b{font-weight:600;color:var(--text2)}
.tm-page{border-radius:var(--r-md);padding:12px 14px;margin:0 0 12px;background:var(--head)}
.tm-page .h{display:flex;align-items:center;gap:8px;justify-content:space-between;flex-wrap:wrap;font-size:13px}
.tm-page a{color:var(--accent);font-weight:600;text-decoration:none;word-break:break-all}
.tm-dr{display:inline-flex;align-items:center;height:20px;font-size:10.5px;font-weight:600;padding:0 7px;border-radius:var(--r-sm);background:var(--draft-bg);color:var(--draft)}
.tm-unm{padding:14px 18px 6px}
`;
if (!document.getElementById("tm-css")){ const st = document.createElement("style"); st.id = "tm-css"; st.textContent = CSS; document.head.appendChild(st) }

const S = {el: null, ctx: null, data: null, filter: localGet("tm.filter") || "all", search: "", sel: null, view: "map"};
const N = v => v == null || v === "" ? "" : Number(v).toLocaleString("en-GB");
const TY = {root: "R", hub: "H", attribute: "A", commercial: "C"};
const TYN = {root: "Root: the central topic", hub: "Hub: groups the pages under it", attribute: "Attribute: one detail of a topic", commercial: "Commercial: a page that sells"};
const STATE = {gap: "Gap", update: "Update", ok: "OK", unknown: "Unknown"};
const D = v => v == null ? "-" : N(v);  // a source that could not be read: "-", never 0
const scOff = () => !!(S.data && S.data.sources && S.data.sources.search_console === false);

function render(el, ctx){ S.el = el; S.ctx = ctx; S.sel = null; shell(); load() }
async function load(force){
  const m = document.getElementById("tmMain"); if (m && !S.data) m.innerHTML = `<div class="tm-empty">Loading the topical map with Search Console ...</div>`;
  try { S.data = await S.ctx.A("map", {...S.ctx.rq(), ...(force ? {force: 1} : {})}) } catch(e) { if (m) m.innerHTML = `<div class="tm-empty">${esc(e.message)}</div>`; return }
  draw();
}
function shell(){
  S.el.innerHTML = `<div id="tmErr"></div><div class="tm-covs" id="tmCov"></div><div class="tm-sub">
      <div class="fpills" id="tmPills"></div>
      <input type="search" id="tmSearch" placeholder="Search nodes, queries, URLs" value="${esc(S.search)}">
      <span class="spacer"></span>
      <button id="tmSite" title="Central entity, source context, central search intent">Site</button>
      <label class="tm-imp">Import CSV<input type="file" id="tmCsv" accept=".csv,text/csv" style="display:none"></label>
      <button id="tmAdd" title="Add a node to the map by hand">+ Node</button>
    </div><div id="tmSiteLine" class="tm-site"></div>
    <div class="tm-legend" aria-label="Node types">${Object.entries(TY).map(([k, l]) => `<span><span class="tm-type ${k}">${l}</span>${esc(TYN[k])}</span>`).join("")}</div><div id="tmProps"></div>
    <div class="tm-wrap" id="tmWrap"><div class="tm-main" id="tmMain"></div><div class="tm-side" id="tmSide"></div></div>`;
  const $ = s => S.el.querySelector(s);
  $("#tmPills").onclick = e => { const b = e.target.closest("[data-f]"); if (!b) return; S.filter = b.dataset.f; localSet("tm.filter", S.filter); draw() };
  $("#tmSearch").oninput = e => { S.search = e.target.value.toLowerCase(); drawMain() };
  $("#tmSite").onclick = siteDialog;
  $("#tmAdd").onclick = () => openNode(null, S.sel || "");
  $("#tmCsv").onchange = async e => { const f = e.target.files[0]; if (!f) return; e.target.value = "";
    if (!confirm(`Import "${f.name}" into the topical map? Rows are added as nodes (parents by title). Nothing is published.`)) return;
    try { const r = await S.ctx.P("map_import", {csv: await f.text()}); toast(`Imported ${r.done.length} nodes`); load() } catch(err) { toast(err.message) } };
}
function draw(){
  const d = S.data; if (!d) return;
  const $ = s => S.el.querySelector(s);
  const cov = d.coverage || {}, off = scOff();
  // [C2] the errors behind the numbers, on the map itself; without Search Console one box instead of numbers
  const errs = (d.errors || []).filter(e => !(off && /^Search Console/.test(e)));
  $("#tmErr").innerHTML = (off && S.ctx.scBox ? S.ctx.scBox(d.errors, "The map shows which nodes have a WordPress post or page; clicks, impressions, gaps from search data and unmapped demand are hidden until it can be read.") : "")
    + (errs.length ? `<p class="seo-errs">${errs.map(esc).join("<br>")}</p>` : "");
  $("#tmCov").innerHTML = ["core", "outer"].map(s => cov[s] && cov[s].nodes ? `<span class="tm-cov" title="Live = the node has a page with impressions, or a WordPress post or page"><span class="tm-sec">${s}</span><b>${cov[s].pct}%</b><span>live · ${cov[s].gaps} gaps · ${cov[s].update} to update${cov[s].unknown ? ` · ${cov[s].unknown} unknown` : ""}</span><span class="tm-bar"><i style="width:${Math.max(0, Math.min(100, +cov[s].pct || 0))}%"></i></span></span>` : "").join(" ");
  const cnt = k => d.nodes.filter(n => k === "all" || (k === "draft" ? n.new_post || n.in_draft : n.state === k)).length;
  if ((off && S.filter === "unmapped") || (S.filter === "unknown" && !cnt("unknown"))) S.filter = "all";
  $("#tmPills").innerHTML = [["all", "All"], ["gap", "Gaps"], ["update", "Needs update"], ["ok", "OK"], ...(cnt("unknown") ? [["unknown", "Unknown"]] : []), ["draft", "In draft"]].map(([k, l]) => `<button data-f="${k}" class="${S.filter === k ? "on" : ""}">${l} <span class="n">${cnt(k)}</span></button>`).join("")
    + (off ? "" : `<button data-f="unmapped" class="${S.filter === "unmapped" ? "on" : ""}" title="Search Console demand with no node in the map">Unmapped demand <span class="n">${(d.unmapped || []).length}</span></button>`);
  const site = d.site || {};
  $("#tmSiteLine").innerHTML = site.central_entity || site.source_context ? `<b>Central entity:</b> ${esc(site.central_entity || "-")} · <b>Source context:</b> ${esc(site.source_context || "-")} · <b>Central search intent:</b> ${esc(site.central_search_intent || "-")}${(site.exclude || []).length ? ` · <span title="${esc(site.exclude.map(x => x.path + (x.why ? " - " + x.why : "")).join("\n"))}">${site.exclude.length} left out on purpose</span>` : ""}`
    : `No central entity yet. Press <b>Site</b>, or ask Claude to build the map (MCP: seo_map_propose).`;
  drawProps(); drawMain();
  if (S.sel) openNode(S.sel); else $("#tmWrap").classList.add("nopanel");
  if (S.ctx.badge) S.ctx.badge((d.proposals || []).length);
}
function drawProps(){
  const ps = S.data.proposals || [], box = S.el.querySelector("#tmProps");
  if (!ps.length){ box.innerHTML = ""; return }
  const sum = p => { const c = {add: 0, update: 0, remove: 0, site: 0}; (p.summary || []).forEach(x => c[x.op]++);
    return [c.add && `${c.add} new`, c.update && `${c.update} changed`, c.remove && `${c.remove} removed`, c.site && "site settings"].filter(Boolean).join(", ") };
  box.innerHTML = `<div class="tm-prop"><b>${ps.length} proposal${ps.length > 1 ? "s" : ""} for the map</b> <span class="hint">Nothing changes until you accept.</span>
    ${ps.map(p => `<div class="r"><span class="s"><b>${p.by === "claude" ? "Claude" : esc(p.by)}</b> · ${esc(p.at ? window.studioFmt.dateTime(p.at) : "")} · ${esc(sum(p))}${p.note ? " · " + esc(p.note) : ""}</span>
      <button class="link" data-show="${p.id}">Show</button><button data-rej="${p.id}">Reject</button><button class="primary" data-acc="${p.id}">Accept</button></div>`).join("")}</div>`;
  box.onclick = async e => {
    const a = e.target.closest("[data-acc]"), r = e.target.closest("[data-rej]"), s = e.target.closest("[data-show]");
    if (s){ const p = ps.find(x => x.id === s.dataset.show); showDialog("Proposal", `${p.note ? `<p>${esc(p.note)}</p>` : ""}<ul>${(p.summary || []).map(x => `<li>${esc({add: "Add", update: "Change", remove: "Remove", site: "Site settings"}[x.op])}${x.title ? ": " + esc(x.title) : ""}</li>`).join("")}</ul>`, [["Close", () => dlg.close()]]); return }
    try {
      if (a){ const res = await S.ctx.P("map_accept", {id: a.dataset.acc}); toast(`Accepted: ${res.done.length} change${res.done.length === 1 ? "" : "s"}`); load() }
      if (r){ await S.ctx.P("map_reject", {id: r.dataset.rej}); toast("Proposal rejected"); load() }
    } catch(err) { toast(err.message) }
  };
}
function visible(){
  const d = S.data, byId = Object.fromEntries(d.nodes.map(n => [n.id, n]));
  const hit = n => (S.filter === "all" || (S.filter === "draft" ? n.new_post || n.in_draft : n.state === S.filter)) && (!S.search || [n.title, n.url, n.entity, n.attribute, ...(n.queries || []).map(q => q.query)].join(" ").toLowerCase().includes(S.search));
  const show = new Set(), strong = new Set();
  d.nodes.forEach(n => { if (hit(n)){ strong.add(n.id); let p = n; while (p){ show.add(p.id); p = byId[p.parent] } } });
  return {show, strong, byId};
}
function drawMain(){
  const d = S.data, main = S.el.querySelector("#tmMain");
  if (S.filter === "unmapped") return drawUnmapped(main);
  if (!d.nodes.length){ main.innerHTML = `<div class="tm-empty">The map is empty.<br><br>Import a spreadsheet (Import CSV), add nodes by hand (+ Node),<br>or ask Claude to build it with the Koray method (MCP tool seo_map_propose).</div>`; return }
  const {show, strong} = visible();
  const kids = {};
  d.nodes.forEach(n => (kids[n.parent && d.nodes.some(x => x.id === n.parent) ? n.parent : ""] = kids[n.parent && d.nodes.some(x => x.id === n.parent) ? n.parent : ""] || []).push(n));
  Object.values(kids).forEach(a => a.sort((x, y) => (x.priority || 3) - (y.priority || 3) || x.title.localeCompare(y.title)));
  const rows = [];
  const walk = (pid, depth) => (kids[pid] || []).forEach(n => { if (!show.has(n.id)) return; rows.push([n, depth]); walk(n.id, depth + 1) });
  walk("", 0);
  const dc = v => v == null ? "-" : v ? `<span class="${v > 0 ? "seo-up" : "seo-down"}">${v > 0 ? "+" : ""}${N(v)}</span>` : "";
  const off = scOff();  // [T10] no Search Console: its columns are left out, not filled with zeros
  main.innerHTML = rows.length ? `<table><thead><tr><th>Node</th><th>Section</th>${off ? "" : `<th class="num">Clicks</th><th class="num">vs prev</th><th class="num">Impr.</th><th class="num">Pos.</th>`}<th class="num">Sessions</th><th>State</th><th>Issues</th></tr></thead><tbody>
    ${rows.map(([n, depth]) => `<tr class="tm-row ${strong.has(n.id) ? "" : "dim"} ${S.sel === n.id ? "sel" : ""}" data-id="${n.id}">
      <td><span class="tm-t" style="padding-left:${depth * 18}px"><span class="tm-type ${n.type}" title="${esc(TYN[n.type] || n.type)}">${TY[n.type] || "?"}</span>${esc(n.title)}${n.children ? ` <span class="hint">(${n.children})</span>` : ""}${n.new_post ? ` <span class="tm-dr" title="A new page is waiting in the SEO draft">new page in draft</span>` : n.in_draft ? ` <span class="tm-dr" title="Edits waiting in the SEO draft">${n.in_draft} in draft</span>` : ""}</span></td>
      <td><span class="tm-sec">${esc(n.section)}</span></td>${off ? "" : `<td class="num">${D(n.clicks)}</td><td class="num">${dc(n.delta_clicks)}</td><td class="num">${D(n.impressions)}</td>
      <td class="num">${n.position ? n.position.toFixed(1) : n.position == null && n.clicks == null ? "-" : ""}</td>`}<td class="num">${D(n.sessions)}</td>
      <td><span class="tm-st ${n.state}">${STATE[n.state]}</span></td><td class="wrap">${(n.issues || []).length ? `<span class="tm-clamp" title="${esc(n.issues.map(i => i.text).join(" "))}">${esc(n.issues.map(i => i.text).join(" "))}</span>` : ""}</td></tr>`).join("")}</tbody></table>`
    : `<div class="tm-empty">Nothing matches this filter.</div>`;
  main.onclick = e => { const tr = e.target.closest("tr[data-id]"); if (tr){ S.sel = tr.dataset.id; drawMain(); openNode(S.sel) } };
}
function drawUnmapped(main){
  const u = S.data.unmapped || [];
  main.innerHTML = u.length ? `<div class="hint tm-unm">Pages with Search Console impressions that no node points at, with their top queries. Add one as a node, or ask Claude to fit them into the map.</div>
    <table><thead><tr><th>Page</th><th class="num">Impr.</th><th class="num">Clicks</th><th>Top queries</th><th></th></tr></thead><tbody>
    ${u.map((g, i) => `<tr><td>${esc(g.page)}</td><td class="num">${N(g.impressions)}</td><td class="num">${N(g.clicks)}</td>
      <td class="wrap" style="font-size:12px">${esc(g.queries.slice(0, 6).map(q => q.query).join(", "))}</td><td><button class="link" data-add="${i}">Add as node</button></td></tr>`).join("")}</tbody></table>`
    : `<div class="tm-empty">No unmapped demand above the threshold for this period.</div>`;
  main.onclick = e => { const b = e.target.closest("[data-add]"); if (!b) return; const g = u[+b.dataset.add];
    openNode(null, "", {title: g.queries[0] ? g.queries[0].query.replace(/^./, c => c.toUpperCase()) : g.page, url: g.page, queries: g.queries.slice(0, 8).map(q => ({query: q.query}))}) };
}
function openNode(id, parent, preset){
  const d = S.data, n = id ? d.nodes.find(x => x.id === id) : {id: "", parent: parent || "", title: "", section: "outer", type: "attribute", queries: [], links: [], priority: 3, issues: [], ...(preset || {})};
  if (!n) return;
  S.el.querySelector("#tmWrap").classList.remove("nopanel");
  const side = S.el.querySelector("#tmSide");
  Object.assign(side.dataset, {teamColl: "seo:map_nodes", teamId: id || "", teamLabel: n.title || ""});  // [team live] presence, locks, claim
  const opts = (sel, list) => list.map(v => `<option ${v === sel ? "selected" : ""}>${v}</option>`).join("");
  const parents = d.nodes.filter(x => x.id !== n.id).sort((a, b) => a.title.localeCompare(b.title));
  side.innerHTML = `<div class="tm-shd"><b>${id ? "Node" : "New node"}</b><button class="link" id="tmClose">Close</button></div>
    ${id ? pageBox(n) : ""}
    ${(n.issues || []).map(i => `<div class="tm-iss ${i.level}">${esc(i.text)}</div>`).join("")}
    ${id && n.url ? `<p class="hint" style="margin:8px 0">${n.clicks == null ? "Search Console: not available" : `${N(n.clicks)} clicks · ${N(n.impressions)} impressions · CTR ${((n.ctr || 0) * 100).toFixed(1)}% · position ${n.position ? n.position.toFixed(1) : "-"}`} · ${D(n.sessions)} sessions · ${D(n.key_events)} key events</p>` : ""}
    <div class="tm-f"><label>Title</label><input id="tmTitle" value="${esc(n.title)}"></div>
    <div class="tm-2"><div class="tm-f"><label>Section</label><select id="tmSecSel">${opts(n.section, ["core", "outer"])}</select></div>
      <div class="tm-f"><label>Type</label><select id="tmType">${opts(n.type, ["root", "hub", "attribute", "commercial"])}</select></div></div>
    <div class="tm-f"><label>Parent</label><select id="tmParent"><option value="">(top level)</option>${parents.map(p => `<option value="${p.id}" ${p.id === n.parent ? "selected" : ""}>${esc(p.title)}</option>`).join("")}</select></div>
    <div class="tm-2"><div class="tm-f"><label>Entity</label><input id="tmEntity" value="${esc(n.entity || "")}"></div><div class="tm-f"><label>Attribute</label><input id="tmAttr" value="${esc(n.attribute || "")}"></div></div>
    <div class="tm-f"><label>Macro context</label><input id="tmMacro" value="${esc(n.macro_context || "")}"></div>
    <div class="tm-2"><div class="tm-f"><label>Search intent</label><input id="tmIntent" value="${esc(n.intent || "")}" placeholder="informational, commercial ..."></div>
      <div class="tm-f"><label>Priority</label><select id="tmPrio">${[1, 2, 3, 4].map(v => `<option value="${v}" ${v === (n.priority || 3) ? "selected" : ""}>${["", "1 - highest", "2 - high", "3 - normal", "4 - low"][v]}</option>`).join("")}</select></div></div>
    <div class="tm-f"><label>URL</label><input id="tmUrl" value="${esc(n.url || "")}" placeholder="https://... or /path/ (empty = not written yet)"></div>
    <div class="tm-f"><label>Target queries (one per line)</label><textarea id="tmQ" rows="4">${esc((n.queries || []).map(q => q.query || q).join("\n"))}</textarea></div>
    ${id && (n.queries || []).length ? `<table class="tm-q"><tr><th>Query</th><th class="num">Impr.</th><th class="num">Clicks</th><th class="num">Pos.</th><th>Lands on</th></tr>${n.queries.map(q => `<tr><td>${esc(q.query)}</td><td class="num">${N(q.impressions)}</td><td class="num">${N(q.clicks)}</td><td class="num">${q.position ? q.position.toFixed(1) : ""}</td><td>${q.lands_on ? `<span class="seo-down">${esc(q.lands_on)}</span>` : ""}</td></tr>`).join("")}</table>` : ""}
    <div class="tm-f" style="margin-top:10px"><label>Must link to (node, anchor text)</label><div id="tmLinks">${(n.links || []).map((l, i) => linkRow(parents, l, i)).join("")}</div><button class="link" id="tmAddLink">+ link</button></div>
    <div class="tm-f"><label>Note</label><textarea id="tmNote" rows="2">${esc(n.note || "")}</textarea></div>
    <div class="tm-acts"><button class="primary" id="tmSave">${id ? "Save" : "Add node"}</button>${id ? `<button id="tmChild">Add child</button><button class="danger" id="tmDel">Remove</button>` : ""}</div>`;
  const $ = s => side.querySelector(s);
  side.querySelectorAll("[data-pg]").forEach(b => b.onclick = () => {
    const k = b.dataset.pg;
    if (k === "post") S.ctx.openPost(n.post_id);
    if (k === "new") S.ctx.openNewPage({id: n.new_post.id});
    if (k === "write") S.ctx.openNewPage({node: n});
  });
  $("#tmClose").onclick = () => { S.sel = null; side.dataset.teamId = ""; S.el.querySelector("#tmWrap").classList.add("nopanel"); drawMain() };
  $("#tmAddLink").onclick = () => $("#tmLinks").insertAdjacentHTML("beforeend", linkRow(parents, {to: "", anchor: ""}, Date.now()));
  $("#tmSave").onclick = async () => {
    const node = {title: $("#tmTitle").value, section: $("#tmSecSel").value, type: $("#tmType").value, parent: $("#tmParent").value,
      entity: $("#tmEntity").value, attribute: $("#tmAttr").value, macro_context: $("#tmMacro").value, intent: $("#tmIntent").value,
      priority: +$("#tmPrio").value, url: $("#tmUrl").value, queries: $("#tmQ").value.split("\n").map(s => s.trim()).filter(Boolean), note: $("#tmNote").value,
      links: [...side.querySelectorAll(".tm-link")].map(r => ({to: r.querySelector("select").value, anchor: r.querySelector("input").value})).filter(l => l.to)};
    try { const r = await S.ctx.P("map_edit", {ops: [id ? {op: "update", id, node} : {op: "add", node}]}); if (!id) S.sel = r.done[0].id; toast("Saved"); load() }
    catch(e) { toast(e.message) }
  };
  if ($("#tmChild")) $("#tmChild").onclick = () => openNode(null, id);
  if ($("#tmDel")) $("#tmDel").onclick = async () => { if (!confirm(`Remove "${n.title}" from the map? Its children move up one level. The page itself is not touched.`)) return;
    try { await S.ctx.P("map_edit", {ops: [{op: "remove", id}]}); S.sel = null; toast("Removed from the map"); load() } catch(e) { toast(e.message) } };
}
function pageBox(n){
  const ask = `<div class="hint" style="margin-top:6px">Or ask Claude in Terminal: <i>write the page for "${esc(n.title)}"</i> (MCP: seo_write_page). It lands here for you to edit.</div>`;
  if (n.post_id) return `<div class="tm-page"><div class="h"><span><b>Page</b> · WordPress post${n.post_status && n.post_status !== "publish" ? ` (${esc(n.post_status)})` : ""}${n.in_draft ? ` · <span class="tm-dr">${n.in_draft} in draft</span>` : ""}</span>
    <button class="primary" data-pg="post">Edit text</button></div><div class="hint" style="margin-top:4px"><a href="${esc(n.url)}" target="_blank" rel="noopener">${esc(n.url)}</a></div>
    <div class="hint" style="margin-top:6px">Ask Claude to rewrite or update it (MCP: seo_add_draft): the change shows in the editor and in Draft.</div></div>`;
  if (n.new_post) return `<div class="tm-page"><div class="h"><span><b>New page in the draft</b> · ${esc(n.new_post.title)} · ${Number(n.new_post.words || 0).toLocaleString("en-GB")} words · by ${esc(n.new_post.by)}</span>
    <button class="primary" data-pg="new">Edit text</button></div><div class="hint" style="margin-top:4px">Publish creates it in WordPress and points this node at its URL.</div></div>`;
  if (n.url && n.state === "unknown") return `<div class="tm-page"><div class="h"><span><b>Page unknown</b> · not a WordPress post or page, and Search Console could not be read</span><a href="${esc(n.url)}" target="_blank" rel="noopener">Open</a></div>
    <div class="hint" style="margin-top:4px">Studio can't tell if a page is there. Open the URL to check, or sign in to Search Console.</div></div>`;
  if (!n.url || n.state === "gap" && !n.live) return `<div class="tm-page"><div class="h"><span><b>No page yet</b></span><button class="primary" data-pg="write">Write page</button></div>${ask}</div>`;
  return `<div class="tm-page"><div class="h"><span><b>Page</b> · not a blog post (a WordPress page, trip or destination page)</span><a href="${esc(n.url)}" target="_blank" rel="noopener">Open</a></div>
    <div class="hint" style="margin-top:4px">Studio edits blog posts only. Edit this one in wp-admin.</div></div>`;
}
function linkRow(nodes, l, i){
  return `<div class="tm-link tm-2" style="margin-bottom:4px"><select><option value="">(node)</option>${nodes.map(p => `<option value="${p.id}" ${p.id === l.to ? "selected" : ""}>${esc(p.title)}</option>`).join("")}</select><input placeholder="anchor text" value="${esc(l.anchor || "")}"></div>`;
}
function siteDialog(){
  const s = (S.data && S.data.site) || {};
  showDialog("Site: the map's anchor", `<p class="hint">The Koray method starts here. The <b>central entity</b> is what the whole site is about; the <b>source context</b> is who you are and why you publish (it decides which topics belong); the <b>central search intent</b> is the user's underlying purpose.</p>
    <div class="tm-f"><label>Central entity</label><input id="tmCe" value="${esc(s.central_entity || "")}"></div>
    <div class="tm-f"><label>Source context</label><textarea id="tmSc" rows="3">${esc(s.source_context || "")}</textarea></div>
    <div class="tm-f"><label>Central search intent</label><textarea id="tmCi" rows="2">${esc(s.central_search_intent || "")}</textarea></div>
    <div class="tm-f"><label>Left out of the map on purpose (one per line: path - why). A path ending in / covers everything under it.</label><textarea id="tmEx" rows="4" placeholder="/de/ - German translations">${esc((s.exclude || []).map(x => x.path + (x.why ? " - " + x.why : "")).join("\n"))}</textarea></div>`,
    [["Save", async () => { try { await S.ctx.P("map_edit", {ops: [{op: "site", site: {central_entity: $("#tmCe").value, source_context: $("#tmSc").value, central_search_intent: $("#tmCi").value,
      exclude: $("#tmEx").value.split("\n").map(l => l.trim()).filter(Boolean).map(l => { const i = l.indexOf(" - "); return i > 0 ? {path: l.slice(0, i).trim(), why: l.slice(i + 3).trim()} : {path: l} })}}]}); dlg.close(); toast("Saved"); load() } catch(e) { toast(e.message) } }, "primary"]]);
}
// [team live] open a node from the Team tab or a mention (waits for the map to load)
async function openById(id){
  for (let i = 0; i < 100 && !(S.data && S.data.nodes.some(n => n.id === id)); i++) await new Promise(r => setTimeout(r, 100));
  if (!S.data || !S.data.nodes.some(n => n.id === id)) return toast("That node is not in the map any more");
  S.sel = id; drawMain(); openNode(id);
  const tr = S.el.querySelector(`tr[data-id="${CSS.escape(id)}"]`); if (tr) tr.scrollIntoView({block: "center"});
}
window.SEO_MAP = {render, reload: force => load(force), open: openById};
})();
