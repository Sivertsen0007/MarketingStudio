// Marketing Studio - Activity area: everything that happened in a period, across channels. Read-only.
(() => {
const CSS = `
.act-bar{gap:8px}
.act-chips{display:flex;gap:6px;flex-wrap:wrap;padding:8px 14px;background:var(--head);border-bottom:1px solid var(--line);font-size:12px;align-items:center}
.act-chip{display:inline-flex;align-items:center;gap:5px;padding:2px 9px;border-radius:12px;border:1px solid var(--line);background:var(--panel);color:var(--text2);cursor:default}
.act-chip i{width:8px;height:8px;border-radius:50%;background:var(--muted);display:inline-block}
.act-chip.ok i{background:var(--ok)} .act-chip.error i{background:var(--bad)} .act-chip.demo i{background:var(--warn)}
.act-chip.loading i{background:var(--accent);animation:actpulse 1s infinite alternate} .act-chip.not_connected i{background:var(--line)}
@keyframes actpulse{from{opacity:.3}to{opacity:1}}
.act-scroll{overflow:auto;flex:1;padding:14px 18px;min-height:0}
.act-tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px;margin-bottom:12px}
.act-tile{border:1px solid var(--line);border-radius:8px;padding:10px 12px;background:var(--panel);cursor:pointer}
.act-tile:hover{border-color:var(--accent)} .act-tile.on{border-color:var(--accent);background:var(--accent-bg)}
.act-tile .k{font-size:12px;color:var(--muted)} .act-tile .v{font-size:24px;margin-top:2px;font-variant-numeric:tabular-nums}
.act-tile .d{font-size:12px;color:var(--muted);margin-top:2px} .act-tile .d.up{color:var(--ok)} .act-tile .d.down{color:var(--bad)}
.act-chart{border:1px solid var(--line);border-radius:8px;background:var(--panel);padding:10px 12px 6px;margin-bottom:12px;position:relative}
.act-chart h3{margin:0 0 6px;font-size:13px;font-weight:500;color:var(--text2)}
.act-chart svg{display:block;width:100%;height:150px}
.act-chart .bar{fill:var(--accent)} .act-chart .bar:hover,.act-chart .hit:hover+.bar{fill:var(--draft)}
.act-chart .ax{fill:var(--muted);font-size:10px} .act-chart .grid{stroke:var(--line2);stroke-width:1}
.act-tip{position:absolute;pointer-events:none;background:var(--title);color:var(--title-text);font-size:12px;padding:6px 9px;border-radius:4px;white-space:nowrap;display:none;z-index:5;line-height:1.4}
.act-filters{display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin:0 0 8px;font-size:13px}
.act-filters select{max-width:220px}
.act-filters input[type=search]{min-width:220px}
.act-note{font-size:12px;color:var(--muted);margin:0 0 8px;line-height:1.45}
.act-note b{color:var(--text2);font-weight:500}
.act-table td.wrap{white-space:normal;max-width:360px;line-height:1.35;padding-top:5px;padding-bottom:5px;height:auto}
.act-table td.on{color:var(--muted);text-decoration:line-through;max-width:200px}
.act-table td.nn{max-width:260px}
.act-table .demo{font-size:11px;padding:1px 6px;border-radius:2px;background:var(--warn-bg);color:var(--text);margin-left:5px}
.act-more{margin:8px 0}
`;
if (!document.getElementById("act-css")){ const st = document.createElement("style"); st.id = "act-css"; st.textContent = CSS; document.head.appendChild(st) }

const SRC = {google: "Google Ads", meta: "Meta", email: "Email", blog: "Blog", social: "Social", studio: "Studio"};
const TILES = [["google", "Google Ads changes"], ["meta", "Meta changes"], ["email", "Emails sent"], ["blog_pub", "Blog posts published"],
               ["blog_edit", "Blog posts edited"], ["social", "Social posts published"], ["studio", "Studio pushes and undos"]];
const STATUS = {ok: "ok", error: "error", demo: "demo data", loading: "loading", not_connected: "not connected"};
const MAC_TZ = (Intl.DateTimeFormat().resolvedOptions().timeZone) || "Europe/London";
const T = {period: localGet("act.period") || "last_30", from: "", to: "", tz: localGet("act.tz") || "Europe/London", compare: localGet("act.compare") !== "0",
           res: {}, per: null, seq: 0, f: {channel: "", account: "", type: "", by: "", q: ""}, group: "none", sort: {key: "ts", desc: true},
           who: false, show: 300, el: null};
const N = v => Number(v || 0).toLocaleString("en-GB");
const counted = r => r.count !== false && !r.system && !r.demo;
const tileKey = r => r.channel === "blog" ? (r.type === "Post published" ? "blog_pub" : "blog_edit") : r.channel;
const fmtTime = ts => new Date(ts).toLocaleString("en-GB", {timeZone: T.tz, day: "numeric", month: "short", year: "numeric", hour: "2-digit", minute: "2-digit"});
const dayKey = ts => new Intl.DateTimeFormat("en-CA", {timeZone: T.tz, year: "numeric", month: "2-digit", day: "2-digit"}).format(new Date(ts));
const fmtDay = d => new Date(d + "T12:00:00Z").toLocaleDateString("en-GB", {day: "numeric", month: "short"});
const pq = () => ({period: T.period, from: T.period === "custom" ? T.from : "", to: T.period === "custom" ? T.to : "", tz: T.tz});

function render(el){
  T.el = el;
  const opts = [["this_week", "This week"], ["last_week", "Last week"], ["this_month", "This month"], ["last_month", "Last month"],
                ["last_7", "Last 7 days"], ["last_30", "Last 30 days"], ["last_90", "Last 90 days"], ["this_quarter", "This quarter"], ["custom", "Custom ..."]];
  el.innerHTML = `<div class="toolbar act-bar">
    <select id="actPeriod" title="Period">${opts.map(([v, l]) => `<option value="${v}">${l}</option>`).join("")}</select>
    <span id="actCustom" style="display:none"><input type="date" id="actFrom"> to <input type="date" id="actTo"> <button id="actApply">Apply</button></span>
    <select id="actTz" title="Time zone for days and times"><option value="Europe/London">UK time</option>${MAC_TZ !== "Europe/London" ? `<option value="${esc(MAC_TZ)}">This Mac (${esc(MAC_TZ)})</option>` : ""}</select>
    <label style="display:inline-flex;gap:5px;align-items:center;font-size:13px"><input type="checkbox" id="actCompare"> Compare with previous period</label>
    <span class="spacer" style="flex:1"></span>
    <span id="actPerLabel" class="chip"></span>
    <button id="actRefresh" title="Sync Google and Meta into the archive and read every source again (otherwise results are kept until you refresh)">Refresh</button>
  </div>
  <div id="actSync" class="prob" style="display:none;margin:8px 16px 0"></div>
  <div class="act-chips" id="actChips"></div>
  <div class="act-scroll" id="actBody"></div>`;
  $("#actPeriod").value = T.period;
  $("#actTz").value = [...$("#actTz").options].some(o => o.value === T.tz) ? T.tz : "Europe/London";
  T.tz = $("#actTz").value;
  $("#actCompare").checked = T.compare;
  $("#actCustom").style.display = T.period === "custom" ? "" : "none";
  $("#actPeriod").onchange = e => { T.period = e.target.value; $("#actCustom").style.display = T.period === "custom" ? "" : "none";
    if (T.period !== "custom"){ localSet("act.period", T.period); load() } };
  $("#actApply").onclick = () => { T.from = $("#actFrom").value; T.to = $("#actTo").value; if (T.from && T.to) load(); else toast("Pick a from and a to date") };
  $("#actTz").onchange = e => { T.tz = e.target.value; localSet("act.tz", T.tz); load() };
  $("#actCompare").onchange = e => { T.compare = e.target.checked; localSet("act.compare", T.compare ? "1" : "0"); draw() };
  $("#actRefresh").onclick = async () => { $("#actRefresh").disabled = true; await dataRefresh("activity", null, {reload:false}); $("#actRefresh").disabled = false; load() };
  if (T.period === "custom" && !(T.from && T.to)){ T.period = "last_30"; $("#actPeriod").value = "last_30"; $("#actCustom").style.display = "none" }
  load();
}

function load(refresh){
  const seq = ++T.seq;
  T.res = {}; T.per = null; T.show = 300;
  Object.keys(SRC).forEach(s => T.res[s] = {status: "loading", rows: [], by_tile: {}, prev_by_tile: {}, note: "", errors: []});
  draw();
  Object.keys(SRC).forEach(async s => {
    try {
      const r = await api("/api/area/activity/source?" + qs({...pq(), src: s, refresh: refresh ? "1" : ""}));
      if (seq !== T.seq) return;
      T.res[s] = r; T.per = r.period;
      if (r.sync){ const w = $("#actSync"); if (w){ w.textContent = r.sync.warning || ""; w.style.display = r.sync.warning ? "" : "none" } }
    } catch(e) {
      if (seq !== T.seq) return;
      T.res[s] = {status: "error", rows: [], by_tile: {}, prev_by_tile: {}, note: "", errors: [e.message]};
    }
    draw();
  });
}

function allRows(){ return Object.values(T.res).flatMap(r => r.rows || []) }

// Same rules as activity.apply_filters in Python (the CSV export uses those).
function filtered(rows){
  const f = T.f, q = f.q.trim().toLowerCase();
  const out = rows.filter(r => (!f.channel || r.channel === f.channel) && (!f.account || r.account === f.account) && (!f.type || r.type === f.type) &&
    (!f.by || r.by === f.by) && (!q || ["account", "type", "object_type", "object", "where", "old", "new", "by", "who", "fields"].map(k => r[k] || "").join(" ").toLowerCase().includes(q)));
  const k = T.sort.key, d = T.sort.desc ? -1 : 1;
  return out.sort((a, b) => { const x = String(a[k] ?? "").toLowerCase(), y = String(b[k] ?? "").toLowerCase(); return x < y ? -d : x > y ? d : 0 });
}

function draw(){
  if (!T.el || !$("#actBody")) return;
  $("#actPerLabel").textContent = T.per ? `${T.per.label}: ${fmtDay(T.per.days[0])} to ${fmtDay(T.per.days[T.per.days.length - 1])}` : "Loading ...";
  $("#actChips").innerHTML = `<span style="color:var(--muted)">Sources:</span>` + Object.entries(SRC).map(([s, l]) => { const r = T.res[s] || {};
    const tip = [r.note, ...(r.errors || [])].filter(Boolean).join(" | ");
    return `<span class="act-chip ${esc(r.status)}" title="${esc(tip)}" data-src="${s}"><i></i>${esc(l)}: ${esc(STATUS[r.status] || r.status)}${r.status === "ok" || r.status === "demo" ? ` (${N((r.rows || []).length)})` : ""}</span>` }).join("");
  const rows = allRows();
  const body = $("#actBody");
  const keepFocus = document.activeElement && document.activeElement.id === "actQ";
  body.innerHTML = tilesHtml() + chartHtml(rows) + notesHtml() + filtersHtml(rows) + `<div id="actTable"></div>`;
  wire(rows);
  table();
  if (keepFocus){ const i = $("#actQ"); i.focus(); i.setSelectionRange(i.value.length, i.value.length) }
}

function tilesHtml(){
  return `<div class="act-tiles">${TILES.map(([k, label]) => {
    const src = k.startsWith("blog") ? "blog" : k, r = T.res[src] || {};
    const v = r.by_tile ? (r.by_tile[k] || 0) : 0, pv = r.prev_by_tile ? (r.prev_by_tile[k] || 0) : 0;
    let d = "";
    if (r.status === "loading") d = "loading ...";
    else if (r.status === "error") d = "could not read";
    else if (r.status === "not_connected") d = "not connected";
    else if (r.status === "demo") d = `demo only, not counted (${N((r.rows || []).length)} demo)`;
    else if (T.compare) d = r.prev_complete === false ? "no full history to compare" : (v === pv ? "same as previous period" : `${v > pv ? "up" : "down"} ${N(Math.abs(v - pv))} on previous (${N(pv)})`);
    const cls = T.compare && r.status === "ok" && r.prev_complete !== false && v !== pv ? (v > pv ? "up" : "down") : "";
    const on = T.f.channel === src && (src !== "blog" || T.f.type === (k === "blog_pub" ? "Post published" : "Post edited"));
    return `<div class="act-tile ${on ? "on" : ""}" data-tile="${k}" title="Show only these"><div class="k">${esc(label)}</div><div class="v">${r.status === "loading" ? "..." : N(v)}${r.complete === false ? "<span style='font-size:12px;color:var(--muted)'> *</span>" : ""}</div><div class="d ${cls}">${esc(d)}</div></div>` }).join("")}</div>`;
}

function chartHtml(rows){
  if (!T.per) return `<div class="act-chart"><h3>Activities per day</h3><p class="act-note">Loading ...</p></div>`;
  const days = T.per.days, idx = Object.fromEntries(days.map((d, i) => [d, i]));
  const n = days.map(() => 0), per = days.map(() => ({}));
  const fr = filtered(rows);
  fr.forEach(r => { if (!counted(r)) return; const i = idx[dayKey(r.ts)]; if (i === undefined) return; n[i]++; per[i][r.channel] = (per[i][r.channel] || 0) + 1 });
  const W = 1000, H = 150, L = 34, B = 20, top = 8, max = Math.max(1, ...n);
  const step = Math.pow(10, Math.floor(Math.log10(max))); const nice = Math.ceil(max / step) * step;
  const bw = (W - L) / days.length, gap = Math.min(2, bw * 0.2);
  const y = v => H - B - (H - B - top) * v / nice;
  const ticks = [0, nice / 2, nice];
  const every = Math.ceil(days.length / 12);
  T.chart = {days, n, per};
  return `<div class="act-chart"><h3>Activities per day (counted, with the filters below)</h3>
    <svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" role="img" aria-label="Activities per day">
    ${ticks.map(t => `<line class="grid" x1="${L}" x2="${W}" y1="${y(t)}" y2="${y(t)}"/><text class="ax" x="${L - 4}" y="${y(t) + 3}" text-anchor="end">${N(t)}</text>`).join("")}
    ${n.map((v, i) => { const x = L + i * bw + gap / 2, h = H - B - y(v), w = Math.max(1, bw - gap);
      return `<rect class="hit" data-i="${i}" x="${L + i * bw}" y="${top}" width="${bw}" height="${H - B - top}" fill="transparent"/>` +
             (v ? `<path class="bar" d="M${x},${H - B} v${-Math.max(0, h - Math.min(4, h, w / 2))} q0,${-Math.min(4, h, w / 2)} ${Math.min(4, h, w / 2)},${-Math.min(4, h, w / 2)} h${Math.max(0, w - 2 * Math.min(4, h, w / 2))} q${Math.min(4, h, w / 2)},0 ${Math.min(4, h, w / 2)},${Math.min(4, h, w / 2)} v${Math.max(0, h - Math.min(4, h, w / 2))} z" pointer-events="none"/>` : "") }).join("")}
    ${days.map((d, i) => i % every ? "" : `<text class="ax" x="${L + i * bw + bw / 2}" y="${H - 5}" text-anchor="middle">${esc(fmtDay(d))}</text>`).join("")}
    </svg><div class="act-tip" id="actTip"></div></div>`;
}

function notesHtml(){
  const g = T.res.google || {}, m = T.res.meta || {};
  const bits = [];
  if (g.note) bits.push(`<b>Google Ads:</b> ${esc(g.note)}`);
  if (m.note) bits.push(`<b>Meta:</b> ${esc(m.note)}`);
  ["email", "blog", "social"].forEach(s => { const r = T.res[s] || {}; if (r.note) bits.push(`<b>${SRC[s]}:</b> ${esc(r.note)}`) });
  Object.entries(T.res).forEach(([s, r]) => (r.errors || []).forEach(e => bits.push(`<b style="color:var(--bad)">${SRC[s]} error:</b> ${esc(e)}`)));
  bits.push("* = the archive does not cover the whole period yet. Meta's own automatic events (reviews, first delivery, billing) and demo or dry-run rows are listed but not counted.");
  return `<p class="act-note">${bits.join("<br>")}</p>`;
}

function opts(rows, key, cur, label){
  const c = {}; rows.forEach(r => { const v = r[key] || ""; if (v) c[v] = (c[v] || 0) + 1 });
  const list = Object.entries(c).sort((a, b) => b[1] - a[1]);
  return `<select data-f="${key}" title="${esc(label)}"><option value="">${esc(label)}: all</option>${list.map(([v, n]) => `<option value="${esc(v)}" ${v === cur ? "selected" : ""}>${esc(key === "channel" ? SRC[v] || v : v)} (${N(n)})</option>`).join("")}</select>`;
}

function filtersHtml(rows){
  const f = T.f;
  const scope = k => rows.filter(r => Object.entries(f).every(([fk, fv]) => !fv || fk === k || fk === "q" || r[fk] === fv));
  return `<div class="act-filters">
    ${opts(scope("channel"), "channel", f.channel, "Channel")}${opts(scope("account"), "account", f.account, "Account")}
    ${opts(scope("type"), "type", f.type, "Change")}${opts(scope("by"), "by", f.by, "Made by")}
    <input type="search" id="actQ" placeholder="Search object, change, values ..." value="${esc(f.q)}">
    <select id="actGroup" title="Group by"><option value="none">No grouping</option><option value="day">Group by day</option><option value="object">Group by object</option><option value="type">Group by change type</option></select>
    <button id="actClear">Clear filters</button>
    <span class="spacer" style="flex:1"></span>
    <label style="display:inline-flex;gap:5px;align-items:center" title="Who made each change (staff names and emails). Leave off unless the file stays internal."><input type="checkbox" id="actWho" ${T.who ? "checked" : ""}> Include who made it</label>
    <button id="actCsv" title="Download the filtered list as CSV">Export CSV</button></div>`;
}

function wire(rows){
  T.el.querySelectorAll("[data-f]").forEach(s => s.onchange = e => { T.f[e.target.dataset.f] = e.target.value; T.show = 300; draw() });
  let h; $("#actQ").oninput = e => { clearTimeout(h); h = setTimeout(() => { T.f.q = e.target.value; T.show = 300; draw() }, 250) };
  $("#actGroup").value = T.group; $("#actGroup").onchange = e => { T.group = e.target.value; table() };
  $("#actClear").onclick = () => { T.f = {channel: "", account: "", type: "", by: "", q: ""}; draw() };
  $("#actWho").onchange = e => { T.who = e.target.checked };
  $("#actCsv").onclick = exportCsv;
  T.el.querySelectorAll("[data-tile]").forEach(t => t.onclick = () => { const k = t.dataset.tile, src = k.startsWith("blog") ? "blog" : k;
    const type = src === "blog" ? (k === "blog_pub" ? "Post published" : "Post edited") : "";
    const same = T.f.channel === src && T.f.type === type;
    T.f = {...T.f, channel: same ? "" : src, type: same ? "" : type, account: "", by: ""}; draw() });
  const tip = $("#actTip"), svg = T.el.querySelector(".act-chart svg");
  if (svg && T.chart) svg.querySelectorAll(".hit").forEach(r => {
    r.onmousemove = e => { const i = +r.dataset.i, box = T.el.querySelector(".act-chart").getBoundingClientRect();
      const per = Object.entries(T.chart.per[i]).sort((a, b) => b[1] - a[1]).map(([c, n]) => `${esc(SRC[c] || c)}: ${N(n)}`).join("<br>");
      tip.innerHTML = `<b>${esc(fmtDay(T.chart.days[i]))}</b>: ${N(T.chart.n[i])}${per ? "<br>" + per : ""}`; tip.style.display = "block";
      tip.style.left = Math.min(e.clientX - box.left + 12, box.width - 160) + "px"; tip.style.top = (e.clientY - box.top - 10) + "px" };
    r.onmouseleave = () => tip.style.display = "none";
    r.onclick = () => { T.group = "none"; T.f.q = ""; toast(fmtDay(T.chart.days[r.dataset.i]) + ": " + N(T.chart.n[r.dataset.i]) + " activities") };
  });
}

const COLS = [{k: "ts", l: "Time"}, {k: "channel", l: "Channel"}, {k: "account", l: "Account"}, {k: "type", l: "Change"}, {k: "object", l: "Object"},
              {k: "where", l: "Where"}, {k: "old", l: "Old"}, {k: "new", l: "New"}, {k: "by", l: "Made by"}, {k: "who", l: "Who"}, {k: "link", l: ""}];

function table(){
  const el = $("#actTable"); if (!el) return;
  const rows = filtered(allRows());
  if (T.group !== "none"){
    const key = T.group === "day" ? r => dayKey(r.ts) : T.group === "object" ? r => `${r.object || "(no name)"}${r.where ? " | " + r.where : ""}` : r => r.type || "";
    const g = {};
    rows.forEach(r => { const k = key(r); const x = g[k] || (g[k] = {k, n: 0, c: 0, ch: {}}); x.n++; if (counted(r)) x.c++; x.ch[r.channel] = 1 });
    const list = Object.values(g).sort((a, b) => T.group === "day" ? (a.k < b.k ? 1 : -1) : b.n - a.n);
    el.innerHTML = `<p class="act-note">${N(list.length)} groups, ${N(rows.length)} rows.</p><table class="act-table"><thead><tr><th>${T.group === "day" ? "Day" : T.group === "object" ? "Object" : "Change type"}</th><th>Rows</th><th>Counted</th><th>Channels</th></tr></thead><tbody>
      ${list.map(x => `<tr><td class="wrap">${esc(T.group === "day" ? fmtDay(x.k) : x.k)}</td><td class="num">${N(x.n)}</td><td class="num">${N(x.c)}</td><td>${esc(Object.keys(x.ch).map(c => SRC[c] || c).join(", "))}</td></tr>`).join("")}</tbody></table>`;
    return;
  }
  const shown = rows.slice(0, T.show);
  el.innerHTML = `<p class="act-note">${N(rows.length)} rows${rows.length > shown.length ? `, showing the first ${N(shown.length)}` : ""}. Click a column to sort.</p>
    <table class="act-table"><thead><tr>${COLS.map(c => `<th data-sort="${c.k}" class="${T.sort.key === c.k ? "sorted" : ""}">${esc(c.l)}${T.sort.key === c.k ? (T.sort.desc ? " ▾" : " ▴") : ""}</th>`).join("")}</tr></thead><tbody>
    ${shown.map(r => `<tr${counted(r) ? "" : ` style="color:var(--muted)"`}><td>${esc(fmtTime(r.ts))}</td><td>${esc(SRC[r.channel] || r.channel)}</td><td>${esc(r.account)}${r.demo ? `<span class="demo">demo</span>` : ""}</td>
      <td class="wrap" title="${esc(r.fields || "")}">${esc(r.type)}${r.fields ? `<div style="font-size:11px;color:var(--muted);line-height:1.2">${esc(r.fields)}</div>` : ""}</td><td class="wrap" title="${esc(r.object_type || "")}">${esc(r.object)}</td><td class="wrap" style="color:var(--muted)">${esc(r.where)}</td>
      <td class="on" title="${esc(r.old)}">${esc(r.old)}</td><td class="nn" title="${esc(r.new)}">${esc(r.new)}</td><td title="${esc(r.source || "")}">${esc(r.by)}</td><td>${esc(r.who || "")}</td>
      <td>${r.link ? `<a href="${esc(r.link)}" target="_blank" rel="noopener">Open</a>` : ""}</td></tr>`).join("")}</tbody></table>
    ${rows.length > shown.length ? `<button class="act-more" id="actMore">Show ${N(Math.min(500, rows.length - shown.length))} more</button>` : ""}`;
  el.querySelectorAll("[data-sort]").forEach(th => th.onclick = () => { const k = th.dataset.sort; if (!k) return;
    T.sort = T.sort.key === k ? {key: k, desc: !T.sort.desc} : {key: k, desc: k === "ts"}; table() });
  const more = $("#actMore"); if (more) more.onclick = () => { T.show += 500; table() };
}

async function exportCsv(){
  try {
    const f = T.f;
    const r = await api("/api/area/activity/csv?" + qs({...pq(), channel: f.channel, account: f.account, type: f.type, by: f.by, q: f.q,
                                                         sort: T.sort.key, desc: T.sort.desc ? "1" : "0", who: T.who ? "1" : ""}));
    const blob = new Blob([r.csv], {type: "text/csv;charset=utf-8"});
    const a = document.createElement("a"); a.href = URL.createObjectURL(blob); a.download = r.filename; document.body.appendChild(a); a.click();
    setTimeout(() => { URL.revokeObjectURL(a.href); a.remove() }, 1000);
    toast(`Exported ${N(r.rows)} rows${r.include_who ? " with who made them" : ""}`);
  } catch(e) { toast("Export failed: " + e.message) }
}

window.AREAS = window.AREAS || {};
window.AREAS.activity = {label: "Activity", render};
})();
