// Marketing Studio - Alerts (part of Ads): rules on Google Ads and Meta numbers, checked on fresh reads (read-only),
// fired alerts with acknowledge and snooze, and the header badge. Nothing here changes anything on Google or Meta.
(() => {
const STYLE = `
.al-scroll{overflow:auto;flex:1;padding:14px 18px;min-height:0}
.al-card{border:1px solid var(--line);border-radius:8px;background:var(--panel);margin-bottom:16px}
.al-head{display:flex;gap:10px;align-items:center;flex-wrap:wrap;padding:10px 14px;border-bottom:1px solid var(--line2)}
.al-head h3{margin:0;font-size:15px;font-weight:500}
.al-note{font-size:12px;color:var(--muted);line-height:1.45;margin:0 0 10px}
.al-table{width:100%;border-collapse:collapse;font-size:13px}
.al-table th,.al-table td{padding:5px 8px;border-top:1px solid var(--line2);text-align:left;vertical-align:top;white-space:normal}
.al-table th{font-weight:500;color:var(--text2);background:var(--head)}
.al-table tr.acked td{color:var(--muted)}
.al-table td.num{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}
.al-acts{display:flex;gap:6px;flex-wrap:wrap}
.al-form{display:flex;gap:8px;flex-wrap:wrap;align-items:flex-end;padding:10px 14px}
.al-form label{display:flex;flex-direction:column;font-size:12px;color:var(--muted);gap:3px}
.al-form input[type=number]{width:90px}
.al-chip{display:inline-block;font-size:11px;padding:1px 7px;border-radius:9px;background:var(--head);color:var(--text2);white-space:nowrap}
.al-chip.on{background:var(--ok-bg,#e6f4ea);color:var(--ok)}
.al-chip.warn{background:var(--warn-bg);color:var(--text)}
#alBadge .n{display:inline-block;min-width:16px;padding:0 5px;margin-left:4px;border-radius:9px;background:var(--bad);color:#fff;font-size:11px;line-height:16px;text-align:center}
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
  el.innerHTML = `<div class="toolbar">
    <b style="font-weight:500">Alerts</b><span class="hint" id="alPolicy"></span>
    <span class="spacer" style="flex:1"></span>
    <label class="hint" title="A macOS notification when an alert fires"><input type="checkbox" id="alMac"> Mac notification</label>
    <label class="hint" id="alTeamL" style="display:none" title="Also to your own team notifications (bell, and email / webhook as you set them in Team > Notifications)"><input type="checkbox" id="alTeam"> Team notifications</label>
    <button id="alCheck" class="primary" title="Read Google and Meta now (read-only) and check every rule that is switched on">Check now</button>
  </div>
  <div class="al-scroll" id="alBody"><p class="al-note">Loading ...</p></div>`;
  $("#alCheck").onclick = checkNow;
  $("#alMac").onchange = e => saveSettings({mac_notify: e.target.checked});
  $("#alTeam").onchange = e => saveSettings({team_notify: e.target.checked});
  load();
}

async function load(){
  let v; try { v = await api("/api/area/alerts/view") } catch(e) { $("#alBody").innerHTML = `<div class="prob">${h(e.message)}</div>`; return }
  A.v = v; draw(); pollBadge();
}

async function checkNow(){
  const b = $("#alCheck"); b.disabled = true; b.innerHTML = `<span class="spin"></span> Checking ...`;
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
  b.disabled = false; b.textContent = "Check now"; load();
}

async function saveSettings(body){ try { await api("/api/area/alerts/settings", body); toast("Saved") } catch(e) { toast(e.message) } }
async function post(path, body, msg){ try { const r = await api("/api/area/alerts/" + path, body); if (msg) toast(msg); load(); return r } catch(e) { toast(e.message); load() } }

function acctName(id){ if (id === "*") return "Every account"; const a = (A.v.accounts || []).find(x => x.id === id); return a ? a.name : id }

function draw(){
  const v = A.v, cat = v.catalogue;
  $("#alMac").checked = !!v.settings.mac_notify; $("#alTeam").checked = !!v.settings.team_notify;
  $("#alTeamL").style.display = v.team ? "" : "none";
  const jobs = (v.jobs || []).map(j => `${h(j.label)}: ${j.enabled ? (j.held ? "held (Refresh data is manual)" : "next " + h(fmtWhen(j.next_run))) : "off in Settings"}`).join(" · ");
  $("#alPolicy").textContent = v.policy === "manual" ? "Refresh data is manual: rules are checked when you press Check now or refresh Google / Meta" : "Checked hourly, conversion rules daily";
  const open = v.alerts.filter(x => !x.acked_at).length;
  const checks = Object.entries(v.last_check || {}).map(([id, c]) => `${h(acctName(id))}: ${h(fmtWhen(c.at))}${c.error ? ` <span class="al-chip warn" title="${h(c.error)}">error</span>` : c.skipped ? ` <span class="al-chip warn" title="${h(c.skipped)}">skipped</span>` : ""}${c.capped ? ` <span class="al-chip warn" title="At most 20 alerts per rule per check: the rest fire at the next check">+${h(c.capped)} more</span>` : ""}`).join(" · ");
  let out = "";
  if (v.meta_backoff) out += `<div class="warnl" style="margin-bottom:12px">Meta checks are paused until ${h(fmtWhen(v.meta_backoff.until))} (${h(v.meta_backoff.why)}): Meta asked Studio to slow down.</div>`;
  out += `<div class="al-card"><div class="al-head"><h3>Fired alerts</h3><span class="hint">${open} to look at</span><span class="spacer" style="flex:1"></span>
    ${open ? `<button id="alAckAll">Acknowledge all</button>` : ""}</div>`;
  out += v.alerts.length ? `<div style="overflow:auto"><table class="al-table"><thead><tr><th>When</th><th>Alert</th><th>Rule</th><th>Sent to</th><th></th></tr></thead><tbody>${v.alerts.map(alertRow).join("")}</tbody></table></div>`
    : `<p class="al-note" style="padding:10px 14px;margin:0">No alerts have fired yet.</p>`;
  out += `</div>`;
  out += `<div class="al-card"><div class="al-head"><h3>Rules</h3><span class="hint">${v.rules.filter(r => r.enabled).length} of ${v.rules.length} switched on</span></div>
    <div style="overflow:auto"><table class="al-table"><thead><tr><th>On</th><th>Rule</th><th>Account</th><th>Check</th><th>Quiet for</th><th>Last fired</th><th></th></tr></thead>
    <tbody>${v.rules.map(ruleRow).join("") || `<tr><td colspan="7" class="al-note">No rules yet.</td></tr>`}</tbody></table></div>
    ${form(cat, v.accounts)}</div>`;
  out += `<p class="al-note">${checks ? "Last checked: " + checks + ". " : ""}${jobs ? jobs + ". " : ""}${h(v.method)}</p>`;
  $("#alBody").innerHTML = out;
  wire();
}

function alertRow(x){
  const d = x.delivered || {}, sent = ["Studio"].concat(d.mac === "shown" ? ["Mac"] : []).concat(d.team === "sent" ? ["Team"] : []).join(", ");
  const why = d.team && !["sent", "off", "solo"].includes(d.team) ? ` <span class="al-chip warn" title="${h(d.team)}">team: ${h(String(d.team).split(":")[0])}</span>` : "";
  const st = x.snoozed_until ? `<span class="al-chip">snoozed until ${h(fmtWhen(x.snoozed_until))}</span>` : x.acked_at ? `<span class="al-chip">acknowledged</span>` : "";
  return `<tr class="${x.acked_at ? "acked" : ""}"><td class="num">${h(fmtWhen(x.at))}</td><td>${h(x.text)}${(x.details || []).length > 1 ? `<div class="hint">${x.details.map(h).join("<br>")}</div>` : ""}</td>
    <td>${h(x.rule_name)}</td><td>${h(sent)}${why}</td>
    <td><div class="al-acts">${st}${x.acked_at ? "" : `<button data-ack="${h(x.id)}">Acknowledge</button>`}
      <select data-snooze="${h(x.id)}" title="Keep this rule quiet for this ${h(x.object && x.object.level === "account" ? "account" : "object")}"><option value="">Snooze ...</option><option value="4">4 hours</option><option value="24">1 day</option><option value="72">3 days</option><option value="168">1 week</option>${x.snoozed_until ? `<option value="0">Wake up now</option>` : ""}</select></div></td></tr>`;
}

function ruleRow(r){
  return `<tr><td><input type="checkbox" data-on="${h(r.id)}" ${r.enabled ? "checked" : ""}></td>
    <td>${h(r.name)}${r.name !== r.description ? `<div class="hint">${h(r.description)}</div>` : ""}${r.starter ? ` <span class="al-chip">starter</span>` : ""}</td>
    <td>${h(acctName(r.account))}${r.account === "*" ? ` <span class="hint">(${h(PLAT[r.platform] || r.platform)})</span>` : ""}</td>
    <td><span class="al-chip ${r.enabled ? "on" : ""}">${h(r.cadence)}</span></td><td class="num">${h(r.quiet_hours)} h</td>
    <td>${r.last_fired ? h(fmtWhen(r.last_fired)) : ""}</td><td><button data-del="${h(r.id)}" class="danger" title="Delete this rule">Delete</button></td></tr>`;
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
  B.querySelectorAll("[data-del]").forEach(b => b.onclick = () => confirm("Delete this alert rule?") && post("delete", {id: b.dataset.del}, "Rule deleted"));
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
