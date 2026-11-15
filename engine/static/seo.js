// Marketing Studio - SEO area: masterliveaboards.com posts, Search Console, GA4, the SEO draft and the user's Publish button.
(() => {
const CSS = `
.seo-bar{gap:10px}
.seo-bar .seo-tabs button .n{margin-left:4px}
.seo-target{font-size:12px;font-weight:500;padding:3px 9px;border-radius:2px;background:var(--line2);color:var(--text2);white-space:nowrap}
.seo-target.prod{background:var(--bad);color:#fff}
.seo-target.dry{background:var(--info-bg);color:var(--text)}
.seo-sub{display:flex;gap:10px;align-items:center;padding:8px 14px;background:var(--head);border-bottom:1px solid var(--line);flex-wrap:wrap;font-size:13px}
.seo-sub input[type=search]{min-width:240px}
.seo-body{flex:1;min-height:0;display:flex;flex-direction:column}
.seo-scroll{overflow:auto;flex:1;padding:16px 20px}
.seo-body table tr.seo-row{cursor:pointer}
.seo-body table tr.seo-row:hover td{background:var(--hover)}
.seo-body td.title{max-width:360px}
.seo-body td.wrap{white-space:normal;min-width:260px;max-width:420px;line-height:1.35;padding-top:4px;padding-bottom:4px;height:auto}
.seo-up{color:var(--ok)} .seo-down{color:var(--bad)}
.seo-len{font-size:11px;color:var(--muted);margin-left:6px} .seo-len.bad{color:var(--warn)}
.seo-errs{margin:0;padding:8px 14px;background:var(--warn-bg);font-size:13px}
dialog.seo-wide{width:min(1180px,96vw)}
.seo-dtabs{display:flex;gap:6px;margin:0 0 12px}
.seo-grid2{display:grid;grid-template-columns:1fr 1fr;gap:14px}
.seo-f{margin-bottom:12px} .seo-f label{display:flex;justify-content:space-between;color:var(--muted);font-size:13px;margin-bottom:3px}
.seo-f input,.seo-f textarea{width:100%}
.seo-f textarea.code{font-family:"Roboto Mono",ui-monospace,Menlo,monospace;font-size:12px;min-height:340px}
.seo-snip{border:1px solid var(--line);border-radius:8px;padding:12px 14px;background:var(--panel);margin-bottom:12px;font-family:Arial,sans-serif}
.seo-snip .u{font-size:12px;color:var(--text2)} .seo-snip .t{font-size:19px;color:#1a0dab;margin:3px 0;line-height:1.3}
@media (prefers-color-scheme: dark){.seo-snip .t{color:#99c3ff}}
.seo-snip .d{font-size:13px;color:var(--text2);line-height:1.45}
.seo-prev{width:100%;height:340px;border:1px solid var(--line);border-radius:4px;background:#fff}
.seo-cats{display:flex;flex-wrap:wrap;gap:6px}
.seo-cats label{display:inline-flex;gap:5px;align-items:center;font-size:13px;color:var(--text2);border:1px solid var(--line);border-radius:14px;padding:2px 9px}
.seo-img{max-width:100%;max-height:170px;border-radius:4px;border:1px solid var(--line)}
.seo-box{border:1px solid var(--line);border-radius:8px;padding:12px 14px;margin-bottom:14px;background:var(--panel)}
.seo-box h3{margin:0 0 8px;font-size:15px;font-weight:500}
.seo-box table{width:100%;min-width:0}
.seo-sec{margin:0 0 22px} .seo-sec h2{font-size:16px;font-weight:500;margin:0 0 4px}
.seo-ch{display:grid;grid-template-columns:150px 1fr auto;gap:10px;padding:8px 0;border-bottom:1px solid var(--line2);font-size:13px;align-items:start}
.seo-ch .old{color:var(--muted);text-decoration:line-through;word-break:break-word}
.seo-ch .new{word-break:break-word;background:var(--draft-bg);color:var(--text);padding:1px 4px;border-radius:2px}
.seo-iss{display:block;font-size:12px;margin-top:3px} .seo-iss.error{color:var(--bad)} .seo-iss.warn{color:var(--warn)} .seo-iss.info{color:var(--muted)}
.seo-post-h{font-weight:500;margin:14px 0 2px}
.seo-steps{font-size:12px;color:var(--muted)}
.seo-dot{display:inline-block;width:8px;height:8px;border-radius:50%;background:#9aa0a6;margin-right:5px}
.seo-dot.ok{background:var(--ok)} .seo-dot.bad{background:var(--bad)}
.seo-kv{display:grid;grid-template-columns:170px 1fr;gap:6px 12px;font-size:13px}
.seo-kv b{font-weight:500;color:var(--muted)}
`;
if (!document.getElementById("seo-css")){ const st = document.createElement("style"); st.id = "seo-css"; st.textContent = CSS; document.head.appendChild(st) }

const STEPS = ["Checking", "Saving revision note", "Updating", "Re-inspecting"];
const T = {tab: localGet("seo.tab") || "map", view: localGet("seo.view") || "data", filter: "all", range: localGet("seo.range") || "28",
           start: "", end: "", search: "", sort: {key: "clicks", dir: -1}, data: null, draft: null, settings: null, cats: null, opp: null, el: null};
const A = (k, q) => api("/api/area/seo/" + k + (q ? "?" + qs(q) : ""));
const P = (k, body) => api("/api/area/seo/" + k, body || {});
const rq = () => T.range === "custom" ? {start: T.start, end: T.end} : {range: T.range};
const N = v => v == null || v === "" ? "" : Number(v).toLocaleString("en-GB");
const pct = v => v == null ? "" : (100 * v).toFixed(1) + "%";
const day = s => s ? s.slice(0, 10) : "";
const LBL = {title: "Title", slug: "Slug", seo_title: "SEO title", meta_desc: "Meta description", focus_kw: "Focus keyphrase", excerpt: "Excerpt", categories: "Categories", content: "Body"};
const short = (v, n = 140) => { const s = Array.isArray(v) ? v.join(", ") : String(v ?? ""); return s.length > n ? s.slice(0, n) + " ..." : s };

// ---- shell ----
function render(el){
  T.el = el;
  el.innerHTML = `<div class="toolbar seo-bar">
    <select id="seoSite" title="Which site"><option value="ml">masterliveaboards.com</option><option value="dt" disabled>divetravel.co.uk (off until the director agrees)</option></select>
    <div class="fpills seo-tabs" id="seoTabs"></div>
    <span class="spacer"></span>
    <select id="seoRange" title="Date range for Search Console and GA4"><option value="7">Last 7 days</option><option value="28">Last 28 days</option><option value="90">Last 90 days</option><option value="custom">Custom ...</option></select>
    <span id="seoCustom" style="display:none"><input type="date" id="seoStart"> to <input type="date" id="seoEnd"> <button id="seoApply">Apply</button></span>
    <span id="seoTarget" class="seo-target"></span>
    <button id="seoRefresh" title="Read WordPress, Search Console and GA4 again">Refresh</button>
    <button id="seoPublish" class="primary" title="Your button only: publish the SEO draft">Publish</button>
  </div><div id="seoBody" class="seo-body"></div>`;
  $("#seoRange").value = T.range;
  $("#seoRange").onchange = e => { T.range = e.target.value; $("#seoCustom").style.display = T.range === "custom" ? "" : "none"; if (T.range !== "custom"){ localSet("seo.range", T.range); reload() } };
  $("#seoApply").onclick = () => { T.start = $("#seoStart").value; T.end = $("#seoEnd").value; if (T.start && T.end) reload(); else toast("Pick a start and end date") };
  $("#seoRefresh").onclick = async () => { await dataRefresh("seo", null, {reload:false}); T.data = null; T.opp = null; reload(false) };
  $("#seoPublish").onclick = publishFlow;
  $("#seoTabs").onclick = e => { const b = e.target.closest("[data-t]"); if (b){ T.tab = b.dataset.t; localSet("seo.tab", T.tab); show() } };
  loadSettings().then(() => { loadDraft(); show() });
}
function tabs(){
  const n = T.draft ? T.draft.changes.length + (T.draft.new_posts || []).length : 0;
  $("#seoTabs").innerHTML = [["map", `Topical map${T.mapProps ? `<span class="n">${T.mapProps}</span>` : ""}`], ["posts", "Posts"], ["opps", "Opportunities"], ["draft", `Draft<span class="n">${n}</span>`], ["settings", "Settings"]]
    .map(([k, l]) => `<button data-t="${k}" class="${k === T.tab ? "on" : ""}">${l}</button>`).join("");
}
function targetChip(){
  const s = T.settings || {};
  const el = $("#seoTarget"); if (!el) return;
  const live = s.mode === "live" && s.connected;
  el.className = "seo-target " + (live ? (s.target === "prod" ? "prod" : "") : "dry");
  el.textContent = live ? (s.target === "prod" ? "PROD: live WordPress" : "Staging") : `Dry run (${s.target === "prod" ? "prod" : "staging"})`;
  el.title = live ? "Publish writes to WordPress " + s.target : "Publish only records what it would send. Connect WordPress in Settings.";
  $("#seoPublish").className = live && s.target === "prod" ? "live" : "primary";
}
async function loadSettings(){ try { T.settings = await A("settings") } catch(e) { T.settings = {target: "staging", mode: "dry"} } targetChip() }
async function loadDraft(){ try { T.draft = await A("draft") } catch(e) { T.draft = {changes: []} } tabs(); if (T.tab === "draft") drawDraft() }
function reload(force){ if (T.tab === "posts") loadPosts(force); else if (T.tab === "opps") loadOpps(); else if (T.tab === "map" && window.SEO_MAP) window.SEO_MAP.reload(force); }
function mapScript(){
  if (window.SEO_MAP) return Promise.resolve();
  return new Promise((ok, bad) => { const sc = document.createElement("script"); sc.src = "/static/seo_map.js"; sc.onload = ok; sc.onerror = () => bad(new Error("could not load the topical map")); document.head.appendChild(sc) });
}
function show(){
  tabs();
  const b = $("#seoBody");
  if (T.tab === "posts"){ b.innerHTML = postsShell(); wirePosts(); if (T.data) drawPosts(); else loadPosts() }
  else if (T.tab === "opps"){ b.innerHTML = `<div class="seo-scroll" id="seoOpp"><p class="hint">Loading opportunities ...</p></div>`; loadOpps() }
  else if (T.tab === "draft"){ b.innerHTML = `<div class="seo-scroll" id="seoDraft"></div>`; drawDraft(); loadHistory() }
  else if (T.tab === "map"){ b.innerHTML = `<div id="seoMap" style="flex:1;min-height:0;display:flex;flex-direction:column"></div>`;
    mapScript().then(() => window.SEO_MAP.render($("#seoMap"), {A, P, rq, openPost, openNewPage, badge: n => { if (T.mapProps !== n){ T.mapProps = n; tabs() } }})).catch(e => { b.innerHTML = `<p class="hint" style="padding:16px">${esc(e.message)}</p>` }) }
  else { b.innerHTML = `<div class="seo-scroll" id="seoSet"></div>`; drawSettings() }
}

// ---- posts grid ----
const COLS = {
  data: [
    {k: "title", l: "Title", cls: "title"}, {k: "status", l: "Status", f: v => `<span class="pill ${v === "publish" ? "Enabled" : ""}"><i></i>${esc(v === "publish" ? "Published" : v)}</span>`},
    {k: "date", l: "Published", f: day}, {k: "modified", l: "Updated", f: day}, {k: "words", l: "Words", n: 1, f: N},
    {k: "clicks", l: "Clicks", n: 1, f: N}, {k: "impressions", l: "Impr.", n: 1, f: N}, {k: "ctr", l: "CTR", n: 1, f: pct},
    {k: "position", l: "Avg pos.", n: 1, f: v => v == null ? "" : Number(v).toFixed(1)},
    {k: "delta_clicks", l: "Clicks vs prev.", n: 1, f: v => v ? `<span class="${v > 0 ? "seo-up" : "seo-down"}">${v > 0 ? "+" : ""}${N(v)}</span>` : "0"},
    {k: "sessions", l: "Organic sessions", n: 1, f: N}, {k: "select_cabin", l: "Select Cabin", n: 1, f: N},
    {k: "links_in", l: "Links in", n: 1}, {k: "links_out", l: "Links out", n: 1},
    {k: "index", l: "Index", f: (v, r) => r.indexed == null ? `<span class="hint">Not checked</span>` : `<span class="seo-dot ${r.indexed ? "ok" : "bad"}"></span>${esc(v || "")}`}],
  edit: [
    {k: "title", l: "Title", cls: "title", e: 1}, {k: "slug", l: "Slug", e: 1}, {k: "seo_title", l: "SEO title", e: 1, len: [30, 60]},
    {k: "meta_desc", l: "Meta description", e: 1, len: [70, 160], cls: "wrap"}, {k: "focus_kw", l: "Focus keyphrase", e: 1},
    {k: "category_names", l: "Categories", f: v => esc((v || []).join(", "))}, {k: "status", l: "Status"}, {k: "clicks", l: "Clicks", n: 1, f: N}],
};
const FILTERS = [["all", "All"], ["publish", "Published"], ["drafts", "Drafts"], ["losing", "Losing clicks"], ["noindex", "Not indexed"], ["indraft", "In draft"]];
function pass(r, f){
  switch (f || T.filter){
    case "publish": return r.status === "publish";
    case "drafts": return r.status !== "publish";
    case "losing": return r.losing;
    case "noindex": return r.indexed === false;
    case "indraft": return (r.draft || []).length > 0;
    default: return true;
  }
}
function postsShell(){
  return `<div class="seo-sub"><input type="search" id="seoSearch" placeholder="Search title, URL or SEO fields" value="${esc(T.search)}">
    <div class="fpills" id="seoF"></div><span class="spacer"></span>
    <div class="fpills" id="seoView"><button data-v="data">Data</button><button data-v="edit">Edit</button></div>
    <span id="seoRangeLbl" class="hint" style="margin:0"></span></div>
    <div id="seoNote"></div><div class="grid-wrap"><table id="seoTable"><tbody><tr><td class="hint">Loading posts, Search Console and GA4 ...</td></tr></tbody></table></div>`;
}
function wirePosts(){
  $("#seoSearch").oninput = e => { T.search = e.target.value; drawPosts() };
  $("#seoF").onclick = e => { const b = e.target.closest("[data-f]"); if (b && !b.disabled){ T.filter = b.dataset.f; drawPosts() } };
  $("#seoView").onclick = e => { const b = e.target.closest("[data-v]"); if (b){ T.view = b.dataset.v; localSet("seo.view", T.view); drawPosts() } };
}
async function loadPosts(force){
  if (T.tab !== "posts") return;
  try { T.data = await A("posts", {...rq(), ...(force ? {force: 1} : {})}) } catch(e) { $("#seoTable").innerHTML = `<tbody><tr><td class="prob">${esc(e.message)}</td></tr></tbody>`; return }
  T.settings = T.data.settings || T.settings; targetChip(); drawPosts();
}
function drawPosts(){
  if (!T.data || !$("#seoTable")) return;
  const d = T.data, rows = d.rows;
  $("#seoView").querySelectorAll("button").forEach(b => b.classList.toggle("on", b.dataset.v === T.view));
  $("#seoF").innerHTML = FILTERS.map(([k, l]) => { const dis = k === "drafts" && !d.authed;
    return `<button data-f="${k}" class="${k === T.filter ? "on" : ""}" ${dis ? "disabled title=\"WordPress drafts need the Marketing Studio login (Settings)\"" : ""}>${l}${k !== "all" ? `<span class="n">${rows.filter(r => pass(r, k)).length}</span>` : ""}</button>` }).join("");
  $("#seoRangeLbl").textContent = `${d.range.start} to ${d.range.end} vs ${d.range.prev_start} to ${d.range.prev_end}`;
  const notes = [];
  if (d.errors && d.errors.length) notes.push(d.errors.map(esc).join("<br>"));
  if (T.view === "edit" && !d.snippet_installed) notes.push("Focus keyphrase is blank: Yoast does not share it over the REST API until the Studio snippet is installed . SEO title shows the title Google sees.");
  if (T.filter === "noindex") notes.push("Index status comes from URL Inspection. Check a post (open it) or run Check index status in Opportunities.");
  $("#seoNote").innerHTML = notes.length ? `<p class="seo-errs">${notes.join("<br>")}</p>` : "";
  const s = T.search.toLowerCase();
  let list = rows.filter(r => pass(r) && (!s || [r.title, r.url, r.seo_title, r.meta_desc, r.focus_kw].some(x => (x || "").toLowerCase().includes(s))));
  const {key, dir} = T.sort;
  list.sort((a, b) => { const x = a[key], y = b[key]; return (typeof x === "number" || typeof y === "number" ? ((x ?? -1e9) - (y ?? -1e9)) : String(x ?? "").localeCompare(String(y ?? ""))) * dir });
  const cols = COLS[T.view];
  const head = `<thead><tr>${cols.map(c => `<th data-k="${c.k}" class="${c.k === key ? "sorted" : ""}">${c.l}${c.k === key ? (dir > 0 ? " ▲" : " ▼") : ""}</th>`).join("")}</tr></thead>`;
  const body = list.map(r => { const dr = {}; (r.draft || []).forEach(x => { if (x.kind !== "todo") dr[x.field] = x });
    return `<tr class="seo-row" data-id="${r.id}">${cols.map(c => {
      const ch = dr[c.k], v = ch ? ch.new : r[c.k];
      let html = c.f ? c.f(v, r) : esc(v ?? "");
      if (c.len){ const n = (v || "").length; html += `<span class="seo-len ${n && (n < c.len[0] || n > c.len[1]) ? "bad" : ""}">${n}</span>` }
      if (c.k === "title" && (r.draft || []).length) html += ` <span class="chip draft">${r.draft.length} in draft</span>`;
      const cl = [c.cls || "", c.n ? "num" : "", ch ? "changed" : "", c.e && T.view === "edit" ? "edit" : ""].join(" ");
      return `<td class="${cl}" data-k="${c.k}" title="${esc(short(v, 300))}">${ch && c.k !== "content" ? `<span class="old">${esc(short(r[c.k], 40))}</span>` : ""}${html}</td>` }).join("")}</tr>` }).join("");
  $("#seoTable").innerHTML = head + `<tbody>${body || `<tr><td class="hint" colspan="${cols.length}">No posts match.</td></tr>`}</tbody>`;
  $("#seoTable thead").onclick = e => { const th = e.target.closest("th"); if (!th) return; const k = th.dataset.k; T.sort = {key: k, dir: T.sort.key === k ? -T.sort.dir : (typeof (rows[0] || {})[k] === "number" ? -1 : 1)}; drawPosts() };
  $("#seoTable tbody").onclick = e => {
    const td = e.target.closest("td"), tr = e.target.closest("tr[data-id]"); if (!tr) return;
    if (td && td.classList.contains("edit") && !td.querySelector("input")) return inlineEdit(td, +tr.dataset.id);
    if (!td || !td.querySelector("input")) openPost(+tr.dataset.id);
  };
}
function inlineEdit(td, id){
  const r = T.data.rows.find(x => x.id === id), k = td.dataset.k, ch = (r.draft || []).find(x => x.field === k && x.kind !== "todo");
  const cur = ch ? ch.new : (r[k] || "");
  td.innerHTML = `<input value="${esc(cur)}">`; const inp = td.querySelector("input"); inp.focus(); inp.select();
  let done = false;
  const save = async () => { if (done) return; done = true;
    if (inp.value !== cur){ try { const res = await P("add", {changes: [{post_id: id, field: k, new: inp.value}]}); T.draft = res.draft; tabs(); toast("Added to the SEO draft") } catch(e) { toast(e.message) } }
    await loadPosts() };
  inp.onkeydown = e => { if (e.key === "Enter") save(); if (e.key === "Escape"){ done = true; drawPosts() } };
  inp.onblur = save;
}

// ---- post dialog ----
let dlgWired = false;
function wide(on){ const d = $("#dlg"); d.classList.toggle("seo-wide", on); if (!dlgWired){ dlgWired = true; d.addEventListener("close", () => d.classList.remove("seo-wide")) } }
async function openPost(id, startPane){
  wide(true); showDialog("Loading post ...", `<p class="hint">Reading the post and its Search Console queries ...</p>`, []);
  let d; try { d = await A("post", {id, ...rq()}) } catch(e) { $("#dlgB").innerHTML = `<div class="prob">${esc(e.message)}</div>`; return }
  if (!T.cats){ try { T.cats = await A("categories") } catch(e) { T.cats = [] } }
  const p = d.post, dr = {}; d.draft.forEach(x => { if (x.kind !== "todo") dr[x.field] = x.new });
  if (window.TeamReviews) TeamReviews.ownerNote([`seo/post/${id}`, "seo"]); // [team reviews] "Owned by ..."
  const val = k => dr[k] !== undefined ? dr[k] : (p[k] ?? "");
  const init = {}; ["title", "slug", "seo_title", "meta_desc", "focus_kw", "excerpt", "content"].forEach(k => init[k] = val(k)); init.categories = [...(dr.categories || p.categories || [])].map(Number).sort((a, b) => a - b);
  const catBoxes = (T.cats || []).map(c => `<label><input type="checkbox" value="${c.id}" ${init.categories.includes(c.id) ? "checked" : ""}>${esc(c.name)}</label>`).join("");
  const host = (p.url || "").replace(/^https?:\/\//, "").replace(/\/$/, "");
  const ins = d.inspection;
  const edit = `<div class="seo-grid2"><div>
      <div class="seo-f"><label>Title</label><input id="sp_title" value="${esc(init.title)}"></div>
      <div class="seo-f"><label>Slug <span class="hint" style="margin:0">masterliveaboards.com/<b>slug</b>/</span></label><input id="sp_slug" value="${esc(init.slug)}"></div>
      <div class="seo-f"><label>SEO title <span class="cnt" id="sp_tn"></span></label><input id="sp_seo_title" value="${esc(init.seo_title)}"></div>
      <div class="seo-f"><label>Meta description <span class="cnt" id="sp_mn"></span></label><textarea id="sp_meta_desc" rows="3">${esc(init.meta_desc)}</textarea></div>
      <div class="seo-f"><label>Focus keyphrase ${p.focus_kw == null ? `<span class="hint" style="margin:0">not shared by Yoast yet: install the Studio snippet</span>` : ""}</label><input id="sp_focus_kw" value="${esc(init.focus_kw || "")}"></div>
    </div><div>
      <div class="seo-f"><label>Google preview</label><div class="seo-snip"><div class="u">${esc(host.replace(/\/[^/]*$/, ""))} › <span id="sp_su">${esc(init.slug)}</span></div><div class="t" id="sp_st"></div><div class="d" id="sp_sd"></div></div></div>
      <div class="seo-f"><label>Excerpt</label><textarea id="sp_excerpt" rows="3">${esc(init.excerpt)}</textarea></div>
      <div class="seo-f"><label>Categories</label><div class="seo-cats" id="sp_cats">${catBoxes || `<span class="hint">Categories could not be read.</span>`}</div></div>
      <div class="seo-f"><label>Featured image</label>${p.image ? `<img class="seo-img" src="${esc(p.image)}" alt="${esc(p.image_alt || "")}">` : `<span class="hint">None</span>`}</div>
    </div></div>
    <div class="seo-f"><label>Body (HTML) <span class="hint" style="margin:0">${p.content_base === "raw" ? "WordPress source" : "Published HTML: once WordPress is connected the editor loads the WordPress source"} · ${N(p.words)} words · ${p.links_out} internal links out, ${p.links_in} in</span></label>
      <div class="seo-grid2"><textarea id="sp_content" class="code">${esc(init.content)}</textarea><iframe id="sp_prev" class="seo-prev" sandbox="" title="Body preview"></iframe></div></div>`;
  const qrows = d.queries.slice(0, 60).map(q => `<tr><td>${esc(q.query)}</td><td class="num">${N(q.clicks)}</td><td class="num">${N(q.impressions)}</td><td class="num">${pct(q.ctr)}</td><td class="num">${q.position.toFixed(1)}</td></tr>`).join("");
  const can = d.cannibalisation.map(c => `<tr><td>${esc(c.query)}</td><td class="num">${N(c.this.impressions)} · ${c.this.position.toFixed(1)}</td><td>${c.others.slice(0, 3).map(o => `${esc(o.title || o.key)} <span class="hint">(${N(o.impressions)} impr., pos. ${o.position.toFixed(1)})</span>`).join("<br>")}</td></tr>`).join("");
  const gsc = `<div class="seo-box"><h3>Index status</h3><div id="sp_ins">${insHTML(ins)}</div>
      <p style="margin:8px 0 0"><button id="sp_inspect">${ins ? "Inspect again" : "Inspect now"}</button> <span class="hint">Uses 1 of ${N(d.quota_left)} URL Inspection calls left today.</span>
      <a href="${esc(d.inspection_link)}" target="_blank" rel="noopener">Open in Search Console</a> <span class="hint">(request indexing there: Google has no API for it)</span></p></div>
    <div class="seo-box"><h3>Queries for this post <span class="hint">${esc(d.range.start)} to ${esc(d.range.end)}</span></h3>
      ${qrows ? `<table><thead><tr><th>Query</th><th>Clicks</th><th>Impr.</th><th>CTR</th><th>Position</th></tr></thead><tbody>${qrows}</tbody></table>` : `<p class="hint">No Search Console queries in this range.</p>`}</div>
    <div class="seo-box"><h3>Competing pages (cannibalisation)</h3><p class="hint">Other pages that get impressions for this post's top queries.</p>
      ${can ? `<table><thead><tr><th>Query</th><th>This post (impr. · pos.)</th><th>Other pages</th></tr></thead><tbody>${can}</tbody></table>` : `<p class="hint">None found.</p>`}</div>`;
  const links = `<div class="seo-box"><h3>Linked from (${d.linked_from.length})</h3>${d.linked_from.map(x => esc(x.title)).join("<br>") || `<p class="hint">No other post links here.</p>`}</div>
    <div class="seo-box"><h3>Related posts that do not link here yet</h3>${d.related.map(x => `<div class="seo-ch" style="grid-template-columns:1fr auto"><div>${esc(x.title)} <span class="hint">shared: ${esc(x.shared.join(", "))}</span></div>
      <button data-rel="${x.id}">Suggest a link</button></div>`).join("") || `<p class="hint">No close matches.</p>`}</div>`;
  const errs = d.errors.length ? `<p class="seo-errs">${d.errors.map(esc).join("<br>")}</p>` : "";
  const inMap = (d.map_nodes || []).length ? `<p class="hint" style="margin:0 0 8px">In the topical map: ${d.map_nodes.map(n => `<b>${esc(n.title)}</b>${n.queries.length ? ` <span class="hint">(${esc(n.queries.slice(0, 4).join(", "))})</span>` : ""}`).join(" · ")}</p>`
    : `<p class="hint" style="margin:0 0 8px">Not in the topical map yet.</p>`;
  showDialog(p.title, `${errs}${inMap}<div class="fpills seo-dtabs" id="sp_tabs"><button data-p="edit">Edit</button><button data-p="gsc">Search Console</button><button data-p="links">Internal links</button>
      <a class="hint" style="margin:4px 0 0 8px" href="${esc(p.url)}" target="_blank" rel="noopener">${esc(p.url)}</a></div>
    <div data-pane="edit">${edit}</div><div data-pane="gsc" style="display:none">${gsc}</div><div data-pane="links" style="display:none">${links}</div>`,
    [["Close", () => dlg.close()], ["Add changes to draft", () => savePost(p, init), "primary"]]);
  const pane = k => { document.querySelectorAll("#sp_tabs [data-p]").forEach(b => b.classList.toggle("on", b.dataset.p === k)); document.querySelectorAll("#dlgB [data-pane]").forEach(x => x.style.display = x.dataset.pane === k ? "" : "none") };
  $("#sp_tabs").onclick = e => { const b = e.target.closest("[data-p]"); if (b) pane(b.dataset.p) }; pane(startPane || "edit");
  const upd = () => {
    const t = $("#sp_seo_title").value, m = $("#sp_meta_desc").value;
    $("#sp_tn").textContent = `${t.length} / 30-60`; $("#sp_tn").classList.toggle("over", t.length < 30 || t.length > 60);
    $("#sp_mn").textContent = `${m.length} / 70-160`; $("#sp_mn").classList.toggle("over", m.length < 70 || m.length > 160);
    $("#sp_st").textContent = t.length > 60 ? t.slice(0, 58) + " ..." : (t || $("#sp_title").value);
    $("#sp_sd").textContent = m ? (m.length > 160 ? m.slice(0, 157) + " ..." : m) : "No meta description: Google picks text from the page.";
    $("#sp_su").textContent = $("#sp_slug").value;
  };
  ["sp_seo_title", "sp_meta_desc", "sp_title", "sp_slug"].forEach(i => $("#" + i).oninput = upd); upd();
  let pt; const prev = () => { $("#sp_prev").srcdoc = `<base target="_blank"><style>body{font:15px/1.6 Arial,sans-serif;margin:14px;color:#222}img{max-width:100%;height:auto}</style>` + $("#sp_content").value };
  $("#sp_content").oninput = () => { clearTimeout(pt); pt = setTimeout(prev, 400) }; prev();
  $("#sp_inspect").onclick = async () => { $("#sp_inspect").disabled = true; $("#sp_ins").innerHTML = `<span class="hint">Asking Search Console ...</span>`;
    try { const r = await P("inspect", {id: p.id, force: !!ins}); $("#sp_ins").innerHTML = insHTML(r); if (T.data){ const row = T.data.rows.find(x => x.id === p.id); if (row){ row.indexed = r.verdict === "PASS"; row.index = r.coverage } } }
    catch(e) { $("#sp_ins").innerHTML = `<div class="prob">${esc(e.message)}</div>` } $("#sp_inspect").disabled = false };
  document.querySelectorAll("#dlgB [data-rel]").forEach(b => b.onclick = async () => {
    try { await P("add", {changes: [{post_id: +b.dataset.rel, field: "content", kind: "todo", note: `Add an internal link to "${p.title}" (${p.url}).`}]}); b.disabled = true; b.textContent = "In draft"; loadDraft() } catch(e) { toast(e.message) } });
}
function insHTML(r){
  if (!r) return `<span class="hint">Not checked yet.</span>`;
  return `<div class="seo-kv"><b>Verdict</b><span><span class="seo-dot ${r.verdict === "PASS" ? "ok" : "bad"}"></span>${esc(r.coverage || r.verdict || "")}</span>
    <b>Last crawl</b><span>${esc((r.last_crawl || "").replace("T", " ").replace("Z", ""))}</span><b>Google canonical</b><span>${esc(r.google_canonical || "")}</span>
    <b>Robots / fetch</b><span>${esc(r.robots || "")} · ${esc(r.fetch || "")}</span><b>Checked</b><span>${esc(new Date(r.at * 1000).toLocaleString("en-GB"))}</span></div>`;
}
async function savePost(p, init){
  const now = {title: $("#sp_title").value, slug: $("#sp_slug").value.trim(), seo_title: $("#sp_seo_title").value, meta_desc: $("#sp_meta_desc").value,
               focus_kw: $("#sp_focus_kw").value, excerpt: $("#sp_excerpt").value, content: $("#sp_content").value,
               categories: [...document.querySelectorAll("#sp_cats input:checked")].map(i => +i.value).sort((a, b) => a - b)};
  if (!document.querySelector("#sp_cats input")) delete now.categories;
  const changes = Object.entries(now).filter(([k, v]) => JSON.stringify(v) !== JSON.stringify(k === "categories" ? init.categories : (init[k] ?? ""))).map(([k, v]) => ({post_id: p.id, field: k, new: v}));
  if (!changes.length){ toast("No changes"); return }
  try {
    const r = await P("add", {changes}); T.draft = r.draft; tabs();
    const iss = r.results.flatMap(x => x.issues || []).filter(i => i.level !== "info");
    dlg.close(); toast(`${changes.length} change${changes.length > 1 ? "s" : ""} added to the SEO draft${iss.length ? `, ${iss.length} to check` : ""}`);
    if (T.tab === "posts") loadPosts();
  } catch(e) { toast(e.message) }
}

// ---- new page editor (a page for a topical-map node; Claude writes it with seo_write_page, you edit it here) ----
async function openNewPage(o){
  // o: {id} for one in the draft, or {node} to start one from a map node
  if (!T.cats){ try { T.cats = await A("categories") } catch(e) { T.cats = [] } }
  let np = null;
  if (o.id){ try { T.draft = await A("draft") } catch(e) {} np = ((T.draft || {}).new_posts || []).find(x => x.id === o.id); if (!np) return toast("That new page is no longer in the draft") }
  const n = o.node || {};
  const v = np || {node_id: n.id || "", title: n.title || "", slug: ((n.url || "").replace(/^https?:\/\/[^/]+/, "").split("/").filter(Boolean).pop() || ""), seo_title: "", meta_desc: "", focus_kw: ((n.queries || [])[0] || {}).query || "", excerpt: "", categories: [], content: "", status: "publish"};
  wide(true);
  const cats = (T.cats || []).map(c => `<label><input type="checkbox" value="${c.id}" ${(v.categories || []).includes(c.id) ? "checked" : ""}>${esc(c.name)}</label>`).join("");
  const qs_ = (n.queries || []).map(q => q.query || q);
  showDialog(np ? "New page" : "Write a new page", `<div id="np_wrap"><p class="hint" style="margin:0 0 8px">${v.node_id ? `For the map node <b>${esc(np ? np.node_title : n.title)}</b>. ` : ""}Saved to the SEO draft as you type. Nothing goes to WordPress until you press Publish.${np ? ` Written by ${esc(np.by)}.` : ""}${np && np.note ? `<br>Note: ${esc(np.note)}` : ""}</p>
    ${qs_.length ? `<p class="hint" style="margin:0 0 8px">Target queries: ${esc(qs_.join(", "))}</p>` : ""}
    <div class="seo-grid2"><div>
      <div class="seo-f"><label>Title</label><input id="np_title" value="${esc(v.title)}"></div>
      <div class="seo-f"><label>Slug <span class="hint" style="margin:0">masterliveaboards.com/<b>slug</b>/ (empty = from the title)</span></label><input id="np_slug" value="${esc(v.slug)}"></div>
      <div class="seo-f"><label>SEO title <span class="cnt" id="np_tn"></span></label><input id="np_seo_title" value="${esc(v.seo_title)}"></div>
      <div class="seo-f"><label>Meta description <span class="cnt" id="np_mn"></span></label><textarea id="np_meta_desc" rows="3">${esc(v.meta_desc)}</textarea></div>
      <div class="seo-f"><label>Focus keyphrase</label><input id="np_focus_kw" value="${esc(v.focus_kw)}"></div>
    </div><div>
      <div class="seo-f"><label>Google preview</label><div class="seo-snip"><div class="u">masterliveaboards.com › <span id="np_su"></span></div><div class="t" id="np_st"></div><div class="d" id="np_sd"></div></div></div>
      <div class="seo-f"><label>Excerpt</label><textarea id="np_excerpt" rows="2">${esc(v.excerpt)}</textarea></div>
      <div class="seo-f"><label>Categories</label><div class="seo-cats" id="np_cats">${cats || `<span class="hint">Categories could not be read.</span>`}</div></div>
      <div class="seo-f"><label>On Publish</label><select id="np_status"><option value="publish" ${v.status !== "draft" ? "selected" : ""}>Publish it live</option><option value="draft" ${v.status === "draft" ? "selected" : ""}>Save as a WordPress draft</option></select></div>
    </div></div>
    <div class="seo-f"><label>Body (HTML) <span class="hint" style="margin:0" id="np_wc"></span></label>
      <div class="seo-grid2"><textarea id="np_content" class="code">${esc(v.content)}</textarea><iframe id="np_prev" class="seo-prev" sandbox="" title="Body preview"></iframe></div></div>
    <div id="np_iss"></div><span class="hint" id="np_saved"></span></div>`,
    [["Close", async () => { await save(); dlg.close() }], ...(np ? [["Remove", async () => { if (!confirm("Remove this new page from the draft?")) return; T.draft = await P("new_post_remove", {id: np.id}); dlg.close(); tabs(); if (T.tab === "draft") drawDraft(); if (window.SEO_MAP) window.SEO_MAP.reload() }, "danger"]] : []),
     ["Done", async () => { await save(true); dlg.close() }, "primary"]]);
  let id = np && np.id, timer = null, last = "";
  const vals = () => ({node_id: v.node_id, title: $("#np_title").value, slug: $("#np_slug").value.trim(), seo_title: $("#np_seo_title").value, meta_desc: $("#np_meta_desc").value,
    focus_kw: $("#np_focus_kw").value, excerpt: $("#np_excerpt").value, content: $("#np_content").value, status: $("#np_status").value,
    categories: [...document.querySelectorAll("#np_cats input:checked")].map(i => +i.value)});
  async function save(final){
    clearTimeout(timer);
    const b = vals(), key = JSON.stringify(b);
    if (key === last) return;
    if (!id && !b.content.trim() && !final) return; // nothing worth keeping yet
    if (!b.title.trim()){ $("#np_saved").textContent = "Needs a title before it can be saved."; return }
    try { const r = await P("new_post_save", id ? {...b, id} : b); id = r.new_post.id; last = key;
      $("#np_saved").textContent = "Saved to the draft " + new Date().toLocaleTimeString("en-GB", {hour: "2-digit", minute: "2-digit", second: "2-digit"});
      $("#np_iss").innerHTML = issHTML(r.new_post.issues); loadDraft(); if (window.SEO_MAP && final) window.SEO_MAP.reload() }
    catch(e) { $("#np_saved").textContent = e.message }
  }
  const upd = () => {
    const t = $("#np_seo_title").value, m = $("#np_meta_desc").value;
    $("#np_tn").textContent = `${t.length} / 30-60`; $("#np_tn").classList.toggle("over", t.length < 30 || t.length > 60);
    $("#np_mn").textContent = `${m.length} / 70-160`; $("#np_mn").classList.toggle("over", m.length < 70 || m.length > 160);
    $("#np_st").textContent = t ? (t.length > 60 ? t.slice(0, 58) + " ..." : t) : $("#np_title").value;
    $("#np_sd").textContent = m || "No meta description: Google picks text from the page.";
    $("#np_su").textContent = $("#np_slug").value || slugOf($("#np_title").value);
    const words = ($("#np_content").value.replace(/<[^>]+>/g, " ").match(/\S+/g) || []).length; $("#np_wc").textContent = `${N(words)} words`;
  };
  let pt; const prev = () => { $("#np_prev").srcdoc = `<base target="_blank"><style>body{font:15px/1.6 Arial,sans-serif;margin:14px;color:#222}img{max-width:100%;height:auto}</style>` + $("#np_content").value };
  $("#np_wrap").addEventListener("input", e => { upd(); if (e.target.id === "np_content"){ clearTimeout(pt); pt = setTimeout(prev, 400) } clearTimeout(timer); timer = setTimeout(save, 900) });
  $("#np_wrap").addEventListener("change", () => { clearTimeout(timer); timer = setTimeout(save, 300) });
  upd(); prev(); if (np){ last = JSON.stringify(vals()); $("#np_iss").innerHTML = issHTML(np.issues) }
}
const slugOf = s => (s || "").normalize("NFKD").replace(/[̀-ͯ]/g, "").toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "").slice(0, 80);

// ---- opportunities ----
async function loadOpps(){
  const box = $("#seoOpp"); if (!box) return;
  try { T.opp = await A("opportunities", rq()) } catch(e) { box.innerHTML = `<div class="prob">${esc(e.message)}</div>`; return }
  drawOpps();
}
const sugBtn = (s, label) => s ? `<button data-sug='${esc(JSON.stringify(s))}'>${label || "Add a suggestion to the draft"}</button>` : `<span class="hint">Not a blog post</span>`;
function drawOpps(){
  const o = T.opp, box = $("#seoOpp"); if (!o || !box) return;
  const tbl = (head, rows) => rows ? `<div class="grid-wrap" style="max-height:420px;border:1px solid var(--line);border-radius:6px"><table><thead><tr>${head.map(h => `<th>${h}</th>`).join("")}</tr></thead><tbody>${rows}</tbody></table></div>` : `<p class="hint">Nothing found.</p>`;
  const nm = r => r.title ? esc(r.title) : `<span class="hint">${esc(r.key)}</span>`;
  box.innerHTML = `${o.errors.length ? `<p class="seo-errs">${o.errors.map(esc).join("<br>")}</p>` : ""}
    <p class="hint">${esc(o.range.start)} to ${esc(o.range.end)}, compared with ${esc(o.range.prev_start)} to ${esc(o.range.prev_end)}. Suggestions go into the SEO draft as notes for you to write; nothing goes to WordPress.</p>
    <div class="seo-sec"><h2>Many impressions, few clicks (position 3-10)</h2><p class="hint">CTR under 60% of what that position usually gets. Missed = impressions x (usual CTR - CTR).</p>
    ${tbl(["Query", "Page", "Impr.", "Clicks", "CTR", "Position", "Missed", ""], o.low_ctr.map(r => `<tr><td>${esc(r.query)}</td><td>${nm(r)}</td><td class="num">${N(r.impressions)}</td><td class="num">${N(r.clicks)}</td><td class="num">${pct(r.ctr)}</td><td class="num">${r.position.toFixed(1)}</td><td class="num">${N(r.missed)}</td><td>${sugBtn(r.suggest)}</td></tr>`).join(""))}</div>
    <div class="seo-sec"><h2>Losing clicks</h2><p class="hint">At least 10 clicks before, and down 30% or more.</p>
    ${tbl(["Page", "Clicks", "Before", "Change", "Position", "Before", ""], o.losing.map(r => `<tr><td>${nm(r)}</td><td class="num">${N(r.clicks)}</td><td class="num">${N(r.prev_clicks)}</td><td class="num seo-down">${N(r.delta)}</td><td class="num">${r.position == null ? "" : r.position.toFixed(1)}</td><td class="num">${r.prev_position == null ? "" : r.prev_position.toFixed(1)}</td><td>${sugBtn(r.suggest)}</td></tr>`).join(""))}</div>
    <div class="seo-sec"><h2>Not indexed</h2><p class="hint">From URL Inspection results Studio has cached. ${o.not_inspected} published posts not checked yet.
      <button id="seoInspAll">Check index status for unchecked posts</button></p>
    ${tbl(["Post", "Coverage", "Last crawl", ""], o.not_indexed.map(r => `<tr><td>${esc(r.title)}</td><td>${esc(r.coverage || "")}</td><td>${esc(day(r.last_crawl))}</td><td><a href="${esc(r.link)}" target="_blank" rel="noopener">Open in Search Console</a></td></tr>`).join(""))}</div>
    <div class="seo-sec"><h2>No meta description</h2>
    ${tbl(["Post", "Impr.", ""], o.no_meta.map(r => `<tr><td>${esc(r.title)}</td><td class="num">${N(r.impressions)}</td><td>${sugBtn(r.suggest)}</td></tr>`).join(""))}</div>
    <div class="seo-sec"><h2>No internal links in</h2><p class="hint">Posts no other post links to, with related posts that could link to them.</p>
    ${tbl(["Post", "Could be linked from", ""], o.few_links.map(r => `<tr><td>${esc(r.title)}</td><td class="wrap">${r.from.map(x => `${esc(x.title)} <span class="hint">(${esc(x.shared.join(", "))})</span>`).join("<br>") || `<span class="hint">No close match</span>`}</td><td>${r.suggest.length ? `<button data-sugs='${esc(JSON.stringify(r.suggest))}'>Add link suggestions</button>` : ""}</td></tr>`).join(""))}</div>`;
  box.querySelectorAll("[data-sug],[data-sugs]").forEach(b => b.onclick = async () => {
    const list = b.dataset.sug ? [JSON.parse(b.dataset.sug)] : JSON.parse(b.dataset.sugs);
    try { await P("add", {changes: list.map(s => ({...s, kind: "todo"}))}); b.disabled = true; b.textContent = "In draft"; loadDraft() } catch(e) { toast(e.message) } });
  $("#seoInspAll").onclick = async () => {
    try { const r = await seoProgress("Checking index status", "inspect", {all: true}); const list = await lastProgress("/api/progress?key=seo");
      showDialog("Index status checked", `${list}<p>${r.checked} checked, ${r.not_indexed} not indexed, ${r.failed} failed. ${N(r.quota_left)} calls left today.</p>`, []); loadOpps() } catch(e) { toast(e.message) } };
}

// ---- draft ----
function issHTML(list){ return (list || []).map(i => `<span class="seo-iss ${i.level}">${i.level === "error" ? "Error: " : i.level === "warn" ? "Check: " : ""}${esc(i.msg)}</span>`).join("") }
function drawDraft(){
  const box = $("#seoDraft"); if (!box) return;
  const ch = (T.draft || {}).changes || [];
  const by = {}; ch.forEach(x => (by[x.post_id] = by[x.post_id] || []).push(x));
  const nps = (T.draft || {}).new_posts || [];
  const newHTML = nps.length ? `<div class="seo-sec" style="margin:10px 0 18px"><h2>New pages (${nps.length})</h2>${nps.map(x => `<div class="seo-ch" style="grid-template-columns:1fr auto"><div><b>${esc(x.title)}</b> <span class="chip draft">${x.status === "draft" ? "WordPress draft" : "Publishes live"}</span>
      <div class="hint" style="margin:2px 0 0">/${esc(x.slug)}/ · ${x.node_title ? "map: " + esc(x.node_title) + " · " : ""}written by ${esc(x.by)}${x.edited_by && x.edited_by !== x.by ? ", edited by " + esc(x.edited_by) : ""} · ${esc(x.edited_at || x.at)}</div>${x.note ? `<div class="hint">${esc(x.note)}</div>` : ""}${issHTML(x.issues)}</div>
      <div><button data-np="${x.id}" class="primary">Edit</button> <button class="link" data-nprm="${x.id}">Remove</button></div></div>`).join("")}</div>` : "";
  box.innerHTML = `<div style="display:flex;gap:8px;align-items:center;margin-bottom:6px"><b>${ch.length} change${ch.length === 1 ? "" : "s"}${nps.length ? ` and ${nps.length} new page${nps.length === 1 ? "" : "s"}` : ""} in the SEO draft</b><span class="spacer"></span>
      <button id="sdClear" class="danger">Clear</button><button id="sdCheck">Check</button><button id="sdPub" class="primary">Publish ...</button></div>
    <p class="hint">Nothing here is in WordPress. Suggestions (notes) stay in the draft until you write the text in the post editor. Only your Publish button sends edits.</p>
    ${newHTML}${Object.values(by).map(xs => `<div class="seo-post-h">${esc(xs[0].post_title)} <button class="link" data-open="${xs[0].post_id}">Open post</button></div>
      ${xs.map(x => `<div class="seo-ch"><div>${esc(x.label || LBL[x.field] || x.field)}${x.kind === "todo" ? ` <span class="chip warn">Suggestion</span>` : ""}<div class="hint" style="margin:2px 0 0">by ${esc(x.by)} · ${esc(x.at)}</div></div>
        <div>${x.kind === "todo" ? esc(x.note) : `<span class="old">${esc(short(x.old, 300))}</span><br><span class="new">${esc(short(x.new, 300))}</span>`}${x.note && x.kind !== "todo" ? `<div class="hint">${esc(x.note)}</div>` : ""}${issHTML(x.issues)}</div>
        <button class="link" data-rm="${x.id}" title="Remove from the draft">Remove</button></div>`).join("")}`).join("") || `${nps.length ? "" : `<p class="hint">The draft is empty. Open a post or a topical-map node and edit it, or ask Claude to write a page (MCP: seo_write_page).</p>`}`}
    <div class="seo-sec" style="margin-top:26px"><h2>Published from Studio</h2><div id="sdHist"><p class="hint">Loading ...</p></div></div>`;
  box.querySelectorAll("[data-rm]").forEach(b => b.onclick = async () => { T.draft = await P("remove", {ids: [b.dataset.rm]}); drawDraft(); tabs(); loadHistory() });
  box.querySelectorAll("[data-open]").forEach(b => b.onclick = () => openPost(+b.dataset.open));
  box.querySelectorAll("[data-np]").forEach(b => b.onclick = () => openNewPage({id: b.dataset.np}));
  box.querySelectorAll("[data-nprm]").forEach(b => b.onclick = () => { const x = nps.find(y => y.id === b.dataset.nprm);
    showDialog("Remove this new page?", `<p>"${esc(x.title)}" is removed from the draft. It was never in WordPress.</p>`, [["Cancel", () => dlg.close()], ["Remove", async () => { T.draft = await P("new_post_remove", {id: x.id}); dlg.close(); drawDraft(); tabs() }, "danger"]]) });
  $("#sdClear").onclick = () => showDialog("Clear the SEO draft?", `<p>This removes all ${ch.length} changes and suggestions${nps.length ? ` and ${nps.length} new page${nps.length === 1 ? "" : "s"}` : ""}. WordPress is not touched.</p>`,
    [["Cancel", () => dlg.close()], ["Clear", async () => { T.draft = await P("clear"); dlg.close(); drawDraft(); tabs(); loadHistory() }, "danger"]]);
  $("#sdCheck").onclick = async () => { $("#sdCheck").disabled = true; $("#sdCheck").textContent = "Checking links ...";
    try { const r = await P("check"); T.draft = {...T.draft, changes: r.changes}; drawDraft(); loadHistory(); toast("Checked, links included") } catch(e) { toast(e.message) } };
  $("#sdPub").onclick = publishFlow;
}
async function loadHistory(){
  const box = $("#sdHist"); if (!box) return;
  let h = []; try { h = await A("history") } catch(e) {}
  box.innerHTML = h.length ? h.map(p => `<div class="seo-ch" style="grid-template-columns:160px 1fr auto"><div>${esc(p.at)}<div class="hint" style="margin:0">${p.writer === "wp" ? "WordPress" : "Dry run"} · ${esc(p.target)}</div></div>
      <div>${p.posts.map(x => `${x.new ? `<span class="chip draft">New</span> ` : ""}${esc(x.title)} <span class="hint">(${esc(x.fields.map(f => LBL[f] || f).join(", "))})</span>`).join("<br>")}</div>
      <div>${p.undone ? `<span class="chip">Undone</span>` : `<button class="danger" data-undo="${esc(p.push_id)}">Undo</button>`}</div></div>`).join("") : `<p class="hint">Nothing published yet.</p>`;
  box.querySelectorAll("[data-undo]").forEach(b => b.onclick = () => undoFlow(b.dataset.undo));
}

// ---- progress (same look as Push live, with the step name per post) ----
function progHTML(p, title){
  const items = p.items || [];
  const w = x => Math.round(100 * (x.state === "done" || x.state === "reversed" ? 1 : x.sent / x.total));
  const step = x => x.state === "done" ? "Done" : x.state === "failed" ? "Failed" : x.state === "reversed" ? "Restored" : x.state === "waiting" ? "Waiting"
    : p.kind === "publish" ? (STEPS[x.sent] || "Done") : p.kind === "undo" ? "Restoring" : "Checking";
  const n = items.filter(x => ["done", "reversed"].includes(x.state)).length;
  const head = p.running ? `<span class="spin"></span>${esc(p.message || title)}` : p.ok ? `<span class="done">Done</span> ${esc(p.message || "")}` : `<span class="bad">Stopped</span> ${esc(p.message || "")}`;
  return `<div class="prog-h">${head}</div><p class="hint" style="margin:0 0 6px">${n} of ${items.length} done · ${Math.round(p.elapsed || 0)}s${p.kind === "publish" ? " · steps: " + STEPS.join(", ") : ""}</p>`
    + items.map(x => `<div class="prog-row ${x.state} ${p.running && ["checking", "sending"].includes(x.state) ? "busy" : ""}"><div>${esc(x.summary)}</div>
      <div class="bar"><i style="width:${w(x)}%"></i></div><div class="st">${step(x)}</div></div>`).join("");
}
async function seoProgress(title, key, body){
  showDialog(title, `<div class="prog-h"><span class="spin"></span>Starting ...</div>`, []);
  let stop = false;
  const tick = async () => { if (stop) return;
    try { const p = await api("/api/progress?key=seo"); if (!stop && p.items && p.items.length) $("#dlgB").innerHTML = progHTML(p, title) } catch(e) {}
    if (!stop) setTimeout(tick, 400) };
  setTimeout(tick, 150);
  try { return await P(key, body) } finally { stop = true }
}
async function finalProgress(){ try { return progHTML(await api("/api/progress?key=seo"), "") } catch(e) { return "" } }

// ---- publish + undo (the user's own buttons) ----
async function publishFlow(){
  wide(true);
  showDialog("Publish the SEO draft", `<p class="hint">Building the change set and checking links ...</p>`, []);
  const TR = window.TeamReviews; // [team reviews] approvals, only mine / all approved, why note
  let pv; try { pv = await P("publish_preview", TR ? TR.extra() : {}) } catch(e) { $("#dlgB").innerHTML = `<div class="prob">${esc(e.message)}</div>`; return }
  const prod = pv.target === "prod" && pv.live;
  const where = pv.live ? `<span class="seo-target ${prod ? "prod" : ""}">${prod ? "PROD: live WordPress" : "Staging WordPress"}</span>`
    : `<span class="seo-target dry">Dry run</span> Nothing is sent to WordPress: Studio records what it would send (${esc(pv.target)}). Connect WordPress in Settings to publish for real.`;
  const body = pv.posts.map(p => `<div class="seo-post-h">${p.new ? `<span class="chip draft">New page${p.status === "draft" ? ", WordPress draft" : ""}</span> ` : ""}${esc(p.title)} <a class="hint" href="${esc(p.url)}" target="_blank" rel="noopener">${esc(p.url)}</a></div>
    ${p.fields.map(f => `<div class="seo-ch" style="grid-template-columns:150px 1fr"><div>${esc(f.label)}</div><div><span class="old">${esc(short(f.old, 400))}</span><br><span class="new">${esc(short(f.new, 400))}</span></div></div>`).join("")}
    ${issHTML(p.issues)}`).join("");
  showDialog("Publish the SEO draft", `<p>${where}</p>
    <p class="hint">${pv.posts.length} post${pv.posts.length === 1 ? "" : "s"}${pv.new_pages ? ` (${pv.new_pages} new)` : ""} · ${pv.errors} error${pv.errors === 1 ? "" : "s"} · ${pv.warnings} to check${pv.todos ? ` · ${pv.todos} suggestion${pv.todos === 1 ? "" : "s"} stay in the draft` : ""}. Per post: ${STEPS.join(", ")}.</p>
    ${body || `<p class="hint">No edits to publish. Suggestions need text first.</p>`}${pv.errors ? `<p class="prob">Fix the errors before publishing.</p>` : ""}${TR ? TR.section(pv.team) : ""}`,
    [["Cancel", () => dlg.close()], [pv.live ? (prod ? "Publish to prod" : "Publish to staging") : "Publish (dry run)", () => runPublish(pv), prod ? "live" : "primary"]]);
  const btn = [...document.querySelectorAll("#dlgF button")].pop();
  const upd = () => { btn.disabled = pv.blocked || (TR && !TR.ready(pv.team)) }; upd();
  if (TR) TR.wire($("#dlgB"), publishFlow, upd);
}
async function runPublish(pv){
  try {
    const r = await seoProgress(pv.live ? "Publishing to WordPress " + pv.target : "Publishing (dry run)", "publish", {token: pv.token, ...(window.TeamReviews ? TeamReviews.extra() : {})});
    const list = await finalProgress();
    const res = r.results.map(x => `<div class="seo-ch" style="grid-template-columns:1fr auto"><div>${esc(x.title)}${x.error ? `<span class="seo-iss error">${esc(x.error)}</span>` : `<span class="seo-steps"><br>${esc(x.steps.join(" · "))} · ${esc(x.inspection || "")}</span>`}</div>
      <div>${x.ok ? `${x.new && x.url ? `<a href="${esc(x.url)}" target="_blank" rel="noopener">Open the new page</a> · ` : ""}${x.request_indexing ? `<a href="${esc(x.request_indexing)}" target="_blank" rel="noopener">Request indexing in Search Console</a>` : ""}` : ""}</div></div>`).join("");
    showDialog(r.ok ? (r.live ? "Published" : "Dry run done") : "Not all published", `${list}<p>${esc(r.message)}</p>${res}`,
      [["Close", () => dlg.close()], ["Undo this publish", () => undoFlow(r.push_id), "danger"]]);
  } catch(e) { toast(e.message) }
  loadDraft(); if (T.tab === "posts") loadPosts(); if (T.tab === "draft") loadHistory(); if (T.tab === "map" && window.SEO_MAP) window.SEO_MAP.reload();
}
function undoFlow(pushId){
  showDialog("Undo this publish?", `<p>Studio puts back what each post had before this publish: the WordPress revision it noted, or, if WordPress keeps no revisions, the values Studio saved. New pages Studio created go to the WordPress bin (restorable there). If a post was edited in WordPress since, undo stops and changes nothing.</p>`,
    [["Cancel", () => dlg.close()], ["Undo", async () => { try { const r = await seoProgress("Undoing", "undo", {push_id: pushId}); const list = await finalProgress();
      showDialog(r.ok ? "Undone" : "Undo stopped", `${list}<p>${esc(r.message)}</p>`, []) } catch(e) { toast(e.message) } if (T.tab === "draft") loadHistory() }, "danger"]]);
}

// ---- settings ----
async function drawSettings(){
  const box = $("#seoSet"); if (!box) return;
  await loadSettings(); const s = T.settings;
  let sm = null; try { sm = await A("sitemaps") } catch(e) { sm = {error: e.message} }
  const sc = s.scopes || {};
  const yes = (ok, t, f) => `<span class="seo-dot ${ok ? "ok" : "bad"}"></span>${ok ? t : f}`;
  box.innerHTML = `<div class="seo-box"><h3>Sites</h3>${(s.sites || []).map(x => `<div class="seo-kv"><b>${esc(x.name)}</b><span>${x.on ? yes(true, "On") : `<span class="seo-dot"></span>${esc(x.note)}`} <span class="hint">${x.on ? esc(x.note) : ""}</span></span></div>`).join("")}</div>
    <div class="seo-box"><h3>WordPress (masterliveaboards.com)</h3><div class="seo-kv">
      <b>Reading</b><span>${yes(true, "Public REST API, published posts, no login")}</span>
      <b>Writing</b><span>${s.connected ? yes(true, "Connected as " + esc(s.user)) : `<span class="seo-dot"></span>Not connected`} <button id="ssConn">${s.connected ? "Change login" : "Connect ..."}</button>${s.connected ? ` <button id="ssDisc" class="danger">Disconnect</button>` : ""}</span>
      <b>Publish goes to</b><span><label><input type="radio" name="ssTarget" value="staging" ${s.target !== "prod" ? "checked" : ""}> Staging (staging.masterliveaboards.com)</label>
        <label style="margin-left:14px;color:var(--bad)"><input type="radio" name="ssTarget" value="prod" ${s.target === "prod" ? "checked" : ""}> Prod (live site)</label></span>
      <b>Publish mode</b><span><label><input type="radio" name="ssMode" value="dry" ${s.mode !== "live" ? "checked" : ""}> Dry run (records only)</label>
        <label style="margin-left:14px"><input type="radio" name="ssMode" value="live" ${s.mode === "live" ? "checked" : ""} ${s.connected ? "" : "disabled"}> WordPress REST</label>${s.connected ? "" : ` <span class="hint">needs the login</span>`}</span>
      <b>Yoast fields over REST</b><span>${s.snippet_installed ? yes(true, "Snippet installed: focus keyphrase is shared") : `<span class="seo-dot"></span>Snippet not installed: SEO title, meta and focus keyphrase can't be written yet. See the README (Setup).`}</span>
      <b>Secrets file</b><span><code>${esc(s.secrets_file)}</code> <span class="hint">(chmod 600, password never shown)</span></span></div></div>
    <div class="seo-box"><h3>Search Console + GA4</h3><div class="seo-kv">
      <b>Search Analytics</b><span>${yes(sc.search_console_read, "Readable (sc-domain:masterliveaboards.com)", "No access")}</span>
      <b>URL Inspection</b><span>${yes(sc.url_inspection, "Works with the read-only scope", "Needs the full webmasters scope")} · ${N(s.quota_left)} calls left today</span>
      <b>GA4</b><span>${yes(sc.ga4_read, "Readable (property 264228970, organic sessions + select_cabin)", "No access")}</span>
      <b>Request indexing</b><span><span class="seo-dot"></span>No API for normal pages: Studio links to Search Console's URL inspection page.</span>
      <b>Sitemaps</b><span>${sm.error ? esc(sm.error) : (sm.sitemaps || []).map(x => `${esc(x.path)} <span class="hint">(last read ${esc(day(x.lastDownloaded))}${x.errors && +x.errors ? ", " + x.errors + " errors" : ""})</span>`).join("<br>") || "None"}
        <br><button disabled title="${esc(sm.note || "")}">Submit sitemap</button> <span class="hint">${sm.can_submit ? "" : "Needs the full webmasters scope: connect it in the dashboard's Google login first."}</span></span>
    </div></div>`;
  box.querySelectorAll("[name=ssTarget],[name=ssMode]").forEach(r => r.onchange = async () => {
    try { T.settings = await P("settings", {target: box.querySelector("[name=ssTarget]:checked").value, mode: box.querySelector("[name=ssMode]:checked").value}); targetChip(); toast("Saved") } catch(e) { toast(e.message); drawSettings() } });
  $("#ssConn").onclick = connectDialog;
  if ($("#ssDisc")) $("#ssDisc").onclick = async () => { T.settings = await P("disconnect"); targetChip(); drawSettings() };
}
function connectDialog(){
  wide(false);
  showDialog("Connect WordPress", `<p class="hint">Use the dedicated "Marketing Studio" user (Editor role) and an Application Password made for it (Users > Profile > Application Passwords). Studio tests the login, then saves it in ~/.claude/secrets/marketing-studio.env (chmod 600). It is never shown again.</p>
    <div class="row"><label>Site</label><select id="cw_t"><option value="staging">Staging</option><option value="prod">Prod</option></select></div>
    <div class="row"><label>User name</label><input id="cw_u" autocomplete="off" value="${esc((T.settings || {}).user || "")}"></div>
    <div class="row"><label>Application password</label><input id="cw_p" type="password" autocomplete="new-password"></div>`,
    [["Cancel", () => dlg.close()], ["Test and save", async () => {
      const b = [...document.querySelectorAll("#dlgF button")].pop(); b.disabled = true;
      try { const r = await P("connect", {target: $("#cw_t").value, user: $("#cw_u").value, password: $("#cw_p").value}); $("#cw_p").value = ""; dlg.close(); toast(r.message); drawSettings() }
      catch(e) { $("#cw_p").value = ""; toast(e.message); b.disabled = false } }, "primary"]]);
  $("#cw_t").value = (T.settings || {}).target || "staging";
}

window.AREAS = window.AREAS || {};
window.AREAS.seo = {label: "SEO", render};
})();
