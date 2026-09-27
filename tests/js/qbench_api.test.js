// Settings > QBench API: the status line from GET /api/qbench-api-credentials.
const { qbApiStatusText } = require('../../static/js/qbench_api.js');

module.exports = (t) => {
    t.eq(qbApiStatusText({ configured: false, client_id_hint: '', source: '' }), 'Not configured');
    t.eq(qbApiStatusText(null), 'Not configured');
    t.eq(qbApiStatusText({ configured: true, client_id_hint: '…abcd', source: 'store' }),
        'Configured (…abcd)');
    t.eq(qbApiStatusText({ configured: true, client_id_hint: '…abcd', source: 'environment' }),
        'Configured (…abcd) from environment variables, which override anything saved here');
};
