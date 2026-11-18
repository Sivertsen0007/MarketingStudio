// Team marks: what a teammate is doing, shown quietly inside every area. Only active when this Studio is in a team
// (/api/team/live/state says joined); a solo user sees nothing of this.
//   tints   items with a pending draft change by someone else get a soft wash in that member's colour, parents a dot
//           (Ads campaigns / ad groups, topical-map hubs). Hover: "Alex changed Headline 2 · 10:14" (+ old -> new).
//   locks   an editor open on an item a teammate is editing shows "Alex is editing this (since 10:14)" with
//           Edit anyway / Watch. Never blocks. Fields a teammate changed show their value inline.
//   claims  Claim / Release (topical-map nodes, email campaigns, SoMe posts, ad campaigns): "Alex is writing this".
//   comments an icon with the open-thread count on every item, a thread panel (replies, Resolve, edit own, @mentions).
//   presence each area reports where you are and what you have open (every 30 s while the window is visible).
//   bell    unread notifications in the top bar (team_notify.js lists them; unread @mentions on an older server).
// Areas mark an open editor with data-team-coll / data-team-id (data-team-label, data-team-bar="before"); lists and
// trees are found by the attributes the areas already use. Everything from the server is escaped before it is shown.
(function(){
  const PRESS = (document.querySelector('meta[name="studio-press"]') || {}).content || "";
  const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
  const say = m => (typeof toast === "function" ? toast(m) : alert(m));
  const HEX = /^#[0-9a-fA-F]{6}$/;
  async function get(name, q){
    const r = await fetch("/api/team/live/" + name + (q ? "?" + new URLSearchParams(q) : ""));
    const j = await r.json(); if (!r.ok) throw new Error(j.error || r.statusText); return j;
  }
  async function post(name, body){
    const r = await fetch("/api/team/live/" + name, {method:"POST", headers:{"Content-Type":"application/json", "X-Studio-Press":PRESS}, body:JSON.stringify(body || {})});
    const j = await r.json(); if (!r.ok) throw new Error(j.error || r.statusText); return j;
  }

  const TM = window.TeamMarks = {st:{joined:false}, marks:{}, counts:{}, claims:[], presence:[], unread:0, kinds:{},
    since:{}, anyway:{}, watch:{}, listeners:[], get, post, esc};
  const colour = c => HEX.test(c || "") ? c : "#888888";
  const hexA = (h, a) => { h = colour(h); return `rgba(${parseInt(h.slice(1,3),16)},${parseInt(h.slice(3,5),16)},${parseInt(h.slice(5,7),16)},${a})` };
  const dot = (c, extra) => `<span class="tmk-d ${extra || ""}" style="background:${colour(c)}"></span>`;
  const meId = () => String(((TM.st || {}).me || {}).id);
  TM.colour = colour; TM.dot = dot;

  function when(iso){
    if (!iso) return "";
    const d = new Date(iso); if (isNaN(d)) return "";
    const hm = d.toLocaleTimeString("en-GB", {hour:"2-digit", minute:"2-digit"});
    return d.toDateString() === new Date().toDateString() ? hm : `${d.getDate()} ${d.toLocaleString("en-GB", {month:"short"})} ${hm}`;
  }
  TM.when = when;
  TM.memberKey = m => String(m.name || "").replace(/\s+/g, "");
  // a comment body: escaped, @mentions of members highlighted
  TM.fmtBody = body => {
    const keys = new Set();
    (TM.st.members || []).forEach(m => { keys.add(TM.memberKey(m).toLowerCase()); if (m.email) keys.add(String(m.email).split("@")[0].toLowerCase()) });
    return esc(body).replace(/@([A-Za-z0-9._+-]+)/g, (all, t) => keys.has(t.toLowerCase()) || keys.has(t.toLowerCase().replace(/[._+-]+$/, "")) ? `<span class="tmk-at">${all}</span>` : all);
  };

  // ---------- styles ----------
  document.head.insertAdjacentHTML("beforeend", `<style>
  .tmk-tint>td{background-image:linear-gradient(var(--tmk-bg),var(--tmk-bg))}
  .tmk-tint>td:first-child{box-shadow:inset 3px 0 0 var(--tmk)}
  .tmk-tintb{background-image:linear-gradient(var(--tmk-bg),var(--tmk-bg))!important;box-shadow:inset 3px 0 0 var(--tmk)}
  .tmk-d{display:inline-block;width:8px;height:8px;border-radius:50%;flex:none;vertical-align:1px}
  .tmk-d.ring{background:transparent!important;border:2px solid currentColor;width:9px;height:9px}
  .tmk-x{margin-left:6px}
  .tmk-cm{display:inline-flex;align-items:center;gap:3px;border:0;background:none;box-shadow:none;padding:0 4px;color:var(--muted);font-size:11px;font-weight:600;cursor:pointer;vertical-align:middle;line-height:18px;border-radius:var(--r-xs)}
  .tmk-cm svg{width:13px;height:13px;fill:currentColor} .tmk-cm.open{color:var(--accent);background:var(--accent-bg)} .tmk-cm:hover{background:var(--hover);color:var(--text)}
  tr:not(:hover) .tmk-cm.zero,.node:not(:hover) .tmk-cm.zero{visibility:hidden}
  .tmk-claim{display:inline-flex;align-items:center;gap:4px;font-size:11px;font-weight:600;color:var(--text2);white-space:nowrap;vertical-align:middle}
  #tmkTip{position:fixed;z-index:200;max-width:380px;padding:7px 10px;border-radius:var(--r-sm);background:var(--ink);color:var(--ink-text);font-size:12px;font-weight:500;line-height:1.45;white-space:pre-line;pointer-events:none;display:none;box-shadow:var(--sh-pop)}
  .tmk-bar{display:flex;flex-wrap:wrap;align-items:center;gap:6px 10px;padding:8px 12px;margin:0 0 10px;border-radius:var(--r-md);background:var(--panel);box-shadow:var(--sh-1);font-size:12px;font-weight:500;color:var(--text2)}
  .tmk-bar.before{margin:0 28px 12px}
  .tmk-bar .sp{flex:1} .tmk-bar button{height:28px;font-size:12px;padding:0 10px}
  .tmk-lock{display:flex;align-items:center;gap:8px;width:100%;padding:6px 10px;border-radius:var(--r-sm);background:var(--warn-bg);color:var(--text2)}
  .tmk-lock .sp{flex:1}
  .tmk-chg{width:100%;display:flex;gap:6px;align-items:baseline}
  .tmk-fh{grid-column:1/-1;font-size:12px;font-weight:500;color:var(--text2);margin:4px 0 0;padding:3px 8px;border-left:3px solid var(--tmk,var(--faint));background:var(--tmk-bg,transparent);border-radius:0 var(--r-xs) var(--r-xs) 0}
  #tmkPanel{position:fixed;top:64px;right:16px;bottom:44px;width:390px;max-width:calc(100% - 32px);z-index:70;display:none;flex-direction:column;background:var(--panel);color:var(--text);border-radius:var(--r-xl);box-shadow:var(--sh-dlg)}
  #tmkPanel.on{display:flex;animation:tmk-in var(--t) var(--ease)}
  @keyframes tmk-in{from{opacity:0;transform:translateX(8px)}to{opacity:1;transform:none}}
  .tmk-ph{display:flex;gap:8px;align-items:center;padding:16px 18px 12px;border-bottom:1px solid var(--line2)} .tmk-ph .t{font-size:16px;font-weight:600;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
  .tmk-ph .sp{flex:1} .tmk-sub{font-size:11.5px;font-weight:500;color:var(--muted)}
  .tmk-pb{flex:1;overflow:auto;padding:12px 18px}
  .tmk-pf{border-top:1px solid var(--line2);padding:12px 18px 16px;position:relative}
  .tmk-pf textarea{min-height:64px;font-size:13px}
  .tmk-pf .acts{display:flex;gap:8px;align-items:center;margin-top:8px}
  .tmk-th{border-radius:var(--r-md);padding:10px 12px;margin-bottom:10px;background:var(--head)}
  .tmk-th.resolved{opacity:.65}
  .tmk-c{margin:0 0 8px} .tmk-c.reply{margin-left:14px;padding-left:10px;border-left:2px solid var(--line)}
  .tmk-c .h{display:flex;gap:6px;align-items:center;font-size:11.5px;font-weight:500;color:var(--muted)} .tmk-c .h b{color:var(--text);font-weight:600}
  .tmk-c .b{white-space:pre-wrap;word-break:break-word;font-size:13px;color:var(--text2);margin:3px 0 0}
  .tmk-c .a{display:flex;gap:10px;font-size:12px;margin-top:4px} .tmk-c .a button{border:0;background:none;box-shadow:none;padding:0;color:var(--accent);cursor:pointer;font-size:12px;font-weight:600}
  .tmk-c .a button:hover{text-decoration:underline}
  .tmk-at{color:var(--accent);background:var(--accent-bg);border-radius:var(--r-xs);padding:0 3px;font-weight:600}
  .tmk-ac{position:absolute;left:18px;bottom:100%;min-width:220px;background:var(--panel);border-radius:var(--r-lg);box-shadow:var(--sh-pop);display:none;padding:6px;z-index:2}
  .tmk-ac.on{display:block} .tmk-ac div{height:32px;padding:0 10px;border-radius:var(--r-sm);cursor:pointer;display:flex;gap:8px;align-items:center;font-size:13px;font-weight:500} .tmk-ac div.on,.tmk-ac div:hover{background:var(--hover)}
  .tmk-ac div.on{background:var(--accent-bg);color:var(--accent)}
  #tmkBell{position:relative;display:none} #tmkBell svg{width:17px;height:17px} #tmkBell.on{display:inline-flex}
  #tmkBell .n{display:none;min-width:18px;padding:0 6px;border-radius:var(--r-pill);font-size:11px;font-weight:700;line-height:18px;text-align:center;background:var(--bad);color:var(--ink-text)}
  #tmkBell .n.on{display:inline-block}
  </style>`);
  document.body.insertAdjacentHTML("beforeend", `<div id="tmkTip" role="tooltip"></div>
    <aside id="tmkPanel" aria-label="Comments"><div class="tmk-ph"><span class="t" id="tmkPT"></span><span class="sp"></span><span id="tmkPClaim"></span><button id="tmkPX">Close</button></div>
    <div class="tmk-pb" id="tmkPB"></div>
    <div class="tmk-pf"><div class="tmk-ac" id="tmkAC"></div><div id="tmkReplyTo" class="tmk-sub"></div><textarea id="tmkTA" placeholder="Comment for the team. Type @ to mention someone."></textarea>
      <div class="acts"><button class="primary" id="tmkSend">Comment</button><span class="tmk-sub">Cmd+Enter sends</span></div></div></aside>`);
  const tip = document.getElementById("tmkTip"), panel = document.getElementById("tmkPanel");
  const CM_SVG = `<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M20 2H4a2 2 0 0 0-2 2v18l4-4h14a2 2 0 0 0 2-2V4a2 2 0 0 0-2-2zm0 14H5.17L4 17.17V4h16v12z"/></svg>`;

  // ---------- hover ----------
  document.addEventListener("mouseover", e => {
    const t = e.target.closest && e.target.closest("[data-tmk-tip]");
    if (!t){ tip.style.display = "none"; return }
    tip.textContent = t.getAttribute("data-tmk-tip");
    tip.style.display = "block";
    const r = t.getBoundingClientRect(), w = tip.offsetWidth, h = tip.offsetHeight;
    tip.style.left = Math.max(8, Math.min(window.innerWidth - w - 8, r.left)) + "px";
    tip.style.top = (r.bottom + h + 8 > window.innerHeight ? r.top - h - 6 : r.bottom + 6) + "px";
  });
  document.addEventListener("scroll", () => { tip.style.display = "none" }, true);

  // ---------- what changed ----------
  function fieldLine(f){
    if (f.old != null && f.old !== "" && f.new != null) return `${f.field}: ${f.old} → ${f.new}`;
    if (f.new != null && f.new !== "") return `${f.field}: ${f.new}`;
    if (f.old != null) return `${f.field}: removed`;
    return f.field;
  }
  function markTip(m){
    const fs = (m.fields || []);
    if (m.added) return [`${m.who.name} added this${m.at ? " · " + when(m.at) : ""}`, ...fs.slice(0, 5).map(fieldLine)].join("\n");
    const head = `${m.who.name} changed ${fs.length ? fs.slice(0, 3).map(f => f.field).join(", ") + (fs.length > 3 ? ` and ${fs.length - 3} more` : "") : "this"}${m.at ? " · " + when(m.at) : ""}`;
    return [head, ...fs.slice(0, 5).map(fieldLine)].join("\n");
  }
  TM.markTip = markTip;
  function tipFor(ms){ return ms.map(markTip).join("\n\n") }
  function setTint(el, ms, cls, pass){
    const m = ms[0];
    el.classList.add(cls || "tmk-tint");
    el.style.setProperty("--tmk", colour(m.who.colour));
    el.style.setProperty("--tmk-bg", hexA(m.who.colour, 0.09));
    el.setAttribute("data-tmk-tip", tipFor(ms));
    el.dataset.tmkPass = pass;
  }
  function clearTints(pass){
    document.querySelectorAll(".tmk-tint,.tmk-tintb").forEach(el => {
      if (el.dataset.tmkPass === String(pass)) return;
      el.classList.remove("tmk-tint", "tmk-tintb"); el.style.removeProperty("--tmk"); el.style.removeProperty("--tmk-bg");
      el.removeAttribute("data-tmk-tip"); delete el.dataset.tmkPass;
    });
  }
  // our own little nodes inside a host: kept when unchanged, replaced when not, dropped when no longer wanted
  function ensure(host, key, html, pass, where){
    if (!host) return;
    let el = [...host.children].find(x => x.classList.contains("tmk-x") && x.dataset.tmkK === key);
    if (!html){ if (el) el.remove(); return null }
    if (el && el.dataset.sig === html){ el.dataset.tmkPass = pass; return el }
    if (el) el.remove();
    const tmp = document.createElement("span"); tmp.innerHTML = html; el = tmp.firstElementChild;
    el.classList.add("tmk-x"); el.dataset.tmkK = key; el.dataset.sig = html; el.dataset.tmkPass = pass;
    if (where === "first") host.insertBefore(el, host.firstChild); else host.appendChild(el);
    return el;
  }
  function sweep(pass){ document.querySelectorAll(".tmk-x").forEach(el => { if (el.dataset.tmkPass !== String(pass)) el.remove() }) }

  const marksIn = coll => (TM.marks || {})[coll] || {};
  const claimOf = (coll, id) => (TM.claims || []).find(c => c.coll === coll && String(c.id) === String(id));
  const countOf = (coll, id) => ((TM.counts[coll] || {})[String(id)] || {open:0, total:0});
  function cmButton(coll, id, label){
    const c = countOf(coll, id), open = Math.max(0, parseInt(c.open, 10) || 0), total = Math.max(0, parseInt(c.total, 10) || 0);
    return `<button class="tmk-cm ${open ? "open" : total ? "" : "zero"}" data-tmk-c="${esc(coll)}" data-tmk-i="${esc(id)}" data-tmk-l="${esc(label || "")}" title="${open ? open + " open comment thread" + (open > 1 ? "s" : "") : total ? "Comments (all resolved)" : "Comment"}">${CM_SVG}${open || ""}</button>`;
  }
  function claimChip(coll, id, short){
    const c = claimOf(coll, id); if (!c) return "";
    const txt = c.mine ? "You are writing this" : `${c.member.name} is writing this`;
    return `<span class="tmk-claim" data-tmk-tip="${esc(txt + (c.note ? ": " + c.note : "") + (c.at ? " (since " + when(c.at) + ")" : ""))}">${dot(c.member.colour, "ring")}${short ? "" : esc(txt)}</span>`;
  }

  // ---------- where am I (presence) ----------
  const area = () => document.body.classList.contains("ov") ? "overview" : (typeof S !== "undefined" && S.area) || "ads";
  function acctColl(){
    if (typeof S === "undefined") return null;
    const a = (S.accts || []).find(x => x.key === S.acct);
    return a ? "ads:" + a.id : null;
  }
  const KIND1 = {campaigns:"campaign", ad_groups:"ad_group", adsets:"ad_group", ads:"ad", keywords:"keyword"};
  const kind1 = k => KIND1[k] || String(k || "").replace(/s$/, "");
  TM.adsId = (kind, id) => `${kind1(kind)}:${id}`;
  function adsEditor(){
    if (area() !== "ads" || typeof S === "undefined" || !S.focus) return null;
    const pane = document.getElementById("paneEdit");
    if (!pane || !pane.classList.contains("on") || !pane.offsetParent) return null;
    const coll = acctColl(); if (!coll) return null;
    const r = (S.rows || []).find(x => String(x.id) === S.focus);
    return {el: document.getElementById("editBody"), coll, id: TM.adsId(S.kind, S.focus), row: r,
            label: r ? (r.ad || r.keyword || r.text || r.ad_group || r.ad_set || r.campaign || "") : ""};
  }
  function editors(){
    const out = [];
    document.querySelectorAll("[data-team-coll][data-team-id]").forEach(el => {
      if (!el.dataset.teamId || !el.dataset.teamColl || !el.offsetParent || !el.offsetWidth) return;
      out.push({el, coll: el.dataset.teamColl, id: el.dataset.teamId, label: el.dataset.teamLabel || "", before: el.dataset.teamBar === "before"});
    });
    const a = adsEditor(); if (a) out.push(a);
    return out;
  }
  TM.where = () => { const e = editors()[0]; return {area: area(), coll: e ? e.coll : null, id: e ? e.id : null, editing: !!e} };

  // ---------- area adapters: lists and trees ----------
  function decoAds(pass){
    const coll = acctColl(); if (!coll || typeof S === "undefined") return;
    const ms = marksIn(coll);
    const byDraft = id => ms[id] ? [ms[id]] : [];
    document.querySelectorAll("#grid tr[data-i]").forEach(tr => {
      const r = S.rows[+tr.dataset.i]; if (!r) return;
      const ids = [...new Set([...Object.values(r._draft || {}).map(x => x && x.draft_id), r._new && r._new.draft_id].filter(Boolean))];
      const hit = ids.flatMap(byDraft);
      if (hit.length) setTint(tr, hit, "tmk-tint", pass);
      const nk = {campaigns:"campaign", ad_groups:"ad_group", adsets:"ad_set", ads:"ad", keywords:"keyword"}[S.kind];
      const host = (nk && tr.querySelector(`td[data-k="${nk}"]`)) || [...tr.children].find(td => !td.classList.contains("cb") && !td.classList.contains("thumbc"));
      const cid = TM.adsId(S.kind, r.id);
      ensure(host, "cm", cmButton(coll, cid, r.campaign && S.kind === "campaigns" ? r.campaign : r.ad || r.ad_group || r.ad_set || r.keyword || r.campaign || ""), pass);
      if (S.kind === "campaigns") ensure(host, "cl", claimChip(coll, cid, true), pass);
    });
    // tree: a dot on campaigns / ad groups with a teammate's pending change under them
    const list = Object.entries(ms);
    const norm = s => String(s || "").trim().toLowerCase();
    document.querySelectorAll("#tree .node[data-c]").forEach(n => {
      const c = n.dataset.c, g = n.dataset.g, name = norm((n.querySelector(".nm") || {}).textContent);
      if (!c) return;
      const hit = list.filter(([iid, m]) => {
        const p = m.parents || {};
        if (g) return String(p.adset_id) === g || "d-" + iid === g || (p.ad_group && norm(p.ad_group) === name);
        return String(p.campaign_id) === c || "d-" + iid === c || (p.campaign && norm(p.campaign) === name);
      }).map(([, m]) => m);
      const nm = n.querySelector(".nm") || n;
      const cid = g ? "ad_group:" + g : "campaign:" + c;
      ensure(n, "dot", hit.length ? `<span class="tmk-d" style="background:${colour(hit[0].who.colour)}" data-tmk-tip="${esc(tipFor(hit.slice(0, 4)) + (hit.length > 4 ? `\n\nand ${hit.length - 4} more` : ""))}"></span>` : "", pass);
      if (!g) ensure(n, "cl", claimChip(coll, cid, true), pass);
      ensure(n, "cm", cmButton(coll, cid, nm.textContent), pass);
    });
    document.querySelectorAll("#draft .item").forEach(it => {
      const b = it.querySelector("[data-rm]"); const m = b && ms[b.dataset.rm];
      if (m) setTint(it, [m], "tmk-tintb", pass);
    });
  }
  function decoEmail(pass){
    const ms = marksIn("email");
    document.querySelectorAll("tr[data-emid]").forEach(tr => {
      const id = tr.dataset.emid;
      if (ms[id]) setTint(tr, [ms[id]], "tmk-tint", pass);
      const host = tr.children[0];
      ensure(host, "cl", claimChip("email", id), pass);
      ensure(host, "cm", cmButton("email", id, host && host.textContent), pass);
    });
  }
  function decoSeo(pass){
    const ch = marksIn("seo:changes"), np = marksIn("seo:new_posts"), nodes = marksIn("seo:map_nodes");
    const byPost = {};
    Object.values(ch).forEach(m => { const p = (m.parents || {}).post_id; if (p) (byPost[p] = byPost[p] || []).push(m) });
    document.querySelectorAll("#seoTable tr.seo-row[data-id]").forEach(tr => {
      const id = tr.dataset.id, hit = byPost[id] || [];
      if (hit.length){
        setTint(tr, hit, "tmk-tint", pass);
        hit.forEach(m => { const td = tr.querySelector(`td[data-k="${CSS.escape((m.parents || {}).field || "")}"]`); if (td) td.setAttribute("data-tmk-tip", markTip(m)) });
      }
      const host = tr.querySelector("td.title") || tr.children[0];
      ensure(host, "cm", cmButton("seo:posts", id, host && host.textContent), pass);
    });
    document.querySelectorAll("#seoDraft .seo-ch").forEach(el => {
      const rm = el.querySelector("[data-rm]"), n = el.querySelector("[data-np]");
      const m = rm ? ch[rm.dataset.rm] : n ? np[n.dataset.np] : null;
      if (m) setTint(el, [m], "tmk-tintb", pass);
      if (n){ const host = el.querySelector("b"); ensure(host && host.parentNode, "cm", cmButton("seo:new_posts", n.dataset.np, host && host.textContent), pass) }
    });
    // topical map: tint the node, dot its hubs up the tree; new pages dot their node
    const up = {};
    Object.values(nodes).forEach(m => (m.ancestors || []).forEach(a => (up[a] = up[a] || []).push(m)));
    Object.values(np).forEach(m => { const n = (m.parents || {}).node_id; if (n) (up[n] = up[n] || []).push(m) });
    document.querySelectorAll("#tmMain tr.tm-row[data-id]").forEach(tr => {
      const id = tr.dataset.id, host = tr.querySelector(".tm-t") || tr.children[0];
      if (nodes[id]) setTint(tr, [nodes[id]], "tmk-tint", pass);
      const u = up[id] || [];
      ensure(host, "dot", u.length ? `<span class="tmk-d" style="background:${colour(u[0].who.colour)}" data-tmk-tip="${esc("Changes below this node\n\n" + tipFor(u.slice(0, 3)) + (u.length > 3 ? `\n\nand ${u.length - 3} more` : ""))}"></span>` : "", pass);
      ensure(host, "cl", claimChip("seo:map_nodes", id), pass);
      ensure(host, "cm", cmButton("seo:map_nodes", id, host.textContent), pass);
    });
  }
  function decoSocial(pass){
    const ms = marksIn("social");
    const postOf = el => el.dataset.ev ? (el.dataset.ev.startsWith("live:") ? null : el.dataset.ev.split(":")[0]) : el.dataset.open;
    document.querySelectorAll(".so-root .so-ev[data-ev], .so-root .so-ev[data-open], .so-root .so-ft[data-open]").forEach(el => {
      const id = postOf(el); if (!id) return;
      if (ms[id]) setTint(el, [ms[id]], "tmk-tintb", pass);
      const c = countOf("social", id);
      ensure(el, "cl", claimChip("social", id, true), pass);
      ensure(el, "cm", c.total ? cmButton("social", id, el.textContent) : "", pass);
    });
  }

  // ---------- editors: lock banner, changes, claim, comments ----------
  const CLAIMABLE = coll => coll === "email" || coll === "social" || coll === "seo:map_nodes";
  const others = (coll, id) => (TM.presence || []).filter(p => !p.me && p.coll === coll && String(p.id) === String(id));
  function sinceOf(p){
    const k = `${p.member.id}|${p.coll}|${p.id}`;
    if (!TM.since[k]) TM.since[k] = p.at || new Date().toISOString();
    return TM.since[k];
  }
  // marks that belong to an editor's item (the SEO post editor shows the post's changes from seo:changes)
  function marksFor(coll, id){
    if (coll === "seo:posts") return Object.values(marksIn("seo:changes")).filter(m => String((m.parents || {}).post_id) === String(id));
    if (coll.startsWith("ads:")){
      const r = (adsEditor() || {}).row; if (!r) return [];
      const ms = marksIn(coll);
      return [...new Set([...Object.values(r._draft || {}).map(x => x && x.draft_id), r._new && r._new.draft_id].filter(Boolean))].map(i => ms[i]).filter(Boolean);
    }
    const m = marksIn(coll)[id]; return m ? [m] : [];
  }
  const MAP_FIELDS = {title:"tmTitle", section:"tmSecSel", type:"tmType", parent:"tmParent", entity:"tmEntity", attribute:"tmAttr", macro_context:"tmMacro",
    intent:"tmIntent", priority:"tmPrio", url:"tmUrl", queries:"tmQ", note:"tmNote", links:"tmLinks"};
  function fieldHost(ed, coll, key){
    const q = s => ed.el.querySelector(s) || (ed.el.parentNode && ed.el.parentNode.querySelector(s));
    if (coll === "email"){ if (key === "html") return q("#emCodeBox"); if (key === "list_id" || key === "segment_id") return q('[data-f="aud"]'); return q(`[data-f="${CSS.escape(key)}"]`) }
    if (coll === "seo:posts" || coll === "seo:new_posts"){ const i = q("#sp_" + key) || q("#np_" + key); return i && (i.closest(".seo-f") || i.parentNode) }
    if (coll === "seo:map_nodes"){ const i = MAP_FIELDS[key] && q("#" + MAP_FIELDS[key]); return i && (i.closest(".tm-f") || i.parentNode) }
    return null;
  }
  function decoEditor(ed, pass){
    const {coll, id} = ed, key = coll + "|" + id;
    const live = others(coll, id).filter(p => p.editing);
    const ms = marksFor(coll, id);
    let html = "";
    if (live.length && !TM.anyway[key]){
      const p = live[0], names = live.map(x => x.member.name).join(" and ");
      html += `<div class="tmk-lock">${dot(p.member.colour)}<span>${esc(names)} ${live.length > 1 ? "are" : "is"} editing this (since ${esc(when(sinceOf(p)))})${TM.watch[key] ? ". Watching: your fields are read-only here." : ""}</span><span class="sp"></span>
        <button data-tmk-act="anyway">Edit anyway</button>${TM.watch[key] ? "" : `<button data-tmk-act="watch">Watch</button>`}</div>`;
    } else if (live.length){
      html += `<span>${dot(live[0].member.colour)} ${esc(live.map(x => x.member.name).join(" and "))} also ${live.length > 1 ? "have" : "has"} this open</span>`;
    }
    const placed = [];
    for (const m of ms){
      if (m.added) continue;  // a new item: one line in the bar, not a note on every field
      for (const f of m.fields || []){
        const k = coll === "seo:posts" ? (m.parents || {}).field : f.key;
        const host = k && fieldHost(ed, coll, k);
        if (host){ placed.push([host, m, f]); continue }
      }
    }
    const unplaced = ms.filter(m => !placed.some(([, pm]) => pm === m));
    for (const m of unplaced) html += `<div class="tmk-chg">${dot(m.who.colour)}<span data-tmk-tip="${esc(markTip(m))}">${esc(markTip(m).split("\n")[0])}</span></div>`;
    const claimable = CLAIMABLE(coll) || (coll.startsWith("ads:") && id.startsWith("campaign:"));
    const cl = claimOf(coll, id);
    html += `<span class="sp"></span>`;
    if (cl) html += claimChip(coll, id);
    if (claimable && TM.st.me && TM.st.me.role !== "viewer"){
      if (!cl) html += `<button data-tmk-act="claim" title="Tell the team you are writing this">Claim</button>`;
      else if (cl.mine || ["owner", "admin"].includes(TM.st.me.role)) html += `<button data-tmk-act="release">Release</button>`;
    }
    html += cmButton(coll, id, ed.label);
    const bar = `<div class="tmk-bar ${ed.before ? "before" : ""}" data-tmk-coll="${esc(coll)}" data-tmk-id="${esc(id)}" data-tmk-label="${esc(ed.label || "")}">${html}</div>`;
    if (ed.before){ const b = ensure(ed.el.parentNode, "bar:" + key, bar, pass, null); if (b && b.nextElementSibling !== ed.el) ed.el.parentNode.insertBefore(b, ed.el) }
    else ensure(ed.el, "bar:" + key, bar, pass, "first");
    for (const [host, m, f] of placed){
      const txt = f.new != null && f.new !== "" ? `${m.who.name} changed this to "${f.new}"` : `${m.who.name} cleared this`;
      const el = ensure(host, "fh:" + m.who.id, `<div class="tmk-fh" style="--tmk:${colour(m.who.colour)};--tmk-bg:${hexA(m.who.colour, .09)}">${esc(txt)}${f.old ? ` (was "${esc(f.old)}")` : ""}${m.at ? " · " + esc(when(m.at)) : ""}</div>`, pass);
      if (el) el.dataset.tmkPass = pass;
    }
    watchInputs(ed.el, !!(TM.watch[key] && live.length && !TM.anyway[key]));
  }
  function watchInputs(root, on){
    root.querySelectorAll("input, textarea, select").forEach(i => {
      if (i.closest(".tmk-x")) return;
      if (on && !i.dataset.tmkRo){ i.dataset.tmkRo = i.readOnly ? "keep" : "set"; if (i.tagName === "SELECT" || i.type === "checkbox") i.disabled = true; else i.readOnly = true }
      if (!on && i.dataset.tmkRo){ if (i.dataset.tmkRo === "set"){ if (i.tagName === "SELECT" || i.type === "checkbox") i.disabled = false; else i.readOnly = false } delete i.dataset.tmkRo }
    });
  }

  // ---------- one pass over the page ----------
  let passN = 0, obs = null, pending = null;
  function decorate(){
    pending = null;
    const pass = String(++passN);
    if (TM.st.joined && !TM.st.removed){
      const a = area();
      try {
        if (a === "ads") decoAds(pass);
        if (a === "email") decoEmail(pass);
        if (a === "seo") decoSeo(pass);
        if (a === "social") decoSocial(pass);
        editors().forEach(ed => decoEditor(ed, pass));
      } catch(e) { console.warn("team marks", e) }
    } else {
      document.querySelectorAll("[data-tmk-ro]").forEach(i => watchInputs(i.parentNode || document, false));
    }
    clearTints(pass); sweep(pass);
    if (obs) obs.takeRecords();
  }
  TM.refresh = () => { if (!pending) pending = setTimeout(decorate, 30) };
  obs = new MutationObserver(() => { if (TM.st.joined && !pending) pending = setTimeout(decorate, 120) });  // solo: nothing to do
  obs.observe(document.body, {childList:true, subtree:true});

  // clicks on our controls (capture: the areas' own row handlers never see them)
  document.addEventListener("click", async e => {
    const c = e.target.closest && e.target.closest(".tmk-cm");
    if (c){ e.stopPropagation(); e.preventDefault(); return openThread(c.dataset.tmkC, c.dataset.tmkI, c.dataset.tmkL) }
    const b = e.target.closest && e.target.closest(".tmk-bar [data-tmk-act]");
    if (!b) return;
    e.stopPropagation(); e.preventDefault();
    const bar = b.closest(".tmk-bar"), coll = bar.dataset.tmkColl, id = bar.dataset.tmkId, key = coll + "|" + id, act = b.dataset.tmkAct;
    if (act === "anyway"){ TM.anyway[key] = true; delete TM.watch[key] }
    if (act === "watch") TM.watch[key] = true;
    if (act === "claim") await doClaim(coll, id);
    if (act === "release") await doRelease(coll, id);
    decorate();
  }, true);
  async function doClaim(coll, id){
    try {
      const r = await post("claim", {coll, id});
      if (!r.ok){ say(r.message); await loadClaims(); return }
      say("Claimed: the team sees you are writing this"); await loadClaims();
    } catch(err) { say(err.message) }
  }
  async function doRelease(coll, id){ try { await post("release", {coll, id}); await loadClaims() } catch(err) { say(err.message) } }

  // ---------- comment thread panel ----------
  const P = {coll:null, id:null, label:"", parent:null, list:[]};
  async function openThread(coll, id, label){
    if (!TM.st.joined) return;
    P.coll = coll; P.id = id; P.label = (label || "").trim(); P.parent = null;
    panel.classList.add("on"); document.getElementById("tmkReplyTo").textContent = "";
    await loadThread(); document.getElementById("tmkTA").focus();
  }
  TM.openThread = openThread;
  async function loadThread(){
    if (!P.coll) return;
    let r; try { r = await get("comments", {coll:P.coll, id:P.id}) } catch(e) { document.getElementById("tmkPB").innerHTML = `<p class="tmk-sub">${esc(e.message)}</p>`; return }
    P.list = r.comments || [];
    document.getElementById("tmkPT").textContent = P.label || r.label || `${P.coll} ${P.id}`;
    drawPanelClaim();
    const tops = P.list.filter(c => !c.parent), kids = {};
    P.list.filter(c => c.parent).forEach(c => (kids[c.parent] = kids[c.parent] || []).push(c));
    const one = (c, reply) => `<div class="tmk-c ${reply ? "reply" : ""}" data-cid="${esc(c.id)}"><div class="h">${dot(c.author.colour)}<b>${esc(c.author.name)}</b><span>${esc(when(c.at))}${c.edited_at ? " · edited" : ""}</span></div>
      <div class="b">${TM.fmtBody(c.body)}</div><div class="a">${reply ? "" : `<button data-tmk-p="reply">Reply</button>`}${c.mine ? `<button data-tmk-p="edit">Edit</button>` : ""}
      ${reply ? "" : `<button data-tmk-p="${c.resolved ? "reopen" : "resolve"}">${c.resolved ? "Reopen" : "Resolve"}</button>`}</div></div>`;
    const th = c => `<div class="tmk-th ${c.resolved ? "resolved" : ""}">${one(c)}${(kids[c.id] || []).map(k => one(k, true)).join("")}</div>`;
    const open = tops.filter(c => !c.resolved), done = tops.filter(c => c.resolved);
    document.getElementById("tmkPB").innerHTML = (open.map(th).join("") || `<p class="tmk-sub">No open comments. Start one below.</p>`)
      + (done.length ? `<details><summary class="tmk-sub" style="cursor:pointer">${done.length} resolved</summary>${done.map(th).join("")}</details>` : "");
  }
  function drawPanelClaim(){
    const box = document.getElementById("tmkPClaim");
    const claimable = CLAIMABLE(P.coll) || (String(P.coll).startsWith("ads:") && String(P.id).startsWith("campaign:"));
    const cl = claimOf(P.coll, P.id);
    let h = cl ? claimChip(P.coll, P.id) : "";
    if (claimable && TM.st.me && TM.st.me.role !== "viewer"){
      if (!cl) h += ` <button data-tmk-pc="claim">Claim</button>`;
      else if (cl.mine || ["owner", "admin"].includes(TM.st.me.role)) h += ` <button data-tmk-pc="release">Release</button>`;
    }
    box.innerHTML = h;
  }
  document.getElementById("tmkPClaim").onclick = async e => { const b = e.target.closest("[data-tmk-pc]"); if (!b) return;
    if (b.dataset.tmkPc === "claim") await doClaim(P.coll, P.id); else await doRelease(P.coll, P.id); drawPanelClaim(); decorate() };
  document.getElementById("tmkPX").onclick = () => { panel.classList.remove("on"); P.coll = null };
  document.getElementById("tmkPB").onclick = async e => {
    const b = e.target.closest("[data-tmk-p]"); if (!b) return;
    const el = b.closest("[data-cid]"), cid = +el.dataset.cid, c = P.list.find(x => x.id === cid);
    const act = b.dataset.tmkP;
    if (act === "reply"){ P.parent = cid; document.getElementById("tmkReplyTo").innerHTML = `Replying to ${esc(c.author.name)} <button class="link" id="tmkNoReply">Cancel</button>`;
      document.getElementById("tmkNoReply").onclick = () => { P.parent = null; document.getElementById("tmkReplyTo").textContent = "" }; return document.getElementById("tmkTA").focus() }
    if (act === "edit"){
      const bd = el.querySelector(".b"); bd.innerHTML = `<textarea style="min-height:60px">${esc(c.body)}</textarea><div class="a"><button data-tmk-p="save">Save</button><button data-tmk-p="cancel">Cancel</button></div>`;
      wireAC(bd.querySelector("textarea")); return;
    }
    if (act === "cancel") return loadThread();
    try {
      if (act === "save") await post("comment_edit", {cid, body: el.querySelector("textarea").value});
      if (act === "resolve" || act === "reopen") await post("comment_edit", {cid, resolved: act === "resolve"});
    } catch(err) { return say(err.message) }
    await loadThread(); loadCounts([P.coll]);
  };
  async function send(){
    const ta = document.getElementById("tmkTA"), body = ta.value.trim(); if (!body) return;
    try { await post("comment", {coll:P.coll, id:P.id, body, parent:P.parent}) } catch(e) { return say(e.message) }
    ta.value = ""; P.parent = null; document.getElementById("tmkReplyTo").textContent = "";
    await loadThread(); loadCounts([P.coll]);
  }
  document.getElementById("tmkSend").onclick = send;
  document.getElementById("tmkTA").addEventListener("keydown", e => { if (e.key === "Enter" && (e.metaKey || e.ctrlKey)){ e.preventDefault(); send() } });

  // @ autocomplete from the team's members
  function wireAC(ta){
    const ac = document.getElementById("tmkAC");
    let opts = [], sel = 0;
    const close = () => { ac.classList.remove("on"); opts = [] };
    const pick = m => { const pos = ta.selectionStart, before = ta.value.slice(0, pos).replace(/@([A-Za-z0-9._+-]*)$/, "@" + TM.memberKey(m) + " ");
      ta.value = before + ta.value.slice(pos); ta.selectionStart = ta.selectionEnd = before.length; close(); ta.focus() };
    ta.addEventListener("input", () => {
      const m = /(?:^|\s)@([A-Za-z0-9._+-]*)$/.exec(ta.value.slice(0, ta.selectionStart));
      if (!m) return close();
      const q = m[1].toLowerCase();
      opts = (TM.st.members || []).filter(x => String(x.id) !== meId() && (TM.memberKey(x).toLowerCase().startsWith(q) || String(x.email || "").toLowerCase().startsWith(q) || String(x.name).toLowerCase().includes(q))).slice(0, 6);
      if (!opts.length) return close();
      sel = 0;
      ac.innerHTML = opts.map((x, i) => `<div data-i="${i}" class="${i === sel ? "on" : ""}">${dot(x.colour)}${esc(x.name)} <span class="tmk-sub">@${esc(TM.memberKey(x))}</span></div>`).join("");
      ac.classList.add("on");
      ac.onmousedown = e => { const d = e.target.closest("[data-i]"); if (d){ e.preventDefault(); pick(opts[+d.dataset.i]) } };
    });
    ta.addEventListener("keydown", e => {
      if (!ac.classList.contains("on") || !opts.length) return;
      if (e.key === "ArrowDown" || e.key === "ArrowUp"){ e.preventDefault(); sel = (sel + (e.key === "ArrowDown" ? 1 : opts.length - 1)) % opts.length;
        ac.querySelectorAll("[data-i]").forEach((d, i) => d.classList.toggle("on", i === sel)) }
      else if (e.key === "Enter" || e.key === "Tab"){ e.preventDefault(); e.stopPropagation(); pick(opts[sel]) }
      else if (e.key === "Escape") close();
    });
    ta.addEventListener("blur", () => setTimeout(close, 150));
  }
  wireAC(document.getElementById("tmkTA"));
  TM.wireAC = wireAC;

  // ---------- jump to an item ----------
  TM.open = async (coll, id) => {
    if (!coll) return;
    if (coll.startsWith("ads:")) return openAds(coll, id);
    const a = coll === "email" ? "email" : coll.startsWith("seo") ? "seo" : coll === "social" ? "social" : null;
    if (!a || typeof setArea !== "function") return;
    setArea(a);
    const A = (window.AREAS || {})[a];
    if (id && A && typeof A.open === "function") try { await A.open(coll, id) } catch(e) { say(e.message) }
  };
  async function openAds(coll, id){
    if (typeof S === "undefined") return;
    const acc = (S.accts || []).find(x => "ads:" + x.id === coll);
    if (!acc) return say("That ad account is not set up in this Studio");
    setArea("ads");
    if (S.acct !== acc.key && typeof switchAcct === "function") await switchAcct(acc.key);
    const m = /^([a-z_]+):(.+)$/.exec(String(id || ""));
    if (!m){ if (typeof showPane === "function") showPane("draft"); return }
    const k = {campaign:"campaigns", ad_group: isMeta() ? "adsets" : "ad_groups"}[m[1]] || m[1] + "s";
    if (!V()[k]) return;
    S.kind = k; S.camp = null; S.ag = null; S.filter = "all";
    renderTree(); renderTabs(); await loadGrid();
    const r = S.rows.find(x => String(x.id) === m[2]);
    if (r){ focusRow(r); const tr = document.querySelector(`#grid tr[data-i="${S.rows.indexOf(r)}"]`); if (tr) tr.scrollIntoView({block:"center"}) }
    else say("Not found in this account's current view");
  }

  // ---------- data ----------
  function visibleColls(){
    const a = area();
    if (a === "ads"){ const c = acctColl(); return c ? [c] : [] }
    if (a === "email") return ["email"];
    if (a === "seo") return ["seo:posts", "seo:new_posts", "seo:map_nodes"];
    if (a === "social") return ["social"];
    return [];
  }
  async function loadCounts(colls){
    colls = colls || visibleColls(); if (!colls.length || !TM.st.joined) return;
    try { const r = await get("counts", {colls: colls.join(",")}); Object.assign(TM.counts, r.counts || {}); decorate() } catch(e) {}
  }
  async function loadMarks(){ try { const r = await get("marks"); TM.marks = r.marks || {}; decorate() } catch(e) {} }
  async function loadClaims(){ try { TM.claims = (await get("claims")).claims || []; decorate(); if (P.coll) drawPanelClaim() } catch(e) {} }
  async function loadPresence(){ try { TM.presence = (await get("presence")).presence || []; decorate(); emit("presence") } catch(e) {} }
  async function loadUnread(){ try { TM.unread = TM.countUnread ? await TM.countUnread() : ((await get("mentions", {unread:1})).unread || 0); drawBell(); emit("mention") } catch(e) {} }
  TM.loadCounts = loadCounts; TM.loadClaims = loadClaims; TM.loadUnread = loadUnread;
  function emit(kind){ TM.listeners.forEach(fn => { try { fn(kind) } catch(e) {} }) }
  TM.on = fn => TM.listeners.push(fn);

  async function loadState(){
    let st; try { st = await get("state") } catch(e) { return }
    const was = TM.st.joined;
    TM.st = st;
    if (!st.joined || st.removed){ TM.marks = {}; TM.claims = []; TM.presence = []; TM.unread = 0; panel.classList.remove("on"); drawBell(); decorate(); emit("state"); return }
    TM.presence = st.presence || []; TM.claims = st.claims || []; TM.unread = st.unread || 0;
    if (st.ping) TM.kinds = st.ping.kinds || {};
    drawBell(); emit("state");
    if (!was){ loadMarks(); loadCounts() }  // after that, marks reload only when the ping says items changed
    decorate();
  }
  TM.loadState = loadState;

  // live: the server process keeps the stream; this asks it (locally) what changed
  async function tick(){
    if (!TM.st.joined || TM.st.removed) return;
    let p; try { p = await get("ping") } catch(e) { return }
    if (!p.joined){ return loadState() }
    const k = p.kinds || {}, old = TM.kinds || {};
    TM.kinds = k;
    if (k.presence !== old.presence) loadPresence();
    if (k.comment !== old.comment){ loadCounts(); if (P.coll) loadThread() }
    if (k.mention !== old.mention || k.notify !== old.notify) loadUnread();
    if (k.notify !== old.notify) emit("notify");
    if (k.event !== old.event){ loadClaims(); emit("event") }
    if (k.items !== old.items) loadMarks();
  }

  // presence heartbeat: on every change of place, and every 30 s while the window is visible
  let lastSent = "", lastAt = 0, active = Date.now();
  ["mousemove", "keydown", "mousedown", "wheel"].forEach(ev => document.addEventListener(ev, () => { active = Date.now() }, {passive:true, capture:true}));
  async function beat(){
    if (!TM.st.joined || TM.st.removed || document.visibilityState !== "visible") return;
    const w = TM.where(), sig = JSON.stringify(w);
    const idle = Date.now() - active > 5 * 60 * 1000;
    if (sig === lastSent && (Date.now() - lastAt < 30000 || idle)) return;
    lastSent = sig; lastAt = Date.now();
    try { const r = await post("presence", w); TM.presence = r.presence || TM.presence; decorate(); emit("presence") } catch(e) {}
  }

  // ---------- the bell ----------
  const bell = document.createElement("button");
  bell.id = "tmkBell"; bell.className = "tb"; bell.title = "Notifications";
  bell.innerHTML = `<svg viewBox="0 0 24 24" aria-hidden="true" fill="currentColor"><path d="M12 22a2 2 0 0 0 2-2h-4a2 2 0 0 0 2 2zm6-6V11a6 6 0 0 0-5-5.91V4a1 1 0 0 0-2 0v1.09A6 6 0 0 0 6 11v5l-2 2v1h16v-1l-2-2z"/></svg><span class="n"></span>`;
  function placeBell(){ const a = document.getElementById("teamSync") || document.getElementById("setupBtn"); if (a && a.parentNode && bell.nextSibling !== a) a.parentNode.insertBefore(bell, a) }
  function drawBell(){
    placeBell();
    bell.classList.toggle("on", !!(TM.st.joined && !TM.st.removed));
    const n = bell.querySelector(".n"); n.textContent = TM.unread || ""; n.classList.toggle("on", !!TM.unread);
    bell.title = TM.unread ? `${TM.unread} unread notification${TM.unread > 1 ? "s" : ""}` : "Notifications";
  }
  bell.onclick = () => { if (window.TeamNotify) return window.TeamNotify.toggle(bell); if (window.TeamTab) window.TeamTab.show("mentions") };

  // area changes: counts for what is now on screen
  let lastArea = "";
  setInterval(() => { const a = area() + "|" + (acctColl() || ""); if (a !== lastArea && TM.st.joined){ lastArea = a; loadCounts() } beat() }, 1500);
  setInterval(tick, 3000);
  setInterval(() => { if (TM.st.joined) loadCounts() }, 60000);
  setInterval(() => { if (TM.st.joined) loadState() }, 60000);
  document.addEventListener("visibilitychange", () => { if (document.visibilityState === "visible"){ lastSent = ""; beat() } });
  // started by team.js's own status (which it fetches anyway): a solo user makes no request from here
  window.addEventListener("team:status", e => {
    const t = e.detail || {}, on = !!(t.joined && !t.removed);
    if (on && !TM.st.joined) loadState();
    else if (!on && TM.st.joined){ TM.st = {joined: !!t.joined, removed: !!t.removed}; TM.marks = {}; TM.claims = []; TM.presence = []; TM.unread = 0;
      panel.classList.remove("on"); drawBell(); decorate(); emit("state") }
  });
})();
