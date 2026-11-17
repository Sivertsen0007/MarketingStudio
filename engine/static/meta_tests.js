// [meta tests] The Tests tab in the Meta ads area: A/B tests and creative tests (Meta "ad studies").
// Reads: GET /api/meta/tests, /api/meta/tests/results. New tests and ending a test only ever go into the draft
// (POST /api/meta/tests/add); they reach Meta with Push live, gated like every other Meta change.
// Uses the page's helpers: api, qs, esc, S, toast, showDialog, poll, dlg.
(() => {
  const T = {list: null, open: null, results: {}, busy: false};
  const n2 = v => v == null ? "-" : Number(v).toLocaleString("en-GB", {minimumFractionDigits: 2, maximumFractionDigits: 2});
  const n0 = v => v == null ? "-" : Number(v).toLocaleString("en-GB");
  const chip = st => `<span class="chip ${st === "Running" ? "ok" : st === "Scheduled" ? "draft" : ""}">${esc(st)}</span>`;
  const today = () => new Date().toISOString().slice(0, 10);
  const plus = d => new Date(Date.now() + d * 864e5).toISOString().slice(0, 10);
  const v = id => (document.getElementById(id) || {}).value || "";

  async function load(refresh){
    T.busy = true;
    try { T.list = refresh ? await api("/api/meta/tests/refresh", {}) : await api("/api/meta/tests?" + qs({acct: S.acct})) }
    finally { T.busy = false }
  }

  function cellsText(r){
    return r.cells.map(c => `${esc(c.name)}${c.percent != null ? ` <span class="hint">${esc(c.percent)}%</span>` : ""}`).join(" · ");
  }

  function resultsHtml(id){
    const x = T.results[id];
    if (!x) return `<div class="hint">Loading results ...</div>`;
    if (x.error) return `<div class="prob">${esc(x.error)}</div>`;
    const s = x.study, hp = new Set(x.meta.high_performer_ids || []), creative = s.kind === "creative";
    const rows = s.cells.map(c => {
      const ad = c.ids[0], lead = x.leader && (x.leader === c.id || (creative && hp.has(ad)));
      const conf = creative && x.meta.confidence[ad] != null ? `${x.meta.confidence[ad]}%` : "";
      return `<tr class="${lead ? "focus" : ""}"><td>${esc(c.name)}${creative && hp.has(ad) ? ` <span class="chip ok">high performer</span>` : ""}</td>
        <td title="${esc(c.ids.join(", "))}">${esc(c.names.join(", ") || "-")}</td><td class="num">${c.percent != null ? esc(c.percent) + "%" : "-"}</td>
        <td class="num">${n2(c.spend)}</td><td class="num">${n0(c.impressions)}</td><td class="num">${c.ctr != null ? esc(c.ctr) + "%" : "-"}</td>
        <td class="num">${n0(c.results)}</td><td class="num">${n2(c.cost_per_result)}</td>${creative ? `<td class="num">${esc(conf || "-")}</td>` : ""}</tr>`;
    }).join("");
    return `<p class="${x.thin ? "chip warn" : "chip ok"}" style="display:inline-block;white-space:normal">${esc(x.verdict)}</p>
      <table style="margin-top:8px"><thead><tr><th>Cell</th><th>In the cell</th><th>Share</th><th>Spend</th><th>Impr.</th><th>CTR</th><th>Results</th><th>Cost/result</th>${creative ? "<th>Confidence</th>" : ""}</tr></thead>
      <tbody>${rows}</tbody></table>
      ${x.notes.length ? `<ul class="hint">${x.notes.map(n => `<li>${esc(n)}</li>`).join("")}</ul>` : ""}
      <p><button data-t-rr="${esc(id)}">Refresh results</button></p>`;
  }

  function draw(el){
    if (!T.list){ el.innerHTML = `<div class="empty">${T.busy ? "Loading tests from Meta ..." : "No tests loaded."}</div>`; return }
    const head = `<div style="padding:10px 12px;display:flex;gap:8px;flex-wrap:wrap;align-items:center">
      <b>Tests on Meta</b><span class="hint" style="margin:0">A/B tests and creative tests on the business behind this ad account. New tests and ends go into the draft; Push live sends them.</span>
      <span style="flex:1"></span>
      <button data-t-new="split">New A/B test</button><button data-t-new="variant">Duplicate an ad set with one change</button>
      <button data-t-new="creative">New creative test</button><button data-t-refresh="1">Refresh</button></div>`;
    if (!T.list.length){ el.innerHTML = head + `<div class="empty">No tests on this business yet.</div>`; return }
    let h = head + `<table><thead><tr><th>Test</th><th>Type</th><th>Status</th><th>Start (${esc((T.list[0] || {}).tz || "UTC")})</th><th>End</th><th>Cells</th><th></th></tr></thead><tbody>`;
    for (const r of T.list){
      h += `<tr><td title="${esc(r.id)}">${esc(r.name)}${r.by_studio ? ` <span class="chip draft">Studio</span>` : ""}</td><td>${esc(r.label)}</td><td>${chip(r.status)}</td>
        <td>${esc(r.start)}</td><td>${esc(r.end)}</td><td>${cellsText(r)}</td>
        <td><button data-t-open="${esc(r.id)}">${T.open === r.id ? "Hide results" : "Results"}</button>
        ${r.status === "Running" ? ` <button data-t-end="${esc(r.id)}">End test ...</button>` : ""}</td></tr>`;
      if (T.open === r.id) h += `<tr><td colspan="7" style="white-space:normal;padding:10px 16px">${resultsHtml(r.id)}</td></tr>`;
    }
    el.innerHTML = h + "</tbody></table>";
  }

  async function openResults(el, id, refresh){
    T.open = id;
    if (refresh || !T.results[id]){
      delete T.results[id]; draw(el);
      try { T.results[id] = refresh ? await api("/api/meta/tests/refresh", {id}) : await api("/api/meta/tests/results?" + qs({acct: S.acct, id})) }
      catch(e) { T.results[id] = {error: e.message} }
    }
    if (T.open === id) draw(el);
  }

  async function addTest(mode, test, note){
    const items = await api("/api/meta/tests/add", {mode, test, note, by: "you"});
    const bad = items.filter(i => i.problems.length);
    toast(bad.length ? `Added, but ${bad.length} need fixing (see the draft)` : `Added ${items.length} change${items.length > 1 ? "s" : ""} to the draft`);
    dlg.close(); await poll(true);
  }
  const go = (mode, build) => async () => { try { const t = build(); if (t) await addTest(mode, t, v("t_note")) } catch(e) { toast(e.message) } };

  const dates = `<div class="row"><label>Start</label><input id="t_start" type="date" value="${today()}"></div>
    <div class="row"><label>End</label><input id="t_end" type="date" value="${plus(14)}"></div>
    <p class="hint">Days in the ad account's time zone. Meta suggests 7 to 30 days. A test that starts today starts 15 minutes after the push.</p>`;
  const noteRow = `<div class="row"><label>Why (optional)</label><input id="t_note" placeholder="Shown in the draft and the changelog"></div>`;
  const honest = `<p class="hint">A test can't be removed from Meta once it is pushed, only ended, and Meta can't test-run it first: Push live checks the objects with reads, and Meta checks any new ad set or ad. Undo deletes the variant objects Studio created, not the test.</p>`;
  const picks = (rows, label) => `<div style="max-height:260px;overflow:auto;border:1px solid var(--line2);padding:6px">${rows.map(r =>
    `<label class="tick" style="margin:2px 0"><input type="checkbox" data-t-pick="${esc(r.id)}" data-t-name="${esc(label(r))}"> ${esc(label(r))} <span class="hint">${esc(r.status || "")}</span></label>`).join("") || `<div class="hint">Nothing to pick.</div>`}</div>`;
  const picked = () => [...document.querySelectorAll("[data-t-pick]:checked")].map(b => ({id: b.dataset.tPick, name: b.dataset.tName}));
  const grid = kind => api("/api/grid?" + qs({acct: S.acct, kind, status: "active"}));

  async function newSplit(){
    const [sets, camps] = await Promise.all([grid("adsets"), grid("campaigns")]);
    const body = level => `<p class="hint">Each ticked ${level === "adset" ? "ad set" : "campaign"} becomes one cell with an equal share of the audience (2 to 5).</p>
      ${picks(level === "adset" ? sets : camps, r => level === "adset" ? `${r.campaign} / ${r.ad_set}` : r.campaign)}`;
    showDialog("New A/B test", `<div class="row"><label>Name</label><input id="t_name"></div>
      <div class="row"><label>Test</label><select id="t_level"><option value="adset">Ad sets</option><option value="campaign">Campaigns</option></select></div>
      <div id="t_picks">${body("adset")}</div>${dates}${honest}${noteRow}`,
      [["Add to draft", go("split", () => {
        const p = picked(); if (!v("t_name")) return toast("Give the test a name");
        if (p.length < 2 || p.length > 5) return toast("Tick 2 to 5");
        return {name: v("t_name"), level: v("t_level"), ids: p.map(x => x.id), names: p.map(x => x.name), start_date: v("t_start"), end_date: v("t_end")};
      }), "primary"]]);
    document.getElementById("t_level").onchange = e => { document.getElementById("t_picks").innerHTML = body(e.target.value) };
  }

  async function newCreative(){
    const ads = await grid("ads");
    showDialog("New creative test", `<p class="hint">Meta splits the audience between 2 to 7 ads, one per cell, ideally from one ad set, and names the high performers.</p>
      <div class="row"><label>Name</label><input id="t_name"></div>
      ${picks(ads, r => `${r.ad_set} / ${r.ad}`)}
      <div class="row"><label>Test budget/day</label><input id="t_budget" type="number" step="0.01" min="1" placeholder="in the account currency"></div>
      ${dates}${honest}${noteRow}`,
      [["Add to draft", go("creative", () => {
        const p = picked(); if (!v("t_name")) return toast("Give the test a name");
        if (p.length < 2 || p.length > 7) return toast("Tick 2 to 7 ads");
        if (!v("t_budget")) return toast("Give a test budget per day");
        return {name: v("t_name"), ads: p.map(x => x.id), daily_budget: +v("t_budget"), start_date: v("t_start"), end_date: v("t_end")};
      }), "primary"]]);
  }

  const CHANGE_FIELDS = {
    audience: `<div class="row"><label>Countries</label><input id="t_c_countries" placeholder="GB, DE (replaces every location)"></div>
      <div class="row"><label>Age</label><span><input id="t_c_amin" type="number" min="18" max="65" style="width:70px"> to <input id="t_c_amax" type="number" min="18" max="65" style="width:70px"></span></div>`,
    placement: `<div class="row"><label>Placements</label><select id="t_c_adv"><option value="1">Advantage+ (Meta picks)</option><option value="0">Only these platforms</option></select></div>
      <div class="row"><label>Platforms</label><span>${["facebook", "instagram", "audience_network", "messenger", "threads"].map(p => `<label class="tick" style="margin:2px 0"><input type="checkbox" data-t-plat="${p}"> ${p.replace("_", " ")}</label>`).join("")}</span></div>`,
    creative: `<p class="hint">Applied to every copied ad in the variant. Leave a field blank to keep it.</p>
      <div class="row"><label>Primary text</label><textarea id="t_c_text" rows="3"></textarea></div>
      <div class="row"><label>Headline</label><input id="t_c_head"></div><div class="row"><label>Link</label><input id="t_c_link"></div>`,
    optimisation: `<div class="row"><label>Optimisation goal</label><select id="t_c_goal">${["LANDING_PAGE_VIEWS", "LINK_CLICKS", "OFFSITE_CONVERSIONS", "REACH", "IMPRESSIONS", "THRUPLAY", "LEAD_GENERATION", "POST_ENGAGEMENT"].map(g => `<option>${g}</option>`).join("")}</select></div>
      <div class="row"><label>Conversion event</label><input id="t_c_event" placeholder="for conversions, e.g. PURCHASE (optional)"></div>`,
  };
  function changeFrom(what){
    if (what === "audience"){
      const c = {}; const cs = v("t_c_countries").split(/[,\s]+/).filter(Boolean).map(x => x.toUpperCase());
      if (cs.length) c.countries = cs; if (v("t_c_amin")) c.age_min = +v("t_c_amin"); if (v("t_c_amax")) c.age_max = +v("t_c_amax"); return c;
    }
    if (what === "placement"){
      if (v("t_c_adv") === "1") return {advantage: true};
      return {advantage: false, publisher_platforms: [...document.querySelectorAll("[data-t-plat]:checked")].map(b => b.dataset.tPlat)};
    }
    if (what === "creative"){ const c = {}; if (v("t_c_text")) c.primary_text = v("t_c_text"); if (v("t_c_head")) c.headline = v("t_c_head"); if (v("t_c_link")) c.link = v("t_c_link"); return c }
    const c = {optimization_goal: v("t_c_goal")}; if (v("t_c_event")) c.conversion_event = v("t_c_event"); return c;
  }

  async function newVariant(){
    const sets = await grid("adsets");
    showDialog("Duplicate an ad set with one change", `<p class="hint">Studio copies the ad set and its ads, changes one thing, and A/B tests the copy against the original. The copy is created <b>Active</b>, scheduled to deliver only from the test's start to its end (a paused copy would get nothing in the test), so Push live asks you to accept the spend.</p>
      <div class="row"><label>Ad set</label><select id="t_set">${sets.map(r => `<option value="${esc(r.id)}" ${S.ag === String(r.id) ? "selected" : ""}>${esc(r.campaign + " / " + r.ad_set)}</option>`).join("")}</select></div>
      <div class="row"><label>The one change</label><select id="t_what"><option value="audience">Audience</option><option value="placement">Placement</option><option value="creative">Creative</option><option value="optimisation">Optimisation</option></select></div>
      <div id="t_fields">${CHANGE_FIELDS.audience}</div>
      <div class="row"><label>Test name</label><input id="t_name" placeholder="optional"></div>
      <div class="row"><label>Variant share</label><input id="t_pct" type="number" min="10" max="90" value="50"></div>
      ${dates}${honest}${noteRow}`,
      [["Add to draft", go("variant", () => {
        if (!v("t_set")) return toast("Pick an ad set");
        const what = v("t_what"), change = changeFrom(what);
        if (Object.keys(change).length === 0) return toast("Say what the one change is");
        return {adset_id: v("t_set"), change: {what, ...change}, name: v("t_name") || undefined, percent: +v("t_pct") || 50, start_date: v("t_start"), end_date: v("t_end")};
      }), "primary"]]);
    document.getElementById("t_what").onchange = e => { document.getElementById("t_fields").innerHTML = CHANGE_FIELDS[e.target.value] };
  }

  function endDialog(id){
    const r = (T.list || []).find(x => x.id === id); if (!r) return;
    showDialog("End test", `<p>End <b>${esc(r.name)}</b> now? This goes into the draft; it ends when you push live.</p>
      <p class="hint">Ending is final: an ended test can't be restarted or undone. The ad sets and ads in it keep running as normal unless you pause them.</p>
      <label class="tick"><input type="checkbox" id="t_pause"> <span>Also pause the test's ${esc(r.cells.map(c => c.level).filter(Boolean)[0] === "campaign" ? "campaigns" : "ad sets or ads")}</span></label>${noteRow}`,
      [["Add to draft", go("end", () => ({study_id: id, pause: document.getElementById("t_pause").checked})), "primary"]]);
  }

  async function render(grid){
    // own wrapper inside #grid: the grid keeps its own click handler (sorting, editing), which ignores these rows
    let el = grid.querySelector(":scope > .meta-tests");
    if (!el){ grid.innerHTML = `<div class="meta-tests"></div>`; el = grid.firstElementChild }
    el.onclick = async e => {
      const b = e.target.closest("button"); if (!b) return;
      try {
        if (b.dataset.tRefresh){ T.list = null; T.results = {}; draw(el); await load(true); return draw(el) }
        if (b.dataset.tOpen){ if (T.open === b.dataset.tOpen){ T.open = null; return draw(el) } return openResults(el, b.dataset.tOpen) }
        if (b.dataset.tRr) return openResults(el, b.dataset.tRr, true);
        if (b.dataset.tEnd) return endDialog(b.dataset.tEnd);
        if (b.dataset.tNew) return add(b.dataset.tNew);
      } catch(err) { toast(err.message) }
    };
    if (T.acct !== S.acct){ T.acct = S.acct; T.list = null; T.results = {}; T.open = null }
    if (!T.list){ draw(el); try { await load(false) } catch(e) { el.innerHTML = `<div class="empty">${esc(e.message)}</div>`; return } }
    draw(el);
  }
  async function add(mode){
    try { await ({split: newSplit, creative: newCreative, variant: newVariant}[mode || "split"])() } catch(e) { toast(e.message) }
  }
  window.MetaTests = {render, add};
})();
