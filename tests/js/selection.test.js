const { computeRangeSelection, selectionFilesOr } = require('../../static/js/selection.js');

module.exports = function (t) {
    // computeRangeSelection — forward range
    t.eq(computeRangeSelection(['a', 'b', 'c', 'd'], 'b', 'd'), ['b', 'c', 'd']);
    // backward range (anchor after target) is normalised
    t.eq(computeRangeSelection(['a', 'b', 'c', 'd'], 'd', 'b'), ['b', 'c', 'd']);
    // single item / anchor == target
    t.eq(computeRangeSelection(['a', 'b', 'c'], 'b', 'b'), ['b']);
    // unknown anchor falls back to [target]
    t.eq(computeRangeSelection(['a', 'b', 'c'], 'zz', 'c'), ['c']);

    // selectionFilesOr — >1 selected returns picked files in file order
    const files = [{ uid: '1' }, { uid: '2' }, { uid: '3' }];
    t.eq(selectionFilesOr(files, new Set(['1', '3']), files[1]), [files[0], files[2]]);
    // <=1 selected falls back to [file]
    t.eq(selectionFilesOr(files, new Set(['2']), files[1]), [files[1]]);
    t.eq(selectionFilesOr(files, new Set(), files[2]), [files[2]]);
};
