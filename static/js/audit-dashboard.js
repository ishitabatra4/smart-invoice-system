document.addEventListener("DOMContentLoaded", () => {
    const dataElement = document.getElementById("audit-chart-data");
    if (!dataElement) return;
    const ids = ["auditDailyChart", "auditModuleChart", "auditActionChart", "auditUserChart", "auditSecurityChart"];
    const message = (canvas, text) => {
        canvas.hidden = true;
        const note = document.createElement("p");
        note.className = "audit-empty";
        note.textContent = text;
        canvas.parentElement.appendChild(note);
    };
    let data;
    try {
        data = JSON.parse(dataElement.textContent);
        if (!["daily", "modules", "actions", "users", "security"].every(key => Array.isArray(data?.[key]?.labels) && Array.isArray(data?.[key]?.values) && data[key].labels.length === data[key].values.length && data[key].values.every(value => Number.isFinite(Number(value))))) throw new Error("Unsupported chart data");
    } catch (_) {
        ids.forEach(id => { const canvas = document.getElementById(id); if (canvas) message(canvas, "Chart data could not be loaded."); });
        return;
    }

    const colors = ["#477c68", "#c8895d", "#65758b", "#93a997", "#bd6d61", "#a0a29a", "#d4ae77"];
    const render = (id, type, values, options = {}) => {
        const canvas = document.getElementById(id);
        if (!canvas) return;
        if (!window.Chart) {
            message(canvas, "Charts are unavailable. Refresh to try again.");
            return;
        }
        if (!values.labels.length) {
            message(canvas, "No activity in this period.");
            return;
        }
        const datasets = [{
            label: options.label || "Events",
            data: values.values,
            backgroundColor: type === "line" ? "rgba(71,124,104,.12)" : values.labels.map((_, i) => colors[i % colors.length]),
            borderColor: type === "line" ? colors[0] : values.labels.map((_, i) => colors[i % colors.length]),
            borderWidth: type === "line" ? 2 : 1,
            fill: type === "line",
            tension: .32,
            pointRadius: type === "line" ? 3 : 0,
        }];
        try { new Chart(canvas, {
            type,
            data: { labels: values.labels, datasets },
            options: {
                responsive: true,
                maintainAspectRatio: false,
                indexAxis: options.indexAxis || "x",
                plugins: { legend: { display: type === "doughnut", position: "bottom" }, tooltip: { mode: "index", intersect: false } },
                scales: type === "doughnut" ? {} : { y: { beginAtZero: true, ticks: { precision: 0 }, grid: { color: "#e9ece8" } }, x: { grid: { display: false } } },
            },
        }); } catch (_) { message(canvas, "This chart could not be rendered."); }
    };

    render("auditDailyChart", "line", data.daily, { label: "Activity" });
    render("auditModuleChart", "doughnut", data.modules);
    render("auditActionChart", "doughnut", data.actions);
    render("auditUserChart", "bar", data.users, { indexAxis: "y", label: "Events" });
    render("auditSecurityChart", "bar", data.security, { label: "Security events" });
});
