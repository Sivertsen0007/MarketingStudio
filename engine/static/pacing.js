// Marketing Studio - Pacing (part of Ads): month-to-date spend, forecast and budget recommendations per ad account
// and campaign against a monthly target. Recommendations only go into the Ads draft; Push live stays the user's button.
(() => {
const STYLE = `
.pc-scroll{overflow:auto;flex:1;padding:14px 18px;min-height:0}
.pc-card{border:1px solid var(--line);border-radius:8px;background:var(--panel);margin-bottom:16px}
.pc-head{display:flex;gap:10px;align-items:center;flex-wrap:wrap;padding:10px 14px;border-bottom:1px solid var(--line2)}
.pc-head h3{margin:0;font-size:15px;font-weight:500}
.pc-head .sub{font-size:12px;color:var(--muted)}
.pc-tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:10px;padding:10px 14px}
.pc-tile{border:1px solid var(--line2);border-radius:6px;padding:8px 10px}
.pc-tile .k{font-size:12px;color:var(--muted)} .pc-tile .v{font-size:19px;margin-top:2px;font-variant-numeric:tabular-nums}
.pc-tile .d{font-size:12px;color:var(--muted);margin-top:2px}
.pc-flag{display:inline-block;font-size:12px;padding:1px 8px;border-radius:10px;white-space:nowrap}
.pc-flag.over{background:var(--bad-bg,#fde7e9);color:var(--bad)} .pc-flag.under{background:var(--warn-bg);color:var(--text)}
.pc-flag.on_track{background:var(--ok-bg,#e6f4ea);color:var(--ok)} .pc-flag.no_target{color:var(--muted)}
.pc-table{width:100%;border-collapse:collapse;font-size:13px}
.pc-table th,.pc-table td{padding:5px 8px;border-top:1px solid var(--line2);text-align:left;vertical-align:top}
.pc-table th{font-weight:500;color:var(--text2);background:var(--head)}
.pc-table td.num,.pc-table th.num{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}
.pc-table input[type=number]{width:92px;text-align:right}
.pc-table tr.off td{color:var(--muted)}
.pc-table td{white-space:normal} .pc-table td:first-child{min-width:160px;max-width:280px;word-break:break-word}
.pc-rec{font-size:12px;line-height:1.4;min-width:240px;max-width:380px;white-space:normal}
.pc-rec b{font-weight:500}
.pc-rec .w{color:var(--bad);display:block}
.pc-note{font-size:12px;color:var(--muted);line-height:1.45;margin:0 0 10px}
.pc-acts{display:flex;gap:8px;align-items:center;padding:8px 14px 12px;flex-wrap:wrap}
.pc-set{display:inline-flex;gap:6px;align-items:center;font-size:13px}
.pc-set input{width:60px}
`;
if (!document.getElementById("pc-css")){ const st = document.createElement("style"); st.id = "pc-css"; st.textContent = STYLE; document.head.appendChild(st) }

const h = s => String(s ?? "").replace(/[&<>"']/g, c => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[c]));
const cur = (v, c) => v == null || v === "" ? "" : curFmt(v, c);
const FLAG = {over: "Over", under: "Under", on_track: "On track", no_target: "No target"};
const P = {el: null, v: null, busy: false};

function flagChip(st, amt, pct, c){
  if (!st || st === "no_target") return `<span class="pc-flag no_target">No target</span>`;
  const extra = st === "on_track" ? "" : ` by ${cur(Math.abs(amt), c)} (${pct > 0 ? "+" : ""}${pct}%)`;
  return `<span class="pc-flag ${h(st)}">${FLAG[st] || h(st)}${h(extra)}</span>`;
}

function render(el){
  P.el = el;
  el.innerHTML = `<div class="toolbar">
    <b style="font-weight:500">Pacing</b><span class="hint" id="pcMonth"></span>
    <span class="spacer" style="flex:1"></span>
    <span class="pc-set" title="How far a forecast may be from the target before it is flagged">Tolerance <input type="number" id="pcTol" min="1" max="50" step="1">%</span>
    <span class="pc-set" title="Largest budget change per step on Meta (big changes restart learning)">Meta step <input type="number" id="pcStep" min="5" max="50" step="1">%</span>
    <button id="pcShare" style="display:none" title="Make these targets the team's (owners and admins)">Share targets with the team</button>
    <span id="pcFetched" class="chip"></span>
    <button id="pcRefresh" title="Read month-to-date spend again from Google and Meta (read-only)">Refresh</button>
  </div>
  <div class="pc-scroll" id="pcBody"><p class="pc-note">Loading pacing (each account is read once when nothing is cached) ...</p></div>`;
  $("#pcRefresh").onclick = async () => { $("#pcRefresh").disabled = true; $("#pcFetched").innerHTML = `<span class="spin"></span> Fetching ...`;
    await dataRefresh("pacing", null, {reload:false}); $("#pcRefresh").disabled = false; load() };
  const setting = (k, id) => $(id).onchange = e => saveTargets({[k]: e.target.value});
  setting("tolerance_pct", "#pcTol"); setting("meta_step_pct", "#pcStep");
  $("#pcShare").onclick = () => { if (confirm("Share all pacing targets with the team? They then replace everyone's own.")) saveTargets({share: true}) };
  load();
}

async function load(){
  let v;
  try { v = await api("/api/area/pacing/status") } catch(e) { $("#pcBody").innerHTML = `<div class="prob">${h(e.message)}</div>`; return }
  P.v = v; draw();
}

async function saveTargets(body){
  try { await api("/api/area/pacing/targets", body); toast(body.share ? "Targets shared with the team" : "Saved"); load() }
  catch(e) { toast(e.message); load() }
}

async function toDraft(acct, ids){
  try {
    const r = await api("/api/area/pacing/draft", {account: acct.account, ids: ids || null});
    const sk = (r.skipped || []).map(x => `${x.name}: ${x.why}`).join("; ");
    if (!(r.added || []).length){ toast(r.message || ("Nothing added. " + sk)); return }
    toast(`${r.added.length} budget change${r.added.length === 1 ? "" : "s"} added to the ${acct.name} draft${sk ? ". Skipped: " + sk : ""}. Review and push them in Ads.`);
    const go = document.querySelector(`[data-open="${CSS.escape(acct.key)}"]`); if (go) go.classList.add("primary");
  } catch(e) { toast(e.message) }
}

function draw(){
  const v = P.v, info = v.target_info || {}, ro = info.editable === false;
  $("#pcTol").value = v.targets.tolerance_pct; $("#pcStep").value = v.targets.meta_step_pct;
  $("#pcTol").disabled = $("#pcStep").disabled = ro;
  $("#pcShare").style.display = info.joined && info.can_admin && !info.team ? "" : "none";
  const first = v.accounts[0];
  $("#pcMonth").textContent = first ? first.month : "";
  const olds = v.accounts.map(a => a.fetched_at).filter(Boolean).sort();
  $("#pcFetched").textContent = olds.length ? "Fetched " + fmtWhen(olds[0]) : "";
  const d = v.drift, drift = d && (d.drifting || []).length ? `<div class="warnl" style="margin-bottom:12px">Daily check ${h(fmtWhen(d.at))}: ${d.drifting.map(x =>
    `${h(x.name)} ${x.flag === "over" ? "over" : "under"} target by ${h(cur(Math.abs(x.amount), x.currency))} (${x.pct > 0 ? "+" : ""}${h(x.pct)}%)`).join("; ")}</div>` : "";
  const team = info.team ? `<p class="pc-note">Targets are set by your team${info.updated_by ? " (" + h(info.updated_by) + ")" : ""}${ro ? ": read-only here." : ": changes here are saved for everyone."}</p>` : "";
  $("#pcBody").innerHTML = drift + team + v.errors.map(e => `<div class="prob">${h(e.name)}: ${h(e.error)}</div>`).join("")
    + (v.accounts.length ? v.accounts.map(a => card(a, ro)).join("") : `<p class="pc-note">No ad accounts yet: connect Google Ads or Meta in Setup.</p>`)
    + `<p class="pc-note">${h(v.method)} Recommendations go into the account's draft only: check, preview and Push live there (team approval applies, undo works as usual).</p>`;
  wire();
}

function card(a, ro){
  const c = a.currency, dis = ro ? "disabled" : "";
  const recs = a.campaigns.reduce((n, x) => n + x.recommendations.length, 0);
  const stale = a.stale ? `<span class="chip warn" title="Pacing uses spend up to the day it was fetched">Fetched on an earlier day: Refresh</span>` : "";
  const tiles = [
    ["Monthly target", `<input type="number" min="0" step="1" data-month="${h(a.account)}" value="${a.target ?? ""}" placeholder="none" style="width:110px" ${dis}>`, c],
    ["Spent this month", h(cur(a.mtd, c)), a.expected_to_date != null ? `Expected by today: ${h(cur(a.expected_to_date, c))}` : `${a.days_left} days left`],
    ["Forecast to month end", h(cur(a.forecast, c)), flagChip(a.flag, a.flag_amount, a.flag_pct, c)],
    ["Daily budgets now", h(cur(a.daily_budgets, c)), a.needed_daily != null ? `Needed per day: ${h(cur(a.needed_daily, c))}` : "Set a target to see what is needed"],
  ].map(([k, val, d]) => `<div class="pc-tile"><div class="k">${k}</div><div class="v">${val}</div><div class="d">${d || ""}</div></div>`).join("");
  const rows = a.campaigns.map(x => {
    const tgt = x.target_source === "own" ? x.target : "";
    const ph = x.target_source === "share" ? `share ${Number(x.target).toFixed(0)}` : "none";
    const recs = x.recommendations.map(r => `<div class="pc-rec"><b>${r.level === "adset" ? "Ad set " + h(r.name) + ": " : ""}set daily budget to ${h(cur(r.proposed, c))}</b>
      (now ${h(cur(r.current, c))}, ${r.change_pct > 0 ? "+" : ""}${h(r.change_pct)}%)${r.capped ? ` <span title="${h(r.why)}">step 1, goal ${h(cur(r.goal, c))}</span>` : ""}
      ${r.warnings.map(w => `<span class="w">${h(w)}</span>`).join("")}</div>`).join("");
    const notes = x.notes.concat(x.learning ? [`Ad sets ${x.learning}`] : []).map(n => `<div class="pc-rec">${h(n)}</div>`).join("");
    return `<tr class="${x.active ? "" : "off"}"><td>${h(x.name)}<div class="sub" style="font-size:11px;color:var(--muted)">${h(x.channel)}${x.budget_kind === "adsets" ? " · ad set budgets" : ""}</div></td>
      <td>${h(x.status)}</td><td class="num">${h(cur(x.budget, c))}</td><td class="num">${h(cur(x.run_rate, c))}</td>
      <td class="num">${h(cur(x.mtd, c))}</td><td class="num">${h(cur(x.forecast, c))}</td>
      <td class="num"><input type="number" min="0" step="1" data-camp="${h(x.id)}" data-acct="${h(a.account)}" value="${tgt ?? ""}" placeholder="${h(ph)}" ${dis}></td>
      <td class="num">${h(cur(x.needed_daily, c))}</td><td>${flagChip(x.flag, x.flag_amount, x.flag_pct, c)}</td>
      <td>${recs}${notes}</td>
      <td>${x.recommendations.length ? `<button data-add="${h(x.id)}" data-acct="${h(a.account)}">Add to draft</button>` : ""}</td></tr>`;
  }).join("");
  return `<div class="pc-card"><div class="pc-head"><h3>${h(a.name)}</h3><span class="sub">${a.platform === "meta" ? "Meta" : "Google Ads"} · ${h(c)} · ${h(a.tz)} · as of ${h(a.as_of)}</span>${stale}
      <span class="spacer" style="flex:1"></span><button data-open="${h(a.key)}" title="Open this account in Ads (draft, preview and Push live)">Open in Ads</button></div>
    <div class="pc-tiles">${tiles}</div>
    <div style="overflow:auto"><table class="pc-table"><thead><tr><th>Campaign</th><th>Status</th><th class="num">Budget/day</th><th class="num">Run-rate/day</th>
      <th class="num">Spent</th><th class="num">Forecast</th><th class="num" title="Its own monthly target; empty = a share of the account target">Target</th>
      <th class="num">Needed/day</th><th>Pacing</th><th>Recommendation</th><th></th></tr></thead><tbody>${rows || `<tr><td colspan="11" class="pc-note">No campaigns with spend or delivery this month.</td></tr>`}</tbody></table></div>
    <div class="pc-acts">${recs ? `<button class="primary" data-addall="${h(a.account)}">Add ${recs} budget change${recs === 1 ? "" : "s"} to the draft</button>` : `<span class="pc-note" style="margin:0">${a.target ? "No budget changes needed." : "Set a monthly target to get budget recommendations."}</span>`}</div></div>`;
}

function wire(){
  const B = $("#pcBody"), acct = id => P.v.accounts.find(a => a.account === id);
  B.querySelectorAll("[data-month]").forEach(i => i.onchange = () => saveTargets({account: i.dataset.month, monthly: i.value === "" ? null : +i.value}));
  B.querySelectorAll("[data-camp]").forEach(i => i.onchange = () => saveTargets({account: i.dataset.acct, campaigns: {[i.dataset.camp]: i.value === "" ? null : +i.value}}));
  B.querySelectorAll("[data-add]").forEach(b => b.onclick = () => toDraft(acct(b.dataset.acct), [b.dataset.add]));
  B.querySelectorAll("[data-addall]").forEach(b => b.onclick = () => toDraft(acct(b.dataset.addall)));
  B.querySelectorAll("[data-open]").forEach(b => b.onclick = () => { setArea("ads"); switchAcct(b.dataset.open) });
}

window.AREAS = window.AREAS || {};
window.AREAS.pacing = {label: "Pacing", render};
})();
