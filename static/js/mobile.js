// mobile.js (v6.0 lane L5): the shell on phones and tablets. mobile.css does
// the layout; this file only does what CSS cannot:
//
// * the phone top bar's menu button opens the sidebar as a drawer (an
//   overlay, Escape and the close button shut it, Tab stays inside it, the
//   page behind is inert, focus goes back to the menu button);
// * the phone top bar's status (the Live dot, a working spinner while
//   something runs, the bell and its count) mirrors the sidebar footer, so
//   global status stays visible while the drawer is shut; its bell opens the
//   same notifications panel;
// * on the Samples page, <html class="s-detail-open"> while the address bar
//   names a sample (SamplesRouter, the page's own router: no new state), and
//   a "Samples" back button that returns to the list, so below ~900px the
//   list and the detail take turns in one pane;
// * Plotly charts are resized when a pane appears, on resize and on an
//   orientation change.
//
// Nothing here changes a page wider than the breakpoints: the classes and
// the mirrored status are only shown by mobile.css's media queries.
(function () {
    'use strict';
    const PHONE = '(max-width: 759.98px)';
    const $ = (id) => document.getElementById(id);
    const mq = (q) => (window.matchMedia ? window.matchMedia(q) : { matches: false, addEventListener() {} });
    const FOCUSABLE = 'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), ' +
                      'textarea:not([disabled]), [tabindex]:not([tabindex="-1"])';

    function visible(el) {
        if (!el || el.closest('[hidden]')) return false;
        const r = el.getBoundingClientRect();
        if (!r.width && !r.height) return false;
        const cs = getComputedStyle(el);
        return cs.visibility !== 'hidden' && cs.display !== 'none';
    }

    // ── charts: re-measure every Plotly chart that is on screen ─────────────
    let resizeQueued = false;
    function resizeCharts() {
        if (resizeQueued) return;
        resizeQueued = true;
        requestAnimationFrame(() => {
            resizeQueued = false;
            if (!window.Plotly || !window.Plotly.Plots) return;
            document.querySelectorAll('.js-plotly-plot').forEach((p) => {
                if (!p._fullLayout || !visible(p)) return;
                try { window.Plotly.Plots.resize(p); } catch (_e) { /* a chart mid-redraw */ }
            });
        });
    }

    // ── the drawer ──────────────────────────────────────────────────────────
    const phone = mq(PHONE);
    let lastFocus = null;

    function drawerOpen() { return document.documentElement.classList.contains('nav-open'); }

    function setDrawer(open) {
        const sb = $('sidebar');
        const btn = $('nav-open');
        const scrim = $('sb-scrim');
        const main = document.querySelector('.main-col');
        if (!sb || !btn) return;
        open = !!open && phone.matches;
        if (open === drawerOpen()) return;
        document.documentElement.classList.toggle('nav-open', open);
        btn.setAttribute('aria-expanded', open ? 'true' : 'false');
        if (scrim) scrim.hidden = !open;
        if (main) main.inert = open;
        if (open) {
            sb.setAttribute('role', 'dialog');
            sb.setAttribute('aria-modal', 'true');
            lastFocus = document.activeElement;
            const first = $('sb-close') || sb.querySelector(FOCUSABLE);
            if (first) first.focus();
        } else {
            sb.removeAttribute('role');
            sb.removeAttribute('aria-modal');
            const back = lastFocus && document.contains(lastFocus) && visible(lastFocus) ? lastFocus : btn;
            lastFocus = null;
            if (phone.matches) back.focus();
        }
    }

    function anyShellPopupOpen() {
        return ['bell-panel', 'user-menu', 'running-now-popover'].some((id) => { const el = $(id); return el && !el.hidden; });
    }

    function trapTab(ev) {
        const sb = $('sidebar');
        const items = Array.from(sb.querySelectorAll(FOCUSABLE)).filter(visible);
        if (!items.length) return;
        const first = items[0];
        const last = items[items.length - 1];
        const at = document.activeElement;
        if (!sb.contains(at)) { ev.preventDefault(); first.focus(); return; }
        if (ev.shiftKey && at === first) { ev.preventDefault(); last.focus(); }
        else if (!ev.shiftKey && at === last) { ev.preventDefault(); first.focus(); }
    }

    function wireDrawer() {
        const btn = $('nav-open');
        if (!btn || !$('sidebar')) return;
        btn.addEventListener('click', (ev) => { ev.stopPropagation(); setDrawer(!drawerOpen()); });
        const close = $('sb-close');
        if (close) close.addEventListener('click', () => setDrawer(false));
        const scrim = $('sb-scrim');
        if (scrim) scrim.addEventListener('click', () => setDrawer(false));
        // capture: a menu or panel open inside the drawer closes first (shell.js),
        // the drawer only on the next Escape
        window.addEventListener('keydown', (ev) => {
            if (!drawerOpen()) return;
            if (ev.key === 'Escape' && !anyShellPopupOpen() && !document.querySelector('dialog[open]')) {
                ev.preventDefault();
                setDrawer(false);
            } else if (ev.key === 'Tab' && !document.querySelector('dialog[open]') && !anyShellPopupOpen()) {
                trapTab(ev);
            }
        }, true);
        // a link in the drawer to this same page (a hash, the Samples list) leaves nothing to cover
        $('sidebar').addEventListener('click', (ev) => {
            if (drawerOpen() && ev.target.closest('a[href]')) setDrawer(false);
        });
        const onChange = () => { if (!phone.matches) setDrawer(false); resizeCharts(); };
        if (phone.addEventListener) phone.addEventListener('change', onChange);
        else if (phone.addListener) phone.addListener(onChange);
    }

    // ── the top bar's height, for what sticks under it ──────────────────────
    // On a phone the bar can take two rows (the page's actions under the
    // title); --topbar-now on <html> is its real height.
    function trackTopbar() {
        const bar = document.querySelector('.topbar');
        if (!bar) return;
        const set = () => document.documentElement.style.setProperty('--topbar-now', Math.ceil(bar.getBoundingClientRect().height) + 'px');
        set();
        if (typeof ResizeObserver === 'function') new ResizeObserver(set).observe(bar);
    }

    // ── global status in the phone top bar ──────────────────────────────────
    function wireStatus() {
        const dot = $('live-dot');
        const text = $('live-text');
        const count = $('bell-count');
        const mDot = $('tb-live-dot');
        const mLive = $('tb-live');
        const mCount = $('tb-bell-count');
        const mBell = $('tb-bell');
        const running = $('running-now');
        const mWork = $('tb-working');
        if (!mLive || !dot) return;
        function sync() {
            mDot.dataset.state = dot.dataset.state || 'connecting';
            const line = (text && text.textContent.trim()) || 'Connecting…';
            const work = running && !running.hidden ? running.textContent.trim() : '';
            mLive.setAttribute('aria-label', 'Status: ' + line + (work ? '. ' + work : '') + '. Open the menu');
            mLive.title = line + (work ? ' · ' + work : '');
            if (mWork) mWork.hidden = !work;
            if (count && mCount) {
                mCount.hidden = count.hidden;
                mCount.textContent = count.textContent;
                mBell.setAttribute('aria-label', count.hidden || !count.textContent ? 'Notifications'
                    : 'Notifications: ' + count.textContent + ' unread');
            }
        }
        const watch = new MutationObserver(sync);
        for (const el of [dot, text, count, running]) {
            if (el) watch.observe(el, { attributes: true, childList: true, characterData: true, subtree: true });
        }
        sync();
        mLive.addEventListener('click', (ev) => { ev.stopPropagation(); setDrawer(true); });
        const bell = $('bell');
        const panel = $('bell-panel');
        if (mBell && bell) {
            mBell.addEventListener('click', (ev) => {
                // the shell's bell does the opening (and closes the user menu);
                // stopPropagation keeps the shell's outside-click from shutting it again
                ev.stopPropagation();
                bell.click();
                mBell.setAttribute('aria-expanded', panel && !panel.hidden ? 'true' : 'false');
                if (panel && !panel.hidden) {
                    const first = panel.querySelector(FOCUSABLE);
                    if (first) first.focus();
                }
            });
            if (panel) new MutationObserver(() => {
                mBell.setAttribute('aria-expanded', panel.hidden ? 'false' : 'true');
            }).observe(panel, { attributes: true, attributeFilter: ['hidden'] });
        }
    }

    // ── Samples: one pane at a time on narrow screens ───────────────────────
    function wireSamples() {
        const main = $('main');
        const R = window.SamplesRouter;
        if (!main || !main.classList.contains('samples') || !R) return;
        const root = document.documentElement;
        const single = mq('(max-width: 899.98px)');
        let listScroll = 0;

        const title = document.querySelector('.topbar .tb-title');
        const back = document.createElement('button');
        back.type = 'button';
        back.className = 'tb-back';
        back.id = 'detail-back';
        back.setAttribute('data-testid', 'detail-back');
        back.setAttribute('aria-label', 'Back to the samples list');
        const chev = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
        chev.setAttribute('viewBox', '0 0 24 24');
        chev.setAttribute('aria-hidden', 'true');
        const path = document.createElementNS('http://www.w3.org/2000/svg', 'path');
        path.setAttribute('d', 'm15 5-7 7 7 7');
        chev.appendChild(path);
        back.appendChild(chev);
        back.appendChild(document.createTextNode('Samples'));
        if (title && title.parentNode) title.parentNode.insertBefore(back, title);

        function detailNow() { return R.parse(location.pathname, location.search).sampleId != null; }
        function sync() {
            const open = detailNow();
            if (open === root.classList.contains('s-detail-open')) return;
            if (open) {
                listScroll = window.scrollY;
                root.classList.add('s-detail-open');
                if (single.matches) window.scrollTo(0, 0);
            } else {
                root.classList.remove('s-detail-open');
                if (single.matches) window.scrollTo(0, listScroll);
            }
            resizeCharts();
        }
        // first state, before the router's first render
        root.classList.toggle('s-detail-open', detailNow());
        // Back to the list is a step forward in the history (the list's
        // filters kept), so the browser's Back returns to the sample: the
        // detail may have been reached through several views, or a link.
        back.addEventListener('click', () => {
            const st = R.parse(location.pathname, location.search);
            history.pushState(null, '', R.build(Object.assign({}, st, { sampleId: null, standard: null, view: 'overview' })));
            window.dispatchEvent(new PopStateEvent('popstate', { state: null }));
        });
        // the router changes the address bar, then marks the row and fills the
        // detail: any of those is when to look again
        new MutationObserver(sync).observe(main, { attributes: true, subtree: true, attributeFilter: ['class', 'hidden'] });
        window.addEventListener('popstate', () => setTimeout(sync, 0));
    }

    // the Samples page's first paint already shows the right pane (this file
    // loads after samples_router.js, at the end of <body>)
    if (window.SamplesRouter && document.querySelector('main.samples')) {
        document.documentElement.classList.toggle('s-detail-open',
            window.SamplesRouter.parse(location.pathname, location.search).sampleId != null);
    }

    document.addEventListener('DOMContentLoaded', () => {
        trackTopbar();
        wireDrawer();
        wireStatus();
        wireSamples();
        window.addEventListener('resize', resizeCharts);
        window.addEventListener('orientationchange', () => setTimeout(resizeCharts, 150));
        window.GCMobile = { open: () => setDrawer(true), close: () => setDrawer(false), isOpen: drawerOpen, resizeCharts };
    });
})();
