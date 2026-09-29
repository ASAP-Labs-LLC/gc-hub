/* distill_view.js: pure, DOM-free helpers for showing distillation numbers —
   shared by the browser (window.DistillView) and Node tests (module.exports).

   The Distillation Data table's header is built from the columns /api/table
   returns (distill.CSV_HEADER order), so every header sits over its own
   value; each column keeps its colour group (col-meta / col-d86 / col-d2887). */
(function (root) {
    'use strict';

    const META_COLUMNS = ['Lab ID', 'InjectionDateTime'];
    const LABELS = { InjectionDateTime: 'Injection Date' };

    /** The colour-group class of a column (as its cells get it). */
    function columnClass(colName) {
        const c = String(colName || '');
        if (META_COLUMNS.includes(c)) return 'col-meta';
        if (c.includes('D86')) return 'col-d86';
        if (c.includes('2887')) return 'col-d2887';
        return '';
    }

    /** One header cell per column, in the columns' order: {col, label, cls}. */
    function tableHeader(columns) {
        if (!Array.isArray(columns)) return [];
        return columns.map((col) => ({ col, label: LABELS[col] || col, cls: columnClass(col) }));
    }

    /** A header cell's text with the sort arrow when it is the sorted column. */
    function headerText(cell, index, sortCol, sortAsc) {
        const arrow = index === sortCol ? (sortAsc ? ' ▲' : ' ▼') : '';
        return cell.label + arrow;
    }

    const api = { columnClass, tableHeader, headerText };
    if (typeof module !== 'undefined' && module.exports) module.exports = api;
    if (root) root.DistillView = api;
})(typeof window !== 'undefined' ? window : null);
