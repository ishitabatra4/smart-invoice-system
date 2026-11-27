(() => {
    "use strict";
    const form = document.getElementById("manual-invoice-form");
    if (form) {
        form.addEventListener("submit", (event) => {
            if (form.getAttribute("aria-busy") === "true") {
                event.preventDefault();
                return;
            }
            form.setAttribute("aria-busy", "true");
            const submit = form.querySelector("button[type='submit']");
            submit.disabled = true;
            submit.textContent = "Saving draft…";
        });
        window.addEventListener("pageshow", () => {
            form.removeAttribute("aria-busy");
            const submit = form.querySelector("button[type='submit']");
            submit.disabled = false;
            submit.textContent = "Save draft and preview";
        });
    }
    const button = document.getElementById("add-invoice-line");
    if (!button) return;
    button.addEventListener("click", () => {
        const count = document.getElementById("id_items-TOTAL_FORMS");
        const index = Number(count.value);
        if (index >= 100) {
            document.getElementById("line-limit-message").textContent = "An invoice supports up to 100 line items.";
            return;
        }
        const template = document.getElementById("empty-invoice-line").innerHTML.replaceAll("__prefix__", String(index));
        const rows = document.getElementById("invoice-line-rows");
        rows.insertAdjacentHTML("beforeend", template);
        count.value = String(index + 1);
        rows.lastElementChild.querySelector("input")?.focus();
    });
})();
