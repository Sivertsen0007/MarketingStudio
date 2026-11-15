// Team tab (a top-level area next to Ads, Email, SEO, SoMe, Activity; only shown when this Studio is in a team):
// the team's activity feed (edit sessions, pushes, publishes, comments, claims, joins) with filters by person and
// area and live updates, who is here and what they have open, your @mentions (unread first), and the members.
// Data from /api/team/live/* (team_live.py) through team_marks.js; every server string is escaped.
(function(){
  const TM = window.TeamMarks; if (!TM) return;
  const esc = TM.esc, dot = TM.dot, when = TM.when;
  const lget = k => { try { return localStorage.getItem("adsstudio.team." + k) } catch(e) { return null } };
  const lset = (k, v) => { try { localStorage.setItem("adsstudio.team." + k, v) } catch(e) {} };
  const say = m => (typeof toast === "function" ? toast(m) : alert(m));
  const T = {el:null, person: lget("person") || "all", area: lget("area") || "all", feed:[], areas:[], mentions:[], loading:false};
  const AREA = {ads:"Ads", email:"Email", seo:"SEO", social:"SoMe", activity:"Activity", team:"Team", overview:"Overview", multi:"Several areas"};
  const areaName = a => AREA[a] || a || "";

  document.head.insertAdjacentHTML("beforeend", `<style>
  .tt-root{display:flex;flex-direction:column;min-height:0;flex:1}
  .tt-body{flex:1;min-height:0;display:grid;grid-template-columns:minmax(0,1fr) 360px}
  .tt-feed{overflow:auto;padding:10px 16px}
  .tt-side{overflow:auto;border-left:1px solid var(--line);padding:10px 14px;background:var(--side,var(--panel))}
  .tt-side section{margin-bottom:18px}
  .tt-root h3{font-size:13px;font-weight:500;margin:6px 0 8px;color:var(--text2);display:flex;align-items:center;gap:8px}
  .tt-root h3 .sp{flex:1}
  .tt-ev{display:grid;grid-template-columns:auto minmax(0,1fr) auto;gap:4px 10px;padding:8px 4px;border-bottom:1px solid var(--line2);align-items:baseline}
  .tt-ev .w{font-size:12px;color:var(--muted);white-space:nowrap}
  .tt-ev .s{min-width:0} .tt-ev .s b{font-weight:500}
  .tt-ev .k{font-size:11px;color:var(--muted);margin-left:6px}
  .tt-items{grid-column:2/4;display:flex;flex-wrap:wrap;gap:4px}
  .tt-items button,.tt-link{border:1px solid var(--line2);background:var(--panel);border-radius:10px;padding:1px 8px;font-size:12px;color:var(--text2);cursor:pointer;max-width:260px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
  .tt-items button:hover,.tt-link:hover{border-color:var(--accent);color:var(--accent)}
  .tt-p{display:flex;gap:8px;align-items:baseline;padding:5px 0;font-size:13px}
  .tt-p .x{min-width:0;flex:1} .tt-p .m{font-size:12px;color:var(--muted)}
  .tt-mn{padding:7px 8px;border-radius:6px;margin-bottom:6px;border:1px solid var(--line2);cursor:pointer}
  .tt-mn.unread{border-color:var(--accent);background:var(--accent-bg)}
  .tt-mn .h{display:flex;gap:6px;align-items:center;font-size:12px;color:var(--muted)} .tt-mn .h b{color:var(--text);font-weight:500} .tt-mn .h .sp{flex:1}
  .tt-mn .h b{white-space:nowrap} .tt-mn .on{font-size:12px;color:var(--muted);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
  .tt-mn .b{font-size:13px;white-space:pre-wrap;word-break:break-word;margin:3px 0 0}
  .tt-empty{color:var(--muted);font-size:13px;padding:8px 0}
  @media (max-width:900px){.tt-body{grid-template-columns:1fr}.tt-side{border-left:0;border-top:1px solid var(--line)}}
  </style>`);

  // ---------- the tab button: only while in a team ----------
  function tabButton(){
    const bar = document.getElementById("areaTabs"); if (!bar) return;
    let b = bar.querySelector('[data-area="team"]');
    const on = !!(TM.st.joined && !TM.st.removed);
    if (on && !b){ b = document.createElement("button"); b.dataset.area = "team"; b.textContent = "Team"; bar.appendChild(b) }
    if (on && b && b !== bar.lastElementChild) bar.appendChild(b);
    if (on && b && typeof S !== "undefined") b.classList.toggle("on", S.area === "team");
    if (!on && b){ b.remove(); if (typeof S !== "undefined" && S.area === "team" && typeof setArea === "function") setArea("ads") }
  }
  setInterval(tabButton, 1500);

  // ---------- render ----------
  function render(el){
    T.el = el;
    if (!TM.st.joined || TM.st.removed){
      el.innerHTML = `<div class="empty" style="padding:40px">${TM.st.removed ? "This Mac was removed from the team." : "You are not in a team."} Open Setup &gt; Team to join one with an invite link.</div>`;
      return;
    }
    el.innerHTML = `<div class="tt-root"><div class="toolbar"><b>Team: ${esc(TM.st.team || "")}</b>
      <select id="ttPerson" title="Show one person's activity"></select><select id="ttArea" title="Show one area"></select>
      <span class="spacer" style="flex:1"></span><button id="ttRefresh">Refresh</button></div>
      <div class="tt-body"><section class="tt-feed"><h3>Activity</h3><div id="ttFeed"><div class="tt-empty">Loading ...</div></div></section>
      <aside class="tt-side"><section id="ttWho"></section><section id="ttMent"></section><section id="ttMembers"></section></aside></div></div>`;
    const $ = s => el.querySelector(s);
    $("#ttPerson").onchange = e => { T.person = e.target.value; lset("person", T.person); loadFeed() };
    $("#ttArea").onchange = e => { T.area = e.target.value; lset("area", T.area); loadFeed() };
    $("#ttRefresh").onclick = () => { TM.loadState(); loadFeed(); loadMentions() };
    el.onclick = onClick;
    drawFilters(); drawWho(); drawMembers(); loadFeed(); loadMentions();
  }
  const live = () => T.el && document.body.contains(T.el) && T.el.querySelector(".tt-root");

  function drawFilters(){
    if (!live()) return;
    const ms = TM.st.members || [];
    T.el.querySelector("#ttPerson").innerHTML = `<option value="all">Everyone</option>` + ms.map(m => `<option value="${esc(m.id)}" ${String(m.id) === T.person ? "selected" : ""}>${esc(m.name)}</option>`).join("");
    const areas = [...new Set(["ads", "email", "seo", "social", ...T.areas])];
    T.el.querySelector("#ttArea").innerHTML = `<option value="all">All areas</option>` + areas.map(a => `<option value="${esc(a)}" ${a === T.area ? "selected" : ""}>${esc(areaName(a))}</option>`).join("");
  }

  // ---------- activity ----------
  async function loadFeed(){
    if (!live() || T.loading) return;
    T.loading = true;
    try { const r = await TM.get("feed", {person: T.person, area: T.area, limit: 200}); T.feed = r.events || []; T.areas = r.areas || [] }
    catch(e) { T.el.querySelector("#ttFeed").innerHTML = `<div class="tt-empty">${esc(e.message)}</div>`; return }
    finally { T.loading = false }
    drawFilters(); drawFeed();
  }
  const KIND = {edit_session:"edited", push:"pushed", publish:"published", undo:"undid", comment:"commented", mention:"mentioned", claim:"claim", join:"joined", custom:""};
  function itemBtn(coll, id, label){ return `<button data-jc="${esc(coll)}" data-ji="${esc(id || "")}" title="${esc(coll + (id ? " / " + id : ""))}">${esc(label || (id ? id : coll))}</button>` }
  function drawFeed(){
    if (!live()) return;
    const box = T.el.querySelector("#ttFeed");
    if (!T.feed.length){ box.innerHTML = `<div class="tt-empty">Nothing yet${T.person !== "all" || T.area !== "all" ? " with these filters" : ""}. Edits show here once teammates sync.</div>`; return }
    box.innerHTML = T.feed.map(e => {
      const d = e.data || {};
      let items = "";
      if (e.kind === "edit_session"){
        const its = (d.items || []).slice(0, 12);
        items = its.length ? `<div class="tt-items">${its.map(x => itemBtn(x.coll, x.id, x.label || x.id)).join("")}${d.truncated || (d.count || 0) > its.length ? `<span class="k">and ${(d.count || 0) - its.length} more</span>` : ""}</div>` : "";
      } else if (e.coll){
        items = `<div class="tt-items">${itemBtn(e.coll, e.id, e.label || e.id || e.coll)}</div>`;
      }
      return `<div class="tt-ev">${dot(e.who.colour)}<div class="s"><b>${esc(e.who.name)}</b> ${esc(e.summary || KIND[e.kind] || e.kind)}<span class="k">${esc(areaName(e.area))}</span></div>
        <span class="w" title="${esc(e.at || "")}">${esc(when(e.at))}</span>${items}</div>`;
    }).join("");
  }

  // ---------- who's here ----------
  function drawWho(){
    if (!live()) return;
    const ps = (TM.presence || []).slice().sort((a, b) => (a.me ? 1 : 0) - (b.me ? 1 : 0) || String(a.member.name).localeCompare(b.member.name));
    T.el.querySelector("#ttWho").innerHTML = `<h3>Who's here</h3>` + (ps.length ? ps.map(p => `<div class="tt-p">${dot(p.member.colour)}<div class="x"><b>${esc(p.member.name)}</b>${p.me ? " (you)" : ""}
        <div class="m">${esc(areaName(p.area))}${p.coll && p.id ? ` · ${p.editing ? "editing" : "looking at"} ` : ""}${p.coll && p.id ? itemBtn(p.coll, p.id, p.label || p.id).replace("<button ", '<button class="tt-link" ') : ""}</div></div>
        <span class="m">${esc(when(p.at))}</span></div>`).join("") : `<div class="tt-empty">Nobody else is in Studio right now.</div>`);
  }

  // ---------- mentions ----------
  async function loadMentions(){
    if (!live()) return;
    try { const r = await TM.get("mentions"); T.mentions = r.mentions || []; TM.unread = r.unread || 0 } catch(e) { T.mentions = [] }
    drawMentions();
  }
  function drawMentions(){
    if (!live()) return;
    const ms = T.mentions, n = ms.filter(x => !x.read).length;
    T.el.querySelector("#ttMent").innerHTML = `<h3>Mentions${n ? ` (${n} unread)` : ""}<span class="sp"></span>${n ? `<button class="link" data-mall>Mark all read</button>` : ""}</h3>`
      + (ms.length ? ms.slice(0, 50).map(x => { const c = x.comment;
        return `<div class="tt-mn ${x.read ? "" : "unread"}" data-mc="${esc(c.coll)}" data-mi="${esc(c.item_id)}" data-mid="${esc(c.id)}"><div class="h">${dot(c.author.colour)}<b>${esc(c.author.name)}</b><span class="sp"></span><span>${esc(when(c.at))}</span></div><div class="on">on ${esc(x.label || c.item_id)}</div>
          <div class="b">${TM.fmtBody(c.body)}</div>${x.read ? "" : `<button class="link" data-mread="${esc(c.id)}">Mark read</button>`}</div>` }).join("")
      : `<div class="tt-empty">No one has mentioned you yet. Teammates mention you with @${esc(TM.memberKey(TM.st.me || {}))} in a comment.</div>`);
  }
  async function markRead(ids){
    if (!ids.length) return;
    try { await TM.post("mentions_read", {ids}) } catch(e) { return say(e.message) }
    T.mentions.forEach(x => { if (ids.includes(x.comment.id)) x.read = true });
    TM.unread = T.mentions.filter(x => !x.read).length; TM.loadUnread(); drawMentions();
  }

  // ---------- members ----------
  function drawMembers(){
    if (!live()) return;
    const me = String((TM.st.me || {}).id);
    T.el.querySelector("#ttMembers").innerHTML = `<h3>Members</h3>` + (TM.st.members || []).map(m => `<div class="tt-p">${dot(m.colour)}<div class="x"><b>${esc(m.name)}</b>${String(m.id) === me ? " (you)" : ""}<div class="m">@${esc(TM.memberKey(m))}</div></div><span class="m">${esc(m.role)}${m.approver ? ", approver" : ""}</span></div>`).join("");
  }

  async function onClick(e){
    const all = e.target.closest("[data-mall]");
    if (all) return markRead(T.mentions.filter(x => !x.read).map(x => x.comment.id));
    const r = e.target.closest("[data-mread]");
    if (r){ e.stopPropagation(); return markRead([+r.dataset.mread]) }
    const mn = e.target.closest("[data-mc]");
    if (mn){ const m = T.mentions.find(x => x.comment.id === +mn.dataset.mid); if (m && !m.read) markRead([m.comment.id]);
      await TM.open(mn.dataset.mc, mn.dataset.mi); setTimeout(() => TM.openThread(mn.dataset.mc, mn.dataset.mi, m && m.label), 400); return }
    const j = e.target.closest("[data-jc]");
    if (j) return TM.open(j.dataset.jc, j.dataset.ji);
  }

  window.AREAS = window.AREAS || {};
  window.AREAS.team = {label:"Team", render};
  window.TeamTab = {show(section){
    if (typeof setArea === "function") setArea("team");
    setTimeout(() => { const s = T.el && T.el.querySelector(section === "mentions" ? "#ttMent" : "#ttFeed"); if (s) s.scrollIntoView({block:"start"}) }, 200);
  }};
  TM.on(kind => {
    tabButton();
    if (!live()){ if (kind === "state" && T.el && typeof S !== "undefined" && S.area === "team") render(T.el); return }
    if (kind === "event") loadFeed();
    if (kind === "presence") drawWho();
    if (kind === "mention") loadMentions();
    if (kind === "state"){ drawFilters(); drawWho(); drawMembers() }
  });
})();
