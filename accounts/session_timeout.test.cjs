const { test } = require('node:test');
const assert = require('node:assert/strict');
const { readFileSync } = require('node:fs');
const { join } = require('node:path');
const { runInNewContext } = require('node:vm');

function setup({ remaining = 1800, warning = 120, responses = [] } = {}) {
    const timers = []; const requests = []; const navigations = [];
    const stay = { disabled: false, listeners: {}, addEventListener(n, cb) { this.listeners[n] = cb; } };
    const box = {
        hidden: true,
        dataset: { remaining: String(remaining), warning: String(warning), keepaliveUrl: '/accounts/session/keepalive/', loginUrl: '/accounts/login/?expired=idle' },
        querySelector(sel) { return sel === '[data-session-stay]' ? stay : { value: 'csrf-token' }; },
    };
    const window = {
        setTimeout(cb, ms) { timers.push({ cb, ms }); return timers.length; },
        clearTimeout() {},
        location: { assign(url) { navigations.push(url); } },
        fetch(url, options) {
            requests.push({ url, options });
            const next = responses.shift() || { ok: true, body: { remaining: 0 } };
            return Promise.resolve({ ok: next.ok, json: () => Promise.resolve(next.body) });
        },
    };
    const document = { querySelector: () => box };
    runInNewContext(readFileSync(join(__dirname, '../static/js/session-timeout.js'), 'utf8'), { document, window, parseInt });
    return { box, stay, timers, requests, navigations };
}
const flush = () => new Promise((resolve) => setImmediate(resolve));

test('warning is scheduled before expiry and no request is made up front', () => {
    const { timers, requests, box } = setup();
    assert.equal(requests.length, 0);
    assert.equal(timers[0].ms, (1800 - 120) * 1000);
    assert.equal(box.hidden, true);
});

test('status checks are passive and activity in another tab only reschedules', async () => {
    const { timers, requests, box, navigations } = setup({ responses: [{ ok: true, body: { remaining: 1500 } }] });
    timers[0].cb(); await flush();
    assert.equal(requests[0].options.method, 'GET');
    assert.equal(requests[0].options.headers['X-Session-Activity'], 'passive');
    assert.equal(box.hidden, true);
    assert.equal(navigations.length, 0);
    assert.equal(timers[1].ms, (1500 - 120) * 1000);
});

test('warning shows when little time remains and expiry redirects to login', async () => {
    const { timers, box, navigations } = setup({ responses: [{ ok: true, body: { remaining: 100 } }, { ok: false, body: {} }] });
    timers[0].cb(); await flush();
    assert.equal(box.hidden, false);
    timers[1].cb(); await flush();
    assert.deepEqual(navigations, ['/accounts/login/?expired=idle']);
});

test('stay signed in posts with CSRF header and is the only activity request', async () => {
    const { stay, requests, box } = setup({ remaining: 60, responses: [{ ok: true, body: { remaining: 1800 } }] });
    assert.equal(box.hidden, false);
    stay.listeners.click(); await flush();
    assert.equal(requests[0].options.method, 'POST');
    assert.equal(requests[0].options.headers['X-CSRFToken'], 'csrf-token');
    assert.equal(requests[0].options.headers['X-Session-Activity'], undefined);
    assert.equal(box.hidden, true);
});
