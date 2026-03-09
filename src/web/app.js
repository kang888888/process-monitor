// 同域访问（页面由 Python 服务提供）
const API_BASE = location.origin;

const WINDOW_SECONDS = 600; // 10 分钟

const appSelect = document.getElementById("appSelect");
const btnRefresh = document.getElementById("btnRefresh");
const freqSlider = document.getElementById("freqSlider");
const freqLabel = document.getElementById("freqLabel");
const btnStart = document.getElementById("btnStart");
const btnStop = document.getElementById("btnStop");

const chartCpu = document.getElementById("chartCpu");
const chartMem = document.getElementById("chartMem");
const chartGpu = document.getElementById("chartGpu");
const chartDisk = document.getElementById("chartDisk");
const chartNet = document.getElementById("chartNet");
const netUnitSelect = document.getElementById("netUnitSelect");

const procCount = document.getElementById("procCount");
const liveCpu = document.getElementById("liveCpu");
const liveMem = document.getElementById("liveMem");
const liveGpu = document.getElementById("liveGpu");

const procModalOverlay = document.getElementById("procModalOverlay");
const procModalClose = document.getElementById("procModalClose");
const procModalMeta = document.getElementById("procModalMeta");
const procModalContent = document.getElementById("procModalContent");

let charts = {};
let samples = [];
let pollTimer = null;
let pollingInFlight = false;
let isMonitoring = false;
let currentExe = "";
let procModalLoading = false;

function bytesToMB(v) {
  const n = typeof v === "number" ? v : Number(v);
  if (!Number.isFinite(n) || n < 0) return 0;
  return n / (1024 * 1024);
}

/** 字节/秒 → 兆比特/秒 (Mbps, 1 Mbps = 10^6 bps) */
function bytesToMbps(bps) {
  const n = typeof bps === "number" ? bps : Number(bps);
  if (!Number.isFinite(n) || n < 0) return 0;
  return (n * 8) / 1e6;
}

function getNetUnit() {
  return netUnitSelect?.value === "Mbps" ? "Mbps" : "MB/s";
}

function netBpsToDisplay(bps) {
  return getNetUnit() === "Mbps" ? bytesToMbps(bps) : bytesToMB(bps);
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
  isMonitoring = false;
}

function clearStatus() {
  procCount.textContent = "-";
  liveCpu.textContent = "-";
  liveMem.textContent = "-";
  if (liveGpu) liveGpu.textContent = "-";
}

function clearChartsData() {
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
  if (!currentExe) return;

  procModalLoading = true;
  showProcModal();
  if (procModalMeta) procModalMeta.textContent = `应用：${currentExe}（加载中...）`;
  if (procModalContent) procModalContent.innerHTML = `<div style="color:#94a3b8;">正在获取进程信息...</div>`;

  try {
    const r = await fetch(`${API_BASE}/api/monitor/processes?exeName=${encodeURIComponent(currentExe)}`, { cache: "no-store" });
    const d = await r.json();
    if (!d.ok) {
      if (procModalMeta) procModalMeta.textContent = `应用：${currentExe}`;
      if (procModalContent) procModalContent.innerHTML = `<div style="color:#fca5a5;">${d.error || "获取失败"}</div>`;
      return;
    }
    if (procModalMeta) procModalMeta.textContent = `应用：${d.exeName}，进程数：${d.count}`;
    if (procModalContent) procModalContent.innerHTML = renderProcTable(d.processes);
  } catch (e) {
    if (procModalMeta) procModalMeta.textContent = `应用：${currentExe}`;
    if (procModalContent) procModalContent.innerHTML = `<div style="color:#fca5a5;">请求失败，请确认服务正常运行</div>`;
  } finally {
    procModalLoading = false;
  }
}

function initCharts() {
  const common = {
    grid: { left: 60, right: 20, top: 20, bottom: 30, containLabel: true },
    xAxis: { type: "time", boundaryGap: false },
    yAxis: {
      type: "value",
      splitLine: { lineStyle: { color: "#0f3460" } },
    },
    series: [{ type: "line", smooth: true, symbol: "none", sampling: "lttb" }],
  };

  charts.cpu = echarts.init(chartCpu);
  charts.cpu.setOption({
    ...common,
    tooltip: {
      trigger: "axis",
      valueFormatter: (v) => `${Number(v).toFixed(2)} %`,
    },
    yAxis: {
      ...common.yAxis,
      name: "CPU %",
      nameTextStyle: { color: "#94a3b8" },
      nameGap: 12,
      axisLabel: { formatter: (v) => `${v}` },
    },
    series: [{ ...common.series[0], name: "CPU %", itemStyle: { color: "#10b981" } }],
  });

  charts.mem = echarts.init(chartMem);
  charts.mem.setOption({
    ...common,
    tooltip: {
      trigger: "axis",
      valueFormatter: (v) => `${Number(v).toFixed(2)} MB`,
    },
    yAxis: {
      ...common.yAxis,
      name: "MB",
      nameTextStyle: { color: "#94a3b8" },
      nameGap: 12,
      axisLabel: { formatter: (v) => `${v}` },
    },
    series: [{ ...common.series[0], name: "内存 MB", itemStyle: { color: "#38bdf8" } }],
  });

  if (chartGpu) {
    charts.gpu = echarts.init(chartGpu);
    charts.gpu.setOption({
      ...common,
      tooltip: {
        trigger: "axis",
        valueFormatter: (v) => `${Number(v).toFixed(2)} %`,
      },
      yAxis: {
        ...common.yAxis,
        name: "%",
        nameTextStyle: { color: "#94a3b8" },
        nameGap: 12,
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
    legend: { top: 0, textStyle: { color: "#94a3b8" } },
    tooltip: {
      trigger: "axis",
      valueFormatter: (v) => `${Number(v).toFixed(2)} MB/s`,
    },
    yAxis: {
      ...common.yAxis,
      name: "MB/s",
      nameTextStyle: { color: "#94a3b8" },
      nameGap: 12,
      axisLabel: { formatter: (v) => `${v}` },
    },
    series: [
      { ...common.series[0], name: "读 MB/s", itemStyle: { color: "#f59e0b" } },
      { ...common.series[0], name: "写 MB/s", itemStyle: { color: "#ef4444" } },
    ],
  });

  charts.net = echarts.init(chartNet);
  charts.net.setOption({
    ...common,
    legend: { top: 0, textStyle: { color: "#94a3b8" } },
    tooltip: {
      trigger: "axis",
      valueFormatter: (v) => `${Number(v).toFixed(2)} ${getNetUnit()}`,
    },
    yAxis: {
      ...common.yAxis,
      name: getNetUnit(),
      nameTextStyle: { color: "#94a3b8" },
      nameGap: 12,
      axisLabel: { formatter: (v) => `${v}` },
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
  charts.mem.setOption({ series: [{ data: filtered.map((s) => [s.ts * 1000, s.mem_rss_mb]) }] });
  if (charts.gpu) {
    charts.gpu.setOption({
      series: [{ data: filtered.map((s) => [s.ts * 1000, s.gpu_pct != null ? s.gpu_pct : 0]) }]
    });
  }
  // 磁盘 IO：读/写分别绑定，避免与 series 顺序混淆导致两条线用错数据
  const diskReadData = filtered.map((s) => [s.ts * 1000, bytesToMB(s.disk_read_bps)]);
  const diskWriteData = filtered.map((s) => [s.ts * 1000, bytesToMB(s.disk_write_bps)]);
  charts.disk.setOption({
    series: [
      { name: "读 MB/s", data: diskReadData },
      { name: "写 MB/s", data: diskWriteData },
    ],
  });
  // 网络：纵轴单位由 netUnitSelect 决定，并对异常峰值做前端限幅
  const netUnit = getNetUnit();
  const recvValues = filtered.map((s) => netBpsToDisplay(s.net_recv_bps));
  const sentValues = filtered.map((s) => netBpsToDisplay(s.net_sent_bps));
  const recvCapped = capNetOutliers(recvValues);
  const sentCapped = capNetOutliers(sentValues);
  charts.net.setOption({
    tooltip: { valueFormatter: (v) => `${Number(v).toFixed(2)} ${netUnit}` },
    yAxis: { name: netUnit },
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
    appSelect.innerHTML = '<option value="">-- 选择应用 --</option>';
    (data.apps || []).forEach((a) => {
      const opt = document.createElement("option");
      opt.value = a;
      opt.textContent = a;
      appSelect.appendChild(opt);
    });
  } catch (e) {
    console.error("fetch apps failed", e);
  }
}

async function startMonitor() {
  const exe = appSelect.value?.trim();
  if (!exe) {
    alert("请先选择应用");
    return;
  }
  const interval = parseInt(freqSlider.value, 10);
  try {
    stopStreaming();
    // 每次点击“开始监控”都清空旧数据（即使还是同一个应用）
    currentExe = exe;
    clearChartsData();
    const res = await fetch(`${API_BASE}/api/monitor/start`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ exeName: exe, intervalMs: interval }),
    });
    const data = await res.json();
    if (!data.ok) {
      alert(data.error || "启动失败");
      return;
    }

    isMonitoring = true;

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
      } catch (e) {
        // 忽略瞬时失败（例如切换网络/服务短暂重启）
      } finally {
        pollingInFlight = false;
      }
    }, 500);
  } catch (e) {
    alert("连接采集服务失败，请确保 Python 服务已启动");
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
freqSlider.addEventListener("input", onFreqChange);
netUnitSelect?.addEventListener("change", () => {
  updateCharts();
});
appSelect.addEventListener("change", () => {
  // 切换下拉选择时，清空历史数据，避免误以为是新应用的曲线
  stopStreaming();
  clearChartsData();
  hideProcModal();
});

procCount.addEventListener("click", openProcModal);
procModalClose?.addEventListener("click", hideProcModal);
procModalOverlay?.addEventListener("click", (e) => {
  if (e.target === procModalOverlay) hideProcModal();
});
window.addEventListener("keydown", (e) => {
  if (e.key === "Escape") hideProcModal();
});

initCharts();
fetchApps();
