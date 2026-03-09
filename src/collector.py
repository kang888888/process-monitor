"""
进程指标采集器：按 exe 名称聚合所有实例的 CPU、内存、磁盘 IO、GPU。
Windows 下 GPU 使用 PDH 性能计数器 GPU Engine（与任务管理器对齐），仅支持 Windows。
"""
import logging
import os
import re
import sys
import time
import threading
from collections import deque
from dataclasses import dataclass, field
from typing import Optional

import psutil

logger = logging.getLogger(__name__)
# 设置 PROCESS_MONITOR_GPU_DEBUG=1 可输出 GPU 采集调试日志（启动前设置环境变量）
if os.environ.get("PROCESS_MONITOR_GPU_DEBUG"):
    _h = logging.StreamHandler()
    _h.setFormatter(logging.Formatter("[%(name)s] %(message)s"))
    logger.addHandler(_h)
    logger.setLevel(logging.DEBUG)

# 设置 PROCESS_MONITOR_COLLECT_DEBUG=1 可输出采集过程日志到 collect-debug.log，便于前后两次对比分析
_COLLECT_DEBUG = os.environ.get("PROCESS_MONITOR_COLLECT_DEBUG", "").strip().lower() in ("1", "true", "yes", "y", "on")
_COLLECT_LOG_PATH = os.path.join(os.getcwd(), "collect-debug.log")
if _COLLECT_DEBUG:
    _collect_handler = logging.FileHandler(_COLLECT_LOG_PATH, mode="a", encoding="utf-8")
    _collect_handler.setFormatter(logging.Formatter("%(asctime)s.%(msecs)03d [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S"))
    logger.addHandler(_collect_handler)
    logger.setLevel(logging.INFO)

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
    gpu_pct: float  # GPU 利用率 0~100，仅 Windows PDH 有值
    process_count: int
    pids: list[int] = field(default_factory=list)


class ProcessCollector:
    """按 exe 名称聚合进程指标"""

    def __init__(self, window_seconds: int = 600):
        self.window_seconds = window_seconds
        self._buffer: deque[tuple[float, Sample]] = deque()
        self._lock = threading.Lock()
        self._target_exes: set[str] = set()
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
        # 磁盘 IO：是否使用 PDH（仅 Windows，与任务管理器对齐，不含网络流量）
        self._use_pdh_disk_io: bool = False
        self._pdh_disk_zero_count: int = 0  # PDH 连续返回 0 的次数，用于自动回退
        # Windows PDH：磁盘 IO（Process\IO Read/Write Bytes/sec；回退用 IO Data Bytes/sec）
        self._pdh_query = None
        self._pdh_counter_read = None
        self._pdh_counter_write = None
        self._pdh_counter_data = None  # IO Data Bytes/sec，与 Get-Counter 一致，Read/Write 为 0 时回退
        # Windows GPU：GPU Engine Running time 差分得到利用率
        self._prev_gpu_running_sum: float = 0.0
        self._prev_gpu_ts: float = 0.0
        # 磁盘 IO：平滑与峰值抑制（与网络类似）
        self._prev_disk_rate: tuple[float, float] = (0.0, 0.0)  # (read_bps, write_bps)

    def _get_gpu_usage_pdh(self, pids: list[int], now: float) -> float:
        """Windows PDH：当前监控进程的 GPU 利用率（3D 引擎，与任务管理器对齐）。返回 0~100。"""
        if not pids or not _PDH_AVAILABLE or win32pdh is None:
            return 0.0
        pid_set = set(pids)
        # 使用 PDH_FMT_LARGE 避免 100ns 累积值溢出（PDH_FMT_LONG 会截断）
        _PDH_FMT_LARGE = getattr(win32pdh, "PDH_FMT_LARGE", 0x00000400)

        def _to_float(v):
            try:
                return float(v)
            except (TypeError, ValueError):
                if isinstance(v, (list, tuple)) and v:
                    try:
                        return float(v[0])
                    except (TypeError, ValueError):
                        pass
                return 0.0

        def _collect_gpu_items(object_name: str, counter_name: str):
            """用给定对象名和计数器名打开查询并返回 (instance -> value) 的 dict。"""
            query = ctr = None
            try:
                query = win32pdh.OpenQuery()
                path = win32pdh.MakeCounterPath(
                    (None, object_name, "*", None, 0, counter_name)
                )
                ctr = win32pdh.AddCounter(query, path)
                win32pdh.CollectQueryData(query)
                time.sleep(0.5)  # 建议 >500ms 以便 PDH 计算差值
                win32pdh.CollectQueryData(query)
                get_array = getattr(
                    win32pdh, "GetFormattedCounterValueArray", None
                ) or getattr(win32pdh, "GetFormattedCounterArray", None)
                if not get_array:
                    logger.debug("[GPU] GetFormattedCounterArray 不可用")
                    return None
                # 传入 PDH_FMT_LARGE 避免 100ns 累积值溢出
                try:
                    items = get_array(ctr, _PDH_FMT_LARGE)
                except TypeError:
                    items = get_array(ctr)
                return items
            except Exception as e:
                logger.debug("[GPU] _collect_gpu_items 失败: %s", e)
                return None
            finally:
                if ctr is not None and query is not None:
                    try:
                        win32pdh.RemoveCounter(ctr)
                        win32pdh.CloseQuery(query)
                    except Exception:
                        pass

        items = None
        # 1) 优先英文计数器（部分系统可用）
        try:
            query = win32pdh.OpenQuery()
            ctr = win32pdh.AddEnglishCounter(
                query, r"\GPU Engine(*)\Running time"
            )
            win32pdh.CollectQueryData(query)
            time.sleep(0.5)
            win32pdh.CollectQueryData(query)
            get_array = getattr(
                win32pdh, "GetFormattedCounterValueArray", None
            ) or getattr(win32pdh, "GetFormattedCounterArray", None)
            if get_array:
                try:
                    items = get_array(ctr, _PDH_FMT_LARGE)
                except TypeError:
                    items = get_array(ctr)
            win32pdh.RemoveCounter(ctr)
            win32pdh.CloseQuery(query)
        except Exception as e:
            logger.debug("[GPU] AddEnglishCounter 失败（可能需本地化）: %s", e)

        # 2) 若英文失败，枚举本地化对象与计数器
        if not items and getattr(win32pdh, "EnumObjectItems", None):
            # 可选：EnumObjects 列出所有对象，排查 GPU 相关
            if logger.isEnabledFor(logging.DEBUG) and getattr(win32pdh, "EnumObjects", None):
                try:
                    objs = win32pdh.EnumObjects(None, None, win32pdh.PERF_DETAIL_WIZARD, 0)
                    if isinstance(objs, str):
                        objs = [s for s in objs.split("\x00") if s.strip()]
                    gpu_objs = [o for o in objs if "gpu" in (o or "").lower()]
                    logger.debug("[GPU] 系统 PDH 对象中含 GPU: %s", gpu_objs[:20] if gpu_objs else "无")
                except Exception as e:
                    logger.debug("[GPU] EnumObjects 失败: %s", e)
            try:
                obj_name = "GPU Engine"
                counters_list, instances_list = win32pdh.EnumObjectItems(
                    None, None, obj_name, win32pdh.PERF_DETAIL_WIZARD, 0
                )
            except Exception as e:
                logger.debug("[GPU] EnumObjectItems(GPU Engine) 失败: %s", e)
                obj_name = None
                counters_list = []
            if obj_name and counters_list is not None:
                if isinstance(counters_list, str):
                    counters_list = [s for s in counters_list.split("\x00") if s.strip()]
                if not isinstance(counters_list, list):
                    counters_list = list(counters_list) if counters_list else []
                counter_name = None
                for c in counters_list:
                    cn = (c or "").strip()
                    if not cn:
                        continue
                    if "running" in cn.lower() or "time" in cn.lower() or "运行" in cn:
                        counter_name = cn
                        break
                if not counter_name and counters_list:
                    try:
                        counter_name = (counters_list[0].strip() if isinstance(counters_list[0], str) else str(counters_list[0])) or "Running time"
                    except Exception:
                        counter_name = "Running time"
                if counter_name:
                    logger.debug("[GPU] 使用本地化计数器: %s\\%s", obj_name, counter_name)
                    items = _collect_gpu_items(obj_name, counter_name)

        if not items:
            logger.debug("[GPU] 无 PDH 数据，请确认：1) 有 GPU 2) 驱动正常 3) 以管理员运行")
            return 0.0

        # 解析多实例：仅统计 engtype_3D（与任务管理器主表一致，避免多引擎累加超 100%）
        current_sum = 0.0
        matched_instances = []
        if isinstance(items, dict):
            for inst_name, val in items.items():
                inst = (inst_name or "").lower()
                m = re.search(r"pid_(\d+)", inst)
                if m and int(m.group(1)) in pid_set and "engtype_3d" in inst:
                    v = _to_float(val)
                    current_sum += v
                    matched_instances.append((inst_name, v))
        else:
            for item in items:
                if not isinstance(item, (list, tuple)) or len(item) < 2:
                    continue
                inst_name, val = item[0], item[1]
                inst = (str(inst_name) or "").lower()
                m = re.search(r"pid_(\d+)", inst)
                if m and int(m.group(1)) in pid_set and "engtype_3d" in inst:
                    v = _to_float(val)
                    current_sum += v
                    matched_instances.append((inst_name, v))

        if logger.isEnabledFor(logging.DEBUG) and matched_instances:
            logger.debug("[GPU] 匹配 3D 实例数=%d, current_sum=%.0f", len(matched_instances), current_sum)

        # 首次采样只建立基线
        if self._prev_gpu_ts <= 0:
            self._prev_gpu_ts = now
            self._prev_gpu_running_sum = current_sum
            logger.debug("[GPU] 首次采样建立基线, sum=%.0f", current_sum)
            return 0.0
        elapsed = max(now - self._prev_gpu_ts, 0.1)
        self._prev_gpu_ts = now
        delta = max(0.0, current_sum - self._prev_gpu_running_sum)
        self._prev_gpu_running_sum = current_sum
        # Running time 为 100ns 单位
        pct = (delta * 1e-7 / elapsed) * 100.0
        result = min(100.0, max(0.0, round(pct, 2)))
        if logger.isEnabledFor(logging.DEBUG) and result > 0:
            logger.debug("[GPU] delta=%.0f elapsed=%.2fs pct=%.2f", delta, elapsed, result)
        return result

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
            self._pdh_counter_data = None
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
            if self._pdh_counter_data is not None:
                try:
                    win32pdh.RemoveCounter(self._pdh_counter_data)
                except Exception:
                    pass
                self._pdh_counter_data = None
            if self._pdh_query is not None:
                try:
                    win32pdh.CloseQuery(self._pdh_query)
                except Exception:
                    pass
                self._pdh_query = None
        except Exception:
            pass

    def _init_pdh_disk_io(self, target_bases: set[str]) -> bool:
        """初始化 PDH 磁盘 IO 计数器（Process\\IO Read/Write Bytes/sec）。仅 Windows 有效。"""
        if not target_bases or not _PDH_AVAILABLE or win32pdh is None or sys.platform != "win32":
            return False
        self._close_pdh()

        def _try_add_counters(read_path: str, write_path: str, add_data_fallback: bool = True) -> bool:
            try:
                self._pdh_query = win32pdh.OpenQuery()
                self._pdh_counter_read = win32pdh.AddEnglishCounter(
                    self._pdh_query, read_path
                )
                self._pdh_counter_write = win32pdh.AddEnglishCounter(
                    self._pdh_query, write_path
                )
                if add_data_fallback:
                    try:
                        self._pdh_counter_data = win32pdh.AddEnglishCounter(
                            self._pdh_query, r"\Process(*)\IO Data Bytes/sec"
                        )
                    except Exception:
                        self._pdh_counter_data = None
                else:
                    self._pdh_counter_data = None
                # 速率计数器需两次采样建立基线，与 GPU 采集一致
                win32pdh.CollectQueryData(self._pdh_query)
                time.sleep(0.5)
                win32pdh.CollectQueryData(self._pdh_query)
                return True
            except Exception:
                return False

        # 1) 优先英文计数器（与 Get-Counter 一致；并添加 IO Data Bytes/sec 作回退）
        if _try_add_counters(
            r"\Process(*)\IO Read Bytes/sec",
            r"\Process(*)\IO Write Bytes/sec",
        ):
            return True

        self._close_pdh()

        # 2) 本地化回退：枚举 Process 对象的计数器
        if getattr(win32pdh, "EnumObjectItems", None):
            try:
                counters_list, _ = win32pdh.EnumObjectItems(
                    None, None, "Process", win32pdh.PERF_DETAIL_WIZARD, 0
                )
            except Exception:
                counters_list = []
            if isinstance(counters_list, str):
                counters_list = [s for s in counters_list.split("\x00") if s.strip()]
            read_cn = write_cn = None
            for c in counters_list or []:
                cn = (c or "").strip().lower()
                if "io read" in cn or "io 读取" in cn or "读取字节" in cn:
                    read_cn = c.strip()
                elif "io write" in cn or "io 写入" in cn or "写入字节" in cn:
                    write_cn = c.strip()
            if read_cn and write_cn:
                try:
                    self._pdh_query = win32pdh.OpenQuery()
                    path_r = win32pdh.MakeCounterPath(
                        (None, "Process", "*", None, 0, read_cn)
                    )
                    path_w = win32pdh.MakeCounterPath(
                        (None, "Process", "*", None, 0, write_cn)
                    )
                    self._pdh_counter_read = win32pdh.AddCounter(
                        self._pdh_query, path_r
                    )
                    self._pdh_counter_write = win32pdh.AddCounter(
                        self._pdh_query, path_w
                    )
                    win32pdh.CollectQueryData(self._pdh_query)
                    time.sleep(0.5)
                    win32pdh.CollectQueryData(self._pdh_query)
                    return True
                except Exception as e:
                    logger.debug("[Disk IO] PDH 本地化计数器失败: %s", e)

        self._close_pdh()
        return False

    def _pdh_value_to_float(self, val) -> float:
        """将 PDH 返回值转为 float，兼容多种格式。"""
        if isinstance(val, (int, float)):
            return float(val)
        if isinstance(val, (list, tuple)) and len(val) >= 1:
            return self._pdh_value_to_float(val[0])
        try:
            return float(val)
        except (TypeError, ValueError):
            return 0.0

    def _get_disk_io_pdh(self, target_bases: set[str]) -> tuple[float, float]:
        """从 PDH 获取目标进程的磁盘 IO 速率（read_bps, write_bps）。"""
        if not target_bases or not self._pdh_query or not _PDH_AVAILABLE or win32pdh is None:
            return 0.0, 0.0
        try:
            win32pdh.CollectQueryData(self._pdh_query)
            get_array = getattr(
                win32pdh, "GetFormattedCounterValueArray", None
            ) or getattr(win32pdh, "GetFormattedCounterArray", None)
            if not get_array:
                return 0.0, 0.0

            _PDH_FMT_DOUBLE = getattr(win32pdh, "PDH_FMT_DOUBLE", 0x00000100)

            def _sum_for_targets(counter_handle) -> float:
                try:
                    try:
                        items = get_array(counter_handle, _PDH_FMT_DOUBLE)
                    except TypeError:
                        items = get_array(counter_handle)
                except Exception:
                    return 0.0
                if not items:
                    return 0.0
                total = 0.0
                if isinstance(items, dict):
                    for inst_name, val in items.items():
                        base = (inst_name or "").split("#")[0].strip().lower()
                        if base in target_bases:
                            total += self._pdh_value_to_float(val)
                else:
                    for item in items:
                        if isinstance(item, (list, tuple)) and len(item) >= 2:
                            inst_name, val = item[0], item[1]
                        else:
                            continue
                        base = (str(inst_name) or "").split("#")[0].strip().lower()
                        if base in target_bases:
                            total += self._pdh_value_to_float(val)
                return total

            read_bps = _sum_for_targets(self._pdh_counter_read)
            write_bps = _sum_for_targets(self._pdh_counter_write)

            # 回退：当 Read/Write 均为 0 时使用 IO Data Bytes/sec（与 Get-Counter 一致）
            if read_bps == 0 and write_bps == 0 and getattr(self, "_pdh_counter_data", None) is not None:
                data_bps = _sum_for_targets(self._pdh_counter_data)
                if data_bps > 0:
                    read_bps = data_bps / 2.0
                    write_bps = data_bps / 2.0

            # 调试：PDH 返回 0 时输出实例列表（仅首次）
            if _COLLECT_DEBUG and read_bps == 0 and write_bps == 0:
                if not getattr(self, "_pdh_disk_debug_dumped", False):
                    self._pdh_disk_debug_dumped = True
                    try:
                        try:
                            items = get_array(self._pdh_counter_read, _PDH_FMT_DOUBLE)
                        except TypeError:
                            items = get_array(self._pdh_counter_read)
                        if isinstance(items, dict):
                            chrome_like = [(k, v) for k, v in items.items() if "chrome" in (k or "").lower()]
                            all_inst = list(items.items())[:25]
                            logger.info("[PDH 调试] target_bases=%s chrome实例=%s 全部实例(前25)=%s", target_bases, chrome_like[:15], all_inst)
                        elif items:
                            chrome_like = [(i[0], i[1]) for i in items if isinstance(i, (list, tuple)) and len(i) >= 2 and "chrome" in str(i[0]).lower()]
                            all_inst = [(i[0], i[1]) for i in items[:25] if isinstance(i, (list, tuple)) and len(i) >= 2]
                            logger.info("[PDH 调试] target_bases=%s chrome实例=%s 全部实例(前25)=%s", target_bases, chrome_like[:15], all_inst)
                    except Exception as e:
                        logger.info("[PDH 调试] 获取实例列表失败: %s", e)

            return read_bps, write_bps
        except Exception as e:
            logger.debug("[Disk IO] PDH CollectQueryData 失败: %s", e)
            return 0.0, 0.0

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

    def _get_pids_by_exes(self, exe_names: set[str]) -> list[int]:
        """获取多个 exe 的进程 PID 并集（去重）"""
        seen: set[int] = set()
        result: list[int] = []
        for exe in exe_names:
            for pid in self._get_pids_by_exe(exe):
                if pid not in seen:
                    seen.add(pid)
                    result.append(pid)
        return result

    def _sample_once(self) -> Optional[Sample]:
        """执行一次采样"""
        if not self._target_exes:
            return None
        pids = self._get_pids_by_exes(self._target_exes)
        if not pids:
            return Sample(
                ts=time.time(),
                cpu_pct=0.0,
                mem_rss_mb=0.0,
                disk_read_bps=0.0,
                disk_write_bps=0.0,
                net_recv_bps=0.0,
                net_sent_bps=0.0,
                gpu_pct=0.0,
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
        # 磁盘 IO：可选 PDH（与任务管理器一致，仅文件系统 IO）或 psutil（含网络等，易波动）
        dt = now - self._prev_io_ts if self._prev_io_ts else 1.0
        dt = max(dt, 0.05)
        self._prev_io_ts = now

        use_pdh = (
            self._use_pdh_disk_io
            and _PDH_AVAILABLE
            and sys.platform == "win32"
        )
        if use_pdh:
            target_bases = {self._exe_base_for_pdh(e) for e in self._target_exes}
            target_bases = {b for b in target_bases if b}
            io_read, io_write = self._get_disk_io_pdh(target_bases)
            if io_read == 0 and io_write == 0 and pids:
                self._pdh_disk_zero_count = getattr(self, "_pdh_disk_zero_count", 0) + 1
                if self._pdh_disk_zero_count >= 3:
                    self._use_pdh_disk_io = False
                    use_pdh = False
                    io_read, io_write = 0.0, 0.0
                    logger.info("[Disk IO] PDH 连续 3 拍返回 0，已自动回退到 psutil，本拍起使用 psutil 数据")
            else:
                self._pdh_disk_zero_count = 0
        else:
            io_read = 0.0
            io_write = 0.0

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
                # 异常峰值抑制：仅当疑似计数器回绕时抑制（速率从高突降到极低再飙到极高）
                # 原 4x 抑制会误伤下载场景（如 38KB/s -> 10MB/s），改为仅抑制明显异常（>100MB/s 且上一拍>1MB/s）
                prev_recv_bps, prev_sent_bps = self._prev_net_rate
                impossible_bps = 100 * 1024 * 1024  # 100 MB/s 单方向
                if prev_recv_bps > 1024 * 1024 and raw_recv > impossible_bps:
                    raw_recv = prev_recv_bps
                if prev_sent_bps > 1024 * 1024 and raw_sent > impossible_bps:
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
                # 磁盘 IO（psutil 路径）：读/写分别用 read_bytes、write_bytes 差分
                if not use_pdh:
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
                        else:
                            # 新进程（如下载时新建的 handler）：用 累计字节/进程存活时间 近似速率
                            # 仅计入存活时间较短的进程，避免长期运行进程首次纳入时虚高
                            try:
                                ctime = proc.create_time()
                                age = now - ctime if ctime else 0
                                if 0.1 < age < 15.0:
                                    io_read += read_bytes_curr / age
                                    io_write += write_bytes_curr / age
                            except Exception:
                                pass
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
        if not use_pdh:
            self._prev_io = new_io

        # 磁盘 IO：方案 A（最直觉）——直接展示 raw 速率（不做 EMA）
        warmup = getattr(self, "_disk_warmup_remaining", 0)
        if warmup > 0:
            self._disk_warmup_remaining = warmup - 1
            raw_read_bps = 0.0
            raw_write_bps = 0.0
            io_read = 0.0
            io_write = 0.0
        else:
            raw_read_bps = io_read
            raw_write_bps = io_write
            io_read = raw_read_bps
            io_write = raw_write_bps

        # cpu_time_delta_sum 是“所有目标进程在 dt 内消耗的 CPU 秒数之和”
        # 除以 (dt * cpu_count) 得到 0~1 的总占用比例，再乘 100 变成百分比
        cpu_pct = 0.0
        if cpu_dt > 0:
            cpu_pct = (cpu_time_delta_sum / (cpu_dt * self._cpu_count)) * 100.0

        # GPU：仅 Windows 且 PDH 可用时采集（3D 引擎利用率）
        gpu_pct = 0.0
        if sys.platform == "win32":
            gpu_pct = self._get_gpu_usage_pdh(pids, now)

        s = Sample(
            ts=now,
            cpu_pct=float(cpu_pct),
            mem_rss_mb=round(mem_sum, 2),
            disk_read_bps=round(io_read, 2),
            disk_write_bps=round(io_write, 2),
            net_recv_bps=round(net_recv_bps, 2),
            net_sent_bps=round(net_sent_bps, 2),
            gpu_pct=float(gpu_pct),
            process_count=len(pids),
            pids=pids.copy(),
        )
        if _COLLECT_DEBUG:
            s._raw_disk_r = raw_read_bps
            s._raw_disk_w = raw_write_bps
        return s

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
                # 采集调试日志：前 5 拍 + 之后每 10 拍输出一次（需 PROCESS_MONITOR_COLLECT_DEBUG=1）
                if _COLLECT_DEBUG:
                    cnt = getattr(self, "_sample_count", 0)
                    self._sample_count = cnt + 1
                    if cnt < 5 or (cnt >= 5 and (cnt - 5) % 10 == 0):
                        logger.info(
                            "[#%d] pid=%d cpu=%.2f mem=%.2f disk_r=%.0f disk_w=%.0f (raw_r=%.0f raw_w=%.0f) net_recv=%.0f net_sent=%.0f",
                            cnt + 1,
                            s.process_count,
                            s.cpu_pct,
                            s.mem_rss_mb,
                            s.disk_read_bps,
                            s.disk_write_bps,
                            getattr(s, "_raw_disk_r", 0),
                            getattr(s, "_raw_disk_w", 0),
                            s.net_recv_bps,
                            s.net_sent_bps,
                        )
            time.sleep(self._interval_ms / 1000.0)

    def start(
        self,
        exe_names: str | list[str],
        interval_ms: int = 1000,
        use_pdh_disk_io: bool = False,
    ):
        """启动采集，支持单个或多个 exe 名称。use_pdh_disk_io：Windows 下使用 PDH 磁盘 IO（与任务管理器对齐）。"""
        self.stop()
        if isinstance(exe_names, str):
            exe_names = [exe_names] if exe_names.strip() else []
        self._target_exes = {self._normalize_exe(e) for e in exe_names if e and str(e).strip()}
        self._interval_ms = max(200, min(5000, interval_ms))
        self._use_pdh_disk_io = bool(use_pdh_disk_io)
        self._pdh_disk_zero_count = 0
        # 每次开始监控都清空旧窗口数据，避免切换/重启后混入上一轮曲线
        with self._lock:
            self._buffer.clear()
        self._prev_io = {}
        self._prev_io_ts = 0
        self._prev_net = None
        self._prev_net_ts = 0.0
        self._prev_net_rate = (0.0, 0.0)
        self._prev_disk_rate = (0.0, 0.0)
        self._prev_gpu_running_sum = 0.0
        self._prev_gpu_ts = 0.0
        self._proc_cache = {}
        self._prev_cpu_time = {}
        self._prev_cpu_ts = 0.0
        self._running = True
        self._close_pdh()
        if self._use_pdh_disk_io and _PDH_AVAILABLE and sys.platform == "win32":
            target_bases = {self._exe_base_for_pdh(e) for e in self._target_exes}
            target_bases = {b for b in target_bases if b}
            if not self._init_pdh_disk_io(target_bases):
                self._use_pdh_disk_io = False  # PDH 初始化失败则回退到 psutil
        self._pdh_disk_debug_dumped = False
        # 预热：建立 pid->Process 缓存 + 记录初始 cpu_time 基线
        try:
            for pid in self._get_pids_by_exes(self._target_exes):
                try:
                    proc = psutil.Process(pid)
                    self._proc_cache[pid] = proc
                    ct = proc.cpu_times()
                    self._prev_cpu_time[pid] = float((ct.user or 0.0) + (ct.system or 0.0))
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    pass
        except Exception:
            pass
        # 预热磁盘 IO 与网络 IO：两次采样建立基线，第二次采样已有正确差分，直接入缓冲作为首拍
        _ = self._sample_once()  # 建立基线
        warmup_s = max(0.5, self._interval_ms / 2000)
        time.sleep(warmup_s)
        s = self._sample_once()  # 已有正确 disk/net 差分
        if s:
            with self._lock:
                self._buffer.append((s.ts, s))

        # 采集调试日志：开始收集后输出，便于前后两次对比分析（需 PROCESS_MONITOR_COLLECT_DEBUG=1）
        if _COLLECT_DEBUG:
            pids = self._get_pids_by_exes(self._target_exes)
            logger.info(
                "========== 开始收集 (日志: %s) ========== exe=%s interval_ms=%d use_pdh_disk=%s pid_count=%d pids=%s",
                _COLLECT_LOG_PATH,
                sorted(self._target_exes),
                self._interval_ms,
                self._use_pdh_disk_io,
                len(pids),
                pids[:20] if len(pids) > 20 else pids,
            )
            if s:
                logger.info(
                    "[首拍] cpu=%.2f mem=%.2f disk_r=%.0f disk_w=%.0f (raw_r=%.0f raw_w=%.0f) net_recv=%.0f net_sent=%.0f",
                    s.cpu_pct,
                    s.mem_rss_mb,
                    s.disk_read_bps,
                    s.disk_write_bps,
                    getattr(s, "_raw_disk_r", 0),
                    getattr(s, "_raw_disk_w", 0),
                    s.net_recv_bps,
                    s.net_sent_bps,
                )

        self._sample_count = 0
        # 磁盘 IO 预热：前 6 拍（约 6 秒）不输出且不更新 EMA 基线，避免首段异常尖峰/拖尾
        self._disk_warmup_remaining = 6
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()

    def stop(self):
        """停止采集"""
        if _COLLECT_DEBUG and self._running:
            logger.info("========== 结束收集 ==========")
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
        self._prev_gpu_running_sum = 0.0
        self._prev_gpu_ts = 0.0

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
                    "gpu_pct": s.gpu_pct,
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
                "gpu_pct": s.gpu_pct,
                "process_count": s.process_count,
            }
