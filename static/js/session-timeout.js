/* Pre-expiry warning for the server-enforced idle timeout.
 * The server is authoritative. This script never stores form data or tokens.
 * Status checks are sent as passive requests (they never extend the session) and
 * only happen at the warning and expiry moments; "Stay signed in" is the only
 * request that records activity. */
(function () {
    'use strict';
    var box = document.querySelector('[data-session-timeout]');
    if (!box) { return; }
    var remaining = parseInt(box.dataset.remaining, 10);
    var warning = parseInt(box.dataset.warning, 10);
    if (!(remaining > 0) || !(warning >= 0)) { return; }
    var timer = null;
    var stay = box.querySelector('[data-session-stay]');
    function csrfToken() {
        var input = box.querySelector('input[name="csrfmiddlewaretoken"]');
        return input ? input.value : '';
    }
    function expire() { window.location.assign(box.dataset.loginUrl); }
    function request(method) {
        var headers = { 'Accept': 'application/json' };
        if (method === 'POST') { headers['X-CSRFToken'] = csrfToken(); } else { headers['X-Session-Activity'] = 'passive'; }
        return window.fetch(box.dataset.keepaliveUrl, { method: method, credentials: 'same-origin', headers: headers })
            .then(function (response) {
                if (!response.ok) { throw new Error('expired'); }
                return response.json();
            }).then(function (data) { return parseInt(data.remaining, 10) || 0; });
    }
    function check() {
        request('GET').then(function (seconds) {
            if (seconds <= 0) { expire(); } else { schedule(seconds); }
        }).catch(expire);
    }
    function schedule(seconds) {
        window.clearTimeout(timer);
        if (warning > 0 && seconds > warning) {
            box.hidden = true;
            timer = window.setTimeout(check, (seconds - warning) * 1000);
        } else {
            box.hidden = warning === 0;
            timer = window.setTimeout(check, seconds * 1000 + 1000);
        }
    }
    if (stay) {
        stay.addEventListener('click', function () {
            stay.disabled = true;
            request('POST').then(function (seconds) { stay.disabled = false; schedule(seconds); }).catch(expire);
        });
    }
    schedule(remaining);
}());
