// Zero-dependency runner for the frontend pure-logic tests.
// Usage: node tests/js/run.js
// A test module exports (t) => void, or (t) => Promise (awaited).
const assert = require('assert');
const t = {
    eq(a, b) { assert.deepStrictEqual(a, b); },
};
const tests = ['./selection.test.js', './report_payload.test.js', './flagrules.test.js', './restart.test.js', './qbench_api.test.js', './samples.test.js', './instruments.test.js', './instrument_filter.test.js', './comments.test.js', './ladder.test.js', './diagnostics.test.js', './lem_machines.test.js', './session.test.js', './hub_admin.test.js', './report_zip.test.js', './distill_view.test.js', './live.test.js', './live_poller.test.js', './deeplink.test.js', './ui_logic.test.js', './live_adapter.test.js', './purge.test.js', './live_tasks.test.js', './sample_order.test.js', './running_now.test.js', './admin_unlock.test.js', './hub_admin_view.test.js', './admin_page.test.js', './calibration.test.js', './backfill.test.js', './compare_logic.test.js', './report_queue.test.js', './results_logic.test.js', './settings_logic.test.js', './notifications_panel.test.js'];
(async () => {
    let failed = 0;
    for (const f of tests) {
        try { await require(f)(t); console.log('PASS', f); }
        catch (e) { failed++; console.error('FAIL', f, '\n', e.message); }
    }
    process.exit(failed ? 1 : 0);
})();
