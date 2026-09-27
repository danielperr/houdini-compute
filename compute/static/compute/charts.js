// Renders the Overview charts from data-* attributes. Re-runs after htmx swaps.
(function () {
  const charts = new Set();

  function css(name) {
    return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  }

  function fmt(v) {
    return Number(v).toLocaleString(undefined, { maximumFractionDigits: 1 });
  }

  function baseOptions() {
    return {
      animation: false,
      textStyle: { fontFamily: 'system-ui, -apple-system, "Segoe UI", sans-serif', color: css("--ink-2") },
      grid: { left: 8, right: 24, top: 28, bottom: 8, containLabel: true },
      tooltip: {
        backgroundColor: css("--surface"),
        borderColor: css("--border"),
        textStyle: { color: css("--ink") },
      },
    };
  }

  function narrow(el) {
    return el.clientWidth < 480;
  }

  function axisStyle() {
    return {
      axisLine: { lineStyle: { color: css("--axis") } },
      axisTick: { show: false },
      axisLabel: { color: css("--muted"), hideOverlap: true },
      splitLine: { lineStyle: { color: css("--grid"), width: 1 } },
    };
  }

  function ladder(el) {
    const values = JSON.parse(el.dataset.values);
    const labels = ["Allocatable", "Tenant quota", "Requested", "Used (avg)"];
    const colors = [css("--context"), css("--seq-light"), css("--series-1"), css("--series-2")];
    return Object.assign(baseOptions(), {
      grid: { left: 8, right: 56, top: 8, bottom: 8, containLabel: true },
      tooltip: Object.assign(baseOptions().tooltip, {
        trigger: "item",
        formatter: (p) => `${p.name}: <b>${fmt(p.value)}</b> ${el.dataset.unit}`,
      }),
      xAxis: Object.assign({ type: "value", splitNumber: narrow(el) ? 3 : 5 }, axisStyle()),
      yAxis: Object.assign({ type: "category", data: labels, inverse: true }, axisStyle(), {
        splitLine: { show: false },
        axisLabel: { color: css("--ink-2") },
      }),
      series: [{
        type: "bar",
        barMaxWidth: 22,
        data: values.map((v, i) => ({ value: v, itemStyle: { color: colors[i], borderRadius: [0, 4, 4, 0] } })),
        label: { show: true, position: "right", color: css("--ink-2"), formatter: (p) => fmt(p.value) },
      }],
    });
  }

  function trend(el) {
    const data = JSON.parse(document.getElementById(el.dataset.source).textContent);
    const line = (name, values, color, extra) => Object.assign({
      name, type: "line", data: values, showSymbol: false, symbolSize: 8,
      lineStyle: { width: 2, color }, itemStyle: { color },
    }, extra || {});
    const threshold = data.x.map(() => data.threshold);
    // "2026-09-27 13:00" -> "09-27 13:00"; the year only adds clipping.
    const short = (v) => v.slice(5);
    return Object.assign(baseOptions(), {
      grid: { left: 8, right: 24, top: narrow(el) ? 52 : 32, bottom: 8, containLabel: true },
      legend: { top: 0, left: 0, textStyle: { color: css("--ink-2") }, icon: "roundRect", itemHeight: 4 },
      tooltip: Object.assign(baseOptions().tooltip, {
        trigger: "axis",
        axisPointer: { type: "line", lineStyle: { color: css("--axis") } },
        valueFormatter: (v) => `${fmt(v)} ${el.dataset.unit}`,
      }),
      xAxis: Object.assign({ type: "category", data: data.x, boundaryGap: false }, axisStyle(), {
        splitLine: { show: false },
        axisLabel: { color: css("--muted"), hideOverlap: true, formatter: short },
      }),
      yAxis: Object.assign({ type: "value", min: 0 }, axisStyle()),
      series: [
        line("Allocatable", data.allocatable, css("--context")),
        line("Reserve threshold", threshold, css("--muted"), { lineStyle: { width: 1, color: css("--muted") } }),
        line("Requested", data.requested, css("--series-1"), { areaStyle: { color: css("--series-1"), opacity: 0.1 } }),
        line("Used (avg)", data.used, css("--series-2")),
      ],
    });
  }

  const builders = { ladder, trend };

  function render(root) {
    if (!window.echarts) return;
    for (const c of charts) { if (!document.body.contains(c.getDom())) { c.dispose(); charts.delete(c); } }
    root.querySelectorAll("[data-chart]").forEach((el) => {
      const chart = echarts.init(el, null, { renderer: "svg" });
      chart.setOption(builders[el.dataset.chart](el));
      charts.add(chart);
    });
  }

  document.addEventListener("DOMContentLoaded", () => render(document));
  document.addEventListener("htmx:afterSettle", (e) => render(e.detail.elt));
  window.addEventListener("resize", () => charts.forEach((c) => c.resize()));
  window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => {
    charts.forEach((c) => { c.dispose(); });
    charts.clear();
    render(document);
  });
})();
