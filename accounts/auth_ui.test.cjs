const { test } = require('node:test');
const assert = require('node:assert/strict');
const { readFileSync } = require('node:fs');
const { join } = require('node:path');
const { runInNewContext } = require('node:vm');

function setup({ missingInput = false } = {}) {
    const input = { type: 'password' };
    function element(attributes = {}) {
        return {
            dataset: {}, attributes: { ...attributes }, listeners: {},
            textContent: '', disabled: false, hidden: true,
            getAttribute(name) { return this.attributes[name]; },
            setAttribute(name, value) { this.attributes[name] = value; },
            removeAttribute(name) { delete this.attributes[name]; },
            addEventListener(name, callback) { this.listeners[name] = callback; },
        };
    }
    const toggle = element({ 'aria-label': 'Show password', 'aria-pressed': 'false' });
    toggle.dataset.passwordToggle = 'id_password';
    const submit = element();
    submit.textContent = 'Sign in';
    submit.dataset.busyText = 'Signing in…';
    const form = element();
    form.querySelectorAll = () => [submit];
    const events = {};
    const document = {
        querySelectorAll(selector) { return selector === '[data-password-toggle]' ? [toggle] : [form]; },
        getElementById() { return missingInput ? null : input; },
    };
    const window = { addEventListener(name, callback) { events[name] = callback; } };
    runInNewContext(readFileSync(join(__dirname, '../static/js/auth.js'), 'utf8'), { document, window });
    return { input, toggle, submit, form, events };
}

test('password visibility toggle updates text and accessible state', () => {
    const { toggle, input } = setup();
    assert.equal(toggle.hidden, false);
    toggle.listeners.click();
    assert.equal(input.type, 'text');
    assert.equal(toggle.textContent, 'Hide');
    assert.equal(toggle.getAttribute('aria-pressed'), 'true');
    assert.equal(toggle.getAttribute('aria-label'), 'Hide password');
    toggle.listeners.click();
    assert.equal(input.type, 'password');
    assert.equal(toggle.getAttribute('aria-pressed'), 'false');
});

test('submit button shows pending state and repeat submissions are prevented', () => {
    const { form, submit } = setup();
    form.listeners.submit({ preventDefault() { throw new Error('first submission must proceed'); } });
    assert.equal(submit.disabled, true);
    assert.equal(submit.textContent, 'Signing in…');
    assert.equal(form.getAttribute('aria-busy'), 'true');
    let prevented = false;
    form.listeners.submit({ preventDefault() { prevented = true; } });
    assert.equal(prevented, true);
});

test('back navigation restores the form after a pending submission', () => {
    const { form, submit, events } = setup();
    form.listeners.submit({ preventDefault() {} });
    events.pageshow();
    assert.equal(submit.disabled, false);
    assert.equal(submit.textContent, 'Sign in');
    assert.equal(form.getAttribute('aria-busy'), undefined);
});

test('a missing toggle input is handled without a runtime error', () => {
    assert.equal(setup({ missingInput: true }).toggle.hidden, true);
});
