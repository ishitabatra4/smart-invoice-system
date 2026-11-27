/* Presentation only: never replace server validation, idempotency or authorization. */
(function (root) {
    'use strict';
    function attach(doc) {
        const pending = new Map();
        function restore(form) {
            const state = pending.get(form);
            if (!state) return;
            form.removeAttribute('aria-busy');
            state.buttons.forEach(button => button.removeAttribute('aria-disabled'));
            state.status.remove(); pending.delete(form);
        }
        doc.querySelectorAll('form[method="post" i]').forEach(form => {
            // The manual invoice editor already owns its specialized submit state.
            if (form.id === 'manual-invoice-form') return;
            form.addEventListener('submit', event => {
                if (event.defaultPrevented) return;
                if (pending.has(form)) { event.preventDefault(); return; }
                const buttons = Array.from(form.querySelectorAll('button:not([type="button"]):not([type="reset"]), input[type="submit"]'));
                const status = doc.createElement('span');
                status.className = 'workspace-submit-status'; status.setAttribute('role', 'status'); status.textContent = 'Working…';
                form.appendChild(status); form.setAttribute('aria-busy', 'true');
                // Keep native submitters enabled so their name/value still reaches Django.
                buttons.forEach(button => button.setAttribute('aria-disabled', 'true'));
                pending.set(form, {buttons, status});
                Promise.resolve().then(() => { if (event.defaultPrevented) restore(form); });
            });
        });
        if (root.addEventListener) root.addEventListener('pageshow', () => Array.from(pending.keys()).forEach(restore));
        const menu = doc.querySelector('[data-workspace-menu]');
        if (menu && root.matchMedia) {
            const media = root.matchMedia('(max-width: 900px)');
            const resize = () => { menu.open = !media.matches; };
            resize(); media.addEventListener('change', resize);
            menu.addEventListener('keydown', event => {
                if (event.key === 'Escape' && media.matches && menu.open) {
                    menu.open = false; menu.querySelector('summary').focus();
                }
            });
        }
        const groups = Array.from(doc.querySelectorAll('[data-nav-accordion]'));
        const animations = new Map();
        function expand(group, open) {
            const running = animations.get(group);
            if (running) running.cancel();
            const start = group.getBoundingClientRect().height;
            const summary = group.querySelector('summary');
            group.open = true;
            const end = open ? group.scrollHeight : summary.getBoundingClientRect().height;
            const reduced = root.matchMedia && root.matchMedia('(prefers-reduced-motion: reduce)').matches;
            if (!group.animate || reduced) { group.open = open; group.style.overflow = ''; animations.delete(group); return; }
            group.style.overflow = 'hidden';
            const animation = group.animate([{height: start + 'px'}, {height: end + 'px'}], {duration: 180, easing: 'ease-out'});
            animation.opening = open;
            animations.set(group, animation);
            animation.onfinish = () => { if (animations.get(group) !== animation) return; group.open = open; group.style.overflow = ''; animations.delete(group); };
            animation.oncancel = () => { if (animations.get(group) !== animation) return; group.style.overflow = ''; animations.delete(group); };
        }
        groups.forEach(group => group.querySelector('summary').addEventListener('click', event => {
            event.preventDefault();
            const running = animations.get(group);
            const opening = running ? !running.opening : !group.open;
            if (opening) groups.forEach(other => { if (other !== group && other.open) expand(other, false); });
            expand(group, opening);
        }));
        if (menu) menu.addEventListener('click', event => {
            if (event.target.closest && event.target.closest('a[href]') && root.matchMedia && root.matchMedia('(max-width: 900px)').matches) menu.open = false;
        });
        function revealAnchor() {
            if (!root.location || !root.location.hash || !doc.getElementById) return;
            const target = doc.getElementById(root.location.hash.slice(1));
            if (!target) return;
            const links = Array.from(doc.querySelectorAll('.workspace-navigation a[href]'));
            const selected = links.find(link => link.hash === root.location.hash && link.pathname === root.location.pathname && link.search === root.location.search);
            if (selected) {
                links.forEach(link => { link.classList.remove('is-active'); link.removeAttribute('aria-current'); });
                selected.classList.add('is-active'); selected.setAttribute('aria-current', 'page');
                groups.forEach(group => {
                    const active = group.contains(selected);
                    group.querySelector('summary').classList.toggle('is-parent-active', active);
                    if (active) group.open = true;
                });
            }
            let parent = target.parentElement;
            while (parent) { if (parent.tagName === 'DETAILS') parent.open = true; parent = parent.parentElement; }
            if (target.scrollIntoView) target.scrollIntoView({block: 'nearest'});
        }
        revealAnchor();
        if (root.addEventListener) root.addEventListener('hashchange', revealAnchor);
        function tables() {
            doc.querySelectorAll('.table-responsive').forEach(region => {
                if (region.scrollWidth > region.clientWidth) {
                    region.setAttribute('tabindex', '0'); region.setAttribute('role', 'region');
                    if (!region.hasAttribute('aria-label')) region.setAttribute('aria-label', 'Scrollable data table');
                } else { region.removeAttribute('tabindex'); region.removeAttribute('role'); }
            });
        }
        tables(); if (root.addEventListener) root.addEventListener('resize', tables);
        doc.querySelectorAll('details.workspace-secondary').forEach(details => details.addEventListener('toggle', () => {
            if (!details.open || !root.Chart || !root.Chart.getChart) return;
            details.querySelectorAll('canvas').forEach(canvas => { const chart = root.Chart.getChart(canvas); if (chart) chart.resize(); });
        }));
    }
    if (typeof module !== 'undefined') module.exports = {attach};
    if (root.document) attach(root.document);
})(typeof window !== 'undefined' ? window : globalThis);
