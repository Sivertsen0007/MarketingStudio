// [videos] The Videos tab in the Google Ads area: YouTube video assets, uploads and Demand Gen (video ads on YouTube).
// Reads: GET /api/videos (cached until Refresh), /api/videos/info, /api/videos/uploads, /api/videos/upload_status.
// Everything that changes Google Ads goes into the draft (/api/draft/add) and reaches Google only with Push live.
// A video file upload is a video_upload draft item that runs only from its own Upload button here (gated like a push).
// Uses the page's helpers: api, qs, S, toast, showDialog, dlg, poll, addChanges, withProgress, lastProgress, PRESS.
(() => {
  const V = {lib: null, acct: null, q: "", hist: [], busy: false, err: ""};
  const h = s => String(s ?? "").replace(/[&<>"']/g, c => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[c]));
  const el = id => document.getElementById(id);
  const v = id => ((el(id) || {}).value || "").trim();
  const ls = id => v(id).split("\n").map(x => x.trim()).filter(Boolean);
  const CHANNELS = [["youtube_in_stream", "YouTube in-stream"], ["youtube_in_feed", "YouTube in-feed"], ["youtube_shorts", "YouTube Shorts"],
    ["discover", "Discover"], ["gmail", "Gmail"], ["display", "Display Network"], ["maps", "Maps"]];
  const CTA = ["learn_more", "book_now", "get_quote", "sign_up", "contact_us", "subscribe", "download", "shop_now", "buy_now", "apply_now",
    "order_now", "see_more", "start_now", "visit_site", "watch_now"];
  const label = x => x.replace(/_/g, " ").replace(/^./, c => c.toUpperCase());
  const dur = s => s == null ? "" : `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
  const trial = name => ((V.lib || {}).campaigns || []).some(c => c.campaign === name && c.trial);
  const uploads = () => (S.draftItems || []).filter(i => i.change && i.change.type === "video_upload");

  async function load(refresh){
    V.busy = true; V.err = "";
    try {
      V.lib = refresh ? await api("/api/videos/refresh", {}) : await api("/api/videos?" + qs({acct: S.acct}));
      V.acct = S.acct;
      V.hist = await api("/api/videos/uploads?" + qs({acct: S.acct}));
    } catch(e) { V.err = e.message } finally { V.busy = false }
  }

  function render(root){
    root = root || document.getElementById("grid");
    if (V.acct !== S.acct && !V.busy){ V.lib = null; root.innerHTML = `<div class="empty">Loading videos and Demand Gen (read once, then kept until Refresh) ...</div>`;
      return load(false).then(() => S.kind === "videos" && render(document.getElementById("grid"))) }
    if (!V.lib){ root.innerHTML = `<div class="empty">${V.err ? h(V.err) : "Loading ..."}</div>`; return }
    const L = V.lib, q = V.q.toLowerCase();
    const vids = L.videos.filter(x => !q || `${x.title} ${x.name} ${x.video_id}`.toLowerCase().includes(q));
    const pend = uploads();
    const errs = Object.entries(L.errors || {});
    root.innerHTML = `<div style="padding:18px 24px;overflow:auto;height:100%;box-sizing:border-box">
      <div style="display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin-bottom:8px">
        <button class="primary" id="vdNewCamp" title="A Demand Gen campaign: video ads on YouTube (Video campaigns can't be made through the API)">+ Demand Gen campaign</button>
        <button id="vdNewGroup">+ Ad group</button><button id="vdNewAd">+ Video ad</button>
        <button id="vdAddVids" title="Add YouTube videos to the account's asset library by URL or id">+ Videos by URL</button>
        <button id="vdUpload" title="Upload a video file from this Mac to YouTube (unlisted, on the account's Google-managed channel)">Upload from Mac</button>
        <span style="flex:1"></span><span class="hint">Read ${h(String(L.fetched_at || "").replace("T", " ").slice(0, 16))}</span>
        <button id="vdRefresh" title="Read again from Google Ads (read-only)">Refresh</button></div>
      <p class="hint" style="margin:0 0 10px">Video ads on YouTube are made as Demand Gen video ads (Google doesn't let tools create Video campaigns). Pick
        <b>YouTube only</b> channels to keep them on YouTube. Everything here goes into the draft; Push live sends it, new campaigns Paused.</p>
      ${errs.length ? `<p class="prob">Some parts didn't load: ${errs.map(([k, e]) => h(k + ": " + e)).join("; ")}</p>` : ""}
      ${pend.length ? `<h3 style="font-weight:600;font-size:16px;margin:22px 0 10px;color:var(--text)">Waiting to upload</h3><table><thead><tr><th>Video</th><th>File</th><th>By</th><th></th></tr></thead><tbody>
        ${pend.map(i => `<tr><td>${h(i.change.title)}</td><td>${h(i.change.file_name || String(i.change.file || "").split("/").pop())}</td><td>${i.by === "claude" ? "Claude" : h(i.by)}</td>
          <td>${i.problems.length ? `<span class="prob">${h(i.problems.join("; "))}</span>` : `<button class="live" data-up="${h(i.id)}">Upload</button>`}</td></tr>`).join("")}</tbody></table>` : ""}
      <h3 style="font-weight:600;font-size:16px;margin:22px 0 10px;color:var(--text)">Demand Gen ads (${L.ads.length})</h3>
      ${L.ads.length ? `<table><thead><tr><th>Status</th><th>Campaign</th><th>Ad group</th><th>Type</th><th>Headline</th><th>Videos</th><th>Approval</th><th></th></tr></thead><tbody>
        ${L.ads.map(a => { const ro = trial(a.campaign); return `<tr><td>${h(a.status)}</td><td>${h(a.campaign)}${ro ? ` <span class="chip warn">trial</span>` : ""}</td><td>${h(a.ad_group)}</td>
          <td>${h(a.type)}</td><td>${h(a.headlines[0] || a.name || "")}</td><td>${a.videos.length || ""}</td><td>${h(a.approval)}</td>
          <td style="white-space:nowrap">${ro ? "" : `<button data-st="${h(a.ad_id)}" data-to="${a.status === "Enabled" ? "paused" : "enabled"}">${a.status === "Enabled" ? "Pause" : "Enable"}</button>
          ${a.video ? `<button data-edit="${h(a.ad_id)}">Edit</button>` : ""}`}</td></tr>` }).join("")}</tbody></table>` : `<p class="hint">No Demand Gen ads yet.</p>`}
      <h3 style="font-weight:600;font-size:16px;margin:22px 0 10px;color:var(--text)">Demand Gen ad groups (${L.ad_groups.length})</h3>
      ${L.ad_groups.length ? `<table><thead><tr><th>Status</th><th>Campaign</th><th>Ad group</th><th>Channels</th><th></th></tr></thead><tbody>
        ${L.ad_groups.map(g => `<tr><td>${h(g.status)}</td><td>${h(g.campaign)}</td><td>${h(g.ad_group)}</td><td>${h(g.channels)}</td>
          <td>${trial(g.campaign) ? `<span class="chip warn">trial</span>` : `<button data-grp="${h(g.id)}">Change</button>`}</td></tr>`).join("")}</tbody></table>` : `<p class="hint">No Demand Gen ad groups.</p>`}
      <h3 style="font-weight:600;font-size:16px;margin:22px 0 10px;color:var(--text)">Uploads from Studio</h3>
      ${V.hist.length ? `<table><thead><tr><th>When</th><th>Title</th><th>File</th><th>Result</th><th></th></tr></thead><tbody>
        ${V.hist.slice(0, 20).map((r, i) => `<tr><td>${h(r.ts.replace("T", " ").slice(0, 16))}</td><td>${h(r.title)}</td><td>${h(r.file)}</td>
          <td>${r.ok ? "Uploaded" : `<span class="prob">${h(r.error || "failed")}</span>`}<span id="vdSt${i}"></span></td>
          <td>${r.ok && r.resource ? `<button data-chk="${i}">Check status</button>` : ""}</td></tr>`).join("")}</tbody></table>`
        : `<p class="hint">None yet. Upload from Mac sends a file to YouTube (unlisted) through Google Ads; it needs your Upload click (and the team's approval if your team requires it).</p>`}
      <h3 style="font-weight:600;font-size:16px;margin:22px 0 10px;color:var(--text)">Video assets (${L.videos.length}) <input id="vdQ" placeholder="Search title or id" value="${h(V.q)}" style="margin-left:8px;font-weight:500;font-size:13px"></h3>
      <table><thead><tr><th>Title</th><th>YouTube id</th><th>Length</th><th>Asset id</th><th></th></tr></thead><tbody>
        ${vids.slice(0, 300).map(x => `<tr><td>${h(x.title || x.name)}</td><td><a href="https://www.youtube.com/watch?v=${encodeURIComponent(x.video_id)}" target="_blank" rel="noopener">${h(x.video_id)}</a></td>
          <td>${dur(x.duration_s)}</td><td>${h(x.asset_id)}</td><td><button data-use="${h(x.video_id)}">Use in a video ad</button></td></tr>`).join("")}
      </tbody></table>${vids.length > 300 ? `<p class="hint">${vids.length - 300} more: search to narrow.</p>` : ""}
      ${L.uploads.length ? `<p class="hint">Google Ads lists ${L.uploads.length} YouTube upload${L.uploads.length === 1 ? "" : "s"} for this account (${L.uploads.filter(u => u.state === "PROCESSED").length} processed).</p>` : ""}
    </div>`;
    wire(root);
  }

  function wire(root){
    const on = (id, fn) => { const b = el(id); if (b) b.onclick = fn };
    on("vdRefresh", async () => { el("vdRefresh").disabled = true; await load(true); render() });
    on("vdNewCamp", () => campaignDialog());
    on("vdNewGroup", () => groupDialog());
    on("vdNewAd", () => adDialog({}));
    on("vdAddVids", () => videosDialog());
    on("vdUpload", () => uploadDialog());
    const q = el("vdQ"); if (q) q.oninput = () => { V.q = q.value; clearTimeout(V.t); V.t = setTimeout(() => { render(); const n = el("vdQ"); n.focus(); n.setSelectionRange(n.value.length, n.value.length) }, 250) };
    root.querySelectorAll("[data-st]").forEach(b => b.onclick = () => { const a = V.lib.ads.find(x => x.ad_id === b.dataset.st);
      addChanges([{type: "dg_ad", campaign: a.campaign, ad_group: a.ad_group, ad_id: a.ad_id, status: b.dataset.to}]).catch(e => toast(e.message)) });
    root.querySelectorAll("[data-edit]").forEach(b => b.onclick = () => adDialog(V.lib.ads.find(x => x.ad_id === b.dataset.edit), true));
    root.querySelectorAll("[data-grp]").forEach(b => b.onclick = () => groupEditDialog(V.lib.ad_groups.find(x => x.id === b.dataset.grp)));
    root.querySelectorAll("[data-use]").forEach(b => b.onclick = () => adDialog({videos: [b.dataset.use]}));
    root.querySelectorAll("[data-up]").forEach(b => b.onclick = () => uploadNow(b.dataset.up));
    root.querySelectorAll("[data-chk]").forEach(b => b.onclick = async () => {
      const r = V.hist[+b.dataset.chk], out = el("vdSt" + b.dataset.chk);
      try { const s = await api("/api/videos/upload_status?" + qs({acct: S.acct, resource: r.resource}));
        out.innerHTML = ` · ${h(label(s.state.toLowerCase()))}` + (s.video_id ? ` · <a href="https://www.youtube.com/watch?v=${encodeURIComponent(s.video_id)}" target="_blank" rel="noopener">${h(s.video_id)}</a>
          <button data-lib="${h(s.video_id)}">Add to library</button> <button data-use2="${h(s.video_id)}">Use in a video ad</button>` : "");
        out.querySelectorAll("[data-lib]").forEach(x => x.onclick = () => addChanges([{type: "video_asset", videos: [{id: x.dataset.lib, name: r.title}]}]).catch(e => toast(e.message)));
        out.querySelectorAll("[data-use2]").forEach(x => x.onclick = () => adDialog({videos: [x.dataset.use2]}));
      } catch(e) { out.textContent = " · " + e.message }
    });
  }

  // ---- dialogs ----
  const noteRow = `<div class="row"><label>Why (optional)</label><input id="vd_note" placeholder="Shown in the draft and the changelog"></div>`;
  const channelRows = (cur) => `<div class="row"><label>Channels</label><span><select id="vd_chp">
      <option value="youtube" ${cur === "youtube" ? "selected" : ""}>YouTube only (in-stream, in-feed, Shorts)</option>
      <option value="all" ${cur === "all" ? "selected" : ""}>All channels</option>
      <option value="google_owned" ${cur === "google_owned" ? "selected" : ""}>Google-owned channels only</option>
      <option value="pick" ${cur === "pick" ? "selected" : ""}>Pick channels ...</option>${cur === "" ? `<option value="" selected>Leave as it is</option>` : ""}</select>
      <span id="vd_chs" style="display:${cur === "pick" ? "block" : "none"}">${CHANNELS.map(([k, l]) => `<label class="tick" style="display:inline-flex;margin-right:10px"><input type="checkbox" data-ch="${k}"> ${h(l)}</label>`).join("")}</span></span></div>`;
  const wireChannels = () => { const s = el("vd_chp"); s.onchange = () => { el("vd_chs").style.display = s.value === "pick" ? "block" : "none" } };
  const chosenChannels = () => { const p = v("vd_chp"); return p === "pick" ? [...document.querySelectorAll("[data-ch]")].filter(c => c.checked).map(c => c.dataset.ch) : p || undefined };
  const locs = () => v("vd_loc").split(/[,\s]+/).filter(Boolean).map(x => ({id: Number(x)}));
  const langs = () => v("vd_lang").split(/[,\s]+/).filter(Boolean).join(",");
  const campOpts = (sel) => V.lib.campaigns.filter(c => !c.trial).map(c => `<option ${c.campaign === sel ? "selected" : ""}>${h(c.campaign)}</option>`).join("");
  async function submit(changes){ try { await addChanges(changes, v("vd_note")); dlg.close(); render() } catch(e) { toast(e.message) } }

  function campaignDialog(){
    showDialog("New Demand Gen campaign", `<p class="hint">Created <b>Paused</b> with its first ad group. Daily budget, not shared; Demand Gen's minimum is about 5 USD (4 GBP) a day.
      Locations and languages go on the ad group.</p>
      <div class="row"><label>Campaign name</label><input id="vd_name" placeholder="[DG] YouTube - ..."></div>
      <div class="row"><label>Budget per day</label><input id="vd_budget" type="number" step="0.01" min="0"></div>
      <div class="row"><label>Bidding</label><select id="vd_bid"><option value="maximize_conversions">Maximise conversions</option><option value="target_cpa">Target CPA</option><option value="maximize_clicks">Maximise clicks</option></select></div>
      <div class="row"><label>Target CPA</label><input id="vd_cpa" type="number" step="0.01" min="0" placeholder="needed for Target CPA, optional for Maximise conversions"></div>
      <div class="row"><label>First ad group</label><input id="vd_ag" placeholder="Ad group name"></div>
      <div class="row"><label>Location IDs</label><input id="vd_loc" placeholder="2826, 2276 (find them in the Add campaign dialog)"></div>
      <div class="row"><label>Languages</label><input id="vd_lang" placeholder="en, de"></div>${channelRows("youtube")}
      <div class="row"><label>Start / end</label><span><input id="vd_start" type="date"> <input id="vd_end" type="date"> <span class="hint">optional</span></span></div>${noteRow}`,
      [["Cancel", () => dlg.close()], ["Add to draft", () => {
        const c = {type: "dg_campaign", new: true, campaign: v("vd_name"), budget: Number(v("vd_budget")), bidding: v("vd_bid"), ad_group: v("vd_ag"),
                   locations: locs(), languages: langs(), channels: chosenChannels()};
        if (v("vd_cpa")) c.target_cpa = Number(v("vd_cpa"));
        if (v("vd_start")) c.start_date = v("vd_start"); if (v("vd_end")) c.end_date = v("vd_end");
        submit([c]) }, "primary"]]);
    wireChannels();
  }

  function groupDialog(){
    if (!V.lib.campaigns.some(c => !c.trial)) return toast("No Demand Gen campaign yet: make one first");
    showDialog("New Demand Gen ad group", `<p class="hint">Added Paused unless you tick Enabled when you push. If the campaign sets locations and languages for the
      whole campaign, leave them empty here: the ad group uses the campaign's.</p>
      <div class="row"><label>Campaign</label><select id="vd_camp">${campOpts()}</select></div>
      <div class="row"><label>Ad group name</label><input id="vd_ag"></div>
      <div class="row"><label>Location IDs</label><input id="vd_loc" placeholder="optional"></div>
      <div class="row"><label>Languages</label><input id="vd_lang" placeholder="optional, e.g. en"></div>${channelRows("youtube")}${noteRow}`,
      [["Cancel", () => dlg.close()], ["Add to draft", () => submit([{type: "dg_ad_group", new: true, campaign: v("vd_camp"), ad_group: v("vd_ag"),
        locations: locs(), languages: langs(), channels: chosenChannels()}]), "primary"]]);
    wireChannels();
  }

  function groupEditDialog(g){
    showDialog("Change ad group", `<p class="hint">${h(g.campaign)} / ${h(g.ad_group)} · now: ${h(g.channels)}, ${h(g.status)}</p>
      <div class="row"><label>Status</label><select id="vd_st"><option value="">Leave as it is</option><option value="enabled">Enabled</option><option value="paused">Paused</option></select></div>
      ${channelRows("")}${noteRow}`,
      [["Cancel", () => dlg.close()], ["Add to draft", () => { const c = {type: "dg_ad_group", campaign: g.campaign, ad_group: g.ad_group};
        if (v("vd_st")) c.status = v("vd_st"); const ch = chosenChannels(); if (ch && (!Array.isArray(ch) || ch.length)) c.channels = ch;
        submit([c]) }, "primary"]]);
    wireChannels();
  }

  function adDialog(a, edit){
    a = a || {};
    if (!V.lib.campaigns.some(c => !c.trial)) return toast("No Demand Gen campaign yet: make one first (+ Demand Gen campaign)");
    const groups = camp => V.lib.ad_groups.filter(g => g.campaign === camp).map(g => `<option ${g.ad_group === a.ad_group ? "selected" : ""}>${h(g.ad_group)}</option>`).join("");
    const camp0 = a.campaign || (V.lib.campaigns.find(c => !c.trial) || {}).campaign;
    const logos = V.lib.logos.map(l => `<option value="${h(l.asset_id)}" ${l.asset_id === a.logo_asset_id ? "selected" : ""}>${h(l.name || "Image " + l.asset_id)} (${h(l.size)})</option>`).join("");
    const t = (id, lab, list, n, hint) => `<div class="row"><label>${lab}</label><span><textarea id="${id}" rows="${n}" placeholder="one per line">${h((list || []).join("\n"))}</textarea><span class="hint">${hint}</span></span></div>`;
    showDialog(edit ? `Edit Demand Gen video ad ${a.ad_id}` : "New Demand Gen video ad", `<p class="hint">${edit ? "Only what you change is sent; a list you change replaces the whole list." :
      "Added Paused unless you tick Enabled when you push. Videos are YouTube ids or links; ones not in the account yet are added as video assets."}</p>
      <div class="row"><label>Campaign</label><select id="vd_camp" ${edit ? "disabled" : ""}>${campOpts(camp0)}</select></div>
      <div class="row"><label>Ad group</label><select id="vd_grp" ${edit ? "disabled" : ""}>${groups(camp0)}</select></div>
      ${t("vd_vids", "Videos", a.videos, 3, "1 to 5 YouTube links or ids <button id=\"vd_chk\" class=\"link\">Check</button> <span id=\"vd_chkOut\"></span>")}
      ${t("vd_h", "Headlines", a.headlines, 3, "1 to 5, up to 40 characters (3 or more recommended)")}
      ${t("vd_lh", "Long headlines", a.long_headlines, 3, "1 to 5, up to 90 characters")}
      ${t("vd_d", "Descriptions", a.descriptions, 3, "1 to 5, up to 90 characters")}
      <div class="row"><label>Business name</label><input id="vd_bn" maxlength="25" value="${h(a.business_name || "")}"></div>
      <div class="row"><label>Logo</label><span><select id="vd_logo"><option value="">${edit ? "Keep the current logo" : "Pick a square logo ..."}</option>${logos}</select>
        <input id="vd_logof" placeholder="or a full path to a square PNG / JPG on this Mac" style="margin-top:4px"></span></div>
      <div class="row"><label>Final URL</label><input id="vd_url" value="${h(a.final_url || "")}" placeholder="https://"></div>
      <div class="row"><label>Call to action</label><select id="vd_cta"><option value="">${edit ? "Keep" : "Automatic"}</option>${CTA.map(x => `<option value="${x}">${h(label(x))}</option>`).join("")}</select></div>${noteRow}`,
      [["Cancel", () => dlg.close()], ["Add to draft", () => {
        const c = {type: "dg_video_ad", campaign: edit ? a.campaign : v("vd_camp"), ad_group: edit ? a.ad_group : v("vd_grp")};
        const same = (x, y) => JSON.stringify(x || []) === JSON.stringify(y || []);
        const put = (k, val, old) => { if (!edit || !same(val, old)) c[k] = val };
        put("videos", ls("vd_vids"), a.videos); put("headlines", ls("vd_h"), a.headlines); put("long_headlines", ls("vd_lh"), a.long_headlines);
        put("descriptions", ls("vd_d"), a.descriptions);
        if (!edit || v("vd_bn") !== (a.business_name || "")) c.business_name = v("vd_bn");
        if (!edit || v("vd_url") !== (a.final_url || "")) c.final_url = v("vd_url");
        if (v("vd_logof")) c.logo_file = v("vd_logof"); else if (v("vd_logo") && v("vd_logo") !== a.logo_asset_id) c.logo_asset_id = v("vd_logo");
        if (v("vd_cta")) c.call_to_action = v("vd_cta");
        if (edit){ c.edit = true; c.ad_id = a.ad_id }
        submit([c]) }, "primary"]]);
    el("vd_camp").onchange = () => { el("vd_grp").innerHTML = groups(v("vd_camp")) };
    el("vd_chk").onclick = async e => { e.preventDefault(); checkVideos(ls("vd_vids"), el("vd_chkOut")) };
  }

  async function checkVideos(list, out){
    out.textContent = "Checking ...";
    const res = [];
    for (const x of list.slice(0, 10)){
      try { const r = await api("/api/videos/info?" + qs({acct: S.acct, v: x}));
        res.push(`${h(r.video_id)}: ${r.title ? h(r.title) : "id is fine (Google Ads hasn't seen it yet)"}${r.duration_s != null ? " · " + dur(r.duration_s) : ""}${r.in_library ? " · in the library" : ""}`)
      } catch(e) { res.push(`<span class="prob">${h(e.message)}</span>`) }
    }
    out.innerHTML = res.join("<br>") || "Nothing to check";
  }

  function videosDialog(){
    showDialog("Add YouTube videos to the library", `<p class="hint">YouTube links or ids, one per line. They go into the draft and become video assets with Push live.
      A video must be public or unlisted on YouTube.</p>
      <div class="row"><label>Videos</label><textarea id="vd_vids" rows="5" placeholder="https://www.youtube.com/watch?v=..."></textarea></div>
      <p><button id="vd_chk">Check</button> <span id="vd_chkOut" class="hint"></span></p>${noteRow}`,
      [["Cancel", () => dlg.close()], ["Add to draft", () => submit([{type: "video_asset", videos: ls("vd_vids")}]), "primary"]]);
    el("vd_chk").onclick = () => checkVideos(ls("vd_vids"), el("vd_chkOut"));
  }

  function uploadDialog(){
    showDialog("Upload a video from this Mac", `<p class="hint">Uploads go to YouTube through Google Ads, to the account's <b>Google-managed channel</b>, as an
      <b>unlisted</b> video (the only option there). It goes into the draft first; it uploads only when you press Upload on it in this tab,
      and needs your team's approval if your team requires it. It is never part of Push live, and Claude can't start it.</p>
      <p class="hint">Your own brand channel needs that channel's own Google sign-in, which Studio doesn't have: upload there in YouTube or Google Ads,
      then add the video here by its link. Linking a channel to the Ads account is done in Google Ads.</p>
      <div class="row"><label>File</label><span><input type="file" id="vd_file" accept="video/*,.mp4,.mov,.m4v,.avi,.wmv,.flv,.mpg,.mpeg,.webm,.3gp,.mkv">
        <input id="vd_path" placeholder="or the full path (Finder: Option + right-click, Copy as Pathname)" style="margin-top:4px"><span id="vd_fileOut" class="hint"></span></span></div>
      <div class="row"><label>Title</label><input id="vd_title" maxlength="100"></div>
      <div class="row"><label>Description</label><textarea id="vd_desc" rows="3" maxlength="5000"></textarea></div>
      <div class="row"><label>AI disclosure</label><select id="vd_ai"><option value="">Don't say</option><option value="non_generative">Not made with generative AI</option><option value="generative">Made with generative AI</option></select></div>${noteRow}`,
      [["Cancel", () => dlg.close()], ["Add to draft", async () => {
        let file = v("vd_path"), name = "";
        const f = el("vd_file").files[0];
        try {
          if (!file && f){ el("vd_fileOut").textContent = "Copying the file into Studio ..."; const st = await stage(f); file = st.file; name = st.name }
          if (!file) return toast("Pick a file or give its full path");
          const c = {type: "video_upload", file, title: v("vd_title") || (f ? f.name.replace(/\.[^.]+$/, "") : ""), description: v("vd_desc")};
          if (name) c.file_name = name;
          if (v("vd_ai")) c.ai_disclosure = v("vd_ai");
          await submit([c]);
        } catch(e) { toast(e.message); el("vd_fileOut").textContent = "" }
      }, "primary"]]);
  }

  async function stage(f){
    const r = await fetch("/api/videos/stage?" + qs({name: f.name}), {method: "POST", headers: {"Content-Type": "application/octet-stream", "X-Studio-Press": PRESS}, body: f});
    const j = await r.json(); if (!r.ok) throw new Error(j.error || r.statusText); return j;
  }

  async function uploadNow(id){
    const it = uploads().find(i => i.id === id); if (!it) return;
    const TR = window.TeamReviews, gate = TR ? await TR.gate("ads", "ads:" + V.lib.account, id) : null;
    showDialog("Upload to YouTube", `<p>Upload <b>${h(it.change.title)}</b> (${h(it.change.file_name || String(it.change.file).split("/").pop())}) to YouTube now?</p>
      <p class="hint">Unlisted, on the account's Google-managed channel. Google then processes it; check its status under Uploads from Studio.
      Removing it later deletes it from YouTube too (in Google Ads).</p>${TR ? TR.section(gate) : ""}`,
      [["Cancel", () => dlg.close()], ["Upload", async () => {
        try { const r = await withProgress("Uploading to YouTube", "/api/videos/upload", {id, ...(TR ? TR.extra() : {})});
          const list = await lastProgress();
          showDialog(r.ok ? "Uploaded" : "Not uploaded", `${list}<p>${h(r.message || "")}</p>`, [["Done", () => dlg.close(), "primary"]]);
          await load(false);
        } catch(e) { toast(e.message) }
        poll(true); render();
      }, "live"]]);
    if (TR && gate){ const b = document.querySelector("#dlgF .live"); const upd = () => { b.disabled = !TR.ready(gate) || (gate.required && !gate.count) };
      TR.wire(document.getElementById("dlgB"), null, upd); upd() }
  }

  const add = () => !V.lib ? toast("Still loading") : V.lib.campaigns.some(c => !c.trial) ? adDialog({}) : campaignDialog();
  window.Videos = {render, add, reload: async () => { await load(true); render() }};
})();
