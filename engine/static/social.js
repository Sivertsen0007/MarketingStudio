// SoMe area of Marketing Studio: calendar, post editor with checks and phone previews, published posts with insights.
// Post now / Schedule / Cancel / Delete run only from these buttons (they send pressed:"user"); Claude has no tool for them.
(() => {
const A = "/api/area/social/";
const PROG = "/api/progress?key=social";
const CH = {facebook: "Facebook", instagram: "Instagram", linkedin: "LinkedIn"};
const CHS = Object.keys(CH);
const SO = {view: localGet("so.view") || "calendar", mode: localGet("so.mode") || "month", anchor: new Date(), post: null, state: null,
            cal: null, pub: null, cols: localGet("so.cols") || "data", tab: "all", pubDays: "30", pubCh: "", fch: "", fst: "",
            anaDays: "30", heatCh: "instagram", lib: null, tags: null, csv: null};

const CSS = `
.so-root{display:flex;flex-direction:column;height:100%;min-height:0;background:var(--bg)}
.so-root{--so-fb:var(--ch-fb);--so-ig:var(--ch-ig);--so-li:var(--ch-li)}
.so-root button.danger{color:var(--bad-text)}
.so-head{display:flex;align-items:flex-end;gap:12px;padding:22px 28px 14px;flex-wrap:wrap}
.so-head .ttl{flex:1;min-width:240px}
.so-head h1{margin:0;font-size:24px;font-weight:600;letter-spacing:-.3px;line-height:1.2;color:var(--text)}
.so-head p{margin:4px 0 0;font-size:13.5px;font-weight:500;color:var(--muted)}
.so-head .acts{display:flex;gap:8px;align-items:center;flex-wrap:wrap}
.so-head .chip.ok{display:inline-flex;align-items:center;gap:6px;height:28px;padding:0 10px;border-radius:var(--r-pill);background:var(--ok-bg);color:var(--ok-text);font-size:12px;font-weight:600}
.so-head .chip.ok::before{content:"";width:6px;height:6px;border-radius:50%;background:var(--ok)}
.so-root .toolbar.so-bar{background:transparent;border:0;box-shadow:none;padding:0 28px 14px;gap:10px}
.so-body{display:flex;flex:1;min-height:0;gap:12px;padding:0 28px 20px}
.so-main{flex:1;min-width:0;overflow:auto;display:flex;flex-direction:column;background:var(--panel);border-radius:var(--r-lg);box-shadow:var(--sh-card)}
.so-side{width:440px;max-width:48vw;overflow:auto;background:var(--panel);border-radius:var(--r-lg);box-shadow:var(--sh-card);display:none}
.so-side.on{display:block}
.so-seg{display:inline-flex;gap:2px;padding:3px;border-radius:var(--r-md);background:var(--sunken)}
.so-seg button{height:32px;padding:0 13px;border:0;border-radius:var(--r-sm);background:transparent;box-shadow:none;color:var(--muted);font-size:13px;font-weight:600}
.so-seg button:hover{background:transparent;color:var(--text)}
.so-seg button.on{background:var(--panel);color:var(--text);box-shadow:var(--sh-1)}
.so-nav{width:34px;height:34px;padding:0;font-size:16px;line-height:1}
.so-title{font-weight:600;font-size:15px;color:var(--text);min-width:150px;text-align:center}
.so-test{display:inline-flex;align-items:center;height:28px;background:var(--warn-bg);color:var(--warn);border-radius:var(--r-pill);padding:0 10px;font-size:12px;font-weight:600}
.so-month{display:grid;grid-template-columns:repeat(7,minmax(0,1fr));grid-template-rows:38px;grid-auto-rows:minmax(118px,auto);align-content:start;flex:1 0 auto}
.so-dow{display:flex;align-items:center;padding:0 12px;font-size:11.5px;font-weight:600;color:var(--muted);background:var(--head);border-bottom:1px solid var(--line2)}
.so-dow.today{color:var(--accent)}
.so-day{border-right:1px solid var(--line2);border-bottom:1px solid var(--line2);padding:6px 6px 4px;min-height:104px;overflow:hidden;transition:background var(--t) var(--ease)}
.so-day:nth-child(7n){border-right:0}
.so-day:hover,.so-slot:hover{background:var(--hover)}
.so-day.out{background:var(--head)}
.so-day.out .so-dn{color:var(--muted)}
.so-day.today .so-dn{background:var(--accent);color:var(--ink-text);border-radius:var(--r-pill);padding:0 7px;min-width:22px}
.so-day.over,.so-slot.over{outline:2px dashed var(--accent);outline-offset:-3px;background:var(--accent-bg)}
.so-dn{font-size:12px;font-weight:600;color:var(--text2);display:inline-flex;align-items:center;justify-content:center;height:22px;margin:0 0 4px 2px;font-variant-numeric:tabular-nums}
.so-ev{--c:var(--so-fb);display:block;width:100%;height:32px;text-align:left;font-size:11.5px;font-weight:600;line-height:32px;padding:0 7px 0 18px;margin:0 0 3px;border:0;border-radius:var(--r-sm);position:relative;
  background:color-mix(in srgb,var(--c) 9%,var(--panel));box-shadow:none;color:var(--text);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;transition:background var(--t) var(--ease)}
.so-ev::before{content:"";position:absolute;left:7px;top:calc(50% - 3px);width:6px;height:6px;border-radius:50%;background:var(--c)}
.so-ev:hover{background:color-mix(in srgb,var(--c) 18%,var(--panel))}
.so-ev.facebook{--c:var(--so-fb)}.so-ev.instagram{--c:var(--so-ig)}.so-ev.linkedin{--c:var(--so-li)}
.so-ev.moved{background:var(--draft-bg);box-shadow:inset 0 0 0 1px var(--draft)}
.so-ev[draggable=true]{cursor:grab}
.so-ev .t{color:var(--muted);font-weight:500;margin-right:5px;font-variant-numeric:tabular-nums}
.so-ev .so-st{height:16px;font-size:10px;vertical-align:1px}
:is(.so-day,.so-slot) .so-ev .so-st{position:absolute;right:7px;top:calc(50% - 3.5px);width:7px;height:7px;padding:0;margin:0;border-radius:50%;font-size:0;background:var(--faint)}
:is(.so-day,.so-slot) .so-ev{padding-right:20px}
:is(.so-day,.so-slot) .so-ev .so-st.scheduled{background:var(--accent)}:is(.so-day,.so-slot) .so-ev .so-st.published{background:var(--ok)}:is(.so-day,.so-slot) .so-ev .so-st.failed{background:var(--bad)}:is(.so-day,.so-slot) .so-ev .so-st.draft{background:var(--draft)}
:is(.so-day,.so-slot) .so-ev .so-st.needs_checking,:is(.so-day,.so-slot) .so-ev .so-st.missed,:is(.so-day,.so-slot) .so-ev .so-st.not_scheduled,:is(.so-day,.so-slot) .so-ev .so-st.publishing{background:var(--c4)}
.so-st{display:inline-flex;align-items:center;height:18px;font-size:10.5px;font-weight:600;padding:0 6px;border-radius:var(--r-xs);margin-left:4px;background:var(--sunken);color:var(--text2);white-space:nowrap}
.so-st.scheduled{background:var(--accent-bg);color:var(--accent)}.so-st.published{background:var(--ok-bg);color:var(--ok-text)}.so-st.failed{background:var(--bad-bg);color:var(--bad-text)}.so-st.draft{background:var(--draft-bg);color:var(--draft-text)}
.so-st.needs_checking,.so-st.missed,.so-st.not_scheduled,.so-st.publishing{background:var(--warn-bg);color:var(--warn)}
.so-week{display:grid;grid-template-columns:56px repeat(7,minmax(0,1fr));flex:1 0 auto}
.so-week>.so-dow{position:sticky;top:0;z-index:1;height:38px}
.so-hr{font-size:10.5px;font-weight:500;color:var(--faint);text-align:right;padding:4px 8px;border-right:1px solid var(--line2);font-variant-numeric:tabular-nums}
.so-slot{border-right:1px solid var(--line2);border-bottom:1px dashed var(--line2);min-height:38px;padding:3px;transition:background var(--t) var(--ease)}
.so-legend{display:flex;gap:14px;align-items:center;font-size:12px;font-weight:500;color:var(--muted);padding:12px 18px;border-bottom:1px solid var(--line2);flex-wrap:wrap}
.so-legend>span{display:inline-flex;align-items:center;gap:4px}
.so-dot{display:inline-block;width:8px;height:8px;border-radius:50%;margin-right:4px}
.so-pending{background:var(--draft-bg);color:var(--text);padding:10px 18px;display:flex;gap:10px;align-items:center;flex-wrap:wrap;font-size:13px}
.so-pending b{color:var(--draft-text);font-weight:600}
.so-pending button{height:30px;padding:0 12px;font-size:12.5px}
.so-unpl{display:flex;gap:6px;align-items:center;flex-wrap:wrap;padding:12px 18px;border-top:1px solid var(--line2);font-size:13px}
.so-unpl b{font-weight:600;color:var(--text2)}
.so-unpl .so-ev{display:inline-block;width:auto;max-width:260px;margin:0}
.so-more{display:block;width:100%;height:32px;padding:0 7px;margin:0;border:0;border-radius:var(--r-sm);background:transparent;box-shadow:none;text-align:left;font-size:11.5px;font-weight:600;color:var(--accent)}
.so-more:hover{background:var(--accent-bg)}
.so-main>.empty button{margin-top:10px}
.so-ed{padding:18px 20px 90px}
.so-edh{display:flex;justify-content:space-between;align-items:center;margin-bottom:12px}
.so-ed h3{margin:0;font-weight:600;font-size:18px;color:var(--text);display:flex;align-items:center;gap:8px}
.so-row{margin-bottom:14px}.so-row>label{display:block;font-size:12px;font-weight:600;color:var(--muted);margin-bottom:6px}
.so-row>label.inl{display:flex;gap:8px;align-items:center;color:var(--text);font-size:13px;font-weight:500}
.so-row input[type=text],.so-row input[type=datetime-local],.so-row select{width:100%}
.so-ticks{display:flex;gap:14px;flex-wrap:wrap}.so-ticks label{display:flex;gap:6px;align-items:center;font-weight:500}
.so-cnt{font-size:11.5px;font-weight:500;color:var(--muted);text-align:right;margin-top:4px}.so-cnt.over{color:var(--bad-text)}
.so-tabs{display:inline-flex;gap:2px;margin-bottom:6px;padding:3px;border-radius:var(--r-md);background:var(--sunken)}
.so-tabs button{height:32px;padding:0 12px;font-size:12px;border-radius:var(--r-sm);background:transparent;box-shadow:none;color:var(--muted)}
.so-tabs button:hover{background:transparent;color:var(--text)}
.so-tabs button.on{background:var(--panel);color:var(--text);box-shadow:var(--sh-1)}
.so-media{display:flex;gap:8px;flex-wrap:wrap}.so-mi{position:relative;width:80px;height:80px;border-radius:var(--r-md);overflow:hidden;background:var(--sunken);font-size:11px;color:var(--muted);display:flex;align-items:center;justify-content:center;text-align:center;box-shadow:var(--sh-1)}
.so-mi img{width:100%;height:100%;object-fit:cover}.so-mi .x{position:absolute;top:3px;right:3px;padding:0 6px;font-size:11px;line-height:18px;height:18px}
.so-mi .l{position:absolute;bottom:3px;left:3px;padding:0 6px;font-size:11px;line-height:18px;height:18px}
.so-prob,.so-warn,.so-okk{font-size:12.5px;font-weight:500;border-radius:var(--r-sm);padding:7px 10px;margin:4px 0;color:var(--text2)}
.so-prob{background:var(--bad-bg)} .so-prob b{color:var(--bad-text)}
.so-warn{background:var(--warn-bg)} .so-warn b{color:var(--warn)}
.so-okk{background:var(--ok-bg)} .so-okk b{color:var(--ok-text)}
.so-main>.so-prob,.so-main>.so-warn{margin:12px 18px}
.so-consent{background:var(--warn-bg);color:var(--text2);border-radius:var(--r);padding:8px 10px;font-size:12.5px;margin-top:6px}
.so-acts{position:sticky;bottom:0;background:var(--panel);border-top:1px solid var(--line2);padding:12px 20px;display:flex;gap:8px;flex-wrap:wrap;box-shadow:var(--sh-up)}
.so-acts .hint{margin:0;font-size:12px}
.so-phones{display:flex;gap:14px;overflow-x:auto;padding:4px 2px 10px}
.so-phone{flex:none;width:236px;border:7px solid var(--ink);border-radius:26px;background:var(--panel);color:var(--text);font-size:12px;overflow:hidden;box-shadow:var(--sh-pop)}
.so-phone .bar{height:14px;background:var(--ink)}
.so-phone .hd{display:flex;align-items:center;gap:7px;padding:8px 10px;font-weight:600}.so-phone .av{width:24px;height:24px;border-radius:50%;background:linear-gradient(135deg,var(--accent),var(--violet))}
.so-phone .md{background:var(--sunken);aspect-ratio:1/1;display:flex;align-items:center;justify-content:center;color:var(--muted);overflow:hidden;position:relative}
.so-phone .md img{width:100%;height:100%;object-fit:cover}.so-phone.instagram .md{aspect-ratio:4/5}
.so-phone .md .n{position:absolute;top:6px;right:6px;background:var(--overlay);color:var(--ink-text);border-radius:var(--r-pill);padding:0 7px;font-size:10px}
.so-phone .cp{padding:8px 10px;white-space:pre-wrap;word-break:break-word;max-height:150px;overflow:hidden;color:var(--text2)}
.so-phone .cp.sub{color:var(--muted)}
.so-phone .lk{margin:0 10px 10px;border:1px solid var(--line);border-radius:var(--r-sm);padding:6px 8px;color:var(--muted);font-size:11px;word-break:break-all}
.so-phone .ch{padding:4px 10px;background:var(--head);color:var(--muted);font-size:10.5px;font-weight:600}
.so-res a{color:var(--accent);font-weight:600}
.so-grid{width:100%}
.so-grid th{height:40px;background:var(--head);color:var(--muted);font-size:11.5px;font-weight:600;border-right:0;border-bottom:1px solid var(--line2);cursor:default}
.so-grid td{height:46px;border-right:0;color:var(--text2)}
.so-grid th:first-child,.so-grid td:first-child{padding-left:18px}
.so-grid tbody tr:first-child td{background:var(--head)}
.so-grid td.txt{max-width:340px;color:var(--text)}
.so-grid td button{height:28px;padding:0 10px;font-size:12px}
.so-grid img{width:34px;height:34px;object-fit:cover;border-radius:var(--r-sm);vertical-align:middle}
.so-chan{font-weight:600}.so-chan.facebook{color:var(--so-fb)}.so-chan.instagram{color:var(--ch-ig-text)}.so-chan.linkedin{color:var(--ch-li-text)}
.so-quick{display:flex;gap:4px;flex-wrap:wrap;margin-top:6px}.so-quick button{height:26px;font-size:12px;padding:0 9px;border-radius:var(--r-pill)}
.so-libbar{display:flex;gap:6px;flex-wrap:wrap}.so-libbar select{flex:1;min-width:120px}
.so-tagsug{margin-top:6px;display:flex;gap:4px;flex-wrap:wrap;align-items:center}
.so-tagsug .chip{border:0;cursor:pointer;font-weight:600;font-size:11.5px;height:24px;padding:0 9px;border-radius:var(--r-pill);background:var(--accent-bg);color:var(--accent)}
.so-day,.so-slot{cursor:cell}
.so-dayv{grid-template-columns:56px minmax(0,1fr)}
.so-list{padding:8px 18px 14px}.so-lday{margin-bottom:12px}
.so-ldh{font-weight:600;font-size:13.5px;color:var(--text);padding:10px 0 8px;border-bottom:1px solid var(--line2);margin-bottom:6px;display:flex;gap:10px;align-items:center}
.so-lrow{display:flex;gap:10px;align-items:center;padding:3px 0}.so-lrow .so-ev{width:auto;max-width:640px;margin:0}
.so-feed{display:grid;grid-template-columns:repeat(3,minmax(0,170px));gap:4px;padding:16px 18px;justify-content:start}
.so-ft{aspect-ratio:3/4;background:linear-gradient(160deg,var(--accent-bg),var(--violet-bg));position:relative;overflow:hidden;cursor:pointer;display:flex;align-items:center;justify-content:center;font-size:12px;font-weight:600;color:var(--muted);border-radius:var(--r-xs);transition:filter var(--t) var(--ease)}
.so-ft:hover{filter:brightness(.96)}
.so-ft img{width:100%;height:100%;object-fit:cover}.so-ft.plan{outline:2px dashed var(--accent);outline-offset:-2px}
.so-fl{position:absolute;left:4px;bottom:4px;right:4px;background:var(--overlay);color:var(--ink-text);font-size:10.5px;padding:3px 6px;border-radius:var(--r-sm);display:flex;align-items:center;gap:4px;flex-wrap:wrap}
.so-ana{padding:18px 20px}
.so-main:has(> .so-ana:not(.so-lib)){background:transparent;box-shadow:none;border-radius:0}
.so-main>.so-ana:not(.so-lib){padding:0 0 8px}
.so-ana .tile .hint{font-size:12px;color:var(--muted)}
.so-anabar{display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-bottom:14px}
.so-anabar .hint,.so-ana .hint{margin:0 0 8px;font-size:12px;font-weight:500}
.so-anabar .hint{margin:0}
.so-ana .tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:12px;margin-bottom:8px}
.so-ana .tile{border:0;border-radius:var(--r-lg);padding:16px 18px;background:var(--panel);box-shadow:var(--sh-card)}
.so-ana .tile .k{font-size:13px;font-weight:500;color:var(--text2)} .so-ana .tile .v{font-size:28px;font-weight:600;letter-spacing:-.5px;margin:6px 0 4px;color:var(--text)}
.so-h{font-weight:600;font-size:16px;color:var(--text);margin:22px 0 8px;display:flex;gap:10px;align-items:center}
.so-h select{font-size:13px;font-weight:600}
.so-tw{overflow:auto;border-radius:var(--r-lg);box-shadow:var(--sh-card);background:var(--panel)}
.so-tw table{width:100%}
.so-tw th{height:38px;background:var(--head);color:var(--muted);font-size:11.5px;font-weight:600;border-right:0;border-bottom:1px solid var(--line2);cursor:default}
.so-tw td{height:40px;border-right:0;color:var(--text2)}
.so-tw th:first-child,.so-tw td:first-child{padding-left:16px}
.so-tw tbody tr:last-child td{border-bottom:0}
.so-heat{display:grid;grid-template-columns:40px repeat(24,minmax(14px,1fr));gap:3px;max-width:920px;padding:14px 16px;border-radius:var(--r-lg);background:var(--panel);box-shadow:var(--sh-card)}
.so-heat .hh,.so-heat .hd{font-size:10.5px;font-weight:500;color:var(--faint);align-self:center}
.so-heat .hc{height:20px;border-radius:var(--r-xs);background:color-mix(in srgb,var(--accent) calc(var(--a) * 100%),var(--sunken));transition:transform var(--t) var(--ease)}
.so-heat .hc:hover{transform:scale(1.15)}
.so-heat .hc.top{box-shadow:0 0 0 2px var(--panel),0 0 0 3.5px var(--ink)}
.so-lib .so-lrow{padding:8px 0;border-bottom:1px solid var(--line2)}
.so-lib textarea{margin:8px 0 0}
.so-lib details summary{cursor:pointer;color:var(--accent);font-weight:600}
.so-alposts{max-height:200px;overflow:auto;border:1px solid var(--line);border-radius:var(--r);padding:6px 10px;margin:8px 0}
`;

const pad = n => String(n).padStart(2, "0");
const ymd = d => `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
const localISO = d => `${ymd(d)}T${pad(d.getHours())}:${pad(d.getMinutes())}`;
const MONTHS = ["January","February","March","April","May","June","July","August","September","October","November","December"];
const DOW = ["Mon","Tue","Wed","Thu","Fri","Sat","Sun"];
const fmtDate = s => s ? window.studioFmt.dateTime(s) : "";  // 25 Nov 2026, 14:05
const num = v => v == null || v === "" ? "" : Number(v).toLocaleString("en-GB");
const mondayOf = d => { const x = new Date(d); x.setHours(0,0,0,0); x.setDate(x.getDate() - ((x.getDay() + 6) % 7)); return x };
// U22: statuses are shown capitalised ("Scheduled", "Needs checking"), like Email and Ads; the class keeps the raw value
const stWord = s => { const t = String(s ?? "").replace(/_/g, " "); return t.charAt(0).toUpperCase() + t.slice(1) };
const stChip = s => `<span class="so-st ${esc(String(s).replace(/ .*/, ""))}">${esc(stWord(s))}</span>`;
const mediaUrl = id => "/api/meta/media?" + qs({acct: "meta", id});
const sapi = (k, body) => api(A + k, body);
// ---- the posting time zone (SoMe settings, shared with Email; default Europe/London): the editor's time field, quick
// picks, queue slots and the best time are wall-clock times there, whatever zone this Mac is in ----
const MAC_TZ = (Intl.DateTimeFormat().resolvedOptions().timeZone) || "";
const ptz = () => (SO.state && SO.state.posting_tz) || "Europe/London";
function zoneParts(d, tz){
  const o = {}; for (const x of new Intl.DateTimeFormat("en-GB", {timeZone: tz, year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hourCycle: "h23"}).formatToParts(d)) o[x.type] = x.value;
  return o;
}
const zoneISO = (d, tz) => { const o = zoneParts(d, tz); return `${o.year}-${o.month}-${o.day}T${o.hour}:${o.minute}` };  // for datetime-local
function fromZone(wall, tz){  // "YYYY-MM-DDTHH:MM" as a wall-clock time in tz -> Date
  const m = /^(\d{4})-(\d\d)-(\d\d)T(\d\d):(\d\d)/.exec(wall || ""); if (!m) return null;
  const want = Date.UTC(+m[1], m[2] - 1, +m[3], +m[4], +m[5]); let t = want;
  for (let i = 0; i < 3; i++){ const o = zoneParts(new Date(t), tz); const shown = Date.UTC(+o.year, o.month - 1, +o.day, +o.hour, +o.minute); if (shown === want) break; t += want - shown }
  return new Date(t);
}
const zoneTime = (v, tz) => { const d = new Date(v); return isNaN(d) ? "" : d.toLocaleString("en-GB", {timeZone: tz, weekday: "short", day: "numeric", month: "short", hour: "2-digit", minute: "2-digit"}) };
const macNote = v => v && MAC_TZ && MAC_TZ !== ptz() ? `This Mac (${MAC_TZ}): ${zoneTime(v, MAC_TZ)}` : "";
// characters as the server counts them (code points), and Instagram's hashtags (caption + first comment), as _social_checks.py
const nChars = t => [...String(t || "")].length;
const nTags = t => (String(t || "").match(/(^|[^\p{L}\p{N}_])#[\p{L}\p{N}_]+/gu) || []).length;
// After an await the user may have switched to another area: write into a detached stand-in instead of throwing on null.
const byId = id => document.getElementById(id) || document.createElement("div");
const MAXDAY = 3;  // month view: at most this many posts per day, then "+N more" (opens the day view)

function render(el){
  if (!document.getElementById("so-css")){ const st = document.createElement("style"); st.id = "so-css"; st.textContent = CSS; document.head.appendChild(st) }
  el.innerHTML = `<div class="so-root">
    <div class="so-head"><div class="ttl"><h1>SoMe</h1><p>Organic Facebook, Instagram and LinkedIn posts. Post now, Schedule and Cancel are your own buttons.</p></div>
      <div class="acts"><span id="soTest"></span>
      <button id="soRefresh" title="Read posts and insights from Meta again">Refresh SoMe</button>
      <button id="soSet">SoMe settings</button>
      <button class="primary" id="soNew">New post</button></div></div>
    <div class="toolbar so-bar">
      <div class="so-seg" id="soView"><button data-v="calendar">Calendar</button><button data-v="list">List</button><button data-v="feed">Feed</button><button data-v="published">Published</button><button data-v="analytics">Analytics</button><button data-v="library">Library</button></div>
      <span id="soCalCtl" style="display:flex;gap:8px;align-items:center;flex-wrap:wrap">
        <div class="so-seg" id="soMode"><button data-m="month">Month</button><button data-m="week">Week</button><button data-m="day">Day</button></div>
        <button id="soPrev" class="so-nav" title="Previous" aria-label="Previous">‹</button><button id="soToday">Today</button><button id="soNext" class="so-nav" title="Next" aria-label="Next">›</button>
        <span class="so-title" id="soTitle"></span>
        <select id="soFCh" title="Channel"><option value="">All channels</option>${CHS.map(c => `<option value="${c}">${CH[c]}</option>`).join("")}</select>
        <select id="soFSt" title="Status"><option value="">All statuses</option>${["draft","scheduled","published","failed","missed","needs_checking","not_scheduled"].map(s => `<option value="${s}">${s[0].toUpperCase() + s.slice(1).replace("_", " ")}</option>`).join("")}</select></span>
      <span id="soPubCtl" style="display:none;gap:8px;align-items:center">
        <select id="soPubDays"><option value="7">Last 7 days</option><option value="14">Last 14 days</option><option value="30" selected>Last 30 days</option><option value="90">Last 90 days</option></select>
        <select id="soPubCh"><option value="">Facebook + Instagram</option><option value="facebook">Facebook</option><option value="instagram">Instagram</option></select>
        <div class="so-seg" id="soCols"><button data-c="data">Data</button><button data-c="edit">Edit</button></div></span>
    </div>
    <div class="so-body"><div class="so-main" id="soMain"><div class="empty">Loading ...</div></div><div class="so-side" id="soSide"></div></div></div>`;
  const $$ = s => el.querySelector(s);
  $$("#soView").onclick = e => { const v = e.target.dataset.v; if (v){ SO.view = v; localSet("so.view", v); draw() } };
  $$("#soMode").onclick = e => { const m = e.target.dataset.m; if (m){ SO.mode = m; localSet("so.mode", m); draw() } };
  $$("#soCols").onclick = e => { const c = e.target.dataset.c; if (c){ SO.cols = c; localSet("so.cols", c); drawPublished() } };
  $$("#soPrev").onclick = () => shift(-1); $$("#soNext").onclick = () => shift(1);
  $$("#soToday").onclick = () => { SO.anchor = new Date(); draw() };
  $$("#soFCh").onchange = e => { SO.fch = e.target.value; drawCal() };
  $$("#soFSt").onchange = e => { SO.fst = e.target.value; drawCal() };
  $$("#soPubDays").onchange = e => { SO.pubDays = e.target.value; loadPublished() };
  $$("#soPubCh").onchange = e => { SO.pubCh = e.target.value; loadPublished() };
  $$("#soNew").onclick = () => openPost(null);
  $$("#soSet").onclick = settingsDialog;
  $$("#soRefresh").onclick = async () => { const b = $$("#soRefresh"); b.disabled = true; b.textContent = "Reading ...";
    // Studio's data policy: data is kept until this Refresh. dataRefresh (index.html) runs it through /api/data/refresh.
    try { if (typeof dataRefresh === "function") await dataRefresh("social", null, {reload: false});
      else { const r = await sapi("refresh", {}); toast(r.errors && r.errors.length ? r.errors.join("; ") : "Read from Meta") } } catch(e) { toast(e.message) }
    SO.tags = null; SO.quick = null; SO.lib = null;
    b.disabled = false; b.textContent = "Refresh SoMe"; await loadState(); draw() };
  loadState().then(draw);
}

async function loadState(){
  try { SO.state = await sapi("state") } catch(e) { SO.state = {posts: [], meta: {}, meta_error: e.message, settings: {slots: {}, utm: {}}} }
  const t = document.getElementById("soTest");
  if (t) t.innerHTML = (SO.state.test_mode ? `<span class="so-test">Test mode: nothing is posted</span> ` : "")
    + (SO.state.settings && SO.state.settings.local_scheduler ? `<span class="chip ok" title="Studio publishes scheduled Instagram and LinkedIn posts while it is open">Scheduler on</span>` : "");
}

function shift(n){
  const a = new Date(SO.anchor);
  if (SO.view === "list") a.setDate(a.getDate() + 14 * n);
  else if (SO.mode === "month"){ a.setDate(1); a.setMonth(a.getMonth() + n) }
  else a.setDate(a.getDate() + (SO.mode === "week" ? 7 : 1) * n);
  SO.anchor = a; draw();
}

function draw(){
  const root = document.querySelector(".so-root"); if (!root) return;
  root.querySelectorAll("#soView button").forEach(b => b.classList.toggle("on", b.dataset.v === SO.view));
  root.querySelectorAll("#soMode button").forEach(b => b.classList.toggle("on", b.dataset.m === SO.mode));
  root.querySelectorAll("#soCols button").forEach(b => b.classList.toggle("on", b.dataset.c === SO.cols));
  const cal = SO.view === "calendar" || SO.view === "list";
  document.getElementById("soCalCtl").style.display = cal ? "flex" : "none";
  document.getElementById("soMode").style.display = SO.view === "calendar" ? "" : "none";
  document.getElementById("soPubCtl").style.display = SO.view === "published" ? "flex" : "none";
  if (cal) loadCalendar(); else if (SO.view === "published") loadPublished(); else if (SO.view === "feed") loadFeed();
  else if (SO.view === "analytics") loadAnalytics(); else loadLibrary();
}

// ---------- calendar (month / week / day) and list ----------
function range(){
  if (SO.view === "list"){ const s = new Date(SO.anchor); s.setHours(0,0,0,0); const e = new Date(s); e.setDate(e.getDate() + 28); return [s, e] }
  if (SO.mode === "day"){ const s = new Date(SO.anchor); s.setHours(0,0,0,0); const e = new Date(s); e.setDate(e.getDate() + 1); return [s, e] }
  if (SO.mode === "week"){ const s = mondayOf(SO.anchor); const e = new Date(s); e.setDate(e.getDate() + 7); return [s, e] }
  const first = new Date(SO.anchor.getFullYear(), SO.anchor.getMonth(), 1); const s = mondayOf(first); const e = new Date(s); e.setDate(e.getDate() + 42); return [s, e];
}
const shortD = d => `${d.getDate()} ${MONTHS[d.getMonth()].slice(0,3)}`;
async function loadCalendar(){
  const [s, e] = range(), last = new Date(e - 864e5);
  byId("soTitle").textContent = SO.view === "list" ? `${shortD(s)} - ${shortD(last)} ${last.getFullYear()}`
    : SO.mode === "month" ? `${MONTHS[SO.anchor.getMonth()]} ${SO.anchor.getFullYear()}`
    : SO.mode === "day" ? `${DOW[(s.getDay() + 6) % 7]} ${s.getDate()} ${MONTHS[s.getMonth()]} ${s.getFullYear()}`
    : `${shortD(s)} - ${shortD(last)} ${s.getFullYear()}`;
  try { SO.cal = await sapi("calendar?" + qs({start: localISO(s), end: localISO(e)})) } catch(err) { byId("soMain").innerHTML = `<div class="so-prob">${esc(err.message)}</div>`; return }
  drawCal();
}
function drawCal(){ if (!SO.cal) return; if (SO.view === "list") drawList(); else drawCalendar() }
const shown = ev => (!SO.fch || ev.channel === SO.fch) && (!SO.fst || (ev.moved ? "draft" : ev.status) === SO.fst);
function evHTML(ev){
  // C30: a post with any channel already published can't move (the move would shift its other channels' time only)
  const partly = ev.post && ((SO.cal && SO.cal.events) || []).some(z => z.post === ev.post && z.status === "published");
  const t = new Date(ev.when), drag = ev.source === "studio" && ev.status !== "published" && !partly;
  return `<button class="so-ev ${esc(ev.channel)} ${ev.moved ? "moved" : ""}" data-ev="${esc(ev.key)}" ${drag ? `draggable="true"` : ""}
    title="${esc(CH[ev.channel] + " · " + stWord(ev.status) + " · " + fmtDate(ev.when) + (ev.moved ? " (moved, not confirmed)" : "") + "\n" + ev.title)}"><span class="t">${pad(t.getHours())}:${pad(t.getMinutes())}</span>${esc(ev.title || "(no text)")}${stChip(ev.moved ? "moved" : ev.status)}</button>`;
}
function calHead(c){
  const moved = [...new Set(c.events.filter(e => e.moved).map(e => e.post))];
  let h = `<div class="so-legend">${CHS.map(k => `<span><span class="so-dot" style="background:var(--so-${k === "facebook" ? "fb" : k === "instagram" ? "ig" : "li"})"></span>${CH[k]}</span>`).join("")}
    <span>Status: ${["draft","scheduled","published","failed","missed"].map(stChip).join(" ")}</span><span>Click an empty day or hour to plan a post there. Drag a post to move it; the move waits for your OK.</span></div>`;
  if (c.errors && c.errors.length) h += `<div class="so-warn" style="margin:6px 14px">Meta: ${esc(c.errors.join("; "))}</div>`;
  if (moved.length) h += `<div class="so-pending"><b>${moved.length} moved post(s) not confirmed yet.</b>${moved.map(id => { const ev = c.events.find(e => e.post === id && e.moved);
      return `<span>${esc((ev.title || "").slice(0, 40))} → ${esc(fmtDate(ev.when))} <button class="primary" data-cm="${esc(id)}">Confirm</button> <button data-dm="${esc(id)}">Undo move</button></span>` }).join("")}</div>`;
  return h;
}
function drawCalendar(){
  const c = SO.cal, [s] = range(), today = ymd(new Date());
  const byDay = {}; for (const ev of c.events.filter(shown)){ const k = ymd(new Date(ev.when)); (byDay[k] = byDay[k] || []).push(ev) }
  let h = calHead(c);
  const hours = (days, cls) => { let x = ""; for (let hr = 0; hr < 24; hr++){
      x += `<div class="so-hr">${pad(hr)}:00</div>` + days.map(d => { const k = ymd(d); const evs = (byDay[k] || []).filter(e => new Date(e.when).getHours() === hr);
        return `<div class="so-slot" data-day="${k}" data-hour="${hr}" title="Plan a post at ${pad(hr)}:00">${evs.map(evHTML).join("")}</div>` }).join("") } return x };
  if (SO.mode === "month"){
    h += `<div class="so-month">${DOW.map(d => `<div class="so-dow">${d}</div>`).join("")}`;
    for (let i = 0; i < 42; i++){ const d = new Date(s); d.setDate(d.getDate() + i); const k = ymd(d);
      const evs = byDay[k] || [], cut = evs.length > MAXDAY ? MAXDAY - 1 : MAXDAY;  // keep the cell at 3 rows: 2 posts + "+N more"
      h += `<div class="so-day ${d.getMonth() !== SO.anchor.getMonth() ? "out" : ""} ${k === today ? "today" : ""}" data-day="${k}" title="Plan a post on this day"><span class="so-dn">${d.getDate()}</span>${evs.slice(0, cut).map(evHTML).join("")}`
        + (evs.length > cut ? `<button class="so-more" data-more="${k}" title="Show all ${evs.length} posts on this day">+${evs.length - cut} more</button>` : "") + `</div>` }
    h += `</div>`;
  } else if (SO.mode === "week"){
    const days = [...Array(7)].map((_, i) => { const d = new Date(s); d.setDate(d.getDate() + i); return d });
    h += `<div class="so-week"><div class="so-dow"></div>` + days.map(d => `<div class="so-dow ${ymd(d) === today ? "today" : ""}">${DOW[(d.getDay() + 6) % 7]} ${shortD(d)}</div>`).join("") + hours(days) + `</div>`;
  } else {
    h += `<div class="so-week so-dayv"><div class="so-dow"></div><div class="so-dow">${DOW[(s.getDay() + 6) % 7]} ${shortD(s)}</div>` + hours([s]) + `</div>`;
  }
  if (c.unplanned.length) h += `<div class="so-unpl"><b>No time yet:</b> ${c.unplanned.map(u => `<button class="so-ev ${esc((u.channels || [])[0] || "")}" data-open="${esc(u.id)}">${esc(u.title || "(untitled)")}${stChip(u.status)}</button>`).join("")}</div>`;
  const main = byId("soMain"); main.innerHTML = h;
  if (SO.mode !== "month"){ const slot = main.querySelector('[data-hour="7"]'); if (slot) main.scrollTop = slot.offsetTop - 40 }
  wireCal(main);
}
function drawList(){
  const c = SO.cal, evs = c.events.filter(shown);
  const byDay = {}; for (const ev of evs){ const k = ymd(new Date(ev.when)); (byDay[k] = byDay[k] || []).push(ev) }
  let h = calHead(c) + `<div class="so-list">`;
  const keys = Object.keys(byDay).sort();
  if (!keys.length) h += `<div class="empty">Nothing planned or published in these four weeks${SO.fch || SO.fst ? " with these filters" : ""}.<br><button class="primary" data-newpost>New post</button></div>`;
  for (const k of keys){ const d = new Date(k + "T00:00");
    h += `<div class="so-lday"><div class="so-ldh">${DOW[(d.getDay() + 6) % 7]} ${d.getDate()} ${MONTHS[d.getMonth()]} <button class="link" data-newday="${k}">+ Add post</button></div>`
      + byDay[k].map(ev => `<div class="so-lrow">${evHTML(ev)}<span class="so-chan ${esc(ev.channel)}">${esc(CH[ev.channel])}</span></div>`).join("") + `</div>` }
  h += `</div>`;
  const main = byId("soMain"); main.innerHTML = h; wireCal(main);
}
function wireCal(main){
  main.onclick = async e => {
    const cm = e.target.closest("[data-cm]"), dm = e.target.closest("[data-dm]"), op = e.target.closest("[data-open]"), ev = e.target.closest("[data-ev]"), nd = e.target.closest("[data-newday]");
    const more = e.target.closest("[data-more]");
    if (more){ const [y, m, d] = more.dataset.more.split("-").map(Number); SO.anchor = new Date(y, m - 1, d); SO.mode = "day"; localSet("so.mode", "day"); return draw() }
    if (e.target.closest("[data-newpost]")) return openPost(null);
    if (cm) return confirmMove(cm.dataset.cm);
    if (dm){ try { await sapi("discard_move", {id: dm.dataset.dm}); loadCalendar() } catch(err) { toast(err.message) } return }
    if (op) return openPost(op.dataset.open);
    if (nd) return newAt(nd.dataset.newday);
    if (ev){ const x = SO.cal.events.find(z => z.key === ev.dataset.ev); if (!x) return;
      if (x.post) return openPost(x.post);
      if (x.url) window.open(x.url, "_blank", "noopener"); return }
    const cell = e.target.closest("[data-day]");
    if (cell && !e.target.closest("button")) return newAt(cell.dataset.day, cell.dataset.hour);
  };
  main.ondragstart = e => { const ev = e.target.closest("[data-ev]"); if (ev) e.dataTransfer.setData("text/plain", ev.dataset.ev) };
  main.ondragover = e => { const t = e.target.closest("[data-day]"); if (t){ e.preventDefault(); main.querySelectorAll(".over").forEach(x => x.classList.remove("over")); t.classList.add("over") } };
  main.ondragleave = e => { const t = e.target.closest("[data-day]"); if (t) t.classList.remove("over") };
  main.ondrop = async e => {
    const t = e.target.closest("[data-day]"); if (!t) return; e.preventDefault(); t.classList.remove("over");
    const x = SO.cal.events.find(z => z.key === e.dataTransfer.getData("text/plain")); if (!x || !x.post) return;
    if (SO.cal.events.some(z => z.post === x.post && z.status === "published")) return toast("Part of this post is already published, so it can't be moved. Open it to change the other channels.");
    const old = new Date(x.when), [y, m, d] = t.dataset.day.split("-").map(Number);
    const nw = new Date(y, m - 1, d, t.dataset.hour !== undefined ? +t.dataset.hour : old.getHours(), old.getMinutes());
    if (+nw === +old) return;
    try { await sapi("move", {id: x.post, when: localISO(nw)}); toast("Moved. Confirm it in the bar above the calendar."); loadCalendar() } catch(err) { toast(err.message) }
  };
}
async function newAt(day, hour){
  // fallbacks are wall-clock times in the posting zone, like the engine's slot answer, never this Mac's own zone
  const tz = ptz(), at = (fromZone(`${day}T${String(hour ?? 9).padStart(2, "0")}:00`, tz) || new Date(`${day}T09:00`));
  let when = at.toISOString();
  try { const r = await sapi("slot?" + qs({day, hour: hour ?? "", channels: "facebook,instagram"})); when = r.when } catch(e) {}
  if (new Date(when) < new Date()){
    const w = zoneISO(new Date(Date.now() + 3600000), tz);  // the next full hour, in the posting zone
    const n = fromZone(w.slice(0, 14) + "00", tz);
    if (n && w.slice(0, 10) === day) when = n.toISOString();
  }
  openPost(null, {when});
}
async function confirmMove(id){
  let p; try { p = await sapi("post?" + qs({id})) } catch(e) { return toast(e.message) }
  const sched = Object.entries(p.results || {}).filter(([, r]) => r.status === "scheduled").map(([c]) => CH[c]);
  const go = async () => { dlg.close(); try {
      const r = sched.length ? await withProgress("Moving the scheduled post", A + "confirm_move", {id, pressed: "user"}, PROG) : await sapi("confirm_move", {id, pressed: "user"});
      if (!r.ok) showDialog("Not moved", `${(r.problems || []).map(x => `<div class="so-prob">${esc(x)}</div>`).join("")}<p>${esc(r.message || "")}</p>`, []);
      else { if (sched.length) dlg.close(); toast(r.message) }
    } catch(e) { toast(e.message) } loadCalendar(); if (SO.post && SO.post.id === id) openPost(id) };
  if (!sched.length) return go();
  showDialog("Move the scheduled post?", `<p>It is scheduled on ${esc(sched.join(", "))}. Confirming moves it there to ${esc(fmtDate(p.move.when))}.</p>`, [["Keep the old time", () => dlg.close()], ["Move it", go, "primary"]]);
}

// ---------- published grid ----------
async function loadPublished(){
  const main = byId("soMain"); main.innerHTML = `<div class="empty">Reading posts from Meta ...</div>`;
  try { SO.pub = await sapi("published?" + qs({days: SO.pubDays, channel: SO.pubCh})) } catch(e) { main.innerHTML = `<div class="so-prob">${esc(e.message)}</div>`; return }
  drawPublished();
}
function drawPublished(){
  const main = byId("soMain"); if (!SO.pub || SO.view !== "published") return;
  document.querySelectorAll("#soCols button").forEach(b => b.classList.toggle("on", b.dataset.c === SO.cols));
  const rows = SO.pub.rows;
  const data = [["reach","Reach"],["views","Views"],["likes","Likes / reactions"],["comments","Comments"],["shares","Shares"],["saves","Saves"],["link_clicks","Link clicks"],["clicks","All clicks"],["video_views","Video views"],["engagement","Engagement"]];
  const tot = k => rows.reduce((a, r) => a + (Number(r[k]) || 0), 0);
  let h = `<div class="so-legend">${rows.length} posts · read ${esc(fmtDate(SO.pub.fetched_at))} · numbers are lifetime totals per post from Meta (comments are counted, not read).</div>`;
  if (SO.pub.errors && SO.pub.errors.length) h += `<div class="so-warn" style="margin:6px 14px">${esc(SO.pub.errors.join("; "))}</div>`;
  h += `<div class="grid-wrap"><table class="so-grid"><thead><tr><th></th><th>Published</th><th>Channel</th><th>Format</th><th>Text</th>`;
  h += SO.cols === "data" ? data.map(([, l]) => `<th class="num">${l}</th>`).join("") : `<th>Link</th><th>In Studio</th>`;
  h += `<th></th></tr></thead><tbody>`;
  if (SO.cols === "data") h += `<tr><td></td><td><b>Total</b></td><td></td><td></td><td></td>${data.map(([k]) => `<td class="num"><b>${num(tot(k))}</b></td>`).join("")}<td></td></tr>`;
  for (const r of rows){
    h += `<tr><td>${r.image ? `<img src="${esc(r.image)}" alt="" loading="lazy" referrerpolicy="no-referrer">` : ""}</td><td>${esc(fmtDate(r.when))}</td><td class="so-chan ${esc(r.channel)}">${esc(CH[r.channel])}</td><td>${esc(r.format)}</td><td class="txt" title="${esc(r.text)}">${esc(r.text)}</td>`;
    h += SO.cols === "data" ? data.map(([k]) => `<td class="num">${num(r[k])}</td>`).join("")
      : `<td><a href="${esc(r.url)}" target="_blank" rel="noopener">Open</a></td><td>${r.studio_post ? `<button class="link" data-open="${esc(r.studio_post)}">Open post</button>` : ""}</td>`;
    h += `<td><button data-boost="${esc(r.id)}" data-ch="${esc(r.channel)}">Boost</button></td></tr>`;
  }
  h += `</tbody></table></div>`;
  main.innerHTML = rows.length ? h : `<div class="empty">No published posts in this period.</div>`;
  main.onclick = e => { const b = e.target.closest("[data-boost]"), o = e.target.closest("[data-open]");
    if (b) boostDialog(b.dataset.boost, b.dataset.ch, rows.find(r => r.id === b.dataset.boost));
    if (o) openPost(o.dataset.open) };
  main.ondragstart = main.ondragover = main.ondrop = null;
}
async function boostDialog(postId, channel, row){
  showDialog("Boost this post", `<div class="empty">Loading ad sets ...</div>`, []);
  let sets; try { sets = await sapi("adsets") } catch(e) { $("#dlgB").innerHTML = `<div class="so-prob">${esc(e.message)}</div>`; return }
  $("#dlgB").innerHTML = `<p class="hint">Makes a new ad from this ${esc(CH[channel])} post, <b>Paused</b>, in the Meta ads draft. Nothing changes on Meta until you push the draft in the Ads area (Meta account).</p>
    <div class="so-row"><label>Ad set</label><select id="soBSet">${sets.map(a => `<option value="${esc(a.id)}">${esc(a.campaign)} › ${esc(a.name)} (${esc(String(a.status).toLowerCase())})</option>`).join("")}</select></div>
    <div class="so-row"><label>Ad name</label><input type="text" id="soBName" value="${esc("Boost " + (row && row.text ? row.text.slice(0, 40).replace(/\s+/g, " ").trim() : postId))}"></div>`;
  $("#dlgF").innerHTML = ""; const b = document.createElement("button"); b.className = "primary"; b.textContent = "Add to Meta draft"; $("#dlgF").appendChild(b);
  b.onclick = async () => { b.disabled = true;
    try { const r = await sapi("boost", {post_id: postId, channel, adset_id: $("#soBSet").value, name: $("#soBName").value});
      showDialog(r.ok ? "Added to the Meta draft" : "Added, with problems", `<p>${esc(r.summary)}</p>${(r.problems || []).map(x => `<div class="so-prob">${esc(x)}</div>`).join("")}${(r.warnings || []).map(x => `<div class="so-warn">${esc(x)}</div>`).join("")}<p class="hint">Open Ads, pick the Meta account and use Push live to make it real (it stays Paused).</p>`, []);
    } catch(e) { b.disabled = false; toast(e.message) } };
}

// ---------- editor ----------
async function openPost(id, pre){
  const side = byId("soSide"); side.classList.add("on");
  if (!SO.lib) sapi("library").then(l => { SO.lib = l; if (SO.post) drawEditor() }).catch(() => {});
  if (!SO.tags) sapi("hashtags").then(t => { SO.tags = t; if (SO.post) drawEditor() }).catch(() => { SO.tags = [] });
  if (!id){ SO.post = {id: null, title: "", channels: ["facebook", "instagram"], when: "", caption: "", captions: {}, first_comment: "", hashtags: "", link: "", alt_text: "", location: "", media: [], format: "text", guest_photos: false, consent_confirmed: false, approval: "", note: "", utm: true, results: {}, status: "draft", check: null, final_links: {}, ...(pre || {})}; SO.tab = "all"; SO.quick = null; return drawEditor() }
  side.innerHTML = `<div class="empty">Loading ...</div>`;
  try { SO.post = await sapi("post?" + qs({id})) } catch(e) { side.innerHTML = `<div class="so-prob">${esc(e.message)}</div>`; return }
  if (window.TeamReviews) TeamReviews.ownerNote([`social/${id}`, "social"]); // [team reviews] "Owned by ..."
  SO.tab = "all"; SO.quick = null; drawEditor();
}
const locked = p => Object.values(p.results || {}).some(r => ["scheduled", "publishing"].includes(r.status));
function drawEditor(){
  const p = SO.post, side = byId("soSide"), ro = locked(p), dis = ro ? "disabled" : "";
  const li = SO.state && SO.state.linkedin_connected;
  const txt = SO.tab === "all" ? p.caption : ((p.captions || {})[SO.tab] ?? "");
  const res = Object.entries(p.results || {});
  const whenVal = p.when && !isNaN(new Date(p.when)) ? zoneISO(new Date(p.when), ptz()) : "";
  side.innerHTML = `<div class="so-ed" ${p.id ? `data-team-coll="social" data-team-id="${esc(p.id)}" data-team-label="${esc(p.title || (p.caption || "").slice(0, 60))}"` : ""}>
    <div class="so-edh"><h3>${p.id ? "Post" : "New post"} ${stChip(p.status || "draft")}</h3><button id="soClose" title="Close">Close</button></div>
    ${ro ? `<div class="so-warn">Scheduled: cancel it to edit. You can still move it in the calendar.</div>` : ""}
    ${res.length ? `<div class="so-res">${res.map(([c, r]) => `<div class="${r.error ? "so-prob" : "so-okk"}"><b>${esc(CH[c])}</b>: ${esc(stWord(r.status))}${r.scheduled_for && r.status === "scheduled" ? " for " + esc(fmtDate(r.scheduled_for)) + (r.via === "vps" ? " (VPS job)" : r.via === "local" ? " (Studio on this Mac)" : " (Meta)") : ""}${r.url ? ` · <a href="${esc(r.url)}" target="_blank" rel="noopener">open</a>` : ""}${r.error ? " · " + esc(r.error) : ""}${r.warning ? " · " + esc(r.warning) : ""}${r.status === "missed" ? " · use Post now if you still want it" : ""}${r.status === "needs_checking" ? ` <button data-checkagain="${esc(c)}">Check again</button>` : ""}</div>`).join("")}</div>` : ""}
    <div class="so-row"><label>Name in Studio (not posted)</label><input type="text" id="so_title" value="${esc(p.title)}" ${dis}></div>
    <div class="so-row"><label>Channels</label><div class="so-ticks">${CHS.map(c => { const off = c === "linkedin" && !li && !p.channels.includes(c);
      return `<label ${off ? `title="LinkedIn is not connected: see SoMe settings"` : ""}><input type="checkbox" data-chan="${c}" ${p.channels.includes(c) ? "checked" : ""} ${dis || (off ? "disabled" : "")}> ${CH[c]}${c === "linkedin" && !li ? ` <span class="chip">not connected</span>` : ""}</label>` }).join("")}</div></div>
    <div class="so-row"><label>Date and time (posting time zone: ${esc(ptz())})</label><input type="datetime-local" id="so_when" value="${esc(whenVal)}" ${dis}>
      <div class="hint" id="soMacTime" style="margin:4px 0 0">${esc(macNote(p.when))}</div>
      ${ro ? "" : `<div class="so-quick" id="soQuick">${(SO.quick || []).map(x => `<button data-qt="${esc(x.when)}">${esc(x.label)}</button>`).join("")}${p.id ? `<button data-addq="1" title="Give it the next free posting slot of its channels">Add to queue</button>` : ""}</div>`}</div>
    <div class="so-row"><label class="inl"><input type="checkbox" id="so_appr" ${p.approval ? "checked" : ""} ${dis}> Needs approval before it goes out</label>
      ${p.approval ? `<div class="so-consent">${p.approval === "approved" ? "Approved." : "Waiting for approval."} <input type="text" id="so_note" value="${esc(p.note)}" placeholder="Note for the approver" style="width:100%;margin-top:4px" ${dis}>${p.approval === "needed" && p.id ? ` <button id="soApprove" style="margin-top:4px">Approve</button>` : ""}</div>` : ""}</div>
    ${ro ? "" : `<div class="so-row so-libbar"><select id="soTpl"><option value="">Insert a template ...</option>${((SO.lib || {}).templates || []).map(t => `<option value="${esc(t.id)}">${esc(t.name)}</option>`).join("")}</select>
      <select id="soHs"><option value="">Add a hashtag set ...</option>${((SO.lib || {}).hashtag_sets || []).map(t => `<option value="${esc(t.id)}">${esc(t.name)}</option>`).join("")}</select>
      <button id="soSaveTpl" title="Keep this caption, hashtags, link and first comment as a template">Save as template</button></div>`}
    <div class="so-row"><label>Caption</label><div class="so-tabs" id="soTabs"><button data-tab="all" class="${SO.tab === "all" ? "on" : ""}">All channels</button>${p.channels.map(c => `<button data-tab="${c}" class="${SO.tab === c ? "on" : ""}">${CH[c]}${(p.captions || {})[c] ? " *" : ""}</button>`).join("")}</div>
      <textarea id="so_caption" rows="7" ${dis} placeholder="${SO.tab === "all" ? "The text for every ticked channel" : "Only for " + CH[SO.tab] + ": leave empty to use the shared caption"}">${esc(txt)}</textarea>
      <div class="so-cnt" id="soCnt"></div></div>
    <div class="so-row"><label>Hashtags (added at the end)</label><input type="text" id="so_hashtags" value="${esc(p.hashtags)}" placeholder="liveaboard scubadiving" ${dis}>
      ${ro || !(SO.tags || []).length ? "" : `<div class="so-tagsug"><span class="hint" style="margin:0">Our best hashtags (average engagement of our posts that used them):</span> ${(SO.tags || []).slice(0, 14).map(t => `<button class="chip" data-tag="${esc(t.tag)}" title="${t.posts} posts, ${t.avg_engagement} average engagement">${esc(t.tag)}</button>`).join(" ")}</div>`}</div>
    <div class="so-row"><label>Link (Facebook and LinkedIn only)</label><input type="text" id="so_link" value="${esc(p.link)}" placeholder="https://www.masterliveaboards.com/..." ${dis}>
      <label style="display:flex;gap:6px;margin-top:4px;font-size:12.5px;color:var(--text2)"><input type="checkbox" id="so_utm" ${p.utm !== false ? "checked" : ""} ${dis}> Trackable link (adds utm_source, utm_medium=social, utm_campaign where SoMe settings allow; never to links that have UTMs)</label>
      ${Object.entries(p.final_links || {}).map(([c, u]) => `<div class="hint" style="margin:2px 0;word-break:break-all">${esc(CH[c])}: ${esc(u)}</div>`).join("")}</div>
    <div class="so-row"><label>First comment (Instagram)</label><input type="text" id="so_first_comment" value="${esc(p.first_comment)}" ${dis}></div>
    <div class="so-row"><label>Alt text (what the image shows)</label><input type="text" id="so_alt_text" value="${esc(p.alt_text)}" ${dis}></div>
    <div class="so-row"><label>Location (optional: the location's Facebook Page ID)</label><input type="text" id="so_location" value="${esc(p.location)}" ${dis}></div>
    <div class="so-row"><label>Media: one image, 2-10 images (carousel) or one video / Reel. JPG, PNG, MP4 or MOV (Instagram: JPG only).</label>
      <div class="so-media" id="soMedia">${(p.media || []).map((m, i) => `<div class="so-mi">${m.kind === "image" ? `<img src="${esc(mediaUrl(m.media_id))}" alt="">` : `Video<br>${esc(m.name || "")}`}${ro ? "" : `<button class="x" data-rm="${i}" title="Remove" aria-label="Remove media ${i + 1}">x</button>${i ? `<button class="l" data-left="${i}" title="Move left" aria-label="Move media ${i + 1} left">‹</button>` : ""}`}</div>`).join("")}</div>
      ${ro ? "" : `<input type="file" id="soFile" accept=".jpg,.jpeg,.png,.mp4,.mov" multiple style="margin-top:6px">`}
      ${(p.media || []).some(m => m.kind === "video") ? `<label style="display:flex;gap:6px;margin-top:6px"><input type="checkbox" id="so_reel" ${p.format === "reel" ? "checked" : ""} ${dis}> Post as a Reel on Instagram (videos on Instagram are always Reels)</label>` : ""}</div>
    <div class="so-row"><label style="display:flex;gap:6px;color:var(--text)"><input type="checkbox" id="so_guest" ${p.guest_photos ? "checked" : ""} ${dis}> The media shows guests or other people</label>
      ${p.guest_photos ? `<div class="so-consent">Image consent must be confirmed before posting: every person shown has agreed to the use of their image.<label style="display:flex;gap:6px;margin-top:4px"><input type="checkbox" id="so_consent" ${p.consent_confirmed ? "checked" : ""} ${dis}> Consent is confirmed</label></div>` : ""}</div>
    <div class="so-row"><label>Checks</label><div id="soChecks">${checksHTML(p.check)}</div>${p.id ? `<button id="soLinks" style="margin-top:4px">Check links too</button>` : ""}</div>
    <div class="so-row"><label>Preview</label><div class="so-phones" id="soPhones"></div></div>
  </div>
  <div class="so-acts">${ro || p.status === "published" ? "" : `<span id="soAuto" class="hint" style="align-self:center" title="Edits are saved as a draft on their own, about a second after you stop">${p.id ? "Edits save on their own" : "Saves as a draft when you start typing"}</span>`}
    ${!ro && p.channels.some(c => !["published", "scheduled"].includes((p.results[c] || {}).status)) ? `<button class="primary" id="soSched">Schedule</button><button class="primary" id="soNow">Post now</button>` : ""}
    ${p.id && res.some(([, r]) => r.status === "scheduled") ? `<button class="danger" id="soCancel">Cancel schedule</button>` : ""}
    ${p.id && res.some(([c, r]) => r.status === "published" && c !== "instagram") ? `<button class="danger" id="soDelLive">Delete on channel ...</button>` : ""}
    ${p.id && !res.some(([, r]) => ["scheduled", "published", "publishing"].includes(r.status)) ? `<button class="danger" id="soDel">Delete draft</button>` : ""}</div>`;
  const q = s => side.querySelector(s);
  q("#soClose").onclick = async () => { if (SO.autoT) await autoSave(); side.classList.remove("on"); SO.post = null };
  const capEl = q("#so_caption");
  const upd = () => { collect(); counts(); phones(); const mt = q("#soMacTime"); if (mt) mt.textContent = macNote(p.when) };
  side.oninput = e => { if (e.target.matches("input[type=text], textarea, input[type=datetime-local]")){ upd(); if (!ro) scheduleAutoSave() } };
  side.onchange = e => { if (e.target.matches("input[type=checkbox]")){ collect(); drawEditor(); if (!ro) scheduleAutoSave() } };
  q("#soTabs").onclick = e => { const t = e.target.dataset.tab; if (!t) return; collect(); SO.tab = t; drawEditor() };
  side.querySelectorAll("[data-rm]").forEach(b => b.onclick = () => { collect(); p.media.splice(+b.dataset.rm, 1); drawEditor(); scheduleAutoSave() });
  side.querySelectorAll("[data-left]").forEach(b => b.onclick = () => { collect(); const i = +b.dataset.left; [p.media[i - 1], p.media[i]] = [p.media[i], p.media[i - 1]]; drawEditor(); scheduleAutoSave() });
  if (q("#soFile")) q("#soFile").onchange = e => addFiles([...e.target.files]);
  side.querySelectorAll("[data-checkagain]").forEach(b => b.onclick = async () => { b.disabled = true;
    try { const r = await sapi("check_again", {id: p.id, channel: b.dataset.checkagain}); SO.post = r.post; toast(r.message); drawEditor(); loadCalendar() } catch(e) { b.disabled = false; toast(e.message) } });
  if (q("#soQuick")) q("#soQuick").onclick = async e => { const t = e.target.closest("[data-qt]"), aq = e.target.closest("[data-addq]");
    if (t){ collect(); p.when = t.dataset.qt; drawEditor(); scheduleAutoSave() }
    if (aq){ collect(); const r = await saveDraft(false); if (!r) return; try { SO.post = await sapi("add_to_queue", {id: r.id}); toast("Next free slot: " + fmtDate(SO.post.when) + ". Press Schedule to make it real."); drawEditor(); loadCalendar() } catch(err) { toast(err.message) } } };
  if (!SO.quick && !ro) sapi("quick_times?" + qs({channels: p.channels.join(",")})).then(x => { SO.quick = x; if (SO.post === p) drawEditor() }).catch(() => { SO.quick = [] });
  if (q("#soApprove")) q("#soApprove").onclick = async () => { collect(); await saveDraft(false); try { SO.post = await sapi("approve", {id: SO.post.id, pressed: "user"}); toast("Approved"); drawEditor() } catch(e) { toast(e.message) } };
  if (q("#soTpl")) q("#soTpl").onchange = e => { const t = ((SO.lib || {}).templates || []).find(x => x.id === e.target.value); if (!t) return; collect();
    for (const k of ["caption", "hashtags", "link", "first_comment"]) if (t[k]) p[k] = t[k]; SO.tab = "all"; drawEditor() };
  if (q("#soHs")) q("#soHs").onchange = e => { const t = ((SO.lib || {}).hashtag_sets || []).find(x => x.id === e.target.value); if (!t) return; collect();
    p.hashtags = [...new Set((p.hashtags + " " + t.tags).split(/[\s,]+/).filter(Boolean))].join(" "); drawEditor() };
  if (q("#soSaveTpl")) q("#soSaveTpl").onclick = async () => { collect(); const name = prompt("Template name:", p.title || ""); if (!name) return;
    try { await sapi("library_save", {kind: "templates", item: {name, caption: p.caption, hashtags: p.hashtags, link: p.link, first_comment: p.first_comment}}); SO.lib = await sapi("library"); toast("Template saved"); drawEditor() } catch(e) { toast(e.message) } };
  side.querySelectorAll("[data-tag]").forEach(b => b.onclick = () => { collect(); const t = b.dataset.tag.replace(/^#/, ""); if (!p.hashtags.split(/[\s,]+/).map(x => x.replace(/^#/, "")).includes(t)) p.hashtags = (p.hashtags + " " + t).trim(); drawEditor() });
  if (q("#soSave")) q("#soSave").onclick = () => saveDraft(true);
  if (q("#soNow")) q("#soNow").onclick = postNow;
  if (q("#soSched")) q("#soSched").onclick = scheduleIt;
  if (q("#soCancel")) q("#soCancel").onclick = cancelIt;
  if (q("#soDelLive")) q("#soDelLive").onclick = deleteLive;
  if (q("#soDel")) q("#soDel").onclick = async () => { if (!confirm("Delete this draft from Studio? Nothing is posted or changed on any channel.")) return;
    try { await sapi("delete_draft", {id: p.id}); side.classList.remove("on"); SO.post = null; await loadState(); draw() } catch(e) { toast(e.message) } };
  if (q("#soLinks")) q("#soLinks").onclick = async () => { q("#soChecks").innerHTML = `<div class="hint">Checking links ...</div>`;
    try { const r = await sapi("post?" + qs({id: p.id, links: "1"})); p.check = r.check; q("#soChecks").innerHTML = checksHTML(p.check) } catch(e) { toast(e.message) } };
  counts(); phones();
  if (capEl && !ro) capEl.focus();
}
function checksHTML(c){
  if (!c) return `<div class="hint">Save the draft to run the checks.</div>`;
  if (!c.problems.length && !c.warnings.length) return `<div class="so-okk">All checks passed.</div>`;
  return c.problems.map(x => `<div class="so-prob">${esc(x)}</div>`).join("") + c.warnings.map(x => `<div class="so-warn">${esc(x)}</div>`).join("");
}
function collect(){
  const p = SO.post, side = byId("soSide"); if (!p || !side) return;
  const v = id => { const e = side.querySelector("#" + id); return e ? e.value : null };
  for (const k of ["title", "hashtags", "link", "first_comment", "alt_text", "location"]){ const x = v("so_" + k); if (x !== null) p[k] = x }
  const w = v("so_when"); if (w !== null){ const d = w ? fromZone(w, ptz()) : null; p.when = d ? d.toISOString() : "" }
  const cap = v("so_caption"); if (cap !== null){ if (SO.tab === "all") p.caption = cap; else { p.captions = p.captions || {}; if (cap) p.captions[SO.tab] = cap; else delete p.captions[SO.tab] } }
  const chans = [...side.querySelectorAll("[data-chan]")]; if (chans.length) p.channels = chans.filter(c => c.checked).map(c => c.dataset.chan);
  const g = side.querySelector("#so_guest"); if (g) p.guest_photos = g.checked;
  const ap = side.querySelector("#so_appr"); if (ap) p.approval = ap.checked ? (p.approval || "needed") : "";
  const nt = v("so_note"); if (nt !== null) p.note = nt;
  const ut = side.querySelector("#so_utm"); if (ut) p.utm = ut.checked;
  const cs = side.querySelector("#so_consent"); if (cs) p.consent_confirmed = cs.checked;
  const reel = side.querySelector("#so_reel");
  p.format = !p.media.length ? "text" : p.media.length > 1 ? "carousel" : p.media[0].kind === "video" ? (reel && reel.checked ? "reel" : "video") : "image";
  if (SO.tab !== "all" && !p.channels.includes(SO.tab)) SO.tab = "all";
}
const LIM = {facebook: 3000, instagram: 2200, linkedin: 3000};
function textFor(p, ch){
  let t = (p.captions || {})[ch] || p.caption || "";
  const tags = String(p.hashtags || "").replace(/,/g, " ").split(/\s+/).filter(x => x.replace(/#/g, "")).map(x => x.startsWith("#") ? x : "#" + x).join(" ");
  if (tags && !t.includes(tags)) t = (t.trimEnd() + "\n\n" + tags).trim();
  return t;
}
function counts(){
  const p = SO.post, el = document.getElementById("soCnt"); if (!el) return;
  el.innerHTML = p.channels.map(c => { const n = nChars(textFor(p, c)); return `<span class="${n > LIM[c] ? "over" : ""}">${CH[c]} ${n}/${LIM[c]}</span>` }).join(" · ")
    + (p.channels.includes("instagram") ? (() => { const n = nTags(textFor(p, "instagram")) + nTags(p.first_comment);
        return ` · <span class="${n > 30 ? "over" : ""}">${n}/30 hashtags${nTags(p.first_comment) ? " (with the first comment)" : ""}</span>` })() : "");
}
function phones(){
  const p = SO.post, el = document.getElementById("soPhones"); if (!el) return;
  const m = p.media || [], first = m[0];
  const name = (SO.state && SO.state.meta && (SO.state.meta.page_name)) || "Master Liveaboards";
  const ig = (SO.state && SO.state.meta && SO.state.meta.ig_username) || "master_liveaboards";
  el.innerHTML = p.channels.map(c => {
    const md = first ? `<div class="md">${first.kind === "image" ? `<img src="${esc(mediaUrl(first.media_id))}" alt="${esc(p.alt_text)}">` : `<span>${c === "instagram" ? "Reel" : "Video"}</span>`}${m.length > 1 ? `<span class="n">1/${m.length}</span>` : ""}</div>` : (c === "instagram" ? `<div class="md"><span>Needs an image or video</span></div>` : "");
    const link = c !== "instagram" && p.link ? `<div class="lk">${esc(p.link)}</div>` : "";
    return `<div class="so-phone ${c}"><div class="bar"></div><div class="ch">${CH[c]} preview</div><div class="hd"><span class="av"></span>${esc(c === "instagram" ? ig : name)}</div>
      ${c === "instagram" ? md + `<div class="cp"><b>${esc(ig)}</b> ${esc(textFor(p, c))}</div>` + (p.first_comment ? `<div class="cp sub">${esc(ig)} ${esc(p.first_comment)}</div>` : "")
        : `<div class="cp">${esc(textFor(p, c))}</div>` + md + link}</div>`;
  }).join("") || `<div class="hint">Tick a channel.</div>`;
}
async function addFiles(files){
  const p = SO.post; collect();
  for (const f of files){
    try {
      const data = await new Promise((res, rej) => { const r = new FileReader(); r.onload = () => res(r.result); r.onerror = rej; r.readAsDataURL(f) });
      const m = await sapi("media", {filename: f.name, data});
      p.media.push({media_id: m.media_id, kind: m.kind, name: m.name});
    } catch(e) { toast(f.name + ": " + e.message) }
  }
  drawEditor(); scheduleAutoSave();
}
// ---- autosave: the open post is saved as a draft ~1s after each edit, without redrawing the editor ----
function soAutoTxt(t){ const x = document.getElementById("soAuto"); if (x) x.textContent = t }
function scheduleAutoSave(){
  const p = SO.post; if (!p || p.status === "published") return;
  clearTimeout(SO.autoT); soAutoTxt("Saving ..."); SO.autoT = setTimeout(autoSave, 1000);
}
function autoSave(){
  clearTimeout(SO.autoT); SO.autoT = null;
  const p = SO.post; if (!p) return SO.autoChain || Promise.resolve();
  collect();
  SO.autoChain = (SO.autoChain || Promise.resolve()).then(async () => {
    const body = {post: {...p, when: p.when || ""}, by: "you"}; if (p.id) body.id = p.id;
    try {
      const r = await sapi("save", body); p.id = r.id; p.check = r.check;
      if (SO.post === p){ soAutoTxt("Saved as draft"); const c = document.getElementById("soChecks"); if (c) c.innerHTML = checksHTML(p.check) }
      await loadState(); if (SO.view === "calendar") loadCalendar();
    } catch(e) { soAutoTxt("Not saved: " + e.message); toast(e.message) }
  });
  return SO.autoChain;
}
async function saveDraft(show){
  if (SO.autoT){ clearTimeout(SO.autoT); SO.autoT = null } await SO.autoChain;
  collect(); const p = SO.post;
  const body = {post: {...p}, by: "you"}; if (p.id) body.id = p.id;
  body.post.when = p.when || "";
  try { const r = await sapi("save", body); SO.post = r; if (show) toast("Draft saved"); drawEditor(); await loadState(); if (SO.view === "calendar") loadCalendar(); return r }
  catch(e) { toast(e.message); return null }
}
// When nothing ran (refused before any step), there is no progress list to show: just the plain answer and the problems.
function resultDialog(title, r, list, notTitle){
  const ran = r.results && Object.keys(r.results).length;
  showDialog(r.ok ? title : (notTitle || "Not done"), `${ran ? (list || "") : ""}<p>${esc(r.message || "")}</p>${(r.problems || []).map(x => `<div class="so-prob">${esc(x)}</div>`).join("")}
    ${r.results ? `<div class="so-res">${Object.entries(r.results).map(([c, x]) => `<div class="${x.error ? "so-prob" : "so-okk"}"><b>${esc(CH[c])}</b>: ${esc(stWord(x.status))}${x.url ? ` · <a href="${esc(x.url)}" target="_blank" rel="noopener">${esc(x.url)}</a>` : ""}${x.error ? " · " + esc(x.error) : ""}</div>`).join("")}</div>` : ""}`, []);
}
async function confirmThen(title, html, label, fn){
  showDialog(title, html, [["Back", () => dlg.close()], [label, fn, "live"]]);
}
// [team reviews] in a team: what needs approval, and the one-line why, in the Post now / Schedule dialogs
const teamGate = id => window.TeamReviews ? TeamReviews.gate("social", "social", id) : Promise.resolve(null);
const teamSection = t => window.TeamReviews ? TeamReviews.section(t) : "";
const teamExtra = () => window.TeamReviews ? TeamReviews.extra() : {};
function teamWire(t){
  if (!window.TeamReviews) return;
  const bs = [...document.querySelectorAll("#dlgF .live")];
  const upd = () => bs.forEach(b => { b.disabled = !TeamReviews.ready(t) || !!(t && t.required && !t.count) });
  TeamReviews.wire(document.getElementById("dlgB"), null, upd); upd();
}
async function postNow(){
  const p = await saveDraft(false); if (!p) return;
  if (!p.check.ok) return resultDialog("", {ok: false, message: "Fix the problems first.", problems: p.check.problems}, "", "Not posted");
  const chs = p.channels.filter(c => !["published", "scheduled"].includes((p.results[c] || {}).status));
  const t = await teamGate(p.id);
  confirmThen("Post now?", `<p>This posts to <b>${esc(chs.map(c => CH[c]).join(", "))}</b> straight away${SO.state.test_mode ? " (test mode: nothing leaves the Mac)" : ""}.</p>
    ${chs.includes("instagram") ? `<p class="hint">Instagram fetches the files from a public address: Studio first puts them on media.mlmediahub.com.</p>` : ""}
    ${p.check.warnings.length ? `<p class="hint">Warnings:</p>${p.check.warnings.map(x => `<div class="so-warn">${esc(x)}</div>`).join("")}` : ""}${teamSection(t)}`, "Post now", async () => {
    sendNow(p.id, false);
  });
  teamWire(t);
}
function approvalDialog(r, then){
  showDialog("Needs approval", `<p>${esc(r.message)}</p>${r.note ? `<div class="so-consent"><b>Note:</b> ${esc(r.note)}</div>` : ""}`,
    [["Back", () => dlg.close()], ["Approve and continue", async () => { try { SO.post = await sapi("approve", {id: SO.post.id, pressed: "user"}); drawEditor(); then() } catch(e) { toast(e.message) } }, "primary"]]);
}
async function sendNow(id, again){
  try {
    const r = await withProgress("Posting", A + "post_now", {id, pressed: "user", confirm_repost: again, ...teamExtra()}, PROG);
    if (r.post){ SO.post = r.post; drawEditor() }
    if (r.needs_approval) return approvalDialog(r, () => sendNow(id, again));
    if (r.needs_confirm){
      showDialog("It may already be live", `<div class="so-warn">${esc(r.message)}</div><p class="hint">Open the channel and look for the post first. Posting again can make a double post.</p>`,
        [["Don't post", () => dlg.close()], ["Post again anyway", () => sendNow(id, true), "live"]]);
    } else resultDialog("Posted", r, await lastProgress(PROG), "Not posted");
  } catch(e) { toast(e.message) }
  await loadState(); draw();
}
// The dialog shows the engine's own answer (schedule_preview = what Schedule would do): its refusals, the checks, how each
// channel goes out and the shared calendar's clash warnings, before you confirm. It only offers what the engine accepts.
async function scheduleIt(){
  const p = await saveDraft(false); if (!p) return;
  if (!p.when) return toast("Pick a date and time first (or a quick pick)");
  if (p.approval === "needed") return approvalDialog({message: "This post is marked Needs approval: approve it first.", note: p.note}, scheduleIt);
  let pv; try { pv = await sapi("schedule_preview?" + qs({id: p.id})) } catch(e) { return toast(e.message) }
  const refused = Object.values(pv.refused || {}).flat();
  const needsSched = refused.some(x => x.includes("Studio's scheduler"));
  const go = chans => async () => {
    try { const r = await withProgress("Scheduling", A + "schedule", {id: p.id, pressed: "user", channels: chans, ...teamExtra()}, PROG);
      if (r.needs_approval) return approvalDialog(r, scheduleIt);
      resultDialog("Scheduled", r, await lastProgress(PROG), "Not scheduled"); if (r.post){ SO.post = r.post; drawEditor() } }
    catch(e) { toast(e.message) }
    await loadState(); draw();
  };
  const btns = [["Back", () => dlg.close()]];
  for (const o of pv.options || []) btns.push([o.label, go(o.channels), "live"]);
  const t = (pv.options || []).length ? await teamGate(p.id) : null;
  const mac = macNote(pv.when);
  const html = `<p>For <b>${esc(zoneTime(pv.when, pv.posting_tz || ptz()))}</b> (${esc(pv.posting_tz || ptz())}) on ${esc(pv.channels.map(c => CH[c]).join(", "))}.${mac ? `<br><span class="hint">${esc(mac)}</span>` : ""}</p>
    ${(pv.problems || []).length ? `<p class="hint">Fix these first:</p>${pv.problems.map(x => `<div class="so-prob">${esc(x)}</div>`).join("")}` : ""}
    ${refused.map(x => `<div class="so-prob">${esc(x)}</div>`).join("")}${needsSched ? `<button id="soOpenSet" style="margin:4px 0">Open SoMe settings</button>` : ""}
    ${Object.values(pv.how || {}).map(x => `<p class="hint">${esc(x)}</p>`).join("")}
    ${(pv.options || []).length ? "" : `<p class="hint">Nothing can be scheduled like this. Change the time or the channels, or use Post now at the time.</p>`}
    ${(pv.clashes || []).length ? `<p class="hint">Heads-up from the shared calendar (warnings only, nothing is stopped):</p>${pv.clashes.map(x => `<div class="so-warn">${esc(x.message)}</div>`).join("")}` : ""}
    ${(pv.warnings || []).length ? `<p class="hint">Warnings:</p>${pv.warnings.map(x => `<div class="so-warn">${esc(x)}</div>`).join("")}` : ""}${t ? teamSection(t) : ""}`;
  showDialog((pv.options || []).length ? "Schedule?" : "Not scheduled", html, btns);
  const os = document.getElementById("soOpenSet"); if (os) os.onclick = settingsDialog;
  if (t) teamWire(t);
}
function cancelIt(){
  const p = SO.post, chs = Object.entries(p.results || {}).filter(([, r]) => r.status === "scheduled").map(([c]) => c);
  confirmThen("Cancel the schedule?", `<p>${esc(chs.map(c => CH[c]).join(", "))} will not go out. The post stays here as a draft.</p>`, "Cancel schedule", async () => {
    try { const r = await withProgress("Cancelling", A + "cancel", {id: p.id, pressed: "user"}, PROG); resultDialog("Cancelled", r, await lastProgress(PROG), "Not cancelled"); if (r.post){ SO.post = r.post; drawEditor() } }
    catch(e) { toast(e.message) } await loadState(); draw();
  });
}
function deleteLive(){
  const p = SO.post, chs = Object.entries(p.results || {}).filter(([c, r]) => r.status === "published" && c !== "instagram").map(([c]) => c);
  confirmThen("Delete the live post?", `<p>This deletes the post on the channel. It can't be undone.</p>${chs.map(c => `<label style="display:flex;gap:6px"><input type="checkbox" data-dl="${c}" checked> ${CH[c]}</label>`).join("")}
    <p class="hint">Instagram posts can't be deleted through the API: delete those in the Instagram app.</p>`, "Delete", async () => {
    const sel = [...document.querySelectorAll("[data-dl]")].filter(x => x.checked).map(x => x.dataset.dl); if (!sel.length) return toast("Tick a channel");
    try { const r = await withProgress("Deleting", A + "delete_post", {id: p.id, pressed: "user", channels: sel}, PROG); resultDialog("Deleted", r, await lastProgress(PROG), "Not deleted"); if (r.post){ SO.post = r.post; drawEditor() } }
    catch(e) { toast(e.message) } await loadState(); draw();
  });
}

// ---------- settings ----------
async function settingsDialog(){
  await loadState(); const s = SO.state, st = s.settings || {};
  showDialog("SoMe settings", `
    <div class="set-sec"><h3>Meta</h3>${s.meta && s.meta.page_name ? `<p>Facebook Page <b>${esc(s.meta.page_name)}</b> and Instagram <b>@${esc(s.meta.ig_username)}</b> are connected through the Studio Meta token. DiveTravel pages are not connected.</p>` : `<div class="so-prob">${esc(s.meta_error || "Meta not reachable")}</div>`}</div>
    <div class="set-sec"><h3>Scheduling</h3>
      <p class="hint">Facebook is always scheduled by Meta itself when the time is 10 minutes to 30 days ahead. Instagram and LinkedIn (and Facebook further ahead) need something of ours to publish at the time.</p>
      <label style="display:flex;gap:6px;align-items:center"><input type="checkbox" id="soLocal" ${st.local_scheduler ? "checked" : ""}> <b>Let Studio on this Mac publish scheduled Instagram and LinkedIn posts while it is open</b></label>
      <div class="so-consent" id="soLocalNote" style="${st.local_scheduler ? "" : "display:none"}">The Mac must be on and Studio running at the scheduled time. If the Mac is asleep or Studio is closed, the post is <b>not</b> posted late: it shows as Missed, with a Post now button.
        <label style="display:flex;gap:6px;align-items:center;margin-top:6px">Still post up to <input type="number" id="soLate" min="0" max="240" value="${esc(st.late_minutes || 0)}" style="width:70px"> minutes late (0 = never)</label>
        ${s.scheduler_running ? "" : `<div class="hint" style="margin:4px 0 0">The scheduler thread starts with Studio (after a Studio restart).</div>`}</div>
      <label style="display:flex;gap:6px;align-items:center;margin-top:8px;color:var(--muted)"><input type="checkbox" id="soVps" disabled> ${esc(s.vps_parked || "VPS publisher: parked, not available yet. See the README")}</label></div>
    <div class="set-sec"><h3>Posting time zone</h3><p class="hint">Times in the post editor, quick picks, posting slots and the best time are in this zone. Email uses the same one (Setup &gt; Email &gt; Send timezone). This Mac is on ${esc(MAC_TZ || "an unknown zone")}.</p>
      <input type="text" id="soTz" list="soTzList" value="${esc(s.posting_tz || "Europe/London")}" style="width:260px"><datalist id="soTzList">${["Europe/London", "Europe/Copenhagen", "Europe/Berlin", "Asia/Bangkok", "Asia/Dubai", "America/New_York", MAC_TZ].filter((x, i, a) => x && a.indexOf(x) === i).map(z => `<option value="${esc(z)}">`).join("")}</datalist></div>
    <div class="set-sec"><h3>Posting slots (the queue)</h3><p class="hint">Weekly times per channel, for example "Tue 09:00, Thu 18:00". Add to queue picks the next free one.</p>
      ${CHS.map(c => `<div class="so-row"><label>${CH[c]}</label><input type="text" data-slots="${c}" value="${esc(((st.slots || {})[c] || []).join(", "))}" style="width:100%"></div>`).join("")}</div>
    <div class="set-sec"><h3>Trackable links</h3>
      ${["facebook", "linkedin"].map(c => `<label style="display:flex;gap:6px"><input type="checkbox" data-utm="${c}" ${(st.utm || {})[c] ? "checked" : ""}> Add UTMs to ${CH[c]} links</label>`).join("")}
      <div class="so-row" style="margin-top:6px"><label>utm_campaign when a post has no name</label><input type="text" id="soCamp" value="${esc(st.utm_campaign || "")}"></div>
      <p class="hint">utm_source = the channel, utm_medium = social, utm_campaign = the post's name. Links that already have UTMs are left alone. Instagram has no clickable links.</p></div>
    <div class="set-sec"><h3>LinkedIn</h3><p>${s.linkedin_connected ? "Connected." : "Not connected."}</p>
      <p class="hint"><b>Connect LinkedIn:</b> 1. Confirm the Master Liveaboards company page and who its admins are. 2. Create a LinkedIn developer app and apply for the Community Management API (LinkedIn approves it, this can take weeks). 3. A page admin signs in once to give the app w_organization_social. 4. Add the token and the page URN in Setup > LinkedIn (Access token, Organisation URN). Studio never shows the token.</p>
      <button id="soLiTest">Test connection</button> <span id="soLiOut" class="hint"></span></div>`, [["Close", () => dlg.close()], ["Save", saveSet, "primary"]]);
  document.getElementById("soLocal").onchange = e => { document.getElementById("soLocalNote").style.display = e.target.checked ? "" : "none" };
  // fields the team has set (Calendar > Team settings): read-only here
  const tf = s.team_fields || [], tsel = {slots:"[data-slots]", utm:"[data-utm]", utm_campaign:"#soCamp", late_minutes:"#soLate"};
  for (const k of tf) document.querySelectorAll(tsel[k] || "x-none").forEach(x => { x.disabled = true; x.title = "Set by your team"; });
  if (tf.length) document.querySelector(tsel[tf[0]] || "body").closest(".set-sec").insertAdjacentHTML("afterbegin", `<p class="hint"><b>Set by your team:</b> ${esc(tf.join(", ").replace(/_/g, " "))}. Owners and admins change these in Calendar &gt; Team settings.</p>`);
  document.getElementById("soLiTest").onclick = async () => { const o = document.getElementById("soLiOut"); o.textContent = "Checking ...";
    try { const r = await sapi("linkedin_test", {}); o.textContent = r.message } catch(e) { o.textContent = e.message } };
  async function saveSet(){
    const b = {local_scheduler: document.getElementById("soLocal").checked, late_minutes: +document.getElementById("soLate").value || 0,
      slots: Object.fromEntries([...document.querySelectorAll("[data-slots]")].map(x => [x.dataset.slots, x.value])),
      utm: Object.fromEntries([...document.querySelectorAll("[data-utm]")].map(x => [x.dataset.utm, x.checked])), utm_campaign: document.getElementById("soCamp").value,
      posting_tz: document.getElementById("soTz").value.trim()};
    for (const k of tf) delete b[k];
    try { await sapi("settings", b); toast("Saved"); dlg.close(); SO.quick = null; await loadState(); draw() } catch(e) { toast(e.message) }
  }
}

// ---------- feed preview ----------
async function loadFeed(){
  const main = byId("soMain"); main.innerHTML = `<div class="empty">Loading the Instagram grid ...</div>`;
  let f; try { f = await sapi("feed") } catch(e) { main.innerHTML = `<div class="so-prob">${esc(e.message)}</div>`; return }
  main.innerHTML = `<div class="so-legend">Instagram grid as followers will see it: planned posts (not live yet) first, then what is live. Newest top left.</div>
    <div class="so-feed">${f.items.map(x => `<div class="so-ft ${x.status !== "published" ? "plan" : ""}" ${x.post ? `data-open="${esc(x.post)}"` : x.url ? `data-url="${esc(x.url)}"` : ""} title="${esc(x.text)}">
      ${x.image ? `<img src="${esc(x.image)}" alt="" loading="lazy" referrerpolicy="no-referrer">` : `<span>${esc(x.kind || "Post")}</span>`}
      ${x.status !== "published" ? `<span class="so-fl">${esc(x.when ? fmtDate(x.when) : "no time")} ${stChip(x.status)}</span>` : ""}</div>`).join("") || `<div class="empty">Nothing yet.</div>`}</div>`;
  main.onclick = e => { const o = e.target.closest("[data-open]"), u = e.target.closest("[data-url]"); if (o) openPost(o.dataset.open); else if (u) window.open(u.dataset.url, "_blank", "noopener") };
  main.ondragstart = main.ondragover = main.ondrop = null;
}

// ---------- analytics, best time, competitors ----------
async function loadAnalytics(){
  const main = byId("soMain"); main.innerHTML = `<div class="empty">Reading from Meta ...</div>`;
  let a, hm, comp;
  try { [a, hm, comp] = await Promise.all([sapi("analytics?" + qs({days: SO.anaDays})), sapi("heatmap?" + qs({channel: SO.heatCh})), sapi("competitors")]) }
  catch(e) { main.innerHTML = `<div class="so-prob">${esc(e.message)}</div>`; return }
  const tbl = (head, rows) => `<div class="so-tw"><table><thead><tr>${head.map(h => `<th>${h}</th>`).join("")}</tr></thead><tbody>${rows.join("")}</tbody></table></div>`;
  const tiles = Object.entries(a.channels).map(([c, v]) => `<div class="tile"><div class="k">${esc(CH[c])}</div><div class="v">${num(v.reach)}</div><div class="hint" style="margin:0">reach · ${num(v.posts)} posts · ${v.engagement_rate ?? "-"}% engagement rate${v.followers != null ? ` · ${num(v.followers)} followers` : ""}${v.growth != null ? ` (${v.growth >= 0 ? "+" : ""}${num(v.growth)})` : ""}${v.link_clicks != null ? ` · ${num(v.link_clicks)} link clicks` : ""}</div></div>`).join("");
  const top = new Set((hm.top || []).map(t => t.day + ":" + t.hour));
  const flat = hm.cells.flat(), lo = Math.min(...flat), hi = Math.max(...flat), sh = v => hi > lo ? ((v - lo) / (hi - lo)).toFixed(3) : 0;  // full colour range
  const heat = `<div class="so-heat"><div></div>${[...Array(24)].map((_, h) => `<div class="hh">${h % 3 === 0 ? pad(h) : ""}</div>`).join("")}
    ${hm.cells.map((row, d) => `<div class="hd">${hm.days[d]}</div>` + row.map((v, h) => `<div class="hc ${top.has(d + ":" + h) ? "top" : ""}" style="--a:${sh(v)}" title="${hm.days[d]} ${pad(h)}:00 · score ${Math.round(v * 100)}"></div>`).join("")).join("")}</div>`;
  main.innerHTML = `<div class="so-ana">
    <div class="so-anabar"><select id="soAnaDays">${["7","14","30","90"].map(d => `<option value="${d}" ${SO.anaDays === d ? "selected" : ""}>Last ${d} days</option>`).join("")}</select>
      <button id="soReport">Download report (HTML)</button><span class="hint" style="margin:0">Organic posts only. Engagement rate = engagement / reach of the same posts.</span></div>
    <div class="tiles">${tiles}</div>
    <h3 class="so-h">Best time to post <select id="soHeatCh">${["instagram","facebook"].map(c => `<option value="${c}" ${SO.heatCh === c ? "selected" : ""}>${CH[c]}</option>`).join("")}</select></h3>
    <p class="hint">${esc((hm.basis || []).join(" + ") || "Not enough data yet")}. Darker = better; outlined = the top 3: ${(hm.top || []).map(t => esc(t.label)).join(", ") || "none yet"}. Times are in the posting time zone (${esc(hm.tz || ptz())})${SO.heatCh === "instagram" ? " (Meta's follower hours read as Pacific time, medium confidence)" : ""}.</p>${heat}
    <h3 class="so-h">By post type</h3>${tbl(["Channel","Type","Posts","Reach","Engagement","Engagement rate %"], a.by_type.map(x => `<tr><td>${esc(CH[x.channel])}</td><td>${esc(x.format)}</td><td class="num">${num(x.posts)}</td><td class="num">${num(x.reach)}</td><td class="num">${num(x.engagement)}</td><td class="num">${x.engagement_rate ?? ""}</td></tr>`))}
    <h3 class="so-h">By day of the week</h3>${tbl(["Day","Posts","Reach","Engagement","Engagement rate %"], a.by_day.map(x => `<tr><td>${esc(x.day)}</td><td class="num">${num(x.posts)}</td><td class="num">${num(x.reach)}</td><td class="num">${num(x.engagement)}</td><td class="num">${x.engagement_rate ?? ""}</td></tr>`))}
    <h3 class="so-h">Top posts</h3>${tbl(["Date","Channel","Type","Text","Reach","Engagement",""], a.top.map(x => `<tr><td>${esc(fmtDate(x.when))}</td><td>${esc(CH[x.channel])}</td><td>${esc(x.format)}</td><td class="txt" style="max-width:360px">${esc(x.text)}</td><td class="num">${num(x.reach)}</td><td class="num">${num(x.engagement)}</td><td><a href="${esc(x.url)}" target="_blank" rel="noopener">Open</a></td></tr>`))}
    <h3 class="so-h">Competitors (public facts)</h3><p class="hint">${esc(comp.note)}</p>
    <div style="display:flex;gap:6px;align-items:center;margin-bottom:6px"><input type="text" id="soComp" value="${esc(((SO.state.settings || {}).competitors || []).join(", "))}" placeholder="Instagram usernames, comma separated" style="flex:1"><button id="soCompSave">Save list</button></div>
    ${comp.rows.length ? tbl(["Account","Followers","Posts","Average likes","Average comments","Posts per week"], comp.rows.map(x => x.error ? `<tr><td>@${esc(x.username)}</td><td colspan="5">${esc(x.error)}</td></tr>`
      : `<tr><td>@${esc(x.username)}</td><td class="num">${num(x.followers)}</td><td class="num">${num(x.media_count)}</td><td class="num">${num(x.avg_likes)}</td><td class="num">${x.avg_comments ?? ""}</td><td class="num">${x.posts_per_week ?? ""}</td></tr>`)) : `<p class="hint">No accounts in the list yet.</p>`}</div>`;
  document.getElementById("soAnaDays").onchange = e => { SO.anaDays = e.target.value; loadAnalytics() };
  document.getElementById("soHeatCh").onchange = e => { SO.heatCh = e.target.value; loadAnalytics() };
  document.getElementById("soCompSave").onclick = async () => { try { await sapi("settings", {competitors: document.getElementById("soComp").value.split(/[\s,]+/).filter(Boolean)}); await loadState(); loadAnalytics() } catch(e) { toast(e.message) } };
  document.getElementById("soReport").onclick = async () => { try { const r = await sapi("report", {days: SO.anaDays});
      const url = URL.createObjectURL(new Blob([r.html], {type: "text/html"})); const a2 = document.createElement("a"); a2.href = url; a2.download = r.name; document.body.appendChild(a2); a2.click(); a2.remove(); setTimeout(() => URL.revokeObjectURL(url), 5000);
      toast("Report saved: " + r.name) } catch(e) { toast(e.message) } };
  main.onclick = null; main.ondragstart = main.ondragover = main.ondrop = null;
}

// ---------- library: templates, hashtag sets, evergreen autolists, CSV import ----------
async function loadLibrary(){
  const main = byId("soMain");
  try { SO.lib = await sapi("library") } catch(e) { main.innerHTML = `<div class="so-prob">${esc(e.message)}</div>`; return }
  const L = SO.lib, posts = (SO.state.posts || []);
  main.innerHTML = `<div class="so-ana so-lib">
    <h3 class="so-h" style="margin-top:0">Bulk import (CSV)</h3><p class="hint">Columns: date, time, channels, caption, link, first_comment, hashtags, media (a file path on this Mac or an https address; several split by |). Dates 2026-11-25 or 25/11/2026, times 09:00, channels facebook, instagram, linkedin (or fb, ig, li). Import makes drafts only: nothing is scheduled.</p>
    <input type="file" id="soCsvFile" accept=".csv,text/csv"> <textarea id="soCsv" rows="4" placeholder="date,time,channels,caption,link&#10;2026-11-25,09:00,fb;ig,Seven nights in the northern Red Sea.,https://www.masterliveaboards.com/">${esc(SO.csv || "")}</textarea>
    <div style="margin:6px 0"><button id="soCsvPv">Preview</button> <button class="primary" id="soCsvGo" disabled>Make drafts</button></div><div id="soCsvOut"></div>
    <h3 class="so-h">Templates</h3>${(L.templates || []).map(t => `<div class="so-lrow"><b>${esc(t.name)}</b> <span class="hint" style="margin:0">${esc((t.caption || "").slice(0, 90))}</span> <button class="link" data-rmlib="templates:${esc(t.id)}">Remove</button></div>`).join("") || `<p class="hint">None yet: use Save as template in the post editor.</p>`}
    <h3 class="so-h">Hashtag sets</h3>${(L.hashtag_sets || []).map(t => `<div class="so-lrow"><b>${esc(t.name)}</b> <span class="hint" style="margin:0">${esc(t.tags)}</span> <button class="link" data-rmlib="hashtag_sets:${esc(t.id)}">Remove</button></div>`).join("")}
    <div style="display:flex;gap:6px;margin:6px 0"><input type="text" id="soHsName" placeholder="Name, e.g. Egypt"><input type="text" id="soHsTags" placeholder="redsea egypt liveaboard" style="flex:1"><button id="soHsAdd">Add set</button></div>
    <h3 class="so-h">Evergreen autolists</h3><p class="hint">A list of saved posts that fills free posting slots with copies, in turn, as drafts. You still press Schedule on each: nothing is scheduled by itself.</p>
    ${(L.autolists || []).map(l => `<div class="so-lrow"><b>${esc(l.name)}</b> <span class="hint" style="margin:0">${l.post_ids.length} posts · ${esc((l.channels || []).map(c => CH[c]).join(", ") || "Facebook, Instagram")}</span> <button data-fill="${esc(l.id)}">Fill free slots, next 7 days</button> <button class="link" data-rmlib="autolists:${esc(l.id)}">Remove</button></div>`).join("")}
    <details style="margin:6px 0"><summary>New autolist</summary><div style="padding:6px 0"><input type="text" id="soAlName" placeholder="Name"> ${CHS.map(c => `<label><input type="checkbox" data-alch="${c}" ${c !== "linkedin" ? "checked" : ""}> ${CH[c]}</label>`).join(" ")}
      <div class="so-alposts">${posts.filter(p => !p.autolist).map(p => `<label style="display:block"><input type="checkbox" data-alp="${esc(p.id)}"> ${esc(p.title || (p.caption || "").slice(0, 70) || "(untitled)")} ${stChip(p.status)}</label>`).join("") || `<p class="hint">Save some posts first.</p>`}</div>
      <button id="soAlAdd">Save autolist</button></div></details></div>`;
  const out = document.getElementById("soCsvOut");
  document.getElementById("soCsvFile").onchange = async e => { const f = e.target.files[0]; if (f){ document.getElementById("soCsv").value = await f.text() } };
  document.getElementById("soCsvPv").onclick = async () => { SO.csv = document.getElementById("soCsv").value;
    try { const r = await sapi("csv_preview", {text: SO.csv});
      out.innerHTML = `<p class="hint">${r.ok} of ${r.total} rows are ready. Rows with problems are skipped.</p><div style="overflow:auto"><table><thead><tr><th>Row</th><th>When</th><th>Channels</th><th>Caption</th><th>Media</th><th>Checks</th></tr></thead><tbody>${r.rows.map(x => `<tr><td>${x.row}</td><td>${esc(x.fields.when ? fmtDate(x.fields.when) : "")}</td><td>${esc(x.fields.channels.map(c => CH[c]).join(", "))}</td><td class="txt">${esc(x.fields.caption)}</td><td>${x.media.length}</td><td>${x.problems.length ? x.problems.map(p => `<div class="so-prob">${esc(p)}</div>`).join("") : `<span class="so-okk">OK</span>`}${(x.warnings || []).map(w => `<div class="so-warn">${esc(w)}</div>`).join("")}</td></tr>`).join("")}</tbody></table></div>`;
      document.getElementById("soCsvGo").disabled = !r.ok } catch(e) { out.innerHTML = `<div class="so-prob">${esc(e.message)}</div>` } };
  document.getElementById("soCsvGo").onclick = async () => { try { const r = await sapi("csv_import", {text: document.getElementById("soCsv").value}); toast(r.message); SO.csv = ""; await loadState(); loadLibrary() } catch(e) { toast(e.message) } };
  document.getElementById("soHsAdd").onclick = async () => { try { await sapi("library_save", {kind: "hashtag_sets", item: {name: document.getElementById("soHsName").value, tags: document.getElementById("soHsTags").value}}); loadLibrary() } catch(e) { toast(e.message) } };
  document.getElementById("soAlAdd").onclick = async () => { try { await sapi("library_save", {kind: "autolists", item: {name: document.getElementById("soAlName").value,
      channels: [...main.querySelectorAll("[data-alch]")].filter(x => x.checked).map(x => x.dataset.alch), post_ids: [...main.querySelectorAll("[data-alp]")].filter(x => x.checked).map(x => x.dataset.alp)}}); loadLibrary() } catch(e) { toast(e.message) } };
  main.onclick = async e => { const rm = e.target.closest("[data-rmlib]"), fl = e.target.closest("[data-fill]");
    if (rm){ const [kind, id] = rm.dataset.rmlib.split(":"); try { await sapi("library_remove", {kind, id}); loadLibrary() } catch(err) { toast(err.message) } }
    if (fl){ try { const r = await sapi("autolist_fill", {id: fl.dataset.fill, days: 7}); toast(r.message); await loadState() } catch(err) { toast(err.message) } } };
  main.ondragstart = main.ondragover = main.ondrop = null;
}

window.AREAS = window.AREAS || {};
window.AREAS.social = {label: "SoMe", render, open: (coll, id) => openPost(id)};  // [team live] open: jump to a post
})();
