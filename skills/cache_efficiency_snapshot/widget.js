/**
 * Cache Efficiency Snapshot — Standalone Module Widget
 * Theme-aware Dashboard with Canvas 2D Charts and Multi-Timeframe Analytics.
 * Zero external CDN dependencies.
 */
(function () {
  const root = document.getElementById('root');
  if (!root) return;

  // Injected Scoped CSS
  const style = document.createElement('style');
  style.textContent = `
    :root {
      color-scheme: dark;
      --ces-background: #0d0b0f; --ces-text: #f8fafc; --ces-value: #ffffff;
      --ces-body: #e2e8f0; --ces-muted: #94a3b8; --ces-slate: #64748b;
      --ces-green: #34d399; --ces-cyan: #38bdf8; --ces-violet: #a855f7;
      --ces-accent: #e85d6f; --ces-danger: #f43f5e; --ces-error: #fca5a5; --ces-amber: #c69245;
      --ces-ink-rgb: 255, 255, 255; --ces-base-rgb: 13, 11, 15;
      --ces-surface-rgb: 23, 18, 28; --ces-tooltip-rgb: 17, 13, 22;
      --ces-soft-accent-rgb: 240, 122, 134; --ces-accent-rgb: 232, 93, 111;
      --ces-danger-rgb: 244, 63, 94; --ces-cyan-rgb: 56, 189, 248; --ces-shadow-rgb: 0, 0, 0;
      --ces-grid: rgba(var(--ces-ink-rgb), 0.06);
      --ces-crosshair: rgba(var(--ces-soft-accent-rgb), 0.45);
    }
    :root[data-theme=light] {
      color-scheme: light;
      --ces-background: #f8fafc; --ces-text: #18212f; --ces-value: #0f172a;
      --ces-body: #334155; --ces-muted: #526174; --ces-slate: #64748b;
      --ces-green: #047857; --ces-cyan: #0369a1; --ces-violet: #7e22ce;
      --ces-accent: #b82f43; --ces-danger: #be123c; --ces-error: #9f1239; --ces-amber: #92600c;
      --ces-ink-rgb: 15, 23, 42; --ces-base-rgb: 241, 245, 249;
      --ces-surface-rgb: 255, 255, 255; --ces-tooltip-rgb: 255, 255, 255;
      --ces-soft-accent-rgb: 184, 47, 67; --ces-accent-rgb: 184, 47, 67;
      --ces-danger-rgb: 190, 18, 60; --ces-cyan-rgb: 3, 105, 161; --ces-shadow-rgb: 148, 163, 184;
    }
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body, #root {
      font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, 'SF Pro Display', Inter, sans-serif;
      background: var(--ces-background);
      color: var(--ces-text);
      width: 100%;
      min-height: 100%;
      overflow-x: hidden;
      -webkit-font-smoothing: antialiased;
    }
    
    #root { padding: 16px 18px; }
    /* Layout */
    .ces-container {
      display: flex;
      flex-direction: column;
      gap: 14px;
      width: 100%;
      max-width: 1200px;
      margin: 0 auto;
    }

    /* Glass Card */
    .ces-card {
      background: rgba(var(--ces-surface-rgb), 0.75);
      backdrop-filter: blur(16px);
      -webkit-backdrop-filter: blur(16px);
      border: 1px solid rgba(var(--ces-soft-accent-rgb), 0.14);
      border-radius: 12px;
      padding: 14px 18px;
      box-shadow: 0 8px 32px rgba(var(--ces-shadow-rgb), 0.36);
      transition: border-color 0.2s ease, box-shadow 0.2s ease;
    }
    .ces-card:hover {
      border-color: rgba(var(--ces-soft-accent-rgb), 0.28);
      box-shadow: 0 10px 36px rgba(var(--ces-accent-rgb), 0.08);
    }

    /* Error Banner */
    .ces-error-banner {
      display: none;
      background: rgba(var(--ces-danger-rgb), 0.16);
      border: 1px solid rgba(var(--ces-danger-rgb), 0.45);
      color: var(--ces-error);
      padding: 10px 14px;
      border-radius: 8px;
      font-size: 0.8rem;
      align-items: center;
      justify-content: space-between;
      gap: 10px;
    }

    /* Header */
    .ces-header {
      display: flex;
      justify-content: space-between;
      align-items: center;
      flex-wrap: wrap;
      gap: 12px;
    }
    .ces-title-group {
      display: flex;
      align-items: center;
      gap: 12px;
    }
    .ces-pulse {
      width: 10px;
      height: 10px;
      border-radius: 50%;
      background: var(--ces-green);
      box-shadow: 0 0 12px var(--ces-green);
      animation: ces-pulse-glow 2s infinite ease-in-out;
    }
    @keyframes ces-pulse-glow {
      0%, 100% { transform: scale(1); opacity: 0.85; }
      50% { transform: scale(1.3); opacity: 1; box-shadow: 0 0 16px var(--ces-green); }
    }
    .ces-pulse.error {
      background: var(--ces-danger);
      box-shadow: 0 0 12px var(--ces-danger);
      animation: none;
    }
    .ces-title {
      font-size: 1.12rem;
      font-weight: 700;
      letter-spacing: -0.02em;
      color: var(--ces-text);
      display: flex;
      align-items: center;
      gap: 8px;
    }
    .ces-subtitle {
      font-size: 0.76rem;
      color: var(--ces-muted);
      margin-top: 2px;
    }

    /* Controls */
    .ces-controls {
      display: flex;
      align-items: center;
      gap: 10px;
      flex-wrap: wrap;
    }
    .ces-timeframe-group {
      display: inline-flex;
      background: rgba(var(--ces-base-rgb), 0.85);
      border: 1px solid rgba(var(--ces-ink-rgb), 0.08);
      border-radius: 8px;
      padding: 3px;
    }
    .ces-tf-btn {
      background: transparent;
      border: none;
      color: var(--ces-muted);
      font-size: 0.75rem;
      font-weight: 600;
      padding: 5px 12px;
      border-radius: 6px;
      cursor: pointer;
      transition: all 0.15s ease;
    }
    .ces-tf-btn:hover {
      color: var(--ces-text);
      background: rgba(var(--ces-ink-rgb), 0.05);
    }
    .ces-tf-btn.active {
      color: var(--ces-text);
      background: rgba(var(--ces-accent-rgb), 0.22);
      border: 1px solid rgba(var(--ces-accent-rgb), 0.4);
      box-shadow: 0 0 10px rgba(var(--ces-accent-rgb), 0.25);
    }

    .ces-btn {
      background: rgba(var(--ces-surface-rgb), 0.9);
      border: 1px solid rgba(var(--ces-soft-accent-rgb), 0.2);
      color: var(--ces-text);
      font-size: 0.78rem;
      font-weight: 600;
      padding: 6px 14px;
      border-radius: 8px;
      cursor: pointer;
      display: inline-flex;
      align-items: center;
      gap: 6px;
      transition: all 0.2s ease;
    }
    .ces-btn:hover {
      background: rgba(var(--ces-accent-rgb), 0.15);
      border-color: rgba(var(--ces-accent-rgb), 0.45);
    }
    .ces-btn:active { transform: scale(0.97); }
    .ces-btn.spinning svg {
      animation: ces-spin 0.8s linear infinite;
    }
    @keyframes ces-spin { 100% { transform: rotate(360deg); } }

    /* KPI Grid */
    .ces-kpi-grid {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
      gap: 12px;
    }
    .ces-kpi-card {
      position: relative;
      overflow: hidden;
      display: flex;
      flex-direction: column;
      justify-content: space-between;
      gap: 8px;
    }
    .ces-kpi-card::before {
      content: '';
      position: absolute;
      top: 0; left: 0; bottom: 0;
      width: 4px;
      border-radius: 4px 0 0 4px;
    }
    .ces-kpi-card.crimson::before { background: var(--ces-accent); box-shadow: 0 0 8px var(--ces-accent); }
    .ces-kpi-card.cyan::before { background: var(--ces-cyan); box-shadow: 0 0 8px var(--ces-cyan); }
    .ces-kpi-card.emerald::before { background: var(--ces-green); box-shadow: 0 0 8px var(--ces-green); }
    .ces-kpi-card.violet::before { background: var(--ces-violet); box-shadow: 0 0 8px var(--ces-violet); }

    .ces-kpi-header {
      display: flex;
      justify-content: space-between;
      align-items: center;
    }
    .ces-kpi-label {
      font-size: 0.74rem;
      font-weight: 600;
      color: var(--ces-muted);
      text-transform: uppercase;
      letter-spacing: 0.04em;
    }
    .ces-kpi-badge {
      font-size: 0.68rem;
      font-weight: 700;
      padding: 2px 7px;
      border-radius: 10px;
      background: rgba(var(--ces-ink-rgb), 0.08);
      color: var(--ces-text);
    }
    .ces-kpi-val {
      font-size: 1.75rem;
      font-weight: 800;
      letter-spacing: -0.03em;
      color: var(--ces-value);
      font-variant-numeric: tabular-nums;
      display: flex;
      align-items: baseline;
      gap: 4px;
    }
    .ces-kpi-unit {
      font-size: 0.95rem;
      font-weight: 600;
      color: var(--ces-muted);
    }
    .ces-kpi-sub {
      font-size: 0.74rem;
      color: var(--ces-muted);
      display: flex;
      align-items: center;
      gap: 5px;
    }

    /* Chart Section */
    .ces-chart-card {
      display: flex;
      flex-direction: column;
      gap: 12px;
    }
    .ces-chart-header {
      display: flex;
      justify-content: space-between;
      align-items: center;
      flex-wrap: wrap;
      gap: 10px;
    }
    .ces-chart-title {
      font-size: 0.92rem;
      font-weight: 700;
      color: var(--ces-text);
    }
    .ces-chart-mode-group {
      display: inline-flex;
      background: rgba(var(--ces-base-rgb), 0.85);
      border: 1px solid rgba(var(--ces-ink-rgb), 0.08);
      border-radius: 6px;
      padding: 2px;
    }
    .ces-chart-mode-btn {
      background: transparent;
      border: none;
      color: var(--ces-muted);
      font-size: 0.72rem;
      font-weight: 600;
      padding: 4px 10px;
      border-radius: 4px;
      cursor: pointer;
    }
    .ces-chart-mode-btn.active {
      color: var(--ces-cyan);
      background: rgba(var(--ces-cyan-rgb), 0.15);
    }

    .ces-canvas-wrap {
      position: relative;
      width: 100%;
      height: 230px;
      background: rgba(var(--ces-base-rgb), 0.5);
      border-radius: 10px;
      border: 1px solid rgba(var(--ces-ink-rgb), 0.04);
      overflow: hidden;
    }
    canvas {
      display: block;
      width: 100%;
      height: 100%;
    }
    .ces-tooltip {
      position: absolute;
      pointer-events: none;
      display: none;
      background: rgba(var(--ces-tooltip-rgb), 0.94);
      backdrop-filter: blur(12px);
      border: 1px solid rgba(var(--ces-soft-accent-rgb), 0.3);
      box-shadow: 0 6px 20px rgba(var(--ces-shadow-rgb),0.6);
      border-radius: 8px;
      padding: 8px 12px;
      font-size: 0.74rem;
      color: var(--ces-text);
      z-index: 100;
      white-space: nowrap;
      transform: translate(-50%, -110%);
      transition: opacity 0.1s ease;
    }

    /* Model Table */
    .ces-table td:first-child { min-width: 170px; max-width: 290px; overflow-wrap: anywhere; }
    .ces-table-wrap {
      overflow-x: auto;
      margin-top: 6px;
    }
    table.ces-table {
      width: 100%;
      border-collapse: collapse;
      font-size: 0.78rem;
      text-align: left;
    }
    table.ces-table th {
      padding: 8px 10px;
      font-weight: 600;
      color: var(--ces-muted);
      text-transform: uppercase;
      font-size: 0.68rem;
      letter-spacing: 0.05em;
      border-bottom: 1px solid rgba(var(--ces-ink-rgb), 0.08);
      cursor: pointer;
      user-select: none;
    }
    table.ces-table th:hover { color: var(--ces-text); }
    table.ces-table td {
      padding: 10px;
      border-bottom: 1px solid rgba(var(--ces-ink-rgb), 0.04);
      color: var(--ces-body);
      font-variant-numeric: tabular-nums;
    }
    table.ces-table tr:hover td {
      background: rgba(var(--ces-ink-rgb), 0.02);
    }
    .ces-bar-wrap {
      width: 100%;
      min-width: 90px;
      height: 5px;
      background: rgba(var(--ces-ink-rgb), 0.08);
      border-radius: 3px;
      overflow: hidden;
      margin-top: 4px;
    }
    .ces-bar-fill {
      height: 100%;
      border-radius: 3px;
      background: linear-gradient(90deg, var(--ces-accent), var(--ces-cyan));
    }

    /* Diagnostics Drawer */
    details.ces-drawer {
      border-top: 1px solid rgba(var(--ces-ink-rgb), 0.06);
      padding-top: 8px;
    }
    summary.ces-drawer-summary {
      font-size: 0.76rem;
      font-weight: 600;
      color: var(--ces-muted);
      cursor: pointer;
      list-style: none;
      display: flex;
      align-items: center;
      gap: 6px;
      user-select: none;
    }
    summary.ces-drawer-summary::-webkit-details-marker { display: none; }
    summary.ces-drawer-summary:hover { color: var(--ces-text); }
    .ces-drawer-content {
      margin-top: 10px;
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
      gap: 10px;
      font-size: 0.74rem;
      color: var(--ces-muted);
    }
    .ces-diag-item {
      background: rgba(var(--ces-base-rgb), 0.6);
      padding: 7px 10px;
      border-radius: 6px;
      border: 1px solid rgba(var(--ces-ink-rgb), 0.03);
    }
    .ces-diag-item b { color: var(--ces-text); }
    .ces-note { color: var(--ces-muted); font-size: 12px; line-height: 1.5; }
    .ces-route { display: block; color: var(--ces-muted); font-size: 12px; font-weight: 400; overflow-wrap: anywhere; margin-top: 4px; }
    .ces-kpi-sub { flex-wrap: wrap; line-height: 1.4; }
    .ces-tooltip { max-width: calc(100% - 16px); white-space: normal; transform: none; }
    @media (max-width: 540px) {
      #root { padding: 10px; }
      .ces-card { padding: 12px; }
      .ces-kpi-grid { grid-template-columns: 1fr; }
      .ces-controls { width: 100%; }
      .ces-tf-btn { padding: 7px 9px; }
      .ces-table { min-width: 650px; }
    }
  `;
  document.head.appendChild(style);

  // App State
  const state = {
    timeframe: '24H',
    chartMode: 'trend',
    autoRefreshSec: 30,
    pollTimer: null,
    data: null,
    loading: false,
    errorMessage: '',
    reqSequence: 0,
    sortCol: 'prompt_tokens',
    sortAsc: false,
    disposed: false,
  };

  // Helpers
  function formatTokens(val) {
    if (val === null || val === undefined || !Number.isFinite(val)) return '—';
    if (val >= 1e9) return (val / 1e9).toFixed(2) + ' B';
    if (val >= 1e6) return (val / 1e6).toFixed(2) + ' M';
    if (val >= 1e3) return (val / 1e3).toFixed(1) + ' K';
    return Number(val).toLocaleString();
  }

  function formatRate(value) {
    return Number.isFinite(value) ? `${value.toFixed(2)}%` : 'Unavailable';
  }

  function escapeHtml(value) {
    const node = document.createElement('span');
    node.textContent = String(value);
    return node.innerHTML;
  }

  function counted(value, unit) {
    return `${value.toLocaleString()} ${unit}${value === 1 ? '' : 's'}`;
  }

  function recordUnits(value) {
    const units = [counted(value.request_count, 'request'), counted(value.session_count, 'session')];
    if (value.other_count) units.push(counted(value.other_count, 'other record'));
    return units.join(' · ');
  }

  function coverageText(value) {
    return `${value.measured_records.toLocaleString()} / ${counted(value.total_records, 'record')} measured`;
  }

  // Bins are labelled in the viewer's own time zone, and the zone is named:
  // unlabelled server (UTC) times read as local time and moved a drop by hours.
  function zoneName() {
    const minutes = -new Date().getTimezoneOffset();
    const abs = Math.abs(minutes);
    const offset = `UTC${minutes >= 0 ? '+' : '-'}${String(Math.floor(abs / 60)).padStart(2, '0')}:${String(abs % 60).padStart(2, '0')}`;
    let zone = '';
    try { zone = Intl.DateTimeFormat().resolvedOptions().timeZone || ''; } catch (err) { zone = ''; }
    return zone ? `${zone} (${offset})` : offset;
  }

  function localStamp(iso, withYear) {
    const date = new Date(iso);
    if (Number.isNaN(date.getTime())) return String(iso || '');
    const pad = value => String(value).padStart(2, '0');
    const stamp = `${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(date.getHours())}:${pad(date.getMinutes())}`;
    return withYear ? `${date.getFullYear()}-${stamp}` : stamp;
  }

  function bucketLabel(bucket) {
    return localStamp(bucket.ts, Boolean(state.data && state.data.timeframe === 'ALL'));
  }

  function legendText() {
    const base = state.chartMode === 'trend'
      ? 'Gaps mean no comparable measurement. A measured zero is 0%.'
      : 'Green: cache reads · Slate: other input · Amber: input with unknown cache reads. Unknown input volumes cannot be drawn.';
    const marks = state.data && state.data.markers && state.data.markers.length
      ? ' Dashed violet lines: first start of a new code version (a time mark, not a cause).' : '';
    return `${base}${marks} Times: ${zoneName()}.`;
  }

  // Render Skeleton Layout
  root.innerHTML = `
    <div class="ces-container">
      <div class="ces-error-banner" id="errorBanner">
        <span id="errorText">Failed to load live metrics</span>
        <button class="ces-btn" id="retryBtn" style="padding:4px 10px; font-size:0.72rem;">Retry</button>
      </div>

      <div class="ces-card ces-header">
        <div class="ces-title-group">
          <div class="ces-pulse" id="livePulse"></div>
          <div>
            <div class="ces-title">Cache Efficiency Snapshot</div>
            <div class="ces-subtitle" id="timeRangeSub">Measured input tokens, cache reads and coverage</div>
          </div>
        </div>
        <div class="ces-controls">
          <div class="ces-timeframe-group" id="tfGroup">
            <button class="ces-tf-btn" data-tf="1H">1H</button>
            <button class="ces-tf-btn" data-tf="6H">6H</button>
            <button class="ces-tf-btn active" data-tf="24H">24H</button>
            <button class="ces-tf-btn" data-tf="7D">7D</button>
            <button class="ces-tf-btn" data-tf="ALL">ALL</button>
          </div>
          <button class="ces-btn" id="refreshBtn">
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><path d="M21.5 2v6h-6M21.34 15.57a10 10 0 1 1-.57-8.38l5.67-5.67"/></svg>
            Refresh
          </button>
        </div>
      </div>

      <div class="ces-kpi-grid">
        <div class="ces-card ces-kpi-card crimson">
          <div class="ces-kpi-header">
            <span class="ces-kpi-label">Cache Read Rate</span>
            <span class="ces-kpi-badge" id="rateBadge">Token-Weighted</span>
          </div>
          <div class="ces-kpi-val"><span id="kpiRate">—</span></div>
          <div class="ces-kpi-sub" id="kpiRateSub">Measured input only</div>
        </div>

        <div class="ces-card ces-kpi-card cyan">
          <div class="ces-kpi-header">
            <span class="ces-kpi-label">Known Cache Reads</span>
            <span class="ces-kpi-badge" id="tokensRatioBadge">Tokens</span>
          </div>
          <div class="ces-kpi-val"><span id="kpiCacheReads">--</span></div>
          <div class="ces-kpi-sub" id="kpiCachedSub">Cache-read measurements</div>
        </div>

        <div class="ces-card ces-kpi-card emerald">
          <div class="ces-kpi-header">
            <span class="ces-kpi-label">Measurement Coverage</span>
            <span class="ces-kpi-badge" style="color:var(--ces-green)">Records</span>
          </div>
          <div class="ces-kpi-val"><span id="kpiCoverage">--</span></div>
          <div class="ces-kpi-sub" id="kpiCoverageSub">Unknown measurements stay unknown</div>
        </div>

        <div class="ces-card ces-kpi-card violet">
          <div class="ces-kpi-header">
            <span class="ces-kpi-label">Known Input Volume</span>
            <span class="ces-kpi-badge" id="recordsBadge">— records</span>
          </div>
          <div class="ces-kpi-val"><span id="kpiVolume">--</span></div>
          <div class="ces-kpi-sub" id="kpiVolumeSub">Requests and sessions counted separately</div>
        </div>
      </div>

      <div class="ces-card ces-chart-card">
        <div class="ces-chart-header">
          <div class="ces-chart-title" id="chartTitle">Cache-Read Rate Trend (%)</div>
          <div class="ces-chart-mode-group">
            <button class="ces-chart-mode-btn active" data-mode="trend">📈 Rate Trend</button>
            <button class="ces-chart-mode-btn" data-mode="volume">📊 Token Volume</button>
          </div>
        </div>
        <div class="ces-canvas-wrap" id="canvasWrap">
          <canvas id="chartCanvas" tabindex="0" role="img" aria-label="Cache measurements by time. Use arrow keys or select a point to inspect."></canvas>
          <div class="ces-tooltip" id="chartTooltip"></div>
        </div>
        <div class="ces-note" id="chartLegend">Gaps mean no comparable measurement. A measured zero is 0%.</div>
        <div class="ces-note" id="chartReading" aria-live="polite">Select a point to inspect its measurements.</div>
      </div>

      <div class="ces-card">
        <div class="ces-chart-title" style="margin-bottom:10px;">Model and Route Measurements</div>
        <div class="ces-note">Session totals may span attempts and models; their label names the final reported model. A session recorded without a usage event shows only its route; its tokens are unknown here. Rates use only records with both input and cache-read measurements.</div>
        <div class="ces-table-wrap">
          <table class="ces-table" id="modelsTable">
            <thead>
              <tr>
                <th data-sort="display_name">Model</th>
                <th data-sort="total_records">Recorded Units</th>
                <th data-sort="prompt_tokens">Known Input</th>
                <th data-sort="cached_tokens">Known Cache Reads</th>
                <th data-sort="rate">Read Rate</th>
                <th data-sort="measured_records">Coverage</th>
              </tr>
            </thead>
            <tbody id="modelsTbody">
              <tr><td colspan="6" style="text-align:center; color:var(--ces-slate);">Loading analytics data...</td></tr>
            </tbody>
          </table>
        </div>

        <details class="ces-drawer" style="margin-top:14px;">
          <summary class="ces-drawer-summary">
            <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M9 18l6-6-6-6"/></svg>
            Data Quality & System Diagnostics
          </summary>
          <div class="ces-drawer-content" id="diagnosticsContent">
            <div class="ces-diag-item">Window Samples: <b id="diagWindow">--</b></div>
            <div class="ces-diag-item">Retained Records: <b id="diagTotal">--</b></div>
            <div class="ces-diag-item">Window Span: <b id="diagSpan">--</b></div>
            <div class="ces-diag-item">Omitted Rows / Buckets / Marks: <b id="diagOmitted">--</b></div>
          </div>
          <div class="ces-note" id="retainedCoverage" style="margin-top:8px;"></div>
        </details>
      </div>
    </div>
  `;

  // Canvas & Interaction Engine
  const canvas = document.getElementById('chartCanvas');
  const ctx = canvas.getContext('2d');
  let paintDpr = 1;
  const canvasWrap = document.getElementById('canvasWrap');
  const tooltip = document.getElementById('chartTooltip');
  let colors;
  function readChartColors() {
    const css = getComputedStyle(document.documentElement);
    colors = {};
    for (const name of ['background', 'muted', 'slate', 'green', 'cyan', 'violet', 'accent', 'amber', 'grid', 'crosshair']) {
      colors[name] = css.getPropertyValue('--ces-' + name).trim();
    }
  }
  readChartColors();

  let mouseX = -1;
  let isHovering = false;

  function resizeCanvas() {
    const rect = canvasWrap.getBoundingClientRect();
    const dpr = window.devicePixelRatio || 1;
    canvas.width = rect.width * dpr;
    canvas.height = rect.height * dpr;
    if (typeof ctx.resetTransform === 'function') {
      ctx.resetTransform();
    } else {
      ctx.setTransform(1, 0, 0, 1, 0, 0);
    }
    ctx.scale(dpr, dpr);
    paintDpr = dpr;
    renderChart();
  }

  window.addEventListener('resize', resizeCanvas);

  function inspectPointer(e) {
    const rect = canvasWrap.getBoundingClientRect();
    mouseX = e.clientX - rect.left;
    isHovering = true;
    renderChart();
  }
  canvasWrap.addEventListener('mousemove', inspectPointer);
  canvasWrap.addEventListener('click', inspectPointer);
  canvas.addEventListener('keydown', (event) => {
    if (event.key !== 'ArrowLeft' && event.key !== 'ArrowRight') return;
    event.preventDefault();
    const count = state.data?.buckets?.length || 0;
    if (!count) return;
    // Step by whole bins on the same centres both charts draw (see renderTrendChart).
    const chartW = canvasWrap.getBoundingClientRect().width - 65;
    const current = mouseX < 0 ? -1 : Math.round((mouseX - 45) / chartW * count - 0.5);
    const next = Math.max(0, Math.min(count - 1, current + (event.key === 'ArrowRight' ? 1 : -1)));
    mouseX = 45 + (next + 0.5) * chartW / count;
    isHovering = true;
    renderChart();
  });

  function clearInspection() {
    isHovering = false;
    mouseX = -1;
    tooltip.style.display = 'none';
    document.getElementById('chartReading').textContent = 'Select a point to inspect its measurements.';
  }

  canvasWrap.addEventListener('mouseleave', () => {
    clearInspection();
    renderChart();
  });

  function renderChart() {
    // A retained frame can receive a theme while its host page is hidden.
    // Its layout rect is then zero, but the canvas keeps its last paint size.
    // Use the scale applied at resize, even if the display DPR since changed.
    const w = canvas.width / paintDpr;
    const h = canvas.height / paintDpr;
    ctx.clearRect(0, 0, w, h);

    if (!state.data || !state.data.buckets || state.data.buckets.length === 0) {
      // Nothing left to inspect: a stale reading would describe a vanished bin.
      clearInspection();
      ctx.fillStyle = colors.slate;
      ctx.font = '12px system-ui';
      ctx.textAlign = 'center';
      ctx.fillText(state.errorMessage ? 'Data unavailable: ' + state.errorMessage : 'No activity records in this timeframe', w / 2, h / 2);
      return;
    }

    const buckets = state.data.buckets;
    const padding = { top: 20, right: 20, bottom: 26, left: 45 };
    const chartW = w - padding.left - padding.right;
    const chartH = h - padding.top - padding.bottom;

    if (state.chartMode === 'trend') {
      renderTrendChart(buckets, padding, chartW, chartH, w, h);
    } else {
      renderVolumeChart(buckets, padding, chartW, chartH, w, h);
    }
  }

  function drawAxes(buckets, points, pad, cw, ch, w, h, maximum, percentage) {
    ctx.strokeStyle = colors.grid;
    ctx.lineWidth = 1;
    ctx.fillStyle = colors.muted;
    ctx.font = '10px monospace';
    ctx.textAlign = 'right';
    for (let index = 0; index <= 4; index++) {
      const y = pad.top + ch - index / 4 * ch;
      ctx.beginPath();
      ctx.moveTo(pad.left, y);
      ctx.lineTo(pad.left + cw, y);
      ctx.stroke();
      ctx.fillText(percentage ? `${index * 25}%` : formatTokens(maximum * index / 4), pad.left - 6, y + 3);
    }
    ctx.textAlign = 'center';
    const labelStep = Math.max(1, Math.ceil(buckets.length / Math.max(1, Math.floor(cw / 120))));
    for (let index = 0; index < buckets.length; index += labelStep) {
      const x = Math.max(75, Math.min(w - 65, points[index].x));
      ctx.fillText(bucketLabel(buckets[index]), x, h - 8);
    }
  }

  // A dashed line is the first start of new code: a time mark, not a cause.
  function drawMarkers(count, pad, cw, ch) {
    const marks = (state.data && state.data.markers) || [];
    if (!marks.length) return;
    ctx.save();
    ctx.strokeStyle = colors.violet;
    ctx.fillStyle = colors.violet;
    ctx.setLineDash([4, 4]);
    ctx.lineWidth = 1;
    ctx.font = '10px monospace';
    ctx.textAlign = 'left';
    let labelEnd = -Infinity;
    for (const mark of marks) {
      const x = pad.left + mark.position * cw / count;
      ctx.beginPath();
      ctx.moveTo(x, pad.top);
      ctx.lineTo(x, pad.top + ch);
      ctx.stroke();
      // A label that would overlap its neighbour is left to the inspection reading.
      const text = mark.code.slice(0, 7);
      const width = ctx.measureText(text).width;
      const textX = Math.min(x + 3, pad.left + cw - width);
      if (textX >= labelEnd + 6) {
        ctx.fillText(text, textX, pad.top + 10);
        labelEnd = textX + width;
      }
    }
    ctx.restore();
  }

  function inspectChart(points, pad, cw, ch) {
    if (!isHovering || !points.length) return;
    const closest = points.reduce((best, point) => Math.abs(mouseX - point.x) < Math.abs(mouseX - best.x) ? point : best);
    const data = closest.data;
    const index = points.indexOf(closest);
    const label = bucketLabel(data);
    const marks = ((state.data && state.data.markers) || []).filter(mark => mark.position >= index && mark.position < index + 1);
    const started = marks.length ? `New code started: ${marks.map(mark => `${mark.code} at ${localStamp(mark.ts, false).slice(6)}`).join(', ')}.` : '';
    ctx.strokeStyle = colors.crosshair;
    ctx.setLineDash([3, 3]);
    ctx.beginPath();
    ctx.moveTo(closest.x, pad.top);
    ctx.lineTo(closest.x, pad.top + ch);
    ctx.stroke();
    ctx.setLineDash([]);
    const measured = Number.isFinite(data.rate) ? ` (${formatTokens(data.eligible_cached_tokens)} / ${formatTokens(data.eligible_prompt_tokens)} measured)` : '';
    const detail = data.total_records === 0 ? 'No activity' : `${formatRate(data.rate)}${measured} · ${coverageText(data)} · ${recordUnits(data)}`;
    const unknown = `${counted(data.unknown_total_records, 'record')} with unknown input; ${data.unknown_read_records} with unknown cache reads`;
    document.getElementById('chartReading').textContent = `${label}: ${detail}. ${unknown}.${started ? ' ' + started : ''}`;
    tooltip.innerHTML = `<b>${escapeHtml(label)}</b><br>${escapeHtml(detail)}<br>Known input: ${formatTokens(data.prompt_tokens)}<br>Known cache reads: ${formatTokens(data.cached_tokens)}<br>${escapeHtml(unknown)}${started ? '<br>' + escapeHtml(started) : ''}`;
    tooltip.style.display = 'block';
    const width = tooltip.offsetWidth;
    tooltip.style.left = `${Math.max(8, Math.min(closest.x - width / 2, canvasWrap.clientWidth - width - 8))}px`;
    tooltip.style.top = '8px';
  }

  function renderTrendChart(buckets, pad, cw, ch, w, h) {
    const points = buckets.map((data, index) => ({
      x: pad.left + (index + 0.5) * cw / buckets.length,
      y: Number.isFinite(data.rate) ? pad.top + ch - data.rate / 100 * ch : null,
      data,
    }));
    drawAxes(buckets, points, pad, cw, ch, w, h, 100, true);
    // Connect only adjacent measured bins; unavailable bins break the path.
    ctx.beginPath();
    let previous = null;
    for (const point of points) {
      if (point.y === null) { previous = null; continue; }
      if (previous) ctx.lineTo(point.x, point.y);
      else ctx.moveTo(point.x, point.y);
      previous = point;
    }
    ctx.strokeStyle = colors.accent;
    ctx.lineWidth = 2.5;
    ctx.stroke();
    for (const point of points) {
      if (point.y === null) continue;
      ctx.beginPath();
      ctx.arc(point.x, point.y, 3, 0, Math.PI * 2);
      ctx.fillStyle = colors.background;
      ctx.fill();
      ctx.strokeStyle = colors.cyan;
      ctx.lineWidth = 1.5;
      ctx.stroke();
    }
    if (!points.some(point => point.y !== null)) {
      ctx.fillStyle = colors.muted;
      ctx.textAlign = 'center';
      ctx.fillText('No comparable cache measurements', pad.left + cw / 2, pad.top + ch / 2);
    }
    drawMarkers(buckets.length, pad, cw, ch);
    inspectChart(points, pad, cw, ch);
  }

  function renderVolumeChart(buckets, pad, cw, ch, w, h) {
    const maximum = Math.max(...buckets.map(bucket => bucket.prompt || 0), 1);
    const step = cw / buckets.length;
    const points = buckets.map((data, index) => ({x: pad.left + (index + 0.5) * step, data}));
    drawAxes(buckets, points, pad, cw, ch, w, h, maximum, false);
    buckets.forEach((bucket, index) => {
      let y = pad.top + ch;
      for (const [value, color] of [[bucket.cached, colors.green], [bucket.uncached, colors.slate], [bucket.unknown, colors.amber]]) {
        const height = value / maximum * ch;
        y -= height;
        ctx.fillStyle = color;
        ctx.fillRect(points[index].x - step * 0.32, y, step * 0.64, height);
      }
    });
    drawMarkers(buckets.length, pad, cw, ch);
    inspectChart(points, pad, cw, ch);
  }

  // Data Fetching
  async function fetchData() {
    if (state.disposed) return;
    const seq = ++state.reqSequence;
    state.loading = true;
    const btn = document.getElementById('refreshBtn');
    const pulse = document.getElementById('livePulse');
    const errorBanner = document.getElementById('errorBanner');
    const errorText = document.getElementById('errorText');

    if (btn) btn.classList.add('spinning');

    try {
      const resp = await window.fetch(`/api/extensions/cache_efficiency_snapshot/data?timeframe=${state.timeframe}`);
      if (!resp.ok) {
        throw new Error(`HTTP ${resp.status} ${resp.statusText}`);
      }
      const json = await resp.json();

      if (seq === state.reqSequence) {
        state.data = json;
        state.errorMessage = '';
        if (errorBanner) errorBanner.style.display = 'none';
        if (pulse) pulse.classList.remove('error');
        updateUI();
      }
    } catch (err) {
      console.error('[CacheEfficiency] Fetch error:', err);
      if (seq === state.reqSequence) {
        state.errorMessage = err.message || 'Connection failed';
        if (errorBanner) {
          errorBanner.style.display = 'flex';
          if (errorText) errorText.textContent = `Error fetching data: ${state.errorMessage} (showing last known data)`;
        }
        if (pulse) pulse.classList.add('error');
      }
    } finally {
      if (seq === state.reqSequence) {
        state.loading = false;
        if (btn) btn.classList.remove('spinning');
      }
    }
  }

  function updateUI() {
    if (!state.data || !state.data.summary) return;
    const s = state.data.summary;
    const q = state.data.quality || {};
    const errorBanner = document.getElementById('errorBanner');
    const errorText = document.getElementById('errorText');
    const pulse = document.getElementById('livePulse');

    if (state.data.status === 'degraded' || q.read_error) {
      if (errorBanner) {
        errorBanner.style.display = 'flex';
        if (errorText) errorText.textContent = `Warning: Usage log read degraded (${q.read_error || 'partial read'}). Data may be incomplete.`;
      }
      if (pulse) pulse.classList.add('error');
    }

    // Rates use a matching measured subset; known token sums disclose missing records.
    document.getElementById('kpiRate').textContent = formatRate(s.cache_read_rate);
    document.getElementById('kpiRateSub').textContent = Number.isFinite(s.cache_read_rate)
      ? `${formatTokens(s.eligible_cached_tokens)} cache reads / ${formatTokens(s.eligible_prompt_tokens)} input tokens in ${counted(s.rate_records, 'record')}`
      : 'No record has both a positive input total and a cache-read measurement';
    document.getElementById('kpiCacheReads').textContent = formatTokens(s.cached_tokens);
    document.getElementById('tokensRatioBadge').textContent = `${s.unknown_read_records} unknown`;
    document.getElementById('kpiCachedSub').textContent = 'Reported cache reads; unknown records excluded';
    document.getElementById('kpiCoverage').textContent = `${s.measured_records} / ${s.total_records}`;
    document.getElementById('kpiCoverageSub').textContent = `Records with both measurements · ${s.zero_volume_records} have zero input`;
    document.getElementById('kpiVolume').textContent = formatTokens(s.prompt_tokens);
    document.getElementById('recordsBadge').textContent = `${s.unknown_total_records} unknown`;
    document.getElementById('kpiVolumeSub').textContent = recordUnits(s);
    document.getElementById('timeRangeSub').textContent = `${state.data.timeframe === 'ALL' ? 'All retained records' : state.data.timeframe + ' window'} · measured input tokens and cache reads`;
    document.getElementById('diagWindow').textContent = `${q.window_records || 0} records`;
    document.getElementById('diagTotal').textContent = `${q.settled_records_total || 0} records`;
    document.getElementById('diagOmitted').textContent = `${q.models_omitted || 0} rows / ${q.buckets_omitted || 0} buckets / ${q.markers_omitted || 0} code marks`;
    document.getElementById('diagSpan').textContent = q.oldest_ts && q.newest_ts ? `${localStamp(q.oldest_ts, true).slice(0, 10)} → ${localStamp(q.newest_ts, true).slice(0, 10)}` : 'No activity';
    document.getElementById('retainedCoverage').textContent = `${q.coverage || ''} Archives read: ${q.raw_stats?.archives_read || 0} · duplicate events skipped: ${q.raw_stats?.duplicate_events_skipped || 0} · sessions in this window without a usage event: ${q.sessions_without_usage_event || 0}.`;
    document.getElementById('chartLegend').textContent = legendText();

    // A poll keeps the selected point; renderChart re-inspects the nearest bin.
    renderTable();
    renderChart();
  }

  function renderTable() {
    const tbody = document.getElementById('modelsTbody');
    if (!tbody || !state.data || !state.data.models) return;

    const models = [...state.data.models];
    models.sort((a, b) => {
      let va = a[state.sortCol];
      let vb = b[state.sortCol];
      if (va === null) return vb === null ? 0 : 1;
      if (vb === null) return -1;
      if (typeof va === 'string') return state.sortAsc ? va.localeCompare(vb) : vb.localeCompare(va);
      return state.sortAsc ? (va - vb) : (vb - va);
    });

    if (models.length === 0) {
      tbody.innerHTML = '<tr><td colspan="6" style="text-align:center; color:var(--ces-slate);">No model activity in this timeframe</td></tr>';
      return;
    }

    tbody.innerHTML = models.map(model => `
      <tr>
        <td style="font-weight:600; color:var(--ces-text);">
          ${escapeHtml(model.model)}
          <span class="ces-route">${escapeHtml(model.provider)} · ${model.kind !== 'subscription_session' ? escapeHtml(model.kind) : model.model === 'no usage event' ? 'Session · tokens not in the event log' : 'Session aggregate · final reported model'}</span>
          ${model.rate === null ? '' : `<div class="ces-bar-wrap"><div class="ces-bar-fill" style="width:${model.rate}%"></div></div>`}
        </td>
        <td>${counted(model.total_records, model.kind === 'attempt' ? 'request' : model.kind === 'subscription_session' ? 'session' : 'record')}</td>
        <td>${formatTokens(model.prompt_tokens)}</td>
        <td style="color:var(--ces-cyan)">${formatTokens(model.cached_tokens)}</td>
        <td><b style="color:var(--ces-accent)">${formatRate(model.rate)}</b><span class="ces-route">${model.rate === null ? 'No comparable input' : `${formatTokens(model.eligible_cached_tokens)} / ${formatTokens(model.eligible_prompt_tokens)} measured`}</span></td>
        <td>${model.measured_records} / ${model.total_records}<span class="ces-route">${model.unknown_total_records} unknown input · ${model.unknown_read_records} unknown reads</span></td>
      </tr>
    `).join('');
  }

  // Event Listeners
  document.getElementById('tfGroup').addEventListener('click', (e) => {
    if (e.target.dataset.tf) {
      document.querySelectorAll('#tfGroup .ces-tf-btn').forEach(b => b.classList.remove('active'));
      e.target.classList.add('active');
      state.timeframe = e.target.dataset.tf;
      clearInspection();
      fetchData();
    }
  });

  document.getElementById('refreshBtn').addEventListener('click', () => {
    fetchData();
  });

  const retryBtn = document.getElementById('retryBtn');
  if (retryBtn) {
    retryBtn.addEventListener('click', () => {
      fetchData();
    });
  }

  document.querySelector('.ces-chart-mode-group').addEventListener('click', (e) => {
    if (e.target.dataset.mode) {
      document.querySelectorAll('.ces-chart-mode-btn').forEach(b => b.classList.remove('active'));
      e.target.classList.add('active');
      state.chartMode = e.target.dataset.mode;
      clearInspection();
      document.getElementById('chartTitle').textContent = state.chartMode === 'trend' ? 'Cache-Read Rate Trend (%)' : 'Token Volume Composition';
      document.getElementById('chartLegend').textContent = legendText();
      renderChart();
    }
  });

  document.querySelectorAll('table.ces-table th').forEach(th => {
    th.addEventListener('click', () => {
      const col = th.dataset.sort;
      if (state.sortCol === col) {
        state.sortAsc = !state.sortAsc;
      } else {
        state.sortCol = col;
        state.sortAsc = false;
      }
      renderTable();
    });
  });

  // Polling Lifecycle
  function startPolling() {
    stopPolling();
    if (state.autoRefreshSec > 0) {
      state.pollTimer = setInterval(fetchData, state.autoRefreshSec * 1000);
    }
  }

  function stopPolling() {
    if (state.pollTimer) {
      clearInterval(state.pollTimer);
      state.pollTimer = null;
    }
  }

  // Repaint only the existing canvas; controls, selection and scroll stay put.
  const offTheme = window.OuroborosWidget && typeof window.OuroborosWidget.onTheme === 'function'
    ? window.OuroborosWidget.onTheme(theme => {
    if (state.disposed) return;
    document.documentElement.dataset.theme = theme;
    readChartColors();
    renderChart();
  }) : null;

  // Widget Disposal Hook
  window.__ouroWidgetOnDispose(function () {
    state.disposed = true;
    if (typeof offTheme === 'function') offTheme();
    state.reqSequence += 1;
    stopPolling();
    window.removeEventListener('resize', resizeCanvas);
  });

  // Initial Load
  resizeCanvas();
  fetchData();
  startPolling();
})();
