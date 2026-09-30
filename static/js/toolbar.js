/* toolbar.js: the classic main page's "More" menu (v4.0 lane E review).
   Comparison Export, Help and Restart Server sit behind it, so the toolbar
   fits at 1366 px. The buttons keep their ids, so app.js's handlers are
   unchanged; choosing one closes the menu. The menu is position: fixed under
   its button, because the toolbar scrolls sideways and would clip it. */
(function () {
    'use strict';
    if (typeof document === 'undefined') return;

    function mount() {
        const btn = document.getElementById('btn-more');
        const menu = document.getElementById('more-menu');
        if (!btn || !menu) return;

        function setOpen(open) {
            menu.hidden = !open;
            btn.setAttribute('aria-expanded', open ? 'true' : 'false');
            if (open) {
                const r = btn.getBoundingClientRect();
                menu.style.top = Math.round(r.bottom + 6) + 'px';
                menu.style.left = Math.round(Math.max(8, Math.min(r.left, window.innerWidth - 200))) + 'px';
            }
        }

        btn.addEventListener('click', (e) => { e.stopPropagation(); setOpen(menu.hidden); });
        menu.addEventListener('click', (e) => {
            if (e.target.closest('button')) setOpen(false);     // an item was chosen
        });
        document.addEventListener('click', (e) => {
            if (!menu.hidden && !menu.contains(e.target)) setOpen(false);
        });
        document.addEventListener('keydown', (e) => { if (e.key === 'Escape' && !menu.hidden) setOpen(false); });
    }

    if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', mount);
    else mount();
})();
