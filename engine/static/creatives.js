// Marketing Studio - Creatives (part of Ads): asset and creative performance per account for a date range, the same
// asset added up across ads. Read-only towards Google and Meta; row actions only go into the Ads draft (Push live stays
// the user's button). Every text, name and link here comes from the platforms, so all of it is escaped.
(() => {
const STYLE = `
.cr-scroll{overflow:auto;flex:1;padding:12px 18px;min-height:0}
.cr-bar{display:flex;gap:8px;align-items:center;flex-wrap:wrap}
.cr-bar select,.cr-bar input{font-size:13px}
.cr-bar input[type=number]{width:70px}
.cr-note{font-size:12px;color:var(--muted);line-height:1.45;margin:0 0 10px}
.cr-tot{display:flex;gap:18px;flex-wrap:wrap;font-size:13px;margin:0 0 10px;color:var(--text2)}
.cr-tot b{font-weight:500;color:var(--text);font-variant-numeric:tabular-nums}
.cr-table{width:100%;border-collapse:collapse;font-size:13px}
.cr-table th,.cr-table td{padding:5px 8px;border-top:1px solid var(--line2);text-align:left;vertical-align:top}
.cr-table th{font-weight:500;color:var(--text2);background:var(--head);position:sticky;top:0;cursor:pointer;white-space:nowrap;user-select:none}
.cr-table th.on{color:var(--text)}
.cr-table td.num,.cr-table th.num{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}
.cr-table tr.low td{color:var(--muted)}
.cr-table tr.cr-r{cursor:pointer}
.cr-table tr.cr-r:hover td{background:var(--hover,rgba(0,0,0,.03))}
.cr-text{min-width:220px;max-width:460px;white-space:pre-wrap;word-break:break-word}
.cr-sub{font-size:11px;color:var(--muted);margin-top:2px}
.cr-prev{width:72px}
.cr-prev img{width:64px;height:64px;object-fit:cover;border-radius:4px;border:1px solid var(--line2);display:block;background:var(--line2)}
.cr-chip{display:inline-block;font-size:11px;padding:0 6px;border-radius:8px;background:var(--warn-bg);color:var(--text);margin-left:4px;white-space:nowrap}
.cr-acts{display:flex;gap:4px;flex-wrap:wrap}
.cr-acts button{font-size:12px;padding:2px 8px}
.cr-mem td{background:var(--panel);font-size:12px}
.cr-mem table{width:100%;border-collapse:collapse}
.cr-mem th,.cr-mem td{padding:3px 6px;border-top:1px solid var(--line2);text-align:left}
.cr-mem th{font-weight:500;color:var(--text2);position:static;cursor:default;background:none}
`;
if (!document.getElementById("cr-css")){ const st = document.createElement("style"); st.id = "cr-css"; st.textContent = STYLE; document.head.appendChild(st) }

const h = s => String(s ?? "").replace(/[&<>"']/g, c => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[c]));
const safeUrl = u => /^https:\/\//.test(String(u || "")) ? String(u) : "";
const num = (v, d) => v == null || v === "" ? "" : Number(v).toLocaleString("en-GB", {maximumFractionDigits: d ?? 0});
const money = (v, c) => v == null || v === "" ? "" : curFmt(v, c);
const get = (k, d) => { const v = localGet("cr." + k); return v == null || v === "" ? d : v };
const set = (k, v) => localSet("cr." + k, v == null ? "" : String(v));
const C = {el: null, v: null, open: new Set(), sort: null, dir: -1, busy: false};
const ACT = {pause_ad: "Pause ad", replace_text: "Replace text", remove_text: "Remove text"};
const ACT_TIP = {pause_ad: "Adds a pause for the ad(s) to the draft", replace_text: "Adds a text change to the draft: the ad keeps its other texts",
                 remove_text: "Adds a change to the draft that takes this text out of the ad(s)"};

function q(){
  const p = new URLSearchParams();
  const acct = get("acct", ""); if (acct) p.set("account", acct);
  const view = get("view." + (acct || ""), ""); if (view) p.set("view", view);
  const r = get("range", "30");
  if (r === "custom"){ p.set("start", get("start", "")); p.set("end", get("end", "")) } else p.set("days", r);
  p.set("grouped", get("grouped", "1")); p.set("min_impressions", get("min", "500"));
  return p;
}
function rangeBody(){ const r = get("range", "30"); return r === "custom" ? {start: get("start", ""), end: get("end", "")} : {days: +r} }

function render(el){
  C.el = el;
  if (window.CR_ACCT){ set("acct", window.CR_ACCT); window.CR_ACCT = null }
  el.innerHTML = `<div class="toolbar cr-bar">
    <b style="font-weight:500">Creatives</b>
    <select id="crAcct" title="Ad account"></select>
    <select id="crView" title="What to show"></select>
    <select id="crRange" title="Date range"><option value="7">Last 7 days</option><option value="14">Last 14 days</option><option value="30">Last 30 days</option><option value="90">Last 90 days</option><option value="custom">Custom ...</option></select>
    <select id="crGroup" title="Add up the same text, image or video across ads, or one row per ad"><option value="1">Per asset (across ads)</option><option value="0">Per ad and asset</option></select>
    <select id="crField" title="Asset type"><option value="">All asset types</option></select>
    <input id="crSearch" placeholder="Search text, campaign, ad" style="width:190px">
    <label class="hint" style="margin:0" title="Rows with fewer impressions are flagged: too little data to judge">Low volume under <input type="number" id="crMin" min="0" step="100"> impr.</label>
    <label class="hint" style="margin:0"><input type="checkbox" id="crHideLow"> Hide low volume</label>
    <span class="spacer" style="flex:1"></span>
    <span id="crFetched" class="chip"></span>
    <button id="crRefresh" title="Read this account's creatives again for this date range (read-only)">Refresh</button>
  </div>
  <div class="cr-scroll" id="crBody"><p class="cr-note">Loading creatives (read once when nothing is cached for this account and date range) ...</p></div>`;
  $("#crRange").value = get("range", "30"); $("#crGroup").value = get("grouped", "1"); $("#crMin").value = get("min", "500");
  $("#crHideLow").checked = get("hidelow", "0") === "1";
  $("#crAcct").onchange = e => { set("acct", e.target.value); C.open.clear(); load() };
  $("#crView").onchange = e => { set("view." + get("acct", ""), e.target.value); C.open.clear(); set("field", ""); load() };
  $("#crRange").onchange = e => {
    if (e.target.value === "custom"){
      const a = prompt("From (YYYY-MM-DD):", get("start", "")); if (!a){ e.target.value = get("range", "30"); return }
      const b = prompt("To (YYYY-MM-DD):", get("end", "")); if (!b){ e.target.value = get("range", "30"); return }
      set("start", a.trim()); set("end", b.trim());
    }
    set("range", e.target.value); C.open.clear(); load();
  };
  $("#crGroup").onchange = e => { set("grouped", e.target.value); C.open.clear(); load() };
  $("#crMin").onchange = e => { set("min", e.target.value || "0"); load() };
  $("#crField").onchange = e => { set("field", e.target.value); draw() };
  $("#crSearch").oninput = () => draw();
  $("#crHideLow").onchange = e => { set("hidelow", e.target.checked ? "1" : "0"); draw() };
  $("#crRefresh").onclick = refresh;
  load();
}

async function load(){
  let v;
  try { v = await api("/api/area/creatives/view?" + q().toString()) }
  catch(e) { $("#crBody").innerHTML = `<div class="prob">${h(e.message)}</div>`; return }
  C.v = v;
  if (!get("acct", "")) set("acct", v.account);
  draw();
}

async function refresh(){
  if (C.busy || !C.v) return;
  C.busy = true; $("#crRefresh").disabled = true; $("#crFetched").innerHTML = `<span class="spin"></span> Fetching ...`;
  try {
    const r = await api("/api/area/creatives/refresh", {account: C.v.account, ...rangeBody()});
    const bad = (r.errors || []).map(e => e.error).join("; ");
    toast(bad ? "Refresh failed: " + bad.slice(0, 300) : "Fresh creative data: " + C.v.name);
  } catch(e) { toast(e.message) }
  C.busy = false; $("#crRefresh").disabled = false;
  try { await loadDataStatus(); drawData() } catch(e) {}
  load();
}

function cols(v, rows){
  const c = v.currency, video = rows.some(r => r.plays), views = rows.some(r => r.views);
  const out = [
    ["ads", "Ads", r => num(r.ads)], ["impressions", "Impr.", r => num(r.impressions)], ["clicks", "Clicks", r => num(r.clicks)],
    ["ctr", "CTR", r => r.ctr == null ? "" : num(r.ctr, 2) + "%"], ["cost", "Cost", r => money(r.cost, c)],
    ["conversions", "Conv.", r => num(r.conversions, 2)], ["cpa", "CPA", r => money(r.cpa, c)], ["roas", "ROAS", r => r.roas == null ? "" : num(r.roas, 2)],
  ];
  if (views) out.push(["views", "Views", r => num(r.views)]);
  if (video) out.push(["plays", "Plays", r => num(r.plays)], ["thruplays", "ThruPlays", r => num(r.thruplays)],
    ["p25", "25%", r => num(r.p25)], ["p50", "50%", r => num(r.p50)], ["p75", "75%", r => num(r.p75)], ["p100", "100%", r => num(r.p100)],
    ["avg_watch", "Avg watch", r => r.avg_watch == null ? "" : num(r.avg_watch, 1) + " s"]);
  return out;
}

function preview(r){
  const img = safeUrl(r.image_url), yt = /^[A-Za-z0-9_-]{6,20}$/.test(r.video_id || "") && r.kind === "video" && !img ? r.video_id : "";
  const src = img || (yt ? `https://i.ytimg.com/vi/${yt}/mqdefault.jpg` : "");
  if (!src) return "";
  const link = yt ? `https://www.youtube.com/watch?v=${yt}` : safeUrl(r.video_url) || img;
  return `<a href="${h(link)}" target="_blank" rel="noopener noreferrer"><img src="${h(src)}" alt="" loading="lazy" referrerpolicy="no-referrer"></a>`;
}

function textCell(v, r){
  let main;
  if (r.kind === "ad") main = (r.headline ? `<b style="font-weight:500">${h(r.headline)}</b>\n` : "") + h(r.primary_text || "");
  else if (r.kind === "video") main = h(r.video_title || r.text || ("Video " + (r.video_id || "")));
  else if (r.kind === "image") main = h(r.text || "Image");
  else main = h(r.text);
  const where = (r.campaigns || []).slice(0, 2).join(", ") + ((r.campaigns || []).length > 2 ? ` +${r.campaigns.length - 2}` : "");
  const tags = (r.low_volume ? `<span class="cr-chip" title="Fewer impressions than the low-volume threshold: too little data to judge">Low volume</span>` : "")
    + (r.flexible ? `<span class="cr-chip" title="Uses several text or media options: see the Assets view for its parts">Flexible</span>` : "");
  return `<div class="cr-text">${main}${tags}</div><div class="cr-sub">${h(r.field_label)}${(r.channels || []).length ? " · " + h(r.channels.join(", ")) : ""}${where ? " · " + h(where) : ""}</div>`;
}

function actButtons(r, m){
  const acts = (m ? m.actions : r.actions) || [];
  if (!acts.length) return m ? "" : (r.why_not ? `<span class="cr-sub" title="${h(r.why_not)}">Read-only</span>` : `<span class="cr-sub">Read-only</span>`);
  const own = m ? ` data-owner="${h(m.owner_id)}"` : "";
  const many = !m && r.ads > 1 ? " (all)" : "";
  return `<div class="cr-acts">${acts.map(a => `<button data-act="${h(a)}" data-key="${h(r.key)}"${own} title="${h(ACT_TIP[a])}${many ? ": every ad in this row that allows it" : ""}">${h(ACT[a])}${many}</button>`).join("")}</div>`;
}

function members(v, r, ncols){
  const c = v.currency;
  const rows = r.members.map(m => `<tr><td>${h(m.ad_name || (m.ad_id ? "Ad " + m.ad_id : ""))}<div class="cr-sub">${h(m.where)}</div></td><td>${h(m.status || "")}${m.enabled === false ? " (removed from ad)" : ""}</td>
    <td class="num">${num(m.impressions)}</td><td class="num">${num(m.clicks)}</td><td class="num">${money(m.cost, c)}</td><td class="num">${num(m.conversions, 2)}</td>
    <td>${actButtons(r, m)}</td></tr>`).join("");
  return `<tr class="cr-mem"><td></td><td colspan="${ncols}"><table><thead><tr><th>Ad / asset group</th><th>Status</th><th class="num">Impr.</th><th class="num">Clicks</th>
    <th class="num">Cost</th><th class="num">Conv.</th><th></th></tr></thead><tbody>${rows}</tbody></table></td></tr>`;
}

function draw(){
  const v = C.v; if (!v) return;
  $("#crAcct").innerHTML = v.accounts.map(a => `<option value="${h(a.key)}">${h(a.name)} (${a.platform === "meta" ? "Meta" : "Google Ads"})</option>`).join("");
  $("#crAcct").value = v.key;
  $("#crView").innerHTML = v.views.map(x => `<option value="${h(x.id)}">${h(x.label)}</option>`).join(""); $("#crView").value = v.view;
  const fsel = get("field", "");
  $("#crField").innerHTML = `<option value="">All asset types</option>` + v.fields.map(f => `<option value="${h(f.id)}">${h(f.label)}</option>`).join("");
  $("#crField").value = v.fields.some(f => f.id === fsel) ? fsel : "";
  $("#crFetched").textContent = v.fetched_at ? `${v.window} · fetched ${fmtWhen(v.fetched_at)}` : "";
  const s = ($("#crSearch").value || "").toLowerCase(), field = $("#crField").value, hide = $("#crHideLow").checked;
  let rows = v.rows.filter(r => (!field || r.field === field) && (!hide || !r.low_volume)
    && (!s || [r.text, r.headline, r.primary_text, r.video_title, (r.campaigns || []).join(" "), r.members.map(m => (m.ad_name || "") + " " + (m.where || "")).join(" ")].join(" ").toLowerCase().includes(s)));
  const cc = cols(v, v.rows);  // columns from every row, so filtering never shifts the headers
  if (C.sort){ const k = C.sort, d = C.dir; rows = rows.slice().sort((a, b) => (a[k] == null) - (b[k] == null) || d * ((a[k] ?? 0) - (b[k] ?? 0))) }
  // totals of the rows shown (search, asset type and hide-low filters included), the same way the server adds them up
  const t = rows.reduce((a, r) => { for (const k of ["impressions", "clicks", "cost", "conversions"]) a[k] += Number(r[k] || 0); return a },
                        {impressions: 0, clicks: 0, cost: 0, conversions: 0});
  t.ctr = t.impressions ? t.clicks / t.impressions * 100 : null;
  const tot = `<div class="cr-tot"><span>${num(rows.length)} row${rows.length === 1 ? "" : "s"} of ${num(v.count)}</span><span>Impressions <b>${num(t.impressions)}</b></span><span>Clicks <b>${num(t.clicks)}</b></span>
    <span>CTR <b>${t.ctr == null ? "-" : num(t.ctr, 2) + "%"}</b></span><span>Cost <b>${h(money(t.cost, v.currency))}</b></span><span>Conversions <b>${num(t.conversions, 2)}</b></span>
    <span class="cr-sub" style="margin:0">Totals add up every asset row, so the same click counts once per asset it showed with.</span></div>`;
  const warn = (v.warnings || []).map(w => `<div class="warnl" style="margin-bottom:8px">${h(w)}</div>`).join("")
    + ((v.missing || []).length ? `<p class="cr-note"><button id="crRest" title="Read only the asset breakdowns that were not fetched (read-only)">Fetch the rest (${v.missing.length})</button></p>` : "");
  const head = `<tr><th class="cr-prev"></th><th data-sort="text">Asset</th>${cc.map(([k, l]) => `<th class="num${C.sort === k ? " on" : ""}" data-sort="${k}">${h(l)}${C.sort === k ? (C.dir < 0 ? " ▾" : " ▴") : ""}</th>`).join("")}<th></th></tr>`;
  const body = rows.map(r => `<tr class="cr-r${r.low_volume ? " low" : ""}" data-row="${h(r.key)}" title="Click to see the ads it ran in">
      <td class="cr-prev">${preview(r)}</td><td>${textCell(v, r)}</td>${cc.map(([k, , f]) => `<td class="num">${h(f(r))}</td>`).join("")}<td>${actButtons(r)}</td></tr>`
    + (C.open.has(r.key) ? members(v, r, cc.length + 2) : "")).join("");
  $("#crBody").innerHTML = warn + tot + (rows.length
      ? `<div style="overflow:auto"><table class="cr-table"><thead>${head}</thead><tbody>${body}</tbody></table></div>`
      : `<p class="cr-note">No ${v.count ? "rows match these filters" : "assets with impressions in this date range"}.</p>`)
    + `<p class="cr-note" style="margin-top:12px">${h(v.method)} Actions only add changes to the account's draft: check, preview and Push live there (team approval applies, undo works as usual).</p>`;
  wire();
}

async function act(btn){
  const v = C.v, r = v.rows.find(x => x.key === btn.dataset.key); if (!r) return;
  const a = btn.dataset.act, body = {account: v.account, view: v.view, key: r.key, action: a, grouped: v.grouped, ...rangeBody()};
  if (btn.dataset.owner) body.ad_ids = [btn.dataset.owner];
  if (a === "replace_text"){
    if (r.kind === "ad"){
      const which = prompt("Replace which text? Type 1 for the primary text, 2 for the headline:", "1"); if (!which) return;
      body.text_field = which.trim() === "2" ? "headline" : "primary_text";
    }
    const old = r.kind === "ad" ? (body.text_field === "headline" ? r.headline : r.primary_text) : r.text;
    const t = prompt(`New ${r.kind === "ad" ? body.text_field.replace("_", " ") : r.field_label.toLowerCase()} (replaces "${String(old || "").slice(0, 80)}"):`, old || "");
    if (t == null || !t.trim() || t === old) return;
    body.new_text = t;
  } else if (!confirm(`${ACT[a]}: add this to the ${v.name} draft${!body.ad_ids && r.ads > 1 ? ` for every ad in this row that allows it (${r.ads})` : ""}? Nothing changes until you push the draft live.`)) return;
  try {
    const res = await api("/api/area/creatives/draft", body);
    const sk = (res.skipped || []).map(x => `${x.name}: ${x.why}`).join("; ");
    const probs = (res.added || []).flatMap(x => x.problems || []);
    if (!(res.added || []).length){ toast((res.message || "Nothing added.") + (sk ? " " + sk : "")); return }
    toast(`${res.added.length} change${res.added.length === 1 ? "" : "s"} added to the ${v.name} draft${probs.length ? " (with problems: " + probs.join("; ").slice(0, 160) + ")" : ""}${sk ? ". Skipped: " + sk.slice(0, 200) : ""}. Review and push them in Ads.`);
  } catch(e) { toast(e.message) }
}

function wire(){
  const B = $("#crBody");
  B.querySelectorAll("th[data-sort]").forEach(th => th.onclick = () => {
    const k = th.dataset.sort; if (k === "text") { C.sort = null; draw(); return }
    if (C.sort === k) C.dir = -C.dir; else { C.sort = k; C.dir = k === "cpa" ? 1 : -1 }
    draw();
  });
  B.querySelectorAll("tr[data-row]").forEach(tr => tr.onclick = e => {
    if (e.target.closest("button,a")) return;
    const k = tr.dataset.row; C.open.has(k) ? C.open.delete(k) : C.open.add(k); draw();
  });
  B.querySelectorAll("button[data-act]").forEach(b => b.onclick = e => { e.stopPropagation(); act(b) });
  const rest = $("#crRest"); if (rest) rest.onclick = async () => {
    rest.disabled = true; rest.innerHTML = `<span class="spin"></span> Fetching ...`;
    try { const r = await api("/api/area/creatives/rest", {account: C.v.account, ...rangeBody()});
      toast(r.missing.length ? (r.warnings[0] || "Meta is still busy: try again later") : "All asset breakdowns fetched") } catch(e) { toast(e.message) }
    load();
  };
}

window.AREAS = window.AREAS || {};
window.AREAS.creatives = {label: "Creatives", render};
})();
