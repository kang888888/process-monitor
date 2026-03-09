"""
REST API：枚举应用、启动/停止监控、配置。
同时提供静态页面（浏览器直接访问）。
"""
import os
from flask import Flask, jsonify, request, send_from_directory
from flask_cors import CORS

from src.config import DEFAULT_INTERVAL_MS, MIN_INTERVAL_MS, MAX_INTERVAL_MS
from src.collector import ProcessCollector

app = Flask(__name__)
CORS(app)

collector = ProcessCollector(window_seconds=600)

# 静态资源目录：Web 前端静态文件
# 注意：当前项目的静态文件在 src/web/
WEB_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "web"))

@app.after_request
def add_no_cache_headers(resp):
    # 开发/本地工具性质：避免浏览器缓存导致“改了代码但还在跑旧前端”
    path = (request.path or "").lower()
    if path == "/" or path.endswith((".js", ".css", ".html")):
        resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        resp.headers["Pragma"] = "no-cache"
        resp.headers["Expires"] = "0"
    return resp


@app.route("/", methods=["GET"])
def index():
    return send_from_directory(WEB_DIR, "index.html")


@app.route("/favicon.ico", methods=["GET"])
def favicon():
    # 前端未提供 favicon 时避免刷 404 日志
    return ("", 204)


@app.route("/<path:filename>", methods=["GET"])
def static_files(filename: str):
    return send_from_directory(WEB_DIR, filename)


def _list_exes():
    """枚举当前运行进程的 exe 名称（去重）"""
    seen = set()
    result = []
    import psutil
    for p in psutil.process_iter(["name", "exe"]):
        try:
            name = p.info.get("name") or p.info.get("exe") or ""
            if not name:
                continue
            base = name.split("\\")[-1].split("/")[-1].lower()
            if base and base not in seen:
                seen.add(base)
                result.append(base)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
    return sorted(result)


@app.route("/api/apps", methods=["GET"])
def list_apps():
    """获取可监控的应用（exe）列表"""
    return jsonify({"apps": _list_exes()})


@app.route("/api/monitor/start", methods=["POST"])
def start_monitor():
    """启动监控，支持单个 exeName 或多个 exeNames。usePdhDiskIo：Windows 下使用 PDH 磁盘 IO（与任务管理器对齐）。"""
    data = request.get_json() or {}
    interval_ms = data.get("intervalMs", DEFAULT_INTERVAL_MS)
    use_pdh_disk_io = data.get("usePdhDiskIo", False)
    exe_names = data.get("exeNames")
    if exe_names is None:
        exe_name = (data.get("exeName") or "").strip()
        exe_names = [exe_name] if exe_name else []
    else:
        exe_names = [str(e).strip() for e in exe_names if e and str(e).strip()]
    if not exe_names:
        return jsonify({"ok": False, "error": "exeName 或 exeNames 必填"}), 400
    interval_ms = max(MIN_INTERVAL_MS, min(MAX_INTERVAL_MS, interval_ms))
    collector.start(exe_names, interval_ms, use_pdh_disk_io=use_pdh_disk_io)
    return jsonify({
        "ok": True,
        "exeNames": exe_names,
        "intervalMs": interval_ms,
        "usePdhDiskIo": collector._use_pdh_disk_io,
    })


@app.route("/api/monitor/stop", methods=["POST"])
def stop_monitor():
    """停止监控"""
    collector.stop()
    return jsonify({"ok": True})


@app.route("/api/monitor/config", methods=["POST"])
def config_monitor():
    """动态更新采集频率（需已启动监控）"""
    data = request.get_json() or {}
    interval_ms = data.get("intervalMs")
    if interval_ms is None:
        return jsonify({"ok": False, "error": "intervalMs required"}), 400
    interval_ms = max(MIN_INTERVAL_MS, min(MAX_INTERVAL_MS, interval_ms))
    collector._interval_ms = interval_ms
    return jsonify({"ok": True, "intervalMs": interval_ms})


@app.route("/api/monitor/samples", methods=["GET"])
def get_samples():
    """获取当前窗口内所有采样（用于前端初始化/重连）"""
    return jsonify({"samples": collector.get_samples()})


@app.route("/api/monitor/latest", methods=["GET"])
def get_latest():
    """获取最新一条采样（用于轮询更新）"""
    return jsonify({"sample": collector.get_latest()})


def _parse_exe_names_from_request():
    """从请求中解析要查询的 exe 名称列表"""
    exe_list = request.args.getlist("exeNames")
    if exe_list:
        return [e.strip() for e in exe_list if e and str(e).strip()]
    exe_names = request.args.get("exeNames")
    if exe_names:
        return [e.strip() for e in exe_names.split(",") if e.strip()]
    exe_name = (request.args.get("exeName") or "").strip()
    if exe_name:
        return [exe_name]
    return list(collector._target_exes) if collector._target_exes else []


@app.route("/api/monitor/processes", methods=["GET"])
def get_processes():
    """获取当前监控应用的进程信息（用于弹窗查看），支持多应用"""
    import psutil
    exe_names = _parse_exe_names_from_request()
    if not exe_names:
        return jsonify({"ok": False, "error": "exeName 或 exeNames 必填"}), 400

    targets = {e.lower().split("\\")[-1].split("/")[-1] for e in exe_names}
    procs = []
    for p in psutil.process_iter(["pid", "name", "exe", "cmdline", "create_time", "status", "username", "memory_info"]):
        try:
            name = (p.info.get("name") or p.info.get("exe") or "").lower()
            if not name:
                continue
            base = name.split("\\")[-1].split("/")[-1]
            if base not in targets:
                continue
            mi = p.info.get("memory_info")
            rss_mb = round(((mi.rss or 0) / (1024 * 1024)), 2) if mi else 0.0
            cmdline = p.info.get("cmdline") or []
            procs.append(
                {
                    "pid": p.info.get("pid"),
                    "name": p.info.get("name"),
                    "exe": p.info.get("exe"),
                    "status": p.info.get("status"),
                    "username": p.info.get("username"),
                    "create_time": p.info.get("create_time"),
                    "rss_mb": rss_mb,
                    "cmdline": " ".join(cmdline) if isinstance(cmdline, list) else str(cmdline),
                }
            )
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
        except Exception:
            continue

    procs.sort(key=lambda x: (x.get("rss_mb") or 0.0), reverse=True)
    return jsonify({"ok": True, "exeNames": exe_names, "processes": procs, "count": len(procs)})


@app.route("/api/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"})


@app.route("/api/capabilities", methods=["GET"])
def capabilities():
    """返回能力标识，供前端决定是否展示 PDH 磁盘 IO 选项"""
    import sys
    from src.collector import _PDH_AVAILABLE
    return jsonify({
        "pdhDiskIoAvailable": sys.platform == "win32" and _PDH_AVAILABLE,
    })
