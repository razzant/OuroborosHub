/* Claudexor Quotas widget — v0.9.0
 *
 * Runs as a reviewed module widget: a classic inline script inside an
 * opaque-origin sandboxed iframe whose window.fetch is a parent-mediated
 * bridge restricted to this skill's own extension route prefix.
 *
 * Display law: a value that was not read is labeled as not read.
 * Never 0, never "unlimited", never an empty cell standing in for a refused facet.
 *
 * One screen, one story per limit (0.8.0): the family's limits as rows of
 * per-account bars with the current figure and the next reported reset; one
 * timeline below them for the limit chosen there — the record behind now,
 * one conditional future ahead of it on the row's own basis, and the reset
 * schedule that future is made of; the account a bar or a row of the account
 * list selects; and that list itself. Every number is the skill's.
 *
 * 0.8.1 is presentation only: every row has one bar-track height and one
 * control slot at its right edge (the chart toggle, the timeline's only one);
 * the unit is said by the figure and the title, the future's assumption by
 * the legend, each once. No number, request or route changed.
 */
(function () {
    'use strict';

    var ROUTE = '/api/extensions/claudexor_quotas/quotas';
    var REFRESH_ROUTE = '/api/extensions/claudexor_quotas/refresh';
    var REFRESH_MS = 30000;
    // How long one request may take before the widget stops waiting for it.
    // Each bound sits above the skill's own bound on that route — its status
    // read gives up after 25 s, its live refresh after 180 s — with room for
    // the history read and the bridge, so the skill's own answer (a failure
    // included) normally arrives first. The bridge enforces the bound itself
    // (init.timeoutMs); BACKSTOP_MS later the widget stops waiting on its own,
    // for a host whose bridge does not.
    var GET_TIMEOUT_MS = 60000;
    var REFRESH_TIMEOUT_MS = 210000;
    var BACKSTOP_MS = 5000;
    var FACET_ORDER = ['catalog', 'accounts', 'quota'];
    var TONE_WORD = { ok: 'ready', warn: 'needs a look', bad: 'alert', muted: 'no live reading' };
    // How far the timeline looks back and ahead. Not a saved choice: each
    // mount opens on the limit's own span — a week for a limit longer than a
    // day, else a day — until the reader picks one.
    var HORIZONS = ['24h', '7d'];
    // The two futures the timeline can draw, both of the accounts read now.
    var SCENARIOS = [
        { key: 'no_new_use', name: 'No new use' },
        { key: 'recent_pace', name: 'Recent pace' }
    ];

    var root = document.getElementById('root');
    var generation = 0;
    var dataTimer = null;
    var lastGood = null;
    var lastGoodAt = 0;
    var stopped = false;
    var disposed = false;
    var themeOff = null;
    var inFlight = false;
    // The family on screen, and the account the reader selected in it — none
    // until a bar or a row of the account list is clicked. Both survive the
    // 30-second redraw and fall away when what they point at is gone.
    var selectedHarness = '';
    var selectedAccountKey = '';
    // The bar or account-list row that selected it (its data-focus key):
    // where Clear and Escape give the keyboard back (clearAccount).
    var selectionOpener = '';
    var accountsOpen = false;
    var diagnosticsOpen = false;
    var aboutOpen = false;
    var currentView = null;
    var staleMessage = '';
    var actionMessage = '';
    // The timeline: open on every mount. Its limit per family is the one the
    // reader picked, else the family's "lowest left"; the horizon is the
    // reader's pick ('' = the limit's own); the scenario starts at "no new
    // use". The last chart request asked on the reader's behalf is kept so a
    // mismatch is asked about once, never in a loop.
    var timelineOpen = true;
    var chartKeys = {};
    var chartAsked = '';
    var horizonChoice = '';
    var scenario = 'no_new_use';
    var detailsOpen = false;
    var scheduleAll = false;
    // The page of the sightings table under Details, counted back from the
    // newest, and the chart (limit and span) that page is of.
    var sightingsBack = 0;
    var sightingsOf = '';
    var chartCursor = null;
    // Set while render() throws the old tree away. Chromium fires blur on a
    // focused node as it is removed; that blur is the redraw's, not the
    // reader's, and must not take the chart's keyboard cursor with it.
    var rebuilding = false;
    // The control the keyboard was on when a redraw drew it disabled — Refresh
    // while its own request is in the air. A disabled button cannot hold focus,
    // so the browser drops it to the body; the key waits here for the redraw
    // that enables the control again, unless the reader has moved on.
    var focusParked = '';
    // Set when the last answer could not be drawn: the screen before it stays
    // up, with this said above it and a Retry.
    var drawFault = '';

    var STYLE_ID = 'claudexor-quotas-style';

    // One neutral surface and one type scale for the whole card.
    var STYLE = [
        ":root{",
        "color-scheme:dark;",
        "--bg-canvas:#171719;--surface:#202023;--inset:#29292d;--popup:#242427;",
        "--text-primary:#e4e4e7;--text-meta:#b0b0b8;--text-secondary:#a0a0aa;--text-disabled:#787881;",
        "--edge:#38383e;--edge-strong:#62626c;--neutral-rgb:228,228,231;",
        "--status-ok:#82bd9c;--status-warn:#dfb574;--status-warn-bg:#3b3224;",
        "--status-warn-border:#665238;--status-bad:#e99a9f;--status-bad-bg:#40292e;--status-bad-border:#72444b;",
        "--warn-text:var(--status-warn);--bad-text:var(--status-bad);",
        "--focus-accent-border:#ec7782;--share-ink:#a6a6b3;--scenario-ink:#9ab9fa;",
        "--share-track:rgba(var(--neutral-rgb),.07);--area:rgba(var(--neutral-rgb),.10);",
        "--type-meta:12px;--type-body:14px;--type-section:16px;--row-h:32px;--ctl-w:96px;",
        "--space-1:4px;--space-2:8px;--space-3:12px;--space-4:16px;--space-5:24px;",
        "--radius-sm:6px;--radius-md:8px;",
        // The hatch of a last-known value: a 6px tile drawn once per theme, a
        // mark of state, not decoration.
        "--hatch:url(data:image/svg+xml;base64,PHN2ZyB4bWxucz0naHR0cDovL3d3dy53My5vcmcvMjAwMC9zdmcnIHdpZHRoPSc2JyBoZWlnaHQ9JzYnPjxwYXRoIGQ9J00tMSAxbDItMk0wIDZsNi02TTUgN2wyLTInIHN0cm9rZT0nI2E2YTZiMycgc3Ryb2tlLXdpZHRoPScxLjUnLz48L3N2Zz4=);",
        "--hatch-warn:url(data:image/svg+xml;base64,PHN2ZyB4bWxucz0naHR0cDovL3d3dy53My5vcmcvMjAwMC9zdmcnIHdpZHRoPSc2JyBoZWlnaHQ9JzYnPjxwYXRoIGQ9J00tMSAxbDItMk0wIDZsNi02TTUgN2wyLTInIHN0cm9rZT0nI2RmYjU3NCcgc3Ryb2tlLXdpZHRoPScxLjUnLz48L3N2Zz4=)}",
        ":root[data-theme=light]{color-scheme:light;--bg-canvas:#fafafa;--surface:#fff;--inset:#f3f3f5;--popup:#fff;",
        "--text-primary:#26262b;--text-meta:#60606b;--text-secondary:#686873;--text-disabled:#90909a;",
        "--edge:#e2e2e7;--edge-strong:#91919d;--neutral-rgb:38,38,43;",
        "--status-ok:#38704f;--status-warn:#845713;--status-warn-bg:#fbf5e9;",
        "--status-warn-border:#e4c78d;--status-bad:#a73c49;--status-bad-bg:#fcf0f1;--status-bad-border:#e8b3ba;",
        "--focus-accent-border:#b62f42;--share-ink:#787884;--scenario-ink:#365eac;",
        "--hatch:url(data:image/svg+xml;base64,PHN2ZyB4bWxucz0naHR0cDovL3d3dy53My5vcmcvMjAwMC9zdmcnIHdpZHRoPSc2JyBoZWlnaHQ9JzYnPjxwYXRoIGQ9J00tMSAxbDItMk0wIDZsNi02TTUgN2wyLTInIHN0cm9rZT0nIzc4Nzg4NCcgc3Ryb2tlLXdpZHRoPScxLjUnLz48L3N2Zz4=);",
        "--hatch-warn:url(data:image/svg+xml;base64,PHN2ZyB4bWxucz0naHR0cDovL3d3dy53My5vcmcvMjAwMC9zdmcnIHdpZHRoPSc2JyBoZWlnaHQ9JzYnPjxwYXRoIGQ9J00tMSAxbDItMk0wIDZsNi02TTUgN2wyLTInIHN0cm9rZT0nIzg0NTcxMycgc3Ryb2tlLXdpZHRoPScxLjUnLz48L3N2Zz4=)}",
        "*{box-sizing:border-box}",
        "body{margin:0;padding:var(--space-4);font:var(--type-body)/1.5 -apple-system,BlinkMacSystemFont,\"Segoe UI\",Roboto,sans-serif;color:var(--text-primary);background:var(--bg-canvas);-webkit-font-smoothing:antialiased}",
        "#root{display:flex;flex-direction:column;gap:var(--space-4);min-width:0}",
        "button{font:inherit;color:var(--text-primary);cursor:pointer}",
        "button:disabled{color:var(--text-disabled);cursor:default}",
        "button:focus-visible,summary:focus-visible,.chart-plot:focus-visible{outline:2px solid var(--focus-accent-border);outline-offset:2px}",
        "button svg{flex:none}",
        ".sr-only{position:absolute;width:1px;height:1px;padding:0;margin:-1px;overflow:hidden;clip:rect(0 0 0 0);white-space:nowrap;border:0}",
        // The control bar: families on the left, About and Refresh on the right.
        ".control-bar{display:flex;align-items:flex-start;justify-content:space-between;flex-wrap:wrap;gap:var(--space-2)}",
        ".harness-seg,.action-seg{display:flex;align-items:center;flex-wrap:wrap;gap:var(--space-1)}",
        ".harness-seg{flex:1 1 280px}",
        ".action-seg{margin-left:auto}",
        ".harness-btn,.action-btn,.pill-btn,.seg-opt{display:inline-flex;align-items:center;justify-content:center;gap:6px;min-height:var(--row-h);padding:4px 10px;border:1px solid transparent;border-radius:var(--radius-sm);background:transparent;color:var(--text-primary);font-size:var(--type-meta);line-height:1.35;position:relative}",
        ".harness-btn{padding-inline:6px;gap:5px}",
        ".harness-btn:hover,.action-btn:hover:not(:disabled),.pill-btn:hover,.seg-opt:hover{background:var(--inset)}",
        ".harness-btn.active,.action-btn.is-open,.pill-btn.on,.seg-opt.active{background:var(--inset);border-color:var(--edge);font-weight:600}",
        ".action-btn,.pill-btn{border-color:var(--edge);background:var(--surface)}",
        ".action-refresh{font-weight:600}",
        ".harness-btn.loading{color:var(--text-disabled)}",
        ".harness-initial{display:inline-flex;align-items:center;justify-content:center;width:16px;height:16px;font-size:var(--type-meta);font-weight:600}",
        ".harness-count{color:var(--text-meta);font-weight:400;font-variant-numeric:tabular-nums}",
        ".pip,.state-dot{display:inline-block;flex:none;width:6px;height:6px;border-radius:50%;background:var(--text-disabled)}",
        ".pip.ok,.state-dot.ok{background:var(--status-ok)}",
        ".pip.warn,.state-dot.warn{background:var(--status-warn)}",
        ".pip.bad,.state-dot.bad{background:var(--status-bad)}",
        ".pip.muted,.state-dot.muted{background:var(--text-disabled)}",
        ".action-btn.has-problem{color:var(--status-bad);border-color:var(--status-bad-border)}",
        ".dot-label{display:inline-flex;align-items:center;gap:6px;font-size:var(--type-meta);color:var(--text-meta)}",
        ".dot-label.strong{color:var(--text-primary)}",
        ".icon-spin{display:inline-flex}",
        "@keyframes spin{to{transform:rotate(360deg)}}",
        ".action-btn.is-refreshing .icon-spin{animation:spin 1s linear infinite}",
        "@media(prefers-reduced-motion:reduce){.action-btn.is-refreshing .icon-spin{animation:none}}",
        // Banners: what failed, in the open, above everything it touches.
        ".banners{display:flex;flex-direction:column;gap:var(--space-2)}",
        ".banners:empty{display:none}",
        ".banner{display:flex;align-items:flex-start;gap:var(--space-2);padding:var(--space-3);border:1px solid var(--status-warn-border);border-radius:var(--radius-md);background:var(--status-warn-bg);color:var(--warn-text);font-size:var(--type-meta);overflow-wrap:anywhere}",
        ".banner.bad{background:var(--status-bad-bg);border-color:var(--status-bad-border);color:var(--bad-text)}",
        ".banner-icon{display:flex;flex:none;margin-top:2px}",
        ".banner-text{flex:1 1 auto;min-width:0}",
        ".banner .banner-retry{margin:-4px 0 -4px auto;min-height:26px;padding:2px 10px;color:inherit;border-color:currentColor;background:transparent;white-space:nowrap;flex:0 0 auto}",
        // The family's limits.
        ".reserve{min-width:0}",
        ".reserve-head{display:flex;align-items:baseline;justify-content:space-between;gap:var(--space-2) var(--space-3);flex-wrap:wrap;margin-bottom:var(--space-1)}",
        ".reserve-title,.acc-title{margin:0;font-size:var(--type-section);font-weight:600}",
        ".reserve-family{font-weight:400;color:var(--text-meta)}",
        ".reserve-age,.reserve-note{font-size:var(--type-meta);color:var(--text-meta)}",
        ".reserve-age{display:inline-flex;align-items:baseline;gap:6px;text-align:right;font-variant-numeric:tabular-nums}",
        ".reserve-age .pip{position:relative;top:-1px}",
        ".reserve-age.status-warn{color:var(--warn-text)}",
        ".reserve-age.status-bad{color:var(--bad-text)}",
        ".reserve-note{margin:var(--space-2) 0}",
        ".reset-credits{font-size:var(--type-meta);color:var(--text-meta);margin:var(--space-2) 0;overflow-wrap:anywhere}",
        ".reset-credits strong{color:var(--text-primary);font-weight:500}",
        ".lrows{margin-top:var(--space-2);border-top:1px solid var(--edge)}",
        // Every row is the same shape: name and figure, bars and their caption,
        // the tail — and one control slot of one width at the right edge, so
        // the chart toggles of all rows stand in one column.
        ".lrow{display:grid;grid-template-columns:minmax(0,1fr) auto var(--ctl-w);grid-template-areas:\"name fig ctl\" \"strip sub ctl\" \"tail tail tail\";column-gap:var(--space-3);row-gap:6px;padding:var(--space-3) var(--space-2);border-bottom:1px solid var(--edge)}",
        // The charted row and its timeline are one shaded block: no rule between them.
        ".lrow.sel{background:var(--inset);border-bottom-color:transparent}",
        ".l-name{grid-area:name;justify-self:start;align-self:start;display:inline-flex;align-items:baseline;flex-wrap:wrap;gap:2px 8px;min-width:0;max-width:100%;overflow-wrap:anywhere;font-size:var(--type-body);font-weight:500;line-height:1.35}",
        ".lrow.sel .l-name{font-weight:600}",
        // The row's one control: its chart, shown or hidden. The same width
        // in every row, whatever it says.
        ".l-chart{grid-area:ctl;align-self:center;justify-self:stretch;display:inline-flex;align-items:center;justify-content:center;gap:5px;min-height:26px;padding:2px 8px;border:1px solid var(--edge);border-radius:999px;background:var(--surface);color:var(--text-meta);font-size:var(--type-meta);font-weight:400;line-height:1.35;white-space:nowrap}",
        ".l-chart:hover{background:var(--inset);color:var(--text-primary)}",
        ".l-chart.on{background:var(--inset);border-color:var(--edge-strong);color:var(--text-primary);font-weight:600}",
        ".rs-tight{font-size:var(--type-meta);color:var(--text-meta);font-weight:400;white-space:nowrap}",
        ".l-fig{grid-area:fig;justify-self:end;white-space:nowrap;line-height:1.35}",
        ".l-fig b{font-size:var(--type-section);font-weight:600;font-variant-numeric:tabular-nums}",
        ".l-fig .of{color:var(--text-meta);font-size:var(--type-meta);font-variant-numeric:tabular-nums}",
        ".l-sub{grid-area:sub;align-self:end;justify-self:end;text-align:right;color:var(--text-meta);font-size:var(--type-meta);white-space:nowrap;font-variant-numeric:tabular-nums}",
        ".l-sub.warn{color:var(--warn-text)}",
        ".l-sub .sw{display:inline-block;width:9px;height:9px;margin-right:5px;vertical-align:-1px;border-radius:1px;background:rgba(var(--neutral-rgb),.08) var(--hatch)}",
        ".l-tail{grid-area:tail;min-width:0;color:var(--text-meta);font-size:var(--type-meta);overflow-wrap:anywhere;font-variant-numeric:tabular-nums}",
        ".rs-bad{color:var(--bad-text)}",
        ".rs-warn{color:var(--warn-text)}",
        // One bar per account, as tall as its share left on one 0–100% scale.
        // One track height in every row, charted or not: the same share is
        // the same height wherever it stands.
        ".bars{grid-area:strip;justify-self:start;display:flex;align-items:flex-end;height:40px;max-width:100%;border-bottom:1px solid var(--edge-strong)}",
        ".bar{position:relative;display:block;flex:1 1 0;min-width:4px;height:100%;min-height:0;padding:0;border:0;border-radius:3px 3px 0 0;background:var(--share-track)}",
        ".bar>.fill{position:absolute;left:0;right:0;bottom:0;border-radius:2px 2px 0 0;background:var(--share-ink)}",
        ".bar:hover:not(:disabled)>.fill{filter:brightness(.82)}",
        ".bar.last>.fill{background:rgba(var(--neutral-rgb),.07) var(--hatch);box-shadow:inset 0 1.5px 0 var(--share-ink)}",
        ".bar.held>.fill{background:var(--status-warn)}",
        ".bar.held.last>.fill{background:transparent var(--hatch-warn);box-shadow:inset 0 1.5px 0 var(--status-warn)}",
        ".bar.spent{box-shadow:inset 0 -3px 0 var(--status-bad)}",
        ".bar.unknown{background:transparent;box-shadow:inset 0 0 0 1px rgba(var(--neutral-rgb),.32)}",
        ".bar.unknown:after{content:\"?\";position:absolute;left:0;right:0;bottom:4px;text-align:center;color:var(--text-meta);font-size:var(--type-meta);line-height:1}",
        ".bar.sel{outline:2px solid var(--text-primary);outline-offset:1px;z-index:1}",
        ".bar:disabled{cursor:default}",
        ".strip-legend{display:flex;flex-wrap:wrap;gap:var(--space-2) var(--space-3);font-size:var(--type-meta);color:var(--text-meta);margin-top:var(--space-2)}",
        ".strip-legend .bar{display:inline-block;width:12px;height:16px;flex:none}",
        // The timeline: one limit, the record behind now and one future ahead.
        ".inspector,.accounts,.about-panel{min-width:0;padding:var(--space-3) var(--space-4);border:1px solid var(--edge);border-radius:var(--radius-md);background:var(--surface)}",
        ".tl-block{padding:var(--space-1) var(--space-2) var(--space-3);border-bottom:1px solid var(--edge);background:var(--inset)}",
        ".cover-link{margin-top:var(--space-2);padding:2px 0;border:0;background:none;font-size:var(--type-meta);color:var(--text-meta);text-decoration:underline;text-decoration-color:var(--edge-strong);text-underline-offset:3px}",
        ".acc-cover{margin-top:var(--space-2);font-size:var(--type-meta);color:var(--text-meta)}",
        ".insp-head,.acc-head{display:flex;align-items:center;justify-content:space-between;gap:var(--space-2);flex-wrap:wrap}",
        ".tl-controls{display:flex;align-items:center;flex-wrap:wrap;gap:var(--space-2) var(--space-3)}",
        ".seg{display:inline-flex;align-items:center;flex-wrap:wrap;gap:2px}",
        ".seg-label{font-size:var(--type-meta);color:var(--text-meta);margin-right:var(--space-1)}",
        ".seg-opt{border-color:var(--edge)}",
        ".tl-scope{margin-top:var(--space-2);font-size:var(--type-meta);color:var(--text-meta);overflow-wrap:anywhere}",
        ".chart-plot{position:relative;margin-top:var(--space-3);border-radius:var(--radius-sm)}",
        ".chart-svg{display:block;max-width:100%;height:auto;overflow:visible;touch-action:none}",
        ".chart-svg .grid,.chart-svg .tick,.chart-svg .day-rule{stroke:var(--edge);stroke-width:1}",
        ".chart-svg .grid.base,.chart-svg .grid.cap{stroke:var(--edge-strong)}",
        ".chart-svg .reset-line{stroke:var(--scenario-ink);stroke-width:1;stroke-dasharray:1 3;opacity:.7}",
        ".chart-svg .axis-text{fill:var(--text-meta);font-size:var(--type-meta);font-family:inherit;font-variant-numeric:tabular-nums}",
        ".chart-svg .axis-text.now,.chart-svg .axis-text.day{fill:var(--text-primary);font-weight:500}",
        ".chart-svg .future-bg{fill:rgba(var(--neutral-rgb),.035)}",
        ".chart-svg .now-line,.chart-svg .cursor-line{stroke:var(--text-meta);stroke-width:1}",
        ".chart-svg .hit{fill:transparent;cursor:crosshair}",
        ".area-observed{fill:var(--area)}",
        ".line-observed{fill:none;stroke:var(--text-primary);stroke-width:2;stroke-linejoin:round;stroke-linecap:round}",
        ".line-carried{fill:none;stroke:var(--text-primary);stroke-width:2;stroke-dasharray:3 4;stroke-linejoin:round;stroke-linecap:round}",
        ".line-scenario{fill:none;stroke:var(--scenario-ink);stroke-width:2;stroke-dasharray:6 4;stroke-linejoin:round;stroke-linecap:round}",
        ".scenario-start{fill:var(--scenario-ink);stroke:var(--surface);stroke-width:1.5}",
        ".chart-svg .cursor-dot{stroke:var(--surface);stroke-width:2;stroke-dasharray:none}",
        ".chart-svg .cursor-dot.line-observed{fill:var(--text-primary)}",
        ".chart-svg .cursor-dot.line-scenario{fill:var(--scenario-ink)}",
        ".chart-tip{position:absolute;z-index:5;pointer-events:none;max-width:100%;padding:var(--space-2);border-radius:var(--radius-sm);background:var(--popup);border:1px solid var(--edge-strong);font-size:var(--type-meta);color:var(--text-meta);overflow-wrap:anywhere}",
        ".tip-when,.tip-val{color:var(--text-primary);font-weight:500}",
        ".tip-row{display:flex;align-items:center;gap:6px;font-variant-numeric:tabular-nums}",
        ".tip-row svg{flex:none}",
        ".tip-foot{margin-top:var(--space-1)}",
        // Under the chart, one line per line drawn: whose total it is and, for
        // the future, what it assumes — the legend is the only place that says so.
        ".chart-legend{display:flex;flex-direction:column;gap:var(--space-1);margin-top:var(--space-2);font-size:var(--type-meta);color:var(--text-meta)}",
        ".legend-item{display:flex;align-items:flex-start;gap:6px;overflow-wrap:anywhere}",
        ".legend-item svg{flex:none;margin-top:4px}",
        ".tl-facts{display:flex;flex-direction:column;gap:var(--space-1);margin-top:var(--space-1);font-size:var(--type-meta);color:var(--text-meta)}",
        ".tl-facts:empty{display:none}",
        ".tl-facts .warn{color:var(--warn-text)}",
        ".schedule{width:100%;margin-top:var(--space-3);border-collapse:collapse;font-size:var(--type-meta);font-variant-numeric:tabular-nums}",
        ".schedule caption{text-align:left;padding-bottom:var(--space-1);font-size:var(--type-body);font-weight:500;color:var(--text-primary)}",
        ".schedule th,.schedule td{padding:5px var(--space-2) 5px 0;border-top:1px solid var(--edge);text-align:left;color:var(--text-meta);vertical-align:top}",
        ".schedule th{font-weight:500;color:var(--text-primary)}",
        ".schedule td.n{text-align:right;color:var(--text-primary)}",
        ".schedule .schedule-more{min-height:26px;margin:2px 0;padding:2px 10px}",
        ".schedule th.n{text-align:right}",
        ".schedule-notes{display:flex;flex-direction:column;gap:2px;margin-top:var(--space-2);font-size:var(--type-meta);color:var(--text-meta)}",
        ".checkpoints{margin-top:var(--space-2);font-size:var(--type-body);color:var(--text-meta);font-variant-numeric:tabular-nums}",
        ".checkpoints b{color:var(--text-primary);font-weight:500}",
        ".chart-table{margin-top:var(--space-3);overflow-x:auto}",
        ".chart-table summary{cursor:pointer;font-size:var(--type-meta);color:var(--text-primary);min-height:var(--row-h);padding:6px 0}",
        ".chart-table table{border-collapse:collapse;width:100%;font-size:var(--type-meta);font-variant-numeric:tabular-nums}",
        ".chart-table th,.chart-table td{text-align:left;padding:6px var(--space-2) 6px 0;border-top:1px solid var(--edge);color:var(--text-meta);vertical-align:top}",
        ".chart-table th{font-weight:500;color:var(--text-primary)}",
        ".chart-table caption{text-align:left;padding:var(--space-3) 0 var(--space-1);font-weight:500;color:var(--text-primary)}",
        ".sightings-nav{display:flex;align-items:center;gap:var(--space-2);margin-top:var(--space-2)}",
        // A page button at its end stays focusable, so the keyboard is not dropped.
        ".pill-btn[aria-disabled=true]{color:var(--text-disabled);background:var(--surface);cursor:default}",
        ".chart-notes p{margin:0 0 var(--space-2);font-size:var(--type-meta);color:var(--text-meta)}",
        ".reserve-detail{display:grid;grid-template-columns:max-content minmax(0,1fr);gap:6px var(--space-4);margin:0 0 var(--space-3);padding:var(--space-3);background:var(--inset);border-radius:var(--radius-sm);font-size:var(--type-meta);color:var(--text-primary);font-variant-numeric:tabular-nums}",
        ".reserve-detail dt{color:var(--text-meta)}",
        ".reserve-detail dd{margin:0;min-width:0;overflow-wrap:anywhere}",
        // The selected account.
        ".insp-title{display:flex;align-items:center;flex-wrap:wrap;gap:var(--space-1) var(--space-2);min-width:0;font-size:var(--type-body);font-weight:600}",
        ".insp-name{min-width:0;overflow-wrap:anywhere}",
        ".plan-chip,.next-up{font-size:var(--type-meta);font-weight:400;color:var(--text-meta)}",
        ".insp-meta{display:flex;align-items:center;gap:var(--space-1) var(--space-2);flex-wrap:wrap;margin-top:var(--space-1);font-size:var(--type-meta);color:var(--text-meta);overflow-wrap:anywhere}",
        ".insp-meta span+span:before{content:\"\\00b7\";margin-right:var(--space-2)}",
        ".meta-bad{color:var(--bad-text)}",
        ".quota-primary-row{display:flex;align-items:center;gap:var(--space-2);flex-wrap:wrap;margin-top:var(--space-2);font-size:var(--type-meta)}",
        ".quota-primary-text{color:var(--text-meta)}",
        ".quota-primary-text.exhausted{color:var(--bad-text)}",
        ".quota-primary-text.cooling{color:var(--warn-text)}",
        ".quota-when{display:inline-flex;align-items:center;flex-wrap:wrap;gap:4px;color:var(--text-meta)}",
        ".quota-cooldown,.quota-exhaustion,.quota-unavailable,.acct-note{display:flex;align-items:flex-start;gap:6px;margin-top:var(--space-2);font-size:var(--type-meta);color:var(--warn-text)}",
        ".quota-cooldown>svg,.quota-exhaustion>svg,.quota-unavailable>svg,.acct-note>svg{flex:none;margin-top:3px}",
        ".quota-exhaustion.past,.acct-note.muted{color:var(--text-meta)}",
        ".quota-cooldown-body{min-width:0;overflow-wrap:anywhere}",
        // An inline-flex part drops the space its own text starts with, and
        // "Fable · until" read "Fable· until": the gap is given back here.
        ".quota-cooldown-body>.quota-when{margin-left:.3em}",
        ".quota-action{color:var(--warn-text)}",
        ".quota-unavailable{flex-wrap:wrap}",
        ".ticker{font-variant-numeric:tabular-nums}",
        ".ticker.bad{color:var(--bad-text)}",
        ".ticker.warn{color:var(--warn-text)}",
        ".rel-time{color:var(--text-meta)}",
        ".acct-lims{margin-top:var(--space-2)}",
        ".acct-lim{display:grid;grid-template-columns:minmax(88px,150px) 64px 44px minmax(0,1fr);align-items:center;column-gap:var(--space-3);margin-top:6px;font-size:var(--type-meta)}",
        ".acct-lim .k{color:var(--text-meta);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}",
        ".acct-lim .m{position:relative;height:5px;border-radius:3px;background:var(--share-track);overflow:hidden}",
        ".acct-lim .m>i{position:absolute;left:0;top:0;bottom:0;background:var(--share-ink)}",
        ".acct-lim .m.last>i{background:rgba(var(--neutral-rgb),.07) var(--hatch)}",
        ".acct-lim .m.held>i{background:var(--status-warn)}",
        ".acct-lim .m.none{background:transparent}",
        ".acct-lim .p{text-align:right;font-variant-numeric:tabular-nums;color:var(--text-primary)}",
        ".acct-lim .p.bad{color:var(--bad-text);font-weight:600}",
        ".acct-lim .p.meta{color:var(--text-meta)}",
        ".acct-lim .r{min-width:0;color:var(--text-meta);overflow-wrap:anywhere;font-variant-numeric:tabular-nums}",
        ".acct-lim .r .warn{color:var(--warn-text)}",
        ".acct-lim .r .est{color:var(--scenario-ink)}",
        ".insp-diag{margin-top:var(--space-3)}",
        ".insp-diag-body{margin-top:var(--space-2)}",
        ".diag-block{margin-top:var(--space-3)}",
        ".diag-title{font-size:var(--type-meta);color:var(--warn-text)}",
        ".diag-title.muted{color:var(--text-meta)}",
        ".win-lines{display:flex;flex-direction:column;gap:4px;margin-top:var(--space-1)}",
        ".win-line{display:flex;align-items:center;flex-wrap:wrap;gap:4px var(--space-2);font-size:var(--type-meta);color:var(--text-meta);font-variant-numeric:tabular-nums}",
        ".win-tag{min-width:64px;color:var(--text-primary)}",
        ".win-tag.bad,.win-used.bad,.win-word.bad{color:var(--bad-text)}",
        ".win-tag.warn,.win-word.warn{color:var(--warn-text)}",
        ".win-line.stale .win-tag{color:var(--text-meta)}",
        ".acct-cap{font-size:var(--type-meta);color:var(--text-meta);background:var(--inset);padding:0 4px;border-radius:3px;max-width:100%;overflow-wrap:anywhere}",
        ".acct-cap.exhausted{color:var(--bad-text);background:var(--status-bad-bg)}",
        ".acct-cap.held{color:var(--warn-text);background:var(--status-warn-bg)}",
        ".acct-pool{max-width:180px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;direction:rtl;text-align:left}",
        // One bar language for a single window: as long as the share left.
        ".meter{position:relative;display:inline-block;width:36px;height:4px;flex:none;box-shadow:0 1px 0 var(--edge-strong)}",
        ".meter-fill{position:absolute;left:0;top:0;bottom:0;background:var(--share-ink);border-radius:0 1px 1px 0}",
        ".meter.restricted{box-shadow:0 1px 0 var(--status-warn)}",
        ".meter.restricted .meter-fill{background:var(--status-warn)}",
        ".meter.spent{box-shadow:0 2px 0 var(--status-bad)}",
        ".meter.stale .meter-fill{background:rgba(var(--neutral-rgb),.24)}",
        // The account list: every account, its share left in every limit.
        ".acc-table{margin-top:var(--space-2);font-size:var(--type-meta)}",
        ".acc-headrow,.acc-row{display:grid;align-items:center;column-gap:var(--space-2);min-height:var(--row-h)}",
        ".acc-headrow{color:var(--text-meta);border-bottom:1px solid var(--edge)}",
        ".acc-headrow span{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}",
        ".acc-row{width:100%;padding:2px 0;border:0;border-bottom:1px solid var(--edge);border-radius:0;background:transparent;text-align:left;font-size:var(--type-meta)}",
        ".acc-row:hover,.acc-row.active{background:var(--inset)}",
        ".acc-name{display:flex;align-items:center;gap:6px;min-width:0;font-size:var(--type-body)}",
        ".acc-name-text{min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}",
        ".acc-cell{text-align:right;font-variant-numeric:tabular-nums;color:var(--text-primary)}",
        ".acc-cell.last{color:var(--text-meta);text-decoration:underline dotted;text-underline-offset:3px}",
        ".acc-cell.bad{color:var(--bad-text)}",
        ".acc-cell.held{color:var(--warn-text)}",
        ".acc-cell.none,.acc-cell.unknown{color:var(--text-meta)}",
        ".acc-state{color:var(--text-meta);overflow-wrap:anywhere}",
        ".acc-state.bad{color:var(--bad-text)}",
        ".acc-state.warn{color:var(--warn-text)}",
        ".acc-sec{margin-top:var(--space-3);font-size:var(--type-meta);color:var(--text-meta)}",
        // About: what the screen is made of, and the state of the machine.
        ".about-items{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:var(--space-3) var(--space-5);margin-top:var(--space-3);font-size:var(--type-meta);color:var(--text-meta)}",
        ".about-title{font-size:var(--type-body);font-weight:500;color:var(--text-primary);margin-bottom:var(--space-1)}",
        ".about-items p{margin:0}",
        ".system-state{display:flex;flex-wrap:wrap;gap:var(--space-2) var(--space-4);margin-top:var(--space-2)}",
        ".empty-card{padding:var(--space-5);background:var(--surface);border:1px solid var(--edge);border-radius:var(--radius-md);text-align:center;display:flex;align-items:center;flex-direction:column;gap:var(--space-2)}",
        ".empty-icon{color:var(--text-meta)}",
        ".empty-title{font-size:var(--type-section);font-weight:500;margin:0}",
        ".empty-desc{font-size:var(--type-meta);color:var(--text-meta);max-width:360px;margin:0}",
        ".empty-notes{display:flex;gap:var(--space-3);flex-wrap:wrap}",
        // Give dense strips a full row before the figure and fixed control
        // column leave less room than their individual bars need.
        "@media(max-width:640px){.lrow{grid-template-areas:\"name fig ctl\" \"sub sub ctl\" \"strip strip strip\" \"tail tail tail\";}.l-sub{justify-self:start;text-align:left;white-space:normal}.bars{justify-self:stretch;width:auto!important}}",
        "@media(max-width:520px){body{padding:var(--space-3)}.tl-block{padding-inline:4px}.lrow{column-gap:var(--space-2);padding:10px 4px}.acct-lim{grid-template-columns:72px 40px 40px minmax(0,1fr);column-gap:var(--space-2)}.reserve-head{display:block}.reserve-age{text-align:left;margin-top:2px}.inspector,.accounts,.about-panel{padding:var(--space-3)}.acc-headrow{display:none}.acc-row{display:flex!important;flex-wrap:wrap;gap:2px var(--space-2);padding:6px 0}.acc-name{flex:1 1 100%}.acc-cell{text-align:left}.acc-cell:before{content:attr(data-limit) \" \";color:var(--text-meta)}.reserve-detail{grid-template-columns:minmax(0,1fr);gap:2px}.reserve-detail dd{margin-bottom:var(--space-2)}}",
        // Narrower still, the figure and its caption each take a line of their
        // own under the name (sharing one line left them overlapping at a
        // 343 px card); the control spans the name and the figure.
        "@media(max-width:360px){.lrow{grid-template-areas:\"name name ctl\" \"fig fig ctl\" \"sub sub sub\" \"strip strip strip\" \"tail tail tail\"}.l-fig{justify-self:start;white-space:normal}}"
    ].join('');

    function el(tag, cls, text) {
        var node = document.createElement(tag);
        if (cls) node.className = cls;
        if (text !== undefined && text !== null && text !== '') node.textContent = String(text);
        return node;
    }

    // Icons are drawn, not typed: an emoji renders in the host OS font and looks
    // different on every machine, and the widget frame forbids loading an icon
    // font. These are stroke paths that inherit the surrounding text colour.
    var ICON_PATHS = {
        info: ['M12 21a9 9 0 100-18 9 9 0 000 18z', 'M12 11v5', 'M12 8h.01'],
        warn: ['M10.3 4.3L2.6 17.5A2 2 0 004.3 20.5h15.4a2 2 0 001.7-3L13.7 4.3a2 2 0 00-3.4 0z', 'M12 9v4', 'M12 17h.01'],
        error: ['M12 21a9 9 0 100-18 9 9 0 000 18z', 'M15 9l-6 6', 'M9 9l6 6'],
        refresh: ['M20.5 12a8.5 8.5 0 11-2.6-6.1', 'M20.5 4.5V10h-5.5'],
        caret: ['M6 9l6 6 6-6'],
        close: ['M6 6l12 12', 'M18 6L6 18'],
        // A line over time: the timeline's own toggle.
        chart: ['M4 19h16', 'M5 15l4-5 4 3 6-7']
    };

    // Family marks are the vendors' own: a widget that names an account's CLI
    // and then draws a shape of its own invention makes the reader guess. These
    // are filled marks, not stroked outlines, so they take their own renderer.
    // Vendor marks, monochrome, each in the grid its owner drew it on. Codex,
    // Claude, Cursor and OpenCode are copied byte-for-byte from the host's own
    // list of harness marks (web/modules/harness_presentation.js), which in turn
    // carries them from Claudexor's HarnessLogoData.swift: Claude, Cursor and
    // OpenCode from Simple Icons, Codex from SVGL. Antigravity is the silhouette
    // of the SVGL mark, the same source Codex came from.
    //
    // The paths are untouched; only the frame around each one is computed, from
    // the shape itself rather than from the grid it was published on, because
    // not every mark sits in the middle of the grid it was published on —
    // Antigravity rides high in its 16-by-15 — and at this size that is most of
    // a pixel, which is the crookedness the row was pulled up on. The frames
    // account for the arcs, not only the points the path names: the Codex mark
    // bulges past its own listed points at the top, and a frame drawn to those
    // points clipped its crown. Product names and marks remain the property of
    // their owners.
    var BRAND_MARKS = Object.assign(Object.create(null), {
        codex: { viewBox: '-1.76 0 259.52 259.52', path: 'M239.184 106.203a64.716 64.716 0 0 0-5.576-53.103C219.452 28.459 191 15.784 163.213 21.74A65.586 65.586 0 0 0 52.096 45.22a64.716 64.716 0 0 0-43.23 31.36c-14.31 24.602-11.061 55.634 8.033 76.74a64.665 64.665 0 0 0 5.525 53.102c14.174 24.65 42.644 37.324 70.446 31.36a64.72 64.72 0 0 0 48.754 21.744c28.481.025 53.714-18.361 62.414-45.481a64.767 64.767 0 0 0 43.229-31.36c14.137-24.558 10.875-55.423-8.083-76.483Zm-97.56 136.338a48.397 48.397 0 0 1-31.105-11.255l1.535-.87 51.67-29.825a8.595 8.595 0 0 0 4.247-7.367v-72.85l21.845 12.636c.218.111.37.32.409.563v60.367c-.056 26.818-21.783 48.545-48.601 48.601Zm-104.466-44.61a48.345 48.345 0 0 1-5.781-32.589l1.534.921 51.722 29.826a8.339 8.339 0 0 0 8.441 0l63.181-36.425v25.221a.87.87 0 0 1-.358.665l-52.335 30.184c-23.257 13.398-52.97 5.431-66.404-17.803ZM23.549 85.38a48.499 48.499 0 0 1 25.58-21.333v61.39a8.288 8.288 0 0 0 4.195 7.316l62.874 36.272-21.845 12.636a.819.819 0 0 1-.767 0L41.353 151.53c-23.211-13.454-31.171-43.144-17.804-66.405v.256Zm179.466 41.695-63.08-36.63L161.73 77.86a.819.819 0 0 1 .768 0l52.233 30.184a48.6 48.6 0 0 1-7.316 87.635v-61.391a8.544 8.544 0 0 0-4.4-7.213Zm21.742-32.69-1.535-.922-51.619-30.081a8.39 8.39 0 0 0-8.492 0L99.98 99.808V74.587a.716.716 0 0 1 .307-.665l52.233-30.133a48.652 48.652 0 0 1 72.236 50.391v.205ZM88.061 139.097l-21.845-12.585a.87.87 0 0 1-.41-.614V65.685a48.652 48.652 0 0 1 79.757-37.346l-1.535.87-51.67 29.825a8.595 8.595 0 0 0-4.246 7.367l-.051 72.697Zm11.868-25.58 28.138-16.217 28.188 16.218v32.434l-28.086 16.218-28.188-16.218-.052-32.434Z' },
        claude: { viewBox: '0 0 24 24', path: 'm4.7144 15.9555 4.7174-2.6471.079-.2307-.079-.1275h-.2307l-.7893-.0486-2.6956-.0729-2.3375-.0971-2.2646-.1214-.5707-.1215-.5343-.7042.0546-.3522.4797-.3218.686.0608 1.5179.1032 2.2767.1578 1.6514.0972 2.4468.255h.3886l.0546-.1579-.1336-.0971-.1032-.0972L6.973 9.8356l-2.55-1.6879-1.3356-.9714-.7225-.4918-.3643-.4614-.1578-1.0078.6557-.7225.8803.0607.2246.0607.8925.686 1.9064 1.4754 2.4893 1.8336.3643.3035.1457-.1032.0182-.0728-.164-.2733-1.3539-2.4467-1.445-2.4893-.6435-1.032-.17-.6194c-.0607-.255-.1032-.4674-.1032-.7285L6.287.1335 6.6997 0l.9957.1336.419.3642.6192 1.4147 1.0018 2.2282 1.5543 3.0296.4553.8985.2429.8318.091.255h.1579v-.1457l.1275-1.706.2368-2.0947.2307-2.6957.0789-.7589.3764-.9107.7468-.4918.5828.2793.4797.686-.0668.4433-.2853 1.8517-.5586 2.9021-.3643 1.9429h.2125l.2429-.2429.9835-1.3053 1.6514-2.0643.7286-.8196.85-.9046.5464-.4311h1.0321l.759 1.1293-.34 1.1657-1.0625 1.3478-.8804 1.1414-1.2628 1.7-.7893 1.36.0729.1093.1882-.0183 2.8535-.607 1.5421-.2794 1.8396-.3157.8318.3886.091.3946-.3278.8075-1.967.4857-2.3072.4614-3.4364.8136-.0425.0304.0486.0607 1.5482.1457.6618.0364h1.621l3.0175.2247.7892.522.4736.6376-.079.4857-1.2142.6193-1.6393-.3886-3.825-.9107-1.3113-.3279h-.1822v.1093l1.0929 1.0686 2.0035 1.8092 2.5075 2.3314.1275.5768-.3218.4554-.34-.0486-2.2039-1.6575-.85-.7468-1.9246-1.621h-.1275v.17l.4432.6496 2.3436 3.5214.1214 1.0807-.17.3521-.6071.2125-.6679-.1214-1.3721-1.9246L14.38 17.959l-1.1414-1.9428-.1397.079-.674 7.2552-.3156.3703-.7286.2793-.6071-.4614-.3218-.7468.3218-1.4753.3886-1.9246.3157-1.53.2853-1.9004.17-.6314-.0121-.0425-.1397.0182-1.4328 1.9672-2.1796 2.9446-1.7243 1.8456-.4128.164-.7164-.3704.0667-.6618.4008-.5889 2.386-3.0357 1.4389-1.882.929-1.0868-.0062-.1579h-.0546l-6.3385 4.1164-1.1293.1457-.4857-.4554.0608-.7467.2307-.2429 1.9064-1.3114Z' },
        cursor: { viewBox: '0 0 24 24', path: 'M11.503.131 1.891 5.678a.84.84 0 0 0-.42.726v11.188c0 .3.162.575.42.724l9.609 5.55a1 1 0 0 0 .998 0l9.61-5.55a.84.84 0 0 0 .42-.724V6.404a.84.84 0 0 0-.42-.726L12.497.131a1.01 1.01 0 0 0-.996 0M2.657 6.338h18.55c.263 0 .43.287.297.515L12.23 22.918c-.062.107-.229.064-.229-.06V12.335a.59.59 0 0 0-.295-.51l-9.11-5.257c-.109-.063-.064-.23.061-.23' },
        opencode: { viewBox: '0 0 24 24', path: 'M22 24H2V0h20zM17 4.8H7v14.4h10z' },
        agy: { viewBox: '0 -0.61 15.53 15.53', path: 'M14.0777 13.984C14.945 14.6345 16.2458 14.2008 15.0533 13.0084C11.476 9.53949 12.2349 0 7.79033 0C3.34579 0 4.10461 9.53949 0.527295 13.0084C-0.773543 14.3092 0.635692 14.6345 1.50293 13.984C4.86344 11.7076 4.64663 7.69664 7.79033 7.69664C10.934 7.69664 10.7172 11.7076 14.0777 13.984Z' }
    });

    var SVG_NS = 'http://www.w3.org/2000/svg';

    // Every drawing here stands beside words that already say the same thing,
    // so all of them are set up the same way and all of them are kept out of
    // the reading order.
    function svgCanvas(viewBox, width, height) {
        var svg = document.createElementNS(SVG_NS, 'svg');
        svg.setAttribute('viewBox', viewBox);
        svg.setAttribute('width', String(width));
        svg.setAttribute('height', String(height));
        svg.setAttribute('aria-hidden', 'true');
        svg.setAttribute('focusable', 'false');
        return svg;
    }

    function svgIcon(paths, size, filled, viewBox) {
        var svg = svgCanvas(viewBox || '0 0 24 24', size, size);
        if (filled) {
            svg.setAttribute('fill', 'currentColor');
        } else {
            svg.setAttribute('fill', 'none');
            svg.setAttribute('stroke', 'currentColor');
            svg.setAttribute('stroke-width', '1.8');
            svg.setAttribute('stroke-linecap', 'round');
            svg.setAttribute('stroke-linejoin', 'round');
        }
        paths.forEach(function (d) {
            var path = document.createElementNS(SVG_NS, 'path');
            path.setAttribute('d', d);
            svg.appendChild(path);
        });
        return svg;
    }

    function icon(name, size) {
        return svgIcon(ICON_PATHS[name] || [], size || 14, false);
    }

    // Vendor marks are filled shapes where icon() strokes, each in its own grid.
    function brandIcon(name, size) {
        var mark = BRAND_MARKS[name];
        return svgIcon([mark.path], size, true, mark.viewBox);
    }

    function withIcon(node, name, size) {
        node.insertBefore(icon(name, size), node.firstChild);
        return node;
    }

    function svgEl(tag, attrs) {
        var node = document.createElementNS(SVG_NS, tag);
        Object.keys(attrs || {}).forEach(function (k) { node.setAttribute(k, String(attrs[k])); });
        return node;
    }

    // Colour alone says nothing to a screen reader, so a visible dot is either
    // paired with a word or explicitly hidden from the reading order by whoever
    // puts the state into a label of its own.
    function stateDot(tone, spoken) {
        var dot = el('span', 'state-dot ' + tone);
        if (!spoken) dot.setAttribute('aria-hidden', 'true');
        return dot;
    }

    function dotLabel(text, tone) {
        var wrap = el('span', 'dot-label');
        wrap.appendChild(stateDot(tone || 'muted', true));
        wrap.appendChild(document.createTextNode(text));
        return wrap;
    }

    /* ------------------------------------------------------------------
       Words for time and numbers.
       ------------------------------------------------------------------ */

    var MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
    var WEEKDAYS = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'];

    function pad2(n) {
        return (n < 10 ? '0' : '') + n;
    }

    // When a window resets or a cooldown ends, as a date and hour: no ticking.
    function formatResetAt(iso) {
        if (!iso) return '';
        var at = new Date(String(iso));
        if (isNaN(at.getTime())) return '';
        return at.getDate() + ' ' + MONTHS[at.getMonth()] + ', ' + pad2(at.getHours()) + ':' + pad2(at.getMinutes());
    }

    function relSince(ms) {
        var at = new Date(ms);
        return isFinite(at.getTime()) ? relTime(at.toISOString()) : '';
    }

    // Which side of now a moment falls on is read before the rounding: half a
    // minute ago is not "in a moment".
    function relTime(iso) {
        if (!iso) return '';
        var at = Date.parse(String(iso));
        if (!isFinite(at)) return '';
        var delta = at - Date.now();
        var mins = Math.round(Math.abs(delta) / 60000);
        var future = delta >= 0;
        var body;
        if (mins <= 1) body = 'a moment';
        else if (mins < 60) body = mins + 'm';
        else if (mins < 2880) body = Math.round(mins / 60) + 'h';
        else body = Math.round(mins / 1440) + 'd';
        return future ? ('in ' + body) : (body + ' ago');
    }

    function isoOf(seconds) {
        var at = new Date(seconds * 1000);
        return isNaN(at.getTime()) ? '' : at.toISOString();
    }

    function clockAt(iso) {
        var at = new Date(String(iso || ''));
        if (isNaN(at.getTime())) return '';
        return pad2(at.getHours()) + ':' + pad2(at.getMinutes());
    }

    // A moment, with its day and month: "Thu 8 Oct 22:59", in local time.
    function whenWords(iso) {
        var at = new Date(String(iso || ''));
        if (isNaN(at.getTime())) return '';
        return WEEKDAYS[at.getDay()] + ' ' + at.getDate() + ' ' + MONTHS[at.getMonth()] + ' '
            + pad2(at.getHours()) + ':' + pad2(at.getMinutes());
    }

    // The whole moment, for a tooltip or a table: "Sat 27 Sep, 14:05".
    function momentWords(seconds) {
        var at = new Date(seconds * 1000);
        if (isNaN(at.getTime())) return '';
        return WEEKDAYS[at.getDay()] + ' ' + at.getDate() + ' ' + MONTHS[at.getMonth()]
            + ', ' + pad2(at.getHours()) + ':' + pad2(at.getMinutes());
    }

    function ageWords(iso) {
        var at = Date.parse(String(iso || ''));
        if (!isFinite(at)) return '';
        var mins = Math.round(Math.max(0, Date.now() - at) / 60000);
        if (mins < 2) return 'just now';
        if (mins < 90) return mins + ' min';
        if (mins < 2880) return Math.round(mins / 60) + ' h';
        return Math.round(mins / 1440) + ' d';
    }

    function minutesWords(seconds) {
        if (typeof seconds !== 'number' || !isFinite(seconds)) return '';
        var mins = Math.round(seconds / 60);
        return mins < 90 ? (mins + ' min') : (Math.round(mins / 6) / 10 + ' h');
    }

    // "33 min", "30–33 min", "1.2–2 h": one unit said once.
    function spanWords(lo, hi) {
        var a = minutesWords(lo);
        var b = minutesWords(hi);
        if (a === b) return a;
        var unit = / (min|h)$/.exec(a);
        if (unit && b.slice(-unit[0].length) === unit[0]) return a.slice(0, -unit[0].length) + '–' + b;
        return a + '–' + b;
    }

    // "in 1 h", "in 6 h", "in 3 days": a checkpoint's distance from now.
    function offsetWords(seconds) {
        if (seconds % 86400 === 0 && seconds >= 86400) {
            var days = seconds / 86400;
            return days === 1 ? 'in 24 h' : 'in ' + days + ' days';
        }
        return 'in ' + Math.round(seconds / 3600) + ' h';
    }

    function tzWords() {
        var offset = -new Date().getTimezoneOffset();
        var sign = offset >= 0 ? '+' : '−';
        var abs = Math.abs(offset);
        var words = 'UTC' + sign + Math.floor(abs / 60) + (abs % 60 ? ':' + pad2(abs % 60) : '');
        try {
            var name = Intl.DateTimeFormat().resolvedOptions().timeZone;
            if (name) words = name + ' (' + words + ')';
        } catch (err) {
            // No zone name in this frame: the offset alone is still true.
        }
        return words;
    }

    function num(value, digits) {
        return (typeof value === 'number' && isFinite(value)) ? value.toFixed(digits) : '';
    }

    function plural(n, word) {
        return n + ' ' + word + (n === 1 ? '' : 's');
    }

    // A share left, as a whole percent — except that rounding never makes a
    // share that is there read as none, or one that is not full read as full.
    function leftPct(value) {
        if (typeof value !== 'number' || !isFinite(value)) return '';
        var shown = Math.round(value);
        if (shown <= 0 && value > 0) return '<1';
        if (shown >= 100 && value < 100) return '>99';
        return String(shown);
    }

    // The same share to the hundredth of a percent, for one bar under the
    // pointer: 0.04% stays 0.04%, and a share not at the limit never reads 0.
    function exactPct(value, spent) {
        var shown = +value.toFixed(2);
        if (shown <= 0 && !spent) return '<0.01';
        if (shown >= 100 && value < 100) return '>99.99';
        return String(shown);
    }

    // Account-windows to two decimals — "<0.01" when accounts not at the
    // limit add up to less than that, never a "0.00" that reads as all spent.
    function windowsLeft(m) {
        var text = num(m.windows, 2);
        return text === '0.00' && (m.at_limit || 0) < (m.accounts || 0) ? '<0.01' : text;
    }

    // An amount a scenario gives back or holds: "+1.80", "+<0.01".
    function amountWords(value) {
        if (typeof value !== 'number' || !isFinite(value)) return '';
        var text = value.toFixed(2);
        return text === '0.00' && value > 0 ? '<0.01' : text;
    }

    // Only the host's explicit quota projection omits catalog deliberately.
    // Its not_read provenance stays intact; the same state on a legacy status
    // answer remains a failure to read a requested facet.
    function catalogOmitted(view, facet) {
        return facet === 'catalog' && !!view && !!view.passive_read
            && view.passive_read.mode === 'quota' && (view.facets || {}).catalog === 'not_read';
    }

    function unreadFacetsOf(view) {
        var facets = (view && view.facets) || {};
        return FACET_ORDER.filter(function (f) {
            return !catalogOmitted(view, f) && (facets[f] || 'indeterminate') !== 'ok';
        });
    }

    function facetTone(state) {
        if (state === 'ok') return 'ok';
        if (state === 'failed') return 'bad';
        if (state === 'not_read') return 'warn';
        return 'muted';
    }

    function facetWord(state) {
        if (state === 'ok') return 'read';
        if (state === 'not_read') return 'not read';
        if (state === 'failed') return 'failed';
        return 'indeterminate';
    }

    /* ------------------------------------------------------------------
       One account's windows and holds, as the skill read them.
       ------------------------------------------------------------------ */

    function hasPct(value) {
        return value !== null && value !== undefined;
    }

    // Whether a window is at its limit is the skill's verdict on the
    // unrounded share (at_limit), never a rounded percent: 99.6% is not
    // spent. An answer without the verdict falls back to the percent.
    function atLimit(view) {
        if (!view) return false;
        if (typeof view.at_limit === 'boolean') return view.at_limit;
        return hasPct(view.used_pct) && view.used_pct >= 100;
    }

    // The share used, in the skill's own words for it ("58", "99.6", "<100").
    function pctText(view) {
        if (!view || !hasPct(view.used_pct)) return '';
        if (typeof view.used_text === 'string' && view.used_text) return view.used_text;
        return String(view.used_pct);
    }

    // One bar language for the whole widget: a bar is as long (or tall) as
    // the share LEFT on one 0–100% scale, in one neutral ink over a hairline
    // base. Amber is a share held back now, a share at its limit has no fill
    // but a red base, and a last-known reading is muted and claims neither.
    // No number, no bar: an empty bar would read as "0% left".
    function meter(leftPctValue, how) {
        if (typeof leftPctValue !== 'number' || !isFinite(leftPctValue)) return null;
        how = how || {};
        var bar = el('span', 'meter'
            + (how.stale ? ' stale' : (how.spent ? ' spent' : (how.held ? ' restricted' : ''))));
        bar.setAttribute('aria-hidden', 'true');
        var left = Math.max(0, Math.min(100, leftPctValue));
        var spent = !how.stale && !!how.spent;
        bar.title = exactPct(left, spent || left <= 0) + '% left' + (spent ? ' — at the limit' : '')
            + (how.stale ? ' — last known' : (!spent && how.held ? ' — held back now' : ''));
        if (left > 0 && !spent) {
            var fill = el('span', 'meter-fill');
            fill.style.width = +left.toFixed(4) + '%';
            bar.appendChild(fill);
        }
        return bar;
    }

    function windowMeter(view, how) {
        if (!view || !hasPct(view.used_pct)) return null;
        how = how || {};
        var stale = !!how.stale;
        var spent = !stale && atLimit(view);
        return meter(100 - view.used_pct, {
            stale: stale,
            spent: spent,
            held: !stale && !spent && (!!how.held || windowCooling(view))
        });
    }

    function liveWindows(account) {
        return ((account.quota || {}).constraints || []);
    }

    function isModelWindow(view) {
        return !!(view && view.scoped_models && view.scoped_models.length);
    }

    // A hold names a model scope only with the skill's own identity of one:
    // '-' is "no model named" and '' an answer that carried none.
    function isModelScope(key) {
        return !!key && key !== '-';
    }

    // The cooldowns the skill carried as facts of their own: what each holds
    // (the whole account or some models), until when or why that is not known,
    // and whether a stale reading reported it. Never a reset.
    function cooldownsOf(quota, scope) {
        return ((quota || {}).cooldowns || []).filter(function (c) {
            return !!c && (!scope || c.scope === scope);
        });
    }

    function exhaustionsOf(quota) {
        return ((quota || {}).model_exhaustions || []).filter(function (e) { return !!e; });
    }

    function liveExhaustions(quota) {
        return exhaustionsOf(quota).filter(function (e) { return e.live === true; });
    }

    // Every model scope held now, apart from the windows: a cooldown on some
    // models, or a model limit reported out until a reset still ahead. A
    // live exhaustion naming no model holds nothing (the reserve counts it
    // against none) and is only disclosed.
    function modelHolds(quota) {
        var out = cooldownsOf(quota, 'models').map(function (c) {
            return { kind: 'cooldown', key: c.scope_key || '', models: c.models || [],
                     omitted: c.models_omitted || 0, until: c.until || '', note: c.until_note || '',
                     stale: !!c.freshness && c.freshness !== 'fresh' };
        });
        liveExhaustions(quota).forEach(function (e) {
            out.push({ kind: 'limit', key: e.scope_key || '', models: e.models || [],
                       omitted: e.models_omitted || 0, until: e.resets_at || '', note: e.reset_note || '',
                       stale: !!e.freshness && e.freshness !== 'fresh' });
        });
        return out.filter(function (h) { return isModelScope(h.key); });
    }

    // The hold a model window stands under, tied by the whole scope
    // (scope_key), never by printed names; a cooldown before a limit.
    function scopeHoldOf(view, quota) {
        var scope = view && view.scope_key;
        if (!isModelScope(scope) || !isModelWindow(view)) return null;
        var mine = modelHolds(quota).filter(function (h) { return h.key === scope; });
        return mine.filter(function (h) { return h.kind === 'cooldown'; })[0] || mine[0] || null;
    }

    function isCooling(view) {
        return !!(view && view.cooldown_until && Date.parse(view.cooldown_until) > Date.now());
    }

    // A cooldown the engine sent with a date nobody can read: still a cooldown.
    function unreadableCooldown(view) {
        return !!(view && view.cooldown_until && !formatResetAt(view.cooldown_until));
    }

    function windowCooling(view) {
        return isCooling(view) || unreadableCooldown(view);
    }

    // The engine repeats a cooldown's own end as the resets_at of a bare
    // cooldown constraint: that instant is when the cooldown lifts, not a
    // reset, and is said once, as the cooldown's end.
    function ownReset(view) {
        var reset = (view && view.resets_at) || '';
        if (reset && view.cooldown_until && !view.window_seconds && !hasPct(view.used_pct)
            && Date.parse(reset) === Date.parse(view.cooldown_until)) return '';
        return reset;
    }

    // Spent is the measured share at its limit — the skill's verdict, and the
    // only red. A cooldown holds a share back; it never spends it.
    function isSpent(view) {
        return atLimit(view);
    }

    function isOut(view) {
        return isSpent(view) || windowCooling(view);
    }

    // A failed check arrives as tone 'warn'; 'bad' is taken the same way.
    function verificationFailed(account) {
        var tone = (account.verification || {}).tone;
        return tone === 'warn' || tone === 'bad';
    }

    // Why an account cannot run anything at all — or '' when it can. With no
    // login there is nothing to say about a check, and a switched-off account
    // is the reader's own doing before it is anything else. While the account
    // list is not read now no account is said not to run: its state is not
    // known, only last known.
    function inactiveReason(account, facets) {
        if (facets && facets.accounts && facets.accounts !== 'ok') return '';
        if (!account.signed_in) return 'signed_out';
        if (account.enabled === false) return 'disabled';
        if (verificationFailed(account)) return 'failed';
        return '';
    }

    var INACTIVE_WORDS = {
        signed_out: 'not signed in',
        disabled: 'switched off in Claudexor',
        failed: 'verification failed'
    };

    // An account that calls for a look: a spent shared window, a cooldown or a
    // failed check. Switched off and signed out are states, not alarms.
    function isAlertAccount(account) {
        var quota = account.quota || {};
        var cooling = liveWindows(account).some(function (c) { return !isModelWindow(c) && isCooling(c); });
        return quota.state === 'exhausted' || quota.state === 'cooling' || verificationFailed(account) || cooling;
    }

    function isActive(account) {
        return !!account.signed_in && account.enabled !== false;
    }

    // The engine skips per-model caps when it decides whether an account is
    // spent, so everything that prints one number for a whole account skips
    // them too.
    function worstWindow(account) {
        var worst = null;
        liveWindows(account).forEach(function (c) {
            if (isModelWindow(c)) return;
            if (typeof c.used_pct === 'number' && (!worst || c.used_pct > worst.used_pct)) worst = c;
        });
        return worst;
    }

    function hasSpentModelCap(account) {
        return liveWindows(account).some(function (c) { return isModelWindow(c) && isOut(c); });
    }

    // The dot answers one question: can this account work right now. Green is
    // "read, and fine"; grey is no live reading (never dressed as good); red
    // is an alert; amber a model out or a shared window near its edge.
    function accountTone(account, facets) {
        if (isAlertAccount(account)) return 'bad';
        if (facets && facets.accounts && facets.accounts !== 'ok') return 'muted';
        if (!isActive(account)) return 'muted';
        var worst = worstWindow(account);
        if (!worst) return 'muted';
        if (liveWindows(account).some(function (c) { return !isModelWindow(c) && atLimit(c); })) return 'bad';
        if (hasSpentModelCap(account) || modelHolds(account.quota).length) return 'warn';
        if (worst.used_pct >= 85) return 'warn';
        return 'ok';
    }

    // The account's state in a few words, as its row in the account list says
    // it: what stops it, or how its windows stand.
    function accountState(account, facets) {
        var reason = inactiveReason(account, facets);
        // The engine answers a failed check with signed_in false: the account
        // stands under its first reason, and the failed check is still said
        // and still red — what places it and what colours it are two answers.
        if (reason && reason !== 'failed' && verificationFailed(account)) {
            return { tone: 'bad', word: INACTIVE_WORDS[reason] + ' · ' + INACTIVE_WORDS.failed };
        }
        if (reason) return { tone: reason === 'failed' ? 'bad' : 'muted', word: INACTIVE_WORDS[reason] };
        var quota = account.quota || {};
        if (quota.state === 'exhausted') return { tone: 'bad', word: 'limit reached' };
        if (quota.state === 'cooling' || cooldownsOf(quota, 'account').length) {
            return { tone: 'warn', word: 'cooling down' };
        }
        if (verificationFailed(account)) return { tone: 'bad', word: 'verification failed' };
        if (quota.absence) {
            var action = absenceAction(account, quota.absence);
            return { tone: 'warn', word: 'quota unavailable' + (action ? ' · ' + action : '') };
        }
        if (liveWindows(account).some(function (c) { return isModelWindow(c) && isSpent(c); })) {
            return { tone: 'warn', word: 'a model at its limit' };
        }
        var held = modelHoldWords(quota);
        if (held) return { tone: 'warn', word: held.text, full: held.full };
        if (hasSpentModelCap(account)) return { tone: 'warn', word: 'a model held' };
        if (quota.state === 'not_checked') return { tone: 'muted', word: 'not checked' };
        if (quota.state === 'no_fresh_window' || quota.state === 'not_current') {
            return { tone: 'muted', word: 'no current reading' };
        }
        if (!worstWindow(account)) return { tone: 'muted', word: liveWindows(account).length ? 'no ratio' : 'no reading' };
        var tone = accountTone(account, facets);
        return { tone: tone, word: tone === 'warn' ? 'nearly used' : (tone === 'muted' ? 'last known' : 'ready') };
    }

    // The model holds the skill carried apart from the windows, in words:
    // one is named with its model and its kind; several are counted, and the
    // full sentence names each scope, its end and its provenance — two
    // scopes are never one "model cooldown".
    function modelHoldWords(quota) {
        var holds = modelHolds(quota);
        if (!holds.length) return null;
        function kindWords(h) { return h.kind === 'cooldown' ? 'model cooldown' : 'model limit reached'; }
        function sentence(h) {
            var when = formatResetAt(h.until);
            var end = when ? ' until ' + when
                : (h.kind === 'cooldown' ? ', ' + (COOLDOWN_END[h.note] || COOLDOWN_END.not_reported) : '');
            return kindWords(h) + ': ' + (modelLabel(h.models, h.omitted) || 'models not named') + end
                + (h.stale ? ', reported by a stale reading' : '') + ' (' + scopeList(h.models, h.omitted) + ')';
        }
        if (holds.length === 1) {
            return { text: kindWords(holds[0]) + ': ' + (modelLabel(holds[0].models, holds[0].omitted) || 'models not named'),
                     full: sentence(holds[0]) };
        }
        var kinds = {};
        holds.forEach(function (h) { kinds[h.kind] = true; });
        var noun = kinds.cooldown && kinds.limit ? 'model holds' : (kinds.cooldown ? 'model cooldowns' : 'model limits reached');
        return { text: holds.length + ' ' + noun, full: holds.length + ' ' + noun + ': ' + holds.map(sentence).join('; ') };
    }

    // Presentation labels from the host's web/modules/harness_presentation.js,
    // the existing source for our family marks. A passive read omits the
    // catalog; its raw id fallback must not displace the known display name.
    var FAMILY_LABELS = Object.assign(Object.create(null), {
        codex: 'Codex', claude: 'Claude Code', cursor: 'Cursor',
        opencode: 'OpenCode', agy: 'Antigravity'
    });

    function familyName(group) {
        var label = String(group.family_label || '').trim();
        return label && label !== group.harness_id ? label
            : FAMILY_LABELS[group.harness_id] || label || group.harness_id;
    }

    // "claude_max" beside "Claude Code" says Claude twice: the vendor prefix
    // is dropped only when the family name carries it; the title keeps all.
    function planWord(plan, group) {
        var text = String(plan || '').replace(/_/g, ' ').replace(/\s+/g, ' ').trim();
        var family = String(familyName(group) || '').trim().split(/\s+/)[0];
        if (family && text.toLowerCase().indexOf(family.toLowerCase() + ' ') === 0) {
            text = text.slice(family.length + 1).trim();
        }
        return text;
    }

    // A harness that is down or switched off is trouble even with no account.
    function groupTrouble(group) {
        return !!(group.harness_status && group.harness_status !== 'ok') || group.harness_enabled === false;
    }

    var FAMILY_MARK_PX = 14;

    // A family looks the same wherever it is named: its own mark when the
    // widget carries one, the ring with its initial when it does not — never
    // somebody else's logo.
    function familyMark(group) {
        return BRAND_MARKS[group.harness_id]
            ? brandIcon(group.harness_id, FAMILY_MARK_PX)
            : el('span', 'harness-initial', ((familyName(group) || '?').charAt(0) || '?').toUpperCase());
    }

    function appendHarnessNotes(parent, group, facets) {
        if (group.harness_status && group.harness_status !== 'ok') {
            parent.appendChild(dotLabel('harness ' + group.harness_status, 'warn'));
        }
        if (group.harness_enabled === false) {
            parent.appendChild(dotLabel('harness disabled', 'warn'));
        }
        if (group.catalog_known === false && !catalogOmitted(currentView, 'catalog')) {
            parent.appendChild(dotLabel('catalog ' + facetWord(facets.catalog), facetTone(facets.catalog)));
        }
    }

    /* ------------------------------------------------------------------
       Window names, pools and model scopes.
       ------------------------------------------------------------------ */

    var WINDOW_UNITS = [
        [604800, 'week'], [86400, 'day'], [3600, 'hour'], [60, 'minute']
    ];

    // Whether a name only repeats the length — asked about the value, not the
    // shape: "3 day" beside a week is two read values that disagree.
    function sameLength(text, seconds) {
        var words = WINDOW_UNITS.map(function (u) { return u[1]; }).join('|');
        var found = new RegExp('^(\\d+)\\s+(' + words + ')s?$', 'i').exec(String(text || '').trim());
        if (!found) return false;
        var unit = found[2].toLowerCase();
        for (var i = 0; i < WINDOW_UNITS.length; i++) {
            if (WINDOW_UNITS[i][1] === unit) return WINDOW_UNITS[i][0] * Number(found[1]) === seconds;
        }
        return false;
    }

    // The unit has to divide the length exactly, and a single one drops the
    // number: "week", not "1 week".
    function windowLength(seconds) {
        if (typeof seconds !== 'number' || seconds <= 0) return '';
        for (var i = 0; i < WINDOW_UNITS.length; i++) {
            var size = WINDOW_UNITS[i][0];
            if (seconds % size) continue;
            var n = seconds / size;
            return n === 1 ? WINDOW_UNITS[i][1] : (n + ' ' + WINDOW_UNITS[i][1] + 's');
        }
        return '';
    }

    var ROLE_WORDS = ['primary', 'secondary', 'tertiary'];

    // Window names come as "GPT-5.3-Codex-Spark primary", "7 day", "7 day
    // (Fable)" or a whole phrase. `pool` says whether the name is a pool the
    // window belongs to (a name with a role, or a bracket).
    function splitWindowName(label, seconds) {
        var length = windowLength(seconds);
        var text = String(label || '').trim();
        if (!text) return { model: length ? '' : 'Window Limit', role: '', pool: false };
        var parts = text.split(/\s+/);
        var role = ROLE_WORDS.indexOf(parts[parts.length - 1].toLowerCase()) !== -1 ? parts.pop() : '';
        var rest = parts.join(' ');
        var bracket = /\(([^()]+)\)/.exec(rest);
        if (bracket) return { model: bracket[1], role: role, pool: true };
        if (length && sameLength(rest, seconds)) return { model: '', role: role, pool: false };
        if (!rest && !length) return { model: 'Window Limit', role: role, pool: false };
        return { model: rest, role: role, pool: !!role };
    }

    // A window scoped to a model lists every name that model answers to;
    // the names collapse to the shortest real stem, and "+N" counts every
    // name left out, including names past the 24 sent.
    var MODEL_ALIASES = { best: 1, latest: 1, fastest: 1, 'default': 1 };

    function modelLabel(models, omitted) {
        var list = (models || []).map(function (m) { return String(m || '').trim(); })
            .filter(function (m) { return !!m; });
        if (!list.length) return '';
        var real = list.filter(function (m) { return !MODEL_ALIASES[m.toLowerCase()]; });
        if (!real.length) real = list;
        var stems = {};
        real.forEach(function (m) {
            stems[m.toLowerCase().replace(/^claude-/, '').replace(/([-_][\d.]+)+$/, '')] = 1;
        });
        var names = Object.keys(stems).filter(function (k) { return !!k; });
        if (!names.length) return '';
        names.sort(function (a, b) { return a.length - b.length; });
        var head = names[0].charAt(0).toUpperCase() + names[0].slice(1);
        var more = names.length - 1 + (omitted > 0 ? omitted : 0);
        return head + (more > 0 ? ' +' + more : '');
    }

    // A scope's names in full, as far as the skill sent them.
    function scopeList(models, omitted) {
        var names = (models || []).join(', ') || 'no model named';
        return omitted ? names + ' +' + omitted + ' more not listed' : names;
    }

    function poolOf(view) {
        if (isModelWindow(view)) return modelLabel(view.scoped_models);
        var name = splitWindowName(view.label, view.window_seconds);
        return name.pool ? name.model : '';
    }

    function roleRank(view) {
        var rank = ROLE_WORDS.indexOf(splitWindowName(view.label, view.window_seconds).role.toLowerCase());
        return rank === -1 ? ROLE_WORDS.length : rank;
    }

    function knownLength(view) {
        var seconds = view.window_seconds;
        return typeof seconds === 'number' && seconds > 0 ? seconds : Infinity;
    }

    function compareText(a, b) {
        return a < b ? -1 : (a > b ? 1 : 0);
    }

    // Shorter first, then primary before secondary, then by name.
    function windowOrder(a, b) {
        var la = knownLength(a);
        var lb = knownLength(b);
        if (la !== lb) return la < lb ? -1 : 1;
        var ra = roleRank(a);
        var rb = roleRank(b);
        if (ra !== rb) return ra - rb;
        return compareText(String(a.label || ''), String(b.label || ''));
    }

    // The general pool first, named pools by name — never the engine's order.
    function poolOrder(a, b) {
        var named = (a.pool ? 1 : 0) - (b.pool ? 1 : 0);
        if (named) return named;
        return compareText(a.pool.toLowerCase(), b.pool.toLowerCase())
            || compareText(a.pool, b.pool)
            || compareText(a.key || '', b.key || '');
    }

    // A model's pool is grouped by the skill's identity of its whole scope,
    // never by its printed name: two scopes can print the same "Fable".
    function poolKey(view) {
        if (isModelWindow(view) && isModelScope(view.scope_key)) return 'scope:' + view.scope_key;
        return 'pool:' + poolOf(view);
    }

    function poolGroups(constraints) {
        var groups = [];
        var byPool = Object.create(null);
        (constraints || []).forEach(function (view) {
            var key = poolKey(view);
            if (!byPool[key]) {
                byPool[key] = { pool: poolOf(view), key: key, windows: [] };
                groups.push(byPool[key]);
            }
            byPool[key].windows.push(view);
        });
        groups.sort(poolOrder);
        groups.forEach(function (group) { group.windows.sort(windowOrder); });
        return groups;
    }

    // What a window is called on its line: its length; two of one length in
    // one pool take the role word too, and with no role their own names.
    function windowTags(constraints) {
        var marks = constraints.map(function (c) {
            return { c: c, scoped: isModelWindow(c), pool: poolOf(c),
                     tag: windowLength(c.window_seconds) || String(c.label || '') || 'Window Limit' };
        });
        var seen = {};
        marks.forEach(function (m) { seen[m.tag] = (seen[m.tag] || 0) + 1; });
        marks.forEach(function (m) {
            if (seen[m.tag] < 2) return;
            var role = splitWindowName(m.c.label, m.c.window_seconds).role;
            m.tag = role ? m.tag + ' ' + role : (String(m.c.label || '') || m.tag);
        });
        return marks;
    }

    // The pool's chip, coloured only for a model's own pool: red when its
    // measured share is spent, amber when a cooldown or a reported model
    // limit holds it. Its hover says the whole scope.
    function poolChip(pool, tone, view) {
        var chip = el('span', 'acct-cap acct-pool'
            + (tone === 'bad' ? ' exhausted' : (tone === 'warn' ? ' held' : '')));
        chip.appendChild(el('bdi', null, pool));
        chip.title = isModelWindow(view) ? pool + ' (' + scopeList(view.scoped_models) + ')' : pool;
        return chip;
    }

    // Why one window is out for now, in the words and colour of its own
    // scope, or null: spent (red, back at its reset), cooling (amber, its
    // own cooldown or one on exactly its models, until that ends), or a model
    // limit the engine reports out (amber, until its reported reset).
    function windowOut(m, quota) {
        if (isSpent(m.c)) {
            return { kind: 'spent', word: 'spent', stamp: m.c.resets_at || '', missing: 'no reset time' };
        }
        if (windowCooling(m.c)) {
            return { kind: 'cooling', word: 'cooling down', stamp: m.c.cooldown_until || '',
                     missing: m.c.cooldown_until ? 'end time unreadable' : 'no end time' };
        }
        var hold = scopeHoldOf(m.c, quota);
        if (!hold) return null;
        if (hold.kind === 'cooldown') {
            return { kind: 'cooling', word: 'cooling down', stamp: hold.until,
                     missing: hold.note === 'unreadable' ? 'end time unreadable' : 'no end time', stale: hold.stale };
        }
        return { kind: 'limit', word: 'limit reported', stamp: hold.until, missing: 'no reset time', stale: hold.stale };
    }

    // Held back as a whole: a cooldown or a spent shared limit, or the
    // account cannot run at all.
    function accountHeld(account) {
        var state = ((account || {}).quota || {}).state;
        return state === 'cooling' || state === 'exhausted' || !!inactiveReason(account || {}, null);
    }

    function windowHeld(account, view) {
        var quota = (account || {}).quota;
        return accountHeld(account) || cooldownsOf(quota, 'account').length > 0 || !!scopeHoldOf(view, quota);
    }

    // A moment plus how far off it is; false when the timestamp does not
    // parse ("unreadable date" when one was sent and cannot be read).
    function appendStamp(wrap, iso, tone, withRel) {
        var cls = 'ticker' + (tone === true || tone === 'bad' ? ' bad' : (tone === 'warn' ? ' warn' : ''));
        if (iso && !formatResetAt(iso)) {
            wrap.appendChild(el('span', cls, 'unreadable date'));
            return true;
        }
        var stamp = formatResetAt(iso);
        if (!stamp) return false;
        wrap.appendChild(el('span', cls, stamp));
        if (!withRel) return true;
        // Only for a moment still ahead: "44h ago" beside a reset reads as a
        // window that should have reset and did not.
        if (Date.parse(iso) <= Date.now()) return true;
        var rel = relTime(iso);
        if (rel) wrap.appendChild(el('span', 'rel-time', rel));
        return true;
    }

    // Every window of one reading as lines: its name and pool, its share left,
    // the share used in words, and why it is out with until when — or its
    // own reported reset. A stale reading claims neither red nor amber.
    function windowLines(constraints, account, stale) {
        var quota = (account || {}).quota;
        var box = el('div', 'win-lines');
        poolGroups(constraints).forEach(function (group) {
            windowTags(group.windows).forEach(function (m) {
                var out = stale ? null : windowOut(m, quota);
                var tone = out ? (out.kind === 'spent' ? 'bad' : 'warn') : '';
                var line = el('div', 'win-line' + (stale ? ' stale' : ''));
                var tag = el('span', 'win-tag' + (tone ? ' ' + tone : ''), m.tag);
                tag.title = m.c.label || m.tag;
                line.appendChild(tag);
                if (m.pool) line.appendChild(poolChip(m.pool, m.scoped && !stale ? tone : '', m.c));
                var bar = windowMeter(m.c, { stale: stale, held: !stale && windowHeld(account, m.c) });
                if (bar) line.appendChild(bar);
                var used = hasPct(m.c.used_pct) ? pctText(m.c) + '% used'
                    : (m.c.ratio_problem ? 'Unreadable ratio' : 'no ratio');
                line.appendChild(el('span', 'win-used' + (!stale && hasPct(m.c.used_pct) && atLimit(m.c) ? ' bad' : ''),
                    used));
                var said = [(m.pool ? m.pool + ' ' : '') + m.tag, used];
                if (out) {
                    line.appendChild(el('span', 'win-word ' + tone, out.word));
                    var when = el('span', 'quota-when');
                    // A date that cannot be read says which one is missing, never a gap.
                    if (formatResetAt(out.stamp) && appendStamp(when, out.stamp, out.kind === 'spent' ? 'bad' : 'warn', true)) {
                        line.appendChild(when);
                        said.push(out.word + (out.kind === 'spent' ? ', back ' : ' until ') + formatResetAt(out.stamp));
                    } else {
                        line.appendChild(el('span', 'quota-when', out.missing));
                        said.push(out.word + ', ' + out.missing);
                    }
                    if (out.stale) {
                        line.appendChild(el('span', 'quota-when', 'reported by a stale reading'));
                        said.push('reported by a stale reading');
                    }
                } else {
                    // A last-known reading's cooldown is what it reported then,
                    // in the muted voice: never amber, never "cooling down" now.
                    if (stale && m.c.cooldown_until) {
                        var cool = el('span', 'quota-when', 'cooldown reported ');
                        if (appendStamp(cool, m.c.cooldown_until, false, false)) {
                            line.appendChild(cool);
                            said.push('cooldown reported until ' + (formatResetAt(m.c.cooldown_until) || 'an unreadable date'));
                        }
                    }
                    var reset = el('span', 'quota-when');
                    if (appendStamp(reset, ownReset(m.c), false, !stale)) {
                        reset.insertBefore(el('span', null, stale ? 'reported reset' : 'resets'), reset.firstChild);
                        line.appendChild(reset);
                        said.push((stale ? 'reported reset ' : 'resets ') + formatResetAt(ownReset(m.c)));
                    }
                }
                line.setAttribute('aria-label', said.join(', '));
                box.appendChild(line);
            });
        });
        return box;
    }

    var COOLDOWN_END = {
        unreadable: 'end time unreadable',
        not_reported: 'no end time reported',
        passed: 'its reported end has passed'
    };

    // One sentence per cooldown: what it holds, until when (or why not known)
    // and whether a stale reading reported it. Never "Limit reached".
    function renderCooldowns(parent, quota) {
        cooldownsOf(quota).forEach(function (c) {
            var row = el('div', 'quota-cooldown');
            row.appendChild(icon('warn', 12));
            var body = el('span', 'quota-cooldown-body');
            row.appendChild(body);
            var what = c.scope === 'models' ? (modelLabel(c.models, c.models_omitted) || 'some models') : 'whole account';
            body.appendChild(el('span', null, 'Cooling down · ' + what));
            var said = ['Cooling down', what];
            var until = el('span', 'quota-when', ' · until ');
            if (c.until && appendStamp(until, c.until, 'warn', true)) {
                body.appendChild(until);
                said.push('until ' + formatResetAt(c.until) + ' ' + relTime(c.until));
            } else {
                var end = COOLDOWN_END[c.until_note] || COOLDOWN_END.not_reported;
                body.appendChild(el('span', 'quota-when', ' · ' + end));
                said.push(end);
            }
            if (c.freshness && c.freshness !== 'fresh') {
                var seen = relTime(c.observed_at);
                var from = 'reported by a stale reading' + (seen ? ' observed ' + seen : '');
                body.appendChild(el('span', 'quota-when', ' · ' + from));
                said.push(from);
            }
            if (c.scope === 'models') {
                row.title = scopeList(c.models, c.models_omitted);
                said.push(row.title);
            }
            row.setAttribute('aria-label', said.join(', '));
            parent.appendChild(row);
        });
    }

    var EXHAUSTION_RESET = {
        not_reported: 'no reset time reported',
        unreadable: 'reset time unreadable',
        passed: 'its reported reset has passed'
    };

    // The model-scoped exhaustions the engine reports, each a fact of its
    // own: a model's limit, never the account, never a cooldown. `brief`
    // keeps what may hold now and leaves a passed one, one naming no model,
    // and one whose window `named` already shows at its limit to the details.
    function renderExhaustions(parent, quota, brief, named) {
        exhaustionsOf(quota).forEach(function (e) {
            var unnamed = e.live === true && !isModelScope(e.scope_key);
            if (brief && (unnamed || e.reset_note === 'passed' || (named && named[e.scope_key]))) return;
            var live = e.live === true && !unnamed;
            var row = el('div', 'quota-exhaustion' + (live ? '' : ' past'));
            row.appendChild(icon(live ? 'warn' : 'info', 12));
            var body = el('span', 'quota-cooldown-body');
            row.appendChild(body);
            var what = modelLabel(e.models, e.models_omitted) || 'models not named';
            var lead = live ? 'Model limit reached' : 'Reported model limit reached';
            body.appendChild(el('span', null, lead + ' · ' + what));
            var said = [lead, what];
            var until = el('span', 'quota-when', unnamed ? ' · reported until ' : ' · until ');
            if (unnamed) {
                if (e.resets_at && appendStamp(until, e.resets_at, false, true)) {
                    body.appendChild(until);
                    said.push('reported until ' + formatResetAt(e.resets_at) + ' ' + relTime(e.resets_at));
                }
                body.appendChild(el('span', 'quota-when', ' · holds no window'));
                said.push('holds no window');
            } else if (live && e.resets_at && appendStamp(until, e.resets_at, 'warn', true)) {
                body.appendChild(until);
                said.push('until ' + formatResetAt(e.resets_at) + ' ' + relTime(e.resets_at));
            } else {
                var end = EXHAUSTION_RESET[e.reset_note] || EXHAUSTION_RESET.not_reported;
                if (e.reset_note === 'passed' && formatResetAt(e.resets_at)) end += ' (' + formatResetAt(e.resets_at) + ')';
                body.appendChild(el('span', 'quota-when', ' · ' + end));
                said.push(end);
            }
            if (e.freshness && e.freshness !== 'fresh') {
                var seen = relTime(e.observed_at);
                var from = 'reported by a stale reading' + (seen ? ' observed ' + seen : '');
                body.appendChild(el('span', 'quota-when', ' · ' + from));
                said.push(from);
            }
            row.title = (e.constraint_id ? e.constraint_id + ': ' : '') + scopeList(e.models, e.models_omitted);
            said.push(row.title);
            row.setAttribute('aria-label', said.join(', '));
            parent.appendChild(row);
        });
    }

    function retryAction(retryAt) {
        var at = Date.parse(String(retryAt || ''));
        if (!isFinite(at) || at <= Date.now()) return '';
        return 'Retry after ' + Math.max(1, Math.ceil((at - Date.now()) / 60000)) + 'm';
    }

    // Only the approved generic actions; never raw reason or detail text.
    function absenceAction(account, absence) {
        if (!absence) return '';
        if (absence.action_kind === 'sign_in_if_unverified') return account && account.verified_live ? '' : 'Sign-in required';
        if (absence.action_kind === 'source_missing') return 'No live quota source';
        if (absence.action_kind === 'retry') return retryAction(absence.retry_at);
        return '';
    }

    function renderAbsence(parent, account, absence) {
        if (!absence) return;
        var row = el('div', 'quota-unavailable');
        row.appendChild(icon('warn', 13));
        row.appendChild(el('span', null, absence.message || 'Quota temporarily unavailable'));
        var action = absenceAction(account, absence);
        if (action) row.appendChild(el('span', 'quota-action', action));
        parent.appendChild(row);
    }

    function quotaClass(state) {
        if (state === 'exhausted') return 'quota-primary-text exhausted';
        if (state === 'cooling') return 'quota-primary-text cooling';
        return 'quota-primary-text';
    }

    // The account's own verdict: "Limit reached · Resets …" with the spent
    // window's reset, "Cooling down until …" (an end, never a reset), or why
    // there is no current window.
    function renderQuotaVerdict(quota) {
        var p = el('div', 'quota-primary-row');
        p.appendChild(el('span', quotaClass(quota.state), quota.label || 'No quota data reported'));
        if (quota.state === 'cooling') {
            var untilWrap = el('span', 'quota-when', 'until ');
            if (appendStamp(untilWrap, quota.cooling_until, 'warn', true)) p.appendChild(untilWrap);
            return p;
        }
        var resetWrap = el('span', 'quota-when', 'Resets ');
        if (appendStamp(resetWrap, quota.resets_at, quota.state === 'exhausted', true)) p.appendChild(resetWrap);
        return p;
    }

    function quotaObservedAt(account) {
        return String(((account || {}).quota || {}).observed_at || '');
    }

    /* ------------------------------------------------------------------
       The family's limits.

       Every number is the skill's (quota_summary.py), the same calculation
       the model's quota_summary tool reads. The widget formats; it never
       adds, averages or projects anything of its own.
       ------------------------------------------------------------------ */

    function reserveGroups(view, harnessId) {
        var summary = view && view.reserve && view.reserve.summary;
        if (!summary || !summary.groups) return [];
        return summary.groups.filter(function (g) { return g.harness === harnessId; });
    }

    function groupByKey(groups, key) {
        for (var i = 0; i < groups.length; i++) if (groups[i].key === key) return groups[i];
        return null;
    }

    // The limit the timeline shows for a family: the one the reader picked,
    // as long as it still exists, else the family's lowest left — the rule
    // the skill applies when asked for none. The limit it opens on is kept as
    // a pick is: an answer during an outage that names another limit "lowest
    // left" does not move it; only a click, or the limit's absence from a
    // whole answer, chooses again.
    function chartKeyFor(harnessId, groups) {
        var picked = chartKeys[harnessId];
        var i;
        for (i = 0; i < groups.length; i++) {
            if (groups[i].key === picked) return picked;
        }
        var key = '';
        for (i = 0; i < groups.length && !key; i++) if (groups[i].tightest) key = groups[i].key;
        for (i = 0; i < groups.length && !key; i++) {
            if (groups[i].measured && groups[i].measured.accounts) key = groups[i].key;
        }
        if (!key && groups.length) key = groups[0].key;
        var whole = !!(currentView && (currentView.complete === undefined
            ? currentView.ok : currentView.complete === true));
        if (key && (picked === undefined || whole)) chartKeys[harnessId] = key;
        return key;
    }

    // The horizon of a limit's timeline: the reader's pick, else the limit's
    // own — a week for a limit longer than a day, so all its next resets fit.
    function horizonFor(g) {
        if (horizonChoice) return horizonChoice;
        return g && typeof g.window_seconds === 'number' && g.window_seconds > 86400 ? '7d' : '24h';
    }

    function reserveUrl(reuse) {
        var parts = [];
        if (selectedHarness) parts.push('harness=' + encodeURIComponent(selectedHarness));
        if (timelineOpen) {
            var groups = selectedHarness ? reserveGroups(currentView, selectedHarness) : [];
            var key = selectedHarness ? chartKeyFor(selectedHarness, groups) : '';
            if (key) parts.push('group=' + encodeURIComponent(key));
            parts.push('horizon=' + encodeURIComponent(horizonFor(groupByKey(groups, key))));
        } else {
            // The timeline is folded: the skill need not draw it, nor read the
            // longer stretch of history it would need.
            parts.push('chart=0');
        }
        if (reuse) parts.push('reuse=1');
        return ROUTE + '?' + parts.join('&');
    }

    function chartMissing(view) {
        if (!timelineOpen) return false;
        var groups = reserveGroups(view, selectedHarness);
        if (!groups.length) return false;
        var key = chartKeyFor(selectedHarness, groups);
        var chart = view.reserve.chart;
        return !chart || chart.group_key !== key || chart.horizon !== horizonFor(groupByKey(groups, key));
    }

    // After any drawing: when the timeline is open and the chart on hand is
    // for another limit or horizon, ask once for the right one, with reuse.
    // The same question is never asked twice in a row.
    function askForChart() {
        if (stopped || inFlight || !currentView || !chartMissing(currentView)) return;
        var url = reserveUrl(true);
        if (url === chartAsked) return;
        chartAsked = url;
        Promise.resolve().then(function () { load(true); });
    }

    // How old the readings behind the family's figures are: the provider's
    // own observation times, never the moment this page asked for them.
    var OLD_READING_MS = 30 * 60000;

    function readingAge(summary, groups) {
        var newest = '';
        var oldest = '';
        groups.forEach(function (g) {
            var o = g.observed || {};
            if (o.newest_at && (!newest || o.newest_at > newest)) newest = o.newest_at;
            if (o.oldest_at && (!oldest || o.oldest_at < oldest)) oldest = o.oldest_at;
        });
        if (!newest) return { text: 'No fresh reading', old: true, detail: 'no fresh reading' };
        var a = ageWords(oldest);
        var b = ageWords(newest);
        var text;
        if (a === b) {
            text = a === 'just now' ? 'Observed just now' : 'Observed ' + a + ' ago';
        } else if (b === 'just now') {
            text = 'Observed up to ' + a + ' ago';
        } else {
            var unit = / (min|h|d)$/.exec(a);
            text = 'Observed ' + (unit && b.slice(-unit[0].length) === unit[0]
                ? b.slice(0, -unit[0].length) : b) + '–' + a + ' ago';
        }
        var detail = 'provider readings observed ' + clockAt(oldest)
            + (clockAt(oldest) !== clockAt(newest) ? '–' + clockAt(newest) : '')
            + (summary.status_read_at ? ' · status read ' + clockAt(summary.status_read_at) : '')
            + ' · times in ' + tzWords();
        return { text: text, old: Date.now() - Date.parse(oldest) > OLD_READING_MS, detail: detail };
    }

    var FLAG_WORDS = {
        disabled: 'disabled',
        signed_out: 'signed out',
        auth_failed: 'check failed',
        cooling: 'cooldown reported',
        model_exhausted: 'with this model limit reported reached',
        other_limit_spent: 'with another limit spent',
        account_state_unknown: 'account state not read'
    };
    var FLAG_SHORT = {
        disabled: 'disabled',
        signed_out: 'signed out',
        auth_failed: 'check failed',
        cooling: 'cooling down',
        model_exhausted: 'model limit reported reached',
        other_limit_spent: 'blocked by another limit',
        account_state_unknown: 'state not read'
    };

    var LENGTH_NAMES = { week: 'Weekly', day: 'Daily', hour: 'Hourly', minute: 'Per-minute' };

    // "5 hours" → "5-hour", "week" → "Weekly".
    function lengthName(seconds) {
        var words = windowLength(seconds);
        if (!words) return 'No stated length';
        if (LENGTH_NAMES[words]) return LENGTH_NAMES[words];
        var found = /^(\d+) (\w+?)s$/.exec(words);
        return found ? found[1] + '-' + found[2] : words;
    }

    // What tells a limit apart in its family besides its length.
    function limitTag(g) {
        var name = splitWindowName(g.label, g.window_seconds);
        var models = g.models || [];
        if (models.length) return (name.pool && name.model) ? name.model : (modelLabel(models, g.models_omitted) || models[0]);
        // Tied to models whose names the history did not keep: still scoped.
        if (g.model_scope === 'names_unknown') return 'model-scoped';
        return name.pool ? name.model : '';
    }

    // The short names of one family's limits, never two alike on screen.
    function limitNames(groups) {
        var names = {};
        function collide() {
            var seen = {};
            groups.forEach(function (g) { seen[names[g.key]] = (seen[names[g.key]] || 0) + 1; });
            return groups.filter(function (g) { return seen[names[g.key]] > 1; });
        }
        groups.forEach(function (g) {
            var tag = limitTag(g);
            // A pool named after the family itself says nothing the tab does not.
            if (tag && String(tag).toLowerCase() === String(g.harness || '').toLowerCase()) tag = '';
            names[g.key] = lengthName(g.window_seconds) + (tag ? ' · ' + tag : '');
        });
        collide().forEach(function (g) {
            var role = splitWindowName(g.label, g.window_seconds).role;
            if (role) names[g.key] += ' · ' + role;
        });
        collide().forEach(function (g) {
            var models = g.models || [];
            var more = models.length - 1 + (g.models_omitted || 0);
            names[g.key] = models.length
                ? lengthName(g.window_seconds) + ' · ' + models[0] + (more ? ' +' + more : '')
                : names[g.key] + ' · ' + String(g.label || g.meaning || '');
        });
        var counter = {};
        collide().forEach(function (g) {
            counter[names[g.key]] = (counter[names[g.key]] || 0) + 1;
            names[g.key] += ' (' + counter[names[g.key]] + ')';
        });
        return names;
    }

    // The whole scope, in words: what the short name abbreviates.
    function scopeWords(g) {
        var models = g.models || [];
        var omitted = g.models_omitted || 0;
        if (!models.length && g.model_scope === 'names_unknown') {
            return 'model-scoped: model names unavailable from the history';
        }
        return models.length ? (models.length + omitted === 1 ? 'model: ' : 'models: ') + models.join(', ')
            + (omitted ? ' +' + omitted + ' more' : '') : '';
    }

    function restrictionWords(g) {
        var out = [];
        var r = g.restrictions || {};
        Object.keys(r).forEach(function (flag) {
            var slot = r[flag] || {};
            out.push(slot.accounts + ' ' + (FLAG_WORDS[flag] || flag) + ' (' + num(slot.windows, 2) + ')');
        });
        return out;
    }

    function applicabilityUnknown(g) {
        return typeof g.applicability_unknown === 'number'
            ? g.applicability_unknown : ((g.coverage || {}).other_family_accounts || 0);
    }

    function coverageWords(g) {
        var c = g.coverage || {};
        var out = [];
        if (c.stale_only) out.push(c.stale_only + ' stale');
        if (c.invalid) out.push(c.invalid + ' unreadable');
        if (c.conflicting) out.push(c.conflicting + ' sources disagree');
        if (c.reset_passed) out.push(c.reset_passed + ' reset since reading');
        var lk = (g.last_known || {}).accounts;
        if (lk) out.push(lk + ' shown as last known, not current');
        var other = applicabilityUnknown(g);
        if (other) out.push(other + ' other account' + (other === 1 ? '' : 's') + ': no reading, limit may not apply');
        return out;
    }

    function unreadCount(g) {
        var c = g.coverage || {};
        return (c.stale_only || 0) + (c.invalid || 0) + (c.conflicting || 0) + (c.reset_passed || 0);
    }

    // The accounts of the view by their key, for names.
    function accountIndex(view) {
        var out = {};
        ((view && view.groups) || []).forEach(function (group) {
            (group.accounts || []).forEach(function (a) { if (a && a.key) out[a.key] = a; });
        });
        return out;
    }

    function accountName(index, key) {
        var a = key ? index[key] : null;
        return a ? String(a.label || a.email || key) : 'an account';
    }

    // The bars of one limit as the skill sends them; an older answer without
    // them falls back on its unnamed current shares.
    function barsOf(g) {
        if (Array.isArray(g.bars)) return g.bars;
        var out = (g.shares || []).map(function (s) {
            return { state: 'current', left: s.left, at_limit: s.at_limit, restricted: s.restricted };
        });
        for (var i = 0; i < unreadCount(g); i++) out.push({ state: 'unknown', left: null, why: 'not_read' });
        return out;
    }

    function barOf(g, accountKey) {
        var found = null;
        barsOf(g).forEach(function (b) { if (b.account && b.account === accountKey) found = b; });
        return found;
    }

    // How many accounts the limit is known to apply to: the scale of its row
    // and of its timeline. It does not move when a reading goes stale.
    function slotCount(g) {
        return typeof g.slots === 'number' ? g.slots : barsOf(g).length;
    }

    var UNKNOWN_WORDS = {
        not_read: 'no usable reading',
        unreadable: 'its reading could not be read',
        sources_disagree: 'its sources disagree',
        reset_passed: 'its window reset after the last reading',
        too_old: 'its last reading is older than the window',
        no_reading_in_answer: 'no reading in this answer'
    };

    function originWords(origin) {
        if (origin === 'history') return 'from the local history';
        if (origin === 'screen') return 'kept from the last answer shown here';
        if (origin === 'cached') return 'from the last answer that read quota';
        return 'reported stale';
    }

    // What one bar says under the pointer and to a screen reader.
    function barWords(bar, index, rowName) {
        var name = accountName(index, bar.account);
        if (bar.state === 'unknown') {
            var last = bar.last_reading;
            return name + ' · ' + rowName + ': unknown — ' + (UNKNOWN_WORDS[bar.why] || 'no usable reading')
                + (last && typeof last.left === 'number' ? '; last read '
                    + exactPct(last.left * 100, last.left <= 0) + '% left ' + (relTime(last.observed_at) || '') : '')
                + ' — unknown, not counted, never a zero';
        }
        var left = Math.max(0, Math.min(1, bar.left));
        var spent = !!bar.at_limit;
        if (bar.state === 'last_known') {
            // What it had when it was read, dated: never a verdict on now.
            var then = name + ': ' + exactPct(left * 100, spent) + '% left when last read'
                + (spent ? ' (at the limit then)' : '') + (bar.restricted ? ' — restricted now' : '');
            if (bar.resets_at) then += ' · reported reset ' + whenWords(bar.resets_at);
            return then + ' · last known, read ' + (relTime(bar.observed_at) || 'at an unreported time')
                + ' (' + originWords(bar.origin) + '), not current';
        }
        var words = name + ': ' + exactPct(left * 100, spent) + '% left'
            + (spent ? ' — at the limit' : '') + (bar.restricted ? ' — restricted now' : '');
        if (bar.resets_at) words += ' · ' + (spent ? 'until ' : 'resets ') + whenWords(bar.resets_at);
        if (bar.observed_at) words += ' · read ' + relTime(bar.observed_at);
        return words;
    }

    // Restrictions that hold an account back now. "Account state not read"
    // is not one of them: it says what is unknown, once, above the rows.
    function heldFlags(bar) {
        return (bar.flags || []).filter(function (f) { return f !== 'account_state_unknown'; });
    }

    function heldNow(bar) {
        return heldFlags(bar).length > 0 || (!bar.flags && !!bar.restricted);
    }

    // Bars are wide enough to point at and keep one width per slot count.
    function barWidth(n) {
        if (n <= 6) return 36;
        if (n <= 12) return 28;
        if (n <= 24) return 24;
        return Math.max(6, Math.floor(600 / n) - 2);
    }

    // One bar per account the limit applies to, as tall as its share left,
    // fullest first in the skill's order: hatched for a dated last-known
    // value, amber when a restriction holds it now, a red base at the limit,
    // an outlined "?" (never a zero) for no usable value. A bar selects its
    // account — the same selection the account list makes — and the bars of
    // that account are marked in every row.
    function barStrip(g, rowName, index) {
        var bars = barsOf(g);
        var n = Math.max(bars.length, 1);
        var gap = n > 24 ? 2 : 3;
        var strip = el('div', 'bars');
        strip.setAttribute('role', 'group');
        strip.setAttribute('aria-label', rowName + ': each account’s share left, fullest first');
        strip.style.gap = gap + 'px';
        strip.style.width = (n * barWidth(n) + (n - 1) * gap) + 'px';
        bars.forEach(function (bar, i) {
            var known = bar.state !== 'unknown' && typeof bar.left === 'number' && isFinite(bar.left);
            var cls = 'bar';
            if (!known) cls += ' unknown';
            if (bar.state === 'last_known') cls += ' last';
            // Red is a current reading at the limit; a last-known zero stays
            // hatched and dated, never current exhaustion.
            if (known && bar.at_limit && bar.state !== 'last_known') cls += ' spent';
            if (known && bar.restricted) cls += heldNow(bar) ? ' held restricted' : ' restricted';
            var isSel = !!bar.account && bar.account === selectedAccountKey;
            if (isSel) cls += ' sel';
            var b = el('button', cls);
            b.setAttribute('type', 'button');
            if (!known) b.appendChild(el('span', 'sr-only', '?'));
            b.setAttribute('data-focus', 'bar:' + g.key + ':' + (bar.account || i));
            b.setAttribute('data-state', bar.state || 'current');
            if (known) {
                var left = Math.max(0, Math.min(1, bar.left));
                if (left > 0) {
                    var f = el('span', 'fill');
                    f.style.height = +(left * 100).toFixed(4) + '%';
                    b.appendChild(f);
                }
            }
            var words = barWords(bar, index, rowName);
            b.title = words;
            b.setAttribute('aria-label', words + (bar.account ? (isSel ? ' — selected' : ' — select this account') : ''));
            b.setAttribute('aria-pressed', isSel ? 'true' : 'false');
            if (bar.account) {
                b.addEventListener('click', function (e) {
                    e.stopPropagation();
                    selectAccount(bar.account === selectedAccountKey ? '' : bar.account,
                        'bar:' + g.key + ':' + bar.account);
                    restoreFocus('bar:' + g.key + ':' + bar.account);
                });
            } else {
                b.disabled = true;
            }
            strip.appendChild(b);
        });
        return strip;
    }

    function selectAccount(key, opener) {
        selectedAccountKey = key || '';
        selectionOpener = selectedAccountKey ? (opener || '') : '';
        diagnosticsOpen = false;
        rerender();
    }

    // Clear and Escape drop the selection and give the keyboard back to the
    // bar or row that selected the account; gone since (another reading, the
    // list folded), to the account's row, the account list or its family —
    // never left on the card the redraw has just removed.
    function clearAccount() {
        var keys = [selectionOpener, 'acct:' + selectedAccountKey, 'accounts', 'harness:' + selectedHarness];
        selectAccount('');
        for (var i = 0; i < keys.length; i++) {
            if (!keys[i]) continue;
            restoreFocus(keys[i]);
            var focused = document.activeElement;
            if (focused && focused.getAttribute && focused.getAttribute('data-focus') === keys[i]) return;
        }
    }

    // The figure: account-windows left now of the accounts the limit applies
    // to, current readings only. With none current it is "—", never 0; a
    // measured 0 (every current account at the limit) is a real 0.
    function rowFigure(g) {
        var m = g.measured || {};
        var lk = g.last_known || {};
        return { any: !!m.accounts, text: m.accounts ? windowsLeft(m) : '—', of: slotCount(g),
                 lastKnown: lk.accounts ? lk : null };
    }

    // The words under a row's bars: who is at the limit or held back, what is
    // unknown, and the next reported reset — with how much it gives back if
    // nothing more is used before it.
    function limitTail(g, index) {
        var out = [];
        var bars = barsOf(g);
        var named = function (b) { return !!(b.account && index[b.account]); };
        var spent = bars.filter(function (b) { return b.state === 'current' && b.at_limit; });
        if (spent.length === 1 && named(spent[0])) {
            out.push({ text: accountName(index, spent[0].account) + ' at the limit'
                + (spent[0].resets_at ? ' until ' + whenWords(spent[0].resets_at) : ''), tone: 'bad' });
        } else if (spent.length) {
            out.push({ text: spent.length + ' at the limit', tone: 'bad' });
        }
        var held = bars.filter(function (b) { return b.state !== 'unknown' && heldNow(b) && !b.at_limit; });
        if (held.length) {
            var flags = {};
            held.forEach(function (b) { heldFlags(b).forEach(function (f) { flags[f] = true; }); });
            var keys = Object.keys(flags);
            var word = keys.length === 1 ? (FLAG_SHORT[keys[0]] || keys[0]) : 'restricted';
            out.push({ text: (held.length === 1 && named(held[0]) ? accountName(index, held[0].account)
                : held.length) + ' ' + word, tone: 'warn' });
        }
        // At the limit when last read: dated and muted, not exhaustion now.
        var wasSpent = bars.filter(function (b) { return b.state === 'last_known' && b.at_limit; });
        if (wasSpent.length) {
            out.push({ text: (wasSpent.length === 1 && named(wasSpent[0]) ? accountName(index, wasSpent[0].account)
                : wasSpent.length) + ' at the limit when last read'
                + (wasSpent.length === 1 ? ' (' + (relTime(wasSpent[0].observed_at) || 'at an unreported time') + ')' : ''),
                tone: '' });
        }
        var unknown = bars.filter(function (b) { return b.state === 'unknown'; }).length;
        if (unknown) out.push({ text: unknown + ' unknown — not counted', tone: '' });
        var m = g.measured || {};
        if (g.next_reset && g.next_reset.at) {
            var n = g.next_reset.accounts || 0;
            var back = g.next_reset_returns >= 0.005 ? amountWords(g.next_reset_returns) : '';
            out.push({ text: 'next reset ' + whenWords(g.next_reset.at)
                + (n > 1 ? ' · ' + n + ' accounts' : '')
                + (back ? ' · +' + back + ' if unused' : ''), tone: '',
                title: 'The soonest reported reset of a current account'
                    + (back ? ': a full refill then gives back the ' + back + ' account-windows '
                        + (n === 1 ? 'it uses' : 'they use') + ' now, if nothing more is used before it' : '') + '.' });
        } else if (m.accounts && g.reset_unknown === m.accounts) {
            out.push({ text: 'no reset time reported', tone: '' });
        }
        return out;
    }

    // "lowest left" names a ranking, and its words say so.
    function tightWords(g) {
        return 'Lowest average share left among this family’s measured limits: a ranking, not a verdict'
            + (scopeWords(g) ? '. It binds only its own ' + scopeWords(g) + ', not the family’s other models' : '')
            + '.';
    }

    // One row per limit: its name, one bar per account, the current figure
    // with the average or the dated last-known line under it, the next
    // reported reset — and one control, in the same slot of every row, that
    // shows its chart below (on the charted row, hides it).
    function limitRow(g, name, chartKey, index, markTightest) {
        var inChart = timelineOpen && g.key === chartKey;
        var row = el('div', 'lrow' + (inChart ? ' sel' : ''));
        var fig = rowFigure(g);
        var m = g.measured || {};
        var words = limitTail(g, index);

        var nameBox = el('div', 'l-name');
        nameBox.appendChild(el('span', 'l-name-text', name));
        if (markTightest) {
            var badge = el('span', 'rs-tight', 'lowest left');
            badge.title = tightWords(g);
            nameBox.appendChild(badge);
        }
        row.appendChild(nameBox);

        // The row's one control carries the row's whole spoken summary: a
        // reader tabbing through the limits hears each one, then what the
        // control does.
        var nameBtn = el('button', 'l-chart' + (inChart ? ' on' : ''), inChart ? 'Hide chart' : 'Show chart');
        nameBtn.insertBefore(icon('chart', 12), nameBtn.firstChild);
        nameBtn.setAttribute('type', 'button');
        nameBtn.setAttribute('data-focus', 'limit:' + g.key);
        nameBtn.setAttribute('aria-pressed', inChart ? 'true' : 'false');
        var spoken = [name];
        if (scopeWords(g)) spoken.push(scopeWords(g));
        spoken.push(fig.any ? fig.text + ' of ' + fig.of + ' account-windows left now' : 'no current reading');
        if (fig.lastKnown) {
            spoken.push('last known ' + num(fig.lastKnown.windows, 2) + ' account-windows, read '
                + (relTime(fig.lastKnown.oldest_observed_at) || 'at an unreported time') + ', not counted');
        }
        spoken.push((m.accounts || 0) + ' current of ' + fig.of + ' accounts');
        words.forEach(function (w) { spoken.push(w.text); });
        restrictionWords(g).forEach(function (w) { spoken.push(w); });
        if (restrictionWords(g).length && num(g.unrestricted_windows, 2)) {
            spoken.push(num(g.unrestricted_windows, 2) + ' unrestricted');
        }
        coverageWords(g).forEach(function (w) { spoken.push(w); });
        if (markTightest) spoken.push('lowest average share left in this family, a ranking, not a verdict');
        var picked = g.key === chartKey;
        nameBtn.setAttribute('aria-label', (inChart ? 'Hide chart' : 'Show chart') + ' — ' + spoken.join(' — ')
            + (inChart ? ' — its timeline is shown below' : ' — show its timeline'));
        nameBtn.title = inChart ? 'Hide the timeline of ' + name : 'Show the timeline of ' + name;
        nameBtn.addEventListener('click', function (e) {
            e.stopPropagation();
            if (!picked) scheduleAll = false;
            chartKeys[g.harness] = g.key;
            // On the charted limit the control folds its timeline or unfolds
            // it; on another it shows that limit's instead.
            timelineOpen = picked ? !timelineOpen : true;
            chartAsked = '';
            chartCursor = null;
            rerender();
        });
        row.appendChild(nameBtn);

        // The figure says its unit by itself: account-windows of the accounts
        // the limit applies to — "10.35 of 19 accounts".
        var figure = el('div', 'l-fig');
        figure.appendChild(el('b', null, fig.text));
        figure.appendChild(el('span', 'of', ' of ' + plural(fig.of, 'account')));
        figure.title = (fig.any ? '' : 'No current reading — not zero. ')
            + 'Account-windows left now: the share each account read now has left of this limit, added up (a '
            + 'full account counts 1), of the ' + plural(fig.of, 'account') + ' it applies to. Last-known and '
            + 'unknown accounts are not counted, never as 0.';
        row.appendChild(figure);

        row.appendChild(barStrip(g, name, index));

        var sub = el('div', 'l-sub');
        if (fig.lastKnown) {
            sub.className = 'l-sub warn';
            sub.appendChild(el('span', 'sw'));
            sub.appendChild(document.createTextNode('Last known ' + num(fig.lastKnown.windows, 2) + ' · '
                + (ageWords(fig.lastKnown.oldest_observed_at) || 'age unknown')));
            sub.title = 'Last known, not current and not in the figure: ' + num(fig.lastKnown.windows, 2)
                + ' account-windows of ' + plural(fig.lastKnown.accounts, 'account') + ', read up to '
                + (relTime(fig.lastKnown.oldest_observed_at) || 'an unreported time') + '. The figure: '
                + (m.accounts ? windowsLeft(m) + ' of ' + plural(m.accounts, 'current account') : 'no current reading') + '.';
        } else if (m.accounts) {
            sub.appendChild(document.createTextNode(leftPct(m.average_remaining_pct) + '% avg left'));
        } else {
            sub.appendChild(document.createTextNode('no current reading'));
        }
        row.appendChild(sub);

        var tail = el('div', 'l-tail');
        words.forEach(function (w, i) {
            if (i) tail.appendChild(el('span', null, ' · '));
            var part = el('span', w.tone ? 'rs-' + w.tone : null, w.text);
            if (w.title) part.title = w.title;
            tail.appendChild(part);
        });
        row.appendChild(tail);
        return row;
    }

    // The one line beside the title: how much of the family is current, what
    // is last known and how old, and whether Claudexor answered at all.
    // Counted over accounts, not limits.
    function reserveStatus(view, summary, groups) {
        var cached = (view && view.cached) || summary.cached || {};
        var cachedAt = cached.quota || cached.accounts || cached.catalog || '';
        var seen = {};
        groups.forEach(function (g) {
            barsOf(g).forEach(function (b, i) {
                var key = b.account || (g.key + '#' + i);
                var rank = b.state === 'current' ? 3 : (b.state === 'last_known' ? 2 : 1);
                if (!seen[key] || seen[key] < rank) seen[key] = rank;
            });
        });
        var keys = Object.keys(seen);
        var current = keys.filter(function (k) { return seen[k] === 3; }).length;
        var last = keys.filter(function (k) { return seen[k] === 2; }).length;
        var unknown = keys.length - current - last;
        var age = readingAge(summary, groups);
        var lkOldest = '';
        groups.forEach(function (g) {
            var o = (g.last_known || {}).oldest_observed_at;
            if (o && (!lkOldest || o < lkOldest)) lkOldest = o;
        });
        if (view && (view.transport_error || view.kept) && cachedAt) {
            return { tone: 'bad', text: 'Claudexor not read now · all as read ' + (relTime(cachedAt) || clockAt(cachedAt)),
                     detail: 'The status read failed. Every value is last known, with its age.' };
        }
        if (cached.quota) {
            return { tone: 'warn', text: 'Quota not read now · last known as read ' + (relTime(cached.quota) || clockAt(cached.quota)),
                     detail: 'The quota facet did not answer; its last answer is shown, dated, never as current.' };
        }
        if (!keys.length) return { tone: 'muted', text: age.text, detail: age.detail };
        if (!last && !unknown) {
            return { tone: age.old ? 'warn' : 'ok',
                     text: (current === 1 ? 'Read' : 'All ' + current + ' read') + ' · ' + age.text.replace(/^Observed /, 'observed '),
                     detail: age.detail };
        }
        var parts = [current + ' current'];
        if (last) parts.push(last + ' last known' + (lkOldest ? ' (' + ageWords(lkOldest) + ')' : ''));
        if (unknown) parts.push(unknown + ' unknown');
        return { tone: 'warn', text: parts.join(' · '), detail: age.detail };
    }

    // The family's accounts that stand in no row at all, by name when few:
    // switched off, or with no reading of these limits here or in the history.
    function outsideWords(groups, family, index, keptAt) {
        if (!family || !(family.accounts || []).length) return '';
        var inBars = {};
        var named = false;
        groups.forEach(function (g) {
            barsOf(g).forEach(function (b) { if (b.account) { inBars[b.account] = true; named = true; } });
        });
        if (!named) return '';
        var missing = family.accounts.filter(function (a) { return !inBars[a.key]; });
        var off = missing.filter(function (a) { return a.enabled === false; });
        var other = missing.filter(function (a) { return a.enabled !== false; });
        function names(list) {
            return list.length <= 3 ? list.map(function (a) { return accountName(index, a.key); }).join(', ')
                : plural(list.length, 'account');
        }
        var out = [];
        if (off.length && keptAt !== undefined && keptAt !== null) {
            var when = relTime(keptAt) || clockAt(keptAt);
            out.push(names(off) + (off.length === 1 ? ' was' : ' were') + ' switched off in Claudexor when last read'
                + (when ? ' (' + when + ')' : '') + ' — not counted.');
        } else if (off.length) {
            out.push(names(off) + (off.length === 1 ? ' is' : ' are') + ' switched off in Claudexor — not counted.');
        }
        if (other.length) {
            out.push(names(other) + ' — no reading of these limits, here or in the history: '
                + (other.length === 1 ? 'it' : 'they') + ' may not apply; not counted, never as zero.');
        }
        return out.join(' ');
    }

    function coverSentence(groups) {
        var counts = groups.map(applicabilityUnknown);
        var most = Math.max.apply(null, [0].concat(counts));
        if (!most) return '';
        if (counts.every(function (c) { return c === counts[0]; })) {
            return plural(most, 'account') + ' with no reading of ' + (groups.length > 1 ? 'these limits' : 'this limit')
                + ' (may not apply): not counted, never as zero.';
        }
        return 'Some accounts have no reading of some limits (may not apply): not counted, never as zero.';
    }

    function rosterWords(summary) {
        var cached = summary.cached || {};
        if (summary.roster === 'cached' && cached.accounts) {
            return 'Account list as read ' + (relTime(cached.accounts) || clockAt(cached.accounts))
                + ' — not read now, so no account’s current state is known.';
        }
        if (summary.roster === 'unknown') {
            return 'Account list not read — accounts with no reading in this answer are not listed.';
        }
        return '';
    }

    // Manual reset credits are the provider's separate count. They are never
    // converted to a window, a currency balance, or a promised refill.
    function creditCount(value) {
        return typeof value === 'number' && isFinite(value) && value >= 0;
    }

    function creditProvenance(credits) {
        var text = credits.observed_at ? 'observed ' + whenWords(credits.observed_at) : 'observation time unavailable';
        if (credits.source) text += ' · source ' + credits.source;
        if (credits.label) text += ' · reported “' + credits.label + '”';
        return text + '. The count does not say which limit a manual reset restores.';
    }

    function renderFamilyCredits(parent, family) {
        var c = family && family.reset_credits;
        if (!c || !(c.current_accounts || c.last_known_accounts || c.unreadable_accounts || c.conflict_accounts)) return;
        var row = el('div', 'reset-credits family-credits');
        row.appendChild(el('strong', null, 'Manual reset credits'));
        var parts = [];
        if (c.current_accounts && creditCount(c.count)) {
            parts.push(String(c.count) + ' reported across ' + plural(c.current_accounts, 'account'));
        }
        if (c.last_known_accounts && creditCount(c.last_known_count)) {
            var age = ageWords(c.last_known_oldest_observed_at);
            parts.push('last known ' + c.last_known_count + ' across ' + plural(c.last_known_accounts, 'account')
                + (age ? ' (' + age + (age === 'just now' ? '' : ' ago') + ')' : ' (age unknown)'));
        }
        if (c.unreadable_accounts) parts.push(c.unreadable_accounts + ' unreadable');
        if (c.conflict_accounts) parts.push(c.conflict_accounts + ' with sources disagreeing');
        if (c.unknown_accounts) parts.push(c.unknown_accounts + ' unknown');
        row.appendChild(document.createTextNode(' · ' + (parts.join(' · ') || 'not reported')));
        row.title = 'Provider-reported manual reset credits, separate from automatic resets. Unknown, unreadable '
            + 'and last-known counts are not included in the current count. No refill amount or currency is assumed.';
        parent.appendChild(row);
    }

    function renderAccountCredits(parent, quota) {
        var c = quota && quota.reset_credits;
        if (!c || c.reason === 'not_reported') return;
        var row = el('div', 'reset-credits account-credits');
        row.appendChild(el('strong', null, 'Manual reset credits'));
        var text;
        if ((c.state === 'current' || c.state === 'last_known') && creditCount(c.count)) {
            text = (c.state === 'last_known' ? 'last known ' : '') + c.count;
            text += c.observed_at ? ' · observed ' + (relTime(c.observed_at) || whenWords(c.observed_at)) : ' · age unknown';
            if (c.state === 'last_known') text += ', not current';
        } else if (c.state === 'unreadable') {
            text = 'count unreadable' + (c.label ? ' · reported “' + c.label + '”' : '');
        } else if (c.state === 'conflict') {
            text = 'unknown · sources disagree';
        } else {
            text = c.reason === 'not_reported' ? 'not reported' : 'unknown · no current reading';
        }
        row.appendChild(document.createTextNode(' · ' + text));
        row.title = creditProvenance(c);
        parent.appendChild(row);
    }

    // The family's limits. Returns what the timeline, the selected account
    // and the account list read from — or null when there is no overview.
    function renderReserve(parent, reserve, family, view) {
        if (!reserve || typeof reserve !== 'object') reserve = {};
        var section = el('section', 'reserve');
        section.setAttribute('aria-label', 'Reserve overview');
        var head = el('div', 'reserve-head');
        var title = el('h3', 'reserve-title', 'Reserve');
        if (family) title.appendChild(el('span', 'reserve-family', ' · ' + familyName(family)));
        // The unit, once, where the figures are read from — not a line of its
        // own above every row; About says it in full.
        title.title = 'Account-windows left: the share each account read now has left of a limit, added up — '
            + 'a full account counts 1, whatever its plan. Limits are never added together.';
        head.appendChild(title);
        var summary = reserve.summary;
        if (!summary) {
            section.appendChild(head);
            renderFamilyCredits(section, family);
            section.appendChild(el('div', 'reserve-note', 'Reserve overview unavailable'
                + (reserve.error ? ' (' + reserve.error + ')' : '') + '. The account list below is unaffected.'));
            parent.appendChild(section);
            return null;
        }
        var hid = family ? family.harness_id : '';
        var groups = reserveGroups({ reserve: reserve }, hid);
        var status = reserveStatus(view, summary, groups);
        var stamp = el('span', 'reserve-age status-' + status.tone);
        stamp.appendChild(el('span', 'pip ' + status.tone));
        stamp.appendChild(el('span', null, status.text));
        stamp.title = status.detail || '';
        head.appendChild(stamp);
        section.appendChild(head);
        renderFamilyCredits(section, family);
        var reads = summary.reads || {};
        var ctx = { reserve: reserve, summary: summary, groups: groups, names: {}, index: accountIndex(view) };
        if (reads.quota && reads.quota !== 'ok' && !(summary.cached || {}).quota && !groups.length) {
            section.appendChild(el('div', 'reserve-note', 'Quota was not read on this answer, and no earlier '
                + 'reading is kept in this session, so nothing is summed.'));
            parent.appendChild(section);
            return ctx;
        }
        if (!groups.length) {
            section.appendChild(el('div', 'reserve-note', 'No quota limit reported for '
                + (family ? familyName(family) : 'this family') + '.'));
            parent.appendChild(section);
            return ctx;
        }
        ctx.names = limitNames(groups);
        var chartKey = chartKeyFor(hid, groups);
        var measuredGroups = groups.filter(function (g) { return g.measured && g.measured.accounts; }).length;
        var table = el('div', 'lrows');
        table.setAttribute('role', 'group');
        table.setAttribute('aria-label', 'Limits of ' + (family ? familyName(family) : 'this family'));
        groups.forEach(function (g) {
            table.appendChild(limitRow(g, ctx.names[g.key], chartKey, ctx.index, g.tightest && measuredGroups > 1));
            // The timeline of the charted limit stands right under its row;
            // folded, it leaves nothing behind — the row's control says "Show chart".
            if (g.key === chartKey && timelineOpen) table.appendChild(timelineBlock(ctx, g));
        });
        section.appendChild(table);
        var keptAt = (view && view.kept) || summary.roster === 'cached'
            ? String((summary.cached || {}).accounts || '') : null;
        // Who stands in no row, by name, is said in the account list; here a
        // short line counts them and opens it.
        ctx.cover = [outsideWords(groups, family, ctx.index, keptAt) || coverSentence(groups), rosterWords(summary)]
            .filter(Boolean);
        var outside = outsideCount(groups, family);
        if (ctx.cover.length) {
            var link = el('button', 'cover-link', (outside ? plural(outside, 'account') + ' in no row'
                : 'Coverage') + ' · see Accounts');
            link.setAttribute('type', 'button');
            link.setAttribute('data-focus', 'cover');
            link.setAttribute('aria-label', ctx.cover.join(' ') + ' — open the account list');
            link.title = ctx.cover.join(' ');
            link.addEventListener('click', function (e) {
                e.stopPropagation();
                accountsOpen = true;
                rerender();
                restoreFocus('accounts');
            });
            section.appendChild(link);
        }
        parent.appendChild(section);
        return ctx;
    }

    // How many of the family's accounts stand in no row at all.
    function outsideCount(groups, family) {
        if (!family) return 0;
        var inBars = {};
        groups.forEach(function (g) { barsOf(g).forEach(function (b) { if (b.account) inBars[b.account] = true; }); });
        if (!Object.keys(inBars).length) return 0;
        return (family.accounts || []).filter(function (a) { return !inBars[a.key]; }).length;
    }

    /* ------------------------------------------------------------------
       The timeline: the record behind now, one future ahead of it.
       ------------------------------------------------------------------ */

    function scenarioName(key) {
        for (var i = 0; i < SCENARIOS.length; i++) if (SCENARIOS[i].key === key) return SCENARIOS[i].name;
        return key;
    }

    // The scenario drawn: the reader's pick, from the skill's chart. None on
    // a chart with no current reading (a kept screen, or nothing current).
    function scenarioOf(chart) {
        var all = chart && chart.scenarios;
        return all && all[scenario] ? all[scenario] : null;
    }

    function segControl(label, options, current, focusPrefix, onPick) {
        var seg = el('div', 'seg');
        seg.setAttribute('role', 'group');
        seg.setAttribute('aria-label', label);
        seg.appendChild(el('span', 'seg-label', label));
        options.forEach(function (opt) {
            var on = opt.key === current;
            var b = el('button', 'seg-opt' + (on ? ' active' : ''), opt.name);
            b.setAttribute('type', 'button');
            b.setAttribute('aria-pressed', on ? 'true' : 'false');
            if (opt.spoken) b.setAttribute('aria-label', opt.spoken);
            if (opt.title) b.title = opt.title;
            b.setAttribute('data-focus', focusPrefix + opt.key);
            b.addEventListener('click', function (e) {
                e.stopPropagation();
                onPick(opt.key);
            });
            seg.appendChild(b);
        });
        return seg;
    }

    // The timeline of the charted limit, right under its row (its one toggle
    // is the row's control): its span and its future; the chart; under it
    // the legend — whose total each line is, and what the future assumes —
    // where that future stands a few moments ahead, the next reported
    // resets, and the details folded.
    function timelineBlock(ctx, g) {
        var box = el('div', 'tl-block');
        box.setAttribute('role', 'group');
        box.setAttribute('aria-label', 'Timeline of ' + ctx.names[g.key]);
        var name = ctx.names[g.key];
        var horizon = horizonFor(g);
        var controls = el('div', 'tl-controls');
        controls.appendChild(segControl('Span', HORIZONS.map(function (h) {
            return { key: h, name: h === '24h' ? '24 h' : '7 days',
                     spoken: (h === '24h' ? '24 hours' : '7 days') + ' back and ahead' };
        }), horizon, 'horizon:', function (picked) {
            horizonChoice = picked;
            chartCursor = null;
            rerender();
        }));
        controls.appendChild(segControl('Future', SCENARIOS.map(function (s) {
            return { key: s.key, name: s.name, title: s.key === 'no_new_use'
                ? 'Every account read now keeps its share and refills once at its next reported reset'
                : 'The accounts with a measured recent pace keep it; the others are held' };
        }), scenario, 'scenario:', function (picked) {
            scenario = picked;
            rerender();
        }));
        box.appendChild(controls);
        if (scopeWords(g)) box.appendChild(el('div', 'tl-scope', scopeWords(g)));

        var chart = ctx.reserve.chart;
        if (!chart || chart.group_key !== g.key || chart.horizon !== horizon) {
            box.appendChild(el('div', 'reserve-note chart-wait', 'Loading the timeline for this limit…'));
            return box;
        }
        if (!chart.y_max) {
            box.appendChild(el('div', 'reserve-note', 'No account known for this limit: nothing to draw.'));
            return box;
        }
        var sc = scenarioOf(chart);
        drawChart(box, name, chart, sc);
        var facts = timelineFacts(g, chart, sc, ctx);
        box.appendChild(chartLegend(chart, sc, facts.lead));
        if (facts.shown.length) {
            var said = el('div', 'tl-facts');
            facts.shown.forEach(function (f) { said.appendChild(el('div', f[1] || null, f[0])); });
            box.appendChild(said);
        }
        if (sc) {
            var cp = checkpointLine(sc, chart);
            if (cp) box.appendChild(cp);
        }
        if (sc) {
            box.appendChild(scheduleTable(sc, chart, horizon));
            var note = scheduleNote(g, sc, ctx, horizon);
            if (note) box.appendChild(note);
        }
        box.appendChild(detailsBlock(g, chart, sc, ctx, facts.notes));
        return box;
    }

    // Who each line sums, one line each under the chart — the record's
    // accounts, and the future's with what it assumes (timelineFacts) — so a
    // step between them at now is read as two bases, never as use. With no
    // future drawn, the second line says why.
    function chartLegend(chart, sc, lead) {
        var legend = el('div', 'chart-legend');
        function item(cls, text) {
            var li = el('span', 'legend-item');
            if (cls) li.appendChild(legendSwatch(cls));
            li.appendChild(el('span', null, text));
            legend.appendChild(li);
        }
        var recorded = chart.past_accounts || 0;
        if (chart.history && recordDrawn(recordLine(chart))) {
            item('line-observed', 'history · solid: recorded; dashed: carried · breaks: membership changes');
        } else if (!chart.history && recorded && recordDrawn(chart.past)) {
            item('line-observed', 'recorded · ' + plural(recorded, 'account')
                + (chart.past_basis === 'last_known' ? ', none current now' : ''));
        } else {
            item('', 'no record in this span yet');
        }
        item(sc ? 'line-scenario' : '', lead);
        return legend;
    }

    // Whether the record draws anything: a value held over a stretch of time,
    // up to the next vertex (stepRuns). A value alone at its instant — the
    // figure at now, one followed at once by a gap — draws nothing.
    function recordDrawn(past) {
        past = past || [];
        for (var i = 0; i + 1 < past.length; i++) {
            if (past[i][1] !== null && past[i][1] !== undefined && past[i + 1][0] > past[i][0]) return true;
        }
        return false;
    }

    function legendSwatch(cls) {
        var svg = svgCanvas('0 0 22 10', 22, 10);
        svg.appendChild(svgEl('line', { x1: 2, y1: 5, x2: 20, y2: 5, 'class': cls }));
        return svg;
    }

    // The future line's legend entry (lead): its name, whom it covers and what
    // it assumes, in one line — the only place that says so; in pace mode one
    // more line: who runs out, when. Every longer caveat is a note under
    // Details. With no future drawn, the lead says why.
    function timelineFacts(g, chart, sc, ctx) {
        var shown = [];
        var notes = [];
        var lead;
        var notCurrent = Math.max(0, (chart.y_max || 0) - (chart.accounts || 0));
        if (!sc) {
            lead = chart.kept
                ? 'no future · nothing read since ' + clockAt(isoOf(chart.now))
                : chart.history ? 'no future · no current reading; history stays dated'
                    : 'no future · no current reading; the record is of '
                        + plural(chart.past_accounts || 0, 'account') + ' shown as last known';
            return { lead: lead, shown: shown, notes: notes };
        }
        var span = spanWords((g.recent_pace || {}).span_min_seconds, (g.recent_pace || {}).span_max_seconds);
        var scenarioWord = scenarioName(scenario).toLowerCase() + ' · ';
        if (scenario === 'no_new_use') {
            lead = scenarioWord + plural(sc.accounts, 'current account') + (sc.accounts === 1
                ? ' keeps its share, refilled once at its next reported reset; no later reset is assumed'
                : ' keep their share, each refilled once at its next reported reset; no later reset is assumed');
        } else if (!sc.at_pace) {
            lead = scenarioWord + plural(sc.accounts, 'current account')
                + (sc.accounts === 1 ? ' held at its share' : ' held at their shares')
                + '; each refilled once at its next reported reset; no later reset is assumed. '
                + 'No account has a measured pace yet (' + paceStateWords(g) + '): the same as no new use';
        } else {
            lead = scenarioWord + sc.at_pace + ' of ' + sc.accounts + ' current at their pace of the last ' + span
                + (sc.held ? ', ' + sc.held + ' held' : '') + '; each refilled once at its next reported reset; '
                + 'no later reset is assumed';
            var runs = runsOutWords(sc, ctx);
            if (runs) shown.push([runs, 'warn']);
            notes.push('Recent pace is a condition, not an estimate of use nobody observed: the accounts whose pace '
                + 'qualified continue it (after their refill too), the others are held at their share.');
            if (sc.zero_growth) {
                notes.push(plural(sc.zero_growth, 'account') + ' showed no net change in that span — not proof of zero use.');
            }
        }
        notes.push('Only each account’s next reported reset is applied — no later one is assumed, however short '
            + 'the window — and reset times are as reported now and may move.');
        if (notCurrent) {
            notes.push(plural(notCurrent, 'account') + ' with no current reading (last known or unknown) '
                + (notCurrent === 1 ? 'is' : 'are') + ' in no future: nothing is projected from a last-known value.');
        }
        var recorded = chart.past_accounts || 0;
        if (chart.history && !recordDrawn(recordLine(chart))) {
            notes.push('No record in this span yet: the future starts from the row’s figure at now.');
        } else if (chart.history) {
            notes.push('History keeps the last known value of each recorded account through temporary missing readings. '
                + 'The future starts separately from the fresh-only row figure; a difference at now is not consumption.');
        } else if (chart.past_basis !== 'current' || recorded !== chart.accounts) {
            notes.push(recorded
                ? 'The record sums ' + plural(recorded, 'account') + ' with a record in this span; the future starts '
                    + 'from the ' + plural(chart.accounts, 'account') + ' read now (the row’s figure), so the two '
                    + 'lines need not meet at now.'
                : 'No record in this span yet: the future starts from the row’s figure at now.');
        }
        return { lead: lead, shown: shown, notes: notes };
    }

    function paceStateWords(g) {
        var pace = g.recent_pace || {};
        if (pace.state === 'warming_up') return 'warming up: it needs 15 minutes of comparable readings within the last hour';
        if (pace.state === 'unavailable') return 'the local history could not be read';
        return 'no comparable readings in the last hour';
    }

    // When accounts run out at this pace, inside what is drawn.
    function runsOutWords(sc, ctx) {
        var before = (sc.runs_out || []).filter(function (r) { return !r.after_refill && r.reset_reported; });
        var none = (sc.runs_out || []).filter(function (r) { return !r.reset_reported; });
        var again = (sc.runs_out || []).filter(function (r) { return r.after_refill; });
        var parts = [];
        if (before.length) {
            parts.push(plural(before.length, 'account') + ' would run out before ' + (before.length === 1 ? 'its' : 'their')
                + ' reported reset, the first ' + whenWords(isoOf(before[0].at)));
        }
        if (none.length) {
            parts.push(plural(none.length, 'account') + ' with no reported reset would run out, the first '
                + whenWords(isoOf(none[0].at)));
        }
        if (again.length) {
            parts.push(again.length + ' again after ' + (again.length === 1 ? 'its' : 'their') + ' refill, the first '
                + whenWords(isoOf(again[0].at)));
        }
        return parts.length ? 'At this pace ' + parts.join('; ') + '.' : '';
    }

    // The reset schedule of the scenario drawn: when, how many accounts,
    // what each event gives back in this scenario, and the total after it.
    function scheduleTable(sc, chart, horizon) {
        var table = el('table', 'schedule');
        table.appendChild(el('caption', null, 'Reported resets · ' + scenarioName(scenario).toLowerCase()));
        var head = el('tr');
        [['When', ''], ['Accounts', 'n'], ['Back', 'n'], ['Total after', 'n']].forEach(function (h) {
            var th = el('th', h[1] || null, h[0]);
            th.setAttribute('scope', 'col');
            head.appendChild(th);
        });
        var thead = el('thead');
        thead.appendChild(head);
        table.appendChild(thead);
        var body = el('tbody');
        var rows = sc.schedule || [];
        var shown = scheduleAll || rows.length <= SCHEDULE_ROWS ? rows : rows.slice(0, SCHEDULE_ROWS);
        shown.forEach(function (row) {
            var tr = el('tr');
            var iso = isoOf(row.at);
            var when = el('td', null, eventWhen(row));
            when.title = momentWords(row.at) + (row.last && row.last !== row.at ? '–' + clockAt(isoOf(row.last)) : '')
                + ' · ' + relTime(iso);
            tr.appendChild(when);
            tr.appendChild(el('td', 'n', String(row.accounts)));
            tr.appendChild(el('td', 'n', '+' + amountWords(row.adds)));
            tr.appendChild(el('td', 'n', num(row.total_after, 2) + ' of ' + chart.y_max));
            body.appendChild(tr);
        });
        if (rows.length > SCHEDULE_ROWS) {
            var last = rows[rows.length - 1];
            var moreRow = el('tr');
            var moreCell = el('td');
            moreCell.setAttribute('colspan', '4');
            var more = el('button', 'pill-btn schedule-more', scheduleAll ? 'Show the first ' + SCHEDULE_ROWS
                : plural(rows.length - shown.length, 'more reset') + ' to ' + eventWhen(last) + ' · '
                    + num(last.total_after, 2) + ' of ' + chart.y_max + ' after the last');
            more.setAttribute('type', 'button');
            more.setAttribute('aria-expanded', scheduleAll ? 'true' : 'false');
            more.setAttribute('data-focus', 'schedule-all');
            more.addEventListener('click', function (e) {
                e.stopPropagation();
                scheduleAll = !scheduleAll;
                rerender();
                restoreFocus('schedule-all');
            });
            moreCell.appendChild(more);
            moreRow.appendChild(moreCell);
            body.appendChild(moreRow);
        }
        if (!rows.length) {
            var tr = el('tr');
            var td = el('td', null, sc.accounts === sc.no_reset
                ? 'No current account reports a reset time.'
                : 'No reported reset within the next ' + (horizon === '7d' ? '7 days' : '24 hours')
                    + (sc.later ? ' — the next is ' + whenWords(isoOf(sc.later.at)) + ' (' + plural(sc.later.accounts, 'account')
                        + ')' + (horizon === '24h' ? '; the 7-day span shows how much' : '') : '') + '.');
            td.setAttribute('colspan', '4');
            tr.appendChild(td);
            body.appendChild(tr);
        }
        table.appendChild(body);
        return table;
    }

    // How many events the schedule lists before one row offers the rest.
    var SCHEDULE_ROWS = 2;

    // When an event is: resets reported within two minutes of each other are
    // one event, said as the span they cover when it shows on the clock.
    function eventWhen(row) {
        var first = whenWords(isoOf(row.at));
        if (!row.last || clockAt(isoOf(row.last)) === clockAt(isoOf(row.at))) return first;
        return first + '–' + clockAt(isoOf(row.last));
    }

    // One short line under the schedule: what it cannot say — accounts with
    // no reported reset, resets past the span — and that a reset refills only
    // this limit while another can still hold the account back.
    function scheduleNote(g, sc, ctx, horizon) {
        var parts = [];
        if (sc.later && (sc.schedule || []).length) {
            parts.push('then ' + whenWords(isoOf(sc.later.at)) + ' and later, past this span');
        }
        if (sc.no_reset) {
            parts.push(sc.no_reset + ' report' + (sc.no_reset === 1 ? 's' : '') + ' no reset: no refill assumed');
        }
        var others = ctx.groups.filter(function (x) { return x.key !== g.key; });
        if (others.length && (sc.schedule || []).length) {
            var spent = others.filter(function (x) { return (x.measured || {}).at_limit; });
            parts.push('another limit can still hold an account'
                + (spent.length ? ' (' + spent.map(function (x) { return ctx.names[x.key] + ': ' + x.measured.at_limit
                    + ' at the limit'; }).join('; ') + ')' : ''));
        }
        if (!parts.length) return null;
        var line = parts.join(' · ');
        return el('div', 'schedule-notes', line.charAt(0).toUpperCase() + line.slice(1) + '.');
    }

    // Approximately how much is left at a few moments ahead, in this scenario.
    function checkpointLine(sc, chart) {
        var points = sc.checkpoints || [];
        if (!points.length) return null;
        var line = el('div', 'checkpoints');
        line.appendChild(document.createTextNode(scenario === 'no_new_use'
            ? 'Left if nothing more is used: ' : 'Left at recent pace: '));
        points.forEach(function (cp, i) {
            if (i) line.appendChild(document.createTextNode(' · '));
            line.appendChild(document.createTextNode(offsetWords(cp.after_seconds) + ' '));
            line.appendChild(el('b', null, '≈ ' + num(cp.value, 2)));
        });
        line.appendChild(document.createTextNode(' (of ' + chart.y_max + ')'));
        return line;
    }

    // What the collector's watch vouches for: where the unbroken watch began,
    // or only a lower bound.
    function watchWords(history) {
        var w = (history || {}).unbroken_watch;
        if (!w || !w.since) return '';
        if (w.exact) return 'watched without a break since ' + clockAt(w.since);
        return 'watched without a break at least since ' + clockAt(w.since);
    }

    function recordWords(history) {
        var h = history || {};
        if (h.state === 'unavailable') {
            return 'Local history unavailable' + (h.error ? ' (' + h.error + ')' : '') + ': nothing recorded can be drawn';
        }
        if (!h.collecting_since) return 'Nothing recorded yet';
        return 'Recorded since ' + formatResetAt(h.collecting_since) + ' (kept up to 14 days'
            + (h.capped_before ? '; older rows dropped by the size cap' : '') + ')';
    }

    function paceWords(g, history) {
        var pace = g.recent_pace || {};
        var out = [];
        if (pace.state === 'ok' || pace.state === 'partial') {
            var words = (pace.state === 'ok' ? 'Recent pace: ' : 'Recent pace (at least): ')
                + num(pace.windows_per_hour, 3) + ' account-windows/h from '
                + pace.accounts_known + ' of ' + pace.of + ' accounts, over '
                + spanWords(pace.span_min_seconds, pace.span_max_seconds) + ' of readings';
            out.push(words + '.');
            if (pace.zero_growth) {
                out.push(pace.zero_growth === pace.accounts_known
                    ? 'No net change observed in that span — not proof of zero use, and not a promise that the reserve lasts.'
                    : plural(pace.zero_growth, 'account') + ' showed no net change in that span (not proof of zero use).');
            }
            if (pace.state === 'partial') out.push('The rest have no comparable readings in the last hour yet.');
        } else if (pace.state === 'warming_up') {
            var watched = watchWords(history);
            out.push('Recent pace: warming up — it needs at least 15 minutes of comparable watched readings within the trailing hour'
                + (watched ? ' (' + watched + ')' : '') + '.');
        } else if (pace.state === 'unavailable') {
            out.push('Recent pace unavailable: the local history could not be read.');
        } else if (pace.state === 'insufficient') {
            out.push('Recent pace: not enough comparable readings in the last hour (a reset, a gap or too few readings).');
        }
        if (pace.exhaust_before_reset) {
            out.push('At that pace ' + plural(pace.exhaust_before_reset, 'account') + ' would run out before the reported reset'
                + (pace.earliest_exhaustion_at ? ', the first ' + formatResetAt(pace.earliest_exhaustion_at) : '') + '.');
        }
        if (pace.reach_limit_no_reported_reset) {
            out.push('At that pace ' + plural(pace.reach_limit_no_reported_reset, 'account')
                + ' with no reported reset would reach the limit'
                + (pace.earliest_no_reset_reach_at ? ', the first ' + formatResetAt(pace.earliest_no_reset_reach_at) : '') + '.');
        }
        return out;
    }

    // A limit's details: every figure the row folds away, one line each.
    function reserveDetail(g, history) {
        var dl = el('dl', 'reserve-detail');
        function item(label, value) {
            if (!value || (Array.isArray(value) && !value.length)) return;
            dl.appendChild(el('dt', null, label));
            var dd = el('dd');
            (Array.isArray(value) ? value : [value]).forEach(function (line) { dd.appendChild(el('div', null, line)); });
            dl.appendChild(dd);
        }
        var m = g.measured || {};
        item('Scope', scopeWords(g));
        item('Left now', m.accounts
            ? windowsLeft(m) + ' account-windows of ' + plural(m.accounts, 'measured account')
                + ' · ' + leftPct(m.average_remaining_pct) + '% left on average'
            : 'no account with a fresh reading, so nothing is summed');
        if (m.at_limit) item('At the limit', plural(m.at_limit, 'account'));
        var limited = restrictionWords(g);
        if (limited.length) {
            item('Restricted now', limited.join(' · ')
                + (num(g.unrestricted_windows, 2) ? ' · ' + num(g.unrestricted_windows, 2) + ' unrestricted' : ''));
        }
        item('Not counted', coverageWords(g).join(' · '));
        if (g.plans && g.plans.mixed) {
            item('Plans', ['mixed plans'].concat((g.plans.breakdown || []).map(function (p) {
                return p.plan + ': ' + num(p.windows, 2) + ' account-windows across ' + plural(p.accounts, 'account');
            })));
        }
        if (g.possible_duplicates) item('Shared sign-in', g.possible_duplicates + ' share a sign-in (they may draw on one pool)');
        var resets = [];
        if (g.next_reset && g.next_reset.at) {
            resets.push('next ' + formatResetAt(g.next_reset.at)
                + (g.next_reset.accounts > 1 ? ' (' + g.next_reset.accounts + ' accounts)' : ''));
        }
        if (g.reset_unknown) {
            resets.push(g.reset_unknown + ' with no reported reset: when ' + (g.reset_unknown === 1 ? 'it refills' : 'they refill')
                + ' is unknown');
        }
        item('Resets', resets.join(' · '));
        if (g.pace_to_reset) {
            item('Even use', 'to each account’s reported reset: ' + num(g.pace_to_reset.windows_per_hour, 3)
                + ' account-windows/h across ' + plural(g.pace_to_reset.accounts, 'account'));
        }
        item('Pace', paceWords(g, history));
        return dl;
    }

    // The limit's details, the notes and the data table, folded under one
    // summary: the diagnostics behind the timeline, never thrown away.
    function detailsBlock(g, chart, sc, ctx, extraNotes) {
        var details = el('details', 'chart-table');
        if (detailsOpen) details.open = true;
        details.addEventListener('toggle', function () { detailsOpen = !!details.open; });
        var summary = el('summary', null, 'Details, notes and data');
        summary.setAttribute('data-focus', 'chart-table');
        details.appendChild(summary);
        details.appendChild(reserveDetail(g, ctx.summary.history));
        var notes = el('div', 'chart-notes');
        (extraNotes || []).forEach(function (line) { notes.appendChild(el('p', null, line)); });
        var history = ctx.summary.history || {};
        var watched = watchWords(history);
        notes.appendChild(el('p', null, recordWords(history) + (watched ? '; ' + watched : '') + ' · times in ' + tzWords() + '.'));
        if (chart.history) {
            notes.appendChild(el('p', null, 'History starts each account at its first recorded value; older values are '
                + 'never invented. A dashed segment carries dated readings through missing or stale answers. A '
                + 'reported reset passing keeps its dated pre-reset value, not an assumed refill. Hover or use the '
                + 'arrow keys for the count, age and provenance at a moment. Carried values never teach recent pace.'));
            if (chart.history.membership_note) notes.appendChild(el('p', null, chart.history.membership_note));
        } else {
            notes.appendChild(el('p', null, 'The record sums every account of this limit with a record in this range ('
                + (chart.past_accounts || 0) + ' of ' + (chart.y_max || 0) + '), whatever its reading now, and stops '
                + 'wherever any of them was not vouched for — an outage, a sweep that did not see it, a reset, a stale '
                + 'reading — never drawing a zero there. The scale is every account the limit applies to.'));
        }
        var clippedBefore = chart.history ? chart.history.clipped_before : chart.past_clipped_before;
        if (clippedBefore) {
            notes.appendChild(el('p', null, 'Changes before ' + momentWords(clippedBefore)
                + ' are not drawn: more were recorded in this range than one chart holds.'));
        }
        var points = chart.points || [];
        if (points.length && chart.history) {
            notes.appendChild(el('p', null, plural(points.length, 'total') + ' seen at one sweep only are listed '
                + 'separately below. A settled value can be carried in history; unsettled sources supply no replacement value.'));
        } else if (points.length) {
            notes.appendChild(el('p', null, plural(points.length, 'total') + ' seen at one sweep only — a reading seen '
                + 'once, or sources that disagree at one sweep — are listed in the table, not drawn: each holds for '
                + 'no stretch of time.'));
        }
        (chart.assumptions || []).forEach(function (a) {
            if (chart.history && (a === chart.history.membership_note || /^History:/.test(a))) return;
            notes.appendChild(el('p', null, a));
        });
        details.appendChild(notes);
        details.appendChild(dataTable(chart));
        var seen = sightingsTable(chart);
        if (seen) details.appendChild(seen);
        return details;
    }

    // The table is a sample of the recorded line (with the beginnings of up to
    // 8 gaps its stride missed, not every gap) and the scenarios at reported
    // resets and a quarter, half and all of the span. The sightings in
    // chart.table are only the newest few: every one of them is in the
    // sightings table instead.
    function dataTable(chart) {
        if (chart.history) return historyDataTable(chart);
        var rows = (chart.table || []).filter(function (row) {
            return row.observed !== undefined && row.event !== 'now' && !row.sighting;
        });
        var recorded = typeof chart.past_points === 'number' ? chart.past_points : Math.max(0, (chart.past || []).length - 1);
        var box = el('div');
        box.appendChild(el('p', 'chart-notes', 'The table lists ' + rows.length + ' of ' + recorded
            + ' recorded changes; the chart and its cursor use all of them.'));
        var table = el('table');
        var head = el('tr');
        ['Time', 'Recorded', 'No new use', 'Recent pace', 'Event'].forEach(function (h) {
            var th = el('th', null, h);
            th.setAttribute('scope', 'col');
            head.appendChild(th);
        });
        var thead = el('thead');
        thead.appendChild(head);
        table.appendChild(thead);
        var body = el('tbody');
        function cell(value) { return value === undefined || value === null ? '' : num(value, 2); }
        (chart.table || []).forEach(function (row) {
            if (row.sighting) return;
            var tr = el('tr');
            var at = Date.parse(String(row.at || ''));
            [isFinite(at) ? momentWords(at / 1000) : row.at,
             row.observed === undefined ? '' : (row.observed === null ? 'gap' : num(row.observed, 2)),
             cell(row.scenario_no_new_use),
             cell(row.scenario_recent_pace),
             (row.event || '') + (row.until ? ' (to ' + clockAt(row.until) + ')' : '')
            ].forEach(function (text) { tr.appendChild(el('td', null, text)); });
            body.appendChild(tr);
        });
        table.appendChild(body);
        box.appendChild(table);
        return box;
    }

    function historyDataTable(chart) {
        var details = chart.history.details || [];
        var stride = Math.max(1, Math.ceil(details.length / 60));
        var recorded = details.filter(function (d, i) {
            return d && (i % stride === 0 || i === details.length - 1 || d.change);
        });
        var rows = recorded.map(function (d) {
            return { at: d.at, history: d, event: historyWords(d, d.at).join('; ') };
        });
        (chart.table || []).forEach(function (row) {
            var at = Date.parse(String(row.at || '')) / 1000;
            if (!isFinite(at) || at < chart.now
                    || (row.scenario_no_new_use === undefined && row.scenario_recent_pace === undefined)) return;
            rows.push({ at: at, noUse: row.scenario_no_new_use, pace: row.scenario_recent_pace,
                event: row.event === 'now' ? 'Future starts from current readings only' : (row.event || '') });
        });
        rows.sort(function (a, b) { return a.at - b.at; });
        var box = el('div');
        box.appendChild(el('p', 'chart-notes', 'The table lists ' + recorded.length + ' of ' + details.length
            + ' recorded changes; the chart and its cursor use all of them. Carried ages are at the listed time.'));
        var table = el('table'), thead = el('thead'), head = el('tr');
        ['Time', 'History', 'No new use', 'Recent pace', 'Context'].forEach(function (text) {
            var th = el('th', null, text); th.setAttribute('scope', 'col'); head.appendChild(th);
        });
        thead.appendChild(head); table.appendChild(thead);
        var body = el('tbody');
        rows.forEach(function (row) {
            var d = row.history;
            var tr = el('tr');
            [momentWords(row.at), d ? (d.value === null ? 'gap' : num(d.value, 2) + ' of ' + d.accounts) : '',
             num(row.noUse, 2), num(row.pace, 2), row.event].forEach(function (text) {
                tr.appendChild(el('td', null, text));
            });
            body.appendChild(tr);
        });
        table.appendChild(body); box.appendChild(table);
        return box;
    }

    // How many sightings one page of their table shows.
    var SIGHTING_ROWS = 20;

    // Every total seen at one sweep only (chart.points), in time order, a page
    // at a time: the newest page first, Older and Newer to the rest. The page
    // is counted back from the newest and kept through the 30-second redraw (a
    // new sighting moves each older page on by one row); another limit or span
    // starts again at the newest. Nothing more is asked of the host.
    function sightingsTable(chart) {
        var points = chart.points || [];
        // Another chart, even one with no sightings yet, is another list.
        var of = chart.group_key + ' ' + chart.horizon;
        if (of !== sightingsOf) {
            sightingsOf = of;
            sightingsBack = 0;
        }
        if (!points.length) return null;
        var pages = Math.ceil(points.length / SIGHTING_ROWS);
        sightingsBack = Math.max(0, Math.min(sightingsBack, pages - 1));
        var hi = points.length - sightingsBack * SIGHTING_ROWS;
        var lo = Math.max(0, hi - SIGHTING_ROWS);
        var box = el('div', 'sightings');
        var table = el('table');
        table.appendChild(el('caption', null, 'Seen at one sweep only · ' + (lo + 1) + '–' + hi + ' of ' + points.length));
        var head = el('tr');
        ['Time', 'Total', 'What was seen'].forEach(function (h) {
            var th = el('th', null, h);
            th.setAttribute('scope', 'col');
            head.appendChild(th);
        });
        var thead = el('thead');
        thead.appendChild(head);
        table.appendChild(thead);
        var body = el('tbody');
        points.slice(lo, hi).forEach(function (p) {
            var unsettled = p[1] === null || p[1] === undefined;
            var tr = el('tr');
            [momentWords(p[0]),
             unsettled ? 'not settled' : num(p[1], 2),
             unsettled ? 'sources disagree at this sweep' : 'seen at this sweep only'
            ].forEach(function (text) { tr.appendChild(el('td', null, text)); });
            body.appendChild(tr);
        });
        table.appendChild(body);
        box.appendChild(table);
        if (pages > 1) {
            var nav = el('div', 'sightings-nav');
            nav.appendChild(sightingsTurn('Older', 'Older sightings', 'sightings:older', 1, sightingsBack < pages - 1));
            nav.appendChild(sightingsTurn('Newer', 'Newer sightings', 'sightings:newer', -1, sightingsBack > 0));
            box.appendChild(nav);
        }
        return box;
    }

    // A page button. At its end it says so and does nothing, but keeps the
    // keyboard: a disabled button would drop focus to the page.
    function sightingsTurn(label, spoken, key, step, can) {
        var btn = el('button', 'pill-btn', label);
        btn.setAttribute('type', 'button');
        btn.setAttribute('aria-label', spoken);
        btn.setAttribute('data-focus', key);
        if (!can) btn.setAttribute('aria-disabled', 'true');
        btn.addEventListener('click', function (e) {
            e.stopPropagation();
            if (!can) return;
            sightingsBack += step;
            rerender();
            restoreFocus(key);
        });
        return btn;
    }

    /* ------------------------------------------------------------------
       The chart itself.
       ------------------------------------------------------------------ */

    function lineAt(points, t) {
        if (!points || !points.length) return null;
        if (t < points[0][0] || t > points[points.length - 1][0]) return null;
        var value = null;
        for (var i = 0; i + 1 < points.length; i++) {
            var a = points[i], b = points[i + 1];
            if (a[0] <= t && t <= b[0]) {
                value = b[0] === a[0] ? b[1] : a[1] + (b[1] - a[1]) * (t - a[0]) / (b[0] - a[0]);
                if (t < b[0]) break;
            }
        }
        return value;
    }

    // The recorded total at t: each vertex holds until the next; null is a
    // gap; before the first vertex there is no record.
    function recordLine(chart) {
        return chart.history ? (chart.history.line || []) : (chart.past || []);
    }

    function recordIndex(line, t) {
        var past = line || [];
        var lo = 0;
        var hi = past.length - 1;
        var found = -1;
        while (lo <= hi) {
            var mid = (lo + hi) >> 1;
            if (past[mid][0] <= t) { found = mid; lo = mid + 1; } else { hi = mid - 1; }
        }
        return found;
    }

    function observedAt(chart, t) {
        var line = recordLine(chart);
        var found = recordIndex(line, t);
        return found < 0 ? null : line[found][1];
    }

    function historyDetailAt(chart, t) {
        if (!chart.history) return null;
        var found = recordIndex(recordLine(chart), t);
        return found < 0 ? null : (chart.history.details || [])[found] || null;
    }

    function historyWords(detail, t) {
        if (!detail || (!detail.accounts && !detail.change)) return [];
        var words = [plural(detail.accounts || 0, 'account') + ' · ' + (detail.measured || 0) + ' recorded'
            + (detail.carried ? ', ' + detail.carried + ' carried' : '')
            + (detail.unknown ? ', ' + detail.unknown + ' unknown' : '')];
        if (detail.unknown) words.push((detail.reasons || {}).sources_disagree
            ? 'Sources disagree · no settled value to carry' : 'No settled value yet · no partial sum or zero inferred');
        if (detail.carried) {
            var age = typeof detail.age_seconds === 'number' ? detail.age_seconds + Math.max(0, t - detail.at) : null;
            words.push('Oldest carried reading ' + (age !== null ? minutesWords(age) + ' old at this moment' : 'age unknown')
                + (detail.oldest_observed_at ? ' · observed ' + whenWords(detail.oldest_observed_at) : ''));
            var origins = Object.keys(detail.origins || {}).map(function (origin) { return originWords(origin); });
            if (origins.length) words.push(origins.join(', '));
        }
        if ((detail.sources || []).length) words.push('Source: ' + detail.sources.join(', '));
        if (detail.reset_passed) words.push(plural(detail.reset_passed, 'account')
            + ' past a reported reset · pre-reset reading retained; refill not measured');
        if (detail.change === 'first_recorded') {
            words.push('First recorded value' + (detail.added ? ' of ' + plural(detail.added, 'account') : '')
                + ' · no earlier value inferred');
            if (detail.added && detail.accounts > detail.added) words.push('Membership changed · not consumption');
        }
        if (detail.change === 'membership_changed') {
            var changes = [];
            if (detail.added) changes.push(plural(detail.added, 'account') + ' first recorded');
            if (detail.removed) changes.push(plural(detail.removed, 'account') + ' removed from the current roster');
            words.push('Membership changed' + (changes.length ? ': ' + changes.join(', ') : '') + ' · not consumption');
        }
        if (detail.removal_time_unknown) words.push('The exact removal time was not recorded');
        return words;
    }

    // What the chart says at one moment, as rows of [series, value].
    function valuesAt(chart, sc, t) {
        var rows = [];
        if (t <= chart.now) {
            var v = observedAt(chart, t);
            rows.push(['recorded', v === null || v === undefined ? null : v]);
        }
        if (sc && t >= chart.now) rows.push([scenarioName(scenario).toLowerCase(), lineAt(sc.line, t)]);
        return { future: t > chart.now, rows: rows };
    }

    // Whose total a recorded value is: the accounts with a record in the
    // span, whatever their reading now — or, with no record at all, the
    // current figure alone at now, of the accounts read now (never "of 0").
    function recordedCount(chart, t) {
        var detail = historyDetailAt(chart, t);
        if (detail) return detail.accounts || 0;
        return chart.past_accounts || chart.accounts || 0;
    }

    function readoutAt(chart, sc, t, name) {
        var at = valuesAt(chart, sc, t);
        var words = at.rows.map(function (r) {
            if (r[0] === 'recorded') {
                return 'recorded ' + (r[1] === null ? 'no record (gap)'
                    : num(r[1], 2) + ' of ' + plural(recordedCount(chart, t), 'account'));
            }
            return r[0] + ' ' + (r[1] === null ? 'not drawn' : num(r[1], 2) + ' of ' + plural(sc.accounts, 'current account'));
        });
        if (!words.length) words.push(at.future ? 'no future drawn' : 'no record');
        var provenance = t <= chart.now ? historyWords(historyDetailAt(chart, t), t) : [];
        return momentWords(t) + (at.future ? ' · scenario' : '') + ' — ' + name + ': ' + words.join(', ')
            + (provenance.length ? '. ' + provenance.join('. ') : '');
    }

    function niceStep(max) {
        var steps = [1, 2, 5, 10, 20, 50, 100, 200, 500];
        for (var i = 0; i < steps.length; i++) if (max / steps[i] <= 4) return steps[i];
        return steps[steps.length - 1];
    }

    // Local clock ticks: whole hours in a step that leaves room for a label,
    // days at midnight, counted on the calendar.
    function timeTicks(t0, t1, usable) {
        var hours = (t1 - t0) / 3600;
        var steps = [1, 2, 3, 6, 12, 24, 48];
        var step = 48;
        for (var i = 0; i < steps.length; i++) {
            if (usable / (hours / steps[i]) >= 46) { step = steps[i]; break; }
        }
        var ticks = [];
        var d = new Date(t0 * 1000);
        d.setHours(0, 0, 0, 0);
        for (var guard = 0; guard < 400 && d.getTime() / 1000 <= t1; guard++) {
            var t = d.getTime() / 1000;
            if (t >= t0) ticks.push({ t: t, midnight: d.getHours() === 0, day: d.getDay(), date: d.getDate(), hour: d.getHours() });
            d.setHours(d.getHours() + step);
        }
        return ticks;
    }

    // Steps of a held line ([t, v] holds v until the next t; null is a gap)
    // as separate runs, so a gap is never bridged.
    function stepRuns(line, x, y, t0) {
        var runs = [];
        var cur = null;
        var prev = null;
        (line || []).forEach(function (p) {
            var px = x(Math.max(p[0], t0));
            if (p[1] === null || p[1] === undefined) {
                if (cur && prev) { cur.push([px, y(prev[1])]); runs.push(cur); }
                cur = null;
                prev = null;
                return;
            }
            if (!cur) cur = [[px, y(p[1])]];
            else { cur.push([px, y(prev[1])]); cur.push([px, y(p[1])]); }
            prev = p;
        });
        if (cur) runs.push(cur);
        return runs;
    }

    function historyRuns(chart, x, y, t0) {
        var out = { recorded: [], carried: [] };
        if (!chart.history) { out.recorded = stepRuns(chart.past || [], x, y, t0); return out; }
        var line = recordLine(chart);
        var details = chart.history.details || [];
        var cur = null, prev = null, kind = '';
        function finish() { if (cur) out[kind].push(cur); cur = null; }
        line.forEach(function (p, i) {
            var px = x(Math.max(p[0], t0));
            if (p[1] === null || p[1] === undefined) {
                if (cur && prev) cur.push([px, y(prev[1])]);
                finish(); prev = null; return;
            }
            var nextKind = details[i] && details[i].carried ? 'carried' : 'recorded';
            if (cur && kind !== nextKind) {
                cur.push([px, y(prev[1])]); finish();
                cur = [[px, y(prev[1])], [px, y(p[1])]];
            } else if (cur) {
                cur.push([px, y(prev[1])]); cur.push([px, y(p[1])]);
            } else cur = [[px, y(p[1])]];
            kind = nextKind; prev = p;
        });
        finish();
        return out;
    }

    function runPath(runs) {
        return runs.map(function (run) {
            return 'M' + run.map(function (pt) { return pt[0].toFixed(1) + ' ' + pt[1].toFixed(1); }).join('L');
        }).join('');
    }

    function runArea(runs, base) {
        return runs.filter(function (run) { return run.length > 1; }).map(function (run) {
            var first = run[0], last = run[run.length - 1];
            return 'M' + first[0].toFixed(1) + ' ' + base.toFixed(1) + 'L'
                + run.map(function (pt) { return pt[0].toFixed(1) + ' ' + pt[1].toFixed(1); }).join('L')
                + 'L' + last[0].toFixed(1) + ' ' + base.toFixed(1) + 'Z';
        }).join('');
    }

    // One plot: a fixed time axis (the chosen span back and ahead, whole
    // hours, local clock) and a fixed value axis (every account the limit
    // applies to). Behind now the record, as a line with its area, broken
    // wherever it is not vouched for; ahead of now the chosen scenario from
    // the row's own figure, with a faint mark at each reported reset.
    function drawChart(panel, name, chart, sc) {
        var avail = (root && root.clientWidth) || 580;
        var W = Math.max(260, Math.min(1000, Math.floor(avail - 2 * 17)));
        var H = W < 460 ? 180 : 210;
        var pl = 30, pr = 12, pt = 10, pb = 24;
        var now = chart.now;
        var t0 = Math.floor(chart.start / 3600) * 3600;
        var t1 = t0 + Math.ceil((chart.end - t0) / 3600) * 3600;
        var ymax = Math.max(1, chart.y_max || 1, chart.history ? chart.history.max_accounts || 0 : 0);
        function x(t) { return pl + (t - t0) / (t1 - t0) * (W - pl - pr); }
        function y(v) { return pt + (1 - v / ymax) * (H - pt - pb); }
        var bottom = H - pb;

        var plot = el('div', 'chart-plot');
        plot.setAttribute('tabindex', '0');
        plot.setAttribute('data-focus', 'chart-plot');
        plot.setAttribute('role', 'group');
        plot.setAttribute('aria-label', 'Timeline of ' + name + ', account-windows left: the record behind now'
            + (sc ? ', ' + scenarioName(scenario).toLowerCase() + ' ahead' : '')
            + '. Arrow keys move through time; the data table under Details lists the values.');
        var svg = svgCanvas('0 0 ' + W + ' ' + H, W, H);
        svg.setAttribute('class', 'chart-svg');
        svg.appendChild(svgEl('rect', { x: x(now), y: pt, width: Math.max(0, x(t1) - x(now)), height: bottom - pt,
            'class': 'future-bg' }));

        var step = niceStep(ymax);
        for (var v = 0; v < ymax - step * 0.35; v += step) {
            svg.appendChild(svgEl('line', { x1: pl, x2: W - pr, y1: y(v), y2: y(v), 'class': v ? 'grid' : 'grid base' }));
            var lab = svgEl('text', { x: pl - 6, y: y(v) + 3.5, 'text-anchor': 'end', 'class': 'axis-text' });
            lab.textContent = String(v);
            svg.appendChild(lab);
        }
        svg.appendChild(svgEl('line', { x1: pl, x2: W - pr, y1: y(ymax), y2: y(ymax), 'class': 'grid cap' }));
        var capLab = svgEl('text', { x: pl - 6, y: y(ymax) + 3.5, 'text-anchor': 'end', 'class': 'axis-text' });
        capLab.textContent = String(ymax);
        svg.appendChild(capLab);

        var lastLabel = -Infinity;
        var nowRoom = chart.kept ? 48 : 34;
        timeTicks(t0, t1, W - pl - pr).forEach(function (tick) {
            var tx = x(tick.t);
            if (tick.midnight) svg.appendChild(svgEl('line', { x1: tx, x2: tx, y1: pt, y2: bottom, 'class': 'day-rule' }));
            svg.appendChild(svgEl('line', { x1: tx, x2: tx, y1: bottom, y2: bottom + 4, 'class': 'tick' }));
            if (Math.abs(tx - x(now)) < nowRoom || tx - lastLabel < 46 || tx < pl + 12 || tx > W - pr - 12) return;
            lastLabel = tx;
            var label = svgEl('text', { x: tx, y: H - 6, 'text-anchor': 'middle',
                'class': 'axis-text' + (tick.midnight ? ' day' : '') });
            label.textContent = tick.midnight ? WEEKDAYS[tick.day] + ' ' + tick.date : pad2(tick.hour) + ':00';
            svg.appendChild(label);
        });

        if (sc) {
            (sc.schedule || []).forEach(function (r) {
                svg.appendChild(svgEl('line', { x1: x(r.at), x2: x(r.at), y1: pt, y2: bottom, 'class': 'reset-line' }));
            });
        }
        svg.appendChild(svgEl('line', { x1: x(now), x2: x(now), y1: pt, y2: bottom + 4, 'class': 'now-line' }));
        var nowLab = svgEl('text', { x: x(now), y: H - 6, 'text-anchor': 'middle', 'class': 'axis-text now' });
        // A kept chart stays on the time axis of its own answer.
        nowLab.textContent = chart.kept ? 'read ' + clockAt(isoOf(now)) : 'now';
        svg.appendChild(nowLab);

        var history = historyRuns(chart, x, y, t0);
        var runs = history.recorded;
        var area = runArea(runs, bottom);
        if (area) svg.appendChild(svgEl('path', { d: area, 'class': 'area-observed' }));
        var d = runPath(runs);
        if (d) svg.appendChild(svgEl('path', { d: d, 'class': 'line-observed' }));
        var carried = runPath(history.carried);
        if (carried) svg.appendChild(svgEl('path', { d: carried, 'class': 'line-carried' }));
        if (sc && sc.line && sc.line.length) {
            var path = sc.line.map(function (p, j) {
                return (j ? 'L' : 'M') + x(p[0]).toFixed(1) + ' ' + y(p[1]).toFixed(1);
            }).join('');
            svg.appendChild(svgEl('path', { d: path, 'class': 'line-scenario' }));
            svg.appendChild(svgEl('circle', { cx: x(now).toFixed(1), cy: y(sc.line[0][1]).toFixed(1), r: 3,
                'class': 'scenario-start' }));
        }

        var cursor = svgEl('line', { x1: 0, x2: 0, y1: pt, y2: bottom, 'class': 'cursor-line', visibility: 'hidden' });
        svg.appendChild(cursor);
        var dots = {
            recorded: svgEl('circle', { cx: 0, cy: 0, r: 3.5, 'class': 'cursor-dot line-observed', visibility: 'hidden' }),
            scenario: svgEl('circle', { cx: 0, cy: 0, r: 3.5, 'class': 'cursor-dot line-scenario', visibility: 'hidden' })
        };
        svg.appendChild(dots.recorded);
        svg.appendChild(dots.scenario);
        var hit = svgEl('rect', { x: pl, y: 0, width: W - pl - pr, height: H, 'class': 'hit' });
        svg.appendChild(hit);
        plot.appendChild(svg);
        var tip = el('div', 'chart-tip');
        tip.setAttribute('aria-hidden', 'true');
        tip.style.display = 'none';
        plot.appendChild(tip);
        var readout = el('div', 'sr-only chart-readout');
        readout.setAttribute('aria-live', 'polite');
        plot.appendChild(readout);
        panel.appendChild(plot);

        var cursorKey = chart.group_key + '|' + chart.horizon + '|' + scenario;
        function show(t, via) {
            // The axis is whole hours; what is drawn is the chosen span.
            t = Math.max(chart.start, Math.min(chart.end, t));
            chartCursor = { key: cursorKey, t: t, via: via };
            var cx = x(t);
            cursor.setAttribute('x1', cx);
            cursor.setAttribute('x2', cx);
            cursor.setAttribute('visibility', 'visible');
            dots.recorded.setAttribute('visibility', 'hidden');
            dots.scenario.setAttribute('visibility', 'hidden');
            var at = valuesAt(chart, sc, t);
            tip.textContent = '';
            tip.appendChild(el('div', 'tip-when', momentWords(t)));
            at.rows.forEach(function (r) {
                var row = el('div', 'tip-row');
                var none = r[1] === null || r[1] === undefined;
                var detail = r[0] === 'recorded' ? historyDetailAt(chart, t) : null;
                row.appendChild(legendSwatch(r[0] === 'recorded' ? (detail && detail.carried ? 'line-carried' : 'line-observed') : 'line-scenario'));
                row.appendChild(el('span', 'tip-val', none ? (r[0] === 'recorded' ? 'no record' : 'not drawn') : num(r[1], 2)));
                row.appendChild(el('span', 'tip-name', r[0] === 'recorded' && detail && detail.carried ? 'last-known history' : r[0]));
                tip.appendChild(row);
                if (!none) {
                    var dot = r[0] === 'recorded' ? dots.recorded : dots.scenario;
                    dot.setAttribute('cx', cx);
                    dot.setAttribute('cy', y(r[1]));
                    dot.setAttribute('visibility', 'visible');
                }
            });
            if (t <= chart.now) historyWords(historyDetailAt(chart, t), t).forEach(function (words) {
                tip.appendChild(el('div', 'tip-foot', words));
            });
            tip.appendChild(el('div', 'tip-foot', name + ' · account-windows left, scale ' + ymax));
            tip.style.display = 'block';
            var rect = svg.getBoundingClientRect ? svg.getBoundingClientRect() : null;
            var scale = rect && rect.width ? rect.width / W : 1;
            var width = tip.offsetWidth || 170;
            var left = cx * scale + 14;
            if (left + width > W * scale - 4) left = cx * scale - 14 - width;
            tip.style.left = Math.max(0, left) + 'px';
            tip.style.top = '4px';
            readout.textContent = readoutAt(chart, sc, t, name);
        }
        function hide() {
            chartCursor = null;
            cursor.setAttribute('visibility', 'hidden');
            dots.recorded.setAttribute('visibility', 'hidden');
            dots.scenario.setAttribute('visibility', 'hidden');
            tip.style.display = 'none';
        }
        function fromPointer(ev) {
            var rect = svg.getBoundingClientRect ? svg.getBoundingClientRect() : null;
            if (!rect || !rect.width) return;
            var px = (ev.clientX - rect.left) * (W / rect.width);
            show(t0 + (px - pl) / (W - pl - pr) * (t1 - t0), 'pointer');
        }
        hit.addEventListener('pointermove', fromPointer);
        hit.addEventListener('pointerdown', fromPointer);
        hit.addEventListener('pointerleave', function () {
            if (!chartCursor || chartCursor.via === 'pointer') hide();
        });
        plot.addEventListener('keydown', function (ev) {
            var stepT = (t1 - t0) / 60;
            var at = chartCursor && chartCursor.key === cursorKey ? chartCursor.t : now;
            var next = null;
            if (ev.key === 'ArrowRight') next = at + stepT;
            else if (ev.key === 'ArrowLeft') next = at - stepT;
            else if (ev.key === 'Home') next = t0;
            else if (ev.key === 'End') next = t1;
            if (next === null) return;
            if (ev.preventDefault) ev.preventDefault();
            show(next, 'key');
        });
        plot.addEventListener('focus', function () {
            show(chartCursor && chartCursor.key === cursorKey ? chartCursor.t : now, 'key');
        });
        plot.addEventListener('blur', function () {
            if (!rebuilding && chartCursor && chartCursor.via === 'key') hide();
        });
        if (chartCursor && chartCursor.key === cursorKey) {
            var keep = chartCursor;
            Promise.resolve().then(function () {
                if (plot.parentNode && chartCursor === keep) show(keep.t, keep.via);
            });
        }
    }

    /* ------------------------------------------------------------------
       The selected account.
       ------------------------------------------------------------------ */

    // The selected account in every limit of its family, from the same bars
    // the rows draw: its share left, its reset, and — only when its own
    // observed pace, continued, would reach the limit before that reset —
    // when. A last-known value says so with its age; an unknown one why.
    function accountLimits(parent, ctx, account) {
        var groups = ctx.groups;
        var keyed = groups.some(function (g) { return barsOf(g).some(function (b) { return !!b.account; }); });
        if (!groups.length || !keyed) return;
        var box = el('div', 'acct-lims');
        box.setAttribute('role', 'list');
        box.setAttribute('aria-label', account.label + ' in each limit');
        groups.forEach(function (g) {
            var bar = barOf(g, account.key);
            var line = el('div', 'acct-lim');
            line.setAttribute('role', 'listitem');
            line.appendChild(el('span', 'k', ctx.names[g.key]));
            if (!bar) {
                line.appendChild(el('span', 'm none'));
                line.appendChild(el('span', 'p meta', '—'));
                line.appendChild(el('span', 'r', 'no reading of this limit — it may not apply'));
                box.appendChild(line);
                return;
            }
            var known = bar.state !== 'unknown' && typeof bar.left === 'number';
            var meterNode = el('span', 'm' + (bar.state === 'last_known' ? ' last' : '') + (heldNow(bar) ? ' held' : ''));
            if (known && bar.left > 0) {
                var fill = el('i');
                fill.style.width = +(Math.max(0, Math.min(1, bar.left)) * 100).toFixed(2) + '%';
                meterNode.appendChild(fill);
            }
            line.appendChild(meterNode);
            line.appendChild(el('span', 'p' + (known && bar.at_limit && bar.state !== 'last_known' ? ' bad'
                : (known ? '' : ' meta')), known ? leftPct(bar.left * 100) + '%' : '?'));
            var rest = el('span', 'r');
            var parts = [];
            if (!known) {
                parts.push([UNKNOWN_WORDS[bar.why] || 'no usable reading', '']);
            } else {
                if (bar.resets_at) {
                    parts.push([(bar.state === 'last_known' ? 'reported reset ' : (bar.at_limit ? 'until ' : 'resets '))
                        + whenWords(bar.resets_at), '']);
                }
                if (bar.state === 'last_known') {
                    parts.push(['last known, read ' + (relTime(bar.observed_at) || 'at an unreported time'), 'warn']);
                } else if (bar.pace && bar.pace.reaches_limit_at) {
                    parts.push(['at its recent pace would reach the limit ~' + whenWords(bar.pace.reaches_limit_at), 'est']);
                }
                if (heldNow(bar) && !bar.at_limit) {
                    parts.push([heldFlags(bar).map(function (f) { return FLAG_SHORT[f] || f; }).join(', ') || 'restricted', 'warn']);
                }
            }
            parts.forEach(function (part, i) {
                if (i) rest.appendChild(document.createTextNode(' · '));
                rest.appendChild(el('span', part[1] || null, part[0]));
            });
            line.appendChild(rest);
            line.title = barWords(bar, ctx.index, ctx.names[g.key]);
            box.appendChild(line);
        });
        parent.appendChild(box);
    }

    // The account's readings as the skill holds them: current windows, then
    // each reading that is not current — a stale one as last known, a fresh
    // one the reserve does not count with its reason.
    function accountReadings(parent, account) {
        var quota = account.quota || {};
        if (quota.note) parent.appendChild(withIcon(el('div', 'acct-note muted', quota.note), 'info', 12));
        if ((quota.constraints || []).length) {
            var current = el('div', 'diag-block');
            current.appendChild(el('div', 'diag-title muted', 'Current windows'
                + (relTime(quotaObservedAt(account)) ? ' · observed ' + relTime(quotaObservedAt(account)) : '')));
            current.appendChild(windowLines(quota.constraints, account, false));
            parent.appendChild(current);
        }
        (quota.stale || []).forEach(function (snap) {
            // A reading with no window in it has nothing to show.
            if (!(snap.constraints && snap.constraints.length)) return;
            var block = el('div', 'diag-block quota-last-known');
            block.appendChild(withIcon(el('div', 'diag-title',
                (snap.why ? 'Not current — ' + snap.why + ' · observed ' : 'Last known · observed ')
                    + (relTime(snap.observed_at) || 'at an unreported time')
                    + (snap.why ? ' · not counted' : ' · not used to grant routing')), 'warn', 12));
            block.appendChild(windowLines(snap.constraints, account, true));
            parent.appendChild(block);
        });
    }

    function renderInspector(parent, group, account, ctx, facets) {
        var quota = account.quota || {};
        var overview = !!(ctx && ctx.groups.length);
        var box = el('section', 'inspector');
        box.setAttribute('aria-label', 'Selected account');
        var head = el('div', 'insp-head');
        var title = el('div', 'insp-title');
        var tone = accountTone(account, facets);
        title.appendChild(stateDot(tone, true));
        title.appendChild(el('span', 'sr-only', TONE_WORD[tone] || tone));
        title.appendChild(el('span', 'insp-name', account.label));
        if (account.plan) {
            var chip = el('span', 'plan-chip', planWord(account.plan, group));
            chip.title = account.plan;
            title.appendChild(chip);
        }
        if (account.next_up) title.appendChild(el('span', 'next-up', '· next up'));
        head.appendChild(title);
        var clear = el('button', 'pill-btn insp-clear', 'Clear');
        clear.insertBefore(icon('close', 12), clear.firstChild);
        clear.setAttribute('type', 'button');
        clear.setAttribute('data-focus', 'inspector-clear');
        clear.setAttribute('aria-label', 'Clear the selected account');
        clear.addEventListener('click', function (e) {
            e.stopPropagation();
            clearAccount();
        });
        head.appendChild(clear);
        box.appendChild(head);

        var meta = el('div', 'insp-meta');
        if (account.email && account.email !== account.label) meta.appendChild(el('span', null, account.email));
        if (account.kind !== 'profile') meta.appendChild(el('span', null, 'Vendor CLI login'));
        if (account.verification && account.verification.label) {
            meta.appendChild(el('span', verificationFailed(account) ? 'meta-bad' : null, account.verification.label));
        }
        var reason = inactiveReason(account, facets);
        if (reason && reason !== 'failed') meta.appendChild(el('span', null, INACTIVE_WORDS[reason]));
        var observed = relTime(quotaObservedAt(account));
        meta.appendChild(el('span', 'quota-observed', observed ? 'Quota observed ' + observed
            : 'No quota observation time reported'));
        box.appendChild(meta);
        var notes = el('div', 'insp-meta');
        appendHarnessNotes(notes, group, facets);
        if (notes.firstChild) box.appendChild(notes);

        // The account's own verdict where it says something the lines below
        // cannot: a limit reached, a facet unread, no window at all. A cooling
        // account has its one cooldown sentence instead of a second verdict.
        var echoes = quota.state === 'ok' && !!(quota.constraints && quota.constraints.length);
        if (!echoes && !(quota.state === 'cooling' && cooldownsOf(quota, 'account').length)) {
            box.appendChild(renderQuotaVerdict(quota));
        }
        renderAccountCredits(box, quota);
        if (overview) accountLimits(box, ctx, account);
        renderAbsence(box, account, quota.absence);
        renderCooldowns(box, quota);
        var modelProblems = (quota.constraints || []).filter(function (c) { return isModelWindow(c) && isOut(c); });
        var named = {};
        modelProblems.forEach(function (c) { if (isSpent(c) && c.scope_key) named[c.scope_key] = true; });
        // Brief only with an overview, whose Diagnostics hold the rest; without
        // one there is no Diagnostics, so every report — the passed and unnamed
        // ones too — is shown here.
        renderExhaustions(box, quota, overview, named);
        var aside = (quota.stale || []).filter(function (s) { return !!s.why && s.constraints && s.constraints.length; });
        if (aside.length) {
            var whys = [];
            aside.forEach(function (s) { if (whys.indexOf(s.why) < 0) whys.push(s.why); });
            box.appendChild(withIcon(el('div', 'acct-note', (aside.length === 1 ? 'A fresh reading'
                : aside.length + ' fresh readings') + ' not counted now (' + whys.join('; ') + ')'), 'warn', 12));
        }

        // Without an overview the account's readings are all there is, and are
        // shown whole — everything Diagnostics would hold; with one they wait
        // under Diagnostics.
        if (!overview) {
            accountReadings(box, account);
            if (account.caption) box.appendChild(el('div', 'acct-note muted', 'Credential: ' + account.caption));
            parent.appendChild(box);
            return;
        }
        var diag = el('div', 'insp-diag');
        var toggle = el('button', 'pill-btn' + (diagnosticsOpen ? ' on' : ''), diagnosticsOpen ? 'Hide diagnostics' : 'Diagnostics');
        toggle.setAttribute('type', 'button');
        toggle.setAttribute('aria-expanded', diagnosticsOpen ? 'true' : 'false');
        toggle.setAttribute('aria-label', (diagnosticsOpen ? 'Hide' : 'Show') + ' every window and last-known reading of this account');
        toggle.setAttribute('data-focus', 'inspector-diag');
        toggle.addEventListener('click', function (e) {
            e.stopPropagation();
            diagnosticsOpen = !diagnosticsOpen;
            rerender();
        });
        diag.appendChild(toggle);
        if (diagnosticsOpen) {
            var body = el('div', 'insp-diag-body');
            accountReadings(body, account);
            // Every reported model exhaustion, the passed and unnamed ones too.
            var all = el('div');
            renderExhaustions(all, quota, false);
            if (all.firstChild) body.appendChild(all);
            if (account.caption) body.appendChild(el('div', 'acct-note muted', 'Credential: ' + account.caption));
            diag.appendChild(body);
        }
        box.appendChild(diag);
        parent.appendChild(box);
    }

    /* ------------------------------------------------------------------
       The account list.
       ------------------------------------------------------------------ */

    // Every account of the family, in the engine's order, with its share left
    // in every limit (from the same bars) and its state; the ones that cannot
    // run anything follow under their reasons. A row selects the account —
    // the same selection a bar makes.
    function renderAccounts(parent, group, ctx, facets, forceOpen) {
        var accounts = group.accounts || [];
        if (!accounts.length) return;
        var groups = ctx ? ctx.groups : [];
        var names = ctx ? ctx.names : {};
        var running = [];
        var idle = [];
        accounts.forEach(function (a) { (inactiveReason(a, facets) ? idle : running).push(a); });
        var open = accountsOpen || forceOpen;
        var box = el('section', 'accounts');
        box.setAttribute('aria-label', 'Accounts of ' + familyName(group));
        var head = el('div', 'acc-head');
        var summaryText = plural(accounts.length, 'account') + (idle.length ? ' · ' + idle.length + ' not running' : '');
        if (forceOpen) {
            head.appendChild(el('h4', 'acc-title', 'Accounts · ' + summaryText));
        } else {
            var toggle = el('button', 'pill-btn acc-toggle' + (open ? ' on' : ''));
            toggle.appendChild(withIcon(el('span', 'pill-icon'), 'caret', 12));
            toggle.appendChild(el('span', null, 'Accounts · ' + summaryText));
            toggle.setAttribute('type', 'button');
            toggle.setAttribute('aria-expanded', open ? 'true' : 'false');
            toggle.setAttribute('data-focus', 'accounts');
            toggle.addEventListener('click', function (e) {
                e.stopPropagation();
                accountsOpen = !accountsOpen;
                rerender();
            });
            head.appendChild(toggle);
        }
        box.appendChild(head);
        if (!open) {
            parent.appendChild(box);
            return;
        }
        ((ctx && ctx.cover) || []).forEach(function (line) { box.appendChild(el('div', 'acc-cover', line)); });
        var template = 'minmax(0,1fr)' + (groups.length ? ' repeat(' + groups.length + ',minmax(52px,72px))' : '')
            + ' minmax(96px,128px)';
        var table = el('div', 'acc-table');
        table.setAttribute('role', 'group');
        table.setAttribute('aria-label', 'Accounts of ' + familyName(group) + ', share left in each limit');
        var headRow = el('div', 'acc-headrow');
        headRow.style.gridTemplateColumns = template;
        headRow.setAttribute('aria-hidden', 'true');
        headRow.appendChild(el('span', null, 'Account'));
        groups.forEach(function (g) {
            var h = el('span', 'acc-cell', names[g.key]);
            h.title = names[g.key] + ' — share left';
            headRow.appendChild(h);
        });
        headRow.appendChild(el('span', null, 'State'));
        table.appendChild(headRow);
        function row(account) {
            var isOn = account.key === selectedAccountKey;
            var btn = el('button', 'acc-row' + (isOn ? ' active' : ''));
            btn.setAttribute('type', 'button');
            btn.style.gridTemplateColumns = template;
            btn.setAttribute('data-focus', 'acct:' + account.key);
            btn.setAttribute('aria-pressed', isOn ? 'true' : 'false');
            var state = accountState(account, facets);
            var nameCell = el('span', 'acc-name');
            nameCell.appendChild(stateDot(state.tone));
            nameCell.appendChild(el('span', 'acc-name-text', account.label));
            btn.appendChild(nameCell);
            var said = [account.label, state.word];
            groups.forEach(function (g) {
                var bar = barOf(g, account.key);
                var text = '—';
                var cls = 'acc-cell none';
                var spoken = names[g.key] + ' no reading, may not apply';
                if (bar && bar.state !== 'unknown' && typeof bar.left === 'number') {
                    text = leftPct(bar.left * 100) + '%';
                    cls = 'acc-cell' + (bar.state === 'last_known' ? ' last'
                        : (bar.at_limit ? ' bad' : (heldNow(bar) ? ' held' : '')));
                    spoken = names[g.key] + ' ' + text + ' left' + (bar.state === 'last_known'
                        ? ', last known, read ' + (relTime(bar.observed_at) || 'at an unreported time') : '');
                } else if (bar) {
                    text = '?';
                    cls = 'acc-cell unknown';
                    spoken = names[g.key] + ' unknown, ' + (UNKNOWN_WORDS[bar.why] || 'no usable reading');
                }
                var c = el('span', cls, text);
                c.setAttribute('data-limit', names[g.key]);
                c.title = spoken;
                btn.appendChild(c);
                said.push(spoken);
            });
            var stateCell = el('span', 'acc-state ' + state.tone, state.word);
            if (state.full) {
                stateCell.title = state.full;
                said[1] = state.full;
            }
            btn.appendChild(stateCell);
            btn.setAttribute('aria-label', said.join(' · ') + (isOn ? ' · selected' : ''));
            btn.addEventListener('click', function (e) {
                e.stopPropagation();
                selectAccount(isOn ? '' : account.key, 'acct:' + account.key);
                restoreFocus('acct:' + account.key);
            });
            return btn;
        }
        running.forEach(function (a) { table.appendChild(row(a)); });
        if (idle.length) {
            table.appendChild(el('div', 'acc-sec', 'Not running — '
                + 'switched off, signed out or failing their check; not counted as alerts'));
            idle.forEach(function (a) { table.appendChild(row(a)); });
        }
        box.appendChild(table);
        parent.appendChild(box);
    }

    /* ------------------------------------------------------------------
       About: what the screen is made of, and the state of the machine.
       ------------------------------------------------------------------ */

    function stripLegend() {
        var legend = el('div', 'strip-legend');
        legend.setAttribute('aria-hidden', 'true');
        [['', 'share left'],
         [' last', 'last known: dated, not current'],
         [' held', 'held back now (cooldown, model limit reported reached, another limit spent)'],
         [' spent', 'at the limit: 0% left'],
         [' unknown', 'no usable value — not counted']].forEach(function (k) {
            var item = el('span', 'legend-item');
            var cell = el('span', 'bar' + k[0]);
            if (k[0] !== ' unknown' && k[0] !== ' spent') {
                var f = el('span', 'fill');
                f.style.height = '62%';
                cell.appendChild(f);
            }
            item.appendChild(cell);
            item.appendChild(el('span', null, k[1]));
            legend.appendChild(item);
        });
        return legend;
    }

    function renderAbout(parent, view, summary, groups, statusProblem) {
        var box = el('section', 'about-panel');
        box.setAttribute('id', 'quotas-about');
        box.setAttribute('aria-label', 'About this widget');
        var facets = view.facets || {};
        var daemon = view.daemon || {};
        var daemonDown = !!(daemon.state && daemon.state !== 'running');
        box.appendChild(el('div', 'about-title', 'System'));
        box.appendChild(el('div', 'reserve-note', view.kept
            ? 'Nothing was read on the latest attempt: nothing below is current.'
            : 'What the daemon answered on this read.'));
        var state = el('div', 'system-state');
        var lastRead = relTime(daemon.read_at) || clockAt(daemon.read_at);
        var dRow = daemon.state
            ? dotLabel('daemon ' + daemon.state + (daemon.engine_version ? ' · ' + daemon.engine_version : ''),
                daemonDown ? 'bad' : 'ok')
            : (daemon.last_state
                ? dotLabel('daemon ' + daemon.last_state + ' when last read' + (lastRead ? ' (' + lastRead + ')' : ''), 'muted')
                : dotLabel('daemon not reported', 'muted'));
        dRow.className = 'dot-label strong';
        state.appendChild(dRow);
        FACET_ORDER.forEach(function (f) {
            var s = facets[f] || 'indeterminate';
            state.appendChild(catalogOmitted(view, f)
                ? dotLabel('catalog not requested for this quota read', 'muted')
                : dotLabel(s === 'ok' ? f : f + ' ' + facetWord(s), facetTone(s)));
        });
        // "next up" is a fact about routing: with no verdict on the wire the
        // widget shows no marker, and says so here.
        var mute = (view.groups || []).filter(function (g) { return g.routing_read === false; });
        if (mute.length) state.appendChild(dotLabel('rotation not reported for ' + mute.map(familyName).join(', '), 'warn'));
        box.appendChild(state);
        if (statusProblem) {
            box.appendChild(el('div', 'reserve-note', 'Each facet the daemon did not answer is shown from its last '
                + 'answer, dated, or as not read — never as zero.'));
        }

        var items = el('div', 'about-items');
        function item(titleText, text, extra) {
            var it = el('div', 'about-item');
            it.appendChild(el('div', 'about-title', titleText));
            it.appendChild(el('p', null, text));
            if (extra) it.appendChild(extra);
            items.appendChild(it);
        }
        var names = limitNames(groups);
        var scoped = groups.filter(function (g) { return (g.models || []).length; })[0];
        item('Windows left', 'Each account counts 1 whatever its plan: its share left of a limit is added up, so '
            + 'accounts with 40% and 70% left make 1.10 account-windows. Not tokens or hours.');
        item('Bars', 'One per account the limit applies to, fullest first — each row sorted on its own, so a column '
            + 'does not follow one account from row to row. Every row has one scale: full height is 100% left, the base '
            + '0%. A hatched bar is a last-known value — dated, not current; a “?” has no usable value and is not '
            + 'counted. Click a bar to select its account: its bars are marked in every row.', stripLegend());
        item('Figure', 'Account-windows left now, of the accounts the limit applies to: current readings only. '
            + 'Last-known values are never in it — they stay hatched bars and the dated “Last known” line under it. '
            + 'With no current reading the figure is “—”, never 0.');
        item('Timeline', 'Behind now, each account starts at its first recorded value. Dashed portions carry older '
            + 'readings through missing answers; hover for age and provenance. Membership changes break the line, '
            + 'and passing a reported reset never fabricates a past refill. Ahead, one condition on the accounts read now: no new use (each keeps its share and refills '
            + 'once at its next reported reset), or recent pace (the accounts with a measured pace keep it, the others '
            + 'are held). Neither is a forecast, and no reset nobody reported is assumed. Each row’s “Show chart” puts '
            + 'its limit in the timeline; on the charted row the same control hides it.');
        item('Lowest left', 'The limit whose measured accounts have the lowest average share left: a ranking, not a '
            + 'verdict on what can run. A limit scoped to models binds only the models it names, never the family’s '
            + 'other models' + (scoped ? ' — “' + names[scoped.key] + '” covers '
                + scopeWords(scoped).replace(/^models?: /, '') : '') + '.');
        item('Never added', 'Limits of different length or model are never added together: a 5-hour and a weekly '
            + 'limit bound the same work at once. Measured quota is not a dispatch guarantee: Claudexor decides routing.');
        if (summary) {
            var age = readingAge(summary, groups);
            var history = summary.history || {};
            var watched = watchWords(history);
            item('Readings', age.detail.charAt(0).toUpperCase() + age.detail.slice(1) + '. '
                + recordWords(history) + (watched ? '; ' + watched : '') + '.');
        }
        box.appendChild(items);
        parent.appendChild(box);
    }

    /* ------------------------------------------------------------------
       Requests.
       ------------------------------------------------------------------ */

    // One request through the bridge, bounded, that never rejects: it settles
    // with {ok, status, body, error, timedOut, bodyLost} however the request
    // ends — an answer, an HTTP error, a bridge or abort error, a body that
    // never finishes or breaks off after a success status (bodyLost), or no
    // answer at all. Whoever calls it decides what each means.
    function request(url, init, timeoutMs) {
        return new Promise(function (resolve) {
            var settled = false;
            var guard = null;
            var head = null;
            function done(result) {
                if (settled) return;
                settled = true;
                if (guard !== null && typeof window.clearTimeout === 'function') window.clearTimeout(guard);
                resolve(result);
            }
            guard = window.setTimeout(function () {
                done({ ok: false, status: 0, body: '', timedOut: true,
                       error: 'no answer within ' + Math.round(timeoutMs / 1000) + ' s' });
            }, timeoutMs + BACKSTOP_MS);
            var options = Object.assign({}, init || {}, { timeoutMs: timeoutMs });
            Promise.resolve().then(function () {
                return window.fetch(url, options);
            }).then(function (response) {
                if (!response || typeof response.text !== 'function') throw new Error('the bridge returned no response');
                head = response;
                return Promise.resolve(response.text()).then(function (body) {
                    done({ ok: !!response.ok, status: response.status, body: String(body || ''),
                           error: '', timedOut: false, bodyLost: false });
                });
            })['catch'](function (err) {
                var message = String((err && err.message) || err || 'bridge error');
                done({ ok: false, status: head ? head.status : 0, body: '', error: message,
                       timedOut: /timed out/i.test(message), bodyLost: !!(head && head.ok) });
            });
        });
    }

    // Whether an answer carries anything a screen can be drawn from.
    function hasContent(view) {
        var summary = view && view.reserve && view.reserve.summary;
        return (Array.isArray(view.groups) && view.groups.length > 0)
            || !!(summary && Array.isArray(summary.groups) && summary.groups.length > 0);
    }

    // One read of the whole projection: {view} to draw (with `why` when it
    // reports itself incomplete), or {why} when there is nothing to draw.
    function readView(reuse) {
        return request(reserveUrl(reuse), { method: 'GET' }, GET_TIMEOUT_MS).then(function (result) {
            var view = null;
            if (result.ok) {
                try { view = JSON.parse(result.body); } catch (err) { view = null; }
            }
            if (!view || typeof view !== 'object' || Array.isArray(view)) {
                return { view: null, why: result.timedOut ? 'no answer within ' + Math.round(GET_TIMEOUT_MS / 1000) + ' s'
                    : (result.error ? 'bridge error: ' + result.error
                        : (result.ok ? 'unreadable response body' : 'HTTP ' + result.status)) };
            }
            // An answer that reports itself as not ok normally carries its own
            // reason; with none and nothing to draw it is not a screen.
            var unexplained = !view.ok && !view.transport_error && !view.facet_note;
            if (unexplained && !hasContent(view)) return { view: null, why: 'response reported itself incomplete' };
            return { view: view, why: unexplained ? 'response reported itself incomplete' : '' };
        });
    }

    // Draws what a read brought. A read that brought nothing keeps the
    // newest screen drawn, re-read at this moment (keptView) — never an older
    // whole one that would look fresher — with why it was not refreshed.
    function land(got, actionText) {
        if (!got.view) {
            if (lastGood) safeRender(keptView(lastGood), got.why, actionText);
            else safeRender({ facets: {}, groups: [], daemon: {}, transport_error: 'widget route: ' + got.why }, '', actionText);
            return;
        }
        if (safeRender(got.view, got.why, actionText)) {
            lastGood = got.view;
            lastGoodAt = Date.now();
        }
    }

    // `reuse` is a chart, limit or family switch: the skill may answer from a
    // status read it made moments ago. The timed poll never passes it. Every
    // way a read can end releases inFlight exactly once, for the read that
    // set it; an answer after the frame was stopped or disposed changes nothing.
    function load(reuse) {
        if (stopped || inFlight) return;
        inFlight = true;
        if (!reuse) chartAsked = '';
        rerender();
        var mine = ++generation;
        readView(!!reuse).then(function (got) {
            if (mine !== generation || stopped) return;
            inFlight = false;
            land(got, got.view ? '' : actionMessage);
        })['catch'](function (err) {
            if (mine === generation) inFlight = false;
            failNow(err);
        });
    }

    function failNow(err) {
        try { console.error('claudexor quotas: reading not handled', err); } catch (e) { /* no console */ }
        actionMessage = 'The widget could not handle the last reading ('
            + String((err && err.message) || err) + '). Retry, or wait for the next reading.';
        rerender();
    }

    var UNKNOWN_OUTCOME = 'Live refresh got no readable answer, so whether it ran is unknown. It is not sent '
        + 'again; the values below stay as they were, and the next reading shows any change.';

    // The owner's foreground refresh: one POST, then — once the host says it
    // ran — one read of the whole projection, so the rows, the timeline and
    // the accounts all come from a reading made after it. A POST with no
    // readable answer may still have run: its outcome is unknown, said so,
    // and it is never sent again on its own. If the read after a refresh
    // fails, the screen stays the coherent one it was, said as kept.
    function refreshQuota() {
        if (stopped || inFlight) return;
        inFlight = true;
        actionMessage = '';
        rerender();
        var mine = ++generation;
        request(REFRESH_ROUTE, { method: 'POST' }, REFRESH_TIMEOUT_MS).then(function (result) {
            if (mine !== generation || stopped) return null;
            function end(message) {
                inFlight = false;
                actionMessage = message;
                rerender();
                return null;
            }
            if (result.timedOut) {
                return end('Live refresh got no answer within ' + Math.round(REFRESH_TIMEOUT_MS / 1000)
                    + ' s, so whether it ran is unknown. It is not sent again; the values below stay as they were, '
                    + 'and the next reading shows any change.');
            }
            var response = null;
            try { response = JSON.parse(result.body); } catch (err) { response = null; }
            // Only the route's own answer — an object that says ok true or
            // false — can say what happened.
            var readable = !!response && typeof response === 'object' && typeof response.ok === 'boolean';
            if ((readable && response.outcome_unknown === true) || (result.ok && !readable)
                    || result.bodyLost || result.status === 0) {
                return end(UNKNOWN_OUTCOME);
            }
            if (!result.ok || !response.ok) {
                return end(response && response.compatibility_error
                    ? 'Live refresh requires a newer Ouroboros host.'
                    : 'Live quota refresh failed. The values below stay as they were.');
            }
            var ranAt = clockAt(new Date().toISOString());
            chartAsked = '';
            return readView(false).then(function (got) {
                if (mine !== generation || stopped) return;
                inFlight = false;
                land(got, got.view ? '' : 'Live refresh ran at ' + ranAt + ', but the reading after it could not be '
                    + 'read: the values below are from before it.');
            });
        })['catch'](function (err) {
            if (mine === generation) inFlight = false;
            failNow(err);
        });
    }

    /* ------------------------------------------------------------------
       A kept screen: the newest one drawn, re-read at this moment.

       A read that failed outright keeps the newest screen drawn: a record,
       and nothing in it is current any more. A current bar becomes the dated
       last-known value it now is; past its reported reset — or, with none
       reported, past its window — a "?" whose last reading stays on record.
       What only current readings support (the figure, the next reset, pace,
       every future) is withdrawn, and each account's windows become last-known
       readings; no facet was read now, the daemon's word is its last one, an
       account's check is last known and none is next up. Re-derived from the
       answer at every failed read, so the cut follows the clock.
       ------------------------------------------------------------------ */

    function keptView(view) {
        if (!view || typeof view !== 'object') return view;
        var daemon = view.daemon || {};
        if (!daemon.state && !daemon.last_state && !(view.groups || []).length && !view.reserve
            && !Object.keys(view.facets || {}).length && !view.transport_error) return view;
        var nowMs = Date.now();
        var summary = view.reserve && view.reserve.summary;
        var readAt = (summary && summary.status_read_at) || (lastGoodAt ? new Date(lastGoodAt).toISOString() : '');
        var cached = Object.assign({}, (summary && summary.cached) || {}, view.cached || {});
        FACET_ORDER.forEach(function (f) {
            if (!cached[f] && readAt && !catalogOmitted(view, f)) cached[f] = readAt;
        });
        var facets = {};
        FACET_ORDER.forEach(function (f) { facets[f] = 'indeterminate'; });
        var out = Object.assign({}, view, {
            ok: false, complete: false, kept: true, cached: cached,
            facets: facets, facet_note: '',
            daemon: { state: '', last_state: daemon.state || daemon.last_state || '',
                      engine_version: daemon.engine_version || '', read_at: daemon.read_at || readAt }
        });
        if (view.passive_read) out.passive_read = { mode: 'unavailable', timings_ms: {}, read_errors: {} };
        if (view.reserve && typeof view.reserve === 'object') {
            var reserve = Object.assign({}, view.reserve);
            if (summary && typeof summary === 'object') {
                reserve.summary = Object.assign({}, summary, {
                    cached: cached,
                    roster: summary.roster === 'unknown' ? 'unknown' : 'cached',
                    groups: (summary.groups || []).map(function (g) { return keptGroup(g, nowMs); })
                });
            }
            if (reserve.chart) reserve.chart = keptChart(reserve.chart);
            out.reserve = reserve;
        }
        out.groups = (view.groups || []).map(function (group) {
            if (!group || !Array.isArray(group.accounts)) return group;
            return Object.assign({}, group, { routing_read: false,
                reset_credits: keptFamilyCredits(group.reset_credits),
                accounts: group.accounts.map(function (a) { return keptAccount(a, nowMs); }) });
        });
        return out;
    }

    function keptFamilyCredits(c) {
        if (!c) return c;
        var accounts = (c.current_accounts || 0) + (c.last_known_accounts || 0);
        var dates = [c.oldest_observed_at, c.last_known_oldest_observed_at].filter(Boolean).sort();
        var newest = [c.newest_observed_at, c.last_known_newest_observed_at].filter(Boolean).sort();
        return Object.assign({}, c, {
            count: null, current_accounts: 0, oldest_observed_at: null, newest_observed_at: null,
            last_known_count: accounts ? (creditCount(c.count) ? c.count : 0)
                + (creditCount(c.last_known_count) ? c.last_known_count : 0) : null,
            last_known_accounts: accounts,
            last_known_oldest_observed_at: dates[0] || null,
            last_known_newest_observed_at: newest[newest.length - 1] || null
        });
    }

    function keptCredits(c, nowMs) {
        if (!c || (c.state !== 'current' && c.state !== 'last_known')) return c;
        var observed = Date.parse(String(c.observed_at || ''));
        return Object.assign({}, c, {
            state: 'last_known', reason: 'screen_not_read',
            age_seconds: isFinite(observed) ? Math.max(0, Math.round((nowMs - observed) / 1000)) : null
        });
    }

    function keptAccount(a, nowMs) {
        if (!a || typeof a !== 'object') return a;
        var label = String((a.verification || {}).label || 'Not verified');
        return Object.assign({}, a, {
            verification: { tone: 'muted', label: /— last known$/.test(label) ? label : label + ' — last known' },
            verified_live: false,
            next_up: false,
            quota: a.quota ? keptQuota(a.quota, nowMs) : a.quota
        });
    }

    // One bar of a kept screen, by the skill's own carry rule (carry_verdict).
    function keptBar(b, windowSeconds, nowMs) {
        if (!b || b.state === 'unknown') return b;
        var left = typeof b.left === 'number' && isFinite(b.left) ? b.left : null;
        var observed = Date.parse(String(b.observed_at || ''));
        var reset = Date.parse(String(b.resets_at || ''));
        var why = '';
        if (b.resets_at && isFinite(reset)) {
            if (reset <= nowMs) why = 'reset_passed';
        } else if (!isFinite(observed) || nowMs - observed > (windowSeconds > 0 ? windowSeconds : 86400) * 1000) {
            why = 'too_old';
        }
        var origin = b.state === 'current' ? 'screen' : b.origin;
        // Whether an account is held back now is not known on a kept screen.
        var base = { account: b.account, flags: ['account_state_unknown'], restricted: false, observed_at: b.observed_at };
        if (why || left === null) {
            return Object.assign(base, { state: 'unknown', left: null, at_limit: false, why: why || 'not_read',
                last_reading: left === null ? null : { left: left, observed_at: b.observed_at,
                                                       resets_at: b.resets_at || null, origin: origin } });
        }
        return Object.assign(base, { state: 'last_known', left: left, at_limit: !!b.at_limit,
            resets_at: b.resets_at || null, origin: origin,
            age_seconds: isFinite(observed) ? Math.max(0, Math.round((nowMs - observed) / 1000)) : null });
    }

    function keptGroup(g, nowMs) {
        if (!g || typeof g !== 'object') return g;
        var known = [];
        var unknown = [];
        barsOf(g).forEach(function (b) {
            var kept = keptBar(b, g.window_seconds, nowMs);
            if (kept) (kept.state === 'unknown' ? unknown : known).push(kept);
        });
        unknown.sort(function (a, b) { return compareText(String(a.account || ''), String(b.account || '')); });
        var windows = 0, oldest = '', newest = '', origins = {}, reasons = {};
        known.forEach(function (b) {
            windows += b.left;
            if (b.observed_at && (!oldest || b.observed_at < oldest)) oldest = b.observed_at;
            if (b.observed_at && (!newest || b.observed_at > newest)) newest = b.observed_at;
            origins[b.origin] = (origins[b.origin] || 0) + 1;
        });
        unknown.forEach(function (b) { reasons[b.why] = (reasons[b.why] || 0) + 1; });
        windows = Math.round(windows * 10000) / 10000;
        var coverage = g.coverage || {};
        return Object.assign({}, g, {
            bars: known.concat(unknown),
            measured: { accounts: 0, windows: 0, average_remaining_pct: null, at_limit: 0 },
            shares: [],
            // Counts of the answer's own moment are re-derived above as last
            // known and unknown; only who the limit may not apply to stays.
            coverage: { measured: 0, other_family_accounts: coverage.other_family_accounts || 0 },
            plans: null,
            unrestricted_windows: 0,
            restrictions: {},
            observed: { newest_at: null, oldest_at: null, stale_newest_at: newest || null },
            next_reset: null,
            next_reset_returns: null,
            reset_unknown: 0,
            pace_to_reset: null,
            recent_pace: { state: 'no_measured_accounts', accounts_known: 0, of: 0, not_known: {} },
            last_known: { accounts: known.length, windows: windows, oldest_observed_at: oldest || null,
                          newest_observed_at: newest || null, origins: origins },
            unknown: { accounts: unknown.length, reasons: reasons },
            with_last_known: { windows: windows, accounts: known.length }
        });
    }

    // The chart of a kept screen: the record stays; nothing is drawn at or
    // after its "now" — no value now, no future, no reset — and that moment is
    // labelled by its clock time, not as now.
    function keptChart(chart) {
        if (!chart || typeof chart !== 'object') return chart;
        var past = (chart.past || []).slice();
        if (past.length) past[past.length - 1] = [past[past.length - 1][0], null];
        var scope = chart.recent_pace_scope || {};
        return Object.assign({}, chart, {
            kept: true,
            past: past, history: keptHistory(chart.history), accounts: 0, past_current_accounts: 0, current_windows: null,
            past_basis: chart.past_accounts ? 'last_known' : 'none',
            scenarios: null,
            no_new_use: null, recent_pace: null, recent_pace_refill_scenario: null, cohort_past: null,
            recent_pace_note: '', resets: [], reset_events: [],
            recent_pace_scope: { accounts: 0, of: 0, slots: scope.slots, until: null, stops_at_reset: false, excluded: {} },
            table: (chart.table || []).filter(function (row) {
                return row && row.observed !== undefined && row.event !== 'now';
            })
        });
    }

    function keptHistory(history) {
        if (!history) return history;
        var line = (history.line || []).map(function (p) { return p.slice(); });
        var details = (history.details || []).slice();
        if (line.length) {
            line[line.length - 1][1] = null;
            if (details.length) details[details.length - 1] = Object.assign({}, details[details.length - 1], { value: null });
        }
        return Object.assign({}, history, { line: line, details: details });
    }

    // An account's readings on a kept screen: its windows are last known; a
    // cooldown whose end has passed no longer holds, and a model exhaustion
    // whose reset has passed is disclosed as passed.
    function keptQuota(q, nowMs) {
        if (!q || typeof q !== 'object') return q;
        var ahead = function (iso) {
            var t = Date.parse(String(iso || ''));
            return !iso || !isFinite(t) || t > nowMs;
        };
        var current = Array.isArray(q.constraints) ? q.constraints : [];
        var stale = (Array.isArray(q.stale) ? q.stale : []).slice();
        if (current.length) stale.unshift({ observed_at: q.observed_at || '', freshness: 'stale', source: '', constraints: current });
        var out = Object.assign({}, q, {
            reset_credits: keptCredits(q.reset_credits, nowMs),
            constraints: [],
            stale: stale,
            resets_at: '',
            cooldowns: (q.cooldowns || []).filter(function (c) { return c && ahead(c.until); })
                .map(function (c) { return Object.assign({}, c, { freshness: 'stale' }); }),
            model_exhaustions: (q.model_exhaustions || []).filter(Boolean).map(function (e) {
                var passed = e.live && e.resets_at && !ahead(e.resets_at);
                return Object.assign({}, e, { freshness: 'stale' }, passed ? { live: false, reset_note: 'passed' } : {});
            }),
            cooling_until: q.cooling_until && ahead(q.cooling_until) ? q.cooling_until : ''
        });
        if (stale.length && q.state !== 'not_checked') {
            out.state = 'no_fresh_window';
            out.label = 'No fresh reading — last reading is stale';
            out.note = 'Stale percentages do not grant routing; live cooldown evidence may still deny or rank.';
        } else if (q.state === 'cooling' && !cooldownsOf(out, 'account').length) {
            out.state = 'no_fresh_window';
            out.label = 'No fresh reading — the cooldown last reported has ended';
            out.note = '';
        }
        return out;
    }

    /* ------------------------------------------------------------------
       Drawing.
       ------------------------------------------------------------------ */

    // render() rebuilds the whole tree. If drawing an answer throws half way,
    // the screen that was up before comes back (re-read as kept), with the
    // error said above it and a Retry — never a blank card, and never an
    // exception thrown out of a timer or a click into the host.
    function safeRender(view, staleText, actionText) {
        var kept = Array.prototype.slice.call(root.childNodes);
        var before = { view: currentView, stale: staleMessage, action: actionMessage };
        try {
            render(view, staleText, actionText);
            drawFault = '';
            return true;
        } catch (err) {
            drawFault = ((err && err.name) ? err.name + ': ' : '') + String((err && err.message) || err);
            try { console.error('claudexor quotas: the answer could not be drawn', err); } catch (e) { /* no console */ }
            currentView = before.view;
            staleMessage = before.stale;
            actionMessage = before.action;
            var redrawn = false;
            if (before.view && before.view !== view) {
                try {
                    render(keptView(before.view), '', before.action);
                    redrawn = true;
                } catch (again) {
                    redrawn = false;
                }
            }
            if (!redrawn) {
                rebuilding = true;
                try { root.textContent = ''; } finally { rebuilding = false; }
                kept.forEach(function (node) { root.appendChild(node); });
            }
            var note = faultBanner(kept.length
                ? 'The latest answer could not be drawn (' + drawFault + '). The screen before it is kept.'
                : 'The answer could not be drawn (' + drawFault + ').');
            root.insertBefore(note, root.firstChild);
            // The kept screen keeps saying what it is until an answer is drawn.
            if (redrawn) staleMessage = before.stale || 'the latest answer could not be drawn';
            return false;
        }
    }

    function faultBanner(text) {
        var node = el('div', 'banner bad draw-fault');
        node.setAttribute('role', 'status');
        node.appendChild(withIcon(el('span', 'banner-icon'), 'warn'));
        node.appendChild(el('span', 'banner-text', text));
        node.appendChild(retryButton());
        return node;
    }

    // Retry is the ordinary read, asked for now: it never repeats a refresh.
    function retryButton() {
        var btn = el('button', 'pill-btn banner-retry', 'Retry');
        btn.setAttribute('type', 'button');
        btn.setAttribute('data-focus', 'retry');
        btn.disabled = inFlight;
        btn.addEventListener('click', function (e) {
            e.stopPropagation();
            load();
        });
        return btn;
    }

    function banner(parent, iconName, text, bad) {
        var node = el('div', 'banner' + (bad ? ' bad' : ''));
        node.setAttribute('role', 'status');
        node.appendChild(withIcon(el('span', 'banner-icon'), iconName));
        node.appendChild(el('span', 'banner-text', text));
        parent.appendChild(node);
        return node;
    }

    function emptyCard(iconName, title, desc) {
        var card = el('div', 'empty-card');
        card.appendChild(withIcon(el('div', 'empty-icon'), iconName, 26));
        card.appendChild(el('h4', 'empty-title', title));
        card.appendChild(el('p', 'empty-desc', desc));
        root.appendChild(card);
        return card;
    }

    // The family on screen: the reader's, while it exists; else the first
    // family with accounts. The selected account stays only while it is
    // still an account of that family: nothing selects itself.
    function syncSelection(groups) {
        var group = null;
        var i;
        for (i = 0; i < groups.length; i++) if (groups[i].harness_id === selectedHarness) { group = groups[i]; break; }
        if (!group) {
            for (i = 0; i < groups.length; i++) if ((groups[i].accounts || []).length) { group = groups[i]; break; }
        }
        if (!group) group = groups[0] || null;
        if (!group) {
            selectedHarness = '';
            selectedAccountKey = '';
            return { group: null, account: null };
        }
        selectedHarness = group.harness_id;
        var account = null;
        (group.accounts || []).forEach(function (a) { if (a && a.key === selectedAccountKey) account = a; });
        if (!account) selectedAccountKey = '';
        return { group: group, account: account };
    }

    function renderHarnessSeg(parent, groups, selected, hasAnswer, facets) {
        var seg = el('div', 'harness-seg');
        seg.setAttribute('role', 'group');
        seg.setAttribute('aria-label', 'Agent family');
        // Before the first answer: three marks shown faint and dead, a shape
        // waiting to be filled — not a claim about the reader's families.
        if (!groups.length && !hasAnswer) {
            ['codex', 'claude', 'cursor'].forEach(function (name) {
                var ghost = el('button', 'harness-btn loading');
                ghost.disabled = true;
                ghost.appendChild(brandIcon(name, 14));
                ghost.appendChild(el('span', null, name.charAt(0).toUpperCase() + name.slice(1)));
                seg.appendChild(ghost);
            });
            seg.setAttribute('aria-label', 'Reading agent families');
            parent.appendChild(seg);
            return;
        }
        groups.forEach(function (group) {
            var isOn = selected && group.harness_id === selected.harness_id;
            var count = (group.accounts || []).length;
            var btn = el('button', 'harness-btn' + (isOn ? ' active' : '') + (count ? '' : ' empty'));
            btn.setAttribute('type', 'button');
            btn.appendChild(familyMark(group));
            var name = familyName(group);
            btn.appendChild(el('span', 'harness-name', name.replace(/ (?:CLI|Code)$/, '')));
            // The number is the accounts switched on: the ones that can carry work.
            var active = (group.accounts || []).filter(function (a) { return a.enabled !== false; }).length;
            if (count) btn.appendChild(el('span', 'harness-count', String(active)));
            var say = name + ' — ' + (count ? count + (count === 1 ? ' account' : ' accounts') : 'no accounts')
                + (groupTrouble(group) ? ', harness ' + (group.harness_enabled === false ? 'disabled' : group.harness_status) : '');
            btn.setAttribute('aria-label', say);
            btn.setAttribute('aria-pressed', isOn ? 'true' : 'false');
            btn.setAttribute('data-focus', 'harness:' + group.harness_id);
            btn.title = say;
            btn.addEventListener('click', function (e) {
                e.stopPropagation();
                if (selectedHarness !== group.harness_id) {
                    selectedHarness = group.harness_id;
                    selectedAccountKey = '';
                    chartCursor = null;
                }
                rerender();
            });
            // No pip for the worst account: one spent account is not the
            // family's state. A harness that is down or off is.
            if (groupTrouble(group)) btn.appendChild(el('span', 'pip warn seg-pip'));
            seg.appendChild(btn);
        });
        parent.appendChild(seg);
    }

    function render(view, staleText, actionText) {
        currentView = view;
        staleMessage = staleText;
        actionMessage = actionText || '';

        // The whole tree is rebuilt every 30 seconds; the keyboard focus and
        // the page's scroll are carried over by key.
        var was = document.activeElement;
        var focusWas = (was && was.getAttribute) ? was.getAttribute('data-focus') : null;
        if (!focusWas && focusParked && focusLost(was)) focusWas = focusParked;
        focusParked = '';
        var scrollWas = readScroll();

        rebuilding = true;
        try {
            root.textContent = '';
        } finally {
            rebuilding = false;
        }

        var facets = view.facets || {};
        var groups = view.groups || [];
        var selection = syncSelection(groups);
        var daemon = view.daemon || {};
        var daemonDown = !!(daemon.state && daemon.state !== 'running');
        var unreadFacets = unreadFacetsOf(view);
        var facetProblem = unreadFacets.length > 0;
        // Before the first answer nothing has been read and nothing has failed.
        var hasAnswer = !!(daemon.state || groups.length || Object.keys(facets).length || view.transport_error);
        var statusProblem = hasAnswer && (daemonDown || facetProblem || !!view.transport_error);

        /* 1. Families, About and Refresh. */
        var controlBar = el('div', 'control-bar');
        renderHarnessSeg(controlBar, groups, selection.group, hasAnswer, facets);
        var actionSeg = el('div', 'action-seg');
        actionSeg.setAttribute('role', 'group');
        actionSeg.setAttribute('aria-label', 'About and refresh');
        var aboutBtn = el('button', 'action-btn action-about' + (aboutOpen ? ' is-open' : '')
            + (statusProblem ? ' has-problem' : ''));
        aboutBtn.setAttribute('type', 'button');
        aboutBtn.appendChild(icon('info', 14));
        aboutBtn.appendChild(el('span', null, 'About'));
        aboutBtn.appendChild(el('span', 'pip seg-pip ' + (!hasAnswer ? 'muted' : (statusProblem ? 'bad' : 'ok'))));
        aboutBtn.setAttribute('aria-expanded', aboutOpen ? 'true' : 'false');
        aboutBtn.setAttribute('aria-controls', 'quotas-about');
        aboutBtn.setAttribute('aria-label', 'About this widget and the system state'
            + (!hasAnswer ? ' — nothing read yet' : (statusProblem ? ' — the daemon or a facet did not answer' : '')));
        aboutBtn.title = aboutBtn.getAttribute('aria-label');
        aboutBtn.setAttribute('data-focus', 'about');
        aboutBtn.addEventListener('click', function (e) {
            e.stopPropagation();
            aboutOpen = !aboutOpen;
            rerender();
        });
        actionSeg.appendChild(aboutBtn);
        var refreshBtn = el('button', 'action-btn action-refresh' + (inFlight ? ' is-refreshing' : ''));
        refreshBtn.setAttribute('type', 'button');
        refreshBtn.appendChild(withIcon(el('span', 'icon-spin'), 'refresh', 14));
        refreshBtn.appendChild(el('span', null, 'Refresh'));
        refreshBtn.disabled = inFlight;
        refreshBtn.setAttribute('aria-label', inFlight ? 'Refreshing…'
            : 'Refresh — ask the host for a live quota reading; the widget also re-reads on its own every '
                + Math.round(REFRESH_MS / 1000) + ' seconds');
        refreshBtn.title = refreshBtn.getAttribute('aria-label');
        refreshBtn.setAttribute('data-focus', 'refresh');
        refreshBtn.addEventListener('click', function () {
            if (inFlight) return;
            refreshQuota();
        });
        actionSeg.appendChild(refreshBtn);
        controlBar.appendChild(actionSeg);
        root.appendChild(controlBar);

        /* 2. Banners: a daemon that is down and a facet that did not answer
           say so in the open, for every family. */
        var banners = el('div', 'banners');
        if (daemonDown) {
            banner(banners, 'warn', 'Claudexor daemon is ' + daemon.state + '. Readings below are last known, not live.', true);
        }
        var cachedFacets = view.cached || {};
        var cachedNames = FACET_ORDER.filter(function (f) { return !!cachedFacets[f]; });
        if (staleMessage) {
            banner(banners, 'warn', 'Reading could not be refreshed (' + staleMessage + '). The last answer, received '
                + (relSince(lastGoodAt) || 'earlier') + ', is kept: nothing in it is current, and each value '
                + 'is dated by when it was observed.', true).appendChild(retryButton());
        }
        if (actionMessage) banner(banners, 'warn', actionMessage, true);
        if (view.transport_error && !staleMessage) {
            var lastAt = cachedNames.length ? cachedFacets[cachedNames[0]] : '';
            // The raw error stays off the screen: it can carry local paths.
            banner(banners, 'error', lastAt
                ? 'Claudexor status could not be read. Everything below is last known from '
                    + (relTime(lastAt) || clockAt(lastAt)) + ' — nothing is current.'
                : 'Endpoint unreachable. No quota claims made.', true).appendChild(retryButton());
        } else if (view.facet_note && facetProblem) {
            var keptFacets = unreadFacets.filter(function (f) { return !!cachedFacets[f]; });
            banner(banners, 'info', 'Not read now: ' + view.facet_note + '. '
                + (keptFacets.length ? keptFacets.join(', ') + ' shown as last known from '
                    + (relTime(cachedFacets[keptFacets[0]]) || clockAt(cachedFacets[keptFacets[0]]))
                    + ', never as current or zero.'
                    : 'Values shown as unread/last known, not zero.'), false);
        }
        root.appendChild(banners);

        var summary = view.reserve && view.reserve.summary;
        if (aboutOpen && hasAnswer) {
            renderAbout(root, view, summary, selection.group ? reserveGroups(view, selection.group.harness_id) : [],
                statusProblem);
        }

        /* 3. The family's limits; the account selected in them; its timeline;
           its accounts. */
        var ctx = null;
        if (hasAnswer && selection.group) ctx = renderReserve(root, view.reserve, selection.group, view);
        if (selection.account) renderInspector(root, selection.group, selection.account, ctx, facets);
        if (selection.group && (selection.group.accounts || []).length) {
            renderAccounts(root, selection.group, ctx, facets, !(ctx && ctx.groups.length));
        }
        if (!hasAnswer) {
            emptyCard('refresh', 'Reading accounts…', 'Asking the Claudexor daemon for accounts and quota.');
        } else if (!selection.group) {
            // No family came back at all: not "you have no accounts".
            emptyCard('warn', 'No agent family reported', view.transport_error
                ? 'The status endpoint did not answer, so nothing is claimed about accounts.'
                : 'The answer carried no agent family. Nothing is claimed about accounts.');
        } else if (!(selection.group.accounts || []).length) {
            var card = emptyCard('info', 'No accounts in ' + familyName(selection.group),
                catalogOmitted(view, 'catalog')
                    ? 'This answer names the agent family but reports no accounts for it.'
                    : 'The catalog lists this agent family, but no account is set up for it yet.');
            var notes = el('div', 'empty-notes');
            appendHarnessNotes(notes, selection.group, facets);
            if (notes.childNodes.length) card.appendChild(notes);
        }

        if (focusWas) restoreFocus(focusWas);
        restoreScroll(scrollWas);
        askForChart();
    }

    // Keys are compared by hand rather than through a selector: an account key
    // is engine-shaped ("codex:codex-default") and would need escaping.
    function restoreFocus(key) {
        if (!key) return;
        var nodes = root.querySelectorAll('[data-focus]');
        var held = false;
        for (var i = 0; i < nodes.length; i++) {
            if (nodes[i].getAttribute('data-focus') !== key) continue;
            if (!nodes[i].disabled) {
                nodes[i].focus();
                return;
            }
            held = true;
        }
        // There, but disabled for now: kept for the redraw that enables it.
        if (held) focusParked = key;
        // Gone with their pages, the sightings' page buttons hand the keyboard
        // to the summary of the Details they were in.
        else if (key.indexOf('sightings:') === 0) restoreFocus('chart-table');
    }

    // Focus is nowhere the reader put it: on nothing, on the page itself, or
    // on a node a redraw has since removed. A frame the reader has left is
    // not pulled back into.
    function focusLost(node) {
        if (typeof document.hasFocus === 'function' && !document.hasFocus()) return false;
        return !node || node === document.body || node === document.documentElement || node.isConnected === false;
    }

    function pageNodes() {
        var out = [];
        if (document.body) out.push(document.body);
        var scroller = document.scrollingElement || document.documentElement;
        if (scroller && scroller !== document.body) out.push(scroller);
        return out;
    }

    function readScroll() {
        var page = 0;
        pageNodes().forEach(function (node) {
            if (typeof node.scrollTop === 'number' && node.scrollTop > page) page = node.scrollTop;
        });
        return page;
    }

    function restoreScroll(page) {
        if (page) pageNodes().forEach(function (node) { node.scrollTop = page; });
    }

    function rerender() {
        if (stopped) return;
        safeRender(currentView || { facets: {}, groups: [], daemon: {} }, staleMessage, actionMessage);
    }

    // A redraw under a resting pointer keeps the chart's cursor; once the
    // pointer moves anywhere but the chart, the cursor goes with it.
    function onDocumentPointer(e) {
        if (!chartCursor || chartCursor.via !== 'pointer') return;
        var t = e.target;
        var inside = t && typeof t.closest === 'function' ? t.closest('.chart-plot') : null;
        if (inside) return;
        chartCursor = null;
        var nodes = root.querySelectorAll ? root.querySelectorAll('.chart-tip') : [];
        for (var i = 0; i < nodes.length; i++) nodes[i].style.display = 'none';
        var lines = root.querySelectorAll ? root.querySelectorAll('.cursor-line,.cursor-dot') : [];
        for (var j = 0; j < lines.length; j++) lines[j].setAttribute('visibility', 'hidden');
    }

    // Escape closes About, else clears the selected account — each returning
    // the keyboard to the control that opened it.
    function onDocumentKey(e) {
        if (e.key !== 'Escape') return;
        if (aboutOpen) {
            aboutOpen = false;
            rerender();
            restoreFocus('about');
        } else if (selectedAccountKey) {
            clearAccount();
        }
    }

    function stop() {
        stopped = true;
        if (themeOff) { themeOff(); themeOff = null; }
        generation++;
        if (dataTimer !== null) { window.clearInterval(dataTimer); dataTimer = null; }
        document.removeEventListener('keydown', onDocumentKey);
        document.removeEventListener('pointermove', onDocumentPointer);
    }

    // The sheet is static: written once, outside the tree render() clears.
    function installStyle() {
        if (document.getElementById(STYLE_ID)) return;
        var style = el('style');
        style.id = STYLE_ID;
        style.textContent = STYLE;
        document.head.appendChild(style);
    }

    function start() {
        installStyle();
        // The host resolves Light/Dark/System; older hosts keep the dark palette.
        if (!themeOff && window.OuroborosWidget && typeof window.OuroborosWidget.onTheme === 'function') {
            themeOff = window.OuroborosWidget.onTheme(function (theme) {
                document.documentElement.dataset.theme = theme;
            });
        }
        document.addEventListener('keydown', onDocumentKey);
        document.addEventListener('pointermove', onDocumentPointer);
        if (dataTimer === null) {
            dataTimer = window.setInterval(function () {
                if (document.visibilityState === 'visible') load();
            }, REFRESH_MS);
        }
        load();
    }

    // The frame is disposable: the host removes it on Stop, on leaving the
    // page and when the skill's revision changes. A terminal disposal cannot
    // restart through pageshow; pagehide and back-forward restoration can.
    window.addEventListener('pagehide', stop);
    if (typeof window.__ouroWidgetOnDispose === 'function') {
        window.__ouroWidgetOnDispose(function () {
            disposed = true;
            stop();
        });
    }
    window.addEventListener('pageshow', function () {
        if (disposed || !stopped) return;
        stopped = false;
        inFlight = false;
        start();
    });
    document.addEventListener('visibilitychange', function () {
        if (document.visibilityState === 'visible' && !stopped) load();
    });

    start();
})();
