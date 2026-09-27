// Zero-dependency runner for the frontend pure-logic tests.
// Usage: node tests/js/run.js
const assert = require('assert');
const t = {
    eq(a, b) { assert.deepStrictEqual(a, b); },
};
const tests = ['./selection.test.js', './report_payload.test.js', './flagrules.test.js', './restart.test.js', './qbench_api.test.js'];
let failed = 0;
for (const f of tests) {
    try { require(f)(t); console.log('PASS', f); }
    catch (e) { failed++; console.error('FAIL', f, '\n', e.message); }
}
process.exit(failed ? 1 : 0);
