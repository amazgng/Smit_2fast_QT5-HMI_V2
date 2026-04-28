import { app, BrowserWindow, ipcMain } from 'electron';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { spawn } from 'node:child_process';
import http from 'node:http';

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);

const BACKEND_HOST = '127.0.0.1';
const DEFAULT_HTTP_PORT = 18080;
const DEFAULT_HOST_PORT = 13001;
const DEFAULT_LOOM_PORT = 13000;
const WRITE_AUTH_USER = 'smit';
const WRITE_AUTH_PASSWORD = '2fast';

let mainWindow;
let backendProcess = null;
let backendStarting = null;
let backendCookie = '';
let backendLastError = '';
let backendLogLines = [];

function appendBackendLog(line) {
  const text = String(line || '').trim();
  if (!text) return;
  backendLogLines.push(`[${new Date().toISOString()}] ${text}`);
  backendLogLines = backendLogLines.slice(-300);
  if (mainWindow && !mainWindow.isDestroyed()) {
    mainWindow.webContents.send('backend:log', backendLogLines.slice(-80));
  }
}

function projectRoot() {
  return path.join(__dirname, '..');
}

function backendSourceDir() {
  return app.isPackaged
    ? path.join(process.resourcesPath, 'backend')
    : path.join(projectRoot(), 'backend');
}

function runtimeBackendDir() {
  return path.join(app.getPath('userData'), 'backend-runtime');
}

function runtimeConfigPath() {
  return path.join(runtimeBackendDir(), 'loom_host_master_config_v23.runtime.json');
}

function readJsonSafe(filePath, fallback = {}) {
  try {
    return JSON.parse(fs.readFileSync(filePath, 'utf-8'));
  } catch {
    return fallback;
  }
}

function normalizeRuntimeConfig(config) {
  const runtimeDir = runtimeBackendDir();
  return {
    listen_host: config.listen_host ?? '0.0.0.0',
    host_port: Number(config.host_port ?? DEFAULT_HOST_PORT),
    enable_udp_listener: config.enable_udp_listener ?? true,
    ts_reply_timeout_seconds: Number(config.ts_reply_timeout_seconds ?? 2.0),
    ts_connect_timeout_seconds: Number(config.ts_connect_timeout_seconds ?? 3.0),
    enable_http_dashboard: true,
    http_host: BACKEND_HOST,
    http_port: Number(config.http_port ?? DEFAULT_HTTP_PORT),
    sqlite_db_path: path.join(runtimeDir, 'loom_events.db'),
    log_dir: path.join(runtimeDir, 'logs'),
    config_backup_dir: path.join(runtimeDir, 'config_backups'),
    write_actions_enabled: config.write_actions_enabled ?? true,
    looms: Array.isArray(config.looms) ? config.looms.map((loom) => ({
      id: loom.id || loom.ip || loom.ipAddress || `loom-${Date.now()}`,
      name: loom.name || loom.ip || '2fast-loom-01',
      ip: loom.ip || loom.ipAddress || '169.254.4.101',
      host_port: Number(loom.host_port ?? loom.hostPort ?? config.host_port ?? DEFAULT_HOST_PORT),
      ts_port: Number(loom.ts_port ?? loom.loomPort ?? DEFAULT_LOOM_PORT),
      supports_qt5_full_status: loom.supports_qt5_full_status ?? true,
      declarations: loom.declarations || {
        '565': 'Employee ID: _____ Production Plan ID: _____ Yarn Package Number: _____'
      },
      enabled: loom.enabled !== false,
      poll_status_every_seconds: Number(loom.poll_status_every_seconds ?? 5),
      poll_full_status_every_seconds: Number(loom.poll_full_status_every_seconds ?? 10)
    })) : []
  };
}

function ensureRuntimeConfig() {
  const runtimeDir = runtimeBackendDir();
  fs.mkdirSync(runtimeDir, { recursive: true });
  fs.mkdirSync(path.join(runtimeDir, 'logs'), { recursive: true });
  fs.mkdirSync(path.join(runtimeDir, 'config_backups'), { recursive: true });

  const target = runtimeConfigPath();
  const source = path.join(backendSourceDir(), 'loom_host_master_config_v23.example.json');
  if (!fs.existsSync(target)) {
    if (fs.existsSync(source)) {
      fs.copyFileSync(source, target);
    } else {
      fs.writeFileSync(target, JSON.stringify({
        listen_host: '0.0.0.0',
        host_port: DEFAULT_HOST_PORT,
        enable_udp_listener: true,
        ts_reply_timeout_seconds: 2.0,
        ts_connect_timeout_seconds: 3.0,
        enable_http_dashboard: true,
        http_host: BACKEND_HOST,
        http_port: DEFAULT_HTTP_PORT,
        sqlite_db_path: 'loom_events.db',
        looms: []
      }, null, 2), 'utf-8');
    }
  }

  const normalized = normalizeRuntimeConfig(readJsonSafe(target, {}));
  fs.writeFileSync(target, JSON.stringify(normalized, null, 2), 'utf-8');
  return target;
}

function getRuntimeConfig() {
  ensureRuntimeConfig();
  return readJsonSafe(runtimeConfigPath(), normalizeRuntimeConfig({}));
}

function getHttpPort() {
  return Number(getRuntimeConfig().http_port ?? DEFAULT_HTTP_PORT);
}

function nowInBeijing() {
  return new Intl.DateTimeFormat('sv-SE', {
    timeZone: 'Asia/Shanghai',
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
    hour12: false
  }).format(new Date()).replace('T', ' ');
}

function httpRequest(method, requestPath, body = null, extraHeaders = {}) {
  const port = getHttpPort();
  const bodyBuffer = body == null ? null : Buffer.from(body, 'utf-8');
  return new Promise((resolve, reject) => {
    const req = http.request({
      hostname: BACKEND_HOST,
      port,
      path: requestPath,
      method,
      timeout: 5000,
      headers: {
        ...(bodyBuffer ? {
          'Content-Type': 'application/x-www-form-urlencoded;charset=UTF-8',
          'Content-Length': bodyBuffer.length
        } : {}),
        ...extraHeaders
      }
    }, (res) => {
      const chunks = [];
      res.on('data', (chunk) => chunks.push(chunk));
      res.on('end', () => {
        const text = Buffer.concat(chunks).toString('utf-8');
        let json = null;
        try { json = text ? JSON.parse(text) : null; } catch {}
        resolve({ statusCode: res.statusCode || 0, headers: res.headers, text, json });
      });
    });
    req.on('timeout', () => {
      req.destroy(new Error(`Backend HTTP timeout: ${method} ${requestPath}`));
    });
    req.on('error', reject);
    if (bodyBuffer) req.write(bodyBuffer);
    req.end();
  });
}

async function backendHealth() {
  try {
    const res = await httpRequest('GET', '/health');
    return res.statusCode === 200 && res.text.trim() === 'OK';
  } catch {
    return false;
  }
}

function wait(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

async function waitForBackend(timeoutMs = 20000) {
  const started = Date.now();
  while (Date.now() - started < timeoutMs) {
    if (await backendHealth()) return true;
    await wait(500);
  }
  return false;
}

function pythonCandidates() {
  if (process.env.PYTHON && process.env.PYTHON.trim()) {
    return [{ command: process.env.PYTHON.trim(), prefixArgs: [] }];
  }
  if (process.platform === 'win32') {
    return [
      { command: 'py', prefixArgs: ['-3'] },
      { command: 'python', prefixArgs: [] },
      { command: 'python3', prefixArgs: [] }
    ];
  }
  return [
    { command: 'python3', prefixArgs: [] },
    { command: 'python', prefixArgs: [] }
  ];
}

async function spawnBackendWith(candidate) {
  ensureRuntimeConfig();
  const scriptPath = path.join(backendSourceDir(), 'loom_host_master.py');
  if (!fs.existsSync(scriptPath)) {
    throw new Error(`Backend script not found: ${scriptPath}`);
  }

  const args = [
    ...candidate.prefixArgs,
    scriptPath,
    '--config', runtimeConfigPath(),
    '--log-level', 'INFO',
    'run'
  ];

  appendBackendLog(`Starting backend: ${candidate.command} ${args.join(' ')}`);
  const child = spawn(candidate.command, args, {
    cwd: backendSourceDir(),
    windowsHide: true,
    env: {
      ...process.env,
      PYTHONUNBUFFERED: '1',
      PYTHONPATH: backendSourceDir()
    }
  });

  backendProcess = child;

  child.stdout.on('data', (data) => appendBackendLog(data.toString('utf-8')));
  child.stderr.on('data', (data) => appendBackendLog(data.toString('utf-8')));

  child.on('error', (error) => {
    backendLastError = `${candidate.command}: ${error.message}`;
    appendBackendLog(`Backend start error: ${backendLastError}`);
  });

  child.on('exit', (code, signal) => {
    const msg = `Backend exited with code=${code ?? 'null'} signal=${signal ?? 'null'}`;
    backendLastError = msg;
    appendBackendLog(msg);
    if (backendProcess === child) backendProcess = null;
  });

  const healthy = await waitForBackend(12000);
  if (!healthy) {
    try { child.kill(); } catch {}
    throw new Error(`${candidate.command} started, but backend /health did not become ready.`);
  }
  backendLastError = '';
  return true;
}

async function startBackend() {
  if (await backendHealth()) {
    backendLastError = '';
    appendBackendLog('Using existing backend at http://127.0.0.1:' + getHttpPort());
    return true;
  }
  if (backendStarting) return backendStarting;

  backendStarting = (async () => {
    const errors = [];
    for (const candidate of pythonCandidates()) {
      try {
        await spawnBackendWith(candidate);
        return true;
      } catch (error) {
        errors.push(error.message);
        backendLastError = error.message;
        appendBackendLog(error.message);
      }
    }
    throw new Error(`Unable to start Python backend. Tried: ${errors.join(' | ')}`);
  })();

  try {
    return await backendStarting;
  } finally {
    backendStarting = null;
  }
}

async function stopOwnedBackend() {
  if (!backendProcess) return false;
  const child = backendProcess;
  appendBackendLog('Stopping backend to apply updated IP Management configuration.');
  try { child.kill(); } catch {}
  await wait(900);
  if (backendProcess === child) {
    try { child.kill('SIGKILL'); } catch {}
    backendProcess = null;
  }
  backendCookie = '';
  return true;
}

async function restartOwnedBackendAfterConfigSave() {
  const stopped = await stopOwnedBackend();
  if (!stopped) {
    appendBackendLog('Config saved. Backend was not started by this Electron process, so it was not auto-restarted.');
    return false;
  }
  await startBackend();
  appendBackendLog('Backend restarted with updated IP Management configuration.');
  return true;
}

async function ensureBackendReady() {
  if (await backendHealth()) return true;
  return startBackend();
}

function encodeForm(data) {
  const params = new URLSearchParams();
  for (const [key, value] of Object.entries(data || {})) {
    if (value !== undefined && value !== null) params.append(key, String(value));
  }
  return params.toString();
}

async function getJson(pathName) {
  await ensureBackendReady();
  const res = await httpRequest('GET', pathName);
  if (res.statusCode < 200 || res.statusCode >= 300) {
    throw new Error(`Backend GET ${pathName} failed: HTTP ${res.statusCode} ${res.text}`);
  }
  return res.json;
}

async function postBackend(pathName, data = {}, { writeAuth = false } = {}) {
  await ensureBackendReady();
  const headers = {};
  if (writeAuth) {
    await ensureWriteAuth();
    if (backendCookie) headers.Cookie = backendCookie;
  }
  const res = await httpRequest('POST', pathName, encodeForm(data), headers);
  if (res.statusCode < 200 || res.statusCode >= 300 || !res.json || res.json.ok === false) {
    const errorText = res.json?.error || res.text || `HTTP ${res.statusCode}`;
    throw new Error(errorText);
  }
  return res.json;
}

async function ensureWriteAuth() {
  const res = await httpRequest('POST', '/auth/login', encodeForm({
    username: WRITE_AUTH_USER,
    password: WRITE_AUTH_PASSWORD
  }));
  if (res.statusCode < 200 || res.statusCode >= 300 || !res.json?.ok) {
    throw new Error(res.json?.error || 'Backend write authentication failed');
  }
  const setCookie = res.headers['set-cookie'];
  if (Array.isArray(setCookie) && setCookie.length) {
    backendCookie = setCookie.map((item) => item.split(';')[0]).join('; ');
  }
  return true;
}

function asNumber(value, fallback = 0) {
  const num = Number(value);
  return Number.isFinite(num) ? num : fallback;
}

function titleCaseStatus(value) {
  const text = String(value || '').replace(/_/g, ' ').trim();
  if (!text) return 'Unknown';
  return text.replace(/\b\w/g, (m) => m.toUpperCase());
}

function firstDefined(...values) {
  for (const value of values) {
    if (value !== undefined && value !== null && value !== '') return value;
  }
  return undefined;
}

function normalizeSpeedRpm(value) {
  const numeric = Number(value);
  if (!Number.isFinite(numeric)) return value;
  let rpm = numeric;
  const absRpm = Math.abs(rpm);
  // Some loom replies encode speed as rpm x 100, e.g. 50432 => 504.32 rpm.
  if (absRpm > 10000) {
    rpm = rpm / 100;
  } else if (absRpm > 2500) {
    const candidate = rpm / 100;
    if (Math.abs(candidate) <= 2500) rpm = candidate;
  }
  return Math.round(rpm * 10) / 10;
}

function normalizeDensityValue(rawValue, unitText) {
  const numeric = Number(rawValue);
  if (!Number.isFinite(numeric)) return { value: '—', unit: 'weft/dm' };
  const unit = String(unitText || '').toLowerCase();
  if (unit.includes('cm')) {
    return { value: (numeric * 10).toFixed(1), unit: 'weft/dm' };
  }
  if (unit.includes('inch') || unit.includes('ppi')) {
    return { value: (numeric * 3.937007874).toFixed(1), unit: 'weft/dm' };
  }
  return { value: numeric.toFixed(1), unit: 'weft/dm' };
}

function normalizeSpeedHistory(history) {
  if (!Array.isArray(history)) return [];
  return history
    .map((item) => {
      let value;
      if (typeof item === 'number') value = item;
      else if (item && typeof item === 'object') value = firstDefined(item.speed_rpm, item.speedRpm, item.value, item.rpm);
      else value = item;
      const numeric = Number(value);
      return Number.isFinite(numeric) ? normalizeSpeedRpm(numeric) : null;
    })
    .filter((value) => Number.isFinite(value));
}


function normalizeAdminCompletion(completion) {
  if (!completion || completion.hasData === false) return null;
  const fields = completion.fields || completion.adminFields || completion.parsedFields || {};
  const receivedAt = firstDefined(completion.receivedAt, completion.ts_iso, completion.tsIso, completion.updated_at_iso, completion.updatedAt, nowInBeijing());
  const normalized = {
    receivedAt,
    employeeId: firstDefined(completion.employeeId, completion.employee_id, fields.employeeId, fields.employee_id, ''),
    productionPlanId: firstDefined(completion.productionPlanId, completion.production_plan_id, fields.productionPlanId, fields.production_plan_id, ''),
    yarnPackageNumber: firstDefined(completion.yarnPackageNumber, completion.yarn_package_number, fields.yarnPackageNumber, fields.yarn_package_number, ''),
    rawText: firstDefined(completion.completedText, completion.completed_text, completion.reply, completion.text, completion.rawText, '')
  };
  if (!normalized.employeeId && !normalized.productionPlanId && !normalized.yarnPackageNumber && !normalized.rawText) return null;
  return normalized;
}

function normalizeReadAllResult(result, loom = {}) {
  const commands = result?.commands || {};
  const fullStatus = commands.full_status || {};
  const fullEvent = fullStatus?.decoded?.event || {};
  const fullInterpreted = fullStatus?.decoded?.interpreted || {};
  const basicStatus = commands.status || {};
  const basicInterpreted = basicStatus?.decoded?.interpreted || {};
  const speedCmd = commands.speed || {};
  const totalPicksCmd = commands.total_picks || {};
  const densityCmd = commands.density || {};
  const patternCmd = commands.pattern_current || {};
  const basicConfig = commands.basic_config || {};

  const speed = normalizeSpeedRpm(firstDefined(fullEvent.speed_rpm, speedCmd.speed_rpm, basicStatus?.decoded?.event?.speed_rpm));
  const picks = firstDefined(fullEvent.total_picks, totalPicksCmd.total_picks);
  const densityRaw = firstDefined(densityCmd.selected_density, fullEvent.density_weft_per_dm);
  const density = normalizeDensityValue(densityRaw, densityCmd.unit_text || (fullEvent.density_weft_per_dm ? 'weft/dm' : ''));
  const interpreted = Object.keys(fullInterpreted).length ? fullInterpreted : basicInterpreted;
  const running = interpreted.running === true;
  const category = interpreted.category || (running ? 'running' : 'unknown');
  const allCommandValues = Object.values(commands);
  const failedCommands = allCommandValues.filter((item) => item && item.error).length;
  const online = allCommandValues.length === 0 ? true : failedCommands < allCommandValues.length;
  const lastSeen = result?.updated_at_iso || nowInBeijing();
  const loomName = loom.name || result?.loom_name || result?.loomName || loom.ipAddress || result?.loom_ip || 'Configured Loom';
  const runtime = result?.runtime || {};
  const speedHistory = normalizeSpeedHistory(firstDefined(result?.speedHistory, runtime.speedHistoryValues, runtime.speedHistory));
  const runtimeMinutes = asNumber(firstDefined(result?.runtimeMinutes, runtime.runtimeMinutes, speedHistory.length), speedHistory.length || 1);

  return {
    loomName,
    status: online ? 'Online' : 'Offline',
    hostPort: asNumber(loom.hostPort, DEFAULT_HOST_PORT),
    loomPort: asNumber(loom.loomPort, DEFAULT_LOOM_PORT),
    protocol: 'TCP',
    currentSpeed: firstDefined(speed, '—'),
    picks: firstDefined(picks, 0),
    density: density.value,
    densityUnit: density.unit || 'weft/dm',
    currentPattern: patternCmd.name || '—',
    shift: fullEvent.shift !== undefined ? `Shift ${fullEvent.shift}` : '—',
    loomState: running ? 'Running' : titleCaseStatus(interpreted.detail || category),
    lastSeen,
    softwareVersion: basicConfig.software_version || '',
    runtimeMinutes,
    runtimeSeconds: asNumber(firstDefined(result?.runtimeSeconds, runtime.runtimeSeconds), 0),
    speedHistory,
    runtimeRaw: runtime,
    backendRaw: result,
    event: {
      time: String(lastSeen).split(' ').pop() || nowInBeijing().split(' ')[1],
      level: online ? 'success' : 'error',
      message: online ? 'Read all status successful' : 'Read all status completed with errors',
      tag: online ? 'Info' : 'Error'
    }
  };
}

function convertBackendConfigToUi(config) {
  return {
    protocol: 'TCP',
    defaultHostPort: asNumber(config.host_port, DEFAULT_HOST_PORT),
    defaultLoomPort: DEFAULT_LOOM_PORT,
    backendHttpPort: asNumber(config.http_port, DEFAULT_HTTP_PORT),
    runtimeConfigPath: runtimeConfigPath(),
    backendSourceDir: backendSourceDir(),
    looms: (config.looms || []).map((loom, index) => ({
      id: `${loom.id || loom.ip || loom.ipAddress || index}`,
      enabled: loom.enabled !== false,
      name: loom.name || loom.ip || `loom-${index + 1}`,
      ipAddress: loom.ip || loom.ipAddress || '',
      hostPort: asNumber(loom.host_port ?? loom.hostPort ?? config.host_port, DEFAULT_HOST_PORT),
      loomPort: asNumber(loom.ts_port ?? loom.loomPort, DEFAULT_LOOM_PORT),
      supportsQt5FullStatus: loom.supports_qt5_full_status !== false,
      declarations: loom.declarations || {},
      status: 'Unknown',
      lastSeen: '-'
    }))
  };
}

function convertUiConfigToBackend(uiConfig) {
  const current = getRuntimeConfig();
  const uiLooms = uiConfig.looms || [];
  const firstHostPort = asNumber(
    uiConfig.defaultHostPort ?? uiLooms[0]?.hostPort,
    current.host_port || DEFAULT_HOST_PORT
  );
  return normalizeRuntimeConfig({
    ...current,
    host_port: firstHostPort,
    looms: uiLooms.map((loom) => ({
      id: loom.id || loom.ipAddress || loom.name,
      name: loom.name,
      ip: loom.ipAddress,
      host_port: asNumber(loom.hostPort ?? firstHostPort, firstHostPort),
      ts_port: asNumber(loom.loomPort, DEFAULT_LOOM_PORT),
      supports_qt5_full_status: loom.supportsQt5FullStatus !== false,
      declarations: loom.declarations || {
        '565': 'Employee ID: _____ Production Plan ID: _____ Yarn Package Number: _____'
      },
      enabled: loom.enabled !== false
    }))
  });
}

function mapBackendEvent(row) {
  const ts = row.ts_iso || nowInBeijing();
  const payload = row.payload || {};
  const eventType = row.event_type || 'event';
  let message = titleCaseStatus(eventType);
  if (payload.interpreted?.category) message += `: ${titleCaseStatus(payload.interpreted.category)}`;
  if (payload.error) message = payload.error;
  return {
    time: String(ts).replace('T', ' ').replace('Z', '').split(' ').pop() || '',
    level: payload.error ? 'error' : 'info',
    message,
    tag: payload.error ? 'Error' : 'Info'
  };
}

function createWindow() {
  mainWindow = new BrowserWindow({
    width: 1672,
    height: 941,
    minWidth: 1280,
    minHeight: 760,
    title: 'Loom Real-Time Monitoring Panel',
    backgroundColor: '#f6f8fb',
    show: true,
    autoHideMenuBar: true,
    webPreferences: {
      preload: path.join(__dirname, 'preload.js'),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: false
    }
  });

const rendererIndex = path.join(__dirname, '..', 'dist', 'index.html');

if (app.isPackaged) {
  mainWindow.loadFile(rendererIndex);
} else {
  mainWindow.loadURL('http://127.0.0.1:5173');
}


mainWindow.webContents.on('did-fail-load', (_event, errorCode, errorDescription, validatedURL) => {
  console.error('Renderer failed to load:', errorCode, errorDescription, validatedURL);
});
}

app.whenReady().then(() => {
  app.setName('Loom Real-Time Monitoring Panel');
  ensureRuntimeConfig();
  startBackend().catch((error) => {
    backendLastError = error.message;
    appendBackendLog(error.stack || error.message);
  });

  ipcMain.handle('backend:status', async () => {
    const healthy = await backendHealth();
    return {
      healthy,
      starting: Boolean(backendStarting),
      port: getHttpPort(),
      runtimeConfigPath: runtimeConfigPath(),
      backendSourceDir: backendSourceDir(),
      pid: backendProcess?.pid || null,
      lastError: backendLastError,
      logTail: backendLogLines.slice(-80)
    };
  });

  ipcMain.handle('backend:start', async () => {
    await startBackend();
    return { ok: true };
  });

  ipcMain.handle('config:load', async () => {
    const config = getRuntimeConfig();
    const ui = convertBackendConfigToUi(config);
    try {
      const snapshot = await getJson('/api/looms');
      ui.looms = ui.looms.map((loom) => {
        const item = snapshot?.[loom.ipAddress];
        return item ? {
          ...loom,
          name: item.name || loom.name,
          status: item.tcp_connected || item.last_status ? 'Online' : loom.status,
          lastSeen: item.last_status?.updated_at_iso || item.last_ts_poll_ts || loom.lastSeen
        } : loom;
      });
    } catch {
      // The UI can still load before the backend is ready.
    }
    return ui;
  });

  ipcMain.handle('config:save', async (_event, uiConfig) => {
    const nextBackendConfig = convertUiConfigToBackend(uiConfig);
    fs.writeFileSync(runtimeConfigPath(), JSON.stringify(nextBackendConfig, null, 2), 'utf-8');
    let backendRestarted = false;
    let restartError = '';
    try {
      backendRestarted = await restartOwnedBackendAfterConfigSave();
    } catch (error) {
      restartError = error.message;
      backendLastError = error.message;
      appendBackendLog(`Backend restart after config save failed: ${error.message}`);
    }
    return {
      ...convertBackendConfigToUi(nextBackendConfig),
      backendRestarted,
      restartError,
      needsBackendRestart: !backendRestarted
    };
  });

  ipcMain.handle('loom:readAllStatus', async (_event, loom) => {
    const loomIp = typeof loom === 'string' ? loom : (loom?.ipAddress || loom?.ip || '');
    if (!loomIp) throw new Error('No loom IP address selected. Please check IP Management.');
    const payload = await postBackend('/action/read-all', { loom_ip: loomIp });
    const normalized = normalizeReadAllResult(payload.result, typeof loom === 'object' ? loom : { name: loomIp, ipAddress: loomIp });
    try {
      const events = await getJson('/api/events?limit=12');
      normalized.events = Array.isArray(events) ? events.map(mapBackendEvent) : [];
    } catch {
      normalized.events = [];
    }
    return normalized;
  });

  ipcMain.handle('loom:sendDeclaration', async (_event, payload) => {
    const loomIp = payload?.loomIp || payload?.ipAddress || payload?.loom?.ipAddress || payload?.loom?.ip || '';
    if (!loomIp) throw new Error('No loom IP address selected. Please check IP Management.');
    const receivedAt = nowInBeijing();
    const template = `Employee ID: ${payload.employeeId || ''} Production Plan ID: ${payload.productionPlanId || ''} Yarn Package Number: ${payload.yarnPackageNumber || ''}`;
    const result = await postBackend('/action/admin-declaration', {
      loom_ip: loomIp,
      code_str: payload.codeStr || '565',
      template
    }, { writeAuth: true });
    return {
      ok: true,
      receivedAt,
      reply: '',
      backendResult: result.result,
      event: {
        time: receivedAt.split(' ')[1],
        level: 'success',
        message: 'Administrative declaration configured; waiting for loom-side reply',
        tag: 'Info'
      }
    };
  });

  ipcMain.handle('loom:pollRealtimeStatus', async (_event, loom) => {
    const loomIp = typeof loom === 'string' ? loom : (loom?.ipAddress || loom?.ip || '');
    if (!loomIp) throw new Error('No loom IP address selected. Please check IP Management.');
    const payload = await postBackend('/action/realtime-status', { loom_ip: loomIp });
    const result = payload.result || {};
    const runtime = result.runtime || {};
    const speedHistory = normalizeSpeedHistory(firstDefined(result.speedHistory, runtime.speedHistoryValues, runtime.speedHistory));
    const speedResult = result.speed || {};
    return {
      ok: true,
      currentSpeed: normalizeSpeedRpm(firstDefined(result.currentSpeed, speedResult.speed_rpm, runtime.lastSpeedRpm, '—')),
      running: runtime.running,
      runtimeMinutes: asNumber(firstDefined(result.runtimeMinutes, runtime.runtimeMinutes, speedHistory.length), speedHistory.length || 1),
      runtimeSeconds: asNumber(firstDefined(result.runtimeSeconds, runtime.runtimeSeconds), 0),
      speedHistory,
      lastSeen: result.updated_at_iso || runtime.lastUpdateIso || nowInBeijing(),
      adminCompletion: normalizeAdminCompletion(result.adminCompletion || result.admin_completion || result.latestDeclaration)
    };
  });

  ipcMain.handle('loom:remoteStop', async (_event, payload) => {
    const loomIp = payload?.loomIp || payload?.ipAddress || payload?.loom?.ipAddress || payload?.loom?.ip || '';
    if (!loomIp) throw new Error('No loom IP address selected. Please check IP Management.');
    const timestamp = nowInBeijing();
    const result = await postBackend('/action/remote-stop', { loom_ip: loomIp }, { writeAuth: true });
    return {
      ok: true,
      time: timestamp,
      message: result?.result?.message || 'Remote stop command sent',
      backendResult: result?.result || null
    };
  });

  createWindow();

  app.on('activate', () => {
    if (BrowserWindow.getAllWindows().length === 0) createWindow();
  });
});

app.on('before-quit', () => {
  if (backendProcess) {
    try { backendProcess.kill(); } catch {}
    backendProcess = null;
  }
});

app.on('window-all-closed', () => {
  if (process.platform !== 'darwin') app.quit();
});
