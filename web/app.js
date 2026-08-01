"use strict";

const TARGETS = ["gateway", "isp_hop", "cloudflare", "google", "quad9"];
const PUBLIC_TARGETS = ["cloudflare", "google", "quad9"];
const RANGE_TARGETS = ["isp_hop", "cloudflare", "google", "quad9"];
const MAX_CHART_RECORDS = 2016;
const COLORS = {
  gateway: "#49d5d0",
  isp_hop: "#f4bd61",
  cloudflare: "#69a7ff",
  google: "#ae8cff",
  quad9: "#ff7e9d",
};

let latestPayload = null;
let chartHistoryRecords = [];
const chartViews = {
  loss: { rangeHours: 24, browseWindow: null },
  latency: { rangeHours: 24, browseWindow: null },
};
const chartStates = new WeakMap();
const chartPanStates = new WeakMap();

function byId(id) {
  return document.getElementById(id);
}

function number(value, suffix = "", digits = 1) {
  return value === null || value === undefined ? "—" : `${Number(value).toFixed(digits)}${suffix}`;
}

function duration(seconds) {
  const total = Math.max(0, Math.round(seconds || 0));
  const days = Math.floor(total / 86400);
  const hours = Math.floor((total % 86400) / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  if (days) return `${days}d ${hours}h`;
  if (hours) return `${hours}h ${minutes}m`;
  return `${minutes}m`;
}

function localTime(value) {
  if (!value) return "—";
  return new Intl.DateTimeFormat(undefined, {
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  }).format(new Date(value));
}

function metricClass(metric, value) {
  if (value === null || value === undefined) return "";
  if (metric === "loss") {
    if (value >= 1) return "value-bad";
    if (value > 0) return "value-warn";
    return "value-good";
  }
  if (metric === "rtt") {
    if (value >= 150) return "value-bad";
    if (value >= 80) return "value-warn";
  }
  return "";
}

function chooseMetrics(payload) {
  if (payload.current && Object.keys(payload.current.targets || {}).length) {
    return payload.current;
  }
  return payload.latest;
}

function renderStatus(payload) {
  const active = chooseMetrics(payload);
  const diagnosis = active?.diagnosis || payload.latest?.diagnosis || {
    message: "Collecting initial samples",
    severity: "neutral",
  };
  const panel = byId("status-panel");
  panel.className = `status-panel ${diagnosis.severity || "neutral"}`;
  byId("status-message").textContent = diagnosis.message;

  const current = payload.current;
  const progress = current?.progress_pct || 0;
  byId("progress-text").textContent = `${Math.round(progress)}%`;
  byId("progress-bar").style.width = `${progress}%`;
  const complete = payload.history.length;
  byId("status-detail").textContent = current
    ? `${current.bursts_completed} synchronized probe bursts completed in this interval.`
    : payload.next_interval_start
      ? `Next synchronized interval begins ${localTime(payload.next_interval_start)}.`
      : complete
        ? "The last five-minute interval is complete."
        : "The first live measurements will appear shortly.";
}

function renderSummary(payload) {
  const discovery = payload.discovery || {};
  byId("gateway-address").textContent = discovery.gateway || "Unavailable";
  byId("gateway-interface").textContent = discovery.interface
    ? `Default route via ${discovery.interface}`
    : "Default route";
  byId("isp-hop-address").textContent = discovery.isp_hop || "Filtered / unavailable";
  const sourceLabels = {
    traceroute: "Fresh traceroute",
    cache: "Cached · public IP unchanged",
    "stale-cache": "Last known hop · retrace pending",
  };
  const hopSource = sourceLabels[discovery.isp_hop_source] || "First responding hop beyond the Router";
  byId("isp-hop-detail").textContent = discovery.public_ip
    ? `${hopSource} · public ${discovery.public_ip}`
    : hopSource;
  byId("runtime").textContent = duration(
    payload.continuous_runtime_seconds ?? payload.runtime_seconds,
  );
  const continuousSince = payload.continuous_started_at
    ? `Since ${localTime(payload.continuous_started_at)}`
    : "Waiting for the first interval";
  const loadedIntervals = chartHistoryRecords.length || payload.history.length;
  byId("sample-count").textContent = `${continuousSince} · ${loadedIntervals} interval${loadedIntervals === 1 ? "" : "s"} loaded`;

  renderLossRanges(payload);

  const warning = byId("warning");
  if (discovery.warning) {
    warning.textContent = discovery.warning;
    warning.classList.remove("hidden");
  } else {
    warning.classList.add("hidden");
  }
}

function renderTargets(payload) {
  const active = chooseMetrics(payload);
  const metrics = active?.targets || {};
  const body = byId("target-rows");
  body.replaceChildren();
  TARGETS.forEach((key) => {
    const target = metrics[key];
    if (!target || !target.address) return;
    const row = document.createElement("tr");
    const values = [
      `<span class="target-name">${target.label}</span>`,
      `<span class="target-address">${target.address}</span>`,
      `<span class="${metricClass("loss", target.loss_pct)}">${number(target.loss_pct, "%")}</span>`,
      `<span class="${metricClass("rtt", target.rtt_avg_ms)}">${number(target.rtt_avg_ms, " ms")}</span>`,
      number(target.rtt_max_ms, " ms"),
      number(target.jitter_ms, " ms"),
      `${target.received || 0} / ${target.sent || 0}`,
    ];
    values.forEach((html) => {
      const cell = document.createElement("td");
      cell.innerHTML = html;
      row.appendChild(cell);
    });
    body.appendChild(row);
  });
  if (!body.children.length) {
    const row = document.createElement("tr");
    const cell = document.createElement("td");
    cell.colSpan = 7;
    cell.textContent = "Waiting for the first synchronized probe burst…";
    row.appendChild(cell);
    body.appendChild(row);
  }
}

function weightedPacketLoss(records, key) {
  let sent = 0;
  let received = 0;
  records.forEach((record) => {
    const target = record.targets[key];
    if (!target || !Number.isFinite(Number(target.sent)) || Number(target.sent) <= 0) return;
    sent += Number(target.sent);
    received += Math.max(0, Math.min(Number(target.received) || 0, Number(target.sent)));
  });
  return sent ? ((sent - received) * 100) / sent : null;
}

function rollingHistory(payload) {
  const end = new Date(payload.server_time).getTime();
  const cutoff = end - 24 * 60 * 60 * 1000;
  return payload.history.filter((record) => {
    const completed = new Date(record.end).getTime();
    return Number.isFinite(completed) && completed >= cutoff && completed <= end;
  });
}

function renderRollingAverages(payload) {
  const records = rollingHistory(payload);
  let coverageSeconds = 0;
  if (records.length) {
    const firstStart = new Date(records[0].start).getTime();
    const serverTime = new Date(payload.server_time).getTime();
    coverageSeconds = Math.min(
      24 * 60 * 60,
      Math.max(0, (serverTime - firstStart) / 1000),
    );
  }
  const windowText = records.length
    ? `Rolling ${duration(coverageSeconds)} · ${records.length} completed interval${records.length === 1 ? "" : "s"}`
    : "No completed intervals";
  TARGETS.forEach((key) => {
    byId(`rolling-loss-${key}`).textContent = number(
      weightedPacketLoss(records, key),
      "%",
    );
    byId(`rolling-meta-${key}`).textContent = windowText;
  });
}

function renderLossRanges(payload) {
  const records = rollingHistory(payload);
  RANGE_TARGETS.forEach((key) => {
    const values = records
      .map((record) => record.targets[key]?.loss_pct)
      .filter((value) => value !== null && value !== undefined)
      .map(Number);
    const low = values.length ? Math.min(...values) : null;
    const high = values.length ? Math.max(...values) : null;
    const lowCell = byId(`range-low-${key}`);
    const highCell = byId(`range-high-${key}`);
    lowCell.textContent = number(low, "%");
    highCell.textContent = number(high, "%");
    lowCell.className = metricClass("loss", low);
    highCell.className = metricClass("loss", high);
  });
}

function average(values) {
  const present = values.filter((value) => value !== null && value !== undefined);
  return present.length ? present.reduce((sum, value) => sum + Number(value), 0) / present.length : null;
}

function lossRange(record) {
  const values = PUBLIC_TARGETS.map((key) => record.targets[key]?.loss_pct)
    .filter((value) => value !== null && value !== undefined)
    .map(Number);
  if (!values.length) return "—";
  return `${Math.min(...values).toFixed(1)}–${Math.max(...values).toFixed(1)}%`;
}

function renderHistory(payload) {
  const body = byId("history-rows");
  body.replaceChildren();
  payload.history.slice(-48).reverse().forEach((record) => {
    const diagnosis = record.diagnosis || {};
    const publicRtt = average(PUBLIC_TARGETS.map((key) => record.targets[key]?.rtt_avg_ms));
    const cells = [
      localTime(record.end),
      `<span class="status-pill ${diagnosis.severity || "neutral"}">${diagnosis.message || "Unknown"}</span>`,
      number(record.targets.gateway?.loss_pct, "%"),
      number(record.targets.isp_hop?.loss_pct, "%"),
      lossRange(record),
      number(publicRtt, " ms"),
    ];
    const row = document.createElement("tr");
    cells.forEach((html, index) => {
      const cell = document.createElement("td");
      if (index === 1) cell.innerHTML = html;
      else cell.textContent = html;
      row.appendChild(cell);
    });
    body.appendChild(row);
  });
}

function renderLegend(container, series) {
  container.replaceChildren();
  series.forEach((item) => {
    const span = document.createElement("span");
    const dot = document.createElement("i");
    dot.style.background = item.color;
    span.appendChild(dot);
    span.append(item.label);
    container.appendChild(span);
  });
}

function recordTimestamp(record) {
  return new Date(record.end).getTime();
}

function mergeChartHistory(existing, incoming) {
  const records = new Map();
  [...existing, ...incoming].forEach((record) => {
    if (record?.start) records.set(record.start, record);
  });
  return [...records.values()]
    .sort((left, right) => recordTimestamp(left) - recordTimestamp(right))
    .slice(-MAX_CHART_RECORDS);
}

function resolveChartView(records, serverTime, chartKey) {
  const settings = chartViews[chartKey];
  const allRows = records.slice(-MAX_CHART_RECORDS);
  const durationMs = settings.rangeHours * 60 * 60 * 1000;
  if (!allRows.length) {
    return {
      allRows,
      startIndex: 0,
      endIndex: -1,
      durationMs,
      followingLatest: true,
      chartKey,
    };
  }
  let latestBoundary = new Date(serverTime).getTime();
  if (!Number.isFinite(latestBoundary)) {
    latestBoundary = recordTimestamp(allRows.at(-1));
  }
  const latestStart = latestBoundary - durationMs;
  const earliestStart = Math.min(
    recordTimestamp(allRows[0]),
    latestStart,
  );
  const requestedStart = settings.browseWindow?.start ?? latestStart;
  const windowStart = Math.max(
    earliestStart,
    Math.min(latestStart, requestedStart),
  );
  const windowEnd = windowStart + durationMs;
  const followingLatest = Math.abs(windowStart - latestStart) < 1000;
  if (followingLatest) settings.browseWindow = null;

  const startIndex = allRows.findIndex(
    (record) => recordTimestamp(record) >= windowStart,
  );
  let endIndex = allRows.length - 1;
  while (
    endIndex >= 0 &&
    recordTimestamp(allRows[endIndex]) > windowEnd
  ) {
    endIndex -= 1;
  }
  return {
    allRows,
    startIndex: startIndex < 0 ? 0 : startIndex,
    endIndex: startIndex < 0 || endIndex < startIndex ? -1 : endIndex,
    durationMs,
    windowStart,
    windowEnd,
    earliestStart,
    latestStart,
    followingLatest,
    chartKey,
  };
}

function setChartBrowseStart(start, state) {
  const settings = chartViews[state.chartKey];
  const windowStart = Math.max(
    state.earliestStart,
    Math.min(state.latestStart, start),
  );
  if (Math.abs(windowStart - state.latestStart) < 1000) {
    settings.browseWindow = null;
  } else {
    settings.browseWindow = {
      start: windowStart,
      end: windowStart + state.durationMs,
    };
  }
  if (latestPayload) renderCharts(latestPayload);
}

function followLatestChart(chartKey) {
  chartViews[chartKey].browseWindow = null;
  if (latestPayload) renderCharts(latestPayload);
}

function updateChartControls(view, chartKey) {
  const settings = chartViews[chartKey];
  const selected = Math.max(0, view.endIndex - view.startIndex + 1);
  const rangeLabel = `${settings.rangeHours} hour${settings.rangeHours === 1 ? "" : "s"}`;
  const label = view.allRows.length
    ? view.followingLatest
      ? `Latest ${rangeLabel} · ${selected} intervals`
      : `Earlier ${rangeLabel} · ${selected} intervals`
    : "Waiting for history";
  byId(`${chartKey}-chart-window`).textContent = label;
  document.querySelector(`[data-chart-range="${chartKey}"]`).value =
    String(settings.rangeHours);
  document.querySelector(`[data-chart-latest="${chartKey}"]`).disabled =
    view.followingLatest || !view.allRows.length;
}

function drawChart(canvas, view, field, minimumTop, unit, metricLabel) {
  const context = canvas.getContext("2d");
  const width = canvas.clientWidth;
  const height = canvas.clientHeight;
  const ratio = window.devicePixelRatio || 1;
  canvas.width = Math.round(width * ratio);
  canvas.height = Math.round(height * ratio);
  context.setTransform(ratio, 0, 0, ratio, 0, 0);
  context.clearRect(0, 0, width, height);

  const margin = { top: 20, right: 18, bottom: 32, left: 48 };
  const plotWidth = width - margin.left - margin.right;
  const plotHeight = height - margin.top - margin.bottom;
  const rows = view.endIndex >= view.startIndex
    ? view.allRows.slice(view.startIndex, view.endIndex + 1)
    : [];
  canvas.classList.toggle(
    "is-zoomed",
    rows.length > 0 && view.earliestStart < view.latestStart,
  );
  const series = TARGETS.map((key) => ({
    key,
    label: rows.at(-1)?.targets[key]?.label || key,
    color: COLORS[key],
    values: rows.map((row) => row.targets[key]?.[field]),
  })).filter((item) => item.values.some((value) => value !== null && value !== undefined));

  const allValues = series.flatMap((item) => item.values).filter((value) => value !== null && value !== undefined).map(Number);
  let top = Math.max(minimumTop, ...(allValues.length ? allValues : [minimumTop]));
  top = Math.ceil(top * 1.15 * 10) / 10;
  chartStates.set(canvas, {
    allRows: view.allRows,
    startIndex: view.startIndex,
    endIndex: view.endIndex,
    durationMs: view.durationMs,
    windowStart: view.windowStart,
    windowEnd: view.windowEnd,
    earliestStart: view.earliestStart,
    latestStart: view.latestStart,
    followingLatest: view.followingLatest,
    chartKey: view.chartKey,
    rows,
    series,
    field,
    unit,
    metricLabel,
    margin,
    plotWidth,
    plotHeight,
    top,
  });

  context.font = "11px ui-sans-serif, system-ui";
  context.strokeStyle = "#253646";
  context.fillStyle = "#718898";
  context.lineWidth = 1;
  for (let step = 0; step <= 4; step += 1) {
    const y = margin.top + (plotHeight * step) / 4;
    const value = top * (1 - step / 4);
    context.beginPath();
    context.moveTo(margin.left, y);
    context.lineTo(width - margin.right, y);
    context.stroke();
    context.fillText(value.toFixed(value < 10 ? 1 : 0), 8, y + 4);
  }

  if (rows.length < 2) {
    context.fillStyle = "#8da3b3";
    context.textAlign = "center";
    context.fillText("A history chart appears after two intervals.", width / 2, height / 2);
    context.textAlign = "left";
    return series;
  }

  const labelCount = Math.min(4, rows.length - 1);
  context.fillStyle = "#718898";
  for (let step = 0; step <= labelCount; step += 1) {
    const index = Math.round(((rows.length - 1) * step) / labelCount);
    const x = margin.left + (plotWidth * index) / (rows.length - 1);
    const label = new Date(rows[index].end).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
    context.textAlign = step === 0 ? "left" : step === labelCount ? "right" : "center";
    context.fillText(label, x, height - 10);
  }
  context.textAlign = "left";

  series.forEach((item) => {
    context.strokeStyle = item.color;
    context.lineWidth = 1.8;
    context.lineJoin = "round";
    context.beginPath();
    let drawing = false;
    item.values.forEach((value, index) => {
      if (value === null || value === undefined) {
        drawing = false;
        return;
      }
      const x = margin.left + (plotWidth * index) / (rows.length - 1);
      const y = margin.top + plotHeight * (1 - Number(value) / top);
      if (drawing) context.lineTo(x, y);
      else {
        context.moveTo(x, y);
        drawing = true;
      }
    });
    context.stroke();
  });
  return series;
}

function bindChartTooltip(canvas, tooltip) {
  canvas.addEventListener("pointermove", (event) => {
    if (chartPanStates.has(canvas)) {
      tooltip.classList.add("hidden");
      return;
    }
    const state = chartStates.get(canvas);
    if (!state || state.rows.length < 2) {
      tooltip.classList.add("hidden");
      return;
    }

    const rect = canvas.getBoundingClientRect();
    const localX = ((event.clientX - rect.left) * canvas.clientWidth) / rect.width;
    const localY = ((event.clientY - rect.top) * canvas.clientHeight) / rect.height;
    const { margin, plotWidth, plotHeight, rows, series, top } = state;
    if (
      localX < margin.left ||
      localX > margin.left + plotWidth ||
      localY < margin.top ||
      localY > margin.top + plotHeight
    ) {
      tooltip.classList.add("hidden");
      return;
    }

    const index = Math.max(
      0,
      Math.min(
        rows.length - 1,
        Math.round(((localX - margin.left) / plotWidth) * (rows.length - 1)),
      ),
    );
    const pointX = margin.left + (plotWidth * index) / (rows.length - 1);
    let nearest = null;
    series.forEach((item) => {
      const value = item.values[index];
      if (value === null || value === undefined) return;
      const pointY = margin.top + plotHeight * (1 - Number(value) / top);
      const distance = Math.hypot(localX - pointX, localY - pointY);
      if (!nearest || distance < nearest.distance) {
        nearest = { item, value: Number(value), distance };
      }
    });
    if (!nearest || nearest.distance > 28) {
      tooltip.classList.add("hidden");
      return;
    }

    const target = rows[index].targets[nearest.item.key] || {};
    const destination = document.createElement("strong");
    destination.textContent = `${nearest.item.label} · ${target.address || "Unavailable"}`;
    destination.style.color = nearest.item.color;
    const timestamp = document.createElement("span");
    timestamp.textContent = localTime(rows[index].end);
    const value = document.createElement("small");
    value.className = "tooltip-value";
    value.textContent = `${state.metricLabel}: ${number(nearest.value, state.unit)}`;
    tooltip.replaceChildren(destination, timestamp, value);
    tooltip.classList.remove("hidden");

    const panel = canvas.closest(".chart-panel");
    const panelRect = panel.getBoundingClientRect();
    const pointerX = event.clientX - panelRect.left;
    const pointerY = event.clientY - panelRect.top;
    const left = Math.max(
      8,
      Math.min(pointerX + 14, panel.clientWidth - tooltip.offsetWidth - 8),
    );
    const topPosition = Math.max(
      8,
      Math.min(pointerY - tooltip.offsetHeight - 12, panel.clientHeight - tooltip.offsetHeight - 8),
    );
    tooltip.style.left = `${left}px`;
    tooltip.style.top = `${topPosition}px`;
  });
  canvas.addEventListener("pointerleave", () => {
    tooltip.classList.add("hidden");
  });
}

function bindChartNavigation(canvas, tooltip) {
  canvas.addEventListener("wheel", (event) => {
    const state = chartStates.get(canvas);
    if (
      !state ||
      state.rows.length < 2 ||
      state.earliestStart >= state.latestStart
    ) {
      return;
    }
    const rect = canvas.getBoundingClientRect();
    const localX = ((event.clientX - rect.left) * canvas.clientWidth) / rect.width;
    if (
      localX < state.margin.left ||
      localX > state.margin.left + state.plotWidth
    ) {
      return;
    }
    event.preventDefault();
    tooltip.classList.add("hidden");
    const delta = event.deltaX || event.deltaY;
    const shiftMs =
      (-delta / state.plotWidth) * state.durationMs * 0.35;
    setChartBrowseStart(state.windowStart + shiftMs, state);
  }, { passive: false });

  canvas.addEventListener("pointerdown", (event) => {
    const state = chartStates.get(canvas);
    if (
      event.button !== 0 ||
      !state ||
      state.earliestStart >= state.latestStart
    ) {
      return;
    }
    const rect = canvas.getBoundingClientRect();
    const localX = ((event.clientX - rect.left) * canvas.clientWidth) / rect.width;
    const localY = ((event.clientY - rect.top) * canvas.clientHeight) / rect.height;
    if (
      localX < state.margin.left ||
      localX > state.margin.left + state.plotWidth ||
      localY < state.margin.top ||
      localY > state.margin.top + state.plotHeight
    ) {
      return;
    }
    event.preventDefault();
    tooltip.classList.add("hidden");
    chartPanStates.set(canvas, {
      pointerId: event.pointerId,
      startClientX: event.clientX,
      windowStart: state.windowStart,
      durationMs: state.durationMs,
      earliestStart: state.earliestStart,
      latestStart: state.latestStart,
      chartKey: state.chartKey,
      plotWidth: state.plotWidth,
      lastStart: state.windowStart,
    });
    canvas.classList.add("is-panning");
    canvas.setPointerCapture(event.pointerId);
  });

  canvas.addEventListener("pointermove", (event) => {
    const pan = chartPanStates.get(canvas);
    if (!pan || event.pointerId !== pan.pointerId) return;
    event.preventDefault();
    const shiftMs =
      (-(event.clientX - pan.startClientX) / pan.plotWidth) *
      pan.durationMs;
    const windowStart = Math.max(
      pan.earliestStart,
      Math.min(pan.latestStart, pan.windowStart + shiftMs),
    );
    if (Math.abs(windowStart - pan.lastStart) < 1000) return;
    pan.lastStart = windowStart;
    setChartBrowseStart(windowStart, pan);
  });

  const stopPanning = (event) => {
    const pan = chartPanStates.get(canvas);
    if (!pan || event.pointerId !== pan.pointerId) return;
    chartPanStates.delete(canvas);
    canvas.classList.remove("is-panning");
    if (canvas.hasPointerCapture(event.pointerId)) {
      canvas.releasePointerCapture(event.pointerId);
    }
  };
  canvas.addEventListener("pointerup", stopPanning);
  canvas.addEventListener("pointercancel", stopPanning);
  canvas.addEventListener("dblclick", (event) => {
    event.preventDefault();
    tooltip.classList.add("hidden");
    followLatestChart(canvas.dataset.chartKey);
  });
}

function renderCharts(payload) {
  const lossView = resolveChartView(
    chartHistoryRecords,
    payload.server_time,
    "loss",
  );
  const latencyView = resolveChartView(
    chartHistoryRecords,
    payload.server_time,
    "latency",
  );
  const lossSeries = drawChart(byId("loss-chart"), lossView, "loss_pct", 5, "%", "Packet loss");
  const latencySeries = drawChart(byId("latency-chart"), latencyView, "rtt_avg_ms", 50, " ms", "Average latency");
  renderLegend(byId("loss-legend"), lossSeries);
  renderLegend(byId("latency-legend"), latencySeries);
  updateChartControls(lossView, "loss");
  updateChartControls(latencyView, "latency");
}

function render(payload) {
  latestPayload = payload;
  renderStatus(payload);
  renderSummary(payload);
  renderRollingAverages(payload);
  renderTargets(payload);
  renderHistory(payload);
  renderCharts(payload);
  byId("last-updated").textContent = `Updated ${localTime(payload.server_time)}`;
  byId("version").textContent = payload.version;
}

async function refresh() {
  try {
    const historyLimit = chartHistoryRecords.length
      ? 288
      : MAX_CHART_RECORDS;
    const response = await fetch(`/api/status?limit=${historyLimit}`, {
      cache: "no-store",
    });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const payload = await response.json();
    chartHistoryRecords = mergeChartHistory(
      chartHistoryRecords,
      payload.history,
    );
    render(payload);
  } catch (error) {
    byId("last-updated").textContent = "Monitor unreachable";
  }
}

window.addEventListener("resize", () => {
  if (latestPayload) renderCharts(latestPayload);
});

bindChartTooltip(byId("loss-chart"), byId("loss-tooltip"));
bindChartTooltip(byId("latency-chart"), byId("latency-tooltip"));
bindChartNavigation(byId("loss-chart"), byId("loss-tooltip"));
bindChartNavigation(byId("latency-chart"), byId("latency-tooltip"));
document.querySelectorAll("[data-chart-range]").forEach((select) => {
  select.addEventListener("change", () => {
    const range = Number(select.value);
    if (![24, 12, 6, 4, 1].includes(range)) return;
    const settings = chartViews[select.dataset.chartRange];
    settings.rangeHours = range;
    settings.browseWindow = null;
    if (latestPayload) renderCharts(latestPayload);
  });
});
document.querySelectorAll("[data-chart-latest]").forEach((button) => {
  button.addEventListener("click", () => {
    followLatestChart(button.dataset.chartLatest);
  });
});
refresh();
setInterval(refresh, 5000);
