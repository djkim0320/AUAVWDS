const { app, BrowserWindow, ipcMain, dialog } = require('electron');
const path = require('node:path');
const fs = require('node:fs');
const os = require('node:os');
const net = require('node:net');
const { spawn } = require('node:child_process');

let mainWindow = null;
let backendProc = null;
let backendPort = null;
let shuttingDown = false;
const mainLogPath = path.join(os.tmpdir(), 'auavwds-main.log');

function logMain(message) {
  try {
    const line = `[${new Date().toISOString()}] ${message}\n`;
    fs.appendFileSync(mainLogPath, line, 'utf8');
  } catch (_err) {}
}

function isDev() {
  return Boolean(process.env.VITE_DEV_SERVER_URL);
}

function userWorkDir() {
  const p = path.join(app.getPath('userData'), 'work');
  fs.mkdirSync(p, { recursive: true });
  return p;
}

function userLogDir() {
  const p = path.join(app.getPath('userData'), 'logs');
  fs.mkdirSync(p, { recursive: true });
  return p;
}

function choosePort() {
  return new Promise((resolve, reject) => {
    const server = net.createServer();

    server.unref();
    server.on('error', reject);
    server.listen(0, '127.0.0.1', () => {
      const address = server.address();
      const port = typeof address === 'object' && address ? address.port : null;
      server.close((err) => {
        if (err) {
          reject(err);
          return;
        }
        if (!port) {
          reject(new Error('Failed to allocate a backend port'));
          return;
        }
        resolve(port);
      });
    });
  });
}

function backendCommand() {
  if (isDev()) {
    return {
      cmd: 'python',
      args: ['backend/main.py'],
      cwd: app.getAppPath(),
    };
  }

  const candidates = [
    path.join(process.resourcesPath, 'backend', 'backend.exe'),
    path.join(process.resourcesPath, 'backend', 'backend', 'backend.exe'),
  ];
  const exe = candidates.find((p) => fs.existsSync(p));
  if (!exe) {
    logMain(`backend.exe not found candidates=${JSON.stringify(candidates)}`);
    throw new Error(`backend.exe not found. looked in: ${candidates.join(', ')}`);
  }
  logMain(`backend.exe selected=${exe}`);
  return {
    cmd: exe,
    args: [],
    cwd: path.dirname(exe),
  };
}

function solverBinDir() {
  if (isDev()) {
    return path.join(app.getAppPath(), 'third_party', 'openvsp', 'win64');
  }
  return path.join(process.resourcesPath, 'bin', 'win64');
}

async function waitForHealth(baseUrl, timeoutMs = 30000) {
  logMain(`waitForHealth start baseUrl=${baseUrl}`);
  const start = Date.now();
  while (Date.now() - start < timeoutMs) {
    try {
      if (typeof fetch !== 'function') {
        throw new Error('global fetch is not available in main process');
      }
      const res = await fetch(`${baseUrl}/health`);
      if (res.ok) return;
    } catch (_err) {}
    await new Promise((r) => setTimeout(r, 500));
  }
  logMain(`waitForHealth timeout baseUrl=${baseUrl}`);
  throw new Error('Backend health-check timeout');
}

async function startBackend() {
  backendPort = await choosePort();
  const baseUrl = `http://127.0.0.1:${backendPort}`;

  const { cmd, args, cwd } = backendCommand();
  const env = {
    ...process.env,
    AUAV_BACKEND_HOST: '127.0.0.1',
    AUAV_BACKEND_PORT: String(backendPort),
    APP_WORK_DIR: userWorkDir(),
    APP_LOG_DIR: userLogDir(),
    AUAV_RESOURCES_PATH: process.resourcesPath,
    AUAV_SOLVER_BIN_DIR: solverBinDir(),
  };

  backendProc = spawn(cmd, args, {
    cwd,
    env,
    windowsHide: true,
    stdio: ['ignore', 'pipe', 'pipe'],
  });

  backendProc.stdout?.on('data', (chunk) => {
    logMain(`[backend:stdout] ${String(chunk).trim()}`);
  });
  backendProc.stderr?.on('data', (chunk) => {
    logMain(`[backend:stderr] ${String(chunk).trim()}`);
  });
  backendProc.on('error', (err) => {
    logMain(`[backend:error] ${err?.stack || err}`);
  });

  backendProc.on('exit', () => {
    if (!shuttingDown) {
      console.error('[AUAVWDS] backend exited unexpectedly');
      logMain('[backend:exit] exited unexpectedly');
    }
  });

  await waitForHealth(baseUrl);
  return baseUrl;
}

async function invokeBackend(pathname, method = 'GET', body) {
  if (!backendPort) {
    throw new Error('Backend is not started');
  }
  const url = `http://127.0.0.1:${backendPort}${pathname}`;
  const res = await fetch(url, {
    method,
    headers: { 'Content-Type': 'application/json' },
    body: body ? JSON.stringify(body) : undefined,
  });

  const text = await res.text();
  let payload = {};
  try {
    payload = text ? JSON.parse(text) : {};
  } catch {
    payload = { detail: text || 'Invalid backend response' };
  }

  if (!res.ok) {
    const msg = payload?.detail || `${method} ${pathname} failed`;
    throw new Error(String(msg));
  }
  return payload;
}

function registerIpc() {
  ipcMain.handle('backend:state', () => invokeBackend('/state/client'));
  ipcMain.handle('backend:state-full', () => invokeBackend('/state'));
  ipcMain.handle('backend:chat', (_evt, req) => invokeBackend('/chat', 'POST', req));
  ipcMain.handle('backend:command', (_evt, req) => invokeBackend('/command', 'POST', req));
  ipcMain.handle('backend:reset', () => invokeBackend('/reset', 'POST'));

  ipcMain.handle('backend:list-saves', () => invokeBackend('/saves'));
  ipcMain.handle('backend:save', (_evt, req) => invokeBackend('/saves', 'POST', req));
  ipcMain.handle('backend:load-save', (_evt, req) => invokeBackend('/saves/load', 'POST', req));
  ipcMain.handle('backend:compare-saves', (_evt, req) => invokeBackend('/saves/compare', 'POST', req));
  ipcMain.handle('backend:export-cfd', (_evt, req) => invokeBackend('/export/cfd', 'POST', req));
}

async function createWindow() {
  await startBackend();

  mainWindow = new BrowserWindow({
    width: 1600,
    height: 980,
    minWidth: 1200,
    minHeight: 760,
    backgroundColor: '#050b16',
    webPreferences: {
      preload: path.join(__dirname, 'preload.cjs'),
      contextIsolation: true,
      nodeIntegration: false,
    },
  });

  const devUrl = process.env.VITE_DEV_SERVER_URL;
  if (devUrl) {
    await mainWindow.loadURL(devUrl);
  } else {
    await mainWindow.loadFile(path.join(app.getAppPath(), 'dist', 'index.html'));
  }

  mainWindow.on('closed', () => {
    mainWindow = null;
  });
}

function stopBackend() {
  shuttingDown = true;
  if (backendProc && !backendProc.killed) {
    try {
      backendProc.kill();
    } catch (_err) {}
  }
  backendProc = null;
}

app.whenReady().then(async () => {
  logMain('app.whenReady');
  registerIpc();
  await createWindow();

  app.on('activate', async () => {
    if (BrowserWindow.getAllWindows().length === 0) {
      await createWindow();
    }
  });
}).catch((err) => {
  logMain(`[main:startup:error] ${err?.stack || err}`);
  try {
    dialog.showErrorBox('AUAVWDS Startup Error', String(err?.message || err));
  } catch (_e) {}
  app.quit();
});

app.on('before-quit', () => {
  logMain('before-quit');
  stopBackend();
});

app.on('window-all-closed', () => {
  logMain('window-all-closed');
  stopBackend();
  if (process.platform !== 'darwin') {
    app.quit();
  }
});

process.on('uncaughtException', (err) => {
  logMain(`[uncaughtException] ${err?.stack || err}`);
});

process.on('unhandledRejection', (reason) => {
  logMain(`[unhandledRejection] ${reason}`);
});

