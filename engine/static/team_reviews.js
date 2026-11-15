// Team reviews (team_reviews.py): the Reviews button in the top bar (reviews waiting for you), the review screen
// (items with old / new lines, per-item and review comments, Approve / Request changes), Send for approval, the
// approval rules and owners (admins), and window.TeamReviews for each area's Push / Publish / Send / Schedule dialog:
// section(plan) shows what needs approval, who the changes are by (only mine / all approved) and the one-line why;
// extra() adds {why, scope} to the push call. Nothing here pushes anything: live actions stay the area's own buttons.
// Only active in a team: section() is "" and extra() is {} for a solo Studio.
(function(){
  const PRESS = (document.querySelector('meta[name="studio-press"]') || {}).content || "";
  const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
  async function call(path, body){
    const r = await fetch(path, body ? {method:"POST", headers:{"Content-Type":"application/json", "X-Studio-Press":PRESS}, body:JSON.stringify(body)} : {});
    const j = await r.json(); if (!r.ok) throw new Error(j.error || r.statusText); return j;
  }
  const R = "/api/team/reviews/";
  const get = (name, q) => call(R + name + (q ? "?" + new URLSearchParams(q).toString() : ""));
  const post = (name, body) => call(R + name, body || {});
  const say = m => (typeof toast === "function" ? toast(m) : alert(m));
  const col = c => /^#[0-9a-fA-F]{6}$/.test(String(c || "")) ? c : "#888888"; // only a real colour goes into style=
  const AREA = {ads:"Ads", email:"Email", seo:"SEO", social:"SoMe", multi:"Several areas"};
  const STATUS = {open:"Waiting", approved:"Approved", changes_requested:"Changes requested", withdrawn:"Withdrawn"};

  const css = `
  #trBtn{position:relative} #trBtn .tr-n{display:none;min-width:16px;padding:0 5px;border-radius:9px;font-size:11px;line-height:16px;background:var(--accent,#1a73e8);color:#fff;margin-left:4px}
  #trBtn .tr-n.on{display:inline-block}
  #trDlg{border:1px solid var(--line,#dadce0);border-radius:8px;padding:0;max-width:900px;width:calc(100% - 32px);background:var(--panel,#fff);color:var(--text,#202124)}
  #trDlg::backdrop{background:#0004}
  .tr-h{display:flex;align-items:center;gap:10px;padding:14px 18px;border-bottom:1px solid var(--line,#dadce0);font-weight:500}
  .tr-h .sp{flex:1} .tr-b{padding:12px 18px;max-height:72vh;overflow:auto} .tr-f{display:flex;gap:8px;justify-content:flex-end;padding:10px 18px;border-top:1px solid var(--line,#dadce0)}
  .tr-tabs{display:flex;gap:6px;margin-bottom:10px;flex-wrap:wrap} .tr-tabs button.on{background:var(--accent,#1a73e8);color:#fff;border-color:transparent}
  .tr-row{border:1px solid var(--line,#dadce0);border-radius:6px;padding:8px 10px;margin-bottom:8px;cursor:pointer}
  .tr-row:hover{background:var(--hover,#f1f3f4)} .tr-meta{font-size:12px;color:var(--muted,#5f6368)}
  .tr-chip{display:inline-block;font-size:11px;border-radius:9px;padding:0 7px;margin-left:6px;background:var(--line2,#e8eaed);color:var(--text2,#3c4043)}
  .tr-chip.approved{background:#e6f4ea;color:#137333} .tr-chip.changes_requested,.tr-chip.stale{background:#fce8e6;color:#c5221f} .tr-chip.open{background:#e8f0fe;color:#1967d2}
  .tr-item{border:1px solid var(--line,#dadce0);border-radius:6px;padding:8px 10px;margin:8px 0}
  .tr-ln{display:grid;grid-template-columns:150px 1fr;gap:8px;font-size:13px;padding:3px 0;border-top:1px dashed var(--line,#dadce0)}
  .tr-ln span.old{text-decoration:line-through;color:var(--muted,#5f6368)} .tr-ln .new{color:var(--text,#202124)} .tr-ln div{min-width:0;word-break:break-word;white-space:pre-wrap}
  .tr-cm{font-size:13px;margin:6px 0 0;padding-left:8px;border-left:3px solid var(--line,#dadce0)} .tr-cm p{margin:3px 0}
  .tr-cm textarea,.tr-note{width:100%;min-height:52px;box-sizing:border-box;font:13px/1.4 inherit}
  .tr-sec{border:1px solid var(--line,#dadce0);border-radius:6px;padding:8px 10px;margin:10px 0;font-size:13px}
  .tr-sec .tr-needs{margin:4px 0 8px} .tr-sec label{display:flex;gap:6px;align-items:baseline;margin:2px 0}
  .tr-sec .tr-why{width:100%;box-sizing:border-box;margin-top:4px} .tr-warn{color:#c5221f}
  .tr-dot{display:inline-block;width:9px;height:9px;border-radius:50%;margin-right:5px}
  #trOwner{position:fixed;left:16px;bottom:16px;z-index:50;background:var(--panel,#fff);border:1px solid var(--line,#dadce0);border-radius:6px;padding:6px 10px;font-size:13px;box-shadow:0 2px 8px #0002;display:none}
  @media (max-width:640px){.tr-ln{grid-template-columns:1fr}}`;
  document.head.insertAdjacentHTML("beforeend", `<style>${css}</style>`);
  document.body.insertAdjacentHTML("beforeend", `<dialog id="trDlg" aria-label="Reviews"></dialog><div id="trOwner" role="status"></div>`);
  const dlg = document.getElementById("trDlg");
  let ST = {joined:false}, TAB = "to_me", WHY = "", SCOPE = "all";

  function open(title, body, buttons){
    dlg.innerHTML = `<div class="tr-h"><span>${esc(title)}</span><span class="sp"></span><button data-x>Close</button></div><div class="tr-b">${body}</div>`
      + (buttons && buttons.length ? `<div class="tr-f">${buttons.map((b, i) => `<button data-b="${i}" class="${esc(b[2] || "")}">${esc(b[0])}</button>`).join("")}</div>` : "");
    dlg.querySelector("[data-x]").onclick = () => dlg.close();
    (buttons || []).forEach((b, i) => dlg.querySelector(`[data-b="${i}"]`).onclick = b[1]);
    if (!dlg.open) dlg.showModal();
  }

  // ---------- top bar ----------
  const btn = document.createElement("button");
  btn.id = "trBtn"; btn.className = "tb"; btn.style.display = "none"; btn.title = "Reviews: approvals asked of you and your own requests";
  btn.innerHTML = `Reviews<span class="tr-n"></span>`;
  const anchor = document.getElementById("setupBtn");
  if (anchor && anchor.parentNode) anchor.parentNode.insertBefore(btn, anchor);
  btn.onclick = () => showList(TAB);
  async function refresh(){
    try { ST = await call("/api/team/status") } catch(e) { return }
    btn.style.display = ST.joined && !ST.removed ? "" : "none";
    if (!ST.joined) return;
    try { const c = await get("counts"); const n = btn.querySelector(".tr-n"); n.textContent = c.to_me || ""; n.classList.toggle("on", !!c.to_me) } catch(e) {}
  }
  refresh(); setInterval(refresh, 60000); window.addEventListener("focus", refresh);

  // ---------- review list and screen ----------
  const when = iso => iso ? new Date(iso).toLocaleString("en-GB", {day:"numeric", month:"short", hour:"2-digit", minute:"2-digit"}) : "";
  const chip = (cls, text) => `<span class="tr-chip ${esc(cls)}">${esc(text)}</span>`;
  async function showList(tab){
    TAB = tab || TAB;
    open("Reviews", `<p class="hint">Loading ...</p>`, []);
    const isAdmin = ST.me && ["owner", "admin"].includes(ST.me.role);
    const tabs = [["to_me", "Waiting for you"], ["mine", "My requests"], ["all", "All"], ["rules", "Approval rules and owners"]];
    const head = `<div class="tr-tabs">${tabs.map(([k, l]) => `<button data-tab="${k}" class="${TAB === k ? "on" : ""}">${esc(l)}</button>`).join("")}</div>`;
    let body = "";
    try {
      if (TAB === "rules") body = await rulesHTML(isAdmin);
      else {
        const r = await get("list", {filter: TAB});
        const cs = r.changeset || {};
        body = (TAB === "mine" && ST.can_send !== false ? `<p><button id="trSendCs">Send my changeset "${esc(cs.name || "")}" for approval</button></p>` : "")
          + (r.reviews.length ? r.reviews.map(v => `<div class="tr-row" data-rv="${v.id}"><b>${esc(v.title || "Review " + v.id)}</b>
              ${chip(v.status, STATUS[v.status] || v.status)}${v.stale && v.status !== "withdrawn" ? chip("stale", "changed since asked") : ""}
              <div class="tr-meta">${esc(AREA[v.area] || v.area)} · ${v.items.length} item${v.items.length === 1 ? "" : "s"} · asked by ${esc(v.requested_by_name)}
              of ${esc(v.reviewer_names.join(", "))} · ${esc(when(v.at))}${v.decided_by_name ? ` · ${esc(STATUS[v.status])} by ${esc(v.decided_by_name)}` : ""}${v.comments ? ` · ${v.comments} comment${v.comments === 1 ? "" : "s"}` : ""}</div></div>`).join("")
            : `<p class="hint">${TAB === "to_me" ? "Nothing waiting for you." : "No reviews yet."}</p>`);
      }
    } catch(e) { body = `<p class="tr-warn">${esc(e.message)}</p>` }
    open("Reviews", head + body, []);
    dlg.querySelectorAll("[data-tab]").forEach(b => b.onclick = () => showList(b.dataset.tab));
    dlg.querySelectorAll("[data-rv]").forEach(b => b.onclick = () => showReview(+b.dataset.rv));
    const sc = document.getElementById("trSendCs"); if (sc) sc.onclick = () => requestDialog({changeset:true}, "your changeset");
    if (TAB === "rules") wireRules(isAdmin);
  }
  // Every field in full: long text folds behind an expander (never cut), HTML shows rendered in a sandbox (no scripts,
  // no same-origin) with its source a click away. What the approver confirms is exactly this set.
  const long = (t, cls) => t.length > 600 || t.split("\n").length > 12
    ? `<details><summary>${esc(t.slice(0, 140).replace(/\s+/g, " "))} ... (${t.length.toLocaleString()} characters: show all)</summary><span class="${cls}">${esc(t)}</span></details>`
    : `<span class="${cls}">${esc(t)}</span>`;
  const html = (t, cls) => t ? `<iframe sandbox="" referrerpolicy="no-referrer" srcdoc="${esc(t)}" style="width:100%;height:320px;border:1px solid var(--line,#dadce0);border-radius:4px;background:#fff"></iframe>
    <details><summary>Source (${t.length.toLocaleString()} characters)</summary><span class="${cls}">${esc(t)}</span></details>` : "";
  function linesHTML(lines){
    return (lines || []).map(l => { const show = l.kind === "html" ? html : long;
      return `<div class="tr-ln"><div>${esc(l.label)}</div><div>${l.old ? `<div class="old">${l.kind === "html" ? "Before:" : ""}${show(l.old, "old")}</div>` : ""}<div>${l.old && l.kind === "html" ? "After:" : ""}${show(l.new, "new")}</div></div></div>` }).join("");
  }
  async function commentsHTML(coll, id){
    try {
      const c = await get("comments", {coll, id});
      return c.comments.map(x => `<p><b>${esc(x.author_name)}</b> <span class="tr-meta">${esc(when(x.at))}</span><br>${esc(x.body)}</p>`).join("") || `<p class="tr-meta">No comments yet.</p>`;
    } catch(e) { return `<p class="tr-warn">${esc(e.message)}</p>` }
  }
  async function showReview(id){
    open("Review", `<p class="hint">Loading ...</p>`, []);
    let v; try { v = await get("view", {id}) } catch(e) { return open("Review", `<p class="tr-warn">${esc(e.message)}</p>`, [["Back", () => showList()]]) }
    const items = v.items.map((it, i) => `<div class="tr-item"><b>${esc(it.label)}</b> <span class="tr-meta">${esc(AREA[it.area] || it.area)}</span>
        ${it.stale ? chip("stale", "changed since asked") : ""}${!it.stale && !it.local_matches ? chip("stale", "your copy differs: sync") : ""}
        ${linesHTML(it.lines)}<div class="tr-cm" data-cm="${i}"><a href="#" data-cmo="${i}">Comments on this item</a></div></div>`).join("");
    const body = `<p class="tr-meta">${esc(AREA[v.area] || v.area)} · asked by ${esc(v.requested_by_name)} of ${esc(v.reviewer_names.join(", "))} · ${esc(when(v.at))} ${chip(v.status, STATUS[v.status] || v.status)}</p>
      ${v.note ? `<p><b>Note:</b> ${esc(v.note)}</p>` : ""}${v.decision_note ? `<p><b>${esc(v.decided_by_name)}:</b> ${esc(v.decision_note)}</p>` : ""}
      ${v.stale ? `<p class="tr-warn">Some items changed after the request, so this review can't be approved: ask for changes, or the requester asks again.</p>`
        : v.items.some(it => !it.local_matches) ? `<p class="tr-warn">What you see here is not what was asked about (your copy is not in sync, or it changed on the platform). Sync, then open the review again.</p>` : ""}
      ${items}<h4 style="margin:12px 0 4px">Discussion</h4><div class="tr-cm" id="trThread">${await commentsHTML("review", String(v.id))}
      <textarea id="trThreadIn" placeholder="Add a comment"></textarea><button id="trThreadSend">Comment</button></div>
      ${v.can_decide ? `<h4 style="margin:12px 0 4px">Your decision</h4><textarea class="tr-note" id="trDecNote" placeholder="A note for ${esc(v.requested_by_name)} (optional)"></textarea>` : ""}`;
    const btns = [["Back", () => showList()]];
    const admin = ST.me && ["owner", "admin"].includes(ST.me.role); // a request for changes holds the content until it changes
    if ((v.mine || admin) && v.status !== "withdrawn" && (v.status !== "changes_requested" || admin)) btns.push(["Withdraw", async () => { try { await post("withdraw", {id:v.id}); say("Withdrawn"); showList() } catch(e) { say(e.message) } }, "danger"]);
    if (v.can_decide){
      const shown = v.items.map(it => ({coll:it.coll, id:it.id, hash:it.shown_hash})); // Approve covers exactly what this screen shows
      const decide = d => async () => { try { const r = await post("decide", {id:v.id, decision:d, shown, note:(document.getElementById("trDecNote") || {}).value || ""});
        if (r.refused) return say(r.refused); say(d === "approve" ? "Approved" : "Changes requested"); refresh(); showList() } catch(e) { say(e.message) } };
      btns.push(["Request changes", decide("request_changes")]);
      if (!v.stale && v.items.every(it => it.local_matches)) btns.push(["Approve", decide("approve"), "primary"]);
    }
    open(v.title || "Review", body, btns);
    document.getElementById("trThreadSend").onclick = async () => {
      const t = document.getElementById("trThreadIn"); if (!t.value.trim()) return;
      try { await post("comment", {coll:"review", id:String(v.id), body:t.value}); showReview(v.id) } catch(e) { say(e.message) } };
    dlg.querySelectorAll("[data-cmo]").forEach(a => a.onclick = async e => {
      e.preventDefault(); const it = v.items[+a.dataset.cmo], box = dlg.querySelector(`[data-cm="${a.dataset.cmo}"]`);
      box.innerHTML = await commentsHTML(it.coll, it.id) + `<textarea placeholder="Comment on this item"></textarea><button>Comment</button>`;
      box.querySelector("button").onclick = async () => { const t = box.querySelector("textarea"); if (!t.value.trim()) return;
        try { await post("comment", {coll:it.coll, id:it.id, body:t.value}); box.innerHTML = await commentsHTML(it.coll, it.id) } catch(err) { say(err.message) } };
    });
  }

  // ---------- Send for approval ----------
  async function requestDialog(spec, what){
    let rv; try { rv = (await get("reviewers")).reviewers } catch(e) { return say(e.message) }
    if (!rv.length) return say("Nobody in the team can approve yet: an admin can tick Approver on a member");
    open("Send for approval", `<p>Send ${esc(what || "these changes")} for approval. Studio syncs first, so the reviewers see exactly this version.</p>
      <p><b>Reviewers</b></p>${rv.map(m => `<label style="display:flex;gap:6px;align-items:center"><input type="checkbox" value="${m.id}"> <span class="tr-dot" style="background:${col(m.colour)}"></span>${esc(m.name)} <span class="tr-meta">${esc(m.role)}${m.approver ? ", approver" : ""}</span></label>`).join("")}
      <p><b>Note</b></p><textarea class="tr-note" id="trReqNote" maxlength="500" placeholder="What should they look at?"></textarea>`,
      [["Cancel", () => dlg.close()], ["Send for approval", async () => {
        const ids = [...dlg.querySelectorAll("input[type=checkbox]:checked")].map(x => +x.value);
        if (!ids.length) return say("Pick at least one reviewer");
        try { const r = await post("request", {...spec, reviewers:ids, note:document.getElementById("trReqNote").value});
          dlg.close(); say(`Sent for approval: ${r.title}`); refresh() } catch(e) { say(e.message) } }, "primary"]]);
  }

  // ---------- approval rules and owners ----------
  async function rulesHTML(isAdmin){
    const [p, o] = await Promise.all([get("policy"), get("owners")]);
    const dis = isAdmin ? "" : "disabled";
    const own = Object.entries(o.owners || {});
    return `<h4 style="margin:0 0 4px">Approval needed before going live</h4><p class="hint">When on, Push live / Publish / Send / Schedule in that area only take changes with a current approval.${isAdmin ? "" : " Only admins and owners can change this."}</p>
      ${["ads", "email", "seo", "social"].map(a => `<label style="display:flex;gap:6px;align-items:center"><input type="checkbox" data-pol="${a}" ${p.require_approval[a] ? "checked" : ""} ${dis}> ${AREA[a]}</label>`).join("")}
      <h4 style="margin:14px 0 4px">Owners</h4><p class="hint">Someone who edits in an owned area sees "Owned by ..." (a note, nothing is blocked). Scopes look like ads:&lt;account&gt;/campaign/&lt;campaign&gt;, email/&lt;campaign id&gt;, seo/post/&lt;post id&gt;, seo:map_nodes/&lt;node id&gt;, social/&lt;post id&gt;.</p>
      ${own.length ? own.map(([s, m]) => `<div class="tr-ln"><div>${esc(s)}</div><div><span class="tr-dot" style="background:${col(m.colour)}"></span>${esc(m.name)} ${isAdmin ? `<a href="#" data-unown="${esc(s)}">remove</a>` : ""}</div></div>`).join("") : `<p class="tr-meta">No owners set.</p>`}
      ${isAdmin ? `<div style="display:flex;gap:6px;margin-top:8px;flex-wrap:wrap"><input id="trOwnScope" placeholder="scope" style="flex:1;min-width:180px"><select id="trOwnWho">${Object.entries(ST.members || {}).map(([id, m]) => `<option value="${esc(id)}">${esc(m.name)}</option>`).join("")}</select><button id="trOwnAdd">Set owner</button></div>` : ""}`;
  }
  function wireRules(isAdmin){
    if (!isAdmin) return;
    dlg.querySelectorAll("[data-pol]").forEach(c => c.onchange = async () => {
      try { await post("policy", {require_approval:{[c.dataset.pol]: c.checked}}); say("Saved") } catch(e) { say(e.message); c.checked = !c.checked } });
    dlg.querySelectorAll("[data-unown]").forEach(a => a.onclick = async e => { e.preventDefault();
      try { await post("owners", {owners:{[a.dataset.unown]: null}}); showList("rules") } catch(err) { say(err.message) } });
    const add = document.getElementById("trOwnAdd");
    if (add) add.onclick = async () => { const s = document.getElementById("trOwnScope").value.trim(); if (!s) return;
      try { await post("owners", {owners:{[s]: +document.getElementById("trOwnWho").value}}); showList("rules") } catch(e) { say(e.message) } };
  }

  // ---------- owners: a gentle note while editing ----------
  const noted = new Set();
  async function ownerNote(scopes){
    if (!ST.joined || !scopes || !scopes.length) return;
    const key = scopes.join("|"); if (noted.has(key)) return; noted.add(key);
    try {
      const o = await get("owner_note", {scopes:key});
      if (!o.owner) return;
      const el = document.getElementById("trOwner");
      el.innerHTML = `<span class="tr-dot" style="background:${col(o.colour)}"></span>${esc(o.note)}`;
      el.style.display = "block"; clearTimeout(el._t); el._t = setTimeout(() => { el.style.display = "none" }, 6000);
    } catch(e) {}
  }

  // ---------- the push section each area's dialog shows ----------
  function section(t){
    if (!t || !t.joined) return "";
    const needs = t.needs || [], people = t.people || [];
    const groups = {};
    needs.forEach(n => { const g = n.group_label || ""; (groups[g] = groups[g] || []).push(n) });
    const needsHTML = needs.length ? `<div class="tr-needs"><b>${needs.length} change${needs.length === 1 ? "" : "s"} need${needs.length === 1 ? "s" : ""} approval</b> (left out of this push)
      ${Object.entries(groups).map(([g, ns]) => `${g ? `<div class="tr-meta" style="margin-top:4px">${esc(g)}</div>` : ""}${ns.map(n => `<label><input type="checkbox" data-trn="${esc(n.coll)}" data-tri="${esc(n.id)}" checked> <span>${esc(n.label)} <span class="tr-meta">${esc(n.reason)}${n.person && n.person !== "me" ? " · by " + esc(n.person) : ""}</span></span></label>`).join("")}`).join("")}
      <button data-trsend style="margin-top:4px">Send for approval</button></div>` : "";
    const others = people.filter(p => !p.mine);
    const who = people.length ? `Includes ${people.map(p => `${p.count} change${p.count === 1 ? "" : "s"} by ${esc(p.name)}`).join(", ")}.` : "";
    const scope = others.length || t.scope === "mine" ? `<div style="margin:4px 0">${who}
      <label><input type="radio" name="trScope" value="all" ${t.scope !== "mine" ? "checked" : ""}> ${t.required ? "All approved" : "Everything"}</label>
      <label><input type="radio" name="trScope" value="mine" ${t.scope === "mine" ? "checked" : ""}> Only mine</label></div>` : (who ? `<div>${who}</div>` : "");
    return `<div class="tr-sec">${t.required ? `<div><b>Your team asks for an approval before this goes out.</b></div>` : ""}
      ${t.error ? `<div class="tr-warn">${esc(t.error)}</div>` : ""}${needsHTML}${scope}
      ${t.why_required ? `<label for="trWhy" style="display:block;margin-top:6px"><b>Why</b> (one line, your team sees it in the activity feed)</label><input id="trWhy" class="tr-why" maxlength="200" value="${esc(WHY)}" placeholder="e.g. Spring sale prices">` : ""}</div>`;
  }
  function wire(root, onScope, onChange){
    root = root || document;
    const w = root.querySelector("#trWhy");
    if (w) w.oninput = () => { WHY = w.value; onChange && onChange() };
    root.querySelectorAll("input[name=trScope]").forEach(r => r.onchange = () => { SCOPE = r.value; onScope && onScope() });
    const s = root.querySelector("[data-trsend]");
    if (s) s.onclick = () => {
      const items = [...root.querySelectorAll("[data-trn]:checked")].map(c => ({coll:c.dataset.trn, id:c.dataset.tri}));
      if (!items.length) return say("Tick the changes to send for approval");
      requestDialog({items}, `${items.length} change${items.length === 1 ? "" : "s"}`);
    };
  }
  const extra = () => ST.joined ? {why:WHY, scope:SCOPE} : {};
  const ready = t => !t || !t.joined || !t.why_required || !!WHY.trim();
  async function gate(area, coll, id){ if (!ST.joined) return null; try { return await get("gate", {area, coll, id, scope:SCOPE}) } catch(e) { return {joined:true, why_required:true, error:e.message, needs:[], people:[]} } }

  window.TeamReviews = {section, wire, extra, ready, gate, ownerNote, requestDialog, open:showList, setWhy:w => { WHY = String(w || "") },
                        get joined(){ return !!ST.joined }, refresh};
})();
