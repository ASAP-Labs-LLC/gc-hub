// Test-only: mounts GCCompare on the harness page as lane S will, from the
// hub's own answers, and records what the page is told (window.__h).
(function () {
    'use strict';
    const H = window.__h = { urls: [], adjust: [], handle: null, ready: false, error: null };
    document.addEventListener('gc:adjust', (e) => H.adjust.push(e.detail));

    async function get(url) {
        const r = await fetch(url, { headers: { Accept: 'application/json' } });
        return (await GCSession.readJson(r)).body;
    }

    H.mount = async function (sampleId) {
        if (H.handle) H.handle.unmount();
        const files = await get('/api/files?ids=' + encodeURIComponent(sampleId));
        const sample = (files.samples || [])[0];
        const standards = await get('/api/comparison-standards');
        const settings = await get('/api/settings');
        H.sample = sample; H.standards = standards; H.settings = settings;
        document.getElementById('h-lab').textContent = sample.lab_id;
        H.handle = GCCompare.mount(document.getElementById('compare-root'), {
            sample, standards, settings,
            onUrlChange: (u) => { H.urls.push(u); },
        });
        H.ready = true;
        return H.handle;
    };

    document.getElementById('h-queue').addEventListener('click', () =>
        GCCompare.addToQueue({ sample: H.sample, standards: H.standards, settings: H.settings }));
    document.getElementById('h-export').addEventListener('click', () =>
        GCCompare.openExportSheet({ sample: H.sample, standards: H.standards, settings: H.settings }));

    const q = new URLSearchParams(location.search);
    const std = q.get('standard');
    H.mount(q.get('sample')).then((handle) => {
        if (std) handle.setStandard(std);
    }).catch((e) => { H.error = String(e && e.stack || e); });
})();
