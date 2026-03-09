"""
采集服务入口：同时启动 REST API/Web UI 与 WebSocket。
"""
import os
import sys
import threading

# 确保打包后仍能正确找到 src 包
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SRC_DIR = os.path.join(BASE_DIR, "src")
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)
if SRC_DIR not in sys.path:
    sys.path.insert(0, SRC_DIR)

from src.config import API_PORT, WS_PORT, ENABLE_WS
from src.api import app as flask_app


def run_ws():
    import asyncio
    import websockets

    async def _serve():
        async with websockets.serve(_handler, "127.0.0.1", WS_PORT):
            await asyncio.Future()

    async def _handler(websocket):
        from src.api import collector
        import json
        try:
            while True:
                sample = collector.get_latest()
                if sample:
                    await websocket.send(json.dumps(sample))
                await asyncio.sleep(0.5)
        except Exception:
            pass

    asyncio.run(_serve())


def run_api():
    flask_app.run(host="127.0.0.1", port=API_PORT, threaded=True, use_reloader=False)


if __name__ == "__main__":
    # 默认不启用 WebSocket，避免端口占用；需要时用环境变量开启：
    # $env:COLLECTOR_ENABLE_WS = 1
    if ENABLE_WS:
        ws_thread = threading.Thread(target=run_ws, daemon=True)
        ws_thread.start()

    # 主线程运行 Flask
    run_api()
