// The report ZIP export is a background job (v3.0.1): what the page shows
// while it polls GET /api/export-analysis-reports-zip/<id>, and when to fetch.
const { zipJobView, isZipDownloadUrl } = require('../../static/js/report_payload.js');

module.exports = (t) => {
    t.eq(zipJobView({ state: 'running', done: 0, total: 12 }),
        { done: false, message: 'Generating reports: 0 of 12…', cls: 'info', download: null });
    t.eq(zipJobView({ state: 'running', done: 5, total: 12 }).message,
        'Generating reports: 5 of 12…');

    const dl = '/api/export-analysis-reports-zip/AbC_9-x/download';
    t.eq(zipJobView({ state: 'done', done: 12, total: 12, download: dl,
                      result: { written: 12, skipped: 0 } }),
        { done: true, message: 'Downloaded 12 reports as ZIP', cls: 'success', download: dl });
    t.eq(zipJobView({ state: 'done', total: 3, download: dl, result: { written: 2, skipped: 1 } })
        .message, 'Downloaded 2 reports as ZIP (1 skipped: unknown sample, no CDF or standard not found)');

    t.eq(zipJobView({ state: 'failed', error: 'All 2 report(s) were skipped', download: null }),
        { done: true, message: 'Download failed: All 2 report(s) were skipped', cls: 'error',
          download: null });
    // a done job without a link of the hub's own shape is not followed
    t.eq(zipJobView({ state: 'done', download: 'https://evil.example/x', result: { written: 1 } }).download,
        null);

    t.eq(isZipDownloadUrl(dl), true);
    t.eq(isZipDownloadUrl('/api/export-analysis-reports-zip/../x/download'), false);
    t.eq(isZipDownloadUrl('//evil.example/api/export-analysis-reports-zip/a/download'), false);
    t.eq(isZipDownloadUrl(null), false);
};
