// Hub admin page (/admin/hub). v4.0 lane E:
// - unlock once: the password lives in admin_unlock.js's 15-minute closure,
//   then everything loads by itself (no per-card Load or Refresh buttons);
// - running work shows without the password, from the live task feed
//   (GCLive), so a job started elsewhere is picked up when the page loads;
// - each job renders in its own card (load-folder in #lf-job, the history
//   import and its dry run in #ih-job), summaries as a short table of counts;
// - messages appear next to the button that caused them; Stop is enabled only
//   while that card's job runs.
// Admin calls are JSON POSTs carrying the password, made one at a time (the
// hub checks one password attempt per client at a time). The DOM is built
// with textContent only: file names and paths come from the server.
// The pure helpers are module.exports for the Node tests
// (tests/js/hub_admin.test.js, hub_admin_view.test.js).
(function () {
  "use strict";

  // ── pure helpers ──────────────────────────────────────────────────────
  const PHASES = {
    match: "Reading CDFs and matching them to the CSV",
    check: "Checking CDFs",
    import: "Classifying samples",
    submit: "Submitting CDFs",
    identify: "Reading the CDFs",
    commit: "Committing",
  };

  // One line for a running job's progress event ({phase, done, total}).
  function progressText(job) {
    const p = (job && job.progress) || {};
    if (!p.phase) return "Starting…";
    if (p.phase === "scan") return "Scanning the folder…";
    const label = PHASES[p.phase] || p.phase;
    return (p.total !== undefined && p.total !== null)
      ? `${label}: ${p.done || 0} of ${p.total}` : `${label}…`;
  }

  function isDryRun(job) {
    return !!job && job.kind === "import-history-dry-run";
  }

  // The history dry run (an admin job since v3.0.1): what the panel shows.
  function dryRunView(job) {
    const summary = (job.result && job.result.summary) || job.summary || null;
    if (job.state === "running") {
      return {done: false, message: "Dry run running (nothing is written): " + progressText(job),
              cls: "", summary: null};
    }
    if (job.state === "done") {
      return {done: true, message: "Dry run done (nothing written)", cls: "ok", summary};
    }
    if (job.state === "stopped") {
      return {done: true, message: "Dry run stopped (nothing written); the summary so far is below",
              cls: "warn", summary};
    }
    return {done: true, message: "Dry run failed: " + (job.error || job.state), cls: "err", summary};
  }

  const CARDS = {"load-folder": "lf", "import-history": "ih", "import-history-dry-run": "ih",
                 "purge": "purge", "diagnostics-bundle": "diag"};
  const TITLES = {"load-folder": "Folder load", "import-history": "History import",
                  "import-history-dry-run": "Dry run", "purge": "Purge",
                  "diagnostics-bundle": "Diagnostics bundle"};

  // The card (lf | ih | purge | diag) a job kind renders in, or null.
  function cardFor(kind) {
    return Object.prototype.hasOwnProperty.call(CARDS, kind) ? CARDS[kind] : null;
  }

  const WORDS = {cdf: "CDF", cdfs: "CDFs", csv: "CSV", sha: "SHA"};
  const LABELS = {cdf_errors: "Unreadable CDFs", dup_sha: "Duplicate files"};

  // "no_injection_time" -> "No injection time"; "csv_rows" -> "CSV rows".
  function label(key) {
    if (LABELS[key]) return LABELS[key];
    const words = String(key).split("_").map((w) => WORDS[w] || w);
    const first = words[0] || "";
    words[0] = first === first.toUpperCase() ? first : first[0].toUpperCase() + first.slice(1);
    return words.join(" ");
  }

  const fmt = (n) => String(n).replace(/\B(?=(\d{3})+(?!\d))/g, ",");
  const SKIP = new Set(["seconds", "run_id"]);

  function took(s) {
    const n = Math.round(s);
    if (n < 60) return `${n} s`;
    return `${Math.floor(n / 60)} min` + (n % 60 ? ` ${n % 60} s` : "");
  }

  // A job summary as [[label, value]]: its non-zero counts in order (the
  // `counts` object when it has one, else its top-level numbers), then how
  // long it took. Never paths, lists or nested objects.
  function summaryRows(summary) {
    if (!summary || typeof summary !== "object") return [];
    const src = (summary.counts && typeof summary.counts === "object") ? summary.counts : summary;
    const rows = [];
    for (const [k, v] of Object.entries(src)) {
      if (SKIP.has(k) || typeof v !== "number" || !isFinite(v) || v === 0) continue;
      rows.push([label(k), fmt(v)]);
    }
    if (!rows.length) rows.push(["Nothing to report", "0"]);
    if (typeof summary.seconds === "number" && isFinite(summary.seconds)) {
      rows.push(["Took", took(summary.seconds)]);
    }
    return rows;
  }

  function summaryWarnings(summary) {
    const w = summary && summary.warnings;
    return Array.isArray(w) ? w.filter((x) => typeof x === "string") : [];
  }

  function hm(iso) {
    const d = new Date(iso || "");
    if (isNaN(d.getTime())) return null;
    return String(d.getHours()).padStart(2, "0") + ":" + String(d.getMinutes()).padStart(2, "0");
  }

  // {line, cls, running} for an admin job (from /api/admin/jobs/status): its
  // progress while running, else what happened and when, once (v4.0 lane E
  // review: no "Dry run: Finished" title-plus-state repetition).
  function jobView(job) {
    const title = TITLES[job.kind] || label(job.kind || "job");
    if (job.state === "running") {
      return {line: `${title}: ${progressText(job)}`, cls: "", running: true};
    }
    const at = hm(job.finished_at);
    const when = at ? ` at ${at}` : "";
    if (job.state === "done") {
      return {line: `${title} finished${when}` + (isDryRun(job) ? " · nothing written" : ""),
              cls: "ok", running: false};
    }
    if (job.state === "stopped") {
      return {line: `${title} stopped${when}` + (isDryRun(job) ? " · nothing written"
        : " · running it again resumes"), cls: "warn", running: false};
    }
    return {line: `${title} failed${when}: ` + (job.error || job.state), cls: "err", running: false};
  }

  // True when ``prev`` (a job this page saw running) has ended in ``now``.
  function jobEnded(prev, now) {
    if (!prev || prev.state !== "running" || !now) return false;
    return now.id !== prev.id || now.state !== "running";
  }

  function stopEnabled(card, job) {
    return !!job && job.state === "running" && cardFor(job.kind) === card;
  }

  // The most recent feed task for a card (the feed lists running first).
  function feedTaskFor(card, tasks) {
    return (tasks || []).find((t) => cardFor(t.kind) === card) || null;
  }

  const ENDED = {done: "finished", failed: "failed", stopped: "stopped", interrupted: "interrupted"};

  // A feed task in one line (no password needed to see it).
  function feedLine(task) {
    const title = TITLES[task.kind] || task.title || "Job";
    if (task.state !== "running") return task.outcome || `${title} ${ENDED[task.state] || "ended"}`;
    const p = task.progress || {};
    const bits = [`${title} running`];
    if (p.text) bits.push(p.text);
    if (typeof p.total === "number") bits.push(`${fmt(p.done || 0)} of ${fmt(p.total)}`);
    if (task.by) bits.push(`started by ${task.by}`);
    return bits.join(" · ");
  }

  const CALL_TIMEOUT_MS = 30000;     // a hung request never blocks the admin-call queue

  const pure = {progressText, isDryRun, dryRunView, cardFor, label, summaryRows, summaryWarnings,
                jobView, jobEnded, stopEnabled, feedTaskFor, feedLine, CALL_TIMEOUT_MS};
  if (typeof module !== "undefined" && module.exports) {
    module.exports = pure;
    return;
  }

  // ── the page ──────────────────────────────────────────────────────────
  const $ = (id) => document.getElementById(id);
  const unlock = window.GCAdminUnlock.page;
  let pollTimer = null;
  let lastJob = null;            // the current or last admin job (unlocked)
  let feed = [];                 // the live task feed
  let chain = Promise.resolve();

  function el(tag, text, cls) {
    const e = document.createElement(tag);
    if (text !== undefined && text !== null) e.textContent = String(text);
    if (cls) e.className = cls;
    return e;
  }

  // A message next to the control that caused it.
  function say(where, text, cls) {
    const m = $(where);
    if (!m) return;
    m.textContent = text || "";
    m.className = "msg " + (cls || "");
  }

  // One admin call at a time, with the unlocked password (or ``opts.password``,
  // the one being checked by Unlock); a 403 locks again. A request that
  // hangs is abandoned after CALL_TIMEOUT_MS, so it never blocks the queue.
  function call(path, body, opts) {
    const run = async () => {
      const password = (opts && opts.password) || unlock.get();
      if (!password) {
        const e = new Error("Unlock with the admin password first.");
        e.status = 0;
        throw e;
      }
      const ctl = typeof AbortController !== "undefined" ? new AbortController() : null;
      const timer = ctl ? setTimeout(() => ctl.abort(), CALL_TIMEOUT_MS) : null;
      let r;
      try {
        r = await fetch(path, {
          method: "POST", headers: {"Content-Type": "application/json"}, cache: "no-store",
          body: JSON.stringify(Object.assign({password}, body || {})),
          signal: ctl ? ctl.signal : undefined,
        });
      } catch (err) {
        const e = new Error(err && err.name === "AbortError"
          ? "The hub did not answer within 30 s; try again." : "Could not reach the hub.");
        e.status = 0;
        throw e;
      } finally {
        if (timer) clearTimeout(timer);
      }
      const j = (await window.GCSession.readJson(r)).body || {};
      if (!r.ok || j.error) {
        const e = new Error((j && j.error) || ("HTTP " + r.status));
        e.status = r.status;
        e.body = j;
        if (r.status === 403) unlock.forget();
        throw e;
      }
      return j;
    };
    const next = chain.then(run, run);
    chain = next.catch(() => {});
    return next;
  }
  window.GCAdminCall = call;          // diagnostics.js uses the same queue

  // ── unlock ──
  function renderLock() {
    const on = unlock.isUnlocked();
    $("unlock-form").hidden = on;
    $("unlocked").hidden = !on;
    $("locked-note").hidden = on;
    $("unlocked-left").textContent = on
      ? window.GCAdminUnlock.remainingText(unlock.remainingMs()) + " left" : "";
  }

  async function doUnlock(e) {
    if (e) e.preventDefault();
    const pw = $("pw").value;
    if (!pw) { say("unlock-msg", "Enter the admin password.", "err"); return; }
    say("unlock-msg", "Checking…");
    try {
      // checked first; only an accepted password unlocks (and is kept)
      const j = await call("/api/admin/jobs/status", null, {password: pw});
      unlock.set(pw);
      lastJob = j.job || null;
      $("pw").value = "";
      say("unlock-msg", "");
      renderLock();
      await loadAll();
    } catch (err) {
      unlock.forget();
      say("unlock-msg", err.message, "err");
      renderLock();
    }
  }

  // Everything behind the password, loaded once unlocked (one call at a time).
  async function loadAll() {
    const steps = [
      [pollJob, "ih-msg"], [refreshExports, "exports-msg"], [() => sessionCall("list"), "sessions-msg"],
      [() => presetCall("list"), "presets-msg"], [ihLastRun, "ih-msg"],
      [() => window.Diagnostics && window.Diagnostics.checkSizes && window.Diagnostics.checkSizes(),
       "diag-msg"],
    ];
    for (const [fn, where] of steps) {
      if (!unlock.isUnlocked()) return;
      try { await fn(); } catch (e) { say(where, e.message, "err"); }
    }
  }

  // ── instruments (open: no password) ──
  async function loadInstruments() {
    const r = await fetch("/api/instruments", {headers: {Accept: "application/json"}});
    const j = (await window.GCSession.readJson(r)).body || {};
    if (!r.ok || !Array.isArray(j.instruments)) return;
    for (const sel of document.querySelectorAll(".inst-select")) {
      const chosen = sel.value;
      sel.textContent = "";
      for (const inst of j.instruments) {
        const opt = el("option", inst.name && inst.name !== inst.id ? `${inst.name} (${inst.id})` : inst.id);
        opt.value = inst.id;
        sel.appendChild(opt);
      }
      if (chosen) sel.value = chosen;
    }
  }

  // ── jobs ──
  function countsTable(summary) {
    const rows = summaryRows(summary);
    if (!rows.length) return null;
    const table = el("table", null, "counts");
    const tb = el("tbody");
    for (const [k, v] of rows) {
      const tr = el("tr");
      tr.append(el("td", k), el("td", v));
      tb.appendChild(tr);
    }
    table.appendChild(tb);
    return table;
  }

  // One card's job area: the unlocked job (with counts), else the feed's line.
  function renderCard(card) {
    const box = $(card + "-job");
    if (!box) return;
    box.textContent = "";
    const job = lastJob && cardFor(lastJob.kind) === card ? lastJob : null;
    const task = feedTaskFor(card, feed);
    if (job) {
      const v = jobView(job);
      box.appendChild(el("p", v.line, "job-line " + v.cls));
      const counts = Object.entries(job.counts || {}).filter(([, n]) => n);
      if (job.state === "running" && counts.length) {
        box.appendChild(el("p", counts.map(([k, n]) => `${label(k)}: ${fmt(n)}`).join(" · "), "muted"));
      }
      const summary = (job.result && job.result.summary) || job.summary;
      if (job.state !== "running" && summary) {
        const t = countsTable(summary);
        if (t) box.appendChild(t);
        for (const w of summaryWarnings(summary)) box.appendChild(el("p", w, "warn"));
      }
    } else if (task) {
      box.appendChild(el("p", feedLine(task), "job-line"));
      if (!unlock.isUnlocked()) box.appendChild(el("p", "Unlock to see its details.", "muted"));
    }
    const stop = $("btn-" + card + "-stop");
    if (stop) {
      stop.disabled = !(stopEnabled(card, job) || (!job && task && task.state === "running"
                                                    && unlock.isUnlocked()));
    }
  }

  function renderCards() {
    renderCard("lf");
    renderCard("ih");
  }

  async function pollJob() {
    clearTimeout(pollTimer);
    const prev = lastJob;
    let j;
    try {
      j = await call("/api/admin/jobs/status");
    } catch (e) {
      // keep following a running job through a failed poll (unless locked out)
      if (prev && prev.state === "running" && unlock.isUnlocked()) {
        say(cardFor(prev.kind) + "-msg", e.message + " (retrying)", "err");
        pollTimer = setTimeout(() => pollJob().catch(() => {}), 5000);
      }
      throw e;
    }
    lastJob = j.job || null;
    if (jobEnded(prev, lastJob)) {
      // "Dry run started…" is stale now: the card says how it ended
      say(cardFor(prev.kind) + "-msg", "");
      if (prev.kind === "import-history") ihLastRun().catch(() => {});
    }
    renderCards();
    if (lastJob && lastJob.state === "running") pollTimer = setTimeout(() => {
      pollJob().catch(() => {});
    }, 1500);
  }

  async function startLoad() {
    say("lf-msg", "Starting…");
    try {
      const j = await call("/api/admin/load-folder", {
        instrument: $("lf-inst").value, folder: $("lf-folder").value.trim(),
        backfill: $("lf-backfill").checked,
      });
      lastJob = j.job;
      say("lf-msg", "Started", "ok");
      renderCards();
      await pollJob();
    } catch (e) { say("lf-msg", e.message, "err"); }
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

  // The dry run answers 202 {job} at once (a whole share folder takes longer
  // than Cloudflare's 100 s); its summary arrives on the finished job.
  async function ihDryRun() {
    say("ih-msg", "Starting the dry run…");
    try {
      const j = await call("/api/admin/import-history/dry-run", ihParams());
      lastJob = j.job;
      say("ih-msg", "Dry run started (nothing is written)", "ok");
      renderCards();
      await pollJob();
    } catch (e) { say("ih-msg", e.message, "err"); }
  }

  async function ihStart() {
    const inst = $("ih-inst").value;
    if (!window.confirm(`Start the real history import for ${inst}? ` +
        "This writes to the hub's database (every imported sample stays backfill).")) return;
    say("ih-msg", "Starting the import…");
    try {
      const j = await call("/api/admin/import-history/start",
        Object.assign(ihParams(), {confirm: true}));
      lastJob = j.job;
      say("ih-msg", "Import started", "ok");
      renderCards();
      await pollJob();
    } catch (e) { say("ih-msg", e.message, "err"); }
  }

  async function stopJob(card) {
    const where = card + "-msg";
    try {
      const j = await call("/api/admin/jobs/stop");
      lastJob = j.job;
      renderCards();
      say(where, isDryRun(j.job) ? "Stop requested; the dry run ends at its next step"
        : "Stop requested; it ends after the batch in progress", "ok");
    } catch (e) { say(where, e.message, "err"); }
  }

  async function ihLastRun() {
    const box = $("ih-last");
    box.textContent = "";
    const j = await call("/api/admin/import-history/last-run", {instrument: $("ih-inst").value});
    const r = j.last_run;
    if (!r) {
      box.textContent = "No import run yet for this instrument.";
      return;
    }
    // fill only what the user has not typed into (the answer can arrive late)
    const fill = (id, v) => { const f = $(id); if (!f.value && !f.dataset.touched) f.value = v; };
    fill("ih-processed", r.processed_dir || "");
    fill("ih-csv", r.results_csv || "");
    fill("ih-aliases", (r.aliases || []).join(", "));
    const n = Object.values(r.counts || {}).reduce((a, b) => a + (typeof b === "number" ? b : 0), 0);
    box.textContent = `Last run ${String(r.started_at || "").replace("T", " ").slice(0, 16)}` +
      (r.stopped ? " (stopped)" : "") + (n ? "; the form is filled from it" : "");
  }

  // ── the task feed (no password) ──
  function renderStatus(u) {
    const ul = $("status-tasks");
    ul.textContent = "";
    for (const t of feed) {
      const li = el("li");
      const R = window.GCRunningNow;
      li.appendChild(el("strong", R.headline(t)));
      const detail = R.detailLine(t);
      if (detail) li.appendChild(el("span", " · " + detail, "muted"));
      const meta = R.metaLine(t);
      if (meta) li.appendChild(el("span", " · " + meta, "muted"));
      ul.appendChild(li);
    }
    if (!feed.length) ul.appendChild(el("li", "Nothing running.", "muted"));
    const hub = u && u.hub;
    if (hub) {
      const paused = window.GCRunningNow.pausedBanner(hub);
      const q = hub.queue || {};
      $("hub-line").textContent = paused || (`Processing: ${fmt(q.waiting || 0)} waiting, ` +
        `${fmt(q.running || 0)} running` +
        (hub.exports_pending ? ` · ${fmt(hub.exports_pending)} rows waiting for the results files` : ""));
      $("hub-line").className = paused ? "warn" : "muted";
    }
  }

  function onLive(u) {
    const before = feedTaskFor("lf", feed) || feedTaskFor("ih", feed);
    feed = u.tasks || [];
    renderStatus(u);
    // a job started elsewhere (or this page's own) moved: follow it
    const now = feedTaskFor("lf", feed) || feedTaskFor("ih", feed);
    const moved = JSON.stringify(before) !== JSON.stringify(now);
    if (moved && unlock.isUnlocked() && !(lastJob && lastJob.state === "running")) {
      pollJob().catch(() => {});
    }
    renderCards();
  }

  // ── exports ──
  // New path / Write fresh: an inline path field under the row (never
  // window.prompt); Enter or the button sends it, Cancel or Escape closes it.
  function openPathEditor(tr, inst, action) {
    const old = document.querySelector("#exports-rows tr.path-edit");
    if (old) old.remove();
    const row = el("tr", null, "path-edit");
    row.dataset.instrument = inst;
    const td = el("td");
    td.colSpan = 5;
    const lbl = el("label", action === "write-fresh"
      ? `Write every result of ${inst} to a NEW .csv (it must not exist yet), then switch to it:`
      : `Point ${inst}'s results at another .csv (an existing file must be adopted first):`);
    const input = document.createElement("input");
    input.type = "text";
    input.className = "path-input";
    input.placeholder = "Absolute path on the server, e.g. D:\\GC\\gc1_results.csv";
    lbl.htmlFor = input.id = "path-" + action + "-" + inst;
    const go = el("button", action === "write-fresh" ? "Write fresh file" : "Use this file", "primary");
    go.type = "button";
    const cancel = el("button", "Cancel", "quiet");
    cancel.type = "button";
    const msg = el("span", null, "msg");
    const send = async () => {
      const path = input.value.trim();
      if (!path) { msg.textContent = "Enter an absolute path."; msg.className = "msg err"; return; }
      go.disabled = true;
      try {
        await call(`/api/admin/exports/${encodeURIComponent(inst)}/${action}`, {path});
        row.remove();
        say("exports-msg", `${inst}: ${action === "write-fresh" ? "fresh file written" : "path changed"}`, "ok");
        refreshExports().catch((e) => say("exports-msg", e.message, "err"));
      } catch (e) {
        msg.textContent = e.message;
        msg.className = "msg err";
        go.disabled = false;
      }
    };
    go.addEventListener("click", send);
    cancel.addEventListener("click", () => row.remove());
    input.addEventListener("keydown", (e) => {
      if (e.key === "Enter") send();
      if (e.key === "Escape") row.remove();
    });
    td.append(lbl, input, go, cancel, msg);
    row.appendChild(td);
    tr.after(row);
    input.focus();
  }

  async function exportAction(inst, action, tr) {
    const body = {};
    if (action === "new-path" || action === "write-fresh") {
      openPathEditor(tr, inst, action);
      return;
    } else if (!window.confirm(`Adopt ${inst}'s results file as it is now? The hub appends after its current content.`)) {
      return;
    }
    try {
      await call(`/api/admin/exports/${encodeURIComponent(inst)}/${action}`, body);
      say("exports-msg", `${action.replace("-", " ")}: done for ${inst}`, "ok");
    } catch (e) { say("exports-msg", `${inst}: ${e.message}`, "err"); }
    refreshExports().catch((e) => say("exports-msg", e.message, "err"));
  }

  async function refreshExports() {
    const tbody = $("exports-rows");
    const j = await call("/api/admin/exports");
    tbody.textContent = "";
    for (const s of j.instruments) {
      const tr = document.createElement("tr");
      tr.appendChild(el("td", s.instrument));
      tr.appendChild(el("td", s.path || s.error || "", "path"));
      tr.appendChild(el("td", s.pending === undefined ? "" : s.pending));
      const bad = s.refused || s.last_error;
      const state = s.refused ? `Refused: ${s.refused_detail || s.refused}`
        : (s.last_error ? `Error: ${s.last_error}` : "Healthy");
      tr.appendChild(el("td", state, bad ? "err" : "ok"));
      const td = el("td", null, "actions");
      if (bad) {     // the fixes only where something is wrong
        for (const [action, lbl] of [["adopt", "Adopt"], ["new-path", "New path"],
                                     ["write-fresh", "Write fresh"]]) {
          const b = el("button", lbl, "quiet");
          b.type = "button";
          b.addEventListener("click", () => exportAction(s.instrument, action, tr));
          td.appendChild(b);
        }
      } else {
        const b = el("button", "Move…", "quiet");
        b.type = "button";
        b.title = "Point this GC's results at another file";
        b.addEventListener("click", () => exportAction(s.instrument, "new-path", tr));
        td.appendChild(b);
      }
      tr.appendChild(td);
      tbody.appendChild(tr);
    }
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
      const btn = (lbl, cls, fn, disabled) => {
        const b = el("button", lbl, cls);
        b.type = "button";
        b.disabled = !!disabled;
        b.addEventListener("click", () => fn().catch((e) => say("presets-msg", e.message, "err")));
        li.appendChild(b);
      };
      btn("Save", "preset-save", async () => {
        await presetCall("update", {id: p.id, text: input.value});
        say("presets-msg", "Preset saved", "ok");
      });
      btn("↑", "preset-up quiet", () => move(presets, i, -1), i === 0);
      btn("↓", "preset-down quiet", () => move(presets, i, +1), i === presets.length - 1);
      btn(p.active ? "Deactivate" : "Activate", "preset-toggle quiet",
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

  async function addPreset() {
    const input = $("preset-new-text");
    try {
      await presetCall("create", {text: input.value});
      input.value = "";
      say("presets-msg", "Preset added", "ok");
    } catch (e) { say("presets-msg", e.message, "err"); }
  }

  // ── hub address and sessions ──
  async function saveHubUrl() {
    try {
      const j = await call("/api/admin/hub-url", {hub_url: $("hub-url").value.trim()});
      $("hub-url-effective").textContent = j.effective || j.hub_url || "https://gc.asaplabs.net";
      say("hub-url-msg", "Saved: " + $("hub-url-effective").textContent, "ok");
    } catch (e) { say("hub-url-msg", e.message, "err"); }
  }

  function renderSessions(rows) {
    const tb = $("sessions-rows");
    tb.textContent = "";
    for (const s of rows || []) {
      const tr = document.createElement("tr");
      for (const v of [s.name, s.method, s.ip || "", (s.last_seen || "").replace("T", " ").slice(0, 16)]) {
        tr.appendChild(el("td", v));
      }
      const td = document.createElement("td");
      const one = el("button", "Revoke", "quiet");
      one.type = "button";
      one.addEventListener("click", () => sessionCall("revoke", {id: s.id}));
      const all = el("button", "Revoke all for this name", "quiet");
      all.type = "button";
      all.addEventListener("click", () => {
        if (confirm("Sign out every session of " + s.name + "?")) sessionCall("revoke-name", {name: s.name});
      });
      td.append(one, all);
      tr.appendChild(td);
      tb.appendChild(tr);
    }
    if (!rows || !rows.length) {
      const tr = document.createElement("tr");
      const td = el("td", "No active sessions.", "muted");
      td.colSpan = 5;
      tr.appendChild(td);
      tb.appendChild(tr);
    }
  }

  async function sessionCall(action, extra) {
    try {
      const j = await call("/api/admin/sessions", Object.assign({action}, extra || {}));
      renderSessions(j.sessions);
      if (action !== "list") say("sessions-msg", "Done", "ok");
    } catch (e) { say("sessions-msg", e.message, "err"); }
  }

  // ── wiring ──
  $("unlock-form").addEventListener("submit", doUnlock);
  $("btn-lock").addEventListener("click", () => { unlock.forget(); });
  unlock.onChange(() => { renderLock(); renderCards(); });
  setInterval(renderLock, 15000);
  $("btn-hub-url").addEventListener("click", saveHubUrl);
  $("btn-preset-add").addEventListener("click", addPreset);
  $("btn-load").addEventListener("click", startLoad);
  $("btn-lf-stop").addEventListener("click", () => stopJob("lf"));
  $("btn-ih-dryrun").addEventListener("click", ihDryRun);
  $("btn-ih-start").addEventListener("click", ihStart);
  $("btn-ih-stop").addEventListener("click", () => stopJob("ih"));
  for (const id of ["ih-processed", "ih-csv", "ih-aliases"]) {
    $(id).addEventListener("input", () => { $(id).dataset.touched = "1"; });
  }
  $("ih-inst").addEventListener("change", () => {
    if (unlock.isUnlocked()) ihLastRun().catch((e) => say("ih-msg", e.message, "err"));
  });
  renderLock();
  renderCards();
  loadInstruments().catch(() => {});
  if (window.GCLive) {
    GCLive.subscribe(onLive);
    GCLive.start();
  }
})();
