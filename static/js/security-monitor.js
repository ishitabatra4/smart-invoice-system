/* Progressive enhancement only: server validation and CSRF remain authoritative. */
(function (root) {
  'use strict';
  function validRange(start, end) {
    if (!start || !end) return true;
    const a = Date.parse(start), b = Date.parse(end);
    return Number.isFinite(a) && Number.isFinite(b) && b >= a && b - a <= 365 * 86400000;
  }
  function attach(doc) {
    doc.querySelectorAll('[data-security-filter]').forEach(function (form) {
      form.addEventListener('submit', function (event) {
        const start = form.querySelector('[name="start_date"]');
        const end = form.querySelector('[name="end_date"]');
        if (start && end && !validRange(start.value, end.value)) {
          event.preventDefault(); end.setCustomValidity('Choose an ordered range of at most 366 days.'); end.reportValidity();
        }
      });
      form.addEventListener('input', function () {
        const end = form.querySelector('[name="end_date"]'); if (end) end.setCustomValidity('');
      });
    });
    doc.querySelectorAll('[data-security-confirm]').forEach(function (form) {
      form.addEventListener('submit', function (event) {
        if (!root.confirm('Record this security decision?')) event.preventDefault();
      });
    });
  }
  if (typeof module !== 'undefined') module.exports = { validRange: validRange, attach: attach };
  if (root.document) attach(root.document);
})(typeof window !== 'undefined' ? window : globalThis);
