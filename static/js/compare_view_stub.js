/* compare_view_stub.js (v5.0.0 lane S): a stand-in for lane C's
   compare_view.js, defined ONLY when window.GCCompare is missing. The
   Samples page loads compare_view.js first and this file only if that one
   did not define GCCompare. Same contract:
     GCCompare.mount(el, {sample, standards, settings, onUrlChange})
       -> {unmount(), setStandard(name)}
   It shows the chosen standard and a link to the classic Analysis tab for
   this run, which does the comparison until lane C's view lands. */
(function (root) {
    'use strict';
    if (root.GCCompare) return;

    function mount(el, opts) {
        const o = opts || {};
        const sample = o.sample || {};
        const standards = Array.isArray(o.standards) ? o.standards : [];
        let standard = sample.standard || null;
        const box = document.createElement('div');
        box.className = 'card compare-fallback';
        box.setAttribute('data-testid', 'compare-stub');
        const h = document.createElement('h2');
        h.textContent = 'Compare';
        const p = document.createElement('p');
        const pick = document.createElement('select');
        pick.setAttribute('aria-label', 'Comparison standard');
        const none = document.createElement('option');
        none.value = '';
        none.textContent = 'Best fit';
        pick.appendChild(none);
        for (const s of standards) {
            const opt = document.createElement('option');
            opt.value = s.name;
            opt.textContent = s.name;
            pick.appendChild(opt);
        }
        const link = document.createElement('a');
        link.className = 'btn btn-sm';
        function sync() {
            pick.value = standard && standards.some(s => s.name === standard) ? standard : '';
            p.textContent = 'The new Compare view is not in this build yet. Open this run in the classic Analysis tab'
                + (standard ? ' against ' + standard : '') + '.';
            link.textContent = 'Open in the classic Analysis tab';
            link.href = '/classic/samples/' + encodeURIComponent(sample.sample_id) + '/compare'
                + (standard ? '?standard=' + encodeURIComponent(standard) : '');
        }
        pick.addEventListener('change', () => {
            standard = pick.value || null;
            sync();
            if (typeof o.onUrlChange === 'function') o.onUrlChange({ standard });
        });
        sync();
        box.append(h, p, pick, link);
        el.appendChild(box);
        return {
            unmount() { box.remove(); },
            setStandard(name) { standard = name || null; sync(); },
        };
    }

    root.GCCompare = { mount, __stub: true };
})(typeof window !== 'undefined' ? window : globalThis);
