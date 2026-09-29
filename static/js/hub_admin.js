// Hub admin page (2A1 T5): the folder-loader job and the export actions.
// Every call is a JSON POST carrying the admin password. The DOM is built with
// textContent only: file names and paths come from the server.
(function () {
  "use strict";
  const $ = (id) => document.getElementById(id);
  let pollTimer = null;

  function el(tag, text, cls) {
    const e = document.createElement(tag);
    if (text !== undefined && text !== null) e.textContent = String(text);
    if (cls) e.className = cls;
    return e;
  }

  function say(text, cls) {
    const m = $("msg");
    m.textContent = text || "";
    m.className = cls || "";
  }

  async function call(path, body) {
    const r = await fetch(path, {
      method: "POST", headers: {"Content-Type": "application/json"},
      body: JSON.stringify(Object.assign({password: $("pw").value}, body || {})),
    });
    let j = null;
    try { j = await r.json(); } catch (e) { j = {}; }
    if (!r.ok) throw new Error((j && j.error) || ("HTTP " + r.status));
    return j;
  }

  function jobStateClass(state) {
    if (state === "failed") return "err";
    if (state === "done") return "ok";
    if (state === "stopped") return "warn";
    return "";
  }

  function renderJob(job) {
    const box = $("job");
    box.textContent = "";
    if (!job) return;
    const p = job.params || {};
    const prog = job.progress || {};
    const source = p.folder || p.processed_dir || "";
    box.appendChild(el("p", `Job ${job.id}: ${job.kind} ${p.instrument || ""} from ${source}` +
      (p.results_csv ? ` (csv ${p.results_csv})` : "") +
      ` — ${job.state}` + (prog.phase ? ` (${prog.phase} ${prog.done || 0}/${prog.total || 0})` : ""),
      jobStateClass(job.state)));
    const counts = Object.entries(job.counts || {}).map(([k, v]) => `${k}: ${v}`).join(", ");
    if (counts) box.appendChild(el("p", counts));
    if (job.error) box.appendChild(el("p", job.error, "err"));
    if (job.summary) box.appendChild(el("pre", JSON.stringify(job.summary, null, 1)));
    else if ((job.recent || []).length) {
      box.appendChild(el("pre", job.recent.map(r => `${r.outcome}  ${r.file}` +
        (r.message ? `  (${r.message})` : "")).join("\n")));
    }
  }

  async function pollJob() {
    clearTimeout(pollTimer);
    try {
      const j = await call("/api/admin/jobs/status");
      renderJob(j.job);
      if (j.job && j.job.state === "running") pollTimer = setTimeout(pollJob, 1500);
    } catch (e) { say(e.message, "err"); }
  }

  async function exportAction(inst, action) {
    const body = {};
    if (action === "new-path" || action === "write-fresh") {
      const path = window.prompt(action === "write-fresh"
        ? "Absolute path of the NEW .csv to write (must not exist):"
        : "Absolute path of the .csv to export to:");
      if (!path) return;
      body.path = path;
    } else if (!window.confirm(`Adopt ${inst}'s export file as it is now? The hub appends after its current content.`)) {
      return;
    }
    try {
      await call(`/api/admin/exports/${encodeURIComponent(inst)}/${action}`, body);
      say(`${action} done for ${inst}`, "ok");
    } catch (e) { say(e.message, "err"); }
    refreshExports();
  }

  function populateInstrumentSelects(names) {
    for (const sel of document.querySelectorAll(".inst-select")) {
      const chosen = sel.value;
      sel.textContent = "";
      for (const name of names) {
        const opt = el("option", name);
        opt.value = name;
        sel.appendChild(opt);
      }
      if (chosen) sel.value = chosen;
    }
  }

  async function refreshExports() {
    const tbody = $("exports");
    const j = await call("/api/admin/exports");
    tbody.textContent = "";
    populateInstrumentSelects(j.instruments.map((s) => s.instrument));
    for (const s of j.instruments) {
      const tr = document.createElement("tr");
      tr.appendChild(el("td", s.instrument));
      tr.appendChild(el("td", s.path || s.error || ""));
      tr.appendChild(el("td", s.pending === undefined ? "" : s.pending));
      const state = s.refused ? `refused: ${s.refused_detail || s.refused}`
        : (s.last_error ? `error: ${s.last_error}` : "ok");
      tr.appendChild(el("td", state, s.refused || s.last_error ? "err" : "ok"));
      const td = document.createElement("td");
      for (const [action, label, cls] of [["adopt", "Adopt", "warn"], ["new-path", "New path", ""],
                                          ["write-fresh", "Write fresh", ""]]) {
        const b = el("button", label, cls);
        b.addEventListener("click", () => exportAction(s.instrument, action));
        td.appendChild(b);
      }
      tr.appendChild(td);
      tbody.appendChild(tr);
    }
  }

  async function refresh() {
    say("");
    try {
      await refreshExports();
      await pollJob();
    } catch (e) { say(e.message, "err"); }
    await loadPresets();
  }

  async function startLoad() {
    try {
      const j = await call("/api/admin/load-folder", {
        instrument: $("lf-inst").value, folder: $("lf-folder").value.trim(),
        backfill: $("lf-backfill").checked,
      });
      renderJob(j.job);
      pollJob();
    } catch (e) { say(e.message, "err"); }
  }

  function ihAliases() {
    return $("ih-aliases").value.split(",").map((s) => s.trim()).filter(Boolean);
  }

  function ihParams() {
    return {
      instrument: $("ih-inst").value, processed_dir: $("ih-processed").value.trim(),
      results_csv: $("ih-csv").value.trim(), aliases: ihAliases(),
    };
  }

  async function ihDryRun() {
    const box = $("ih-result");
    box.textContent = "";
    try {
      const j = await call("/api/admin/import-history/dry-run", ihParams());
      box.appendChild(el("pre", JSON.stringify(j.summary, null, 1)));
      say("Dry run done (nothing written)", "ok");
    } catch (e) { say(e.message, "err"); }
  }

  async function ihStart() {
    if (!window.confirm(`Start the real history import for ${$("ih-inst").value}? ` +
        "This writes to the hub's database (every imported sample stays backfill).")) return;
    try {
      const j = await call("/api/admin/import-history/start",
        Object.assign(ihParams(), {confirm: true}));
      renderJob(j.job);
      pollJob();
    } catch (e) { say(e.message, "err"); }
  }

  async function ihStop() {
    try {
      const j = await call("/api/admin/jobs/stop");
      renderJob(j.job);
      say("Stop requested; the run ends after the batch in progress commits", "ok");
    } catch (e) { say(e.message, "err"); }
  }

  async function ihLastRun() {
    const box = $("ih-last");
    box.textContent = "";
    try {
      const j = await call("/api/admin/import-history/last-run", {instrument: $("ih-inst").value});
      const r = j.last_run;
      if (!r) {
        box.appendChild(el("p", "No import run yet for this instrument.", "muted"));
        return;
      }
      $("ih-processed").value = r.processed_dir || "";
      $("ih-csv").value = r.results_csv || "";
      $("ih-aliases").value = (r.aliases || []).join(", ");
      box.appendChild(el("p",
        `Last run ${r.started_at}${r.stopped ? " (stopped: " + r.stopped + ")" : ""}: ` +
        JSON.stringify(r.counts || {}), "muted"));
    } catch (e) { say(e.message, "err"); }
  }

  // ── comment presets (phase 4) ─────────────────────────────────────────
  // One admin route, /api/admin/comment-presets {action, ...}; every answer
  // carries the full list, which is re-rendered (inputs are set with .value,
  // labels with textContent: preset text is data, never markup).

  let shownPresets = [];

  // Unsaved edits in the list ({id: text}), so a reorder or (de)activation of
  // another preset doesn't throw them away. A saved preset drops its own.
  function unsavedEdits() {
    const out = {};
    document.querySelectorAll("#presets li").forEach((li) => {
      const p = shownPresets.find((q) => String(q.id) === li.dataset.id);
      const v = li.querySelector("input.preset-text").value;
      if (p && v !== p.text) out[p.id] = v;
    });
    return out;
  }

  async function presetCall(action, extra) {
    const edits = unsavedEdits();
    const j = await call("/api/admin/comment-presets", Object.assign({action}, extra || {}));
    if (action === "update" && extra) delete edits[extra.id];
    renderPresets(j.presets || [], edits);
    return j;
  }

  function renderPresets(presets, edits) {
    shownPresets = presets;
    const ul = $("presets");
    ul.textContent = "";
    presets.forEach((p, i) => {
      const li = el("li", null, p.active ? "" : "inactive");
      li.dataset.id = String(p.id);
      const input = document.createElement("input");
      input.type = "text";
      input.maxLength = 200;
      input.className = "preset-text";
      input.value = (edits && edits[p.id] !== undefined) ? edits[p.id] : p.text;
      li.appendChild(input);
      const btn = (label, cls, fn, disabled) => {
        const b = el("button", label, cls);
        b.disabled = !!disabled;
        b.addEventListener("click", () => fn().catch((e) => say(e.message, "err")));
        li.appendChild(b);
      };
      btn("Save", "preset-save", async () => {
        await presetCall("update", {id: p.id, text: input.value});
        say("Preset saved", "ok");
      });
      btn("\u2191", "preset-up", () => move(presets, i, -1), i === 0);
      btn("\u2193", "preset-down", () => move(presets, i, +1), i === presets.length - 1);
      btn(p.active ? "Deactivate" : "Activate", "preset-toggle warn",
          () => presetCall(p.active ? "deactivate" : "activate", {id: p.id}));
      ul.appendChild(li);
    });
  }

  function move(presets, i, delta) {
    const ids = presets.map((p) => p.id);
    const j = i + delta;
    [ids[i], ids[j]] = [ids[j], ids[i]];
    return presetCall("reorder", {ids});
  }

  async function loadPresets() {
    try { await presetCall("list"); } catch (e) { say(e.message, "err"); }
  }

  async function addPreset() {
    const input = $("preset-new-text");
    try {
      await presetCall("create", {text: input.value});
      input.value = "";
      say("Preset added", "ok");
    } catch (e) { say(e.message, "err"); }
  }

  // ── hub address and sessions ──
  async function saveHubUrl() {
    try {
      const j = await call("/api/admin/hub-url", {hub_url: $("hub-url").value.trim()});
      $("hub-url-effective").textContent = j.effective || j.hub_url || "https://gc.asaplabs.net";
      say("Hub address saved: " + $("hub-url-effective").textContent, "ok");
    } catch (e) { say(e.message, "err"); }
  }

  function renderSessions(rows) {
    const tb = $("sessions");
    tb.textContent = "";
    for (const s of rows || []) {
      const tr = document.createElement("tr");
      for (const v of [s.name, s.method, s.ip || "", (s.last_seen || "").replace("T", " ").slice(0, 19)]) {
        const td = document.createElement("td");
        td.textContent = v;
        tr.appendChild(td);
      }
      const td = document.createElement("td");
      const one = document.createElement("button");
      one.textContent = "Revoke";
      one.className = "warn";
      one.addEventListener("click", () => sessionCall("revoke", {id: s.id}));
      const all = document.createElement("button");
      all.textContent = "Revoke all for this name";
      all.className = "warn";
      all.addEventListener("click", () => {
        if (confirm("Sign out every session of " + s.name + "?")) sessionCall("revoke-name", {name: s.name});
      });
      td.appendChild(one);
      td.appendChild(all);
      tr.appendChild(td);
      tb.appendChild(tr);
    }
    if (!rows || !rows.length) {
      const tr = document.createElement("tr");
      const td = document.createElement("td");
      td.colSpan = 5;
      td.textContent = "No active sessions.";
      tr.appendChild(td);
      tb.appendChild(tr);
    }
  }

  async function sessionCall(action, extra) {
    try {
      const j = await call("/api/admin/sessions", Object.assign({action}, extra || {}));
      renderSessions(j.sessions);
      if (action !== "list") say("Done", "ok");
    } catch (e) { say(e.message, "err"); }
  }

  $("btn-hub-url").addEventListener("click", saveHubUrl);
  $("btn-sessions").addEventListener("click", () => sessionCall("list"));
  $("btn-presets-load").addEventListener("click", loadPresets);
  $("btn-preset-add").addEventListener("click", addPreset);
  $("btn-refresh").addEventListener("click", refresh);
  $("btn-load").addEventListener("click", startLoad);
  $("btn-ih-last").addEventListener("click", ihLastRun);
  $("btn-ih-dryrun").addEventListener("click", ihDryRun);
  $("btn-ih-start").addEventListener("click", ihStart);
  $("btn-ih-stop").addEventListener("click", ihStop);
  $("pw").addEventListener("keydown", (e) => { if (e.key === "Enter") refresh(); });
})();
