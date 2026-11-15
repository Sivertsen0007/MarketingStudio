// Team: the Sync button in the top bar (remote changes waiting + changes to send), the conflicts dialog, and the
// Team card in Setup (join with an invite link, who you are, changeset, Auto-sync, Leave). Talks to /api/team/* (team.py).
(function(){
  const PRESS = (document.querySelector('meta[name="studio-press"]') || {}).content || "";
  const esc = s => String(s ?? "").replace(/[&<>"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
  async function call(path, body){
    const r = await fetch(path, body ? {method:"POST", headers:{"Content-Type":"application/json", "X-Studio-Press":PRESS}, body:JSON.stringify(body)} : {});
    const j = await r.json(); if (!r.ok) throw new Error(j.error || r.statusText); return j;
  }
  const say = m => (typeof toast === "function" ? toast(m) : alert(m));
  const css = `
  #teamSync{position:relative} #teamSync.waiting{border-color:var(--accent,#1a73e8);color:var(--accent,#1a73e8)}
  #teamSync .tm-n{display:none;min-width:16px;padding:0 5px;border-radius:9px;font-size:11px;line-height:16px;text-align:center}
  #teamSync .tm-in{background:var(--accent,#1a73e8);color:#fff} #teamSync .tm-out{background:var(--line2,#e8eaed);color:var(--text2,#3c4043)}
  #teamSync .tm-cf{background:#d93025;color:#fff} #teamSync .tm-n.on{display:inline-block}
  #teamSync.busy{opacity:.6;pointer-events:none}
  #teamDlg{border:1px solid var(--line,#dadce0);border-radius:8px;padding:0;max-width:860px;width:calc(100% - 32px);background:var(--panel,#fff);color:var(--text,#202124)}
  #teamDlg::backdrop{background:#0004}
  .tm-h{display:flex;align-items:center;gap:10px;padding:14px 18px;border-bottom:1px solid var(--line,#dadce0);font-weight:500}
  .tm-h .sp{flex:1} .tm-b{padding:12px 18px;max-height:70vh;overflow:auto}
  .tm-c{border:1px solid var(--line,#dadce0);border-radius:6px;padding:10px 12px;margin-bottom:10px}
  .tm-c h4{margin:0 0 6px;font-size:14px;font-weight:500} .tm-meta{font-size:12px;color:var(--muted,#5f6368);margin-bottom:8px}
  .tm-g{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:8px;font-size:13px}
  .tm-g div{background:var(--hover,#f1f3f4);border-radius:4px;padding:6px 8px;min-width:0}
  .tm-g b{display:block;font-size:11px;font-weight:500;color:var(--muted,#5f6368);margin-bottom:2px}
  .tm-g pre{margin:0;white-space:pre-wrap;word-break:break-word;font:12px/1.4 ui-monospace,Menlo,monospace;max-height:160px;overflow:auto}
  .tm-acts{display:flex;gap:8px;margin-top:8px;flex-wrap:wrap} .tm-acts textarea{width:100%;min-height:70px;font:12px ui-monospace,Menlo,monospace}
  .tm-who{display:inline-block;width:10px;height:10px;border-radius:50%;vertical-align:-1px;margin-right:5px}
  @media (max-width:640px){.tm-g{grid-template-columns:1fr}}`;
  document.head.insertAdjacentHTML("beforeend", `<style>${css}</style>`);
  document.body.insertAdjacentHTML("beforeend", `<dialog id="teamDlg" aria-label="Conflicts"></dialog>`);
  const dlg = document.getElementById("teamDlg");

  // ---------- top bar button ----------
  const btn = document.createElement("button");
  btn.id = "teamSync"; btn.className = "tb"; btn.style.display = "none";
  btn.innerHTML = `<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 4V1L8 5l4 4V6a6 6 0 0 1 5.65 8.03l1.46 1.46A8 8 0 0 0 12 4zm0 14a6 6 0 0 1-5.65-8.03L4.89 8.51A8 8 0 0 0 12 20v3l4-4-4-4v3z"/></svg>Sync`
    + `<span class="tm-n tm-in" title="Changes from your team waiting"></span><span class="tm-n tm-out" title="Your changes waiting to be sent"></span><span class="tm-n tm-cf" title="Conflicts to resolve"></span>`;
  const anchor = document.getElementById("setupBtn");
  if (anchor && anchor.parentNode) anchor.parentNode.insertBefore(btn, anchor);
  let T = {joined:false};

  function badge(cls, n){ const el = btn.querySelector(cls); el.textContent = n; el.classList.toggle("on", !!n) }
  function drawButton(){
    btn.style.display = T.joined ? "" : "none";
    if (!T.joined) return;
    badge(".tm-in", T.removed ? 0 : T.remote_waiting || 0);
    badge(".tm-out", T.pending_out || 0);
    badge(".tm-cf", T.conflicts || 0);
    btn.classList.toggle("waiting", !!T.remote_waiting && !T.removed);
    const last = T.last_sync ? new Date(T.last_sync).toLocaleString() : "never";
    btn.title = T.removed ? "This Mac was removed from the team. Open Setup > Team."
      : `Team ${T.team}: last sync ${last}. ${T.remote_waiting || 0} change(s) from the team waiting, ${T.pending_out || 0} of yours to send`
        + (T.conflicts ? `, ${T.conflicts} conflict(s)` : "") + ((T.failed || []).length ? `, ${T.failed.length} not accepted by the server` : "")
        + (T.can_send === false ? ". Viewers can't send changes" : "") + (T.last_error ? `. ${T.last_error}` : "");
  }
  async function refresh(){
    try { T = await call("/api/team/status") } catch(e) { return }
    drawButton();
    if (window._teamCardEl && document.body.contains(window._teamCardEl)) drawCard(window._teamCardEl);
  }
  async function runSync(){
    if (T.removed) return say("This Mac was removed from the team. Open Setup > Team to leave it.");
    btn.classList.add("busy");
    try {
      const r = await call("/api/team/sync", {});
      T = r.status || T; drawButton();
      say(r.ok ? r.message : (r.offline ? "Offline: your changes wait and go with the next sync" : r.message || r.error));
      if (r.ok && r.received && typeof window.poll === "function") { try { window.poll(true) } catch(e) {} }
      if ((T.conflicts || 0) > 0) openConflicts();
    } catch(e) { say(e.message) }
    finally { btn.classList.remove("busy") }
  }
  btn.onclick = runSync;

  // ---------- conflicts ----------
  const show = (v, missing) => missing ? "<i>(not there)</i>" : `<pre>${esc(typeof v === "string" ? v : JSON.stringify(v, null, 1))}</pre>`;
  async function openConflicts(){
    let C;
    try { C = await call("/api/team/conflicts") } catch(e) { return say(e.message) }
    if (!C.count){ if (dlg.open) dlg.close(); return refresh() }
    dlg.innerHTML = `<div class="tm-h">Conflicts (${C.count})<span class="sp"></span><button id="tmClose">Close</button></div>
      <div class="tm-b"><p class="tm-meta" style="margin-top:0">You and a teammate changed the same thing. Nothing was overwritten: your copy stays as it is until you choose.</p>
      ${C.conflicts.map((c, i) => `<div class="tm-c" data-i="${i}"><h4>${esc(c.label)}</h4>
        <div class="tm-meta">${esc(c.coll)} &middot; ${c.field === "*" ? "the whole item" : "field <b>" + esc(c.field) + "</b>"} &middot; changed by ${esc(c.who || "a teammate")}</div>
        <div class="tm-g"><div><b>Before</b>${show(c.base, c.base_missing)}</div><div><b>Theirs (${esc(c.who || "team")})</b>${show(c.theirs, c.theirs_missing)}</div><div><b>Mine</b>${show(c.mine, c.mine_missing)}</div></div>
        <div class="tm-acts"><button data-act="mine">Keep mine</button><button data-act="theirs">Take theirs</button><button data-act="edit">Edit</button></div></div>`).join("")}</div>`;
    dlg.querySelector("#tmClose").onclick = () => dlg.close();
    dlg.querySelectorAll(".tm-c").forEach(card => {
      const c = C.conflicts[+card.dataset.i];
      card.querySelectorAll("button[data-act]").forEach(b => b.onclick = async () => {
        const act = b.dataset.act;
        if (act === "edit"){
          const start = c.mine_missing ? c.theirs : c.mine;
          card.querySelector(".tm-acts").innerHTML = `<textarea>${esc(typeof start === "string" ? start : JSON.stringify(start, null, 1))}</textarea>
            <button class="primary" data-save>Use this</button><button data-cancel>Cancel</button><span class="tm-meta">Plain text, or JSON for lists and objects.</span>`;
          card.querySelector("[data-cancel]").onclick = openConflicts;
          card.querySelector("[data-save]").onclick = async () => {
            const raw = card.querySelector("textarea").value; let v = raw;
            if (typeof start !== "string") { try { v = JSON.parse(raw) } catch(e) { return say("That is not valid JSON") } }
            await resolve(c, "custom", v);
          };
          return;
        }
        await resolve(c, act);
      });
    });
    if (!dlg.open) dlg.showModal();
  }
  async function resolve(c, choice, value){
    try {
      const r = await call("/api/team/resolve", {coll:c.coll, id:c.id, field:c.field, choice, value});
      if (r.sync && r.sync.message) say(r.resolved ? "Resolved. " + r.sync.message : r.sync.message);
    } catch(e) { say(e.message) }
    await refresh(); openConflicts();
  }
  window.openTeamConflicts = openConflicts;

  // ---------- Setup > Team card ----------
  function drawCard(el){
    window._teamCardEl = el;
    if (!T.joined){
      el.innerHTML = `<h3><span class="su-dot"></span>Team</h3>
        <p class="su-help" style="margin:0 0 8px">Work on the same drafts as your team. Paste the invite link your admin sent you (it starts with marketingstudio://join or https://).</p>
        <div class="su-row"><label for="tmLink">Invite link</label><div><input id="tmLink" placeholder="marketingstudio://join?server=...&amp;invite=..." value="${esc(window._teamPendingLink || "")}"></div></div>
        <details><summary class="su-help" style="cursor:pointer">Or enter the server address and invite code</summary>
          <div class="su-row"><label for="tmServer">Team server</label><div><input id="tmServer" placeholder="https://team.example.com"></div></div>
          <div class="su-row"><label for="tmInvite">Invite code</label><div><input id="tmInvite" autocomplete="off"></div></div></details>
        <div class="su-acts"><button id="tmJoin" class="primary">Join team</button><span id="tmMsg" class="su-help"></span></div>`;
      el.querySelector("#tmJoin").onclick = async () => {
        const msg = el.querySelector("#tmMsg"); msg.textContent = "Joining ...";
        try {
          T = await call("/api/team/join", {link: el.querySelector("#tmLink").value.trim(), server: el.querySelector("#tmServer").value.trim(),
                                            invite: el.querySelector("#tmInvite").value.trim()});
          window._teamPendingLink = ""; drawButton(); drawCard(el); say(`Joined ${T.team}. Press Sync to get the team's drafts.`);
        } catch(e) { msg.textContent = e.message }
      };
      return;
    }
    const me = T.me || {};
    el.innerHTML = `<h3><span class="su-dot ${T.removed ? "" : "ok"}"></span>Team: ${esc(T.team)}</h3>
      ${T.removed ? `<p class="su-help" style="margin:0 0 8px;color:#d93025">This Mac was removed from the team. Your drafts are still here. Leave the team to clear it, then join again with a new invite if you need to.</p>` : ""}
      <p class="su-help" style="margin:0 0 8px"><span class="tm-who" style="background:${/^#[0-9a-fA-F]{6}$/.test(me.colour || "") ? me.colour : "#888888"}"></span><b>${esc(me.name)}</b> (${esc(me.role)}${me.approver ? ", approver" : ""}) on ${esc(T.server)}.
        Last sync: ${T.last_sync ? esc(new Date(T.last_sync).toLocaleString()) : "never"}${T.last_error ? `. <span style="color:#d93025">${esc(T.last_error)}</span>` : ""}</p>
      ${T.can_send === false ? `<p class="su-help" style="margin:0 0 8px">You are a viewer: you receive the team's drafts, but viewers can't send changes.</p>` : ""}
      ${(T.failed || []).length ? `<div class="su-help" style="margin:0 0 8px;color:#d93025">Not accepted by the team server (sent again once you edit them):<ul style="margin:4px 0 0 18px;padding:0">
        ${T.failed.map(f => `<li>${esc(f.coll)} ${esc(f.id)}: ${esc(f.reason)}</li>`).join("")}</ul></div>` : ""}
      ${(T.skipped || []).length ? `<details class="su-help" style="margin:0 0 8px"><summary style="cursor:pointer">${T.skipped.length} item(s) from the team could not be used here</summary><ul style="margin:4px 0 0 18px;padding:0">
        ${T.skipped.map(f => `<li>${esc(f.coll)} ${esc(f.id)}: ${esc(f.reason)}</li>`).join("")}</ul></details>` : ""}
      <div class="su-row"><label for="tmCs">Changeset</label><div><input id="tmCs" value="${esc((T.changeset || {}).name)}">
        <div class="su-help">Your edits are grouped under this name for your team. <a href="#" id="tmCsNew">Start a new changeset</a></div></div></div>
      <div class="su-row"><label for="tmAuto">Auto-sync</label><div><label style="padding:0"><input type="checkbox" id="tmAuto" style="width:auto" ${T.auto_sync ? "checked" : ""}> Sync on its own (3 seconds after your edits, and when teammates change something)</label></div></div>
      <div class="su-acts"><button id="tmSyncNow" ${T.removed ? "disabled" : ""}>Sync now</button>${T.conflicts ? `<button id="tmCf">Resolve ${T.conflicts} conflict${T.conflicts > 1 ? "s" : ""}</button>` : ""}
        <span class="sp" style="flex:1"></span><button id="tmLeave" class="danger">Leave team</button><span id="tmMsg" class="su-help"></span></div>`;
    const $ = s => el.querySelector(s);
    $("#tmCs").onchange = async e => { try { await call("/api/team/changeset", {action:"rename", name:e.target.value}); await refresh() } catch(err) { say(err.message) } };
    $("#tmCsNew").onclick = async e => { e.preventDefault(); const n = prompt("Name for the new changeset", ""); if (!n) return;
      try { await call("/api/team/changeset", {action:"set", name:n}); await refresh() } catch(err) { say(err.message) } };
    $("#tmAuto").onchange = async e => { try { T = await call("/api/team/settings", {auto_sync:e.target.checked}); drawButton() } catch(err) { say(err.message) } };
    $("#tmSyncNow").onclick = runSync;
    if ($("#tmCf")) $("#tmCf").onclick = openConflicts;
    $("#tmLeave").onclick = async () => {
      if (!confirm(`Leave ${T.team}? This Mac stops syncing and its access is revoked. Your drafts stay on this Mac.`)) return;
      try { T = await call("/api/team/leave", {}); drawButton(); drawCard(el) } catch(err) { $("#tmMsg").textContent = err.message }
    };
  }
  window.teamCard = el => { drawCard(el); refresh() };

  // marketingstudio://join links the Mac app passes through as #team-join=<link>
  function joinFromHash(){
    const m = /[#&]team-join=([^&]+)/.exec(location.hash);
    if (!m) return;
    window._teamPendingLink = decodeURIComponent(m[1]);
    history.replaceState(null, "", location.pathname + location.search);
    setTimeout(() => { if (typeof window.openSetup === "function") window.openSetup() }, 300);  // Setup > Team, link filled in
  }
  joinFromHash();
  window.addEventListener("hashchange", joinFromHash);
  refresh();
  setInterval(refresh, 15000);
})();
