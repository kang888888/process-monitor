"""
WebSocket 服务：持续推送最新采样数据。
"""
import asyncio
import json
import time

from src.config import WS_PORT
from src.collector import ProcessCollector

# 使用全局 collector 实例（与 api.py 共享）
from src.api import collector


async def handler(websocket):
    """每个连接：按采集频率推送最新采样"""
    try:
        while True:
            sample = collector.get_latest()
            if sample:
                await websocket.send(json.dumps(sample))
            await asyncio.sleep(0.5)  # 推送频率与采集频率解耦，避免空推送
    except Exception:
        pass


async def main():
    import websockets
    async with websockets.serve(handler, "127.0.0.1", WS_PORT):
        await asyncio.Future()


if __name__ == "__main__":
    asyncio.run(main())
