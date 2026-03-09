# 进程监控 (Process Monitor)

Windows 下按应用（exe）聚合监控 CPU、内存、磁盘 IO，ECharts 展示最近 10 分钟滚动曲线。

## 技术栈

- **采集端**：Python + psutil + Flask + WebSockets
- **展示端**：浏览器 + ECharts（由 Python 提供静态页面）

## 快速开始

### 1. 安装 Python 依赖

```bash
pip install -r requirements.txt
```

### 2. 启动 Python 采集服务

```bash
python .\main.py
```

服务将监听：
- REST API: `http://127.0.0.1:8799`
- WebSocket: 默认不启动（可选开启）

如需开启 WebSocket 推送（可能遇到端口占用时可关闭它只走 API）：

```powershell
$env:COLLECTOR_ENABLE_WS = 1
# 可选：自定义 WS 端口
$env:COLLECTOR_WS_PORT = 8877
python .\main.py
```

### 3. 打开浏览器页面

启动采集服务后，在浏览器访问：

`http://127.0.0.1:8799/`

### 4. 使用

1. 点击「刷新列表」获取当前运行进程的 exe 列表
2. 选择要监控的应用（如 `chrome.exe`）
3. 调整采集频率（200ms～5s）
4. 点击「开始监控」

## Electron 桌面运行 / 打包

用 Electron 打包成桌面应用后，会先启动 Python 采集服务，再在窗口内打开 Web UI。

### 前置条件

- 已安装 Node.js（建议 18+）
- 已安装 Python 及项目依赖：`pip install -r requirements.txt`
- 打包出的安装包在目标机器上也需要已安装 Python，或使用 PyInstaller 等先打包 Python 再由 Electron 调用

### 开发模式（Electron 壳 + 本机 Python）

```bash
npm install
npm start
```

会启动 Electron 窗口并自动运行 `python main.py`，窗口加载 `http://127.0.0.1:8799`。

### 打包成安装包（Windows）

```bash
npm run build
# 或仅打 Windows 安装包
npm run build:win
```

产物在 `dist-win/` 目录（如 NSIS 安装程序）。安装后运行「进程监控」会先启动内嵌的 Python 脚本（需目标机已安装 Python 并安装过依赖），再打开界面。

构建已关闭代码签名（`forceCodeSigning: false`），避免在无管理员权限下解压 winCodeSign 时因创建符号链接失败而报错。若需签名，可改回并**以管理员身份**运行终端再执行 `npm run build:win`。

若出现 **EPERM: operation not permitted**：先关掉占用项目目录的进程（如本项目的 Electron 窗口、`python main.py`、IDE 对该目录的占用），再删掉 `dist-win` 与 `.electron-app` 后重试（`npm run build:win` 会先准备 `.electron-app` 并输出到 `dist-win`）；若仍报错可尝试用**管理员权限**打开终端再执行一次。若报错与 winCodeSign 解压/符号链接有关，可先删除缓存目录后再构建：`Remove-Item -Recurse -Force $env:LOCALAPPDATA\electron-builder\Cache\winCodeSign -ErrorAction SilentlyContinue`

若希望**单文件免 Python 环境**，可先用 PyInstaller 把 `main.py` 打成 exe，再把该 exe 放入 `extraResources` 由 Electron 启动（需自行修改 `main.js` 中的启动命令与 `package.json` 的 `extraResources`）。

## 项目结构

```
process-monitor/
├── main.py               # Python 入口（REST + WebSocket）
├── main.js               # Electron 主进程（启动 Python + 开窗口）
├── package.json          # Node/Electron 依赖与打包配置
├── src/                  # Python 采集服务实现
│   ├── __init__.py
│   ├── api.py            # REST 接口 + 静态文件托管
│   ├── collector.py      # 进程指标采集与聚合
│   ├── config.py         # 配置
│   └── ws_server.py      # （可选）单独运行的 WebSocket 服务
│   └── web/              # Web 前端静态文件（被 Python 服务托管，默认用 API 轮询）
│       ├── index.html
│       ├── app.js        # 前端逻辑 + ECharts（轮询 /api/monitor/latest）
│       └── styles.css
├── requirements.txt
└── README.md
```

## API

| 接口 | 方法 | 说明 |
|------|------|------|
| `/api/apps` | GET | 枚举可监控的 exe 列表 |
| `/api/monitor/start` | POST | 启动监控 `{exeName, intervalMs}` |
| `/api/monitor/stop` | POST | 停止监控 |
| `/api/monitor/config` | POST | 动态更新 `{intervalMs}` |
| `/api/monitor/samples` | GET | 获取当前窗口内采样数据 |
| `/api/monitor/latest` | GET | 获取最新一条采样（轮询用） |

## 后续扩展

- GPU 指标（Windows 性能计数器 / ETW）
- 网络按进程汇总（ETW）
- 导出 CSV/JSON
