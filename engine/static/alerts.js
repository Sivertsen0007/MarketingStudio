// Marketing Studio - Alerts (part of Ads): rules on Google Ads and Meta numbers, checked on fresh reads (read-only),
// fired alerts with acknowledge and snooze, and the header badge. Nothing here changes anything on Google or Meta.
(() => {
const STYLE = `
.al-root{display:flex;flex-direction:column;flex:1;min-height:0;background:var(--bg)}
.al-head{display:flex;align-items:flex-end;gap:12px;padding:22px 28px 16px;flex-wrap:wrap}
.al-head .ttl{flex:1;min-width:240px}
.al-head h1{margin:0;font-size:24px;font-weight:600;letter-spacing:-.3px;line-height:1.2;color:var(--text)}
.al-head p{margin:4px 0 0;font-size:13.5px;font-weight:500;color:var(--muted)}
.al-head .acts{display:flex;gap:8px;align-items:center;flex-wrap:wrap}
.al-tog{display:inline-flex;gap:7px;align-items:center;height:34px;padding:0 12px;border-radius:var(--r);background:var(--panel);box-shadow:var(--sh-1);font-size:13px;font-weight:500;color:var(--text2);cursor:pointer;margin:0}
.al-scroll{overflow:auto;flex:1;padding:0 28px 28px;min-height:0}
.al-card{border-radius:var(--r-lg);background:var(--panel);box-shadow:var(--sh-card);margin-bottom:12px;overflow:hidden}
.al-hd{display:flex;gap:10px;align-items:center;flex-wrap:wrap;padding:16px 20px 12px}
.al-hd h3{margin:0;font-size:16px;font-weight:600;color:var(--text)}
.al-count{display:inline-flex;align-items:center;height:22px;padding:0 8px;border-radius:var(--r-sm);background:var(--sunken);color:var(--text2);font-size:11.5px;font-weight:600}
.al-count.hot{background:var(--bad-bg);color:var(--bad-text, #c8303a)}
.al-note{font-size:12px;font-weight:500;color:var(--muted);line-height:1.55;margin:0 0 10px}
.al-foot{padding:4px 4px 0}
.al-root .warnl{padding:10px 14px;border-radius:var(--r);background:var(--warn-bg);color:var(--warn);font-size:13px;font-weight:500}
.al-root .prob{margin:0 28px;padding:10px 14px;border-radius:var(--r);background:var(--bad-bg);color:var(--text);font-size:13px}
.al-table{width:100%;min-width:100%;border-collapse:separate;border-spacing:0;font-size:13px}
.al-table th,.al-table td{padding:10px 12px;height:auto;border:0;border-top:1px solid var(--line2);text-align:left;vertical-align:middle;white-space:normal;max-width:none;overflow:visible;color:var(--text2)}
.al-table th{position:static;font-size:11.5px;font-weight:600;color:var(--muted);background:var(--head);height:38px;padding-top:0;padding-bottom:0;cursor:default;border-top:1px solid var(--line2)}
.al-table th:first-child,.al-table td:first-child{padding-left:20px} .al-table th:last-child,.al-table td:last-child{padding-right:20px}
.al-table td b,.al-table td .t{color:var(--text);font-weight:600}
.al-table tbody tr{transition:background var(--t) var(--ease)} .al-table tbody tr:hover td{background:var(--hover)}
.al-table tr.acked td{color:var(--muted)}
.al-table td.num{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}
.al-table td.when{white-space:nowrap;color:var(--muted);font-variant-numeric:tabular-nums;width:1%}
.al-table .hint{margin:2px 0 0;font-size:12px;font-weight:500;color:var(--muted);line-height:1.45}
.al-acts{display:flex;gap:6px;flex-wrap:nowrap;align-items:center;justify-content:flex-end}
.al-acts select{width:auto;min-width:118px;height:32px;padding:0 30px 0 11px;font-size:12.5px;font-weight:600}
.al-table td.on{width:1%}
.al-acts button{height:32px;padding:0 12px;font-size:12.5px;white-space:nowrap}
.al-form{display:flex;gap:10px;flex-wrap:wrap;align-items:flex-end;padding:16px 20px 18px;background:var(--head);border-top:1px solid var(--line2)}
.al-form label{display:flex;flex-direction:column;font-size:11.5px;font-weight:600;color:var(--muted);gap:5px}
.al-form input[type=number]{width:96px}
.al-form button{height:36px}
.al-chip{display:inline-flex;align-items:center;height:22px;font-size:11px;font-weight:600;padding:0 8px;border-radius:var(--r-sm);background:var(--sunken);color:var(--text2);white-space:nowrap;vertical-align:middle}
.al-chip.on{background:var(--ok-bg);color:var(--ok)}
.al-chip.warn{background:var(--warn-bg);color:var(--warn)}
.al-chip.info{background:var(--accent-bg);color:var(--accent)}
.al-empty{padding:28px 20px;text-align:center;color:var(--muted);font-size:13px;font-weight:500;border-top:1px solid var(--line2)}
.al-empty button{margin-top:10px;height:32px}
.al-table td.more{width:1%;position:relative;padding-left:4px}
.al-dots{width:32px;height:32px;padding:0;border-radius:var(--r);font-size:16px;font-weight:700;letter-spacing:1px;line-height:1;color:var(--text2);background:transparent;box-shadow:none}
.al-dots:hover,.al-dots[aria-expanded=true]{background:var(--hover);color:var(--text)}
.al-menu{position:absolute;right:56px;top:50%;transform:translateY(-50%);z-index:5;min-width:160px;padding:6px;border-radius:var(--r-md);background:var(--panel);box-shadow:var(--sh-pop)}
.al-menu[hidden]{display:none}
.al-menu button{display:flex;align-items:center;width:100%;height:32px;padding:0 10px;border-radius:var(--r-sm);font-size:13px;font-weight:600;text-align:left}
.al-menu button:hover{background:var(--hover);text-decoration:none}
.al-root button.link.danger,.al-root button.danger{color:var(--bad-text, #c8303a)}
#alBadge .n{display:inline-block;min-width:18px;padding:0 6px;margin-left:6px;border-radius:var(--r-pill);background:var(--bad-text, #c8303a);color:var(--ink-text);font-size:11px;font-weight:700;line-height:18px;text-align:center}
`;
if (!document.getElementById("al-css")){ const st = document.createElement("style"); st.id = "al-css"; st.textContent = STYLE; document.head.appendChild(st) }

const h = s => String(s ?? "").replace(/[&<>"']/g, c => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[c]));
const A = {el: null, v: null};
const PLAT = {google: "Google Ads", meta: "Meta", any: "Google and Meta"};

// ---- header badge (every page): unacknowledged alerts ----
function badgeBtn(){
  let b = document.getElementById("alBadge");
  if (b) return b;
  const anchor = document.getElementById("hlBtn");
  if (!anchor) return null;
  anchor.insertAdjacentHTML("beforebegin", `<button id="alBadge" class="tb" title="Ads alerts">Alerts<span class="n" style="display:none"></span></button>`);
  b = document.getElementById("alBadge");
  b.onclick = () => setArea("alerts");
  return b;
}
async function pollBadge(){
  const b = badgeBtn(); if (!b) return;
  let r; try { r = await api("/api/area/alerts/badge") } catch(e) { return }
  const n = b.querySelector(".n");
  n.textContent = r.unacked > 99 ? "99+" : String(r.unacked);
  n.style.display = r.unacked ? "inline-block" : "none";
  b.title = r.unacked ? `${r.unacked} alert${r.unacked === 1 ? "" : "s"} to look at. Latest: ${r.latest}` : "Ads alerts: none to look at";
}
pollBadge(); setInterval(pollBadge, 60000);

// ---- the Alerts page ----
function render(el){
  A.el = el;
  el.innerHTML = `<div class="al-root"><div class="al-head">
    <div class="ttl"><h1>Alerts</h1><p id="alPolicy"></p></div>
    <div class="acts">
    <label class="al-tog" title="A macOS notification when an alert fires"><input type="checkbox" id="alMac"> Mac notification</label>
    <label class="al-tog" id="alTeamL" style="display:none" title="Also to your own team notifications (bell, and email / webhook as you set them in Team > Notifications)"><input type="checkbox" id="alTeam"> Team notifications</label>
    <button id="alCheck" class="primary" title="Read Google and Meta now (read-only) and check every rule that is switched on">Check rules now</button></div>
  </div>
  <div class="al-scroll" id="alBody"><p class="al-note">Loading ...</p></div></div>`;
  $("#alCheck").onclick = checkNow;
  // the ··· row menus close on any click outside them, and on Escape
  if (!A.menuWired){ A.menuWired = true;
    document.addEventListener("click", e => { if (!e.target.closest(".al-more-wrap")) closeMenus() });
    document.addEventListener("keydown", e => { if (e.key === "Escape") closeMenus() }) }
  $("#alMac").onchange = e => saveSettings({mac_notify: e.target.checked});
  $("#alTeam").onchange = e => saveSettings({team_notify: e.target.checked});
  load();
}

async function load(){
  let v; try { v = await api("/api/area/alerts/view") } catch(e) { const b = $("#alBody"); if (b) b.innerHTML = `<div class="prob">${h(e.message)}</div>`; return }
  A.v = v; if ($("#alBody")) draw(); pollBadge();
}

async function checkNow(){
  const b = $("#alCheck") || document.createElement("button"); b.disabled = true; b.innerHTML = `<span class="spin"></span> Checking ...`;
  try {
    const r = await api("/api/area/alerts/check", {});
    if (r.busy) toast(r.message);
    else if (r.message) toast(r.message);
    else {
      const bad = r.checked.filter(x => x.error || x.skipped);
      toast(`${r.fired.length} alert${r.fired.length === 1 ? "" : "s"} fired, ${r.checked.length} account${r.checked.length === 1 ? "" : "s"} checked`
        + (r.suppressed ? `, ${r.suppressed} kept quiet (quiet period or snoozed)` : "") + (r.capped ? `, +${r.capped} more (at most 20 per rule per check; they fire at the next check)` : "") + (bad.length ? ". " + bad.map(x => `${x.name}: ${x.error || x.skipped}`).join("; ") : ""));
    }
  } catch(e) { toast(e.message) }
  b.disabled = false; b.textContent = "Check rules now"; load();
}

async function saveSettings(body){ try { await api("/api/area/alerts/settings", body); toast("Saved") } catch(e) { toast(e.message) } }
async function post(path, body, msg){ try { const r = await api("/api/area/alerts/" + path, body); if (msg) toast(msg); load(); return r } catch(e) { toast(e.message); load() } }

function acctName(id){ if (id === "*") return "Every account"; const a = (A.v.accounts || []).find(x => x.id === id); return a ? a.name : id }

function draw(){
  const v = A.v, cat = v.catalogue;
  $("#alMac").checked = !!v.settings.mac_notify; $("#alTeam").checked = !!v.settings.team_notify;
  $("#alTeamL").style.display = v.team ? "" : "none";
  const jobs = (v.jobs || []).map(j => `${h(j.label)}: ${j.enabled ? (j.held ? "held (Refresh data is manual)" : "next " + h(fmtWhen(j.next_run))) : "off in Settings"}`).join(" · ");
  $("#alPolicy").textContent = v.policy === "manual" ? "Refresh data is manual: rules are checked when you press Check rules now or refresh Google / Meta" : "Checked hourly, conversion rules daily";
  const open = v.alerts.filter(x => !x.acked_at).length;
  const checks = Object.entries(v.last_check || {}).map(([id, c]) => `${h(acctName(id))}: ${h(fmtWhen(c.at))}${c.error ? ` <span class="al-chip warn" title="${h(c.error)}">error</span>` : c.skipped ? ` <span class="al-chip warn" title="${h(c.skipped)}">skipped</span>` : ""}${c.capped ? ` <span class="al-chip warn" title="At most 20 alerts per rule per check: the rest fire at the next check">+${h(c.capped)} more</span>` : ""}`).join(" · ");
  let out = "";
  if (v.meta_backoff) out += `<div class="warnl" style="margin-bottom:12px">Meta checks are paused until ${h(fmtWhen(v.meta_backoff.until))} (${h(v.meta_backoff.why)}): Meta asked Studio to slow down.</div>`;
  out += `<div class="al-card"><div class="al-hd"><h3>Fired alerts</h3><span class="al-count ${open ? "hot" : ""}">${open} to look at</span><span class="spacer" style="flex:1"></span>
    ${open ? `<button id="alAckAll">Acknowledge all</button>` : ""}</div>`;
  out += v.alerts.length ? `<div style="overflow:auto"><table class="al-table"><thead><tr><th>When</th><th>Alert</th><th>Rule</th><th>Sent to</th><th></th></tr></thead><tbody>${v.alerts.map(alertRow).join("")}</tbody></table></div>`
    : `<div class="al-empty">No alerts have fired yet.<br><button data-check-now>Check rules now</button></div>`;
  out += `</div>`;
  out += `<div class="al-card"><div class="al-hd"><h3>Rules</h3><span class="al-count">${v.rules.filter(r => r.enabled).length} of ${v.rules.length} switched on</span></div>
    <div style="overflow:auto"><table class="al-table"><thead><tr><th>On</th><th>Rule</th><th>Account</th><th>Check</th><th>Quiet for</th><th>Last fired</th><th></th></tr></thead>
    <tbody>${v.rules.map(ruleRow).join("") || `<tr><td colspan="7" class="al-empty">No rules yet.<br><button data-new-rule>Add a rule</button></td></tr>`}</tbody></table></div>
    ${form(cat, v.accounts)}</div>`;
  out += `<p class="al-note al-foot">${checks ? "Last checked: " + checks + ". " : ""}${jobs ? jobs + ". " : ""}${h(v.method)}</p>`;
  $("#alBody").innerHTML = out;
  wire();
}

function alertRow(x){
  const d = x.delivered || {}, sent = ["Studio"].concat(d.mac === "shown" ? ["Mac"] : []).concat(d.team === "sent" ? ["Team"] : []).join(", ");
  const why = d.team && !["sent", "off", "solo"].includes(d.team) ? ` <span class="al-chip warn" title="${h(d.team)}">team: ${h(String(d.team).split(":")[0])}</span>` : "";
  const st = x.snoozed_until ? `<span class="al-chip info">snoozed until ${h(fmtWhen(x.snoozed_until))}</span>` : x.acked_at ? `<span class="al-chip on">acknowledged</span>` : "";
  return `<tr class="${x.acked_at ? "acked" : ""}"><td class="when">${h(fmtWhen(x.at))}</td><td><span class="t">${h(x.text)}</span>${(x.details || []).length > 1 ? `<div class="hint">${x.details.map(h).join("<br>")}</div>` : ""}</td>
    <td>${h(x.rule_name)}</td><td>${h(sent)}${why}</td>
    <td><div class="al-acts">${st}${x.acked_at ? "" : `<button data-ack="${h(x.id)}">Acknowledge</button>`}
      <select data-snooze="${h(x.id)}" title="Keep this rule quiet for this ${h(x.object && x.object.level === "account" ? "account" : "object")}"><option value="">Snooze ...</option><option value="4">4 hours</option><option value="24">1 day</option><option value="72">3 days</option><option value="168">1 week</option>${x.snoozed_until ? `<option value="0">Wake up now</option>` : ""}</select></div></td></tr>`;
}

function ruleRow(r){
  return `<tr><td class="on"><input type="checkbox" data-on="${h(r.id)}" ${r.enabled ? "checked" : ""}></td>
    <td><span class="t">${h(r.name)}</span>${r.name !== r.description ? `<div class="hint">${h(r.description)}</div>` : ""}${r.starter ? ` <span class="al-chip">starter</span>` : ""}</td>
    <td>${h(acctName(r.account))}${r.account === "*" ? ` <span class="hint">(${h(PLAT[r.platform] || r.platform)})</span>` : ""}</td>
    <td><span class="al-chip ${r.enabled ? "on" : ""}">${h(r.cadence)}</span></td><td class="num">${h(r.quiet_hours)} h</td>
    <td>${r.last_fired ? h(fmtWhen(r.last_fired)) : ""}</td>
    <td class="more"><span class="al-more-wrap"><button class="al-dots" data-menu="${h(r.id)}" aria-haspopup="menu" aria-expanded="false" aria-label="More actions for ${h(r.name)}" title="More actions">···</button>
      <span class="al-menu" role="menu" hidden><button class="link danger" role="menuitem" data-del="${h(r.id)}" title="Delete this rule">Delete rule</button></span></span></td></tr>`;
}

function closeMenus(){
  document.querySelectorAll(".al-menu").forEach(m => m.hidden = true);
  document.querySelectorAll(".al-dots[aria-expanded=true]").forEach(b => b.setAttribute("aria-expanded", "false"));
}

function form(cat, accts){
  const opts = (o, sel) => Object.entries(o).map(([k, l]) => `<option value="${h(k)}" ${k === sel ? "selected" : ""}>${h(typeof l === "string" ? l : l.label)}</option>`).join("");
  return `<div class="al-form" id="alForm">
    <label>Account<select name="account"><option value="*">Every account</option>${(accts || []).map(a => `<option value="${h(a.id)}">${h(a.name)} (${h(PLAT[a.platform])})</option>`).join("")}</select></label>
    <label>Level<select name="level"><option value="account">Account</option><option value="campaign">Each campaign</option><option value="adset">Each ad set (Meta)</option></select></label>
    <label>Metric<select name="metric">${opts(cat.metrics, "spend")}</select></label>
    <label>Comparison<select name="op">${opts(cat.ops, "above")}</select></label>
    <label>Value<input type="number" name="value" step="any" placeholder="e.g. 120"></label>
    <label>Window<select name="window">${opts(cat.windows, "today")}</select></label>
    <label title="Only fire when the window has at least this much (both periods for a change)">Minimum<span style="display:flex;gap:4px"><select name="min_k"><option value="">none</option><option>conversions</option><option>clicks</option><option>impressions</option><option>spend</option></select><input type="number" name="min_v" min="0" step="any" style="width:70px"></span></label>
    <label>Quiet for (hours)<input type="number" name="quiet_hours" min="1" max="720" value="24"></label>
    <label>Name (optional)<input name="name" placeholder="described for you" style="width:180px"></label>
    <button class="primary" id="alAdd">Add rule</button></div>`;
}

function wire(){
  const B = $("#alBody");
  B.querySelectorAll("[data-ack]").forEach(b => b.onclick = () => post("ack", {ids: [b.dataset.ack]}));
  B.querySelectorAll("[data-snooze]").forEach(s => s.onchange = () => s.value !== "" && post("snooze", {id: s.dataset.snooze, hours: +s.value}, +s.value ? "Snoozed" : "Woken up"));
  B.querySelectorAll("[data-on]").forEach(c => c.onchange = () => post("rule", {id: c.dataset.on, enabled: c.checked}, c.checked ? "Rule switched on" : "Rule switched off"));
  B.querySelectorAll("[data-menu]").forEach(b => b.onclick = e => { e.stopPropagation(); const m = b.nextElementSibling, open = m.hidden;
    closeMenus(); if (open){ m.hidden = false; b.setAttribute("aria-expanded", "true"); m.querySelector("button").focus() } });
  B.querySelectorAll("[data-del]").forEach(b => b.onclick = () => { closeMenus(); confirm("Delete this alert rule?") && post("delete", {id: b.dataset.del}, "Rule deleted") });
  B.querySelectorAll("[data-check-now]").forEach(b => b.onclick = checkNow);
  B.querySelectorAll("[data-new-rule]").forEach(b => b.onclick = () => { const f = $("#alForm"); f.scrollIntoView({block: "center", behavior: "smooth"}); f.querySelector("[name=metric]").focus() });
  const all = $("#alAckAll"); if (all) all.onclick = () => post("ack", {all: true}, "All acknowledged");
  const f = $("#alForm"), val = n => f.querySelector(`[name="${n}"]`).value;
  const kind = () => (A.v.catalogue.metrics[val("metric")] || {}).kind;
  const sync = () => { const k = kind(); ["op", "value", "window"].forEach(n => f.querySelector(`[name="${n}"]`).disabled = k === "check");
    f.querySelector(`[name="window"]`).disabled = k === "check" || k === "count" };
  f.querySelector(`[name="metric"]`).onchange = sync; sync();
  $("#alAdd").onclick = () => {
    const body = {account: val("account"), level: val("level"), metric: val("metric"), op: val("op"), window: val("window"),
                  quiet_hours: val("quiet_hours"), name: val("name"), enabled: true};
    if (kind() !== "check") body.value = val("value") === "" ? (kind() === "count" ? 0 : null) : +val("value");
    if (val("min_k") && val("min_v") !== "") body.min = {[val("min_k")]: +val("min_v")};
    post("rule", body, "Rule added and switched on");
  };
}

window.AREAS = window.AREAS || {};
window.AREAS.alerts = {label: "Alerts", render};
})();
