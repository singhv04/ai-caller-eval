/* VoiceEval UI — Calls → Evaluate → Report → Call. Dependency-free. */
const state = {
  recordings: [], metrics: [], packs: [], profiles: [], health: null,
  selected: new Set(),            // recording ids
  packOn: null, metricOff: new Set(), onlyReady: true, profile: null, readiness: null,
  pollTimer: null, lastRun: null,
};
const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => [...r.querySelectorAll(s)];
const app = $("#app");

// ---------------------------------------------------------------- utils
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const mmss = (s) => `${Math.floor(s / 60)}:${String(Math.round(s % 60)).padStart(2, "0")}`;
const ago = (iso) => {
  if (!iso) return "";
  const d = (Date.now() - new Date(iso)) / 1000;
  if (d < 60) return "just now"; if (d < 3600) return `${Math.round(d / 60)} min ago`;
  if (d < 86400) return `${Math.round(d / 3600)} h ago`; return new Date(iso).toLocaleDateString();
};
const M = (id) => state.metrics.find((m) => m.id === id) || { id, name: id, unit: "", question: "", target: "" };
const packOf = (id) => state.packs.find((p) => p.metrics.includes(id));
function fmt(r) {
  if (!r || r.value == null) return "—";
  const v = r.value, u = r.unit || "";
  if (u === "ms") return v >= 1000 ? `${(v / 1000).toFixed(2)} s` : `${Math.round(v)} ms`;
  if (u === "%") return `${v === 0 ? 0 : v < 10 ? v.toFixed(1) : Math.round(v)}%`;
  if (u === "score") return `${v.toFixed(1)} / 5`;
  if (u === "") return v.toFixed(2);
  if (u === "$") return `$${v.toFixed(2)}`;
  return `${v < 10 ? v.toFixed(2) : v.toFixed(1)} ${u}`;
}
const BAND_LABEL = { good: "On target", warn: "Watch", bad: "Issue" };
const bandPill = (b) => (b ? `<span class="pill ${b}">${BAND_LABEL[b]}</span>` : `<span class="pill neutral">Measured</span>`);
async function api(path, opts = {}) {
  const res = await fetch(path, opts);
  if (!res.ok) {
    let msg = res.statusText;
    try { msg = (await res.json()).detail || msg; } catch {}
    throw new Error(typeof msg === "string" ? msg : JSON.stringify(msg));
  }
  return res.json();
}
const post = (path, body) => api(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
function toast(msg, error = false) {
  const t = $("#toast"); t.textContent = msg; t.className = "toast" + (error ? " error" : "");
  clearTimeout(toast._t); toast._t = setTimeout(() => t.classList.add("hidden"), 4500);
}
const isMono = (r) => r?.meta?.channel_source === "mono_diarized";

// ---------------------------------------------------------------- boot / routing
async function boot() {
  const [h, m, profiles] = await Promise.all([api("/api/health"), api("/api/metrics"), api("/api/profiles")]);
  state.health = h; state.metrics = m.metrics; state.packs = m.packs;
  state.profiles = await Promise.all(profiles.map(async (name) => ({ name, ...(await api(`/api/profiles/${name}`)) })));
  state.profile = state.profiles.find((p) => p.name !== "default")?.name || state.profiles[0]?.name;
  renderStatus();
  window.addEventListener("hashchange", route);
  $$("[data-close]").forEach((el) => el.addEventListener("click", closeDrawer));
  document.addEventListener("keydown", (e) => e.key === "Escape" && closeDrawer());
  route();
}
function renderStatus() {
  const h = state.health;
  $("#sys-status").innerHTML = `
    <a href="#/settings" class="pill ${h.judge.available ? "good" : "locked"}" title="${esc(h.judge.reason || "")}">${h.judge.available ? `● ${esc(h.judge.label)} · ${esc(h.judge.model)}` : "○ LLM judge off — set up"}</a>
    <span class="pill ${h.asr.provider ? "good" : "locked"}">${h.asr.provider ? "● Transcription on" : "○ Transcription off"}</span>`;
}
function route() {
  clearTimeout(state.pollTimer);
  app.onclick = null; app.oninput = null;
  const [, page, id, sub, subId] = location.hash.split("/");
  const nav = { calls: "calls", evaluate: "calls", evaluations: "evaluations", report: "evaluations", run: "evaluations", settings: "settings" }[page] || "calls";
  $$("nav a").forEach((a) => a.classList.toggle("active", a.dataset.nav === nav));
  window.scrollTo(0, 0);
  if (page === "evaluate") { if (id) state.selected = new Set(id.split(",")); return renderEvaluate(); }
  if (page === "evaluations") return renderEvaluations();
  if (page === "settings") return renderSettings();
  if ((page === "report" || page === "run") && id) {
    return renderReport(id).then(() => (sub === "call" || sub === "item") && subId && state.lastRun && openCallFromRun(state.lastRun, subId));
  }
  return renderCalls();
}

// ================================================================= CALLS
async function renderCalls() {
  state.recordings = await api("/api/recordings");
  const recs = state.recordings;
  app.innerHTML = `
    <div class="page-head">
      <div><h1>Calls</h1><p>Recordings of your AI caller talking to people. Upload calls, then select the ones you want to evaluate.</p></div>
    </div>
    ${uploadBlock()}
    <div class="section">
      ${recs.length ? `
      <div class="panel calls">
        <div class="call-head"><input type="checkbox" id="sel-all"/><span>Call</span><span>Length</span><span>Audio</span><span>Latest evaluation</span><span></span></div>
        ${recs.map(callRow).join("")}
      </div>` : `<div class="panel empty"><b>No calls yet</b>Drop a recording above to get started.</div>`}
    </div>
    <div id="actionbar"></div>`;
  bindUpload();
  const sync = () => {
    $$(".call-row").forEach((row) => {
      const on = state.selected.has(row.dataset.id);
      row.classList.toggle("selected", on); $("input", row).checked = on;
    });
    const all = $("#sel-all"); if (all) all.checked = recs.length && recs.every((r) => state.selected.has(r.id));
    const n = state.selected.size;
    $("#actionbar").innerHTML = n ? `<div class="actionbar"><span><b>${n}</b> call${n > 1 ? "s" : ""} selected</span>
      <button class="ghost" id="clear-sel">Clear</button><button class="primary" id="go-eval">Evaluate →</button></div>` : "";
  };
  sync();
  app.onclick = (e) => {
    if (e.target.id === "go-eval") return (location.hash = "#/evaluate");
    if (e.target.id === "clear-sel") { state.selected.clear(); return sync(); }
    if (e.target.id === "sel-all") { recs.forEach((r) => (e.target.checked ? state.selected.add(r.id) : state.selected.delete(r.id))); return sync(); }
    const rep = e.target.closest("[data-report]");
    if (rep) { e.stopPropagation(); location.hash = `#/report/${rep.dataset.report}/call/${rep.dataset.item}`; return; }
    const open = e.target.closest("[data-open]");
    if (open) return openCall(open.dataset.open);
    const row = e.target.closest(".call-row");
    if (row) { const id = row.dataset.id; state.selected.has(id) ? state.selected.delete(id) : state.selected.add(id); sync(); }
  };
}
function callRow(r) {
  const le = r.last_eval;
  return `<div class="call-row" data-id="${r.id}">
    <input type="checkbox"/>
    <div><div class="name">${esc(r.name)}</div><div class="sub">${Object.values(r.tags || {}).map((t) => `<span class="tag">${esc(t)}</span>`).join("") || esc(r.original_filename)}</div></div>
    <span>${mmss(r.duration_s)}</span>
    <span>${isMono(r) ? '<span class="pill warn" title="Single-channel recording; speakers were separated automatically. Overlap timing is approximate.">Mono · auto-split</span>' : '<span class="pill good">Dual-channel</span>'}</span>
    <span>${le ? `<div class="score-mini"><span><i class="dot bad"></i>${le.bad} issue${le.bad === 1 ? "" : "s"}</span><span><i class="dot warn"></i>${le.warn} watch</span><span><i class="dot good"></i>${le.good} ok</span>
         <a href="javascript:void 0" data-report="${le.run_id}" data-item="${le.item_id}">View</a></div><div class="sub">${ago(le.finished_at)}</div>` : '<span class="muted small">Not evaluated yet</span>'}</span>
    <span style="text-align:right"><button class="small" data-open="${r.id}">Open</button></span>
  </div>`;
}
function uploadBlock() {
  return `<div class="dropzone" id="dropzone">
      <div class="dz-icon">⬆</div>
      <div class="dz-text grow"><b>Drop call recordings here</b>
        <span class="muted">WAV, MP3, M4A, FLAC… Stereo files (one person per channel) give exact timing; mono files are split into speakers automatically.</span></div>
      <label class="btn">Browse files<input type="file" id="file-input" multiple accept="audio/*,.wav,.mp3,.flac,.m4a,.ogg,.opus" hidden/></label>
    </div>
    <details class="section" style="margin-top:10px"><summary class="muted small">Upload options — channel side, transcript, tags, separate files per speaker</summary>
      <div class="panel pad" style="margin-top:10px">
        <div class="upload-opts">
          <label class="field">In stereo files, the caller (human) is on
            <select id="up-hc"><option value="left">Left channel</option><option value="right">Right channel</option></select></label>
          <label class="field">Transcript JSON (optional, applies to a single upload)<input type="file" id="up-tr" accept=".json"/></label>
          <label class="field">Tags (JSON, optional)<input type="text" id="up-tags" placeholder='{"accent": "us", "scenario": "booking"}'/></label>
        </div>
        <div class="upload-opts">
          <label class="field">Caller-only audio file<input type="file" id="up-h" accept="audio/*"/></label>
          <label class="field">AI-only audio file<input type="file" id="up-a" accept="audio/*"/></label>
          <div style="align-self:end"><button id="up-pair">Upload as one call</button></div>
        </div>
      </div>
    </details>`;
}
function bindUpload() {
  const dz = $("#dropzone"), fi = $("#file-input");
  ["dragenter", "dragover"].forEach((ev) => dz.addEventListener(ev, (e) => { e.preventDefault(); dz.classList.add("drag"); }));
  ["dragleave", "drop"].forEach((ev) => dz.addEventListener(ev, (e) => { e.preventDefault(); dz.classList.remove("drag"); }));
  dz.addEventListener("drop", (e) => uploadFiles([...e.dataTransfer.files]));
  fi.addEventListener("change", () => uploadFiles([...fi.files]));
  $("#up-pair").addEventListener("click", async () => {
    const h = $("#up-h").files[0], a = $("#up-a").files[0];
    if (!h || !a) return toast("Choose both the caller-only and the AI-only file", true);
    const fd = new FormData(); fd.append("human_file", h); fd.append("ai_file", a); extraFields(fd, true);
    await doUpload([fd]);
  });
}
function extraFields(fd, single) {
  const tags = $("#up-tags")?.value.trim();
  if (tags) fd.append("tags", tags);
  const tr = $("#up-tr")?.files[0];
  if (tr && single) fd.append("transcript", tr);
}
async function uploadFiles(files) {
  files = files.filter((f) => /audio|\.(wav|mp3|flac|m4a|ogg|opus)$/i.test(f.type + f.name));
  if (!files.length) return toast("No audio files found", true);
  const forms = files.map((f) => { const fd = new FormData(); fd.append("file", f); fd.append("human_channel", $("#up-hc").value); extraFields(fd, files.length === 1); return fd; });
  await doUpload(forms);
}
async function doUpload(forms) {
  const dz = $("#dropzone b"); let ok = 0;
  for (const [i, fd] of forms.entries()) {
    dz.textContent = `Processing ${i + 1} of ${forms.length}… (mono files take a few seconds longer)`;
    try { const r = await api("/api/recordings", { method: "POST", body: fd }); state.selected.add(r.id); ok++; }
    catch (e) { toast(e.message, true); }
  }
  if (ok) toast(`Added ${ok} call${ok > 1 ? "s" : ""} — selected and ready to evaluate`);
  renderCalls();
}

// ================================================================= EVALUATE
async function renderEvaluate() {
  if (!state.recordings.length) state.recordings = await api("/api/recordings");
  const ids = [...state.selected].filter((id) => state.recordings.some((r) => r.id === id));
  state.selected = new Set(ids);
  state.readiness = ids.length ? await post("/api/readiness", { recording_ids: ids }) : null;
  if (!state.packOn) {
    state.packOn = new Set(state.packs.filter((p) => readyCount(p) > 0 && p.id !== "human_ratings").map((p) => p.id));
  }
  drawEvaluate();
}
const rd = (id) => state.readiness?.metrics[id] || { status: "ready" };
const runnable = (id) => ["ready", "partial"].includes(rd(id).status);
const readyCount = (p) => p.metrics.filter(runnable).length;
function chosenMetrics() {
  const out = [];
  for (const p of state.packs) {
    if (!state.packOn.has(p.id)) continue;
    for (const id of p.metrics) {
      if (state.metricOff.has(id) || out.includes(id)) continue;
      if (state.onlyReady && !runnable(id)) continue;
      out.push(id);
    }
  }
  return out;
}
const STATUS_TXT = { ready: ["good", "Ready"], partial: ["warn", "Some calls"], needs_llm: ["locked", "Needs LLM key"], needs_transcript: ["locked", "Needs transcript"], needs_data: ["locked", "Needs data"], needs_more_calls: ["locked", "Needs more calls"] };
function drawEvaluate() {
  const recs = state.recordings;
  const sel = recs.filter((r) => state.selected.has(r.id));
  const chosen = chosenMetrics();
  const locked = state.packs.filter((p) => state.packOn.has(p.id)).flatMap((p) => p.metrics).filter((id, i, a) => a.indexOf(id) === i && !runnable(id));
  const judgeOff = !state.health.judge.available;
  const prof = state.profiles.find((p) => p.name === state.profile);
  app.innerHTML = `
    <div class="page-head"><div><h1>New evaluation</h1><p>Three choices: which calls, what to check, and what the AI caller is supposed to do.</p></div></div>

    <div class="step"><div class="step-num">1</div><div>
      <div class="step-title">Calls</div><div class="step-sub">${sel.length ? `${sel.length} selected. Click to add or remove.` : "Pick at least one call."}</div>
      <div class="chips">${recs.map((r) => `<button class="${state.selected.has(r.id) ? "primary" : ""} small" data-toggle-rec="${r.id}">${state.selected.has(r.id) ? "✓ " : "+ "}${esc(r.name)}</button>`).join("")}</div>
    </div></div>

    <div class="step"><div class="step-num">2</div><div>
      <div class="step-title">What do you want to check?</div>
      <div class="step-sub">Each area shows how many of its checks can run on these calls with your current setup.</div>
      ${judgeOff ? `<div class="notice" style="margin-bottom:12px"><span>🔒</span><div><b>Conversation, compliance and safety checks need the LLM judge</b>
        <span class="muted">Connect any LLM — Anthropic Claude, OpenAI, Google Gemini or an OpenAI-compatible API — in <a href="#/settings">Settings</a> (or put the key in <span class="mono">.env</span>). Until then those checks are skipped or use simple keyword rules.</span></div></div>` : ""}
      ${sel.length ? `<div class="packs">${state.packs.map(packCard).join("")}</div>` : '<div class="muted">Select calls first.</div>'}
      <label class="row small muted" style="margin-top:12px"><input type="checkbox" id="only-ready" ${state.onlyReady ? "checked" : ""}/> Only run checks that can produce a result (recommended)</label>
    </div></div>

    <div class="step"><div class="step-num">3</div><div>
      <div class="step-title">What is this AI caller supposed to do?</div>
      <div class="step-sub">The profile holds the agent's goal, required disclosures, escalation rules and pass/fail targets.</div>
      <div class="profiles">${state.profiles.map((p) => `<div class="profile ${p.name === state.profile ? "on" : ""}" data-profile="${esc(p.name)}">
        <div class="pt">${esc(prettyProfile(p.name))}</div><div class="pd">${esc(p.description || p.agent?.role || "")}</div></div>`).join("")}</div>
      <label class="field" style="margin-top:14px;max-width:420px">Name this evaluation (optional)<input type="text" id="run-name" placeholder="e.g. Outbound v2 — week 42"/></label>
    </div></div>

    <div class="runbar">
      <div class="grow"><b>${chosen.length} checks × ${sel.length} call${sel.length === 1 ? "" : "s"}</b>
        <div class="small muted">${locked.length && state.onlyReady ? `${locked.length} check${locked.length > 1 ? "s" : ""} in the chosen areas will be skipped (missing inputs).` : ""} Profile: ${esc(prettyProfile(prof?.name || ""))}</div></div>
      <button class="primary lg" id="run-go" ${chosen.length && sel.length ? "" : "disabled"}>Run evaluation</button>
    </div>`;
  app.onclick = async (e) => {
    const tr = e.target.closest("[data-toggle-rec]");
    if (tr) { const id = tr.dataset.toggleRec; state.selected.has(id) ? state.selected.delete(id) : state.selected.add(id); return renderEvaluate(); }
    const exp = e.target.closest("[data-expand]");
    if (exp) { e.stopPropagation(); exp.closest(".pack").querySelector(".pack-metrics").classList.toggle("hidden"); exp.textContent = exp.textContent.includes("Show") ? "Hide checks" : "Show checks"; return; }
    const pm = e.target.closest("[data-pm]");
    if (pm) { e.stopPropagation(); const id = pm.dataset.pm; state.metricOff.has(id) ? state.metricOff.delete(id) : state.metricOff.add(id); return keepOpen(drawEvaluate); }
    const pk = e.target.closest("[data-pack]");
    if (pk && !e.target.closest(".pack-metrics")) { const id = pk.dataset.pack; state.packOn.has(id) ? state.packOn.delete(id) : state.packOn.add(id); return keepOpen(drawEvaluate); }
    const pr = e.target.closest("[data-profile]");
    if (pr) { state.profile = pr.dataset.profile; return keepOpen(drawEvaluate); }
    if (e.target.id === "only-ready") { state.onlyReady = e.target.checked; return keepOpen(drawEvaluate); }
    if (e.target.id === "run-go") {
      e.target.disabled = true; e.target.textContent = "Starting…";
      try {
        const run = await post("/api/runs", { recording_ids: [...state.selected], metric_ids: chosenMetrics(), profile: state.profile, name: $("#run-name").value });
        location.hash = `#/report/${run.id}`;
      } catch (err) { toast(err.message, true); drawEvaluate(); }
    }
  };
}
function keepOpen(fn) {
  const open = $$(".pack-metrics:not(.hidden)").map((el) => el.closest(".pack").dataset.pack);
  const y = window.scrollY; fn(); window.scrollTo(0, y);
  open.forEach((id) => { const p = $(`.pack[data-pack="${id}"]`); if (p) { $(".pack-metrics", p).classList.remove("hidden"); $("[data-expand]", p).textContent = "Hide checks"; } });
}
const prettyProfile = (n) => n.replace(/_/g, " ").replace(/^\w/, (c) => c.toUpperCase());
function packCard(p) {
  const on = state.packOn.has(p.id);
  const n = p.metrics.length, ready = p.metrics.filter((id) => rd(id).status === "ready").length, partial = p.metrics.filter((id) => rd(id).status === "partial").length;
  const reasons = {};
  p.metrics.filter((id) => !runnable(id)).forEach((id) => { const k = STATUS_TXT[rd(id).status]?.[1] || rd(id).status; reasons[k] = (reasons[k] || 0) + 1; });
  return `<div class="pack ${on ? "on" : ""} ${ready + partial ? "" : "disabled"}" data-pack="${p.id}">
    <div class="pack-top"><div class="pack-icon">${p.icon}</div>
      <div><div class="pack-title">${esc(p.label)}</div><div class="pack-desc">${esc(p.description)}</div></div>
      <input type="checkbox" class="pack-check" ${on ? "checked" : ""} tabindex="-1"/></div>
    <div class="readybar"><div class="r" style="width:${(ready / n) * 100}%"></div><div class="p" style="width:${(partial / n) * 100}%"></div></div>
    <div class="pack-foot"><span><b style="color:var(--text)">${ready + partial} of ${n}</b> checks can run${Object.keys(reasons).length ? " · " + Object.entries(reasons).map(([k, v]) => `${v} ${k.toLowerCase()}`).join(", ") : ""}</span>
      <a href="javascript:void 0" data-expand>Show checks</a></div>
    <div class="pack-metrics hidden">${p.metrics.map((id) => {
      const m = M(id), r = rd(id), [cls, txt] = STATUS_TXT[r.status] || ["neutral", r.status];
      const willRun = on && !state.metricOff.has(id) && (!state.onlyReady || runnable(id));
      return `<div class="pm" data-pm="${id}" title="${esc(m.question)}"><input type="checkbox" ${willRun ? "checked" : ""} ${state.onlyReady && !runnable(id) ? "disabled" : ""}/>
        <span>${esc(m.name)}</span><span class="pill ${cls}">${txt}${r.status === "partial" ? ` (${r.calls})` : ""}</span>
        ${r.detail && !runnable(id) ? `<span class="why">${esc(r.detail)}</span>` : ""}</div>`;
    }).join("")}</div>
  </div>`;
}

// ================================================================= EVALUATIONS
async function renderEvaluations() {
  const runs = await api("/api/runs");
  app.innerHTML = `
    <div class="page-head"><div><h1>Evaluations</h1><p>Every evaluation you've run, newest first.</p></div>
      <a class="btn primary" href="#/calls">New evaluation</a></div>
    <div class="panel">${runs.length ? runs.map((r) => {
      const s = Object.values(r.summary || {});
      const done = (r.item_counts.done || 0) + (r.item_counts.failed || 0);
      const fin = ["done", "failed"].includes(r.status);
      return `<div class="eval-row" data-run="${r.id}">
        <div><b>${esc(r.name)}</b><div class="small muted">${prettyProfile(r.profile)} · ${ago(r.created_at)}</div></div>
        <span>${r.total_items} call${r.total_items === 1 ? "" : "s"} · ${r.metric_ids.length} checks</span>
        <span>${fin ? `<div class="score-mini"><span><i class="dot bad"></i>${s.filter((x) => x.band === "bad").length}</span><span><i class="dot warn"></i>${s.filter((x) => x.band === "warn").length}</span><span><i class="dot good"></i>${s.filter((x) => x.band === "good").length}</span></div>`
          : `<div class="progress"><div style="width:${(done / Math.max(1, r.total_items)) * 100}%"></div></div>`}</span>
        <span class="small ${r.status === "failed" ? "band-bad" : "muted"}">${fin ? (r.status === "done" ? "Complete" : "Failed") : `Running ${done}/${r.total_items}`}</span>
        <button class="icon-btn" data-del="${r.id}" title="Delete">🗑</button>
      </div>`;
    }).join("") : '<div class="empty"><b>No evaluations yet</b>Select calls on the Calls page and click Evaluate.</div>'}</div>`;
  app.onclick = async (e) => {
    const del = e.target.closest("[data-del]");
    if (del) { e.stopPropagation(); if (confirm("Delete this evaluation?")) { await api(`/api/runs/${del.dataset.del}`, { method: "DELETE" }); renderEvaluations(); } return; }
    const row = e.target.closest("[data-run]");
    if (row) location.hash = `#/report/${row.dataset.run}`;
  };
  if (runs.some((r) => !["done", "failed"].includes(r.status))) state.pollTimer = setTimeout(renderEvaluations, 2000);
}

// ================================================================= SETTINGS
async function renderSettings() {
  const st = await api("/api/settings/llm");
  state.llm = st;
  const sel = st.selected;
  const card = (p) => {
    const on = sel === p.name;
    const keyTxt = p.key_masked ? `Key ${p.key_masked} · ${p.key_source.startsWith("env:") ? "from .env (" + p.key_source.slice(4) + ")" : p.key_source === "settings" ? "saved here" : p.key_source}`
      : p.key_source === "profile" ? "Using local Anthropic login" : p.key_optional ? "No key (optional)" : "No key yet";
    return `<div class="panel provider ${on ? "on" : ""}" data-prov="${p.name}" style="margin-bottom:12px">
      <div class="pack-head" style="cursor:pointer" data-pick="${p.name}">
        <input type="radio" name="prov" ${on ? "checked" : ""}/>
        <div class="grow"><h2>${esc(p.label)}</h2><div class="small muted">${esc(p.model_hint)}</div></div>
        ${p.installed ? `<span class="pill ${p.key_masked || p.key_source === "profile" ? "good" : "neutral"}">${esc(keyTxt)}</span>` : `<span class="pill warn">pip install ${esc(p.pip_name)}</span>`}
      </div>
      <div class="pad ${on ? "" : "hidden"} prov-body">
        <div class="upload-opts" style="margin-top:0">
          <label class="field">API key ${p.docs_url ? `<a href="${esc(p.docs_url)}" target="_blank" style="font-weight:400">get a key ↗</a>` : ""}
            <input type="password" data-f="api_key" autocomplete="off" placeholder="${p.key_masked ? "Leave blank to keep " + esc(p.key_masked) : p.key_optional ? "Optional" : "Paste your key"}"/></label>
          <label class="field">Model
            <input type="text" data-f="model" list="models-${p.name}" value="${esc(p.model)}" placeholder="${esc(p.default_model || "model id")}"/>
            <datalist id="models-${p.name}"></datalist></label>
          ${p.needs_base_url ? `<label class="field">Base URL<input type="text" data-f="base_url" value="${esc(p.base_url)}" placeholder="https://openrouter.ai/api/v1  ·  http://localhost:11434/v1"/></label>`
            : p.supports_effort ? `<label class="field">Reasoning effort<select data-f="effort"><option value="">Default</option>${p.effort_levels.map((e) => `<option ${p.effort === e ? "selected" : ""}>${e}</option>`).join("")}</select></label>` : "<div></div>"}
        </div>
        <div class="row" style="margin-top:14px">
          <button class="primary" data-save="${p.name}">Save & use ${esc(p.label)}</button>
          <button data-test="${p.name}">Test connection</button>
          <button class="ghost" data-models="${p.name}">Load models</button>
          ${p.key_source === "settings" ? `<button class="ghost danger" data-forget="${p.name}">Remove saved key</button>` : ""}
          <span class="small muted" data-msg="${p.name}"></span>
        </div>
      </div></div>`;
  };
  app.innerHTML = `
    <div class="page-head"><div><h1>Settings</h1><p>Choose which LLM judges conversation quality, compliance and safety. Any provider works — results use the same rubrics and structured output.</p></div></div>
    <div class="notice ${st.status.available ? "info" : ""}" style="margin-bottom:18px"><span>${st.status.available ? "✓" : "○"}</span><div>
      <b>${st.status.available ? `Using ${esc(st.status.label)} · ${esc(st.status.model)}` : "No LLM connected"}</b>
      <span class="muted">${st.status.available ? `Key source: ${esc(st.status.key_source || "—")}. Change it below; new evaluations use it immediately.` : esc(st.status.reason)}</span></div></div>
    <h3 style="margin-bottom:10px">LLM provider</h3>
    <div class="panel" style="margin-bottom:12px"><div class="pack-head" style="cursor:pointer;border:none" data-pick="auto">
      <input type="radio" name="prov" ${sel === "auto" || !sel ? "checked" : ""}/><div class="grow"><h2>Automatic</h2>
      <div class="small muted">Use the first provider that has a key (Anthropic → OpenAI → Gemini → compatible)</div></div></div></div>
    ${st.providers.map(card).join("")}
    <div class="panel"><div class="pack-head" style="cursor:pointer;border:none" data-pick="none">
      <input type="radio" name="prov" ${sel === "none" ? "checked" : ""}/><div class="grow"><h2>Off</h2>
      <div class="small muted">Skip LLM-judged checks; audio and rule-based checks still run</div></div></div></div>
    <p class="small muted" style="margin-top:16px">Keys saved here are stored on this machine in <span class="mono">data/llm_settings.json</span> (readable only by you). Keys can also go in the <span class="mono">.env</span> file in the project folder.</p>`;

  const formOf = (name) => {
    const el = $(`[data-prov="${name}"]`), v = {};
    $$("[data-f]", el).forEach((i) => (v[i.dataset.f] = i.value));
    return v;
  };
  const msg = (name, text, bad = false) => { const m = $(`[data-msg="${name}"]`); m.textContent = text; m.style.color = bad ? "var(--bad)" : ""; };
  app.onclick = async (e) => {
    const pick = e.target.closest("[data-pick]");
    if (pick) {
      const name = pick.dataset.pick;
      if (["auto", "none"].includes(name)) { await saveLLM({ selected: name }); return renderSettings(); }
      $$(".prov-body").forEach((b) => b.classList.add("hidden"));
      $(".prov-body", $(`[data-prov="${name}"]`)).classList.remove("hidden");
      $("input[type=radio]", pick).checked = true;
      return;
    }
    const save = e.target.closest("[data-save]");
    if (save) {
      const name = save.dataset.save, f = formOf(name);
      const body = { selected: name, providers: { [name]: { model: f.model, base_url: f.base_url, effort: f.effort } } };
      if (f.api_key) body.providers[name].api_key = f.api_key;
      await saveLLM(body); toast("Saved — new evaluations will use this provider"); return renderSettings();
    }
    const forget = e.target.closest("[data-forget]");
    if (forget) { await saveLLM({ providers: { [forget.dataset.forget]: { api_key: "" } } }); return renderSettings(); }
    const test = e.target.closest("[data-test]");
    if (test) {
      const name = test.dataset.test, f = formOf(name);
      msg(name, "Testing…");
      const r = await post("/api/settings/llm/test", { provider: name, api_key: f.api_key || null, model: f.model || null, base_url: f.base_url || null }).catch((err) => ({ ok: false, error: err.message }));
      return msg(name, r.ok ? `✓ Connected — ${r.model} replied` : `✗ ${r.error}`, !r.ok);
    }
    const models = e.target.closest("[data-models]");
    if (models) {
      const name = models.dataset.models, f = formOf(name);
      msg(name, "Loading models…");
      const r = await post("/api/settings/llm/models", { provider: name, api_key: f.api_key || null, base_url: f.base_url || null }).catch((err) => ({ ok: false, error: err.message }));
      if (!r.ok) return msg(name, `✗ ${r.error}`, true);
      $(`#models-${name}`).innerHTML = r.models.map((m) => `<option value="${esc(m)}">`).join("");
      return msg(name, `${r.models.length} models available — click the Model box to choose`);
    }
  };
}
async function saveLLM(body) {
  await api("/api/settings/llm", { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
  state.health = await api("/api/health"); renderStatus();
}

// ================================================================= REPORT
const LOCK_GROUPS = {
  needs_llm: ["🔒 Needs an LLM", "Connect Anthropic, OpenAI, Gemini or any OpenAI-compatible model in Settings (or .env), then re-run. These checks read the transcript and judge goals, accuracy, compliance and safety."],
  needs_transcript: ["🔒 Needs a transcript", "Install local transcription (pip install faster-whisper) or upload a transcript JSON with the call."],
  needs_data: ["📎 Needs extra data from your voice platform", "Attach it to each call (Open call → Settings → Metadata). See the README for the exact format."],
  needs_more_calls: ["➕ Needs more or differently tagged calls", "These compare groups of calls (e.g. clean vs. noisy audio, different accents)."],
  needs_config: ["⚙ Needs profile configuration", "Add the missing policy or gates to the evaluation profile."],
  error: ["⚠ Errors", "Something went wrong computing these checks."],
  not_selected: ["○ Available but not selected", "These can run on these calls — tick them in a new evaluation."],
};
function worseFirst(m) { return (a, b) => (m.direction === "lower" ? b.value - a.value : a.value - b.value); }
async function renderReport(id) {
  const run = await api(`/api/runs/${id}`);
  state.lastRun = run;
  if (!state.recordings.length) state.recordings = await api("/api/recordings");
  const fin = ["done", "failed"].includes(run.status);
  const done = (run.item_counts.done || 0) + (run.item_counts.failed || 0);
  const S = run.summary || {};
  const ids = run.metric_ids;
  const measured = ids.filter((i) => S[i]?.status === "ok");
  const notOcc = ids.filter((i) => S[i]?.status === "not_applicable" && S[i].na_kind === "not_occurred");
  const lockedIds = ids.filter((i) => S[i] && !measured.includes(i) && !notOcc.includes(i));
  const bad = measured.filter((i) => S[i].band === "bad"), warn = measured.filter((i) => S[i].band === "warn");
  const good = measured.filter((i) => S[i].band === "good");
  const items = run.items;
  let skipped = [], rdy = null;
  if (fin) {
    rdy = await post("/api/readiness", { recording_ids: items.map((i) => i.recording_id) });
    skipped = state.metrics.map((m) => m.id).filter((m) => !ids.includes(m));
  }
  const notMeasured = lockedIds.length + skipped.length;

  app.innerHTML = `
    <div class="page-head">
      <div><div class="small muted"><a href="#/evaluations">Evaluations</a> /</div><h1>${esc(run.name)}</h1>
        <p>${items.length} call${items.length === 1 ? "" : "s"}: ${items.map((i) => `<a href="javascript:void 0" data-call="${i.id}">${esc(i.recording_name)}</a>`).join(", ")} · Profile: ${esc(prettyProfile(run.profile))} · ${ago(run.created_at)}</p></div>
      <div class="row"><a class="btn" href="/api/runs/${id}/export.csv">Export CSV</a><button id="rerun">Re-run</button></div>
    </div>
    ${!fin ? `<div class="panel pad"><div class="row"><b>Evaluating… ${done} of ${run.total_items} calls done</b><span class="grow"></span><span class="muted small">Transcribing and analysing — about 1 minute per call</span></div>
      <div class="progress" style="margin-top:10px"><div style="width:${(done / Math.max(1, run.total_items)) * 100}%"></div></div></div>` : `
    <div class="scoreboard">
      <div class="score bad"><div class="n">${bad.length}</div><div class="l">Issues</div></div>
      <div class="score warn"><div class="n">${warn.length}</div><div class="l">To watch</div></div>
      <div class="score good"><div class="n">${good.length}</div><div class="l">On target</div></div>
      <div class="score neutral"><div class="n">${notMeasured}</div><div class="l">Not measured ${notMeasured ? '<a href="javascript:void 0" id="why-nm" class="small">why?</a>' : ""}</div></div>
    </div>
    ${items.some((i) => isMono(state.recordings.find((r) => r.id === i.recording_id))) ? `<div class="notice info section" style="margin-top:14px"><span>ℹ</span><div><b>Some calls are mono recordings</b><span class="muted">Speakers were separated automatically. Talk-over and interruption timing is approximate; use dual-channel recordings for exact numbers.</span></div></div>` : ""}

    <div class="section"><div class="section-head"><h3>Key findings</h3></div>
      <div class="panel">${bad.length + warn.length ? [...bad, ...warn].map((i) => findingRow(i, S[i], items)).join("") : '<div class="empty"><b>No issues found</b>Everything that could be measured is on target.</div>'}</div></div>

    ${state.packs.map((p) => packSection(p, ids.filter((i) => p.metrics.includes(i) && (measured.includes(i) || notOcc.includes(i))), S, items)).join("")}

    ${notMeasured ? `<div class="section" id="not-measured"><div class="section-head"><h3>Not measured — and how to unlock</h3>
        <span class="muted small">${skipped.length ? `${skipped.length} checks weren't included in this evaluation; ` : ""}here's what each needs.</span></div>
      <div class="panel">${lockedSection([...lockedIds, ...skipped], S, rdy)}</div></div>` : ""}`}`;

  app.onclick = async (e) => {
    const c = e.target.closest("[data-call]");
    if (c) return openCallFromRun(run, c.dataset.call, c.dataset.metric);
    if (e.target.id === "why-nm") return $("#not-measured").scrollIntoView({ behavior: "smooth" });
    if (e.target.id === "rerun") {
      const r2 = await post("/api/runs", { recording_ids: items.map((i) => i.recording_id), metric_ids: ids, profile: run.profile, name: run.name + " (re-run)", options: run.options });
      location.hash = `#/report/${r2.id}`;
    }
  };
  if (!fin) state.pollTimer = setTimeout(() => location.hash.startsWith(`#/report/${id}`) && renderReport(id), 2000);
}
function findingRow(id, r, items) {
  const m = M(id);
  const per = items.map((it) => ({ it, r: it.results?.[id] })).filter((x) => x.r?.status === "ok");
  const flagged = per.filter((x) => x.r.band === "bad" || x.r.band === "warn");
  const worst = per.slice().sort((a, b) => worseFirst(m)(a.r, b.r))[0];
  const evs = worst?.r.evidence || [];
  const ev = evs.find((e) => /missing|not |✗|did not|no /i.test(e.note)) || evs[0];
  return `<div class="finding"><div class="ic ${r.band}">${r.band === "bad" ? "!" : "~"}</div>
    <div><div class="ft">${esc(m.name)}: ${fmt(r)}${m.target ? ` <span class="muted" style="font-weight:400">· target ${esc(m.target)}</span>` : ""}</div>
      <div class="fd">${esc(m.question)} ${per.length > 1 ? `Flagged in ${flagged.length} of ${per.length} calls.` : ""}</div>
      ${ev ? `<div class="fe">e.g. <a href="javascript:void 0" data-call="${worst.it.id}" data-metric="${id}">${esc(worst.it.recording_name)}${ev.start != null ? ` at ${mmss(ev.start)}` : ""}</a> — ${esc(ev.note)}</div>` : ""}</div>
    <div><button class="small" data-call="${(worst || per[0])?.it.id}" data-metric="${id}">Inspect</button></div></div>`;
}
function packSection(p, ids, S, items) {
  if (!ids.length) return "";
  const order = { bad: 0, warn: 1, good: 2 };
  ids.sort((a, b) => (S[a].status !== "ok") - (S[b].status !== "ok") || (order[S[a].band] ?? 3) - (order[S[b].band] ?? 3));
  return `<div class="section"><div class="panel">
    <div class="pack-head"><div class="pack-icon">${p.icon}</div><h2>${esc(p.label)}</h2></div>
    <table class="mtable">${ids.map((id) => {
      const m = M(id), r = S[id];
      if (r.status !== "ok") {
        return `<tr class="quiet"><td><div class="mn">${esc(m.name)}</div><div class="mq">${esc(m.question)}</div></td>
          <td class="val" colspan="3">Didn't occur <div class="mq">${esc(r.reason || "")}</div></td></tr>`;
      }
      return `<tr class="click" data-call="${items.find((it) => it.results?.[id]?.status === "ok")?.id}" data-metric="${id}">
        <td><div class="mn">${esc(m.name)}</div><div class="mq">${esc(m.question)}</div></td>
        <td><div class="val ${r.band || ""}">${fmt(r)}</div><div class="tgt" style="text-align:right">${m.target ? "target " + esc(m.target) : ""}</div></td>
        <td style="width:110px;text-align:center">${bandPill(r.band)}</td>
        <td style="width:260px">${items.length > 1 ? `<div class="percall">${items.map((it) => {
          const x = it.results?.[id];
          return `<span class="pc ${x?.status === "ok" ? x.band || "" : ""}" data-call="${it.id}" data-metric="${id}" title="${esc(it.recording_name)}${x?.reason ? " — " + esc(x.reason) : ""}">${esc(short(it.recording_name))}: ${x?.status === "ok" ? fmt(x) : "–"}</span>`;
        }).join("")}</div>` : `<div class="tgt" style="text-align:right">${esc(r.method === "proxy" ? "automated estimate" : r.method === "llm_judge" ? "LLM judge" : r.method || "")}</div>`}</td></tr>`;
    }).join("")}</table></div></div>`;
}
const short = (n) => (n.length > 14 ? n.slice(0, 13) + "…" : n);
function lockedSection(ids, S, rdy) {
  const groups = {};
  const kindOf = (id) => {
    if (S[id]) return S[id].status === "error" ? "error" : S[id].na_kind || "needs_data";
    const st = rdy?.metrics[id]?.status;
    return ["ready", "partial"].includes(st) ? "not_selected" : st || "needs_data";
  };
  const why = (id) => S[id]?.reason || rdy?.metrics[id]?.detail || M(id).question;
  ids.forEach((id) => (groups[kindOf(id)] ||= []).push(id));
  return Object.entries(groups).map(([k, list]) => {
    const [title, how] = LOCK_GROUPS[k] || [k, ""];
    return `<div class="locked-group"><div class="lg-title">${title} <span class="pill neutral">${list.length}</span></div>
      <div class="lg-how">${esc(how)}</div>
      <div class="kv">${list.map((id) => `<dt>${esc(M(id).name)}</dt><dd class="small muted">${esc(k === "needs_llm" ? M(id).question : why(id))}</dd>`).join("")}</div></div>`;
  }).join("");
}

// ================================================================= CALL VIEW
function openDrawer(html) { $("#drawer-body").innerHTML = html; $("#drawer").classList.remove("hidden"); $(".drawer-panel").scrollTop = 0; }
function closeDrawer() { const a = $("#drawer audio"); a && a.pause(); $("#drawer").classList.add("hidden"); $("#drawer-body").innerHTML = ""; }

function openCallFromRun(run, itemId, focusMetric) {
  const it = run.items.find((i) => i.id === itemId);
  if (!it) return;
  const rec = state.recordings.find((r) => r.id === it.recording_id) || { id: it.recording_id, name: it.recording_name, meta: {} };
  const a = it.analysis || {};
  const res = it.results || {};
  const ids = run.metric_ids;
  const flagged = ids.filter((i) => res[i]?.status === "ok" && ["bad", "warn"].includes(res[i].band)).sort((x, y) => (res[x].band === "bad" ? 0 : 1) - (res[y].band === "bad" ? 0 : 1));
  const rest = ids.filter((i) => !flagged.includes(i) && res[i]?.status === "ok");
  const notOk = ids.filter((i) => res[i] && res[i].status !== "ok");
  openDrawer(`
    ${callHeader(rec, `${a.transcript_source ? `Transcript: ${a.transcript_source === "faster_whisper" ? "automatic (Whisper)" : a.transcript_source}` : ""}`)}
    ${it.error ? `<div class="notice">${esc(it.error)}</div>` : ""}
    ${a.timeline ? timelineBlock(a.timeline, rec.id) : ""}
    <div class="callgrid">
      <div class="panel">
        ${flagged.length ? `<div class="pack-head"><h2>Needs attention</h2><span class="pill bad">${flagged.length}</span></div>${flagged.map((i) => resultBlock(i, res[i], i === focusMetric)).join("")}` : ""}
        <div class="pack-head"><h2>Other results</h2></div>${rest.map((i) => resultBlock(i, res[i], i === focusMetric)).join("") || '<div class="pad muted">None</div>'}
        ${notOk.length ? `<details><summary class="pad muted small" style="display:block">${notOk.length} checks not measured for this call</summary>${notOk.map((i) => resultBlock(i, res[i], false)).join("")}</details>` : ""}
      </div>
      <div class="panel"><div class="pack-head"><h2>Transcript</h2></div>${a.turns ? turnsBlock(a.turns) : '<div class="pad muted">No transcript for this evaluation.</div>'}</div>
    </div>`);
  const body = $("#drawer-body");
  if (a.timeline) bindTimeline(a.timeline, body);
  bindResults(body);
  if (focusMetric) { const el = body.querySelector(`[data-result="${focusMetric}"]`); el?.scrollIntoView({ block: "center" }); }
}
function callHeader(rec, extra = "") {
  return `<div class="page-head" style="margin-bottom:14px"><div><h1>${esc(rec.name)}</h1>
    <p>${rec.duration_s ? mmss(rec.duration_s) + " · " : ""}${isMono(rec) ? '<span class="pill warn">Mono · auto-split</span>' : '<span class="pill good">Dual-channel</span>'} ${esc(extra)}</p></div></div>`;
}
function resultBlock(id, r, open) {
  const m = M(id);
  const val = r.status === "ok" ? `<span class="val ${r.band || ""}" style="font-weight:700">${fmt(r)}</span>`
    : `<span class="pill ${r.na_kind === "not_occurred" ? "neutral" : "locked"}">${r.na_kind === "not_occurred" ? "Didn't occur" : r.status === "error" ? "Error" : "Not measured"}</span>`;
  return `<div class="result" data-result="${id}">
    <div class="result-head"><div><div class="rn">${esc(m.name)}</div><div class="rq">${esc(m.question)}</div></div><div class="row">${val}${r.status === "ok" ? bandPill(r.band) : ""}</div></div>
    <div class="result-body ${open ? "" : "hidden"}">
      ${m.target ? `<div class="small muted">Target ${esc(m.target)} · ${esc(m.formula)}</div>` : `<div class="small muted">${esc(m.formula)}</div>`}
      ${r.reason ? `<div style="margin-top:6px">${esc(r.reason)}</div>` : ""}
      ${(r.evidence || []).length ? `<div style="margin-top:8px">${r.evidence.map((ev) => `<div class="evidence" data-seek="${ev.start ?? 0}" data-end="${ev.end ?? ev.start ?? 0}">
          <b class="mono">${ev.start != null ? mmss(ev.start) : ""}</b> ${esc(ev.note)}</div>`).join("")}</div>` : ""}
      ${r.details && Object.keys(r.details).length ? `<details style="margin-top:8px"><summary class="small muted">Technical details</summary><pre class="details">${esc(JSON.stringify(r.details, null, 1))}</pre></details>` : ""}
    </div></div>`;
}
function bindResults(root) {
  root.addEventListener("click", (e) => { const h = e.target.closest(".result-head"); if (h) h.nextElementSibling.classList.toggle("hidden"); });
}

async function openCall(recId) {
  const rec = state.recordings.find((r) => r.id === recId);
  openDrawer(`${callHeader(rec)}<div class="panel pad muted">Analysing channels…</div>`);
  let preview = null;
  try { preview = await api(`/api/recordings/${recId}/preview`); } catch (e) { toast(e.message, true); }
  const le = rec.last_eval;
  openDrawer(`
    ${callHeader(rec)}
    <div class="row" style="margin:-4px 0 14px">${le ? `<a class="btn primary" href="#/report/${le.run_id}/call/${le.item_id}">View latest evaluation</a>` : ""}
      <button id="eval-one">Evaluate this call</button><span class="grow"></span><button class="danger small" id="rec-del">Delete call</button></div>
    ${preview ? timelineBlock(preview.timeline, rec.id) : ""}
    <div class="callgrid">
      <div class="panel"><div class="pack-head"><h2>Transcript</h2></div>${preview?.turns ? turnsBlock(preview.turns) : '<div class="pad muted">A transcript is created when the call is evaluated.</div>'}</div>
      <div class="panel"><div class="pack-head"><h2>Settings</h2></div><div class="pad">
        <label class="field">Name<input type="text" id="ed-name" value="${esc(rec.name)}"/></label>
        <label class="field" style="margin-top:12px">Caller (human) is on
          <select id="ed-hc"><option value="left" ${rec.human_channel === 0 ? "selected" : ""}>Left channel${isMono(rec) ? " (auto-detected)" : ""}</option><option value="right" ${rec.human_channel === 1 ? "selected" : ""}>Right channel${isMono(rec) ? " (swap speakers)" : ""}</option></select></label>
        <label class="field" style="margin-top:12px">Tags (JSON)<textarea id="ed-tags">${esc(JSON.stringify(rec.tags || {}, null, 1))}</textarea></label>
        <label class="field" style="margin-top:12px">Metadata (JSON) — event logs, ratings, cost, survey…<textarea id="ed-meta" style="min-height:180px">${esc(JSON.stringify(rec.meta || {}, null, 1))}</textarea></label>
        <button class="primary" id="ed-save" style="margin-top:12px">Save changes</button>
      </div></div>
    </div>`);
  const body = $("#drawer-body");
  if (preview) bindTimeline(preview.timeline, body);
  $("#eval-one").addEventListener("click", () => { state.selected = new Set([recId]); closeDrawer(); location.hash = "#/evaluate"; });
  $("#ed-save").addEventListener("click", async () => {
    try {
      await api(`/api/recordings/${recId}`, { method: "PATCH", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name: $("#ed-name").value, human_channel: $("#ed-hc").value, tags: JSON.parse($("#ed-tags").value || "{}"), meta: JSON.parse($("#ed-meta").value || "{}") }) });
      toast("Saved"); closeDrawer(); renderCalls();
    } catch (e) { toast(e.message, true); }
  });
  $("#rec-del").addEventListener("click", async () => {
    if (!confirm(`Delete "${rec.name}"? Its evaluations will be removed too.`)) return;
    await api(`/api/recordings/${recId}`, { method: "DELETE" });
    state.selected.delete(recId); closeDrawer(); renderCalls();
  });
}

// ---------------------------------------------------------------- timeline + transcript
function timelineBlock(tl, recId) {
  return `<div class="timeline-box">
    <svg class="timeline-svg" viewBox="0 0 1000 118" preserveAspectRatio="none"></svg>
    <div class="legend">
      <span><i style="background:var(--human)"></i>Caller</span><span><i style="background:var(--ai)"></i>AI</span>
      <span><i style="background:var(--overlap)"></i>Talking over</span><span><i style="background:var(--warn)"></i>Caller interrupts</span>
      <span><i style="background:var(--neutral-bg);border:1px solid var(--border-strong)"></i>Silence</span>
      <span class="grow"></span><span>Click anywhere to play from there</span>
    </div>
    <audio controls preload="metadata" src="/api/recordings/${recId}/audio"></audio>
  </div>`;
}
function bindTimeline(tl, root) {
  const svg = $(".timeline-svg", root), audio = $("audio", root);
  const D = tl.duration || 1, X = (t) => (t / D) * 1000, lane = { human: 16, ai: 64 };
  let h = `<text x="4" y="11" font-size="10" fill="var(--muted)">CALLER</text><text x="4" y="59" font-size="10" fill="var(--muted)">AI</text>`;
  tl.dead_air.forEach((d) => (h += `<rect x="${X(d.start)}" y="14" width="${X(d.end) - X(d.start)}" height="86" fill="var(--neutral-bg)"/>`));
  for (const spk of ["human", "ai"]) tl.segments[spk].forEach((s) => (h += `<rect x="${X(s.start)}" y="${lane[spk]}" width="${Math.max(1.2, X(s.end) - X(s.start))}" height="34" rx="2" fill="var(--${spk})" opacity="${s.backchannel ? 0.35 : 0.9}"/>`));
  tl.overlaps.filter((o) => o.unwanted_s > 0).forEach((o) => (h += `<rect x="${X(o.start)}" y="12" width="${Math.max(1.5, X(o.end) - X(o.start))}" height="90" fill="var(--overlap)"/>`));
  tl.barge_ins.forEach((b) => (h += `<line x1="${X(b.onset)}" x2="${X(b.onset)}" y1="10" y2="104" stroke="var(--warn)" stroke-width="2"/>`));
  h += `<rect class="hl" x="0" y="10" width="0" height="94" fill="var(--accent)" opacity=".2"/><line class="ph" x1="0" x2="0" y1="6" y2="108" stroke="var(--text)" stroke-width="1.5"/>`;
  const step = D > 240 ? 60 : D > 90 ? 30 : 10;
  for (let t = 0; t < D; t += step) h += `<text x="${X(t) + 2}" y="116" font-size="9" fill="var(--muted)">${mmss(t)}</text>`;
  svg.innerHTML = h;
  const ph = $(".ph", svg);
  audio.addEventListener("timeupdate", () => {
    const x = X(audio.currentTime); ph.setAttribute("x1", x); ph.setAttribute("x2", x);
    $$(".turn", root).forEach((el) => el.classList.toggle("active", audio.currentTime >= +el.dataset.start && audio.currentTime <= +el.dataset.end));
  });
  svg.addEventListener("click", (e) => { const r = svg.getBoundingClientRect(); audio.currentTime = ((e.clientX - r.left) / r.width) * D; audio.play(); });
  root.addEventListener("click", (e) => {
    const el = e.target.closest("[data-seek]"); if (!el) return;
    const s = +el.dataset.seek, en = +(el.dataset.end || s), hl = $(".hl", svg);
    hl.setAttribute("x", X(s)); hl.setAttribute("width", Math.max(4, X(en) - X(s)));
    audio.currentTime = Math.max(0, s - 0.3); audio.play();
    svg.scrollIntoView({ block: "nearest", behavior: "smooth" });
  });
}
function turnsBlock(turns) {
  return `<div class="turns">${turns.map((t) => `<div class="turn ${t.speaker}" data-seek="${t.start}" data-end="${t.end}" data-start="${t.start}">
    <span class="spk">${t.speaker === "ai" ? "AI" : "Caller"}<span class="t">${mmss(t.start)}</span></span><span>${esc(t.text)}</span></div>`).join("")}</div>`;
}

boot().catch((e) => { app.innerHTML = `<div class="panel pad">Failed to load: ${esc(e.message)}</div>`; });
