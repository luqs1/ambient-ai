// Ambient main window. Talks only to the local engine on this machine.
const report = msg => fetch("/api/clientlog", {method:"POST", headers:{"content-type":"application/json","X-Ambient":"1"}, body:JSON.stringify({page:location.pathname, msg:String(msg).slice(0,500)})}).catch(()=>{});
addEventListener("error", e => report(`${e.message} @${e.lineno}`));
addEventListener("unhandledrejection", e => report(`unhandled: ${e.reason?.message || e.reason}`));

// Veil (keeping secrets out of coding agents' traffic) is hidden for now. Flip this to bring back its setup step,
// the "secrets kept out" chip and the raw traffic link. The engine side is untouched either way.
const SHOW_VEIL = false;
const $ = s => document.querySelector(s);
const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const get = url => fetch(url).then(r => r.json());
const post = (url, body={}) => fetch(url, {method:"POST", headers:{"content-type":"application/json","X-Ambient":"1"}, body:JSON.stringify(body)}).then(r => r.json());
const toast = t => { const e=$("#toast"); e.textContent=t; e.classList.add("on"); clearTimeout(toast.h); toast.h=setTimeout(()=>e.classList.remove("on"),2600); };
const LOCK = '<svg class="lock" viewBox="0 0 12 12" aria-hidden="true"><path fill="currentColor" d="M3 5V3.5a3 3 0 0 1 6 0V5h1v7H2V5Zm1.5 0h3V3.5a1.5 1.5 0 0 0-3 0Z"/></svg>';

// Header scene: copy the ridge into the mirrored side tiles (CSS fills don't reach <use> copies)
document.querySelectorAll(".valley .flip").forEach(t => t.innerHTML = $("#ridge").outerHTML.replace('id="ridge"', ""));

function ago(ts){ if(!ts) return ""; const s=Date.now()/1000-ts; if(s<60) return "just now"; if(s<3600) return Math.floor(s/60)+"m ago"; if(s<86400) return Math.floor(s/3600)+"h ago"; return Math.floor(s/86400)+"d ago"; }
const cap = s => s ? s[0].toUpperCase() + s.slice(1) : s;
// Agents answer in light markdown: show bold and code, leave everything else as text.
const rich = s => esc(s).replace(/\*\*([^*\n]+)\*\*/g, "<b>$1</b>").replace(/`([^`\n]+)`/g, "<code>$1</code>");

// In-page confirm: the desktop web view answers window.confirm() with "no" without showing anything.
function ask(title, bodyHTML, yes = "OK"){
  return new Promise(res => {
    const m = $("#modal"); $("#m-title").textContent = title; $("#m-body").innerHTML = bodyHTML; $("#m-yes").textContent = yes;
    m.hidden = false; $("#m-yes").focus();
    const done = v => { m.hidden = true; $("#m-yes").onclick = $("#m-no").onclick = m.onclick = null; res(v); };
    $("#m-yes").onclick = () => done(true); $("#m-no").onclick = () => done(false);
    m.onclick = e => { if (e.target === m) done(false); };
  });
}

/* ── icons: an app's own icon, or a coloured letter tile for websites and unknown apps ── */
const noIcon = new Set(), SITES = new Set();
const HUES = ["#6a4fd0","#2b27c9","#1e86b0","#0e6b66","#c23a26","#a8690f","#3c8a76","#5638c4"];
const hue = s => HUES[[...s].reduce((a,c) => (a*31 + c.charCodeAt(0)) >>> 0, 7) % HUES.length];
// Two letters, so GitHub (Gi), Google (Go) and Google Calendar (GC) don't all read "G".
const initials = app => { const w = app.match(/[A-Za-z0-9]+/g) || ["?"]; return (w.length > 1 ? w[0][0] + w[1][0] : w[0].slice(0, 2)).replace(/^./, c => c.toUpperCase()); };
const tile = (app, size) => `<span class="tile" style="width:${size}px;height:${size}px;background:${hue(app)};font-size:${Math.round(size*.4)}px">${esc(initials(app))}</span>`;
const ANY = size => `<svg class="anyapp" width="${size}" height="${size}" viewBox="0 0 24 24" aria-hidden="true"><path fill="var(--violet)" d="M3 3H21V16H11L6 21V16H3Z"/><path fill="var(--far)" d="M3 3H21L12 12Z"/><path fill="var(--lime)" d="M8 8L10 10L8 12L6 10Z"/></svg>`;
function icon(app, size){
  if (!app || app === "your chat apps" || app === "any website" || app === "*") return ANY(size);
  if (noIcon.has(app)) return tile(app, size);       // websites get their icon from a local browser's own store
  return `<img src="/api/icon/${encodeURIComponent(app)}.png" width="${size}" height="${size}" alt="" data-app="${esc(app)}" data-size="${size}" class="${SITES.has(app) ? "siteicon" : ""}">`;
}
document.addEventListener("error", e => {
  const img = e.target; if (img.tagName !== "IMG" || !img.dataset.app) return;
  noIcon.add(img.dataset.app); img.outerHTML = tile(img.dataset.app, +img.dataset.size);
}, true);

const glob = (pat, s) => new RegExp("^" + pat.replace(/[.+^${}()|[\]\\]/g, "\\$&").replace(/\*/g, ".*").replace(/\?/g, ".") + "$").test(s);
const sigOf = m => m.sig || `${(m.app||"").toLowerCase()}:${m.kind}`;

let TRIGGERS = [], AGENTS = [], MS = {}, lastSig = "", asking = new Set(), seen = new Set(), firstPaint = true;

/* ── tabs ── */
let TAB = "moments";
function showTab(t){
  TAB = t;
  document.querySelectorAll(".tabs button").forEach(b => b.classList.toggle("on", b.dataset.tab === t));
  $("#view-moments").hidden = t !== "moments"; $("#view-apps").hidden = t !== "apps";
  if (t === "apps") loadAppsTab();
  try { history.replaceState(null, "", t === "apps" ? (AppsTab.app ? "#apps/" + encodeURIComponent(AppsTab.app) : "#apps") : "#"); } catch {}
}
document.querySelector(".tabs").addEventListener("click", e => { const b = e.target.closest("button"); if (b) showTab(b.dataset.tab); });

/* ── header: connected apps, always in the same order (apps first, then websites, by name) ── */
const STATUS = {connected:"Connected", paused:"Paused", unreadable:"Can't read this app", local:"Reads its local storage"};
const stableOrder = list => [...list.filter(x => !x.website), ...list.filter(x => x.website)];
async function loadApps(){
  const w = await get("/api/connected").catch(()=>null); if (!Array.isArray(w)) return;
  w.forEach(x => { if (x.website) SITES.add(x.app); });
  const show = stableOrder(w).slice(0, 7), more = w.length - show.length;
  const html = show.map(x =>
    `<button class="appbtn ${x.status === "paused" ? "off" : ""}" data-app="${esc(x.app)}" aria-label="${esc(x.app)}: ${esc(STATUS[x.status] || "")}">
      ${icon(x.app,38)}<span class="tip">${esc(x.app)} · ${esc(STATUS[x.status] || "")}</span></button>`).join("") +
    (more > 0 ? `<button class="appmore" aria-label="All apps">+${more}</button>` : "");
  if (html !== loadApps.html) { loadApps.html = html; $("#apps").innerHTML = html; }
}
$("#apps").addEventListener("click", e => {
  const b = e.target.closest("button"); if (!b) return;
  if (b.dataset.app) { if (AppsTab.app !== b.dataset.app) AppsTab.open(b.dataset.app); }
  else if (AppsTab.app) AppsTab.back();
  showTab("apps");
});

/* ── only what needs doing ── */
async function loadTodo(){
  const s = await get("/api/setup").catch(()=>null);
  const items = [];
  if (!s) items.push({t:"Ambient isn't running", d:"Start it from the menu bar and moments will show up here."});
  else {
    if (!s.ca_trusted) items.push({t:"Trust Ambient's certificate", d:"Your system will ask you to confirm. This is what lets Ambient read your apps' traffic on this device.", b:"Trust", k:"ca"});
    if (!s.extension_on) items.push({t:"Let Ambient see app traffic", d:s.capture_fix});
    if (SHOW_VEIL && !s.claude_protected) items.push({t:"Protect your coding agents", d:`One setting in ${s.profile} lets Veil keep secrets out of new Claude Code and Codex sessions.`, b:"Protect", k:"protect", profile:s.profile});
  }
  $("#todo").innerHTML = items.map(i => `<div class="slip"><span class="gem"></span><div class="txt"><b>${esc(i.t)}</b><small>${esc(i.d)}</small></div>${i.b?`<button class="btn go" data-k="${i.k}" data-profile="${esc(i.profile||"")}">${i.b}</button>`:""}</div>`).join("");
  $("#todo").style.display = items.length ? "" : "none";
  document.querySelectorAll(".view").forEach(v => v.style.marginTop = items.length ? "" : "14px");
  // "Listening" only when it really is: capture allowed and the certificate trusted.
  const ready = !!s && s.ca_trusted && s.extension_on;
  $("#status").classList.toggle("off", !ready);
  $("#status span").textContent = !s ? "Not running" : ready ? "Listening" : "Needs setup";
}
$("#todo").addEventListener("click", async e => {
  const b = e.target.closest("button"); if (!b) return;
  if (b.dataset.k === "ca") { b.disabled = true; const r = await post("/api/setup/trust-ca"); toast(r.note || "Done"); if (!r.ok) await post("/api/setup/open-ca"); }
  if (b.dataset.k === "protect") {
    if (!await ask("Let your agents trust Ambient?", `This adds <code>NODE_EXTRA_CA_CERTS</code> to ${esc(b.dataset.profile)}. Only agent sessions you start afterwards are affected. Running ones are left alone.`, "Add it")) return;
    const r = await post("/api/setup/protect-claude", {confirm:true}); toast(r.note || r.error);
  }
  loadTodo();
});

async function loadShield(){
  if (!SHOW_VEIL) return;
  const s = await get("/api/shield").catch(()=>null);
  const n = s?.secrets || 0;
  $("#shield").classList.toggle("on", n > 0);
  $("#shield span").textContent = `${n} secret${n===1?"":"s"} kept out today`;
}

/* ── triggers ── */
const whenText = t => t.text || (t.kinds||[]).map(k => k.label).join(" or ");
const flash = new Set(); let lastFired = null;
async function loadTriggers(){
  const r = await get("/api/triggers").catch(()=>null); if (!r) return;
  AGENTS = r.agents || [];
  if (lastFired) r.triggers.forEach(t => { if (t.fired > (lastFired[t.id] || 0)) { flash.add(t.id); setTimeout(() => { flash.delete(t.id); document.querySelector(`.t[data-id="${t.id}"]`)?.classList.remove("flash"); }, 4000); } });
  lastFired = Object.fromEntries(r.triggers.map(t => [t.id, t.fired]));
  const sigT = JSON.stringify([r.triggers, [...flash]]);
  if (sigT === loadTriggers.sig) return; loadTriggers.sig = sigT;
  TRIGGERS = r.triggers.sort((a, b) => (b.enabled - a.enabled) || (b.auto - a.auto) || (a.id - b.id));
  $("#triggers").innerHTML = TRIGGERS.map(t => {
    const stats = [t.fired ? `Fired ${t.fired}× · ${ago(t.last_fired)}` : "Hasn't fired yet", t.checked ? `${t.matched||0} of ${t.checked} checked matched` : ""].filter(Boolean).join(" · ");
    return `
    <div class="slip t ${t.enabled?"":"off"} ${t.auto&&t.enabled?"armed":""} ${flash.has(t.id)?"flash":""}" data-id="${t.id}">
      <div class="when"><span style="flex:1; min-width:0"><p class="sentence"><i>When</i> ${esc(whenText(t))}</p></span>
        <input type="checkbox" class="switch" data-k="enabled" ${t.enabled?"checked":""} aria-label="${esc(t.name)} on"></div>
      <div class="kchips">${(t.kinds||[]).slice(0,5).map(k => `<span class="kchip" title="${esc(k.sig)}">${icon(k.app,16)}<span>${esc(k.app)} · ${esc(k.label)}</span></span>`).join("")}${(t.kinds||[]).length>5?`<span class="kchip"><span>+${t.kinds.length-5} more</span></span>`:""}</div>
      ${t.semantic ? `<div class="cond">${LOCK}<span>Only when the content matches · checked on this device</span></div>` : ""}
      <div class="then"><span class="arrow">→</span><div class="what"><b>${esc(t.name)}</b>${t.action==="ask"&&t.instruction&&t.instruction!==t.name?`<small>${esc(t.instruction)}</small>`:""}</div></div>
      <div class="foot"><div class="seg" role="group" aria-label="When to run">
          <button data-k="auto" data-v="0" class="${t.auto?"":"on"}">Ask me first</button><button data-k="auto" data-v="1" class="${t.auto?"on":""}">Automatic</button></div>
        <span class="grow"></span><button class="edit" data-k="edit">Edit</button><button class="del" data-k="del">Remove</button></div>
      <div class="fired" style="margin-top:8px">${esc(stats)}</div>
    </div>`; }).join("") || `<div class="slip t"><p class="sentence">No triggers yet.</p><div class="kchips"><span class="kchip"><span>Write one in your own words, or pick something from an app in Apps.</span></span></div></div>`;
  const armed = TRIGGERS.filter(t => t.enabled && t.auto), active = TRIGGERS.filter(t => t.enabled);
  $("#t-hint").textContent = `${active.length} active${armed.length ? ` · ${armed.length} run${armed.length === 1 ? "s" : ""} on ${armed.length === 1 ? "its" : "their"} own` : ""}${TRIGGERS.length > active.length ? ` · ${TRIGGERS.length - active.length} off` : ""}.`;
  $("#armed").classList.toggle("on", active.length > 0);
  const ownText = armed.length === 1 ? ` · “${armed[0].name}” runs on its own` : armed.length ? ` · ${armed.length} run on their own` : "";
  $("#armed span").textContent = `${active.length} trigger${active.length === 1 ? "" : "s"} active${ownText}`;
  $("#armed").title = active.map(t => `${t.auto ? "⚡ " : ""}${t.name}: when ${whenText(t)}`).join("\n");
  lastSig = ""; loadMoments();
}
$("#armed").addEventListener("click", () => showTab("moments"));
$("#triggers").addEventListener("click", async e => {
  const el = e.target.closest("[data-k]"); if (!el || el.dataset.k === "enabled") return;
  const t = TRIGGERS.find(x => x.id == el.closest(".t").dataset.id); if (!t) return;
  if (el.dataset.k === "del") { if (!await ask(`Remove “${t.name}”?`, "It stops firing straight away.", "Remove")) return; await post(`/api/triggers/${t.id}/delete`); }
  if (el.dataset.k === "edit") return Builder.open(t);
  if (el.dataset.k === "auto") {
    const auto = el.dataset.v === "1"; if (!!t.auto === auto) return;
    await post("/api/triggers", {...t, auto}); toast(auto ? `“${t.name}” will now run on its own` : `“${t.name}” will wait for you`);
  }
  loadTriggers();
});
$("#triggers").addEventListener("change", async e => {
  if (e.target.dataset.k !== "enabled") return;
  const t = TRIGGERS.find(x => x.id == e.target.closest(".t").dataset.id);
  await post("/api/triggers", {...t, enabled: e.target.checked}); loadTriggers();
});
$("#addBtn").onclick = () => Builder.open();

/* ── moments ── */
const SCENE = `<svg viewBox="0 0 240 110" aria-hidden="true">
  <path fill="var(--far)" d="M0 80L40 44L70 66L110 30L150 64L185 40L240 72V110H0Z"/><path fill="var(--far-2)" d="M110 30L128 66L150 64Z"/>
  <path fill="var(--mid)" d="M0 92L50 64L95 88L140 58L190 90L240 70V110H0Z"/><path fill="var(--mid-2)" d="M140 58L160 90L190 90Z"/>
  <path fill="var(--meadow)" d="M0 100L80 90L160 104L240 94V110H0Z"/><path fill="var(--meadow-2)" d="M80 90L120 104L160 104Z"/>
  <path fill="var(--aqua)" d="M98 110L120 96H132L114 110Z"/>
  <path fill="var(--pine-d)" d="M196 104L204 70L212 104Z"/><path fill="var(--pine-l)" d="M204 70L212 104L207 104Z"/>
  <path fill="var(--coral)" d="M178 22L185 29L178 36L171 29Z"/></svg>`;
const autoRan = m => { const t = TRIGGERS.find(t => t.id === m.trigger_id); return !!(t && t.auto); };
function line(m){
  if (m.kind === "meeting") return `<b>Meeting ended</b>`;
  if (m.kind === "sent") return `<b>You</b> wrote · ${esc(m.place || m.app)}`;
  if (m.kind === "dm") return `<b>${esc(m.who)}</b> messaged you`;
  if (m.kind === "group") return `<b>${esc(m.who)}</b> in ${esc(m.place)}`;
  if (m.kind === "mention") return `<b>${esc(m.who)}</b> mentioned you in ${esc(m.place)}`;
  return `<b>${esc(m.app)}</b> · ${esc(m.title || "something happened")}`;
}
async function loadMoments(){
  let ms; try { ms = await get("/api/moments?limit=40"); } catch { return; }
  ms = ms.filter(m => m.status !== "dismissed");
  const sig = JSON.stringify([ms.map(m => [m.id, m.status, m.action_status, m.action_result, m.who, m.place]), [...asking], TRIGGERS.map(t=>[t.id,t.enabled,t.name,t.auto])]);
  if (sig === lastSig) return; lastSig = sig;
  MS = Object.fromEntries(ms.map(m => [m.id, m]));
  if (!ms.length) {
    $("#feed").innerHTML = `<div class="slip empty">${SCENE}<h3>All quiet in the valley.</h3>
      <p>When something in your apps matches one of your triggers, it lands here. Nothing else does.</p></div>`;
    firstPaint = false; return;
  }
  const focus = document.activeElement?.closest?.(".ask"), keepId = focus?.dataset.id, keepVal = keepId && document.activeElement.value;
  $("#feed").innerHTML = ms.map(m => {
    const ts = TRIGGERS.filter(t => t.enabled && !t.auto && (t.sigs||[]).some(p => glob(p, sigOf(m))));
    const fresh = m.status === "new" && !m.action_status;
    const landing = !firstPaint && !seen.has(m.id);
    let res = "";
    const gate = (m.gate || []).filter(g => g.passed || g.p == null).map(g => g.p == null
      ? `<div class="gateline no">${LOCK}<span>${esc(g.name)}: the on-device check wasn't available, so nothing was sent</span></div>`
      : `<div class="gateline yes">${LOCK}<span>${esc(g.name)}: matched (${Math.round(g.p*100)}%) · checked on this device</span></div>`).join("");
    if (m.action_status === "running") res = `<div class="result"><div class="by"><span class="thinking"><i></i><i></i><i></i></span> ${esc(m.action || "Your agent")} · working on it</div></div>`;
    else if (m.action_result && m.action_result.trim() === "(nothing)") res = `<div class="result none"><div class="by">${esc(m.action || "Agent")} · nothing to do here</div></div>`;
    else if (m.action_result) res = `<div class="result ${m.action_status==="error"?"err":""}"><div class="by"><span class="grow">${esc(m.action || "Agent")}${autoRan(m) ? " · ran on its own" : ""}</span><button data-copy="${m.id}">Copy</button></div>${rich(m.action_result)}</div>`;
    return `<article class="slip m ${fresh?"fresh":""} ${m.status!=="new"?"done":""} ${landing?"landing":""}">
      <div class="who">${icon(m.app,26)}<span class="line">${line(m)}</span><time>${ago(m.ts)}</time>
        <button class="x" data-dismiss="${m.id}" aria-label="Dismiss" title="Dismiss">✕</button></div>
      ${m.text ? `<p class="say">${esc(m.text)}</p>` : ""}
      <div class="acts">
        ${ts.map((t, j) => `<button class="btn ${j===0 && !m.action_result ? "go" : ""}" data-run="${t.id}" data-id="${m.id}" ${m.action_status==="running"?"disabled":""}>${esc(t.name)}</button>`).join("")}
        <button class="btn" data-askopen="${m.id}">Ask an agent…</button>
        ${m.status === "new" ? `<button class="btn quiet" data-done="${m.id}">Mark done</button>` : ""}</div>
      <form class="ask ${asking.has(m.id)?"on":""}" data-id="${m.id}"><input placeholder="What should your agent do with this?" aria-label="Ask an agent"><button class="btn go">Ask</button></form>
      ${gate}${res}</article>`;
  }).join("");
  ms.forEach(m => seen.add(m.id)); firstPaint = false;
  if (keepId) { const f = $(`.ask[data-id="${keepId}"] input`); if (f) { f.value = keepVal; f.focus(); } }
}
$("#feed").addEventListener("click", async e => {
  const b = e.target.closest("button"); if (!b || b.closest(".ask")) return;
  if (b.dataset.copy) { await navigator.clipboard.writeText(MS[b.dataset.copy].action_result); return toast("Copied"); }
  if (b.dataset.askopen) { const id = +b.dataset.askopen; asking.has(id) ? asking.delete(id) : asking.add(id); lastSig = ""; await loadMoments(); $(`.ask[data-id="${id}"] input`)?.focus(); return; }
  if (b.dataset.dismiss) await post(`/api/moments/${b.dataset.dismiss}/dismiss`);
  else if (b.dataset.done) await post(`/api/moments/${b.dataset.done}/done`);
  else if (b.dataset.run) { b.disabled = true; const r = await post(`/api/moments/${b.dataset.id}/action`, {trigger: +b.dataset.run}); if (r.error) toast(r.error); }
  else return;
  lastSig = ""; loadMoments();
});
$("#feed").addEventListener("submit", async e => {
  e.preventDefault();
  const f = e.target, id = +f.dataset.id, q = f.querySelector("input").value.trim();
  const r = await post(`/api/moments/${id}/action`, {action:"ask", question:q});
  if (r.error) return toast(r.error);
  asking.delete(id); lastSig = ""; loadMoments();
});

/* ── apps: the apps Ambient is connected to, in a fixed order; click one to see what it can see ── */
// Nothing here reorders by activity. New apps take their alphabetical place; counts update where they stand.
const STATUS_NOTE = {
  connected: "Ambient reads this app's activity on this device.",
  paused: "Paused. Ambient counts this app's activity but keeps none of its content.",
  unreadable: "This app seals its traffic and Ambient hasn't found anything it saves on this device yet.",
  local: "This app seals its traffic, so Ambient reads what it saves on this device instead.",
};
const plural = (n, w) => `${n} ${w}${n === 1 ? "" : "s"}`;
const kindsLine = x => x.status === "unreadable" ? "" : x.kinds ? plural(x.kinds, "kind") + " of activity" : "Nothing yet";
const boundSig = sig => TRIGGERS.some(t => t.enabled && (t.sigs||[]).some(p => glob(p, sig)));

const AppsTab = {
  app: null, data: null, keys: "", dkeys: "", scrollY: 0, shown: new Set(),
  tileHTML(x){
    return `<button class="apptile ${x.status}" data-app="${esc(x.app)}">
      ${icon(x.app, x.website ? 28 : 40)}<span class="nm">${esc(x.app)}</span>
      <span class="st"><i></i><span data-f="st">${esc(STATUS[x.status] || "")}</span></span>
      <small data-f="n">${esc(kindsLine(x))}</small></button>`;
  },
  renderList(list){
    list.forEach(x => { if (x.website) SITES.add(x.app); });
    const apps = list.filter(x => !x.website), sites = list.filter(x => x.website);
    const keys = JSON.stringify(list.map(x => [x.app, x.website]));
    if (keys !== this.keys) {                         // the set of apps changed: lay the grid out again
      this.keys = keys;
      $("#g-apps").innerHTML = apps.map(x => this.tileHTML(x)).join("");
      $("#g-sites").innerHTML = sites.map(x => this.tileHTML(x)).join("");
      $("#g-apps-h").hidden = !apps.length; $("#g-sites-h").hidden = !sites.length;
      if (!list.length) $("#g-apps").innerHTML = `<div class="slip empty">${SCENE}<h3>No apps yet.</h3><p>Use your apps for a minute. Each one shows up here once Ambient sees it.</p></div>`;
    } else {                                          // same apps: only the words inside each tile change
      for (const x of list) {
        const el = document.querySelector(`.apptile[data-app="${CSS.escape(x.app)}"]`); if (!el) continue;
        el.className = `apptile ${x.status}`;
        el.querySelector('[data-f="st"]').textContent = STATUS[x.status] || "";
        el.querySelector('[data-f="n"]').textContent = kindsLine(x);
      }
    }
    const on = list.filter(x => x.status !== "paused").length;
    $("#apps-hint").textContent = `${on} connected${list.length > on ? ` · ${list.length - on} paused` : ""}. Pick one to see what Ambient notices there.`;
  },
  kindHTML(k){
    const open = this.shown.has(k.sig);
    return `<div class="akind ${boundSig(k.sig) ? "bound" : ""}" data-sig="${esc(k.sig)}">
      ${dirGlyph(k)}
      <div class="lab"><b>${esc(cap(k.label))}</b><small data-f="n">${esc(kindStats(k))}</small></div>
      <div class="acts"><button class="linkbtn" data-ex="${esc(k.sig)}" aria-expanded="${open}">${open ? "Hide examples" : "Show examples"}</button>
        <button class="btn" data-mk="${esc(k.sig)}">Create a trigger from this</button></div>
      <div class="exs" ${open ? "" : "hidden"}></div></div>`;
  },
  renderDetail(d){
    const keys = JSON.stringify([d.app, d.status, d.kinds.map(k => k.sig), TRIGGERS.map(t => [t.id, t.enabled, t.sigs])]);
    const paused = d.status === "paused";
    if (keys === this.dkeys) {                        // same kinds: just refresh their counts in place
      d.kinds.forEach(k => { const el = document.querySelector(`.akind[data-sig="${CSS.escape(k.sig)}"] [data-f="n"]`); if (el) el.textContent = kindStats(k); });
      return;
    }
    this.dkeys = keys;
    const where = d.via.length ? `<small class="via">${d.website ? "Seen in" : "Also opened in"} ${esc(d.via.join(", "))}</small>` : "";
    $("#app-detail").innerHTML = `
      <button class="back linkbtn" data-back="1">← All apps</button>
      <section class="slip adetail ${d.status}">
        <header>${icon(d.app, 52)}
          <div class="grow"><h2>${esc(d.app)}</h2>
            <span class="st"><i></i>${esc(STATUS[d.status] || "")}</span>${where}</div>
          <label class="pausesw">${paused ? "Paused" : "Reading"}<input type="checkbox" class="switch" data-watch="${esc(d.app)}" ${paused ? "" : "checked"} aria-label="Read ${esc(d.app)}"></label>
        </header>
        <p class="note">${esc(STATUS_NOTE[d.status] || "")}</p>
        <h3 class="grouph">What Ambient can see</h3>
        ${d.kinds.length ? d.kinds.map(k => this.kindHTML(k)).join("")
          : `<p class="note">Nothing to trigger on yet. Use ${esc(d.app)} and what it does will show up here.</p>`}
        ${d.quiet ? `<p class="quiet">${plural(d.quiet, "background kind")} (syncing, loading, telemetry) not shown.</p>` : ""}
      </section>`;
    this.shown.forEach(sig => this.examples(sig));
  },
  async examples(sig){
    const box = document.querySelector(`.akind[data-sig="${CSS.escape(sig)}"] .exs`); if (!box) return;
    const k = this.data?.kinds.find(x => x.sig === sig);
    const ev = await get(`/api/events?sig=${encodeURIComponent(sig)}&limit=5`).catch(()=>[]);
    const texts = (Array.isArray(ev) ? ev : []).map(e => e.text).filter(Boolean);
    if (!texts.length && k?.example) texts.push(k.example);
    box.innerHTML = texts.length ? texts.map(t => `<p>“${esc(t)}”</p>`).join("") : `<p class="none">No text in this one, only that it happened.</p>`;
  },
  async open(app){
    if (!this.app) this.scrollY = scrollY;
    this.app = app; this.dkeys = ""; this.shown = new Set();
    $("#apps-list").hidden = true; $("#app-detail").hidden = false; $("#app-detail").innerHTML = "";
    try { history.replaceState(null, "", "#apps/" + encodeURIComponent(app)); } catch {}
    scrollTo(0, 0);
    await this.refresh();
  },
  back(){
    this.app = null; this.data = null; this.dkeys = "";
    $("#app-detail").hidden = true; $("#apps-list").hidden = false;
    try { history.replaceState(null, "", "#apps"); } catch {}
    scrollTo(0, this.scrollY);
    loadAppsTab();
  },
  async refresh(){
    if (this.app) {
      const app = this.app, d = await get(`/api/connected/${encodeURIComponent(app)}`).catch(()=>null);
      if (this.app !== app) return;
      if (!d || d.error) { $("#app-detail").innerHTML = `<button class="back linkbtn" data-back="1">← All apps</button><div class="slip empty"><h3>Nothing from ${esc(app)} yet.</h3></div>`; this.dkeys = ""; return; }
      this.data = d; this.renderDetail(d);
    } else {
      const list = await get("/api/connected").catch(()=>null);
      if (Array.isArray(list) && !this.app) this.renderList(list);
    }
  },
};
const dirGlyph = k => k.direction === "in" ? `<span class="dir in" title="Reached you">↙</span>` : `<span class="dir" title="You did this">↗</span>`;
const kindStats = k => `${k.n || 0}× · last seen ${ago(k.last_ts) || "a while ago"}${k.source === "local" ? " · from local storage" : ""}`;
function loadAppsTab(){ return AppsTab.refresh(); }

$("#apps-list").addEventListener("click", e => { const t = e.target.closest(".apptile"); if (t) AppsTab.open(t.dataset.app); });
$("#app-detail").addEventListener("click", e => {
  if (e.target.closest("[data-back]")) return AppsTab.back();
  const ex = e.target.closest("[data-ex]");
  if (ex) {
    const sig = ex.dataset.ex, box = ex.closest(".akind").querySelector(".exs"), open = !AppsTab.shown.has(sig);
    open ? AppsTab.shown.add(sig) : AppsTab.shown.delete(sig);
    box.hidden = !open; ex.textContent = open ? "Hide examples" : "Show examples"; ex.setAttribute("aria-expanded", open);
    if (open) { box.innerHTML = `<p class="none">Loading…</p>`; AppsTab.examples(sig); }
    return;
  }
  const mk = e.target.closest("[data-mk]"); if (!mk || !AppsTab.data) return;
  const d = AppsTab.data, k = d.kinds.find(k => k.sig === mk.dataset.mk); if (!k) return;
  Builder.open(null, {sig: k.sig, app: d.app, label: k.label, direction: k.direction, example: k.example, n: k.n, klass: k.klass});
});
$("#app-detail").addEventListener("change", async e => {
  const app = e.target.dataset.watch; if (!app) return;
  const on = e.target.checked;
  await post("/api/watch", {app, on}); toast(on ? `Reading ${app} again` : `Paused ${app}. Nothing from it is kept.`);
  AppsTab.dkeys = ""; loadAppsTab(); loadApps();
});

/* ── trigger builder: one sentence, matched to any app's activity ── */
const Builder = {
  t: null, picked: new Map(), sugg: [], auto: false, rec: null, timer: null, ptimer: null,
  open(t, seed){
    this.t = t || null; this.picked = new Map(); this.sugg = []; this.recorded = []; this.stopRec(); this.touched = false; this.semTouched = !!t; this.unsure = false;
    $("#b-title").textContent = t ? "Edit trigger" : "New trigger";
    $("#b-when").value = t ? (t.text || "") : seed ? `${seed.direction === "in" ? "" : "I "}${seed.label} in ${seed.app}`.trim() : "";
    $("#b-instr").value = t ? (t.action === "draft" ? "Draft a reply in my voice" : t.instruction || "") : "";
    $("#b-sem").checked = t ? !!t.semantic : false;
    $("#b-thr").value = t?.threshold || 0.5;
    this.auto = !!t?.auto;
    [...$("#b-auto").children].forEach(x => x.classList.toggle("on", (x.dataset.v === "1") === this.auto));
    $("#b-agent").innerHTML = `<option value="">Default agent</option>` + AGENTS.filter(a => a.id).map(a => `<option value="${esc(a.id)}" ${a.available?"":"disabled"} ${t?.agent===a.id?"selected":""}>${esc(a.label)}${a.local ? " · stays on this device" : ""}${a.available?"":" (not installed)"}</option>`).join("");
    $("#b-err").textContent = ""; $("#b-preview").innerHTML = "";
    if (t) (t.kinds||[]).forEach(k => this.picked.set(k.sig, k));
    if (seed) this.picked.set(seed.sig, seed);
    $("#sheet").hidden = false; $("#b-when").focus();
    this.render(); this.suggest(!!(t || seed));
  },
  close(){ this.stopRec(); $("#sheet").hidden = true; },
  async suggest(keepPicked){
    const text = $("#b-when").value.trim();
    if (text.length < 4) { this.sugg = []; return this.render(); }
    const r = await post("/api/triggers/suggest", {text}).catch(()=>null); if (!r || $("#b-when").value.trim() !== text) return;
    this.sugg = r.kinds || [];
    if (!keepPicked && !this.touched) {
      this.picked = new Map(); this.sugg.filter(k => k.picked).forEach(k => this.picked.set(k.sig, k));
      if (!this.semTouched) $("#b-sem").checked = !!r.about_content;      // the sentence describes content: check meaning
    }
    this.unsure = !r.sure;
    this.render();
  },
  render(){
    // What you just did (show me once) comes first, then what's ticked, then other suggestions.
    const rows = new Map(); (this.recorded || []).forEach(k => rows.set(k.sig, k));
    this.picked.forEach((k, s) => { if (!rows.has(s)) rows.set(s, k); }); this.sugg.forEach(k => { if (!rows.has(k.sig)) rows.set(k.sig, k); });
    const list = [...rows.values()];
    $("#b-sugg").innerHTML = list.length ? list.map(k => `<label class="opt ${this.picked.has(k.sig)?"on":""}">
        <input type="checkbox" data-sig="${esc(k.sig)}" ${this.picked.has(k.sig)?"checked":""}>${icon(k.app,20)}
        <b>${esc(cap(k.label))}<small>${esc(k.app)}${k.source === "local" ? " · local storage" : ""}</small></b>
        <span class="cnt">${k.justNow ? `<b class="now">seen just now</b>` : k.n ? k.n + "× seen" : ""}</span>
        <span class="ex">${k.example ? "“" + esc(k.example) + "”" : k.app === "your chat apps" ? "Slack, Discord and WhatsApp. For another app, name it or use “Show me once”." : k.app === "any website" ? "Every site you open; the on-device check decides which pages count." : ""}</span></label>`).join("")
      : `<div class="empty-s">${$("#b-when").value.trim().length < 4 ? "" : "Nothing like that seen yet. Use “Show me once” below and do it in the app."}</div>`;
    if (list.length && this.unsure && !this.picked.size) $("#b-sugg").insertAdjacentHTML("afterbegin", `<div class="empty-s">Not sure which of these you mean. Tick one, or use “Show me once” and do it in the app.</div>`);
    const hasText = [...this.picked.values()].some(k => k.klass !== "signal");
    $("#b-gate").hidden = !this.picked.size || !hasText;
    $("#b-strict").style.display = $("#b-sem").checked ? "" : "none";
    this.preview();
  },
  preview(){
    clearTimeout(this.ptimer);
    if (!this.picked.size) { $("#b-preview").innerHTML = ""; return; }
    this.ptimer = setTimeout(async () => {
      const body = {text: $("#b-when").value.trim(), sigs: [...this.picked.keys()], semantic: $("#b-sem").checked, threshold: +$("#b-thr").value, condition: this.t?.condition || null};
      const r = await post("/api/triggers/preview", body).catch(()=>null); if (!r) return;
      if (!r.checked) { $("#b-preview").innerHTML = `<div class="sum">No past activity of this kind to test against yet.</div>`; return; }
      const span = r.span_hours >= 1 ? ` over the last ${Math.round(r.span_hours)}h` : "";
      $("#b-preview").innerHTML = `<div class="sum">${r.semantic ? `Tested on your last ${r.checked} like this${span}: it would have fired <b>${r.matched}</b> time${r.matched===1?"":"s"}.` : `Fires every time. That was <b>${r.checked}</b> time${r.checked===1?"":"s"}${span}. Turn the check on to be pickier.`}</div>` +
        r.samples.slice(0, 8).map(s => `<div class="s ${s.passed?"yes":""}"><span class="p">${s.p == null ? (s.passed ? "✓" : "–") : Math.round(s.p*100) + "%"}</span><span>${s.who && s.who !== "you" ? esc(s.who) + ": " : ""}${esc(s.text || s.label || "")}</span></div>`).join("");
    }, 350);
  },
  async startRec(){
    const r = await post("/api/record/start", {seconds: 90}); this.rec = r.token; this.recorded = []; this.recTouched = false;
    $("#b-recbtn").textContent = "Stop watching";
    const tick = async () => {
      if (!this.rec) return;
      const s = await get(`/api/record/${this.rec}`).catch(()=>null); if (!s || s.error) return this.stopRec();
      $("#b-recmsg").innerHTML = `<span class="dot" style="display:inline-block; vertical-align:middle"></span> Do it now in the app. Watching for ${s.left}s… ${s.kinds.length ? `saw ${s.kinds.length} kind${s.kinds.length===1?"":"s"}` : ""}`;
      this.recorded = s.kinds.map(k => ({...k, justNow: true}));
      if (s.kinds.length && !this.recTouched) { this.picked = new Map(); this.touched = true; }   // a guess from the sentence gives way to what you actually did
      if (s.kinds.length) this.render();
      if (!s.active) return this.stopRec(s.kinds.length ? "Tick the one you just did." : "Didn't see anything new. Try once more, or the app may keep its traffic sealed.");
      this.rtimer = setTimeout(tick, 1000);
    };
    tick();
  },
  stopRec(msg){
    if (this.rec) post(`/api/record/${this.rec}/stop`).catch(()=>{});
    this.rec = null; clearTimeout(this.rtimer);
    $("#b-recbtn").textContent = "Not listed? Show me once"; $("#b-recmsg").textContent = msg || "";
  },
  async save(){
    const text = $("#b-when").value.trim(), instr = $("#b-instr").value.trim();
    if (!this.picked.size) return $("#b-err").textContent = "Pick at least one kind of activity above, or use “Show me once”.";
    if (!instr) return $("#b-err").textContent = "Say what your agent should do.";
    const draft = /^draft (a |my )?reply/i.test(instr);
    const body = {id: this.t?.id, text, sigs: [...this.picked.keys()], semantic: $("#b-sem").checked && !$("#b-gate").hidden, threshold: +$("#b-thr").value,
      condition: this.t?.condition && $("#b-sem").checked && text === this.t.text ? this.t.condition : null,
      action: draft ? "draft" : "ask", instruction: draft ? "" : instr, name: draft ? "Draft a reply" : (this.t && instr === this.t.instruction ? this.t.name : instr.split(/[.\n]/)[0].slice(0, 60)),
      auto: this.auto, agent: $("#b-agent").value, enabled: this.t ? !!this.t.enabled : true};
    const r = await post("/api/triggers", body);
    if (r.error) return $("#b-err").textContent = cap(r.error) + ".";
    this.close(); toast("Trigger saved"); loadTriggers.sig = ""; loadTriggers(); if (TAB === "apps") { AppsTab.dkeys = ""; loadAppsTab(); }
  },
};
$("#b-when").addEventListener("input", () => { Builder.touched = false; clearTimeout(Builder.timer); Builder.timer = setTimeout(() => Builder.suggest(false), 350); });
$("#b-sugg").addEventListener("change", e => {
  const sig = e.target.dataset.sig; if (!sig) return; Builder.touched = true; Builder.recTouched = true;
  const k = (Builder.recorded || []).find(x => x.sig === sig) || Builder.sugg.find(x => x.sig === sig) || Builder.picked.get(sig);
  e.target.checked ? Builder.picked.set(sig, k) : Builder.picked.delete(sig); Builder.render();
});
$("#b-sem").addEventListener("change", () => { Builder.semTouched = true; Builder.render(); });
$("#b-thr").addEventListener("input", () => Builder.preview());
$("#b-recbtn").onclick = () => Builder.rec ? Builder.stopRec() : Builder.startRec();
$("#b-ideas").onclick = e => { const b = e.target.closest("button"); if (b) { $("#b-instr").value = b.textContent; $("#b-instr").focus(); } };
$("#b-auto").onclick = e => { const b = e.target.closest("button"); if (!b) return; Builder.auto = b.dataset.v === "1"; [...$("#b-auto").children].forEach(x => x.classList.toggle("on", x === b)); };
$("#b-cancel").onclick = () => Builder.close();
$("#sheet").addEventListener("mousedown", e => { if (e.target.id === "sheet") Builder.close(); });
$("#builder").onsubmit = e => { e.preventDefault(); Builder.save(); };
// Keyboard: 1 = Moments, 2 = Apps, N = new trigger (when not typing in a field)
document.addEventListener("keydown", e => {
  if (e.metaKey || e.ctrlKey || e.altKey || /^(INPUT|TEXTAREA|SELECT)$/.test(document.activeElement?.tagName || "")) return;
  if (!$("#sheet").hidden || !$("#modal").hidden) return;
  if (e.key === "1") showTab("moments");
  else if (e.key === "2") showTab("apps");
  else if (e.key.toLowerCase() === "n") { e.preventDefault(); Builder.open(); }
});
document.addEventListener("keydown", e => { if (e.key === "Escape") { if (!$("#modal").hidden) $("#m-no").click(); else if (!$("#sheet").hidden) Builder.close(); else if (TAB === "apps" && AppsTab.app) AppsTab.back(); } });

/* ── go ── */
$("#shield").hidden = $("#details").hidden = !SHOW_VEIL;
{ const h = location.hash.match(/^#(?:apps|activity)(?:\/(.+))?$/); if (h) { if (h[1]) AppsTab.open(decodeURIComponent(h[1])); showTab("apps"); } }
loadApps(); loadTodo(); loadShield(); loadTriggers();
setInterval(loadMoments, 2000); setInterval(loadTriggers, 4000); setInterval(loadShield, 15000); setInterval(loadTodo, 15000); setInterval(loadApps, 20000);
setInterval(() => { if (TAB === "apps" && $("#sheet").hidden) loadAppsTab(); }, 3000);
setInterval(() => { lastSig = ""; loadMoments(); }, 60000);
