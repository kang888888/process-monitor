// 同域访问（页面由 Python 服务提供）
const API_BASE = location.origin;

const WINDOW_SECONDS = 600; // 10 分钟

const appInput = document.getElementById("appInput");
const appTags = document.getElementById("appTags");
const appCombo = document.getElementById("appCombo");
const appComboTrigger = document.getElementById("appComboTrigger");
const appDropdown = document.getElementById("appDropdown");
const btnRefresh = document.getElementById("btnRefresh");
const freqSlider = document.getElementById("freqSlider");
const freqLabel = document.getElementById("freqLabel");
const btnStart = document.getElementById("btnStart");
const btnStop = document.getElementById("btnStop");
const btnExport = document.getElementById("btnExport");
const btnImport = document.getElementById("btnImport");
const fileImport = document.getElementById("fileImport");

const chartCpu = document.getElementById("chartCpu");
const chartMem = document.getElementById("chartMem");
const chartGpu = document.getElementById("chartGpu");
const chartDisk = document.getElementById("chartDisk");
const chartNet = document.getElementById("chartNet");
const netUnitSelect = document.getElementById("netUnitSelect");
const diskPdhCheckbox = document.getElementById("diskPdhCheckbox");
const diskPdhOptionWrap = document.getElementById("diskPdhOptionWrap");
const chartDiskDesc = document.getElementById("chartDiskDesc");

const procCount = document.getElementById("procCount");
const liveCpu = document.getElementById("liveCpu");
const liveMem = document.getElementById("liveMem");
const liveGpu = document.getElementById("liveGpu");

const procModalOverlay = document.getElementById("procModalOverlay");
const procModalClose = document.getElementById("procModalClose");
const procModalMeta = document.getElementById("procModalMeta");
const procModalContent = document.getElementById("procModalContent");
const toastEl = document.getElementById("toast");
const currentAppHint = document.getElementById("currentAppHint");
const currentAppNames = document.getElementById("currentAppNames");
const dialogOverlay = document.getElementById("dialogOverlay");
const dialogTitleEl = document.getElementById("dialogTitle");
const dialogClose = document.getElementById("dialogClose");
const dialogMessageEl = document.getElementById("dialogMessage");
const dialogActionsEl = document.getElementById("dialogActions");

let charts = {};
let toastTimer = null;
let samples = [];
let pollTimer = null;
let pollingInFlight = false;
let pollFailCount = 0;
let pollDisconnectedToastShown = false;
let isMonitoring = false;
let selectedApps = [];
let currentExes = [];
let procModalLoading = false;
let appsList = [];
let appDropdownHideTimer = null;
let pdhDiskIoAvailable = false;
let isViewingImportedData = false;
let importedMeta = null;
let dialogResolve = null;

const LS_KEY_USE_PDH_DISK = "process-monitor.usePdhDiskIo";

function hideDialog(result = null) {
  dialogOverlay?.classList.add("hidden");
  if (dialogActionsEl) dialogActionsEl.innerHTML = "";
  if (dialogMessageEl) dialogMessageEl.textContent = "";
  if (dialogTitleEl) dialogTitleEl.textContent = "提示";
  const r = dialogResolve;
  dialogResolve = null;
  if (typeof r === "function") r(result);
}

function showDialog({ title = "提示", message = "", actions = [] } = {}) {
  return new Promise((resolve) => {
    dialogResolve = resolve;
    if (dialogTitleEl) dialogTitleEl.textContent = title;
    if (dialogMessageEl) dialogMessageEl.textContent = String(message ?? "");
    if (dialogActionsEl) {
      dialogActionsEl.innerHTML = "";
      const btns = (actions && actions.length) ? actions : [{ label: "确定", value: true, className: "btn-primary" }];
      btns.forEach((a) => {
        const b = document.createElement("button");
        b.type = "button";
        b.textContent = a.label ?? "确定";
        b.className = a.className ?? "btn-primary";
        b.addEventListener("click", () => hideDialog(a.value));
        dialogActionsEl.appendChild(b);
      });
    }
    dialogOverlay?.classList.remove("hidden");
  });
}

function showAlert(message, title = "提示") {
  return showDialog({
    title,
    message,
    actions: [{ label: "确定", value: true, className: "btn-primary" }],
  });
}

function showConfirm(message, { title = "确认", okText = "确定", cancelText = "取消", danger = false } = {}) {
  return showDialog({
    title,
    message,
    actions: [
      { label: cancelText, value: false, className: "btn-secondary" },
      { label: okText, value: true, className: danger ? "btn-danger" : "btn-primary" },
    ],
  });
}

function buildExportPayload() {
  const nowIso = new Date().toISOString();
  // 导出当前内存中的 samples（默认窗口 10 分钟，数量可控）
  const payload = {
    schema: "process-monitor.samples.v1",
    exportedAt: nowIso,
    meta: {
      exeNames: (isMonitoring && currentExes.length ? currentExes : selectedApps).slice(),
      windowSeconds: WINDOW_SECONDS,
      netUnit: getNetUnit(),
      usePdhDiskIo: !!(diskPdhCheckbox?.checked && pdhDiskIoAvailable),
    },
    samples: Array.isArray(samples) ? samples.slice() : [],
  };
  return payload;
}

function downloadJson(filename, dataObj) {
  const json = JSON.stringify(dataObj, null, 2);
  const blob = new Blob([json], { type: "application/json;charset=utf-8" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

function sanitizeImportedSamples(arr) {
  if (!Array.isArray(arr)) return [];
  const out = [];
  for (const s of arr) {
    if (!s || typeof s !== "object") continue;
    const ts = Number(s.ts);
    if (!Number.isFinite(ts) || ts <= 0) continue;
    out.push({
      ts,
      cpu_pct: Number(s.cpu_pct) || 0,
      mem_rss_mb: Number(s.mem_rss_mb) || 0,
      disk_read_bps: Number(s.disk_read_bps) || 0,
      disk_write_bps: Number(s.disk_write_bps) || 0,
      net_recv_bps: Number(s.net_recv_bps) || 0,
      net_sent_bps: Number(s.net_sent_bps) || 0,
      gpu_pct: Number(s.gpu_pct) || 0,
      process_count: Number(s.process_count) || 0,
    });
  }
  out.sort((a, b) => a.ts - b.ts);
  return out;
}

function setImportedView(meta, importedSamples) {
  stopStreaming();
  isViewingImportedData = true;
  importedMeta = meta || null;
  samples = importedSamples;
  currentExes = (meta?.exeNames && Array.isArray(meta.exeNames)) ? meta.exeNames.slice() : [];
  updateAppComboDisabled();
  updateMonitorButtons();
  updateChartAppNotes();
  updateCharts();
  const last = samples.length ? samples[samples.length - 1] : null;
  if (last) updateStatus(last);
}

/** 模糊匹配：query 的字符按顺序出现在 str 中即匹配（忽略大小写） */
function fuzzyMatch(query, str) {
  if (!query) return true;
  const q = query.toLowerCase();
  const s = String(str).toLowerCase();
  let j = 0;
  for (let i = 0; i < s.length && j < q.length; i++) {
    if (s[i] === q[j]) j++;
  }
  return j === q.length;
}

function filterApps(query) {
  return appsList.filter((a) => fuzzyMatch(query, a));
}

function renderAppTags() {
  if (!appTags) return;
  const esc = (s) => String(s).replace(/&/g, "&amp;").replace(/"/g, "&quot;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
  appTags.innerHTML = selectedApps
    .map(
      (a) =>
        `<span class="app-tag">${esc(a)}<button type="button" class="app-tag-remove" data-app="${esc(a)}" title="移除">×</button></span>`
    )
    .join("");
  appTags.querySelectorAll(".app-tag-remove").forEach((btn) => {
    btn.addEventListener("click", (e) => {
      e.stopPropagation();
      const app = btn.dataset.app;
      if (app) toggleAppSelection(app);
    });
  });
}

function renderAppDropdown(query) {
  const filtered = filterApps(query);
  if (!appDropdown) return;
  if (filtered.length === 0) {
    appDropdown.innerHTML = '<div class="app-dropdown-empty">无匹配应用</div>';
    return;
  }
  const esc = (s) => String(s).replace(/&/g, "&amp;").replace(/"/g, "&quot;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
  const itemsHtml = filtered
    .map((a) => {
      const selected = selectedApps.includes(a) ? " is-selected" : "";
      return `<button type="button" class="app-dropdown-item${selected}" data-app="${esc(a)}"><span class="app-item-label">${esc(a)}</span></button>`;
    })
    .join("");
  appDropdown.innerHTML = `<div class="app-dropdown-list">${itemsHtml}</div>`;
}

function toggleAppSelection(app) {
  if (isMonitoring) return;
  const idx = selectedApps.indexOf(app);
  if (idx >= 0) {
    selectedApps.splice(idx, 1);
  } else {
    selectedApps.push(app);
  }
  renderAppTags();
  renderAppDropdown(appInput?.value?.trim() ?? "");
  appInput.dispatchEvent(new Event("change", { bubbles: true }));
}

function showAppDropdown() {
  if (isMonitoring) return;
  if (appDropdownHideTimer) {
    clearTimeout(appDropdownHideTimer);
    appDropdownHideTimer = null;
  }
  renderAppDropdown(appInput?.value?.trim() ?? "");
  appDropdown?.classList.remove("hidden");
  appComboTrigger?.classList.add("open");
}

function hideAppDropdown() {
  appDropdownHideTimer = setTimeout(() => {
    appDropdown?.classList.add("hidden");
    appComboTrigger?.classList.remove("open");
    if (appInput) {
      appInput.value = "";
    }
    appDropdownHideTimer = null;
  }, 150);
}

function toggleAppDropdown() {
  if (isMonitoring) return;
  if (appDropdown?.classList.contains("hidden")) {
    showAppDropdown();
  } else {
    appDropdown?.classList.add("hidden");
    appComboTrigger?.classList.remove("open");
    if (appInput) {
      appInput.value = "";
    }
  }
}

function bytesToMB(v) {
  const n = typeof v === "number" ? v : Number(v);
  if (!Number.isFinite(n) || n < 0) return 0;
  return n / (1024 * 1024);
}

/** 字节/秒 → 千字节/秒 (K/s) */
function bytesToKB(bps) {
  const n = typeof bps === "number" ? bps : Number(bps);
  if (!Number.isFinite(n) || n < 0) return 0;
  return n / 1024;
}

/** 字节/秒 → 兆比特/秒 (Mbps, 1 Mbps = 10^6 bps) */
function bytesToMbps(bps) {
  const n = typeof bps === "number" ? bps : Number(bps);
  if (!Number.isFinite(n) || n < 0) return 0;
  return (n * 8) / 1e6;
}

function getNetUnit() {
  const v = netUnitSelect?.value;
  if (v === "Mbps") return "Mbps";
  if (v === "KB/s") return "K/s";
  return "MB/s";
}

function netBpsToDisplay(bps) {
  const unit = netUnitSelect?.value;
  if (unit === "Mbps") return bytesToMbps(bps);
  if (unit === "KB/s") return bytesToKB(bps);
  return bytesToMB(bps);
}

function formatAxisNumber(v) {
  const n = typeof v === "number" ? v : Number(v);
  if (!Number.isFinite(n)) return "-";
  if (Math.abs(n) >= 100) return String(Math.round(n));
  if (Math.abs(n) >= 10) return String(Math.round(n * 10) / 10);
  return String(Math.round(n * 100) / 100);
}

/** 将数组中异常大的点压到合理上限，避免单点峰值拉高纵轴（上限 = 4 × 中位数） */
function capNetOutliers(values) {
  if (!values.length) return values;
  const sorted = values.slice().sort((a, b) => a - b);
  const mid = Math.floor(sorted.length / 2);
  const median = sorted.length % 2 ? sorted[mid] : (sorted[mid - 1] + sorted[mid]) / 2;
  const cap = Math.max(median * 4, 0.001);
  return values.map((v) => (Number.isFinite(v) && v > cap ? cap : v));
}

function stopStreaming() {
  if (pollTimer) {
    clearInterval(pollTimer);
    pollTimer = null;
  }
  pollingInFlight = false;
  pollFailCount = 0;
  pollDisconnectedToastShown = false;
  isMonitoring = false;
  updateAppComboDisabled();
  updateMonitorButtons();
  updateChartAppNotes();
}

function updateAppComboDisabled() {
  const collecting = isMonitoring;
  appCombo?.classList.toggle("collecting", collecting);
  btnRefresh?.classList.toggle("collecting", collecting);
  if (diskPdhCheckbox) {
    diskPdhCheckbox.disabled = collecting;
  }
  if (appInput) {
    appInput.readOnly = collecting;
    appInput.tabIndex = collecting ? -1 : 0;
  }
  if (collecting) {
    appCombo?.setAttribute("title", "收集中，请先结束收集后再修改");
    hideAppDropdown();
  } else {
    appCombo?.removeAttribute("title");
  }
}

function updateMonitorButtons() {
  if (btnStart) btnStart.classList.toggle("hidden", isMonitoring);
  if (btnStop) btnStop.classList.toggle("hidden", !isMonitoring);
  if (btnExport) {
    // 保留可点击：收集中/无数据都用 toast 解释原因，避免“点不了还没提示”
    btnExport.disabled = false;
    btnExport.title = isMonitoring
      ? "收集中，无法导出"
      : (samples.length ? "导出当前数据为 JSON" : "暂无数据可导出");
  }
  if (btnImport) {
    btnImport.disabled = false;
    btnImport.title = isMonitoring ? "收集中，无法导入展示" : "导入 JSON 并回放展示";
  }
}

function showToast(msg, opts = 2200) {
  if (!toastEl) return;
  if (toastTimer) clearTimeout(toastTimer);
  // 兼容旧签名 showToast(msg, durationMs)
  const options = typeof opts === "number" ? { durationMs: opts } : (opts || {});
  const type = options.type || "info"; // info|success|warn|error
  const durationMs = Number.isFinite(options.durationMs) ? options.durationMs : 2200;
  const sticky = !!options.sticky;

  toastEl.textContent = msg;
  toastEl.classList.remove("toast--info", "toast--success", "toast--warn", "toast--error");
  toastEl.classList.add(`toast--${type}`);
  toastEl.classList.remove("hidden");
  if (!sticky) {
    toastTimer = setTimeout(() => {
      toastEl.classList.add("hidden");
      toastTimer = null;
    }, durationMs);
  }
}

function updateChartAppNotes() {
  const label = isMonitoring && currentExes.length
    ? currentExes.join("、")
    : (isViewingImportedData
        ? ((currentExes && currentExes.length) ? `导入数据：${currentExes.join("、")}` : "导入数据")
        : "当前应用");
  const winSuffix = " · Windows";
  document.querySelectorAll(".chart-app-note").forEach((el) => {
    const isGpu = el.id?.includes("Gpu");
    const text = isMonitoring && currentExes.length
      ? label + (isGpu ? winSuffix : "")
      : (isGpu ? label + winSuffix : label);
    el.textContent = text;
    el.title = text;
  });
  if (currentAppHint && currentAppNames) {
    if (isMonitoring && currentExes.length) {
      const names = currentExes.join("、");
      currentAppNames.textContent = names;
      currentAppHint.title = names;
      currentAppHint.classList.remove("hidden");
    } else if (isViewingImportedData) {
      const names = (currentExes && currentExes.length) ? currentExes.join("、") : "导入数据";
      currentAppNames.textContent = names;
      currentAppHint.title = names;
      currentAppHint.classList.remove("hidden");
    } else {
      currentAppHint.removeAttribute("title");
      currentAppHint.classList.add("hidden");
    }
  }
}

function clearStatus() {
  procCount.textContent = "-";
  liveCpu.textContent = "-";
  liveMem.textContent = "-";
  if (liveGpu) liveGpu.textContent = "-";
}

function clearChartsData() {
  isViewingImportedData = false;
  importedMeta = null;
  samples = [];
  updateCharts();
  clearStatus();
}

function showProcModal() {
  procModalOverlay?.classList.remove("hidden");
}

function hideProcModal() {
  procModalOverlay?.classList.add("hidden");
  if (procModalMeta) procModalMeta.textContent = "";
  if (procModalContent) procModalContent.innerHTML = "";
  procModalLoading = false;
}

function formatTime(ts) {
  if (!ts) return "-";
  const d = new Date(ts * 1000);
  return d.toLocaleString();
}

function renderProcTable(items) {
  const rows = (items || [])
    .map((p) => {
      const pid = p.pid ?? "";
      const rss = Number.isFinite(p.rss_mb) ? p.rss_mb.toFixed(2) : p.rss_mb ?? "";
      const status = p.status ?? "";
      const user = p.username ?? "";
      const ctime = formatTime(p.create_time);
      const cmd = p.cmdline ?? "";
      return `<tr>
        <td class="mono">${pid}</td>
        <td>${rss}</td>
        <td>${status}</td>
        <td>${user}</td>
        <td>${ctime}</td>
        <td class="mono">${cmd}</td>
      </tr>`;
    })
    .join("");

  return `<table class="proc-table">
    <thead>
      <tr>
        <th style="width: 90px;">PID</th>
        <th style="width: 110px;">内存(MB)</th>
        <th style="width: 110px;">状态</th>
        <th style="width: 140px;">用户</th>
        <th style="width: 180px;">启动时间</th>
        <th>命令行</th>
      </tr>
    </thead>
    <tbody>${rows}</tbody>
  </table>`;
}

async function openProcModal() {
  if (procModalLoading) return;
  if (!currentExes.length) return;

  procModalLoading = true;
  showProcModal();
  const exeLabel = currentExes.join("、");
  if (procModalMeta) procModalMeta.textContent = `应用：${exeLabel}（加载中...）`;
  if (procModalContent) procModalContent.innerHTML = `<div style="color:#94a3b8;">正在获取进程信息...</div>`;

  try {
    const params = new URLSearchParams();
    currentExes.forEach((e) => params.append("exeNames", e));
    const r = await fetch(`${API_BASE}/api/monitor/processes?${params.toString()}`, { cache: "no-store" });
    const d = await r.json();
    if (!d.ok) {
      if (procModalMeta) procModalMeta.textContent = `应用：${exeLabel}`;
      if (procModalContent) procModalContent.innerHTML = `<div style="color:#e11d48;">${d.error || "获取失败"}</div>`;
      return;
    }
    const namesLabel = Array.isArray(d.exeNames) ? d.exeNames.join("、") : d.exeName || exeLabel;
    if (procModalMeta) procModalMeta.textContent = `应用：${namesLabel}，进程数：${d.count}`;
    if (procModalContent) procModalContent.innerHTML = renderProcTable(d.processes);
  } catch (e) {
    if (procModalMeta) procModalMeta.textContent = `应用：${exeLabel}`;
    if (procModalContent) procModalContent.innerHTML = `<div style="color:#e11d48;">请求失败，请确认服务正常运行</div>`;
  } finally {
    procModalLoading = false;
  }
}

function initCharts() {
  const common = {
    grid: { left: 60, right: 20, top: 42, bottom: 30, containLabel: true },
    xAxis: { type: "time", boundaryGap: false, axisLine: { lineStyle: { color: "#bae6fd" } }, axisLabel: { color: "#64748b" } },
    yAxis: {
      type: "value",
      splitLine: { lineStyle: { color: "#e0f2fe" } },
      axisLine: { show: false },
      axisLabel: { color: "#64748b" },
    },
    series: [{ type: "line", smooth: true, symbol: "none", sampling: "lttb" }],
  };

  const axisNameStyle = { color: "#0369a1", nameGap: 12 };

  charts.cpu = echarts.init(chartCpu);
  charts.cpu.setOption({
    ...common,
    tooltip: {
      trigger: "axis",
      valueFormatter: (v) => `${Number(v).toFixed(2)} %`,
      backgroundColor: "rgba(255,255,255,0.95)",
      borderColor: "#bae6fd",
      textStyle: { color: "#334155" },
    },
    yAxis: {
      ...common.yAxis,
      name: "CPU %",
      nameTextStyle: axisNameStyle,
      min: 0,
      max: 100,
      axisLabel: { formatter: (v) => `${v}` },
    },
    series: [{ ...common.series[0], name: "CPU %", itemStyle: { color: "#0ea5e9" } }],
  });

  charts.mem = echarts.init(chartMem);
  charts.mem.setOption({
    ...common,
    tooltip: {
      trigger: "axis",
      valueFormatter: (v) => `${Number(v).toFixed(2)} MB`,
      backgroundColor: "rgba(255,255,255,0.95)",
      borderColor: "#bae6fd",
      textStyle: { color: "#334155" },
    },
    yAxis: {
      ...common.yAxis,
      name: "MB",
      nameTextStyle: axisNameStyle,
      min: 0,
      max: 1024,
      axisLabel: { formatter: (v) => `${v}` },
    },
    series: [{ ...common.series[0], name: "内存 MB", itemStyle: { color: "#06b6d4" } }],
  });

  if (chartGpu) {
    charts.gpu = echarts.init(chartGpu);
    charts.gpu.setOption({
      ...common,
      tooltip: {
        trigger: "axis",
        valueFormatter: (v) => `${Number(v).toFixed(2)} %`,
        backgroundColor: "rgba(255,255,255,0.95)",
        borderColor: "#bae6fd",
        textStyle: { color: "#334155" },
      },
      yAxis: {
        ...common.yAxis,
        name: "%",
        nameTextStyle: axisNameStyle,
        min: 0,
        max: 100,
        axisLabel: { formatter: (v) => `${v}` },
      },
      series: [{ ...common.series[0], name: "GPU %", itemStyle: { color: "#8b5cf6" } }],
    });
  }

  charts.disk = echarts.init(chartDisk);
  charts.disk.setOption({
    ...common,
    legend: { top: 0, textStyle: { color: "#0369a1" } },
    tooltip: {
      trigger: "axis",
      valueFormatter: (v) => `${Number(v).toFixed(2)} MB/s`,
      backgroundColor: "rgba(255,255,255,0.95)",
      borderColor: "#bae6fd",
      textStyle: { color: "#334155" },
    },
    yAxis: {
      ...common.yAxis,
      name: "MB/s",
      nameTextStyle: axisNameStyle,
      min: 0,
      max: 100,
      axisLabel: { formatter: (v) => `${v}` },
    },
    series: [
      { ...common.series[0], name: "读 MB/s", itemStyle: { color: "#f59e0b" } },
      { ...common.series[0], name: "写 MB/s", itemStyle: { color: "#f43f5e" } },
      { ...common.series[0], name: "总 MB/s", itemStyle: { color: "#10b981" } },
    ],
  });

  charts.net = echarts.init(chartNet);
  charts.net.setOption({
    ...common,
    grid: { ...common.grid, left: 70 },
    legend: { top: 0, textStyle: { color: "#0369a1" } },
    tooltip: {
      trigger: "axis",
      valueFormatter: (v) => `${Number(v).toFixed(2)} ${getNetUnit()}`,
      backgroundColor: "rgba(255,255,255,0.95)",
      borderColor: "#bae6fd",
      textStyle: { color: "#334155" },
    },
    yAxis: {
      ...common.yAxis,
      name: getNetUnit(),
      nameTextStyle: axisNameStyle,
      min: 0,
      max: 100,
      axisLabel: { formatter: (v) => formatAxisNumber(v), hideOverlap: true },
    },
    series: [
      { ...common.series[0], name: `下行 ${getNetUnit()}`, itemStyle: { color: "#38bdf8" } },
      { ...common.series[0], name: `上行 ${getNetUnit()}`, itemStyle: { color: "#a78bfa" } },
    ],
  });

  window.addEventListener("resize", () => {
    Object.values(charts).forEach((c) => c.resize());
  });
}

function updateCharts() {
  const cutoff = Date.now() / 1000 - WINDOW_SECONDS;
  const filtered = samples.filter((s) => s.ts >= cutoff);

  charts.cpu.setOption({ series: [{ data: filtered.map((s) => [s.ts * 1000, s.cpu_pct]) }] });
  const memData = filtered.map((s) => [s.ts * 1000, s.mem_rss_mb]);
  charts.mem.setOption({
    series: [{ data: memData }],
    yAxis: filtered.length > 0
      ? { max: Math.ceil(Math.max(...filtered.map((s) => s.mem_rss_mb), 100) * 1.1) }
      : undefined,
  });
  if (charts.gpu) {
    charts.gpu.setOption({
      series: [{ data: filtered.map((s) => [s.ts * 1000, s.gpu_pct != null ? s.gpu_pct : 0]) }]
    });
  }
  // 磁盘 IO：读、写、总三条曲线
  const diskReadData = filtered.map((s) => [s.ts * 1000, bytesToMB(s.disk_read_bps)]);
  const diskWriteData = filtered.map((s) => [s.ts * 1000, bytesToMB(s.disk_write_bps)]);
  const diskTotalData = filtered.map((s) => [
    s.ts * 1000,
    bytesToMB(s.disk_read_bps) + bytesToMB(s.disk_write_bps),
  ]);
  const diskMax =
    filtered.length > 0
      ? Math.ceil(
          Math.max(
            ...diskReadData.map((d) => d[1]),
            ...diskWriteData.map((d) => d[1]),
            ...diskTotalData.map((d) => d[1]),
            10
          ) * 1.1
        )
      : undefined;
  charts.disk.setOption({
    series: [
      { name: "读 MB/s", data: diskReadData },
      { name: "写 MB/s", data: diskWriteData },
      { name: "总 MB/s", data: diskTotalData },
    ],
    yAxis: diskMax != null ? { max: diskMax } : undefined,
  });
  // 网络：纵轴单位由 netUnitSelect 决定，并对异常峰值做前端限幅；小数值时用更细刻度便于观察波动
  const netUnit = getNetUnit();
  const recvValues = filtered.map((s) => netBpsToDisplay(s.net_recv_bps));
  const sentValues = filtered.map((s) => netBpsToDisplay(s.net_sent_bps));
  const recvCapped = capNetOutliers(recvValues);
  const sentCapped = capNetOutliers(sentValues);
  const rawNetMax =
    filtered.length > 0 ? Math.max(...recvCapped, ...sentCapped, 0.01) * 1.1 : undefined;
  const netMax = rawNetMax != null ? Math.ceil(rawNetMax * 10) / 10 : undefined; // 保留一位小数，避免 0.22 变成 1
  const splitNumber =
    netMax != null
      ? (netMax <= 1 ? 4 : netMax <= 5 ? 5 : 6)
      : undefined;
  charts.net.setOption({
    tooltip: { valueFormatter: (v) => `${Number(v).toFixed(2)} ${netUnit}` },
    yAxis: {
      name: netUnit,
      axisLabel: { formatter: (v) => formatAxisNumber(v), hideOverlap: true },
      ...(netMax != null ? { max: netMax, ...(splitNumber != null ? { splitNumber } : {}) } : {}),
    },
    series: [
      { name: `下行 ${netUnit}`, data: filtered.map((s, i) => [s.ts * 1000, recvCapped[i]]) },
      { name: `上行 ${netUnit}`, data: filtered.map((s, i) => [s.ts * 1000, sentCapped[i]]) },
    ],
  });
}

function updateStatus(s) {
  if (!s) return;
  procCount.textContent = s.process_count;
  liveCpu.textContent = Number.isFinite(s.cpu_pct) ? s.cpu_pct.toFixed(2) : s.cpu_pct;
  liveMem.textContent = Number.isFinite(s.mem_rss_mb) ? s.mem_rss_mb.toFixed(2) : s.mem_rss_mb;
  if (liveGpu) {
    liveGpu.textContent = Number.isFinite(s.gpu_pct) ? s.gpu_pct.toFixed(2) : (s.gpu_pct ?? "-");
  }
}

async function fetchApps() {
  try {
    const res = await fetch(`${API_BASE}/api/apps`);
    const data = await res.json();
    appsList = data.apps || [];
    appInput.value = "";
    renderAppTags();
    renderAppDropdown("");
  } catch (e) {
    console.error("fetch apps failed", e);
  }
}

async function fetchCapabilities() {
  try {
    const res = await fetch(`${API_BASE}/api/capabilities`, { cache: "no-store" });
    const data = await res.json();
    pdhDiskIoAvailable = !!data.pdhDiskIoAvailable;
    if (diskPdhOptionWrap) {
      diskPdhOptionWrap.classList.toggle("hidden", !pdhDiskIoAvailable);
    }
    if (diskPdhCheckbox) {
      if (!pdhDiskIoAvailable) {
        diskPdhCheckbox.checked = false;
        try { localStorage.removeItem(LS_KEY_USE_PDH_DISK); } catch (e) {}
      } else {
        // 默认勾选 PDH；若用户曾手动改过则沿用
        let saved = null;
        try { saved = localStorage.getItem(LS_KEY_USE_PDH_DISK); } catch (e) {}
        if (saved === null) {
          diskPdhCheckbox.checked = true;
        } else {
          diskPdhCheckbox.checked = saved === "1";
        }
      }
    }
    updateChartDiskDesc();
  } catch (e) {
    if (diskPdhOptionWrap) diskPdhOptionWrap.classList.add("hidden");
  }
}

function updateChartDiskDesc() {
  if (!chartDiskDesc) return;
  const usePdh = diskPdhCheckbox?.checked && pdhDiskIoAvailable;
  chartDiskDesc.textContent = usePdh
    ? "PDH 性能计数器，与任务管理器“磁盘”列一致（仅文件系统 IO，raw 速率）"
    : "读/写为进程 io_counters 速率（raw 速率，含网络等）";
}

async function startMonitor() {
  if (!selectedApps.length) {
    await showAlert("请先选择应用");
    return;
  }
  const interval = parseInt(freqSlider.value, 10);
  try {
    isViewingImportedData = false;
    importedMeta = null;
    stopStreaming();
    currentExes = [...selectedApps];
    clearChartsData();
    const usePdhDiskIo = !!(diskPdhCheckbox?.checked && pdhDiskIoAvailable);
    const res = await fetch(`${API_BASE}/api/monitor/start`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        exeNames: selectedApps,
        intervalMs: interval,
        usePdhDiskIo,
      }),
    });
    const data = await res.json();
    if (!data.ok) {
      await showAlert(data.error || "启动失败", "启动失败");
      return;
    }

    if (usePdhDiskIo && !data.usePdhDiskIo) {
      showToast("PDH 磁盘 IO 初始化失败，已回退到 psutil", { type: "warn", durationMs: 3200 });
    }

    isMonitoring = true;
    updateAppComboDisabled();
    updateMonitorButtons();
    updateChartAppNotes();

    // 纯 API 轮询最新采样（避免 WebSocket 端口占用问题）
    pollTimer = setInterval(async () => {
      if (!isMonitoring || pollingInFlight) return;
      pollingInFlight = true;
      try {
        const r = await fetch(`${API_BASE}/api/monitor/latest`, { cache: "no-store" });
        const d = await r.json();
        const s = d.sample;
        if (s && s.ts) {
          const last = samples.length ? samples[samples.length - 1] : null;
          if (!last || s.ts > last.ts) {
            samples.push(s);
            updateCharts();
            updateStatus(s);
          } else {
            updateStatus(s);
          }
        }
        pollFailCount = 0;
      } catch (e) {
        // 连续失败认为服务断开，自动退出“收集中”状态，避免输入框一直只读
        pollFailCount += 1;
        if (pollFailCount >= 5) {
          stopStreaming();
          clearStatus();
          hideProcModal();
          if (!pollDisconnectedToastShown) {
            pollDisconnectedToastShown = true;
            showToast("采集服务已断开，已自动结束收集", { type: "error", durationMs: 4200 });
          }
        }
      } finally {
        pollingInFlight = false;
      }
    }, 500);
  } catch (e) {
    await showAlert("连接采集服务失败，请确保 Python 服务已启动", "连接失败");
    console.error(e);
  }
}

async function stopMonitor() {
  stopStreaming();
  try {
    await fetch(`${API_BASE}/api/monitor/stop`, { method: "POST" });
  } catch (e) {}
  clearStatus();
  hideProcModal();
}

function onFreqChange() {
  const v = parseInt(freqSlider.value, 10);
  freqLabel.textContent = v >= 1000 ? `${v / 1000} 秒` : `${v} ms`;
  if (isMonitoring) {
    fetch(`${API_BASE}/api/monitor/config`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ intervalMs: v }),
    }).catch(() => {});
  }
}

btnRefresh.addEventListener("click", fetchApps);
btnStart.addEventListener("click", startMonitor);
btnStop.addEventListener("click", stopMonitor);
dialogClose?.addEventListener("click", () => hideDialog(false));
dialogOverlay?.addEventListener("click", (e) => {
  if (e.target === dialogOverlay) hideDialog(false);
});
btnExport?.addEventListener("click", () => {
  if (isMonitoring) {
    showToast("收集中，无法导出，请先结束收集", { type: "warn" });
    return;
  }
  if (!samples.length) {
    showToast("暂无数据可导出", { type: "warn" });
    return;
  }
  const payload = buildExportPayload();
  const ts = new Date().toISOString().replace(/[:.]/g, "-");
  downloadJson(`process-monitor-${ts}.json`, payload);
  showToast("已导出 JSON", { type: "success" });
});
btnImport?.addEventListener("click", () => {
  if (isMonitoring) {
    showToast("请先结束收集", { type: "warn" });
    return;
  }
  fileImport?.click();
});
fileImport?.addEventListener("change", async (e) => {
  const file = e.target?.files?.[0];
  if (!file) return;
  try {
    const text = await file.text();
    const obj = JSON.parse(text);
    let meta = null;
    let importedSamples = null;
    if (Array.isArray(obj)) {
      importedSamples = obj;
    } else if (obj && typeof obj === "object") {
      meta = obj.meta || null;
      importedSamples = obj.samples || null;
    }
    const sanitized = sanitizeImportedSamples(importedSamples);
    if (!sanitized.length) {
      showToast("导入失败：文件中没有有效 samples", { type: "error", durationMs: 4200 });
      return;
    }
    setImportedView(meta, sanitized);
    showToast(`已导入 ${sanitized.length} 条数据`, { type: "success", durationMs: 2600 });
  } catch (err) {
    showToast("导入失败：不是有效的 JSON", { type: "error", durationMs: 4200 });
  } finally {
    // 允许重复导入同一个文件
    if (fileImport) fileImport.value = "";
  }
});
freqSlider.addEventListener("input", onFreqChange);
netUnitSelect?.addEventListener("change", () => {
  updateCharts();
});
// 应用选择器：点击展开、复选框多选
appComboTrigger?.addEventListener("click", (e) => {
  e.stopPropagation();
  if (isMonitoring) {
    showToast("请先结束收集", { type: "warn" });
    return;
  }
  toggleAppDropdown();
});
appCombo?.addEventListener("click", (e) => {
  if (isMonitoring) {
    e.preventDefault();
    e.stopPropagation();
    showToast("请先结束收集", { type: "warn" });
    return;
  }
  if (e.target === appComboTrigger) return;
  if (appTags?.contains(e.target)) return;
  if (appDropdown?.contains(e.target)) return;
  showAppDropdown();
});
appInput?.addEventListener("focus", (e) => {
  if (isMonitoring) {
    e.target.blur();
    showToast("请先结束收集", { type: "warn" });
  }
});
appInput?.addEventListener("input", () => {
  if (isMonitoring) return;
  if (!appDropdown?.classList.contains("hidden")) {
    renderAppDropdown(appInput.value.trim());
  }
});
appInput?.addEventListener("blur", hideAppDropdown);
document.addEventListener("click", (e) => {
  if (appCombo?.contains(e.target)) return;
  if (!appDropdown?.classList.contains("hidden")) {
    appDropdown.classList.add("hidden");
    appComboTrigger?.classList.remove("open");
    if (appInput) {
      appInput.value = "";
    }
  }
});
appDropdown?.addEventListener("mousedown", (e) => {
  if (isMonitoring) return;
  if (appDropdownHideTimer) {
    clearTimeout(appDropdownHideTimer);
    appDropdownHideTimer = null;
  }
  const item = e.target.closest(".app-dropdown-item");
  if (!item || item.classList.contains("app-dropdown-empty")) return;
  e.preventDefault();
  toggleAppSelection(item.dataset.app);
});
appInput?.addEventListener("keydown", (e) => {
  if (appDropdown?.classList.contains("hidden")) return;
  const items = appDropdown.querySelectorAll(".app-dropdown-item");
  const active = appDropdown.querySelector(".app-dropdown-item.active");
  let idx = active ? [...items].indexOf(active) : -1;
  if (e.key === "ArrowDown") {
    e.preventDefault();
    idx = Math.min(idx + 1, items.length - 1);
    if (idx >= 0) {
      items.forEach((el) => el.classList.remove("active"));
      items[idx].classList.add("active");
      items[idx].scrollIntoView({ block: "nearest" });
    }
  } else if (e.key === "ArrowUp") {
    e.preventDefault();
    idx = Math.max(idx - 1, 0);
    if (items.length) {
      items.forEach((el) => el.classList.remove("active"));
      items[idx].classList.add("active");
      items[idx].scrollIntoView({ block: "nearest" });
    }
  } else if (e.key === "Enter" && idx >= 0 && items[idx]) {
    e.preventDefault();
    toggleAppSelection(items[idx].dataset.app);
  }
});
appInput.addEventListener("change", () => {
  // 同步更新 currentExes，避免进程弹窗等使用旧应用列表产生残留数据
  currentExes = [...selectedApps];
  stopStreaming();
  clearChartsData();
  hideProcModal();
  updateChartAppNotes();
  // 同步停止后端采集，避免直接切换应用后点击收集时前后端状态不一致
  fetch(`${API_BASE}/api/monitor/stop`, { method: "POST" }).catch(() => {});
});

procCount.addEventListener("click", openProcModal);
procModalClose?.addEventListener("click", hideProcModal);
procModalOverlay?.addEventListener("click", (e) => {
  if (e.target === procModalOverlay) hideProcModal();
});
window.addEventListener("keydown", (e) => {
  if (e.key === "Escape") {
    // 先关通用弹窗，再关进程详情弹窗
    if (dialogOverlay && !dialogOverlay.classList.contains("hidden")) {
      hideDialog(false);
      return;
    }
    hideProcModal();
  }
});

initCharts();
fetchApps();
fetchCapabilities();
updateAppComboDisabled();
updateMonitorButtons();
updateChartAppNotes();

diskPdhCheckbox?.addEventListener("change", updateChartDiskDesc);
diskPdhCheckbox?.addEventListener("change", () => {
  if (!pdhDiskIoAvailable) return;
  try {
    localStorage.setItem(LS_KEY_USE_PDH_DISK, diskPdhCheckbox.checked ? "1" : "0");
  } catch (e) {}
});
