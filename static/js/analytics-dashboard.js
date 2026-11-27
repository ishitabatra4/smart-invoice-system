(function () {
    const period = document.getElementById("period");
    const customRange = document.getElementById("custom-range");
    if (period && customRange) {
        const edited = document.getElementById("report-dates-edited");
        ["report-start-date", "report-end-date"].forEach(id => {
            const field = document.getElementById(id);
            if (field) ["input", "change"].forEach(event => field.addEventListener(event, () => {
                period.value = "custom";
                if (edited) edited.value = "1";
                customRange.classList.toggle("d-none", false);
            }));
        });
        period.addEventListener("change", () => {
            if (edited) edited.value = "0";
            customRange.classList.toggle("d-none", period.value !== "custom");
            if (period.value !== "custom") period.form.submit();
        });
    }

    const dataNode = document.getElementById("analytics-chart-data");
    if (!dataNode) return;
    const showChartMessage = (canvas, message) => {
        canvas.hidden = true;
        const empty = document.createElement("div");
        empty.className = "chart-empty-state";
        empty.textContent = message;
        canvas.parentElement.appendChild(empty);
    };
    if (typeof Chart === "undefined") {
        document.querySelectorAll(".chart-wrap canvas").forEach((canvas) => {
            showChartMessage(canvas, "Charts could not be loaded. Refresh to try again.");
        });
        return;
    }

    let data;
    try {
        data = JSON.parse(dataNode.textContent);
        const series = [
            [data?.labels, data?.monthly_sales_purchases?.sales], [data?.labels, data?.monthly_sales_purchases?.purchases],
            [data?.labels, data?.monthly_cash?.inflow], [data?.labels, data?.monthly_cash?.outflow],
            [data?.labels, data?.expenses], [data?.labels, data?.gst_trend?.output], [data?.labels, data?.gst_trend?.input],
            [data?.labels, data?.taxable?.sales], [data?.labels, data?.taxable?.purchases],
            [data?.invoice_status?.labels, data?.invoice_status?.values], [data?.gst_breakdown?.labels, data?.gst_breakdown?.values],
        ];
        if (!series.every(([labels, amounts]) => Array.isArray(labels) && Array.isArray(amounts) && labels.length === amounts.length && amounts.every(value => Number.isFinite(Number(value)))) ||
            !Array.isArray(data.outstanding) || data.outstanding.length !== 2 || !data.outstanding.every(value => Number.isFinite(Number(value))) ||
            ![data.customers, data.suppliers].every(rows => Array.isArray(rows) && rows.every(row => Array.isArray(row) && row.length === 2 && Number.isFinite(Number(row[1]))))) {
            throw new Error("Unsupported chart data");
        }
    } catch (error) {
        console.error("Could not read financial analytics chart data.", error);
        document.querySelectorAll(".chart-wrap canvas").forEach((canvas) => {
            showChartMessage(canvas, "Chart data could not be loaded.");
        });
        return;
    }
    const colors = {
        sales: "#526f64", purchase: "#8d9b8d", inflow: "#477a69",
        outflow: "#c28a64", blue: "#789487", gold: "#c8a466",
        red: "#bd7469", purple: "#938f7d", gray: "#c9c8bf",
    };
    const currency = new Intl.NumberFormat("en-IN", {
        style: "currency", currency: "INR", maximumFractionDigits: 2,
    });
    const values = (items) => (items || []).map((value) => Number(value || 0));
    const baseOptions = {
        responsive: true,
        maintainAspectRatio: false,
        interaction: { intersect: false, mode: "index" },
        plugins: {
            legend: { position: "bottom", labels: { usePointStyle: true, boxWidth: 8, padding: 18, color: "#596057", font: { size: 11 } } },
            tooltip: { callbacks: { label: (context) => {
                const parsed = context.parsed;
                const amount = typeof parsed === "number" ? parsed : context.chart.options.indexAxis === "y" ? parsed.x : parsed.y;
                return `${context.dataset.label}: ${currency.format(amount ?? 0)}`;
            } } },
        },
        scales: {
            x: { grid: { display: false }, title: { display: true, text: "Reporting month", color: "#777a73", font: { size: 10, weight: "600" } }, ticks: { color: "#777a73", maxRotation: 0 } },
            y: { beginAtZero: true, grid: { color: "#eeefe9" }, title: { display: true, text: "Amount (INR)", color: "#777a73", font: { size: 10, weight: "600" } }, ticks: { color: "#777a73", callback: (value) => compactCurrency.format(value) } },
        },
    };
    const compactCurrency = new Intl.NumberFormat("en-IN", {
        style: "currency", currency: "INR", notation: "compact", maximumFractionDigits: 1,
    });
    const create = (id, type, labels, datasets, options = {}) => {
        const canvas = document.getElementById(id);
        if (!canvas) return;
        const hasData = datasets.some((dataset) => (dataset.data || []).some((value) => Number(value || 0) !== 0));
        if (!hasData) {
            canvas.hidden = true;
            const empty = document.createElement("div");
            empty.className = "chart-empty-state";
            empty.textContent = "No records available for the selected period.";
            canvas.parentElement.appendChild(empty);
            return;
        }
        try {
            new Chart(canvas, {
                type,
                data: { labels, datasets },
                options: {
                    ...baseOptions,
                    ...options,
                    plugins: { ...baseOptions.plugins, ...(options.plugins || {}) },
                },
            });
        } catch (error) {
            console.error(`Could not render analytics chart ${id}.`, error);
            showChartMessage(canvas, "This chart could not be rendered.");
        }
    };
    const lineDataset = (label, valuesList, color) => ({
        label, data: values(valuesList), borderColor: color, backgroundColor: color,
        borderWidth: 2, pointRadius: 3, pointHoverRadius: 5, tension: 0.3,
    });
    const barDataset = (label, valuesList, color) => ({
        label, data: values(valuesList), backgroundColor: color, borderRadius: 5,
        categoryPercentage: 0.68, barPercentage: 0.82, maxBarThickness: 38,
    });
    const horizontalCurrencyAxis = {
        scales: {
            x: { beginAtZero: true, grid: { color: "#eeefe9" }, title: { display: true, text: "Amount (INR)", color: "#777a73", font: { size: 10, weight: "600" } }, ticks: { color: "#777a73", callback: (value) => compactCurrency.format(value) } },
            y: { grid: { display: false }, ticks: { color: "#596057" } },
        },
    };

    create("salesPurchasesChart", "bar", data.labels, [
        barDataset("Sales", data.monthly_sales_purchases.sales, colors.sales),
        barDataset("Purchases", data.monthly_sales_purchases.purchases, colors.purchase),
    ]);
    create("cashFlowChart", "bar", data.labels, [
        barDataset("Cash received", data.monthly_cash.inflow, colors.inflow),
        barDataset("Cash paid", data.monthly_cash.outflow, colors.outflow),
    ]);
    const statusColors = {
        Uploaded: colors.gray,
        Processing: colors.blue,
        "Needs review": colors.gold,
        Verified: colors.sales,
        Paid: colors.purple,
    };
    create("invoiceStatusChart", "doughnut", data.invoice_status.labels, [{
        label: "Invoices", data: values(data.invoice_status.values),
        backgroundColor: data.invoice_status.labels.map((label) => statusColors[label] || colors.gray),
        borderWidth: 0,
    }], {
        cutout: "66%",
        plugins: { tooltip: { callbacks: { label: (context) => `${context.label}: ${context.parsed}` } } },
        scales: {},
    });
    create("expensesChart", "bar", data.labels, [barDataset("Expenses", data.expenses, colors.outflow)]);
    create("outstandingChart", "bar", ["Current balance"], [
        barDataset("Receivables", [data.outstanding[0]], colors.sales),
        barDataset("Payables", [data.outstanding[1]], colors.outflow),
    ], {
        plugins: { legend: { position: "bottom" } },
        scales: {
            x: { grid: { display: false }, title: { display: true, text: "Balance type", color: "#777a73", font: { size: 10, weight: "600" } }, ticks: { color: "#596057" } },
            y: { beginAtZero: true, grid: { color: "#eeefe9" }, title: { display: true, text: "Amount (INR)", color: "#777a73", font: { size: 10, weight: "600" } }, ticks: { color: "#777a73", callback: (value) => compactCurrency.format(value) } },
        },
    });
    create("customersChart", "bar", data.customers.map((item) => item[0]), [barDataset("Sales", data.customers.map((item) => item[1]), colors.sales)], {
        indexAxis: "y", plugins: { legend: { display: false } }, scales: horizontalCurrencyAxis.scales,
    });
    create("suppliersChart", "bar", data.suppliers.map((item) => item[0]), [barDataset("Purchases", data.suppliers.map((item) => item[1]), colors.purchase)], {
        indexAxis: "y", plugins: { legend: { display: false } }, scales: horizontalCurrencyAxis.scales,
    });
    create("gstTrendChart", "line", data.labels, [
        lineDataset("Output GST", data.gst_trend.output, colors.sales),
        lineDataset("Input GST", data.gst_trend.input, colors.blue),
    ]);
    create("gstBreakdownChart", "doughnut", data.gst_breakdown.labels, [{
        label: "GST component",
        data: values(data.gst_breakdown.values),
        backgroundColor: [colors.sales, colors.inflow, colors.outflow, "#a7b9ae", "#d7c7a0", "#9fa79a"],
        borderColor: "#ffffff",
        borderWidth: 3,
        hoverOffset: 5,
    }], {
        cutout: "62%",
        plugins: {
            legend: { position: "bottom", labels: { boxWidth: 8, padding: 12 } },
            tooltip: { callbacks: { label: (context) => `${context.label}: ${currency.format(context.parsed ?? 0)}` } },
        },
        scales: {},
    });
    create("taxableTrendChart", "bar", data.labels, [
        barDataset("Taxable sales", data.taxable.sales, colors.sales),
        barDataset("Taxable purchases", data.taxable.purchases, colors.purchase),
    ]);

})();
