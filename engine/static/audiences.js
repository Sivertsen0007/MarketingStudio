// [audiences] The Audiences tab: Google "Audience lists" and Meta "Audiences" (audiences.py, meta_audiences.py).
// Reads: GET /api/audiences, /api/audiences/detail (Meta: one audience's rule). Every create, edit, attach and remove only
// goes into the draft (POST /api/draft/add); it reaches Google or Meta with Push live, gated like every other change.
// Customer lists are shown read-only: Studio never uploads, reads or changes customer data.
// Uses the page's helpers: api, qs, S, toast, showDialog, addChanges, noteRow, isMeta, dlg.
(() => {
  const A = {data: null, acct: null, busy: false, q: ""};
  const h = s => String(s ?? "").replace(/[&<>"']/g, c => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[c]));
  const v = id => ((document.getElementById(id) || {}).value || "").trim();
  const checked = id => !!(document.getElementById(id) || {}).checked;
  const google = () => !isMeta();
  // the same keys as the engine (audiences.URL_OPS): never the spaced labels
  const URL_OPS = [["contains", "URL contains"], ["equals", "URL equals"], ["starts_with", "URL starts with"], ["ends_with", "URL ends with"],
    ["not_contains", "URL doesn't contain"], ["not_equals", "URL isn't"], ["not_starts_with", "URL doesn't start with"], ["not_ends_with", "URL doesn't end with"]];
  const META_OPS = [["contains", "URL contains"], ["equals", "URL equals"], ["not_contains", "URL doesn't contain"], ["event", "Pixel event"]];
  const ENG = {page: ["Facebook Page", {engaged: "Engaged with the Page", visited: "Visited the Page", messaged: "Messaged the Page", cta: "Clicked a call-to-action button",
                                       saved: "Saved the Page or a post", post: "Engaged with a post or ad"}],
               instagram: ["Instagram account", {engaged: "Engaged with the account", all: "Any interaction", visited: "Visited the profile", messaged: "Sent a message"}],
               lead_form: ["Lead form", {opened: "Opened the form", not_submitted: "Opened but didn't submit", submitted: "Submitted the form"}]};

  async function load(){
    A.busy = true;
    try { A.data = await api("/api/audiences?" + qs({acct: S.acct})) } finally { A.busy = false }
  }
  const rows = () => (A.data ? A.data.rows : []).filter(r => !A.q || JSON.stringify([r.name, r.type, r.rule, r.lookalike, r.used_by]).toLowerCase().includes(A.q));
  const byId = id => (A.data ? A.data.rows : []).find(r => r.id === id);

  function usedCell(r){
    if (!r.used_by.length) return `<span class="hint">not used</span>`;
    const first = r.used_by.slice(0, 2).map(h).join("<br>");
    return `<span title="${h(r.used_by.join("\n"))}">${first}${r.used_by.length > 2 ? `<br><span class="hint">and ${r.used_by.length - 2} more</span>` : ""}</span>`;
  }
  function actions(r){
    if (r.customer_list) return `<span class="chip" title="${h(r.note)}">Read-only</span>`;
    const b = [];
    if (r.editable) b.push(`<button data-a-edit="${h(r.id)}">Edit</button>`);
    b.push(`<button data-a-attach="${h(r.id)}">${google() ? "Attach" : "Use in ad set"}</button>`);
    if (!google() && r.seed_ok) b.push(`<button data-a-lal="${h(r.id)}">Lookalike</button>`);
    b.push(`<button class="${r.used_by.length ? "" : "danger"}" data-a-rm="${h(r.id)}" title="${r.used_by.length ? "In use: detach it first" : "Remove (final)"}">Remove</button>`);
    return b.join(" ");
  }

  function draw(el){
    if (!A.data){ el.innerHTML = `<div class="empty">${A.busy ? "Loading audiences ..." : "No audiences loaded."}</div>`; return }
    const g = google();
    const head = `<div style="padding:14px 16px 10px;display:flex;gap:8px;flex-wrap:wrap;align-items:center">
      <b style="font-size:16px;font-weight:600">${g ? "Audience lists" : "Audiences"}</b>
      <input id="a_q" placeholder="Filter" value="${h(A.q)}" style="width:160px">
      <span style="flex:1"></span>
      ${g ? `<button data-a-new="website">New website list</button><button data-a-new="combined">New combined list</button>`
          : `<button data-a-new="website">New website audience</button><button data-a-new="engagement">New engagement audience</button><button data-a-new="lookalike">New lookalike</button>`}
      <button data-a-reload="1" title="Show the data Studio last fetched (Refresh data fetches it again)">Reload</button></div>
      <p class="hint" style="padding:0 16px;margin:0 0 10px">${h(A.data.size_note)} ${h(A.data.customer_note)}. Everything here goes into the draft; Push live sends it.</p>`;
    const rs = rows();
    if (!rs.length){ el.innerHTML = head + `<div class="empty">No audiences${A.q ? " match the filter" : ""}.</div>`; return }
    const cols = g ? ["List", "Type", "Size (Search)", "Size (Display)", "Membership", "Rule", "Used by", "Status", ""]
                   : ["Audience", "Type", "Size", "Retention", "Rule / seed", "Used by", "Status", ""];
    let t = `<table><thead><tr>${cols.map(c => `<th>${h(c)}</th>`).join("")}</tr></thead><tbody>`;
    for (const r of rs){
      const name = `<td title="${h(r.description || r.id)}">${h(r.name)}${r.customer_list ? ` <span class="chip warn" title="${h(r.note)}">Customer list</span>` : ""}</td>`;
      t += g ? `<tr>${name}<td>${h(r.type)}</td><td>${h(r.size_search)}</td><td>${h(r.size_display)}</td><td>${h(r.retention)}</td>
                <td style="white-space:normal;max-width:320px">${h(r.rule || (r.read_only_why && !r.customer_list ? r.read_only_why : ""))}</td><td>${usedCell(r)}</td><td>${h(r.status)}</td><td>${actions(r)}</td></tr>`
             : `<tr>${name}<td>${h(r.type)}</td><td>${h(r.size)}</td><td>${h(r.retention)}</td><td style="white-space:normal;max-width:280px">${h(r.lookalike)}</td>
                <td>${usedCell(r)}</td><td><span class="${r.status_ok ? "" : "chip warn"}" style="white-space:normal">${h(r.status)}</span></td><td>${actions(r)}</td></tr>`;
    }
    el.innerHTML = head + t + "</tbody></table>";
    const q = el.querySelector("#a_q");
    q.oninput = () => { A.q = q.value.trim().toLowerCase(); const pos = q.selectionStart; draw(el); const n = el.querySelector("#a_q"); n.focus(); n.setSelectionRange(pos, pos) };
  }

  // ---- rule editor rows (URL rules; Meta also pixel events) ----
  function ruleRow(ops, r){  // create dialogs only
    r = r || {op: "contains", value: ""};
    const op = r.field === "event" ? "event" : r.op;
    return `<div class="ln a-rule"><select class="a-op">${ops.map(([k, l]) => `<option value="${k}" ${k === op ? "selected" : ""}>${h(l)}</option>`).join("")}</select>
      <input class="a-val" value="${h(r.value)}" placeholder="/egypt or SelectCabin" style="flex:1"><button class="link danger" onclick="this.parentElement.remove()" title="Remove this rule" aria-label="Remove this rule">✕</button></div>`;
  }
  function ruleBox(id, ops, list){
    return `<div class="lines" id="${id}">${(list && list.length ? list : [null]).map(r => ruleRow(ops, r)).join("")}</div>
      <button class="link" data-a-addrule="${id}">+ rule</button>`;
  }
  function readRules(id, meta){
    return [...document.querySelectorAll(`#${id} .a-rule`)].map(d => {
      const op = d.querySelector(".a-op").value, value = d.querySelector(".a-val").value.trim();
      if (!value) return null;
      return meta ? (op === "event" ? {field: "event", value} : {field: "url", op, value}) : {op, value};
    }).filter(Boolean);
  }
  function wireRuleButtons(ops){
    for (const b of document.querySelectorAll("[data-a-addrule]")) b.onclick = () => document.getElementById(b.dataset.aAddrule).insertAdjacentHTML("beforeend", ruleRow(ops));
  }
  const send = async (changes) => { try { await addChanges(changes, v("f_note")); dlg.close() } catch(e) { toast(e.message) } };

  // ---- Google ----
  function gWebsite(){
    showDialog("New website list", `<p class="hint">People who visit pages that match. Push live only (Editor can't take audience lists). Rules are set now: later edits change only the name, description and days.</p>
      <div class="row"><label>Name</label><input id="a_name"></div>
      <div class="row"><label>Visitors who match</label><select id="a_match"><option value="any">any rule</option><option value="all">every rule</option></select></div>
      ${ruleBox("a_inc", URL_OPS)}
      <p class="hint">Leave out people who visited (optional):</p>${ruleBox("a_exc", URL_OPS)}
      <div class="row"><label>Membership (days)</label><input id="a_days" type="number" min="1" max="540" value="30"> <span class="hint">1-540</span></div>
      <label class="tick"><input type="checkbox" id="a_pre" checked> <span>Fill it with recent visitors (Google uses up to the last 30 days, Display only)</span></label>
      <div class="row"><label>Description</label><input id="a_desc"></div>${noteRow}`,
      [["Add to draft", () => {
        if (!v("a_name")) return toast("Give the list a name");
        send([{type: "user_list", action: "create", kind: "website", name: v("a_name"), rules: readRules("a_inc"), exclude: readRules("a_exc"),
               match: v("a_match"), lifespan: +v("a_days"), prepopulate: checked("a_pre"), ...(v("a_desc") ? {description: v("a_desc")} : {})}]);
      }, "primary"]]);
    wireRuleButtons(URL_OPS);
  }
  // Editing an existing list: name, description and membership days only. Its rules / members are shown read-only.
  function gEdit(r){
    showDialog(`Edit list: ${r.name}`, `<p class="hint">Rules: ${h(r.rule || "-")}</p>
      <p class="hint">Rules and members can't be changed on an existing list; make a new list for other rules.</p>
      <div class="row"><label>Name</label><input id="a_name" value="${h(r.name)}"></div>
      <div class="row"><label>Membership (days)</label><input id="a_days" type="number" min="1" max="540" value="${h(r.membership_days)}"> <span class="hint">1-540</span></div>
      <div class="row"><label>Description</label><input id="a_desc" value="${h(r.description)}"></div>${noteRow}`,
      [["Save to draft", () => {
        if (!v("a_name")) return toast("Give the list a name");
        const c = {type: "user_list", action: "edit", list: r.name};
        if (v("a_name") !== r.name) c.new_name = v("a_name");
        const days = v("a_days");  // only when the user changed it: never lifespan 0 for a list without days
        if (days !== "" && days !== String(r.membership_days ?? "")) c.lifespan = +days;
        if (v("a_desc") !== (r.description || "")) c.description = v("a_desc");
        Object.keys(c).length > 3 ? send([c]) : dlg.close();
      }, "primary"]]);
  }
  function listPicker(id, chosen){
    const opts = A.data.rows.filter(x => !x.customer_list && x.kind !== "combined").map(x => `<option ${chosen && chosen.includes(x.name) ? "selected" : ""}>${h(x.name)}</option>`).join("");
    return `<select id="${id}" multiple size="6" style="width:100%">${opts}</select>`;
  }
  const picked = id => [...document.getElementById(id).selectedOptions].map(o => o.value);
  function gCombined(){
    showDialog("New combined list", `<p class="hint">Customer lists and other combined lists can't go in. Combine lists: people on all of the first, or any of the second, and none of the third. Lists made in this same push can be combined from the next push. Members are set now: later edits change only the name, description and days.</p>
      <div class="row"><label>Name</label><input id="a_name"></div>
      <div class="row"><label>On all of</label>${listPicker("a_all")}</div>
      <div class="row"><label>On any of</label>${listPicker("a_any")}</div>
      <div class="row"><label>Not on</label>${listPicker("a_none")}</div>${noteRow}`,
      [["Add to draft", () => {
        const c = {type: "user_list", action: "create", kind: "combined", name: v("a_name")};
        for (const k of ["all", "any", "none"]){ const p = picked("a_" + k); if (p.length) c[k] = p }
        if (!c.name) return toast("Give the list a name");
        send([c]);
      }, "primary"]]);
  }
  function gAttach(r){
    const camps = (S.tree || []).map(c => `<option>${h(c.name)}</option>`).join("");
    showDialog(`Attach "${r.name}"`, `<p class="hint">Adds the list to a campaign. Observation = report and bid only; Targeting = only these people (can cut reach). To detach, use Remove on the campaign's Audiences tab or below.</p>
      <div class="row"><label>Campaign</label><select id="a_camp">${camps}</select></div>
      <label class="tick"><input type="checkbox" id="a_detach"> <span>Detach it from this campaign instead</span></label>${noteRow}`,
      [["Add to draft", () => send([{type: "audience", campaign: v("a_camp"), lists: [r.name], ...(checked("a_detach") ? {remove: true} : {})}]), "primary"]]);
  }

  // ---- Meta ----
  function mWebsite(){
    showDialog("New website audience", `<p class="hint">People the pixel saw on matching pages or firing an event (for example SelectCabin). Created on Meta only when you push live. Rules and retention are set now: later edits change only the name and description.</p>
      <div class="row"><label>Name</label><input id="a_name"></div>
      <div class="row"><label>People who match</label><select id="a_match"><option value="any">any rule</option><option value="all">every rule (same visit)</option></select></div>
      ${ruleBox("a_inc", META_OPS)}<p class="hint">Leave out (optional):</p>${ruleBox("a_exc", META_OPS)}
      <div class="row"><label>Retention (days)</label><input id="a_days" type="number" min="1" max="180" value="30"> <span class="hint">1-180</span></div>
      <label class="tick"><input type="checkbox" id="a_pre" checked> <span>Include people from before today (up to the retention)</span></label>
      <div class="row"><label>Description</label><input id="a_desc"></div>${noteRow}`,
      [["Add to draft", () => {
        if (!v("a_name")) return toast("Give the audience a name");
        send([{type: "meta_audience", action: "create", kind: "website", name: v("a_name"), rules: readRules("a_inc", true), exclude: readRules("a_exc", true),
               match: v("a_match"), retention_days: +v("a_days"), prefill: checked("a_pre"), ...(v("a_desc") ? {description: v("a_desc")} : {})}]);
      }, "primary"]]);
    wireRuleButtons(META_OPS);
  }
  // Editing an existing audience: name and description only. Its rule and retention are shown read-only.
  async function mEdit(r){
    let cur;
    try { cur = await api("/api/audiences/detail?" + qs({acct: S.acct, id: r.id})) } catch(e) { return toast(e.message) }
    showDialog(`Edit audience: ${r.name}`, `<p class="hint">Rule: ${h(cur.rule || r.lookalike || "-")}${cur.retention_days ? ` · Retention: ${h(cur.retention_days)} days` : ""}</p>
      <p class="hint">Rules, retention and sources can't be changed on an existing audience; make a new audience for other rules.</p>
      <div class="row"><label>Name</label><input id="a_name" value="${h(cur.name)}"></div>
      <div class="row"><label>Description</label><input id="a_desc" value="${h(cur.description)}"></div>${noteRow}`,
      [["Save to draft", () => {
        if (!v("a_name")) return toast("Give the audience a name");
        const c = {type: "meta_audience", action: "edit", id: r.id};
        if (v("a_name") !== cur.name) c.name = v("a_name");
        if (v("a_desc") !== (cur.description || "")) c.description = v("a_desc");
        Object.keys(c).length > 3 ? send([c]) : dlg.close();
      }, "primary"]]);
  }
  function mEngagement(){
    const srcOpts = Object.entries(ENG).map(([k, x]) => `<option value="${k}">${h(x[0])}</option>`).join("");
    showDialog("New engagement audience", `<p class="hint">People who interacted with your Page, Instagram account or lead forms. Created on Meta only when you push live.</p>
      <div class="row"><label>Name</label><input id="a_name"></div>
      <div class="row"><label>Source</label><select id="a_src">${srcOpts}</select></div>
      <div class="row"><label>People who</label><select id="a_ev"></select></div>
      <div class="row" id="a_forms_row" style="display:none"><label>Lead form ids</label><input id="a_forms" placeholder="from the Lead forms tab"></div>
      <div class="row"><label>Retention (days)</label><input id="a_days" type="number" min="1" max="730" value="365"> <span class="hint" id="a_dayshint">1-730</span></div>
      <p class="hint">Video viewer audiences are not available yet.</p>${noteRow}`,
      [["Add to draft", () => {
        if (!v("a_name")) return toast("Give the audience a name");
        const src = v("a_src");
        send([{type: "meta_audience", action: "create", kind: "engagement", name: v("a_name"), source: src, event: v("a_ev"), retention_days: +v("a_days"),
               ...(src === "lead_form" ? {form_ids: v("a_forms").split(/[ ,]+/).filter(Boolean)} : {})}]);
      }, "primary"]]);
    const fill = () => { const s = v("a_src"); document.getElementById("a_ev").innerHTML = Object.entries(ENG[s][1]).map(([k, l]) => `<option value="${k}">${h(l)}</option>`).join("");
      document.getElementById("a_forms_row").style.display = s === "lead_form" ? "" : "none";
      const max = s === "lead_form" ? 90 : 730, d = document.getElementById("a_days"); d.max = max; if (+d.value > max) d.value = max;
      document.getElementById("a_dayshint").textContent = "1-" + max };
    document.getElementById("a_src").onchange = fill; fill();
  }
  function mLookalike(seedId){
    const seeds = A.data.rows.filter(x => x.seed_ok).map(x => `<option value="${h(x.id)}" ${x.id === seedId ? "selected" : ""}>${h(x.name)} (${h(x.size)})</option>`).join("");
    showDialog("New lookalike", `<p class="hint">People like the seed audience. Studio never sends a country: the ad set's location decides where it is used. The seed needs at least 100 people. Customer lists can't be seeds here.</p>
      <div class="row"><label>Name</label><input id="a_name"></div>
      <div class="row"><label>Seed</label><select id="a_seed">${seeds}</select></div>
      <div class="row"><label>Size (%)</label><span><input id="a_from" type="number" min="0" max="9" value="0" style="width:70px"> to <input id="a_to" type="number" min="1" max="10" value="1" style="width:70px"></span></div>
      <p class="hint">1% is closest to the seed; 10% is broadest. "From" above 0 makes a band (for example 1-3%).</p>
      <p class="hint" id="a_seedwarn"></p>${noteRow}`,
      [["Add to draft", () => {
        if (!v("a_name")) return toast("Give the lookalike a name");
        const from = +v("a_from"), to = +v("a_to");
        send([{type: "meta_audience", action: "create", kind: "lookalike", name: v("a_name"), seed_id: v("a_seed"), ...(from ? {from, to} : {ratio: to})}]);
      }, "primary"]]);
    const warn = () => { const s = byId(v("a_seed")); document.getElementById("a_seedwarn").textContent = s && /small|not available/.test(s.size) ? `Seed size: ${s.size}. Meta may refuse a seed under 100 people.` : "" };
    document.getElementById("a_seed").onchange = warn; warn();
  }
  function mAttach(r){
    const sets = (S.tree || []).flatMap(c => (c.ad_groups || []).filter(g => !String(g.id).startsWith("d-")).map(g => `<option value="${h(g.id)}">${h(c.name)} / ${h(g.name)}</option>`)).join("");
    showDialog(`Use "${r.name}" in an ad set`, `<p class="hint">Include = only these people (with the ad set's other targeting); exclude = leave them out. Leaving an audience out widens reach and needs the spend tick.</p>
      <div class="row"><label>Ad set</label><select id="a_set">${sets}</select></div>
      <div class="row"><label>Change</label><select id="a_how"><option value="audiences_add">Include it</option><option value="excluded_add">Exclude it</option>
        <option value="audiences_remove">Stop including it</option><option value="excluded_remove">Stop excluding it</option></select></div>${noteRow}`,
      [["Add to draft", () => send([{type: "meta_targeting", level: "adset", id: v("a_set"), [v("a_how")]: [r.id]}]), "primary"]]);
  }

  function remove(r){
    if (r.used_by.length){
      showDialog(`Can't remove "${r.name}"`, `<p>It is in use. Detach it from these first:</p><ul>${r.used_by.map(u => `<li>${h(u)}</li>`).join("")}</ul>`, [["Close", () => dlg.close()]]);
      return;
    }
    showDialog(`Remove "${r.name}"?`, `<p>This goes into the draft. Removing is final: the people collected so far are gone and undo can't bring it back.</p>${noteRow}`,
      [["Add to draft", () => send([google() ? {type: "user_list", action: "remove", list: r.name} : {type: "meta_audience", action: "remove", id: r.id}]), "danger"]]);
  }

  async function add(kind){
    try {
      if (!A.data) await load();
      if (google()) return kind === "combined" ? gCombined() : gWebsite();
      return ({website: mWebsite, engagement: mEngagement, lookalike: () => mLookalike()}[kind || "website"])();
    } catch(e) { toast(e.message) }
  }

  async function render(grid){
    let el = grid.querySelector(":scope > .audiences");
    if (!el){ grid.innerHTML = `<div class="audiences"></div>`; el = grid.firstElementChild }
    el.onclick = async e => {
      const b = e.target.closest("button"); if (!b) return;
      try {
        if (b.dataset.aReload){ A.data = null; draw(el); await load(); return draw(el) }
        if (b.dataset.aNew) return add(b.dataset.aNew);
        const r = byId(b.dataset.aEdit || b.dataset.aAttach || b.dataset.aLal || b.dataset.aRm); if (!r) return;
        if (b.dataset.aEdit) return google() ? gEdit(r) : mEdit(r);
        if (b.dataset.aAttach) return google() ? gAttach(r) : mAttach(r);
        if (b.dataset.aLal) return mLookalike(r.id);
        if (b.dataset.aRm) return remove(r);
      } catch(err) { toast(err.message) }
    };
    if (A.acct !== S.acct){ A.acct = S.acct; A.data = null; A.q = "" }
    if (!A.data){ draw(el); try { await load() } catch(e) { el.innerHTML = `<div class="empty">${h(e.message)}</div>`; return } }
    draw(el);
  }
  window.Audiences = {render, add};
})();
