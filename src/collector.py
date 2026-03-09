"""
进程指标采集器：按 exe 名称聚合所有实例的 CPU、内存、磁盘 IO。
Windows 下磁盘 IO 使用 PDH 性能计数器（与任务管理器同源），非 Windows 或 PDH 不可用时回退到 psutil。
"""
import sys
import time
import threading
from collections import deque
from dataclasses import dataclass, field
from typing import Optional

import psutil

# Windows：使用 PDH 获取与任务管理器一致的磁盘 IO 速率（IO Read/Write Bytes/sec）
if sys.platform == "win32":
    try:
        import win32pdh  # pywin32
        _PDH_AVAILABLE = True
    except ImportError:
        win32pdh = None  # type: ignore
        _PDH_AVAILABLE = False
else:
    win32pdh = None  # type: ignore
    _PDH_AVAILABLE = False


@dataclass
class Sample:
    """单次采样数据"""
    ts: float
    cpu_pct: float
    mem_rss_mb: float
    disk_read_bps: float
    disk_write_bps: float
    net_recv_bps: float
    net_sent_bps: float
    process_count: int
    pids: list[int] = field(default_factory=list)


class ProcessCollector:
    """按 exe 名称聚合进程指标"""

    def __init__(self, window_seconds: int = 600):
        self.window_seconds = window_seconds
        self._buffer: deque[tuple[float, Sample]] = deque()
        self._lock = threading.Lock()
        self._target_exe: Optional[str] = None
        self._interval_ms = 1000
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._prev_io: dict[int, tuple[int, int]] = {}
        self._prev_io_ts: float = 0
        self._prev_net: tuple[int, int] | None = None  # (prev_sent, prev_recv) 下行=recv 上行=sent
        self._prev_net_ts: float = 0.0
        self._prev_net_rate: tuple[float, float] = (0.0, 0.0)  # (recv_bps, sent_bps) 用于抑制异常峰值
        # 复用 Process 对象用于读取信息
        self._proc_cache: dict[int, psutil.Process] = {}
        # 用 CPU time 差分计算占用率，避免 cpu_percent() 在 Windows 下“总是 0”的坑
        self._prev_cpu_time: dict[int, float] = {}
        self._prev_cpu_ts: float = 0.0
        self._cpu_count: int = psutil.cpu_count() or 1
        # Windows PDH：与任务管理器一致的磁盘 IO 速率
        self._pdh_query = None
        self._pdh_counter_read = None
        self._pdh_counter_write = None

    def _normalize_exe(self, name: str) -> str:
        """统一 exe 名称格式（小写、去路径）"""
        if not name:
            return ""
        return name.lower().strip().split("\\")[-1].split("/")[-1]

    def _exe_base_for_pdh(self, normalized_exe: str) -> str:
        """PDH 实例名通常不带 .exe，如 chrome、chrome#1。返回用于匹配的进程名。"""
        if not normalized_exe:
            return ""
        base = normalized_exe.lower()
        if base.endswith(".exe"):
            base = base[:-4]
        return base

    def _sum_pdh_counter_for_process(
        self, counter_handle, target_base: str
    ) -> float:
        """对 PDH 多实例计数器的值按进程名（target_base）求和。实例名形如 chrome、chrome#1。"""
        if not counter_handle or not target_base:
            return 0.0
        try:
            # pywin32: GetFormattedCounterValueArray 或 GetFormattedCounterArray 返回 {实例名: 值}
            get_array = getattr(
                win32pdh, "GetFormattedCounterValueArray", None
            ) or getattr(win32pdh, "GetFormattedCounterArray", None)
            if not get_array:
                return 0.0
            items = get_array(counter_handle)
        except Exception:
            return 0.0
        if not items:
            return 0.0
        total = 0.0
        # 兼容 dict 或 list of (name, value)
        if isinstance(items, dict):
            for inst_name, val in items.items():
                base = (inst_name or "").split("#")[0].strip().lower()
                if base == target_base and isinstance(val, (int, float)):
                    total += float(val)
        else:
            for item in items:
                if isinstance(item, (list, tuple)) and len(item) >= 2:
                    inst_name, val = item[0], item[1]
                else:
                    continue
                base = (str(inst_name) or "").split("#")[0].strip().lower()
                if base == target_base and isinstance(val, (int, float)):
                    total += float(val)
        return total

    def _close_pdh(self) -> None:
        """关闭 PDH 查询与计数器，释放资源。"""
        if not _PDH_AVAILABLE or win32pdh is None:
            self._pdh_query = None
            self._pdh_counter_read = None
            self._pdh_counter_write = None
            return
        try:
            if self._pdh_counter_read is not None:
                try:
                    win32pdh.RemoveCounter(self._pdh_counter_read)
                except Exception:
                    pass
                self._pdh_counter_read = None
            if self._pdh_counter_write is not None:
                try:
                    win32pdh.RemoveCounter(self._pdh_counter_write)
                except Exception:
                    pass
                self._pdh_counter_write = None
            if self._pdh_query is not None:
                try:
                    win32pdh.CloseQuery(self._pdh_query)
                except Exception:
                    pass
                self._pdh_query = None
        except Exception:
            pass

    def _get_pids_by_exe(self, exe_name: str) -> list[int]:
        """获取指定 exe 的所有进程 PID"""
        target = self._normalize_exe(exe_name)
        if not target:
            return []
        pids = []
        for p in psutil.process_iter(["pid", "name", "exe"]):
            try:
                name = (p.info.get("name") or p.info.get("exe") or "").lower()
                if not name:
                    continue
                base = name.split("\\")[-1].split("/")[-1]
                if base == target:
                    pids.append(p.info["pid"])
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
        return pids

    def _sample_once(self) -> Optional[Sample]:
        """执行一次采样"""
        if not self._target_exe:
            return None
        pids = self._get_pids_by_exe(self._target_exe)
        if not pids:
            return Sample(
                ts=time.time(),
                cpu_pct=0.0,
                mem_rss_mb=0.0,
                disk_read_bps=0.0,
                disk_write_bps=0.0,
                net_recv_bps=0.0,
                net_sent_bps=0.0,
                process_count=0,
                pids=[],
            )

        cpu_time_delta_sum = 0.0
        mem_sum = 0.0
        io_read = 0.0
        io_write = 0.0
        now = time.time()
        cpu_dt = now - self._prev_cpu_ts if self._prev_cpu_ts else 0.0
        self._prev_cpu_ts = now
        # 磁盘 IO：Windows 优先用 PDH（与任务管理器一致），否则用 psutil 累计值差分
        dt = now - self._prev_io_ts if self._prev_io_ts else 1.0
        dt = max(dt, 0.05)
        self._prev_io_ts = now

        # 磁盘 IO：统一用 psutil 进程 io_counters 差分，保证有数据展示（Windows 上为进程总 IO）
        # 此前 PDH 方案在部分环境取不到数导致无展示，已停用

        # 网络：全局网卡上下行（整机汇总），带异常峰值抑制
        # 下行 = bytes_recv（接收），上行 = bytes_sent（发送）
        net_dt = now - self._prev_net_ts if self._prev_net_ts else 0.0
        self._prev_net_ts = now
        net_recv_bps = self._prev_net_rate[0]
        net_sent_bps = self._prev_net_rate[1]
        try:
            nio = psutil.net_io_counters()
            sent = int(getattr(nio, "bytes_sent", 0) or 0)
            recv = int(getattr(nio, "bytes_recv", 0) or 0)
            # 仅当间隔足够大时用差分算速率，避免双采样/抖动导致瞬时尖峰
            min_net_dt = 0.25
            max_net_bps = 600 * 1024 * 1024  # 单方向速率上限 600 MB/s
            if self._prev_net is not None and net_dt >= min_net_dt:
                prev_sent, prev_recv = self._prev_net
                delta_sent = sent - prev_sent
                delta_recv = recv - prev_recv
                max_delta = 500 * 1024 * 1024 * net_dt
                raw_recv = (delta_recv / net_dt) if 0 <= delta_recv <= max_delta else net_recv_bps
                raw_sent = (delta_sent / net_dt) if 0 <= delta_sent <= max_delta else net_sent_bps
                raw_recv = min(raw_recv, max_net_bps)
                raw_sent = min(raw_sent, max_net_bps)
                # 异常峰值抑制：若当前速率远高于上一拍且上一拍非零，则沿用上一拍
                prev_recv_bps, prev_sent_bps = self._prev_net_rate
                spike_factor = 4.0
                if prev_recv_bps > 1000 and raw_recv > spike_factor * prev_recv_bps:
                    raw_recv = prev_recv_bps
                if prev_sent_bps > 1000 and raw_sent > spike_factor * prev_sent_bps:
                    raw_sent = prev_sent_bps
                net_recv_bps = raw_recv
                net_sent_bps = raw_sent
            self._prev_net = (sent, recv)
            self._prev_net_rate = (net_recv_bps, net_sent_bps)
        except Exception:
            pass

        new_io: dict[int, tuple[int, int]] = {}
        alive_pids = set(pids)
        for pid in pids:
            try:
                proc = self._proc_cache.get(pid)
                if proc is None:
                    proc = psutil.Process(pid)
                    self._proc_cache[pid] = proc
                # CPU：用 cpu_times 差分（user+system）累计
                try:
                    ct = proc.cpu_times()
                    total_cpu_time = float((ct.user or 0.0) + (ct.system or 0.0))
                    prev_total = self._prev_cpu_time.get(pid)
                    if prev_total is not None and cpu_dt > 0:
                        cpu_time_delta_sum += max(0.0, total_cpu_time - prev_total)
                    self._prev_cpu_time[pid] = total_cpu_time
                except Exception:
                    pass
                # 内存：优先用 USS（独占内存）避免多进程共享页被重复累加
                # 在 Windows 任务管理器中，“内存”更接近私有工作集/独占视角，用 USS 求和通常更贴近
                mem_mb = None
                try:
                    mfi = proc.memory_full_info()
                    uss = getattr(mfi, "uss", None)
                    if uss is not None:
                        mem_mb = float(uss) / (1024 * 1024)
                except Exception:
                    mem_mb = None
                if mem_mb is None:
                    try:
                        mem_mb = float(proc.memory_info().rss or 0) / (1024 * 1024)
                    except Exception:
                        mem_mb = 0.0
                mem_sum += mem_mb
                # 磁盘 IO：读/写分别用 read_bytes、write_bytes 差分，不可混用
                io = proc.io_counters()
                if io:
                    read_bytes_curr = io.read_bytes
                    write_bytes_curr = io.write_bytes
                    new_io[pid] = (read_bytes_curr, write_bytes_curr)
                    if pid in self._prev_io:
                        prev_read, prev_write = self._prev_io[pid]
                        delta_read = max(0, read_bytes_curr - prev_read)
                        delta_write = max(0, write_bytes_curr - prev_write)
                        io_read += delta_read / dt
                        io_write += delta_write / dt
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                self._proc_cache.pop(pid, None)
                self._prev_cpu_time.pop(pid, None)
                pass
        # 清理已退出进程
        if self._proc_cache:
            for pid in list(self._proc_cache.keys()):
                if pid not in alive_pids:
                    self._proc_cache.pop(pid, None)
        if self._prev_cpu_time:
            for pid in list(self._prev_cpu_time.keys()):
                if pid not in alive_pids:
                    self._prev_cpu_time.pop(pid, None)
        self._prev_io = new_io

        # cpu_time_delta_sum 是“所有目标进程在 dt 内消耗的 CPU 秒数之和”
        # 除以 (dt * cpu_count) 得到 0~1 的总占用比例，再乘 100 变成百分比
        cpu_pct = 0.0
        if cpu_dt > 0:
            cpu_pct = (cpu_time_delta_sum / (cpu_dt * self._cpu_count)) * 100.0

        return Sample(
            ts=now,
            cpu_pct=float(cpu_pct),
            mem_rss_mb=round(mem_sum, 2),
            disk_read_bps=round(io_read, 2),
            disk_write_bps=round(io_write, 2),
            net_recv_bps=round(net_recv_bps, 2),
            net_sent_bps=round(net_sent_bps, 2),
            process_count=len(pids),
            pids=pids.copy(),
        )

    def _run_loop(self):
        """采集循环"""
        while self._running:
            s = self._sample_once()
            if s:
                with self._lock:
                    self._buffer.append((s.ts, s))
                    cutoff = time.time() - self.window_seconds
                    while self._buffer and self._buffer[0][0] < cutoff:
                        self._buffer.popleft()
            time.sleep(self._interval_ms / 1000.0)

    def start(self, exe_name: str, interval_ms: int = 1000):
        """启动采集"""
        self.stop()
        self._target_exe = exe_name
        self._interval_ms = max(200, min(5000, interval_ms))
        # 每次开始监控都清空旧窗口数据，避免切换/重启后混入上一轮曲线
        with self._lock:
            self._buffer.clear()
        self._prev_io = {}
        self._prev_io_ts = 0
        self._prev_net = None
        self._prev_net_ts = 0.0
        self._prev_net_rate = (0.0, 0.0)
        self._proc_cache = {}
        self._prev_cpu_time = {}
        self._prev_cpu_ts = 0.0
        self._running = True
        self._close_pdh()
        # 预热：建立 pid->Process 缓存 + 记录初始 cpu_time 基线
        try:
            for pid in self._get_pids_by_exe(self._target_exe):
                try:
                    proc = psutil.Process(pid)
                    self._proc_cache[pid] = proc
                    ct = proc.cpu_times()
                    self._prev_cpu_time[pid] = float((ct.user or 0.0) + (ct.system or 0.0))
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    pass
        except Exception:
            pass
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()

    def stop(self):
        """停止采集"""
        self._running = False
        if self._thread:
            self._thread.join(timeout=2)
            self._thread = None
        self._close_pdh()
        self._proc_cache = {}
        self._prev_cpu_time = {}
        self._prev_cpu_ts = 0.0
        self._prev_net = None
        self._prev_net_ts = 0.0
        self._prev_net_rate = (0.0, 0.0)

    def get_samples(self) -> list[dict]:
        """获取窗口内所有采样（用于前端初始化）"""
        with self._lock:
            cutoff = time.time() - self.window_seconds
            return [
                {
                    "ts": ts,
                    "cpu_pct": s.cpu_pct,
                    "mem_rss_mb": s.mem_rss_mb,
                    "disk_read_bps": s.disk_read_bps,
                    "disk_write_bps": s.disk_write_bps,
                    "net_recv_bps": s.net_recv_bps,
                    "net_sent_bps": s.net_sent_bps,
                    "process_count": s.process_count,
                }
                for ts, s in self._buffer
                if ts >= cutoff
            ]

    def get_latest(self) -> Optional[dict]:
        """获取最新一条采样（用于 WebSocket 增量推送）"""
        with self._lock:
            if not self._buffer:
                return None
            _, s = self._buffer[-1]
            return {
                "ts": s.ts,
                "cpu_pct": s.cpu_pct,
                "mem_rss_mb": s.mem_rss_mb,
                "disk_read_bps": s.disk_read_bps,
                "disk_write_bps": s.disk_write_bps,
                "net_recv_bps": s.net_recv_bps,
                "net_sent_bps": s.net_sent_bps,
                "process_count": s.process_count,
            }
