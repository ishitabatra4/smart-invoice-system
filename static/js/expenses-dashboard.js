(() => {
    const monthlyNode = document.getElementById("expense-monthly-data");
    const categoryNode = document.getElementById("expense-category-data");
    if (!monthlyNode || !categoryNode) return;

    const emptyState = (canvas, message) => {
        canvas.hidden = true;
        const note = document.createElement("div");
        note.className = "chart-empty-state";
        note.textContent = message;
        canvas.parentElement.appendChild(note);
    };
    if (typeof Chart === "undefined") {
        document.querySelectorAll(".expense-chart-wrap canvas").forEach((canvas) => emptyState(canvas, "Charts could not load. Refresh to try again."));
        return;
    }

    const monthly = JSON.parse(monthlyNode.textContent || "[]");
    const categories = JSON.parse(categoryNode.textContent || "[]");
    const currency = new Intl.NumberFormat("en-IN", { style: "currency", currency: "INR", maximumFractionDigits: 2 });
    const compact = new Intl.NumberFormat("en-IN", { style: "currency", currency: "INR", notation: "compact", maximumFractionDigits: 1 });
    const common = {
        responsive: true,
        maintainAspectRatio: false,
        plugins: {
            legend: { position: "bottom", labels: { usePointStyle: true, boxWidth: 8, padding: 16, color: "#596057" } },
            tooltip: { callbacks: { label: (context) => `${context.dataset.label || context.label}: ${currency.format(Number(context.parsed.y ?? context.parsed ?? 0))}` } },
        },
        scales: {
            x: { grid: { display: false }, ticks: { color: "#73796f", maxRotation: 0 } },
            y: { beginAtZero: true, grid: { color: "#eceee8" }, ticks: { color: "#73796f", callback: (value) => compact.format(value) } },
        },
    };

    const trend = document.getElementById("expenseTrendChart");
    const monthValues = monthly.map((item) => Number(item.amount || 0));
    if (trend) {
        if (!monthValues.some((value) => value !== 0)) emptyState(trend, "No expense records for this period.");
        else new Chart(trend, { type: "bar", data: { labels: monthly.map((item) => item.label), datasets: [{ label: "Expenses", data: monthValues, backgroundColor: "#758d7e", borderRadius: 6, maxBarThickness: 44 }] }, options: common });
    }

    const categoryCanvas = document.getElementById("expenseCategoryChart");
    const categoryValues = categories.map((item) => Number(item[1] || 0));
    if (categoryCanvas) {
        if (!categoryValues.some((value) => value !== 0)) emptyState(categoryCanvas, "No expense records for this period.");
        else new Chart(categoryCanvas, {
            type: "doughnut",
            data: {
                labels: categories.map((item) => item[0]),
                datasets: [{ data: categoryValues, backgroundColor: ["#526f64", "#789487", "#c28a64", "#c8a466", "#9ba89b", "#d0d1c8", "#ab7961", "#657d71"], borderColor: "#fff", borderWidth: 3, hoverOffset: 5 }],
            },
            options: { ...common, cutout: "64%", scales: {}, plugins: { ...common.plugins, tooltip: { callbacks: { label: (context) => `${context.label}: ${currency.format(context.parsed)}` } } }, },
        });
    }

    const period = document.getElementById("expense-period");
    if (period) period.addEventListener("change", () => {
        if (period.value !== "custom") period.form.submit();
        else document.querySelector(".expense-custom-dates")?.classList.remove("d-none");
    });
})();
