// Setup: keys (Keychain), Sign in with Google, move over from AI-Hub. Opens on its own when nothing is set up yet.
(function(){
  const PRESS = (document.querySelector('meta[name="studio-press"]') || {}).content || "";
  const esc = s => String(s ?? "").replace(/[&<>"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
  async function call(path, body){
    const r = await fetch(path, body ? {method:"POST", headers:{"Content-Type":"application/json", "X-Studio-Press":PRESS}, body:JSON.stringify(body)} : {});
    const j = await r.json(); if (!r.ok) throw new Error(j.error || r.statusText); return j;
  }
  const css = `
  #setupPage{position:fixed;inset:0;z-index:50;background:var(--bg,#f8f9fa);overflow:auto;display:none}
  #setupPage.on{display:block}
  .su-wrap{max-width:760px;margin:0 auto;padding:28px 20px 60px}
  .su-wrap h2{font-weight:400;font-size:24px;margin:0 0 4px} .su-sub{color:var(--muted,#5f6368);margin:0 0 20px}
  .su-card{background:var(--panel,#fff);border:1px solid var(--line,#dadce0);border-radius:8px;padding:16px 18px;margin-bottom:14px}
  .su-card h3{margin:0 0 10px;font-size:15px;font-weight:500;display:flex;align-items:center;gap:8px}
  .su-row{display:grid;grid-template-columns:200px 1fr;gap:10px;align-items:start;margin:8px 0}
  .su-row label{padding-top:7px;font-size:13px} .su-row input{width:100%;box-sizing:border-box}
  .su-help{font-size:12px;color:var(--muted,#5f6368);margin-top:3px}
  .su-dot{width:8px;height:8px;border-radius:50%;background:#dadce0;display:inline-block} .su-dot.ok{background:#1e8e3e}
  .su-acts{display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-top:6px}
  .su-top{display:flex;align-items:center;gap:10px;margin-bottom:16px} .su-top .sp{flex:1}
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
      <div class="su-card"><h3>Import from a .env file</h3><p class="su-help" style="margin:0 0 8px">Have your keys in a .env file (KEY=value lines)? Pick it and Studio saves every key it knows: secrets into the Keychain. The file itself is not kept.</p>
        <div class="su-acts"><input type="file" id="suEnvFile" accept=".env,text/plain,*" style="max-width:320px"><span id="suEnvMsg" class="su-help"></span></div></div>
      <div class="su-card"><h3>Use with Claude Code</h3><p class="su-help" style="margin:0 0 8px">Run this once in Terminal. Claude then sees the same accounts and drafts as this app, can add changes to the draft (you still press Push), and can load keys from a .env file with the tool setup_import_env_file.</p>
        <div class="su-acts"><code id="suMcp" style="font-size:12px;background:var(--hover,#f1f3f4);padding:6px 8px;border-radius:4px;word-break:break-all;flex:1">${esc(S.mcp_command)}</code><button id="suMcpCopy">Copy</button></div></div>
      <div class="su-card"><h3><span class="su-dot ${gOk ? "ok" : ""}"></span>Google (Ads, Search Console, GA4)</h3>
        <p class="su-help" style="margin:0 0 8px">${S.google_signed_in ? `Signed in${S.google_account ? " as <b>" + esc(S.google_account) + "</b>" : ""}.` : S.google_service_account ? "Using the service-account key copied from AI-Hub." : "Fill in the OAuth client below, save, then sign in. Your Google user needs access to the Ads accounts."}</p>
        <div class="su-acts"><button id="suGoogle" class="primary">${S.google_signed_in ? "Sign in again" : "Sign in with Google"}</button>${S.google_signed_in ? `<button id="suGoogleOut" class="danger">Sign out</button>` : ""}<span id="suGMsg" class="su-help"></span></div></div>
      ${Object.entries(groups).map(([g, ks]) => `<div class="su-card"><h3><span class="su-dot ${ks.some(k => k.set) ? "ok" : ""}"></span>${esc(g)}</h3>
        ${ks.map(k => `<div class="su-row"><label for="su_${k.name}">${esc(k.label)}</label><div>
          <input id="su_${k.name}" data-k="${k.name}" ${k.secret ? `type="password" autocomplete="off" placeholder="${k.set ? "Saved - type to replace" : ""}"` : `value="${esc(k.value)}"`} ${k.from_env ? "disabled title='Set by the environment'" : ""}>
          <div class="su-help">${esc(k.help)}${k.secret && k.set ? ` <a href="#" data-rm="${k.name}">Remove</a>` : ""}</div></div></div>`).join("")}</div>`).join("")}
      <div class="su-acts"><button id="suSave" class="primary">Save</button><span id="suMsg" class="su-help"></span></div></div>`;
    const $ = id => document.getElementById(id);
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
  window.openSetup = open;
  // first run: nothing set up yet -> open on its own
  call("/api/setup").then(s => { if (!s.ready && !s.keys.some(k => k.set)) open() }).catch(() => {});
})();
