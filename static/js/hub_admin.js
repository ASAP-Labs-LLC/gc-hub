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

  function renderJob(job) {
    const box = $("job");
    box.textContent = "";
    if (!job) return;
    const p = job.params || {};
    const prog = job.progress || {};
    box.appendChild(el("p", `Job ${job.id}: ${job.kind} ${p.instrument || ""} from ${p.folder || ""} ` +
      `— ${job.state}` + (prog.phase ? ` (${prog.phase} ${prog.done || 0}/${prog.total || 0})` : ""),
      job.state === "failed" ? "err" : (job.state === "done" ? "ok" : "")));
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

  async function refreshExports() {
    const tbody = $("exports");
    const j = await call("/api/admin/exports");
    tbody.textContent = "";
    const sel = $("lf-inst");
    const chosen = sel.value;
    sel.textContent = "";
    for (const s of j.instruments) {
      const opt = el("option", s.instrument);
      opt.value = s.instrument;
      sel.appendChild(opt);
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
    if (chosen) sel.value = chosen;
  }

  async function refresh() {
    say("");
    try {
      await refreshExports();
      await pollJob();
    } catch (e) { say(e.message, "err"); }
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

  $("btn-refresh").addEventListener("click", refresh);
  $("btn-load").addEventListener("click", startLoad);
  $("pw").addEventListener("keydown", (e) => { if (e.key === "Enter") refresh(); });
})();
