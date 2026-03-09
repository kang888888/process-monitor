/**
 * Electron 主进程：启动 Python 采集服务并加载 Web UI
 */
const path = require('path');
const http = require('http');
const { app, BrowserWindow, Menu } = require('electron');
const { spawn } = require('child_process');

// Windows：设置 AppUserModelID，避免任务栏/快捷方式图标不更新或显示为默认 Electron 图标
if (process.platform === 'win32') {
  app.setAppUserModelId('com.process-monitor.app');
}

// 开发时把 userData 放到项目目录，避免默认缓存路径被占用导致「拒绝访问」
if (!app.isPackaged) {
  app.setPath('userData', path.join(__dirname, '.electron-userdata'));
}
// 关闭 GPU 着色器磁盘缓存，避免 Unable to create cache / Gpu Cache Creation failed
app.commandLine.appendSwitch('disable-gpu-shader-disk-cache');
app.commandLine.appendSwitch('disable-gpu-sandbox');

const API_PORT = 8799;
const API_URL = `http://127.0.0.1:${API_PORT}`;
const HEALTH_URL = `${API_URL}/api/health`;

let pythonProcess = null;
let mainWindow = null;

function getAppPath() {
  if (app.isPackaged) {
    return path.join(process.resourcesPath, 'app');
  }
  return __dirname;
}

function startPython() {
  const appPath = getAppPath();
  let pythonCmd = process.platform === 'win32' ? 'python' : 'python3';
  let env = { ...process.env, PYTHONPATH: appPath };

  // 打包后的 Windows 版本：优先使用内置的嵌入式 Python
  if (process.platform === 'win32' && app.isPackaged) {
    const embedDir = path.join(appPath, 'python');
    const embedExe = path.join(embedDir, 'python.exe');
    pythonCmd = embedExe;
    env = {
      ...env,
      PYTHONHOME: embedDir,
    };
  }

  pythonProcess = spawn(pythonCmd, ['main.py'], {
    cwd: appPath,
    env,
    stdio: ['ignore', 'pipe', 'pipe'],
  });

  pythonProcess.stdout.on('data', (data) => {
    if (process.env.DEBUG) process.stdout.write(data);
  });
  pythonProcess.stderr.on('data', (data) => {
    if (process.env.DEBUG) process.stderr.write(data);
  });
  pythonProcess.on('error', (err) => {
    console.error('Python 启动失败:', err.message);
  });
  pythonProcess.on('exit', (code, signal) => {
    pythonProcess = null;
    if (code !== null && code !== 0 && mainWindow && !mainWindow.isDestroyed()) {
      mainWindow.webContents.send('python-exit', { code, signal });
    }
  });

  return pythonProcess;
}

function waitForServer(maxWaitMs = 15000, intervalMs = 200) {
  const start = Date.now();
  return new Promise((resolve, reject) => {
    function poll() {
      const url = new URL(HEALTH_URL);
      const req = http.get({ hostname: url.hostname, port: url.port, path: url.pathname }, (res) => {
        if (res.statusCode === 200) return resolve(true);
        tryNext();
      });
      req.on('error', tryNext);
      function tryNext() {
        req.destroy();
        if (Date.now() - start >= maxWaitMs) {
          reject(new Error('采集服务启动超时，请确认已安装 Python 及依赖 (pip install -r requirements.txt)'));
          return;
        }
        setTimeout(poll, intervalMs);
      }
    }
    setTimeout(poll, 300);
  });
}

function createWindow() {
  const appPath = getAppPath();
  const iconPath = path.join(appPath, 'src', 'web', 'img', 'app.ico');
  mainWindow = new BrowserWindow({
    width: 1200,
    height: 780,
    minWidth: 800,
    minHeight: 500,
    webPreferences: {
      nodeIntegration: false,
      contextIsolation: true,
    },
    title: '进程监控',
    icon: iconPath,
  });

  mainWindow.loadURL(API_URL);
  mainWindow.on('closed', () => { mainWindow = null; });

  // 右键菜单：在加载远程 URL 时默认无菜单，显式提供「检查」打开 DevTools
  mainWindow.webContents.on('context-menu', (_event, params) => {
    const menu = Menu.buildFromTemplate([
      {
        label: '检查',
        click: () => {
          mainWindow.webContents.inspectElement(params.x, params.y);
          mainWindow.webContents.openDevTools();
        },
      },
    ]);
    menu.popup();
  });
}

app.whenReady().then(() => {
  startPython();
  waitForServer()
    .then(createWindow)
    .catch((err) => {
      console.error(err.message);
      const { dialog } = require('electron');
      dialog.showErrorBox('启动失败', err.message);
      app.quit();
    });
});

app.on('window-all-closed', () => {
  if (pythonProcess) {
    pythonProcess.kill();
    pythonProcess = null;
  }
  app.quit();
});

app.on('quit', () => {
  if (pythonProcess) {
    pythonProcess.kill();
    pythonProcess = null;
  }
});
