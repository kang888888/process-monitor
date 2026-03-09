"""采集服务配置"""
import os

# 数据窗口：最近 N 秒
WINDOW_SECONDS = 600  # 10 分钟

# 默认采样间隔（毫秒）
DEFAULT_INTERVAL_MS = 1000

# 最小/最大采样间隔（毫秒）
MIN_INTERVAL_MS = 200
MAX_INTERVAL_MS = 5000

# WebSocket 端口
WS_PORT = int(os.environ.get("COLLECTOR_WS_PORT", 8765))

# 是否启用 WebSocket（默认关闭：只走 REST API）
ENABLE_WS = os.environ.get("COLLECTOR_ENABLE_WS", "0").strip().lower() in ("1", "true", "yes", "y", "on")

# REST API 端口
API_PORT = int(os.environ.get("COLLECTOR_API_PORT", 8799))

# Web UI 端口（提供静态页面）
WEB_PORT = int(os.environ.get("COLLECTOR_WEB_PORT", 8777))
