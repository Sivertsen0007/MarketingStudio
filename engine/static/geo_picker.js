// Country picker for Meta ad sets (Edit panel, ad set dialog, New ad set). One list of countries from
// GET /api/meta/countries (engine/geo_countries.py), so the UI and the engine agree on codes, names and restrictions.
// The picker only changes the ad set's country list. Regions, cities and exclusions are shown read-only; the engine
// keeps them as they are. The same rules as the engine (geo_countries.geo_problems) run here for instant feedback.
(function(){
  const E = s => String(s ?? "").replace(/[&<>"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
  let DATA = null, loading = null;
  const byCode = {};
  function load(){
    if (DATA) return Promise.resolve(DATA);
    if (!loading) loading = fetch("/api/meta/countries").then(r => r.json()).then(j => {
      DATA = j; (j.countries || []).forEach(c => { byCode[c.code] = c }); refreshSummaries(); return j;
    }).catch(e => { loading = null; throw e });
    return loading;
  }
  const name = c => (byCode[c] || {}).name || c;
  const parse = v => [...new Set(String(v || "").toUpperCase().split(/[^A-Z]+/).filter(Boolean))];
  const placesIn = (geo, code) => ((geo && geo.places) || []).filter(p => p.country === code);
  const phrase = p => p.name === "a map pin" ? p.name : `the ${p.kind} ${p.name}`;
  // the ad set's country groups (eea, worldwide, ...) that hold this country; unknown groups can't be checked
  const groupsIn = (geo, code) => ((geo && geo.country_groups) || []).filter(g => {
    const m = ((DATA && DATA.country_groups) || {})[String(g).toLowerCase()];
    return m === null || (Array.isArray(m) && m.includes(code));
  });

  // the engine's rules, mirrored: -> [problem text]
  function problems(codes, geo){
    const out = [], bad = codes.filter(c => DATA && !byCode[c]);
    if (bad.length) out.push(`Not a country code: ${bad.join(", ")}`);
    codes.forEach(c => { const x = byCode[c]; if (x && x.restricted) out.push(`${x.name} (${c}) can't be targeted: ${x.restricted}`) });
    codes.forEach(c => placesIn(geo, c).forEach(p => out.push(`${name(c)} (${c}) overlaps ${phrase(p)} the ad set already targets: Meta refuses overlapping locations`)));
    codes.forEach(c => groupsIn(geo, c).forEach(g => out.push(`${name(c)} (${c}) is already in the country group ${g} this ad set targets: Meta refuses overlapping locations`)));
    const ex = codes.filter(c => ((geo && geo.excluded_countries) || []).includes(c));
    if (ex.length) out.push(`${ex.map(c => `${name(c)} (${c})`).join(", ")} ${ex.length === 1 ? "is" : "are"} excluded in this ad set: a country can't be both targeted and excluded`);
    const others = ((geo && geo.places) || []).length + ((geo && geo.country_groups) || []).length;
    if (!codes.length && !others) out.push(geo ? "The ad set needs at least one location: pick a country" : "Pick at least one country");
    return out;
  }
  // why a country can't be ticked here (null = it can)
  function blocked(code, geo){
    const x = byCode[code] || {};
    if (x.restricted) return x.restricted;
    const p = placesIn(geo, code);
    if (p.length) return `The ad set already targets ${p.map(q => q.name).join(", ")} here`;
    const g = groupsIn(geo, code);
    if (g.length) return `Already in the ad set's country group ${g.join(", ")}`;
    if (((geo && geo.excluded_countries) || []).includes(code)) return "Excluded in this ad set";
    return null;
  }
  function summaryText(codes){
    if (!codes.length) return "No countries";
    const n = codes.map(name);
    return n.length <= 2 ? n.join(", ") : `${n.slice(0, 2).join(", ")} +${n.length - 2}`;
  }
  function placesText(geo){
    const p = (geo && geo.places) || [], g = (geo && geo.country_groups) || [];
    if (!p.length && !g.length) return "";
    const bits = [...g.map(x => `${x} (country group)`), ...p.slice(0, 4).map(x => `${x.name} (${x.kind}${x.country ? ", " + x.country : ""})`)];
    return bits.join(", ") + (p.length > 4 ? ` and ${p.length - 4} more` : "");
  }

  // ---- the field: a summary, a "Choose countries" button and a hidden input holding the codes ----
  const FIELDS = {};
  function field(id, codes, opt){
    opt = opt || {};
    FIELDS[id] = {geo:opt.geo || null, title:opt.title || "Choose countries"};
    const geo = opt.geo, pt = placesText(geo);
    return `<div class="gp-field">
      <div class="gp-sum"><span id="${id}_sum" class="gp-sumtxt" title="${E(codes.join(", "))}">${E(summaryText(codes))}</span>
        <button type="button" id="${id}_btn" data-gp="${id}">Choose countries</button></div>
      <input type="hidden" id="${id}" value="${E(codes.join(","))}">
      ${pt ? `<div class="hint gp-also">Also targets ${E(pt)} (regions and cities are kept; change them in Meta Ads Manager)</div>` : ""}
      ${geo && geo.excluded_countries && geo.excluded_countries.length ? `<div class="hint gp-also">Excluded: ${E(geo.excluded_countries.map(c => name(c) + " (" + c + ")").join(", "))}</div>` : ""}
      <div id="${id}_prob" class="prob gp-prob" style="display:none"></div></div>`;
  }
  function setField(id, codes){
    const i = document.getElementById(id); if (!i) return;
    i.value = codes.join(",");
    const s = document.getElementById(id + "_sum"); if (s){ s.textContent = summaryText(codes); s.title = codes.join(", ") }
    const n = document.getElementById(id + "_n"); if (n) n.textContent = `${codes.length} selected`;
    const geo = (FIELDS[id] || {}).geo, pr = document.getElementById(id + "_prob");
    const list = !codes.length && !geo ? [] : problems(codes, geo);  // a new ad set starts empty: say so on Add to draft, not before
    if (pr){ pr.style.display = list.length ? "" : "none"; pr.textContent = list.join(". ") }
  }
  function refreshSummaries(){ Object.keys(FIELDS).forEach(id => { const i = document.getElementById(id); if (i) setField(id, parse(i.value)) }) }
  function wire(id){
    const b = document.getElementById(id + "_btn"); if (!b) return;
    setField(id, parse(document.getElementById(id).value));
    b.onclick = async e => {
      e.preventDefault();
      const f = FIELDS[id] || {}, cur = parse(document.getElementById(id).value);
      const got = await open({codes:cur, geo:f.geo, title:f.title});
      b.focus();
      if (!got || got.join(",") === cur.join(",")) return;
      setField(id, got);
      document.getElementById(id).dispatchEvent(new Event("change", {bubbles:true})); // the Edit panel autosaves on change
    };
  }
  const read = id => parse((document.getElementById(id) || {}).value);

  // ---- the pop-up ----
  function dlgEl(){
    let d = document.getElementById("geoDlg");
    if (d) return d;
    d = document.createElement("dialog"); d.id = "geoDlg"; d.className = "gp"; d.setAttribute("aria-labelledby", "gpT");
    document.body.appendChild(d);
    return d;
  }
  async function open(opt){
    try { await load() } catch(e) { if (window.toast) toast("Couldn't load the country list"); return null }
    const d = dlgEl(), geo = opt.geo || null;
    let sel = [...opt.codes], q = "";
    d.innerHTML = `<div class="dlg-h"><span id="gpT">${E(opt.title || "Choose countries")}</span><span id="gpN" class="chip" aria-live="polite"></span></div>
      <div class="dlg-b">
        <div id="gpSel" class="gp-sel" aria-label="Selected countries"></div>
        <input id="gpQ" type="text" placeholder="Search countries or codes, Enter ticks the first match" aria-label="Search countries" aria-controls="gpList" autocomplete="off">
        <div class="gp-groups" role="group" aria-label="Quick groups">${DATA.groups.map((g, i) => `<button type="button" class="ghost" data-gpg="${i}">+ ${E(g.name)}</button>`).join("")}</div>
        <div id="gpList" class="gp-list" role="group" aria-label="Countries"></div>
        ${placesText(geo) ? `<div class="hint gp-also"><b>Also in this ad set's locations:</b> ${E(placesText(geo))}. Regions and cities are not changed here; edit them in Meta Ads Manager.</div>` : ""}
        ${geo && geo.excluded_countries && geo.excluded_countries.length ? `<div class="hint gp-also"><b>Excluded:</b> ${E(geo.excluded_countries.map(c => name(c) + " (" + c + ")").join(", "))}</div>` : ""}
        <div id="gpProb" class="prob" style="display:none"></div>
      </div>
      <div class="dlg-f"><button type="button" class="link" id="gpClear">Clear</button><button type="button" id="gpCancel">Cancel</button><button type="button" class="primary" id="gpDone">Done</button></div>`;
    const $q = d.querySelector("#gpQ"), $list = d.querySelector("#gpList");
    // best first: the code itself, then names starting with the text, then a word starting with it, then anywhere
    const fold = t => t.normalize("NFKD").replace(/[\u0300-\u036f]/g, "").toLowerCase();
    const matches = () => {
      const s = fold(q.trim()); if (!s) return DATA.countries;
      const w = s.replace(/[^a-z0-9]/g, ""), word = w ? new RegExp("\\b" + w) : null;
      const rank = c => { const n = fold(c.name); return c.code.toLowerCase() === s ? 0 : n.startsWith(s) ? 1 : word && word.test(n) ? 2 : n.includes(s) ? 3 : 9 };
      return DATA.countries.map(c => [rank(c), c]).filter(x => x[0] < 9).sort((a, b) => a[0] - b[0]).map(x => x[1]);
    };
    function paintSel(){
      d.querySelector("#gpSel").innerHTML = sel.length ? sel.map(c => `<span class="gp-chip">${E(name(c))} <code>${E(c)}</code><button type="button" class="gp-x" data-gpx="${E(c)}" aria-label="Remove ${E(name(c))}">&times;</button></span>`).join("")
        : `<span class="hint" style="margin:0">No countries selected</span>`;
      d.querySelector("#gpN").textContent = `${sel.length} selected`;
      const pr = problems(sel, geo), box = d.querySelector("#gpProb");
      box.style.display = pr.length ? "" : "none"; box.textContent = pr.join(". ");
      d.querySelector("#gpDone").disabled = pr.length > 0;
      d.querySelector("#gpDone").textContent = `Done (${sel.length})`;
    }
    function paintList(){
      const m = matches();
      $list.innerHTML = m.length ? m.map(c => { const on = sel.includes(c.code), why = blocked(c.code, geo), dis = why && !on;
        return `<label class="gp-row ${on ? "on" : ""} ${dis ? "off" : ""}" ${why ? `title="${E(why)}"` : ""}><input type="checkbox" data-gpc="${c.code}" ${on ? "checked" : ""} ${dis ? "disabled" : ""}>
          <span class="gp-nm">${E(c.name)}${why ? `<span class="gp-why">${E(why)}</span>` : ""}</span><code>${c.code}</code></label>` }).join("")
        : `<div class="hint" style="padding:10px 12px;margin:0">No country matches "${E(q)}"</div>`;
    }
    const toggle = (c, on) => { sel = on ? (sel.includes(c) ? sel : [...sel, c]) : sel.filter(x => x !== c); paintSel() };
    paintSel(); paintList();
    $q.oninput = () => { q = $q.value; paintList() };
    $q.onkeydown = e => {
      if (e.key !== "Enter") return;
      e.preventDefault();
      const first = matches().find(c => sel.includes(c.code) || !blocked(c.code, geo));
      if (!first) return;
      toggle(first.code, !sel.includes(first.code)); paintList();
    };
    $list.onchange = e => { const b = e.target.closest("[data-gpc]"); if (b){ toggle(b.dataset.gpc, b.checked); b.closest(".gp-row").classList.toggle("on", b.checked) } };
    d.querySelector("#gpSel").onclick = e => { const b = e.target.closest("[data-gpx]"); if (!b) return; toggle(b.dataset.gpx, false); paintList(); $q.focus() };
    d.querySelector(".gp-groups").onclick = e => {
      const b = e.target.closest("[data-gpg]"); if (!b) return;
      const g = DATA.groups[+b.dataset.gpg], add = g.codes.filter(c => !sel.includes(c) && !blocked(c, geo));
      sel = [...sel, ...add]; paintSel(); paintList();
      if (window.toast) toast(add.length ? `Added ${add.length} from ${g.name}` : `Everything in ${g.name} is already selected`);
    };
    return new Promise(res => {
      const done = v => { d.onkeydown = null; d.oncancel = null; d.close(); res(v) };
      d.querySelector("#gpClear").onclick = () => { sel = []; paintSel(); paintList(); $q.focus() };
      d.querySelector("#gpCancel").onclick = () => done(null);
      d.querySelector("#gpDone").onclick = () => { if (!problems(sel, geo).length) done(sel) };
      d.oncancel = e => { e.preventDefault(); done(null) };  // Escape
      d.onkeydown = e => {  // Escape cancels; Tab stays inside the picker
        if (e.key === "Escape"){ e.preventDefault(); e.stopPropagation(); return done(null) }
        if (e.key !== "Tab") return;
        const f = [...d.querySelectorAll("button:not(:disabled),input:not(:disabled)")].filter(x => x.offsetParent !== null);
        if (!f.length) return;
        const i = f.indexOf(document.activeElement);
        if (e.shiftKey && i <= 0){ e.preventDefault(); f[f.length - 1].focus() }
        else if (!e.shiftKey && i === f.length - 1){ e.preventDefault(); f[0].focus() }
      };
      d.showModal(); $q.focus();
    });
  }
  window.GeoPicker = {load, field, wire, read, open, problems, summaryText, name};
  load().catch(() => {});
})();
