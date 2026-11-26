// Setup: keys (Keychain), Sign in with Google, move over from AI-Hub. Opens on its own when nothing is set up yet.
// Also the shared en-GB formatters for every static/*.js file (this file loads first): window.studioFmt.
window.studioFmt = window.studioFmt || (() => {
  const MON = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
  const two = n => String(n).padStart(2, "0");
  // ISO strings, "YYYY-MM-DD", "YYYYMMDD" (Google Ads Editor), "YYYY-MM-DD HH:MM", epoch seconds or ms, Date -> Date (or null)
  const parse = v => {
    if (v == null || v === "") return null;
    if (v instanceof Date) return isNaN(v) ? null : v;
    if (typeof v === "number") return new Date(v < 1e12 ? v * 1000 : v);
    const s = String(v).trim();
    if (/^\d{8}$/.test(s)) return new Date(+s.slice(0, 4), +s.slice(4, 6) - 1, +s.slice(6, 8));
    if (/^\d{4}-\d{2}-\d{2}$/.test(s)) return new Date(s + "T12:00:00");
    const d = new Date(/^\d{4}-\d{2}-\d{2} \d/.test(s) ? s.replace(" ", "T") : s);
    return isNaN(d) ? null : d;
  };
  const date = v => { const d = parse(v); return d ? `${d.getDate()} ${MON[d.getMonth()]} ${d.getFullYear()}` : (v == null ? "" : String(v)) };   // 25 Nov 2026
  const time = v => { const d = parse(v); return d ? `${two(d.getHours())}:${two(d.getMinutes())}` : "" };
  const dateTime = v => { const d = parse(v); return d ? `${date(d)}, ${time(d)}` : (v == null ? "" : String(v)) };             // 25 Nov 2026, 14:05
  const num = (v, dp) => v == null || v === "" || isNaN(v) ? "" : Number(v).toLocaleString("en-GB", dp == null ? {} : {minimumFractionDigits: dp, maximumFractionDigits: dp});
  const money = (v, cur) => { if (v == null || v === "" || isNaN(v)) return "";
    try { return cur ? Number(v).toLocaleString("en-GB", {style: "currency", currency: cur}) : num(v, 2) } catch(e) { return num(v, 2) + " " + cur } };
  return {parse, date, time, dateTime, num, money, MON};
})();
(function(){
  const PRESS = (document.querySelector('meta[name="studio-press"]') || {}).content || "";
  const esc = s => String(s ?? "").replace(/[&<>"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
  async function call(path, body){
    const r = await fetch(path, body ? {method:"POST", headers:{"Content-Type":"application/json", "X-Studio-Press":PRESS}, body:JSON.stringify(body)} : {});
    const j = await r.json(); if (!r.ok) throw new Error(j.error || r.statusText); return j;
  }
  const css = `
  #setupPage{position:fixed;inset:0;z-index:50;background:var(--bg);overflow:auto;display:none}
  #setupPage.on{display:block;animation:su-in var(--t) var(--ease)}
  @keyframes su-in{from{opacity:0;transform:translateY(4px)}to{opacity:1;transform:none}}
  .su-wrap{max-width:780px;margin:0 auto;padding:36px 24px 60px}
  .su-wrap h2{font-weight:600;font-size:26px;letter-spacing:-.3px;margin:0 0 4px;color:var(--text)} .su-sub{color:var(--muted);font-size:14px;font-weight:500;margin:0 0 4px}
  .su-card{background:var(--panel);border-radius:var(--r-lg);box-shadow:var(--sh-card);padding:20px 22px;margin-bottom:12px}
  .su-card h3{margin:0 0 10px;font-size:16px;font-weight:600;color:var(--text);display:flex;align-items:center;gap:8px}
  .su-card details summary{margin:4px 0}
  .su-row{display:grid;grid-template-columns:200px 1fr;gap:12px;align-items:start;margin:10px 0}
  .su-row label{padding-top:8px;font-size:13px;font-weight:600;color:var(--text2)} .su-row input{width:100%;box-sizing:border-box}
  .su-help{font-size:12px;font-weight:500;color:var(--muted);margin-top:4px;line-height:1.5}
  .su-help a{color:var(--accent);font-weight:600;text-decoration:none} .su-help a:hover{text-decoration:underline}
  .su-dot{width:8px;height:8px;border-radius:50%;background:var(--line);display:inline-block;flex:none} .su-dot.ok{background:var(--ok);box-shadow:0 0 0 3px var(--ok-bg)}
  .su-acts{display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-top:8px}
  .su-code{font:12px/1.5 var(--mono);background:var(--sunken);color:var(--text2);padding:8px 10px;border-radius:var(--r-sm);word-break:break-all;flex:1}
  .su-top{display:flex;align-items:flex-start;gap:12px;margin-bottom:22px} .su-top .sp{flex:1}
  .su-file{display:inline-flex;align-items:center;height:34px;padding:0 14px;border-radius:var(--r);background:var(--panel);box-shadow:var(--sh-1),0 0 0 1px var(--line-strong);font-size:13px;font-weight:600;color:var(--text);cursor:pointer;margin:0}
  .su-file:hover{background:var(--hover)} .su-file:focus-within{box-shadow:var(--ring)}
  .su-file input{position:absolute;width:1px;height:1px;opacity:0;pointer-events:none}
  .su-card details.su-tech{margin-top:6px;font-size:12px;font-weight:500;color:var(--muted);line-height:1.5}
  .su-card details.su-tech summary{cursor:pointer;color:var(--text2);font-weight:600;padding:7px 0}
  #setupPage button.danger{color:var(--bad-text, #c8303a)}
  @media (max-width:640px){.su-row{grid-template-columns:1fr}}`;
  document.head.insertAdjacentHTML("beforeend", `<style>${css}</style>`);
  document.body.insertAdjacentHTML("beforeend", `<div id="setupPage" role="dialog" aria-label="Setup"></div>`);
  const page = document.getElementById("setupPage");
  let S = null, poll = null;

  function draw(){
    const groups = {};
    S.keys.forEach(k => (groups[k.group] = groups[k.group] || []).push(k));
    const gOk = S.google_signed_in || S.google_service_account;
    page.innerHTML = `<div class="su-wrap">
      <div class="su-top"><div><h2>Setup</h2><p class="su-sub">Your keys stay on this Mac: secrets in the macOS Keychain, the rest in ${esc(S.home)}. Nothing is sent anywhere else.</p></div>
        <span class="sp"></span><button id="suClose">${S.ready ? "Done" : "Later"}</button></div>
      ${S.keychain_error ? `<div class="su-card"><p class="prob" style="margin:0">${esc(S.keychain_error)} Studio never stores secrets anywhere else.</p></div>` : ""}
      <div class="su-card"><h3>Import from a .env file</h3><p class="su-help" style="margin:0 0 8px">Have your keys in a .env file (KEY=value lines)? Pick it and Studio saves every key it knows: secrets into the Keychain. The file itself is not kept.</p>
        <div class="su-acts"><label class="su-file">Choose .env file<input type="file" id="suEnvFile" accept=".env,text/plain,*"></label><span id="suEnvMsg" class="su-help"></span></div></div>
      <div class="su-card"><h3>Use with Claude Code</h3><p class="su-help" style="margin:0 0 8px">Run this once in Terminal. Claude then sees the same accounts and drafts as this app and can add changes to the draft. You still press Push yourself.</p>
        <div class="su-acts"><code id="suMcp" class="su-code">${esc(S.mcp_command)}</code><button id="suMcpCopy">Copy</button></div>
        <details class="su-tech"><summary>Technical details</summary>The command registers Studio as an MCP server in Claude Code. Claude can also load keys from a .env file with the tool setup_import_env_file.</details></div>
      <div class="su-card" id="suTeam"></div>
      <div class="su-card"><h3><span class="su-dot ${gOk ? "ok" : ""}"></span>Google (Ads, Search Console, GA4)</h3>
        <p class="su-help" style="margin:0 0 8px">${S.google_signed_in ? `Signed in${S.google_account ? " as <b>" + esc(S.google_account) + "</b>" : ""}.` : S.google_service_account ? "Connected with a Google robot account copied from AI-Hub." : "Fill in the Google sign-in details below, save, then sign in. Your Google user needs access to the Ads accounts."}</p>
        <div class="su-acts"><button id="suGoogle" class="${S.google_signed_in ? "" : "primary"}">${S.google_signed_in ? "Sign in again" : "Sign in with Google"}</button>${S.google_signed_in ? `<button id="suGoogleOut" class="danger">Sign out</button>` : ""}<span id="suGMsg" class="su-help"></span></div>
        <details class="su-tech"><summary>Technical details</summary>Sign in uses an OAuth client (Client ID and secret from Google Cloud) with the Google Ads, Search Console and Analytics scopes.${S.google_service_account ? " A service-account key from AI-Hub is also on this Mac and is used when nobody is signed in." : ""}</details></div>
      ${Object.entries(groups).map(([g, ks]) => `<div class="su-card"><h3><span class="su-dot ${ks.some(k => k.set) ? "ok" : ""}"></span>${esc(g)}</h3>
        ${ks.map(k => `<div class="su-row"><label for="su_${k.name}">${esc(k.label)}</label><div>
          <input id="su_${k.name}" data-k="${k.name}" ${k.secret ? `type="password" autocomplete="off" placeholder="${k.set ? "Saved - type to replace" : ""}"` : `value="${esc(k.value)}"`} ${k.from_env ? "disabled title='Set by the environment'" : ""}>
          <div class="su-help">${esc(k.help)}${k.secret && k.set ? ` <a href="#" data-rm="${k.name}">Remove</a>` : ""}</div></div></div>`).join("")}</div>`).join("")}
      <div class="su-acts"><button id="suSave" class="primary">Save</button><span id="suMsg" class="su-help"></span></div></div>`;
    const $ = id => document.getElementById(id);
    if (window.teamCard) window.teamCard($("suTeam")); else $("suTeam").remove();
    $("suClose").onclick = close;
    $("suSave").onclick = async () => {
      const values = {}; page.querySelectorAll("input[data-k]").forEach(i => { if (!i.disabled) values[i.dataset.k] = i.value });
      try { S = await call("/api/setup/save", {values}); draw(); document.getElementById("suMsg").textContent = "Saved. Refresh data to load with the new keys." } catch(e) { $("suMsg").textContent = e.message }
    };
    page.querySelectorAll("[data-rm]").forEach(a => a.onclick = async e => { e.preventDefault(); if (!confirm("Remove this key from the Keychain?")) return;
      try { S = await call("/api/setup/save", {values:{[a.dataset.rm]: null}}); draw() } catch(err) { alert(err.message) } });
    $("suEnvFile").onchange = async e => { const f = e.target.files[0]; if (!f) return; $("suEnvMsg").textContent = "Importing ...";
      try { const r = await call("/api/setup/import_env", {text: await f.text()}); S = r.status; draw();
        document.getElementById("suEnvMsg").textContent = r.imported.length ? `Saved ${r.imported.length} key${r.imported.length > 1 ? "s" : ""}: ${r.imported.join(", ")}.` + (r.ignored.length ? ` Not used: ${r.ignored.length} other line${r.ignored.length > 1 ? "s" : ""}.` : "") : "No keys Studio knows were found in that file." }
      catch(err) { $("suEnvMsg").textContent = err.message } };
    $("suMcpCopy").onclick = async () => { try { await navigator.clipboard.writeText(S.mcp_command); $("suMcpCopy").textContent = "Copied" } catch(e) { const r = document.createRange(); r.selectNodeContents($("suMcp")); getSelection().removeAllRanges(); getSelection().addRange(r) } };
    $("suGoogle").onclick = async () => {
      try { const r = await call("/api/setup/google/start", {}); window.open(r.url, "_blank"); $("suGMsg").textContent = "Finish in the browser, then come back here.";
        clearInterval(poll); let n = 0; poll = setInterval(async () => { if (++n > 150) return clearInterval(poll); const s = await call("/api/setup"); if (s.google_signed_in && s.google_account !== S.google_account || s.google_signed_in && !S.google_signed_in){ clearInterval(poll); S = s; draw() } }, 2000) }
      catch(e) { $("suGMsg").textContent = e.message } };
    if ($("suGoogleOut")) $("suGoogleOut").onclick = async () => { if (!confirm("Sign Marketing Studio out of Google?")) return; S = await call("/api/setup/google/sign_out", {}); draw() };
  }
  async function open(){ try { S = await call("/api/setup") } catch(e) { return alert(e.message) } draw(); page.classList.add("on") }
  function close(){ page.classList.remove("on"); clearInterval(poll) }
  // Escape closes Setup like a dialog (not while a real <dialog> on top of it is open)
  document.addEventListener("keydown", e => { if (e.key === "Escape" && page.classList.contains("on") && !document.querySelector("dialog[open]")){ e.preventDefault(); close() } });
  window.openSetup = open;
  // first run: nothing set up yet -> open on its own
  call("/api/setup").then(s => { if (!s.ready && !s.keys.some(k => k.set)) open() }).catch(() => {});
})();
