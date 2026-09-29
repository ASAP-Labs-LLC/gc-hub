// Hub admin "Download diagnostics": sizes, the chosen options, the saved file name.
const D = require('../../static/js/diagnostics.js');

module.exports = (t) => {
    t.eq(D.formatBytes(0), '0 B');
    t.eq(D.formatBytes(1023), '1023 B');
    t.eq(D.formatBytes(2048), '2.0 KB');
    t.eq(D.formatBytes(5 * 1024 * 1024), '5.0 MB');
    t.eq(D.formatBytes(3.5 * 1024 * 1024 * 1024), '3.5 GB');
    t.eq(D.formatBytes(null), '?');

    t.eq(D.chosenOptions([{ key: 'logs', checked: true }, { key: 'all_cdfs', checked: false }]),
        { logs: true, all_cdfs: false });

    const rows = [{ key: 'logs', bytes: 1000 }, { key: 'database', bytes: 5000 },
                  { key: 'all_cdfs', bytes: 2 * 1024 * 1024 * 1024 }];
    t.eq(D.estimateTotal(rows, { logs: true, database: false, all_cdfs: false }), 1000);
    t.eq(D.estimateTotal(rows, { logs: true, database: true }), 6000);

    t.eq(D.sizeWarning(rows, { logs: true }), null);
    t.eq(D.sizeWarning(rows, { all_cdfs: true }).includes('2.0 GB'), true);
    t.eq(D.sizeWarning([{ key: 'problem_cdfs', bytes: 600 * 1024 * 1024 }], { problem_cdfs: true })
        .includes('600.0 MB'), true);

    t.eq(D.filenameFromDisposition('attachment; filename="gc-diagnostics-x.zip"'),
        'gc-diagnostics-x.zip');
    t.eq(D.filenameFromDisposition(null), 'gc-diagnostics.zip');
    t.eq(D.filenameFromDisposition('attachment; filename="../../evil.zip"'), 'evil.zip');

    // Only the hub's own one-time download path is navigated to.
    t.eq(D.isDownloadUrl('/api/admin/diagnostics/download/Ab_9-x'), true);
    t.eq(D.isDownloadUrl('https://evil.example/x'), false);
    t.eq(D.isDownloadUrl('/api/admin/diagnostics/download/../x'), false);
    t.eq(D.isDownloadUrl('//evil.example/api/admin/diagnostics/download/x'), false);
    t.eq(D.isDownloadUrl(null), false);
};
