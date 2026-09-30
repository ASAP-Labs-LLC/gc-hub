/* calibration.js: the peak-assignment page (/calibration?instrument=<id>),
   inside the shell since v4.0 lane E2 (it was an inline script).

   Detect peaks on the instrument's calibration CDF, pick a carbon number (or
   Ignore) for each true n-alkane peak, and Save. Saving needs the admin
   password: the page's one 15-minute unlock (admin_unlock.js, shared with the
   shell's unlock chip), asked for in the shell's masked dialog; there is no
   password box on the page. The DOM is built with textContent only. The pure
   helpers are module.exports for tests/js/calibration.test.js. */
(function (root) {
    'use strict';

    const RT_TOL = 0.02;            // min: match a saved assignment to a detected peak

    function priorFromAssignments(assignments) {
        return (assignments || []).map(a => ({
            rt: a.rt,
            choice: a.ignore ? 'ignore' : (a.carbon != null ? String(a.carbon) : ''),
        }));
    }

    /** Each detected peak with the nearest prior choice within RT_TOL. */
    function matchChoices(peaks, prior) {
        return (peaks || []).map(p => {
            let best = null;
            let bestd = RT_TOL + 1e-9;
            for (const a of prior || []) {
                const d = Math.abs(a.rt - p.rt);
                if (d <= bestd) { bestd = d; best = a; }
            }
            return { rt: p.rt, intensity: p.intensity, choice: best ? best.choice : '' };
        });
    }

    function bpFor(choice, compounds) {
        if (choice === '' || choice === 'ignore') return null;
        const c = (compounds || []).find(x => String(x.carbon) === String(choice));
        return c ? c.bp : null;
    }

    function assignmentsFor(state) {
        return state.map(s => {
            if (s.choice === 'ignore') return { rt: s.rt, ignore: true };
            if (s.choice !== '') return { rt: s.rt, carbon: parseInt(s.choice, 10) };
            return { rt: s.rt };           // unassigned: the server omits it
        });
    }

    function choiceOptions(compounds) {
        return [['', '— Unassigned —'], ['ignore', 'Ignore (extra)']].concat(
            (compounds || []).map(c => [String(c.carbon), 'C' + c.carbon + ' — ' + c.bp + '°C']));
    }

    function savedText(j) {
        const n = j.anchors;
        if (n < 4) {
            return ['Saved, but only ' + n + ' carbon' + (n === 1 ? '' : 's') + ' assigned: assign at least 4 ' +
                '(ideally the full ladder) for accurate distillation numbers.', 'err'];
        }
        return ['Saved: ' + n + ' calibration anchors active' +
            (j.queued ? '; ' + j.queued + ' waiting sample' + (j.queued === 1 ? '' : 's') + ' queued' : ''), 'ok'];
    }

    function counts(state) {
        return { peaks: state.length,
                 assigned: state.filter(s => s.choice !== '' && s.choice !== 'ignore').length };
    }

    const pure = { RT_TOL, priorFromAssignments, matchChoices, bpFor, assignmentsFor, choiceOptions,
                   savedText, counts };
    if (typeof module !== 'undefined' && module.exports) {
        module.exports = pure;
        return;
    }
    if (typeof document === 'undefined') return;

    // ── the page ──────────────────────────────────────────────────────────
    const $ = (id) => document.getElementById(id);
    let DATA = null;
    let STATE = [];
    let selectedIdx = -1;
    const INSTRUMENT = new URLSearchParams(location.search).get('instrument') || 'gc1';
    const CAL_API = '/api/instruments/' + encodeURIComponent(INSTRUMENT) + '/calibration';

    function td(text, cls) {
        const e = document.createElement('td');
        if (text !== undefined && text !== null) e.textContent = String(text);
        if (cls) e.className = cls;
        return e;
    }

    function setStatus(msg, cls) {
        const s = $('status');
        s.textContent = msg || '';
        s.className = 'cal-status' + (cls ? ' ' + cls : '');
    }

    function messageRow(text, extra) {
        const tr = document.createElement('tr');
        const cell = td(text, 'empty');
        cell.colSpan = 5;
        if (extra) { cell.appendChild(document.createElement('br')); cell.appendChild(document.createTextNode(extra)); }
        tr.appendChild(cell);
        $('rows').replaceChildren(tr);
    }

    async function loadInstruments() {
        const sel = $('inst');
        try {
            const r = await fetch('/api/instruments', { headers: { Accept: 'application/json' } });
            const j = (await root.GCSession.readJson(r)).body || {};
            for (const inst of (j.instruments || [])) {
                const o = document.createElement('option');
                o.value = inst.id;
                o.textContent = inst.name && inst.name !== inst.id ? inst.name + ' (' + inst.id + ')' : inst.id;
                sel.appendChild(o);
                if (inst.id === INSTRUMENT) {
                    $('crumb-inst').textContent = inst.name || inst.id;
                }
            }
        } catch (_e) { /* the selector stays empty; the page still edits INSTRUMENT */ }
        if (![...sel.options].some(o => o.value === INSTRUMENT)) {
            const o = document.createElement('option');
            o.value = INSTRUMENT;
            o.textContent = INSTRUMENT;
            sel.appendChild(o);
        }
        sel.value = INSTRUMENT;
        sel.addEventListener('change', () => {
            location.search = '?instrument=' + encodeURIComponent(sel.value);
        });
    }

    async function load(sensitivity, preserveChoices) {
        const prior = preserveChoices ? STATE.filter(s => s.choice !== '').map(s => ({ rt: s.rt, choice: s.choice })) : null;
        setStatus('Detecting peaks…');
        try {
            const url = CAL_API + (sensitivity != null ? ('?sensitivity=' + sensitivity) : '');
            const r = await fetch(url, { headers: { Accept: 'application/json' } });
            const j = (await root.GCSession.readJson(r)).body || {};
            if (!r.ok || j.error) throw new Error(j.error || ('HTTP ' + r.status));
            DATA = j;
            if (j.instrument_name) $('crumb-inst').textContent = j.instrument_name;
            $('cdf-name').textContent = (j.cdf_name || '') +
                (j.usable ? '' : ' (not usable yet: ' + (j.problem || 'assign the peaks') + ')');
            if (sensitivity == null && j.sensitivity != null) {
                $('sens').value = j.sensitivity;
                $('sens-val').textContent = Math.round(j.sensitivity);
            }
            STATE = matchChoices(DATA.peaks, prior != null ? prior : priorFromAssignments(DATA.assignments));
            renderTable();
            drawChart();
            setStatus('');
        } catch (e) {
            setStatus(e.message, 'err');
            messageRow(e.message, 'Choose this instrument\'s calibration CDF on its Instruments page first.');
        }
    }

    function rowClass(choice) {
        if (choice === 'ignore') return 'ignored';
        if (choice !== '') return 'assigned';
        return '';
    }

    function renderTable() {
        const tb = $('rows');
        if (!STATE.length) { messageRow('No peaks detected.'); updateCounts(); return; }
        const opts = choiceOptions(DATA.compounds);
        const rows = STATE.map((s, i) => {
            const tr = document.createElement('tr');
            tr.dataset.idx = i;
            tr.className = rowClass(s.choice) + (i === selectedIdx ? ' sel' : '');
            const sel = document.createElement('select');
            sel.dataset.idx = i;
            sel.setAttribute('aria-label', 'Carbon number for peak ' + (i + 1));
            for (const [v, label] of opts) {
                const o = document.createElement('option');
                o.value = v;
                o.textContent = label;
                sel.appendChild(o);
            }
            sel.value = s.choice;
            sel.addEventListener('change', (e) => {
                STATE[i].choice = e.target.value;
                selectedIdx = i;
                renderTable();
                drawChart();
            });
            const cell = td(null);
            cell.appendChild(sel);
            const bp = bpFor(s.choice, DATA.compounds);
            tr.append(td(i + 1), td(s.rt.toFixed(3), 'num'), td(Math.round(s.intensity).toLocaleString(), 'num'),
                      cell, td(bp == null ? '—' : bp, 'num'));
            tr.addEventListener('click', (ev) => { if (ev.target.tagName !== 'SELECT') selectPeak(i); });
            return tr;
        });
        tb.replaceChildren(...rows);
        updateCounts();
    }

    function updateCounts() {
        const c = counts(STATE);
        $('peak-count').textContent = c.peaks + ' peaks';
        $('assigned-count').textContent = c.assigned + ' assigned';
    }

    function tok(name, fallback) {
        const v = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
        return v || fallback;
    }
    function colorFor(choice) {
        if (choice === 'ignore') return tok('--text-muted', '#64748b');
        if (choice !== '') return tok('--st-final', '#15803d');
        return tok('--chart-ink', '#0f172a');
    }

    function drawChart() {
        if (!DATA || !DATA.trace || !root.Plotly) return;
        const trace = { x: DATA.trace.x, y: DATA.trace.y, type: 'scatter', mode: 'lines',
            line: { color: tok('--chart-ink', '#0f172a'), width: 1 }, name: 'chromatogram', hoverinfo: 'x+y' };
        const markers = {
            x: STATE.map(s => s.rt), y: STATE.map(s => s.intensity), type: 'scatter', mode: 'markers+text',
            marker: { size: 9, color: STATE.map(s => colorFor(s.choice)), line: { width: 1, color: tok('--bg-card', '#fff') } },
            text: STATE.map(s => s.choice === 'ignore' ? 'ign' : (s.choice !== '' ? 'C' + s.choice : '')),
            textposition: 'top center', textfont: { size: 10, color: tok('--text', '#0f172a') },
            name: 'peaks', hovertemplate: 'RT %{x:.3f} min<extra></extra>',
        };
        const shapes = (selectedIdx >= 0 && STATE[selectedIdx]) ? [{
            type: 'line', x0: STATE[selectedIdx].rt, x1: STATE[selectedIdx].rt, yref: 'paper', y0: 0, y1: 1,
            line: { color: tok('--warn-text', '#b45309'), width: 1, dash: 'dot' } }] : [];
        const layout = {
            paper_bgcolor: tok('--bg-card', '#fff'), plot_bgcolor: tok('--bg-card', '#fff'),
            font: { color: tok('--chart-axis', '#64748b'), size: 11 }, margin: { l: 55, r: 12, t: 10, b: 40 },
            showlegend: false,
            xaxis: { title: 'Retention time (min)', gridcolor: tok('--chart-grid', '#eef0f3'), zeroline: false },
            yaxis: { title: 'Intensity', gridcolor: tok('--chart-grid', '#eef0f3'), zeroline: false },
            shapes,
        };
        root.Plotly.react('chart', [trace, markers], layout, { responsive: true, displayModeBar: false });
        const chart = $('chart');
        if (!chart.dataset.wired && chart.on) {
            chart.dataset.wired = '1';
            chart.on('plotly_click', (ev) => {
                if (!ev.points || !ev.points.length) return;
                const px = ev.points[0].x;
                let bi = -1;
                let bd = 1e9;
                STATE.forEach((s, i) => { const d = Math.abs(s.rt - px); if (d < bd) { bd = d; bi = i; } });
                if (bi >= 0) selectPeak(bi);
            });
        }
    }

    function selectPeak(i) {
        selectedIdx = i;
        document.querySelectorAll('#rows tr').forEach(tr => tr.classList.toggle('sel', +tr.dataset.idx === i));
        drawChart();
        const row = document.querySelector('tr[data-idx="' + i + '"]');
        if (row) row.scrollIntoView({ block: 'nearest' });
    }

    async function save() {
        const U = root.GCAdminUnlock;
        const password = await U.ask('save the calibration');
        if (!password) { setStatus('Not saved: the admin password is needed to save.', 'err'); return; }
        setStatus('Saving…');
        $('btn-save').disabled = true;
        try {
            const r = await fetch('/api/admin/instruments/' + encodeURIComponent(INSTRUMENT) + '/calibration', {
                method: 'POST', headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
                body: JSON.stringify({ assignments: assignmentsFor(STATE), sensitivity: +$('sens').value, password }),
            });
            const j = (await root.GCSession.readJson(r)).body || {};
            if (!r.ok || j.error) {
                const e = new Error(r.status === 403 && /incorrect/i.test(j.error || '')
                    ? 'That admin password was not accepted. Press Save to try again.' : (j.error || ('HTTP ' + r.status)));
                e.status = r.status;
                throw e;
            }
            U.accepted(password);
            const [text, cls] = savedText(j);
            setStatus(text, cls);
        } catch (e) {
            U.refused(e);
            setStatus(e.message, 'err');
        } finally {
            $('btn-save').disabled = false;
        }
    }

    function init() {
        if (!$('cal-page')) return;
        $('crumb-inst').textContent = INSTRUMENT;
        $('crumb-inst').setAttribute('href', '/instruments/' + encodeURIComponent(INSTRUMENT));
        $('btn-save').addEventListener('click', save);
        $('btn-reload').addEventListener('click', () => load(+$('sens').value, true));
        let sensTimer = null;
        $('sens').addEventListener('input', (e) => {
            $('sens-val').textContent = e.target.value;
            clearTimeout(sensTimer);
            sensTimer = setTimeout(() => load(+e.target.value, true), 250);
        });
        loadInstruments();
        load();
    }

    if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
    else init();
})(typeof window !== 'undefined' ? window : globalThis);
