(() => {
    "use strict";
    document.querySelectorAll("[data-password-toggle]").forEach((button) => {
        const input = document.getElementById(button.dataset.passwordToggle);
        if (!input) return;
        const label = button.getAttribute("aria-label").replace(/^Show /, "");
        button.hidden = false;
        button.addEventListener("click", () => {
            const visible = input.type === "password";
            input.type = visible ? "text" : "password";
            button.textContent = visible ? "Hide" : "Show";
            button.setAttribute("aria-pressed", String(visible));
            button.setAttribute("aria-label", `${visible ? "Hide" : "Show"} ${label}`);
        });
    });
    const resetForms = () => document.querySelectorAll("[data-auth-form]").forEach((form) => {
        form.removeAttribute("aria-busy");
        form.querySelectorAll("button[type='submit']").forEach((button) => {
            button.disabled = false;
            if (button.dataset.originalText) button.textContent = button.dataset.originalText;
        });
    });
    document.querySelectorAll("[data-auth-form]").forEach((form) => {
        form.addEventListener("submit", (event) => {
            if (form.getAttribute("aria-busy") === "true") {
                event.preventDefault();
                return;
            }
            form.setAttribute("aria-busy", "true");
            form.querySelectorAll("button[type='submit']").forEach((button) => {
                button.dataset.originalText = button.textContent;
                button.textContent = button.dataset.busyText || "Please wait…";
                button.disabled = true;
            });
        });
    });
    window.addEventListener("pageshow", resetForms);
})();
