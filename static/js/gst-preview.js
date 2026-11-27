/* Monetary values come from Python as decimal strings. No browser money arithmetic. */
(function (root) {
    "use strict";
    function summary(result) {
        return `Value before GST ₹${result.taxable_amount} · Discounts ₹${result.total_discount_amount} · CGST ₹${result.cgst_amount} · SGST ₹${result.sgst_amount} · IGST ₹${result.igst_amount} · Total ₹${result.total_amount}`;
    }
    function errors(value) {
        if (Array.isArray(value)) return value.flatMap(errors);
        if (value && typeof value === "object") return Object.values(value).flatMap(errors);
        return value ? [String(value)] : [];
    }
    function attach(form, endpoint, target) {
        let timer, controller, generation = 0;
        function schedule() {
            generation += 1;
            const current = generation;
            clearTimeout(timer);
            if (controller) controller.abort();
            timer = setTimeout(async () => {
                controller = new AbortController();
                const body = new FormData(form);
                body.delete("attachment");
                if (form.dataset.invoiceId) body.set("invoice_id", form.dataset.invoiceId);
                target.setAttribute("aria-busy", "true");
                try {
                    const response = await fetch(endpoint, {method: "POST", body, credentials: "same-origin", signal: controller.signal});
                    const result = await response.json();
                    if (generation !== current) return;
                    target.textContent = response.ok ? summary(result) : errors(result.errors).join(" ");
                } catch (error) {
                    if (error.name !== "AbortError" && generation === current) target.textContent = "Preview unavailable. Saving the draft will still validate amounts on the server.";
                } finally {
                    if (generation === current) target.removeAttribute("aria-busy");
                }
            }, 350);
        }
        form.addEventListener("input", schedule);
        form.addEventListener("change", schedule);
        schedule();
    }
    const api = {summary, errors, attach};
    if (typeof module !== "undefined" && module.exports) module.exports = api;
    root.SmartInvoiceGSTPreview = api;
    if (typeof document !== "undefined") document.querySelectorAll("[data-gst-preview-url]").forEach((form) => {
        const target = document.getElementById(form.dataset.gstPreviewTarget);
        if (target) attach(form, form.dataset.gstPreviewUrl, target);
    });
})(typeof globalThis !== "undefined" ? globalThis : this);
