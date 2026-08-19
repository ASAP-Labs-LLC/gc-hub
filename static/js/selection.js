/* Pure, DOM-free selection helpers — shared by the browser (window globals)
   and Node tests (module.exports). No document/fetch references. */
(function (root) {
    /** Inclusive uid range between anchor and target in an ordered uid list. */
    function computeRangeSelection(orderedUids, anchorUid, targetUid) {
        const a = orderedUids.indexOf(anchorUid);
        const b = orderedUids.indexOf(targetUid);
        if (a === -1 || b === -1) return targetUid != null ? [targetUid] : [];
        const [lo, hi] = a <= b ? [a, b] : [b, a];
        return orderedUids.slice(lo, hi + 1);
    }

    /** Files for the current selection: the multi-selection (in `files` order)
        when >1 uid is selected, else just [file]. */
    function selectionFilesOr(files, selectedUids, file) {
        if (selectedUids && selectedUids.size > 1) {
            const picked = files.filter(f => selectedUids.has(f.uid || f.path));
            if (picked.length) return picked;
        }
        return [file];
    }

    root.computeRangeSelection = computeRangeSelection;
    root.selectionFilesOr = selectionFilesOr;
    if (typeof module !== 'undefined' && module.exports) {
        module.exports = { computeRangeSelection, selectionFilesOr };
    }
})(typeof window !== 'undefined' ? window : globalThis);
