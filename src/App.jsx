import { useEffect, useMemo, useRef, useState } from 'react';

const assetUrl = (fileName) => `${import.meta.env.BASE_URL}${fileName}`;

const fallbackApi = {
  loadConfig: async () => ({
    protocol: 'TCP',
    defaultHostPort: 13001,
    defaultLoomPort: 13000,
    looms: [
      {
        id: 'loom-001',
        enabled: true,
        name: '2fast-loom-01',
        ipAddress: '192.168.1.101',
        hostPort: 13001,
        loomPort: 13000,
        status: 'Online',
        lastSeen: '2025-05-21 15:24:21'
      },
      {
        id: 'loom-002',
        enabled: true,
        name: '2fast-loom-02',
        ipAddress: '192.168.1.102',
        hostPort: 13003,
        loomPort: 13002,
        status: 'Online',
        lastSeen: '2025-05-21 15:23:58'
      }
    ]
  }),
  saveConfig: async (config) => config,
  readAllStatus: async (loom) => {
    const now = beijingDateTime();
    const loomName = typeof loom === 'string' ? loom : (loom?.name || '2fast-loom-01');
    const runtimeMinutes = 60;
    const speedHistory = createInitialSpeedSeries(runtimeMinutes).map((value, index, arr) => {
      if (index === arr.length - 1) return Math.round(520 + Math.random() * 40);
      return value;
    });
    return {
      loomName,
      status: 'Online',
      hostPort: loom?.hostPort ?? 13001,
      loomPort: loom?.loomPort ?? 13000,
      protocol: 'TCP',
      currentSpeed: speedHistory[speedHistory.length - 1],
      picks: 12345678 + Math.floor(Math.random() * 1000),
      density: 28.5,
      densityUnit: 'weft/dm',
      currentPattern: 'PAT-04567',
      shift: 'Day Shift',
      loomState: 'Running',
      runtimeMinutes,
      speedHistory,
      lastSeen: now,
      event: {
        time: now.split(' ')[1],
        level: 'success',
        message: 'Read all status successful',
        tag: 'Info'
      }
    };
  },
  sendDeclaration: async () => {
    const receivedAt = beijingDateTime();
    return {
      ok: true,
      receivedAt,
      reply: '',
      event: {
        time: receivedAt.split(' ')[1],
        level: 'success',
        message: 'Administrative declaration configured; waiting for loom reply',
        tag: 'Info'
      }
    };
  },
  pollRealtimeStatus: async () => {
    const now = beijingDateTime();
    const speed = Math.round(520 + Math.random() * 40);
    return {
      ok: true,
      currentSpeed: speed,
      runtimeMinutes: 60,
      runtimeSeconds: 3600,
      speedHistory: createInitialSpeedSeries(60).map((value, index, arr) => index === arr.length - 1 ? speed : value),
      lastSeen: now,
      adminCompletion: null
    };
  },
  remoteStop: async () => {
    const time = beijingDateTime();
    return { ok: true, time, message: 'Remote stop command sent' };
  },
  getBackendStatus: async () => ({ healthy: false, starting: false, port: 18080, lastError: 'Running in browser fallback mode' }),
  startBackend: async () => ({ ok: false })
};

const api = window.loomHost ?? fallbackApi;

const navItems = [
  { key: 'dashboard', label: 'Dashboard', icon: '▦' },
  { key: 'status', label: 'Loom Status', icon: '▧' },
  { key: 'declaration', label: 'Administrative Declaration', icon: '▤' },
  { key: 'ip', label: 'IP Management', icon: '⌘' },
  { key: 'history', label: 'History', icon: '↺' },
  { key: 'settings', label: 'Settings', icon: '⚙' }
];

function beijingDateTime() {
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

function normalizeSpeedRpm(value) {
  const numeric = Number(value);
  if (!Number.isFinite(numeric)) return value;
  let rpm = numeric;
  const absRpm = Math.abs(rpm);
  if (absRpm > 10000) {
    rpm = rpm / 100;
  } else if (absRpm > 2500) {
    const candidate = rpm / 100;
    if (Math.abs(candidate) <= 2500) rpm = candidate;
  }
  return Math.round(rpm * 10) / 10;
}

function formatSpeed(value) {
  const rpm = normalizeSpeedRpm(value);
  const numeric = Number(rpm);
  if (!Number.isFinite(numeric)) return String(value ?? '—');
  return Number.isInteger(numeric) ? String(numeric) : numeric.toFixed(1);
}

function formatNumber(value) {
  const numeric = Number(value);
  if (!Number.isFinite(numeric)) return String(value ?? '—');
  return numeric.toLocaleString('en-US');
}

function firstDefined(...values) {
  for (const value of values) {
    if (value !== undefined && value !== null && value !== '') return value;
  }
  return '';
}

function parseDeclarationReply(replyText, fallback = {}) {
  const result = {
    employeeId: fallback.employeeId ?? '',
    productionPlanId: fallback.productionPlanId ?? '',
    yarnPackageNumber: fallback.yarnPackageNumber ?? ''
  };

  if (!replyText || typeof replyText !== 'string') return result;

  const text = replyText.trim();
  const labelledPatterns = [
    ['employeeId', /Employee\s*ID\s*[:=]\s*([^|,;\n]+)/i],
    ['productionPlanId', /Production\s*Plan\s*ID\s*[:=]\s*([^|,;\n]+)/i],
    ['yarnPackageNumber', /Yarn\s*Package\s*Number\s*[:=]\s*([^|,;\n]+)/i]
  ];
  for (const [field, pattern] of labelledPatterns) {
    const match = text.match(pattern);
    if (match?.[1]) result[field] = match[1].trim();
  }

  const timeIndex = text.indexOf('|TIME=');
  const payload = timeIndex >= 0 ? text.slice(0, timeIndex) : text;
  const parts = payload.split('|').map((part) => part.trim()).filter(Boolean);

  if (parts.length >= 3) {
    const dataParts = parts.filter((part) => !/^ACK$/i.test(part) && !/^OK$/i.test(part) && !/^DECL$/i.test(part) && !/^CONFIGURED$/i.test(part) && !/^DEC[LC]?$/i.test(part) && !/^565$/.test(part));
    const lastThree = dataParts.slice(-3);
    if (lastThree[0] && !result.employeeId) result.employeeId = lastThree[0];
    if (lastThree[1] && !result.productionPlanId) result.productionPlanId = lastThree[1];
    if (lastThree[2] && !result.yarnPackageNumber) result.yarnPackageNumber = lastThree[2];
  }

  return result;
}

function normalizeDeclarationCompletion(completion, fallback = {}) {
  if (!completion || completion.hasData === false) return null;
  const fields = completion.fields || completion.adminFields || completion.parsedFields || {};
  const rawText = firstDefined(completion.completedText, completion.completed_text, completion.reply, completion.text, completion.rawText, '');
  const parsed = parseDeclarationReply(rawText, fallback);
  const receivedAt = firstDefined(completion.receivedAt, completion.ts_iso, completion.tsIso, completion.updated_at_iso, completion.updatedAt, beijingDateTime());
  const normalized = {
    receivedAt,
    employeeId: firstDefined(completion.employeeId, completion.employee_id, fields.employeeId, fields.employee_id, parsed.employeeId),
    productionPlanId: firstDefined(completion.productionPlanId, completion.production_plan_id, fields.productionPlanId, fields.production_plan_id, parsed.productionPlanId),
    yarnPackageNumber: firstDefined(completion.yarnPackageNumber, completion.yarn_package_number, fields.yarnPackageNumber, fields.yarn_package_number, parsed.yarnPackageNumber)
  };
  if (!normalized.employeeId && !normalized.productionPlanId && !normalized.yarnPackageNumber && !rawText) return null;
  return normalized;
}

function createInitialSpeedSeries(length = 60) {
  return Array.from({ length }, (_, index) => {
    const base = 505 + Math.sin(index / 5) * 14;
    const noise = Math.sin(index * 1.7) * 8 + Math.cos(index * 0.43) * 5;
    return Math.round(base + noise);
  });
}

function createMinuteTickValues(totalMinutes) {
  const safeTotal = Math.max(1, Math.round(Number(totalMinutes) || 1));
  if (safeTotal <= 10) {
    return Array.from({ length: safeTotal }, (_, index) => index + 1);
  }
  const maxLabels = 7;
  const step = Math.max(1, Math.ceil(safeTotal / (maxLabels - 1)));
  const ticks = [1];
  for (let value = step; value < safeTotal; value += step) {
    ticks.push(value);
  }
  if (ticks[ticks.length - 1] !== safeTotal) ticks.push(safeTotal);
  return [...new Set(ticks)];
}

function getRuntimeMinutes(result, fallbackMinutes) {
  const candidates = [
    result?.runtimeMinutes,
    result?.runMinutes,
    result?.runningMinutes,
    result?.loomRunMinutes,
    result?.runTimeMinutes,
    result?.durationMinutes,
    Array.isArray(result?.speedHistory) ? result.speedHistory.length : null
  ];
  for (const value of candidates) {
    const numeric = Math.round(Number(value));
    if (Number.isFinite(numeric) && numeric > 0) return numeric;
  }
  return Math.max(1, Math.round(Number(fallbackMinutes) || 1));
}

function getSpeedSeriesFromResult(result, previousSeries) {
  const backendSeries = Array.isArray(result?.speedHistory)
    ? result.speedHistory
        .map((value) => normalizeSpeedRpm(value && typeof value === 'object' ? (value.speed_rpm ?? value.speedRpm ?? value.value ?? value.rpm) : value))
        .map((value) => Number(value))
        .filter((value) => Number.isFinite(value))
    : [];

  const currentSpeed = Number(normalizeSpeedRpm(result?.currentSpeed));
  const hasCurrentSpeed = Number.isFinite(currentSpeed);

  if (backendSeries.length) {
    if (hasCurrentSpeed && backendSeries[backendSeries.length - 1] !== currentSpeed) {
      return [...backendSeries, currentSpeed];
    }
    return backendSeries;
  }

  if (!hasCurrentSpeed) return previousSeries;

  const runtimeSeconds = Number(result?.runtimeSeconds);
  const maxPoints = Number.isFinite(runtimeSeconds) && runtimeSeconds > 0
    ? Math.max(1, Math.min(Math.ceil(runtimeSeconds), 7200))
    : Math.max(1, getRuntimeMinutes(result, previousSeries.length + 1) * 60);
  return [...previousSeries, currentSpeed].slice(-maxPoints);
}

function ClockLabel() {
  const [time, setTime] = useState(beijingDateTime());

  useEffect(() => {
    const timer = setInterval(() => setTime(beijingDateTime()), 1000);
    return () => clearInterval(timer);
  }, []);

  return (
    <div className="clock-label" title="China Standard Time">
      <span className="clock-icon">◷</span>
      <span>Beijing Time</span>
      <strong>{time}</strong>
    </div>
  );
}

function Sidebar({ active, onChange }) {
  return (
    <aside className="sidebar" aria-label="Main navigation">
      <div className="brand" aria-label="Santex Rimar Group and Smit logo">
        <img src={assetUrl('sidebar_brand_logo.png')} alt="Santex Rimar Group and Smit" />
      </div>
      <nav className="nav-list">
        {navItems.map((item) => (
          <button
            className={`nav-item ${active === item.key ? 'active' : ''}`}
            key={item.key}
            type="button"
            onClick={() => onChange(item.key)}
          >
            <span className="nav-icon">{item.icon}</span>
            <span>{item.label}</span>
          </button>
        ))}
      </nav>
      <div className="system-card">
        <p>Version 1.2.8</p>
        <p>© 2026 Smit SHA Textile Machinery</p>
        <div className="system-normal">
          <span className="dot green" />
          System Normal
        </div>
      </div>
    </aside>
  );
}

function Header({ backendStatus }) {
  const backendClass = backendStatus?.healthy ? 'backend-ok' : (backendStatus?.starting ? 'backend-starting' : 'backend-error');
  const backendText = backendStatus?.healthy ? `Backend Connected :${backendStatus.port}` : (backendStatus?.starting ? 'Backend Starting' : 'Backend Offline');
  return (
    <header className="topbar">
      <h1>Loom Real-Time Monitoring Panel</h1>
      <div className="topbar-right">
        <ClockLabel />
        <span className="divider" />
        <span className={`backend-badge ${backendClass}`} title={backendStatus?.lastError || ''}>{backendText}</span>
        <span className="qt-badge">QT5 Displays Only</span>
      </div>
    </header>
  );
}

function LoomOverview({ selectedLoom, status, onReadAll, busy }) {
  return (
    <section className="overview-card">
      <div className="loom-icon image-icon" aria-hidden="true">
        <img src={assetUrl('2FAST_100px.png')} alt="2FAST loom" />
      </div>
      <div className="overview-main">
        <span className="section-kicker">Loom Overview</span>
        <div className="overview-title-row">
          <h2>{selectedLoom?.name ?? status.loomName}</h2>
          <span className="online-chip"><span className="dot green" />Online</span>
        </div>
        <div className="overview-meta">
          <span>Host Port: {selectedLoom?.hostPort ?? status.hostPort}</span>
          <span>Loom Port: {selectedLoom?.loomPort ?? status.loomPort}</span>
          <span>Protocol: {status.protocol}</span>
        </div>
      </div>
      <button className="primary-button" onClick={onReadAll} disabled={busy}>
        <span className={busy ? 'spin' : ''}>↻</span>
        {busy ? 'Reading...' : 'Read All Status'}
      </button>
    </section>
  );
}

function MetricCard({ label, value, unit, tone, iconSrc, iconAlt = '' }) {
  return (
    <article className="metric-card">
      <div className={`metric-icon ${tone}`}>{iconSrc ? <img src={iconSrc} alt={iconAlt} /> : null}</div>
      <div className="metric-copy">
        <p>{label}</p>
        <strong className={`metric-value ${tone}`}>{value} {unit && <small>{unit}</small>}</strong>
      </div>
    </article>
  );
}

function RemoteStopCard({ onRemoteStop, busy }) {
  return (
    <button type="button" className="metric-card metric-action-card" onClick={onRemoteStop} disabled={busy}>
      <div className="metric-icon remote-stop"><img src={assetUrl('icon_remote_stop.png')} alt="Remote Stop" /></div>
      <div className="metric-copy">
        <p>Remote Stop</p>
        <strong className="metric-value remote">{busy ? 'Stopping...' : 'Send Stop'}</strong>
      </div>
    </button>
  );
}

function MetricGrid({ status, onRemoteStop, remoteStopBusy }) {
  return (
    <section className="metrics-grid">
      <MetricCard label="Current Speed" value={formatSpeed(status.currentSpeed)} unit="rpm" tone="blue" iconSrc={assetUrl('icon_speed.png')} iconAlt="Current Speed" />
      <MetricCard label="Picks" value={formatNumber(status.picks)} tone="teal" iconSrc={assetUrl('icon_picks.png')} iconAlt="Picks" />
      <MetricCard label="Density" value={status.density} unit={status.densityUnit ?? 'weft/dm'} tone="purple" iconSrc={assetUrl('icon_density.png')} iconAlt="Density" />
      <MetricCard label="Current Pattern" value={status.currentPattern} tone="blue" iconSrc={assetUrl('icon_pattern.png')} iconAlt="Current Pattern" />
      <MetricCard label="Shift" value={status.shift} tone="orange" iconSrc={assetUrl('icon_shift.png')} iconAlt="Shift" />
      <MetricCard label="Loom State" value={status.loomState} tone="green" iconSrc={assetUrl('icon_loom_state.png')} iconAlt="Loom State" />
      <RemoteStopCard onRemoteStop={onRemoteStop} busy={remoteStopBusy} />
    </section>
  );
}

function SpeedChart({ points, runtimeMinutes }) {
  const safePoints = Array.isArray(points) && points.length
    ? points.map((point) => Number(normalizeSpeedRpm(point))).filter((point) => Number.isFinite(point))
    : [0];
  const totalMinutes = Math.max(1, Math.round(Number(runtimeMinutes) || Math.ceil(safePoints.length / 60) || 1));
  const width = 820;
  const height = 230;
  const padding = { left: 50, right: 20, top: 20, bottom: 38 };
  const plotWidth = width - padding.left - padding.right;
  const plotHeight = height - padding.top - padding.bottom;
  const minY = 0;
  const maxPoint = Math.max(0, ...safePoints);
  const maxY = Math.max(1000, Math.ceil((maxPoint * 1.15) / 200) * 200);
  const pointDivisor = Math.max(safePoints.length - 1, 1);
  const path = safePoints
    .map((point, index) => {
      const x = padding.left + (index / pointDivisor) * plotWidth;
      const y = padding.top + ((maxY - point) / (maxY - minY)) * plotHeight;
      return `${index === 0 ? 'M' : 'L'} ${x.toFixed(2)} ${y.toFixed(2)}`;
    })
    .join(' ');

  const yStep = Math.max(100, Math.ceil(maxY / 5 / 100) * 100);
  const yTicks = Array.from({ length: 6 }, (_, index) => index * yStep).filter((tick) => tick <= maxY);
  if (yTicks[yTicks.length - 1] !== maxY) yTicks.push(maxY);
  const xTicks = createMinuteTickValues(totalMinutes);

  return (
    <section className="panel chart-panel">
      <div className="panel-header">
        <div>
          <h3>Loom Speed Trend (rpm)</h3>
        </div>
      </div>
      <svg className="speed-chart" viewBox={`0 0 ${width} ${height}`} preserveAspectRatio="none">
        {yTicks.map((tick) => {
          const y = padding.top + ((maxY - tick) / (maxY - minY)) * plotHeight;
          return (
            <g key={tick}>
              <line x1={padding.left} x2={width - padding.right} y1={y} y2={y} className="grid-line" />
              <text x={padding.left - 12} y={y + 4} textAnchor="end" className="chart-label">{tick}</text>
            </g>
          );
        })}
        {xTicks.map((minuteValue) => {
          const denominator = Math.max(totalMinutes - 1, 1);
          const x = padding.left + ((minuteValue - 1) / denominator) * plotWidth;
          return (
            <g key={minuteValue}>
              <line x1={x} x2={x} y1={padding.top} y2={height - padding.bottom} className="grid-line vertical" />
              <text x={x} y={height - 12} textAnchor="middle" className="chart-label">{minuteValue} min</text>
            </g>
          );
        })}
        <path d={path} className="speed-line" />
        {safePoints.length === 1 && (() => {
          const point = safePoints[0];
          const x = padding.left;
          const y = padding.top + ((maxY - point) / (maxY - minY)) * plotHeight;
          return <circle cx={x} cy={y} r="4" className="speed-point" />;
        })()}
      </svg>
      <div className="chart-legend"><span className="legend-line" />Speed (rpm)</div>
    </section>
  );
}

function SystemEvents({ events, onViewAll, expanded = false }) {
  const safeEvents = (events || []).filter(Boolean);
  return (
    <section className={`panel events-panel ${expanded ? 'expanded' : ''}`}>
      <div className="panel-header">
        <h3>System Events</h3>
        {onViewAll && <button type="button" className="link-button" onClick={onViewAll}>View All</button>}
      </div>
      <div className="event-list">
        {safeEvents.length === 0 && <div className="empty-state">No system events yet.</div>}
        {safeEvents.map((event, index) => {
          const level = event.level || 'info';
          const tag = event.tag || (level === 'error' ? 'Error' : 'Info');
          return (
            <div className="event-row" key={`${event.time || 'event'}-${index}`}>
              <span className="event-time">{event.time || '—'}</span>
              <span className={`event-icon ${level}`}>{level === 'success' ? '✓' : level === 'error' ? '!' : 'i'}</span>
              <span className="event-message">{event.message || 'Event received'}</span>
              <span className={`event-tag ${String(tag).toLowerCase()}`}>{tag}</span>
            </div>
          );
        })}
      </div>
    </section>
  );
}

function DeclarationPanel({ onSend, reply }) {
  const [form, setForm] = useState({
    employeeId: 'E10045',
    productionPlanId: 'PLAN-250521-001',
    yarnPackageNumber: 'YP-20250521-0786'
  });
  const [busy, setBusy] = useState(false);

  const update = (field, value) => setForm((prev) => ({ ...prev, [field]: value }));

  async function submit() {
    setBusy(true);
    try {
      await onSend(form);
    } finally {
      setBusy(false);
    }
  }

  return (
    <section className="panel declaration-panel">
      <h3 className="blue-title">Administrative Declaration</h3>
      <div className="declaration-row">
        <label>
          <span>Employee ID</span>
          <div className="input-shell"><span>♙</span><input value={form.employeeId} onChange={(e) => update('employeeId', e.target.value)} /></div>
        </label>
        <label>
          <span>Production Plan ID</span>
          <div className="input-shell"><span>▤</span><input value={form.productionPlanId} onChange={(e) => update('productionPlanId', e.target.value)} /></div>
        </label>
        <label>
          <span>Yarn Package Number</span>
          <div className="input-shell"><span>⬡</span><input value={form.yarnPackageNumber} onChange={(e) => update('yarnPackageNumber', e.target.value)} /></div>
        </label>
        <button className="primary-button declaration-button" onClick={submit} disabled={busy}>{busy ? 'Sending...' : 'Send Declaration'}</button>
      </div>
      <div className="reply-block">
        <div className="reply-block-header">
          <strong>Loom Reply</strong>
          <span className="reply-time">Received: {reply.receivedAt}</span>
        </div>
        <div className="reply-grid aligned-reply-grid">
          <div className="reply-field">
            <span>Employee ID</span>
            <div className="reply-box">{reply.employeeId || '—'}</div>
          </div>
          <div className="reply-field">
            <span>Production Plan ID</span>
            <div className="reply-box">{reply.productionPlanId || '—'}</div>
          </div>
          <div className="reply-field">
            <span>Yarn Package Number</span>
            <div className="reply-box">{reply.yarnPackageNumber || '—'}</div>
          </div>
          <div className="reply-side-cell" />
        </div>
      </div>
    </section>
  );
}

function Toggle({ checked, onChange }) {
  return (
    <button className={`toggle ${checked ? 'on' : ''}`} onClick={() => onChange(!checked)} aria-pressed={checked}>
      <span />
    </button>
  );
}

function IpManagement({ config, selectedId, onSelect, onConfigChange }) {
  const looms = config?.looms ?? [];
  const [editingId, setEditingId] = useState(null);
  const [draft, setDraft] = useState({ name: '', ipAddress: '', hostPort: '', loomPort: '' });
  const [saveError, setSaveError] = useState('');
  const [saving, setSaving] = useState(false);

  function startEdit(loom) {
    setSaveError('');
    setEditingId(loom.id);
    setDraft({
      name: loom.name || '',
      ipAddress: loom.ipAddress || '',
      hostPort: String(loom.hostPort ?? config?.defaultHostPort ?? 13001),
      loomPort: String(loom.loomPort ?? config?.defaultLoomPort ?? 13000)
    });
  }

  function cancelEdit() {
    setSaveError('');
    setEditingId(null);
  }

  function updateDraft(field, value) {
    setDraft((prev) => ({ ...prev, [field]: value }));
  }

  function parsePort(value, fieldName) {
    const numeric = Number(value);
    if (!Number.isInteger(numeric) || numeric < 1 || numeric > 65535) {
      throw new Error(`${fieldName} must be an integer from 1 to 65535.`);
    }
    return numeric;
  }

  function validateDraft() {
    const name = draft.name.trim();
    const ipAddress = draft.ipAddress.trim();
    if (!name) throw new Error('Loom Name cannot be empty.');
    if (!ipAddress) throw new Error('IP Address cannot be empty.');

    const ipv4Pattern = /^(25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)(\.(25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)){3}$/;
    if (!ipv4Pattern.test(ipAddress)) {
      throw new Error('IP Address must be a valid IPv4 address, for example 192.168.1.101.');
    }

    const hostPort = parsePort(draft.hostPort, 'Host Port');
    const loomPort = parsePort(draft.loomPort, 'Loom Port');
    return { name, ipAddress, hostPort, loomPort };
  }

  async function saveEditedLoom(id) {
    setSaving(true);
    setSaveError('');
    try {
      const patch = validateDraft();
      const next = {
        ...config,
        defaultHostPort: patch.hostPort,
        looms: looms.map((loom) => (loom.id === id ? { ...loom, ...patch } : loom))
      };
      const saved = await api.saveConfig(next);
      onConfigChange(saved);
      setEditingId(null);
    } catch (error) {
      setSaveError(error.message);
    } finally {
      setSaving(false);
    }
  }

  async function updateLoom(id, patch) {
    const next = {
      ...config,
      defaultHostPort: patch.hostPort ?? config?.defaultHostPort,
      looms: looms.map((loom) => (loom.id === id ? { ...loom, ...patch } : loom))
    };
    onConfigChange(await api.saveConfig(next));
  }

  async function addLoom() {
    const index = looms.length + 1;
    const nextLoom = {
      id: `loom-${Date.now()}`,
      enabled: false,
      name: `2fast-loom-${String(index).padStart(2, '0')}`,
      ipAddress: `192.168.1.${100 + index}`,
      hostPort: config?.defaultHostPort ?? 13001,
      loomPort: config?.defaultLoomPort ?? 13000,
      status: 'Unknown',
      lastSeen: '-'
    };
    const next = { ...config, looms: [...looms, nextLoom] };
    const saved = await api.saveConfig(next);
    onConfigChange(saved);
    onSelect(nextLoom.id);
    startEdit(nextLoom);
  }

  async function deleteLoom(id) {
    const next = { ...config, looms: looms.filter((loom) => loom.id !== id) };
    const saved = await api.saveConfig(next);
    onConfigChange(saved);
    if (selectedId === id && saved?.looms?.[0]?.id) onSelect(saved.looms[0].id);
  }

  return (
    <section className="panel ip-panel">
      <div className="panel-header">
        <div>
          <h3 className="blue-title">IP Management</h3>
          <p className="panel-note">Click the pencil icon to edit Loom Name, IP Address, Host Port, and Loom Port. Press ✓ Save to write the runtime config.</p>
        </div>
        <button className="outline-button" onClick={addLoom}>＋ Add Loom</button>
      </div>
      {saveError && <div className="inline-error">{saveError}</div>}
      <div className="table-wrap">
        <table>
          <thead>
            <tr>
              <th>Enabled</th>
              <th>Loom Name</th>
              <th>IP Address</th>
              <th>Host Port</th>
              <th>Loom Port</th>
              <th>Status</th>
              <th>Last Seen</th>
              <th>Actions</th>
            </tr>
          </thead>
          <tbody>
            {looms.map((loom) => {
              const isEditing = editingId === loom.id;
              const statusText = loom.status || 'Unknown';
              return (
                <tr key={loom.id} className={selectedId === loom.id ? 'selected-row' : ''} onClick={() => onSelect(loom.id)}>
                  <td><Toggle checked={loom.enabled} onChange={(checked) => updateLoom(loom.id, { enabled: checked })} /></td>
                  <td>{isEditing ? <input className="table-input name-input" value={draft.name} onChange={(e) => updateDraft('name', e.target.value)} onClick={(e) => e.stopPropagation()} /> : loom.name}</td>
                  <td>{isEditing ? <input className="table-input ip-input" value={draft.ipAddress} onChange={(e) => updateDraft('ipAddress', e.target.value)} onClick={(e) => e.stopPropagation()} /> : loom.ipAddress}</td>
                  <td>{isEditing ? <input className="table-input port-input" value={draft.hostPort} onChange={(e) => updateDraft('hostPort', e.target.value)} onClick={(e) => e.stopPropagation()} /> : loom.hostPort}</td>
                  <td>{isEditing ? <input className="table-input port-input" value={draft.loomPort} onChange={(e) => updateDraft('loomPort', e.target.value)} onClick={(e) => e.stopPropagation()} /> : loom.loomPort}</td>
                  <td><span className={`status-pill ${statusText.toLowerCase()}`}><span className="dot" />{statusText}</span></td>
                  <td>{loom.lastSeen}</td>
                  <td className="actions">
                    {isEditing ? (
                      <>
                        <button className="save-action" disabled={saving} onClick={(e) => { e.stopPropagation(); saveEditedLoom(loom.id); }} title="Save IP settings">✓</button>
                        <button onClick={(e) => { e.stopPropagation(); cancelEdit(); }} title="Cancel editing">↩</button>
                      </>
                    ) : (
                      <>
                        <button onClick={(e) => { e.stopPropagation(); startEdit(loom); }} title="Edit loom connection settings">✎</button>
                        <button className="danger" onClick={(e) => { e.stopPropagation(); deleteLoom(loom.id); }} title="Delete loom">🗑</button>
                      </>
                    )}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </section>
  );
}

function ModuleHeader({ title, description }) {
  return (
    <section className="module-header">
      <div>
        <span className="section-kicker">Current Module</span>
        <h2>{title}</h2>
        {description && <p>{description}</p>}
      </div>
    </section>
  );
}

function SettingsPanel({ backendStatus, config, selectedLoom }) {
  const logTail = backendStatus?.logTail || [];
  return (
    <section className="panel settings-panel">
      <div className="panel-header">
        <h3 className="blue-title">System Settings</h3>
      </div>
      <div className="settings-grid">
        <div className="settings-card">
          <span>Backend Status</span>
          <strong>{backendStatus?.healthy ? 'Connected' : backendStatus?.starting ? 'Starting' : 'Offline'}</strong>
          <p>HTTP API Port: {backendStatus?.port ?? config?.backendHttpPort ?? 18080}</p>
        </div>
        <div className="settings-card">
          <span>Selected Loom</span>
          <strong>{selectedLoom?.name || 'No loom selected'}</strong>
          <p>{selectedLoom?.ipAddress || 'IP address not configured'} · TS Port {selectedLoom?.loomPort || '—'}</p>
        </div>
        <div className="settings-card">
          <span>Runtime Config</span>
          <strong>{config?.runtimeConfigPath ? 'Loaded' : 'Not loaded'}</strong>
          <p>{config?.runtimeConfigPath || 'Config path unavailable'}</p>
        </div>
      </div>
      {backendStatus?.lastError && (
        <div className="backend-warning">
          <strong>Last Backend Message:</strong> {backendStatus.lastError}
        </div>
      )}
      <div className="backend-log">
        <h4>Backend Log Tail</h4>
        <pre>{logTail.length ? logTail.slice(-20).join('\n') : 'No backend log lines received yet.'}</pre>
      </div>
    </section>
  );
}

function Dashboard() {
  const [active, setActive] = useState('dashboard');
  const [config, setConfig] = useState(null);
  const [selectedId, setSelectedId] = useState('loom-001');
  const [busy, setBusy] = useState(false);
  const [remoteStopBusy, setRemoteStopBusy] = useState(false);
  const [backendStatus, setBackendStatus] = useState({ healthy: false, starting: true, port: 18080, lastError: '' });
  const [speedSeries, setSpeedSeries] = useState(createInitialSpeedSeries);
  const [status, setStatus] = useState({
    loomName: '2fast-loom-01',
    status: 'Online',
    hostPort: 13001,
    loomPort: 13000,
    protocol: 'TCP',
    currentSpeed: 540,
    picks: 12345678,
    density: 28.5,
    densityUnit: 'weft/dm',
    currentPattern: 'PAT-04567',
    shift: 'Day Shift',
    loomState: 'Running',
    runtimeMinutes: 60,
    lastSeen: '2025-05-21 15:24:21'
  });
  const [reply, setReply] = useState({
    receivedAt: '2025-05-21 15:23:47',
    employeeId: 'E10045',
    productionPlanId: 'PLAN-250521-001',
    yarnPackageNumber: 'YP-20250521-0786'
  });
  const [events, setEvents] = useState([
    { time: '15:24:21', level: 'success', message: 'Read all status successful', tag: 'Info' },
    { time: '15:23:47', level: 'success', message: 'Administrative declaration sent', tag: 'Info' },
    { time: '15:23:47', level: 'info', message: 'Loom reply received', tag: 'Info' },
    { time: '15:20:10', level: 'success', message: 'Read all status successful', tag: 'Info' },
    { time: '15:15:33', level: 'info', message: 'Connection established', tag: 'Success' }
  ]);

  const realtimePollBusyRef = useRef(false);
  const latestAdminReplyTsRef = useRef('');

  useEffect(() => {
    api.loadConfig().then((loaded) => {
      setConfig(loaded);
      if (loaded?.looms?.[0]?.id) setSelectedId(loaded.looms[0].id);
    }).catch((error) => {
      setEvents((prev) => [{ time: beijingDateTime().split(' ')[1], level: 'error', message: `Config load failed: ${error.message}`, tag: 'Error' }, ...prev].slice(0, 12));
    });
  }, []);

  useEffect(() => {
    let cancelled = false;
    async function refreshBackendStatus() {
      try {
        const next = await api.getBackendStatus();
        if (!cancelled) setBackendStatus(next);
      } catch (error) {
        if (!cancelled) setBackendStatus({ healthy: false, starting: false, port: 18080, lastError: error.message });
      }
    }
    refreshBackendStatus();
    const timer = setInterval(refreshBackendStatus, 2000);
    return () => { cancelled = true; clearInterval(timer); };
  }, []);

  const selectedLoom = useMemo(() => {
    return config?.looms?.find((loom) => loom.id === selectedId) ?? config?.looms?.[0];
  }, [config, selectedId]);

  useEffect(() => {
    if (!selectedLoom) return;
    setStatus((prev) => ({
      ...prev,
      loomName: selectedLoom.name,
      hostPort: selectedLoom.hostPort,
      loomPort: selectedLoom.loomPort
    }));
  }, [selectedLoom]);

  useEffect(() => {
    if (!selectedLoom || typeof api.pollRealtimeStatus !== 'function') return;
    let cancelled = false;

    async function pollRealtimeStatus() {
      if (realtimePollBusyRef.current) return;
      realtimePollBusyRef.current = true;
      try {
        const result = await api.pollRealtimeStatus(selectedLoom);
        if (cancelled || !result) return;

        const statusRuntimeMinutes = getRuntimeMinutes(result, Array.isArray(result.speedHistory) ? result.speedHistory.length : 1);
        setSpeedSeries((prevSeries) => {
          const runtimeMinutes = getRuntimeMinutes(result, prevSeries.length);
          return getSpeedSeriesFromResult(result, prevSeries).slice(-runtimeMinutes);
        });
        setStatus((prev) => ({
          ...prev,
          currentSpeed: normalizeSpeedRpm(firstDefined(result.currentSpeed, prev.currentSpeed)),
          runtimeMinutes: statusRuntimeMinutes,
          runtimeSeconds: firstDefined(result.runtimeSeconds, prev.runtimeSeconds),
          lastSeen: result.lastSeen || prev.lastSeen
        }));

        const adminReply = normalizeDeclarationCompletion(result.adminCompletion);
        const replyStamp = adminReply?.receivedAt || '';
        if (adminReply && replyStamp !== latestAdminReplyTsRef.current) {
          latestAdminReplyTsRef.current = replyStamp;
          setReply(adminReply);
          setEvents((prev) => [{
            time: (adminReply.receivedAt || beijingDateTime()).split(' ')[1],
            level: 'success',
            message: 'Loom-side administrative declaration reply received',
            tag: 'Info'
          }, ...prev].slice(0, 12));
        }
      } catch (_error) {
        // Keep the real-time poll silent; explicit Read All Status still reports errors.
      } finally {
        realtimePollBusyRef.current = false;
      }
    }

    pollRealtimeStatus();
    const timer = setInterval(pollRealtimeStatus, 1000);
    return () => { cancelled = true; clearInterval(timer); };
  }, [selectedLoom]);

  async function readAllStatus() {
    if (!selectedLoom) return;
    setBusy(true);
    try {
      const result = await api.readAllStatus(selectedLoom);
      const runtimeMinutes = getRuntimeMinutes(result, status.runtimeMinutes || speedSeries.length);
      const nextSpeedSeries = getSpeedSeriesFromResult(result, speedSeries).slice(-runtimeMinutes);
      setStatus({ ...result, currentSpeed: normalizeSpeedRpm(result.currentSpeed), runtimeMinutes });
      setSpeedSeries(nextSpeedSeries);
      const backendEvents = Array.isArray(result.events) && result.events.length ? result.events : [result.event];
      setEvents((prev) => [...backendEvents, ...prev].slice(0, 12));
      if (config) {
        const updated = {
          ...config,
          looms: config.looms.map((loom) => loom.id === selectedLoom.id ? { ...loom, status: result.status || 'Online', lastSeen: result.lastSeen } : loom)
        };
        setConfig(updated);
      }
    } catch (error) {
      setEvents((prev) => [{ time: beijingDateTime().split(' ')[1], level: 'error', message: `Read failed: ${error.message}`, tag: 'Error' }, ...prev].slice(0, 12));
    } finally {
      setBusy(false);
    }
  }

  async function remoteStop() {
    if (!selectedLoom) return;

    const confirmed = window.confirm(
      `Confirm Remote Stop\n\nSelected loom: ${selectedLoom?.name || 'Unknown'}\nIP address: ${selectedLoom?.ipAddress || 'Not configured'}\nLoom port: ${selectedLoom?.loomPort || '—'}\n\nThis will send a remote stop command to the selected loom. Continue?`
    );

    if (!confirmed) {
      const cancelledAt = beijingDateTime();
      setEvents((prev) => [{ time: cancelledAt.split(' ')[1], level: 'info', message: 'Remote stop cancelled by operator', tag: 'Info' }, ...prev].slice(0, 12));
      return;
    }

    setRemoteStopBusy(true);
    try {
      const result = await api.remoteStop({ loomName: selectedLoom?.name, loomIp: selectedLoom?.ipAddress, loom: selectedLoom });
      const eventTime = (result?.time || beijingDateTime()).split(' ')[1];
      setStatus((prev) => ({ ...prev, loomState: 'Remote Stop Sent', lastSeen: result?.time || beijingDateTime() }));
      setEvents((prev) => [{ time: eventTime, level: 'success', message: result?.message || 'Remote stop command sent', tag: 'Info' }, ...prev].slice(0, 12));
    } catch (error) {
      const receivedAt = beijingDateTime();
      setEvents((prev) => [{ time: receivedAt.split(' ')[1], level: 'error', message: `Remote stop failed: ${error.message}`, tag: 'Error' }, ...prev].slice(0, 12));
    } finally {
      setRemoteStopBusy(false);
    }
  }

  async function sendDeclaration(payload) {
    try {
      const result = await api.sendDeclaration({ ...payload, loomName: selectedLoom?.name, loomIp: selectedLoom?.ipAddress, loom: selectedLoom });
      latestAdminReplyTsRef.current = '';
      setReply({
        receivedAt: result.receivedAt || beijingDateTime(),
        employeeId: '',
        productionPlanId: '',
        yarnPackageNumber: ''
      });
      setEvents((prev) => [
        result.event || { time: (result.receivedAt || beijingDateTime()).split(' ')[1], level: 'success', message: 'Administrative declaration configured; waiting for loom reply', tag: 'Info' },
        { time: (result.receivedAt || beijingDateTime()).split(' ')[1], level: 'info', message: 'Waiting for loom-side reply values', tag: 'Info' },
        ...prev
      ].slice(0, 12));
    } catch (error) {
      const receivedAt = beijingDateTime();
      setReply({
        receivedAt,
        employeeId: '',
        productionPlanId: '',
        yarnPackageNumber: ''
      });
      setEvents((prev) => [{ time: receivedAt.split(' ')[1], level: 'error', message: `Declaration failed: ${error.message}`, tag: 'Error' }, ...prev].slice(0, 12));
    }
  }

  function renderActiveModule() {
    const commonOverview = <LoomOverview selectedLoom={selectedLoom} status={status} onReadAll={readAllStatus} busy={busy} />;
    if (active === 'status') {
      return (
        <>
          <ModuleHeader title="Loom Status" description="Focused view for real-time status, production values, and speed trend monitoring." />
          {commonOverview}
          <MetricGrid status={status} onRemoteStop={remoteStop} remoteStopBusy={remoteStopBusy} />
          <div className="dashboard-grid">
            <SpeedChart points={speedSeries} runtimeMinutes={status.runtimeMinutes || speedSeries.length} />
            <SystemEvents events={events} onViewAll={() => setActive('history')} />
          </div>
        </>
      );
    }
    if (active === 'declaration') {
      return (
        <>
          <ModuleHeader title="Administrative Declaration" description="Send the employee, production plan, and yarn package declaration to the selected loom through the backend declaration template." />
          {commonOverview}
          <DeclarationPanel onSend={sendDeclaration} reply={reply} />
        </>
      );
    }
    if (active === 'ip') {
      return (
        <>
          <ModuleHeader title="IP Management" description="Maintain the loom list used by the host backend. Select a row before reading status or sending declarations." />
          <IpManagement config={config ?? { looms: [] }} selectedId={selectedId} onSelect={setSelectedId} onConfigChange={setConfig} />
        </>
      );
    }
    if (active === 'history') {
      return (
        <>
          <ModuleHeader title="History" description="Recent backend events and dashboard operation messages." />
          <SystemEvents events={events} expanded />
        </>
      );
    }
    if (active === 'settings') {
      return (
        <>
          <ModuleHeader title="Settings" description="Backend connection state, selected loom information, and runtime configuration details." />
          <SettingsPanel backendStatus={backendStatus} config={config} selectedLoom={selectedLoom} />
        </>
      );
    }
    return (
      <>
        {commonOverview}
        <MetricGrid status={status} onRemoteStop={remoteStop} remoteStopBusy={remoteStopBusy} />
        <div className="dashboard-grid">
          <SpeedChart points={speedSeries} runtimeMinutes={status.runtimeMinutes || speedSeries.length} />
          <SystemEvents events={events} onViewAll={() => setActive('history')} />
        </div>
        <DeclarationPanel onSend={sendDeclaration} reply={reply} />
        <IpManagement config={config ?? { looms: [] }} selectedId={selectedId} onSelect={setSelectedId} onConfigChange={setConfig} />
      </>
    );
  }

  return (
    <div className="app-shell">
      <Sidebar active={active} onChange={setActive} />
      <main className="main-area">
        <Header backendStatus={backendStatus} />
        <div className="content-area">
          {renderActiveModule()}
        </div>
      </main>
    </div>
  );
}

export default function App() {
  return <Dashboard />;
}
