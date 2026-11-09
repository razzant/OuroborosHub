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
 * list selects; and that list itself. Every number is the skill's — but one: a
 * family with no time window (Cursor) shows its billing-cycle budget in the 30-day
 * place, and the share, the sum and the mean of those amounts are this widget's own
 * (the billing-cycle budget, in the window row's capsule).
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
    var DISPLAY_ROUTE = '/api/extensions/claudexor_quotas/display';
    var I18N_ROUTE = '/api/extensions/claudexor_quotas/i18n';
    // The text table is a small local read; it never holds up a reading for long.
    var I18N_TIMEOUT_MS = 8000;
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
        { key: 'no_new_use' },
        { key: 'recent_pace' }
    ];

    var root = document.getElementById('root');
    var generation = 0;
    var dataTimer = null;
    var lastGood = null;
    var lastGoodAt = 0;
    var stopped = false;
    var disposed = false;
    var themeOff = null;
    var inFlight = false, refreshing = false;
    // The family on screen, and the account selected in it — the top account of
    // the window until a bar or a row of the account list is clicked (pickDecide). Both survive the
    // 30-second redraw and fall away when what they point at is gone.
    var selectedHarness = '';
    var selectedAccountKey = '';
    // The bar or account-list row that selected it (its data-focus key):
    // where Clear and Escape give the keyboard back (clearAccount) — and, for
    // a bar, the limit it belongs to, where the account's card stands
    // (inspectorSlot).
    var selectionOpener = '';
    var selectionLimit = '';
    var pickAuto = false;
    var pickHome = '';
    var pickBare = '';
    var pickAt = '';
    var pickDue = false;
    var accountsOpen = false;
    // The accounts list stands in the place of the bars, the chart and the card (renderReserve, renderAccounts). accountsRun is 'open' or 'shut' for the one drawing that plays the fold
    // after the list was opened or shut (accountsSet), else ''; accountsSpace is the height of the place the list replaces, in px, measured at the moment it opens.
    var accountsRun = '';
    var accountsSpace = 0;
    var diagnosticsOpen = false;
    var limitDetailsOpen = Object.create(null);
    var aboutOpen = false;
    var currentView = null;
    var displayFrame = String(Date.now()) + '-' + String(Math.random()).slice(2);
    var displaySeq = 0;
    var displaySettled = 0;
    var staleMessage = '';
    var actionMessage = '';
    // The timeline: open on every mount. Its limit per family is the one the
    // reader picked, else the family's "lowest left"; the horizon is the
    // reader's pick ('' = the limit's own); the scenario starts at "no new
    // use". The last chart request asked on the reader's behalf is kept so a
    // mismatch is asked about once, never in a loop.
    var timelineOpen = true;
    // The timers of the pointer's and the focus's going off the card (a beat is kept so the edge does not flicker); the card the script drew last.
    var cardLeave = { hover: 0, focus: 0 };
    var cardWhere = '';
    var chartKeys = {};
    // The order of the head's family tabs (the ids): the order they were first seen in, kept in the widget's memory and nowhere else, so a family
    // that comes and goes in the answers keeps its place and a pick moves no tab.
    var familyOrder = [];
    var chartAsked = '';
    var horizonChoice = '';
    // 'window' draws the selected limit; 'joint' the same-account combined view
    // (one choice for this visit, like the span).
    var graphMode = 'window';
    // Which request the windows are read for: a model the family's limits
    // name ('' = a request naming no model) and the request kind. The model
    // list opens in place and survives a redraw, like About.
    var modelChoice = '';
    var kindChoice = 'text';
    var modelMenuOpen = false;
    var scenario = 'no_new_use';
    var detailsOpen = false;
    // The fold inside the Details ("How it is counted"), open or shut like the Details are.
    var howOpen = false;
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
    // The card everything under the head stands in (capsule: shell): the one the drawing in hand built. The chart takes
    // its room from it, not from the root, so the card's own padding is left out.
    var shellBox = null;
    var railBox = null;

    var STYLE_ID = 'claudexor-quotas-style';

    // One neutral surface and one type scale for the whole card.
    var STYLE = [
        // The tokens: every value the rules below use is written here, once. Dark is the default;
        // the light set (after the dark one) re-sets the colours only, and the tints, built on
        // --neutral-rgb, turn with it. Groups: colour, type, size, space, radius, line, motion.
        ":root{",
        "color-scheme:dark;",
        // colour
        "--surface:#202023;--inset:#29292d;",
        // The text roles: --text-secondary is Ouroboros' secondary text (7.8.0's own values), quieter than meta and still 4.5:1 for 12px
        // (in both themes)
        "--text-primary:#e4e4e7;--text-secondary:rgba(255,255,255,.58);--text-meta:#b0b0b8;--text-disabled:#787881;",
        "--edge:#38383e;--edge-strong:#62626c;--edge-idle:#7a7a86;--neutral-rgb:228,228,231;",
        "--status-ok:#82bd9c;--status-warn:#dfb574;--status-warn-bg:#3b3224;",
        "--status-bad:#e99a9f;--status-bad-bg:#40292e;",
        "--warn-text:var(--status-warn);--bad-text:var(--status-bad);",
        "--focus-accent-border:#ec7782;--share-ink:#a6a6b3;--scenario-ink:#9ab9fa;",
        "--share-track:rgba(var(--neutral-rgb),.07);--calm-a:#2fd359;--calm-b:#2fd359;",
        // colour: the accent, the one red of the shell (the sidebar's: the same in both themes); it paints the dot between About and Refresh and the
        // capital of a lit word (kit: text action), and, in the kit's mark tab, the dot and the number of a tab with a mark and the mark of the tab
        // that is picked or previewed; and the star of a ring whose accounts are not all read (the quota plate)
        "--accent:#c93545;",
        // colour: the ink at a strength (the .07 step is --share-track) and the shadow of a raised pill
        "--tint-1:rgba(var(--neutral-rgb),.035);--tint-2:rgba(var(--neutral-rgb),.05);--tint-3:rgba(var(--neutral-rgb),.06);",
        "--tint-5:rgba(var(--neutral-rgb),.24);",
        "--shadow-pop:0 1px 4px rgba(0,0,0,.18);",
        // The hatch of a last-known value: a 6px tile drawn once per theme, a
        // mark of state, not decoration.
        "--hatch:url(data:image/svg+xml;base64,PHN2ZyB4bWxucz0naHR0cDovL3d3dy53My5vcmcvMjAwMC9zdmcnIHdpZHRoPSc2JyBoZWlnaHQ9JzYnPjxwYXRoIGQ9J00tMSAxbDItMk0wIDZsNi02TTUgN2wyLTInIHN0cm9rZT0nI2E2YTZiMycgc3Ryb2tlLXdpZHRoPScxLjUnLz48L3N2Zz4=);",
        // type: the three sizes of the card and two for a figure (the display's and the plate's number); the weights and the line heights in use; the tracking of the small
        // meta type (the sidebar's, on its version number), worn by the text action and its dot
        "--type-meta:12px;--type-body:14px;--type-section:16px;--type-display:28px;--track-meta:.06em;",
        "--weight-light:300;--weight-regular:400;--weight-medium:500;--weight-semibold:600;",
        "--lh-body:1.5;--lh-ui:1.35;--lh-tight:1.15;",
        "--row-h:32px;--row-h-sm:26px;--tap-h:44px;--dot-size:5px;--sep-size:2px;--bar-min:20px;--bar-max:56px;",
        // space: a ladder of 2 4 6 8 10 12 16 24 (-1h is the half step between 4 and 8, -2h the one between 8 and 12)
        "--space-0:2px;--space-1:4px;--space-1h:6px;--space-2:8px;--space-2h:10px;--space-3:12px;--space-4:16px;--space-5:24px;",
        // radius (-field: the corner of the chart's plot area, -tile: a white tile or card, -card: the plate of the whole chart)
        "--radius-xs:3px;--radius-sm:6px;--radius-md:8px;--radius-field:10px;--radius-row:12px;--radius-tile:14px;--radius-card:18px;--radius-pill:999px;",
        // chart (the graph's field): the width of its curves, the radius of its dots (a reset at the foot, the cursor, the start of the scenario; the ring round a dot is --ring-w), and the dashes of its lines, each "dash gap" in the svg's unit (a dash of .1 with a round cap is a dot)
        "--chart-line:3px;--chart-dot-reset:4.5px;--chart-dot-cursor:6px;--chart-dot-start:5px;",
        "--chart-dash-carried:5 7;--chart-dash-scenario:.1 8;--chart-dash-grid:.1 7;--chart-dash-cursor:.1 6;--chart-dash-reset:.1 5;",
        // shell: the widget is the app's shell in small. The ground (--shell-bg, the sidebar's) carries the head; the card (the
        // page's: its colour, its corner, the gap that keeps it off the head and its glow) carries everything under the head. A block
        // that stands in the card at its padding (--shell-card-pad) has the corner of the card less that padding (--shell-card-inner-radius),
        // so the two corners are concentric. The light set re-sets the ground, the card's colour and its glow.
        "--shell-bg:#0d0b0f;--shell-card-bg:#0d0b0f;--shell-card-radius:20px;--shell-card-gap:var(--space-2);",
        "--shell-card-pad:var(--space-2);--shell-card-inner-radius:calc(var(--shell-card-radius) - var(--shell-card-pad));",
        // The room inside the card on the wide shell (from 860px; the narrow one keeps --shell-card-pad): --shell-card-pad-y over the first part and under the last, --shell-card-pad-x at the sides.
        "--shell-card-pad-y:var(--space-5);--shell-card-pad-x:var(--space-4);",
        "--shell-card-shadow:-6px 0 20px rgba(255,255,255,.05),0 0 10px rgba(255,255,255,.04);",
        // the ring (the box it stands in: the kit's plain size and the large one)
        "--ring-size:28px;--ring-size-lg:47px;",
        // the size of a family's mark in the head (FAMILY_MARK_PX in the script)
        "--mark-px:14px;",
        // colour: the kit's plain ring is the ink at a strength (built on --neutral-rgb, it turns with the theme): its track, arc and dot
        "--ring-track:rgba(var(--neutral-rgb),.16);--ring-arc:rgba(var(--neutral-rgb),.55);--ring-dot:rgba(var(--neutral-rgb),.72);",
        // colour: the app's own (ui.css): the capsule of a window on the narrow shell, its elevation at rest, and the crimson tone of the capsule picked
        "--surface-capsule:rgba(44,34,52,.62);--elev-rest:0 1px 2px rgba(0,0,0,.5),0 8px 24px rgba(0,0,0,.35);",
        "--tone-crimson:rgba(240,90,106,.18);--tone-crimson-ink:#ffa0aa;",
        "--border-w:1px;--ring-w:2px;--ring-offset:2px;--focus-line:var(--ring-w) solid var(--focus-accent-border);",
        // motion: durations from the shortest and the curve
        "--dur-1:120ms;--dur-2:140ms;--dur-5:220ms;",
        "--ease:cubic-bezier(.2,.8,.2,1);",
        // motion: the bars' opening (kit: bars): the basis, the share of the free room and the most of a bar move in --dur-bars on --ease-bars, a longer and more even run than --dur-5 on --ease, so the pair that shuts and opens does not leap in its first frames
        // motion: the blink of a dot, as the sidebar's dots blink while it checks: it dims to --blink-dim and back in --dur-blink (.15, .7s)
        "--dur-blink:700ms;--blink-dim:.15;",
        "--nav-spring:cubic-bezier(.34,1.5,.64,1);--dur-nav:500ms;",
        // motion: the press of a row of the rail (the app's: a state changes colour or size, never the height): under the finger or the button the ring
        // shrinks to --press of its size in --dur-1 and comes back on the spring (--nav-spring, --dur-nav) when it is let go
        "--press:.94;",
        // what the script sets on a node's own style (the room the account list takes), declared at its rest value
        "--acc-space:auto;",
        // The quota plate's own: the capsule its rings and steps stand on, the plate, the selected (crimson) ring, step and card, what is left (green), held (amber),
        // at the limit (red), the hatches of a last-known and an unread bar, and the sizes of the rail, the ring, the card and the bars (qBarsFit writes the last three).
        "--q-capsule:rgba(255,255,255,.07);--q-capsule-hover:rgba(255,255,255,.11);--q-plate:#1b1b1e;--q-edge:rgba(255,255,255,.09);--q-track:rgba(255,255,255,.075);",
        "--q-sel:#872236;--q-sel-ink:#ffdfe1;--q-sel-meta:rgba(255,223,225,.78);--q-sel-rule:rgba(255,223,225,.16);--q-outline:#fe7d85;",
        "--q-green:#2fd359;--q-amber:#f7cb58;--q-red:#ef4444;--q-chip:rgba(255,255,255,.06);--q-limit-bg:#c42127;--q-limit-ink:#fff;--q-held-ink:#2b1d00;",
        "--q-shadow:0 1px 2px rgba(0,0,0,.4),0 6px 20px rgba(0,0,0,.28);--q-sel-shadow:0 0 0 var(--ring-w) var(--q-outline);--q-lit-shadow:0 0 0 var(--ring-w) var(--q-green);",
        "--q-dim-shadow:0 0 0 var(--ring-w) var(--q-amber);--q-edge-shadow:inset 0 0 0 var(--border-w) var(--q-edge);--q-spent-shadow:inset 0 -3px 0 var(--q-red);",
        "--q-hatch-green:url(data:image/svg+xml;base64,PHN2ZyB4bWxucz0naHR0cDovL3d3dy53My5vcmcvMjAwMC9zdmcnIHdpZHRoPSc2JyBoZWlnaHQ9JzYnPjxwYXRoIGQ9J00tMSAxbDItMk0wIDZsNi02TTUgN2wyLTInIHN0cm9rZT0nIzJmZDM1OScgc3Ryb2tlLXdpZHRoPScyLjInLz48L3N2Zz4=);",
        "--q-hatch-amber:url(data:image/svg+xml;base64,PHN2ZyB4bWxucz0naHR0cDovL3d3dy53My5vcmcvMjAwMC9zdmcnIHdpZHRoPSc2JyBoZWlnaHQ9JzYnPjxwYXRoIGQ9J00tMSAxbDItMk0wIDZsNi02TTUgN2wyLTInIHN0cm9rZT0nI2Y3Y2I1OCcgc3Ryb2tlLXdpZHRoPScyLjInLz48L3N2Zz4=);",
        "--q-hatch-grey:url(data:image/svg+xml;base64,PHN2ZyB4bWxucz0naHR0cDovL3d3dy53My5vcmcvMjAwMC9zdmcnIHdpZHRoPSc2JyBoZWlnaHQ9JzYnPjxwYXRoIGQ9J00tMSAxbDItMk0wIDZsNi02TTUgN2wyLTInIHN0cm9rZT0nIzZiNmI3NScgc3Ryb2tlLXdpZHRoPScxJy8+PC9zdmc+);",
        "--q-rail-w:96px;--q-ring:46px;--q-ring-sm:28px;--q-card-w:188px;--q-cols:10;--q-bw:56px;--q-bh:96px;--q-bar-gap:5px;--q-mark:7px;--q-swatch:10px;--q-base:3px;}",
        ":root[data-theme=light]{color-scheme:light;--surface:#fff;--inset:#f3f3f5;",
        "--text-primary:#26262b;--text-secondary:#626977;--text-meta:#60606b;--text-disabled:#90909a;",
        "--edge:#e2e2e7;--edge-strong:#91919d;--edge-idle:#8a8a96;--neutral-rgb:38,38,43;",
        "--status-ok:#38704f;--status-warn:#845713;--status-warn-bg:#fbf5e9;",
        "--status-bad:#a73c49;--status-bad-bg:#fcf0f1;",
        "--focus-accent-border:#b62f42;--share-ink:#787884;--scenario-ink:#365eac;--calm-a:#16a34a;--calm-b:#16a34a;",
        "--shell-bg:#f5f6f8;--shell-card-bg:#fff;--shell-card-shadow:-8px 0 22px rgba(24,16,32,.07),0 0 12px rgba(24,16,32,.055);",
        "--surface-capsule:#fff;--elev-rest:0 1px 2px rgba(28,33,51,.05),0 6px 18px rgba(28,33,51,.06);",
        "--tone-crimson:rgba(201,53,69,.1);--tone-crimson-ink:#b52b3b;",
        "--q-capsule:#fff;--q-capsule-hover:#f5f6f8;--q-plate:#fff;--q-edge:rgba(32,35,43,.08);--q-track:rgba(32,35,43,.065);",
        "--q-sel:rgba(201,53,69,.1);--q-sel-ink:#b52b3b;--q-sel-meta:#9a3a46;--q-sel-rule:rgba(181,43,59,.14);--q-outline:#bf2f40;",
        "--q-green:#22c55e;--q-amber:#f59e0b;--q-chip:rgba(32,35,43,.05);",
        "--q-shadow:0 1px 2px rgba(28,33,51,.05),0 6px 18px rgba(28,33,51,.06);",
        "--q-hatch-green:url(data:image/svg+xml;base64,PHN2ZyB4bWxucz0naHR0cDovL3d3dy53My5vcmcvMjAwMC9zdmcnIHdpZHRoPSc2JyBoZWlnaHQ9JzYnPjxwYXRoIGQ9J00tMSAxbDItMk0wIDZsNi02TTUgN2wyLTInIHN0cm9rZT0nIzIyYzU1ZScgc3Ryb2tlLXdpZHRoPScyLjInLz48L3N2Zz4=);",
        "--q-hatch-amber:url(data:image/svg+xml;base64,PHN2ZyB4bWxucz0naHR0cDovL3d3dy53My5vcmcvMjAwMC9zdmcnIHdpZHRoPSc2JyBoZWlnaHQ9JzYnPjxwYXRoIGQ9J00tMSAxbDItMk0wIDZsNi02TTUgN2wyLTInIHN0cm9rZT0nI2Y1OWUwYicgc3Ryb2tlLXdpZHRoPScyLjInLz48L3N2Zz4=);",
        "--q-hatch-grey:url(data:image/svg+xml;base64,PHN2ZyB4bWxucz0naHR0cDovL3d3dy53My5vcmcvMjAwMC9zdmcnIHdpZHRoPSc2JyBoZWlnaHQ9JzYnPjxwYXRoIGQ9J00tMSAxbDItMk0wIDZsNi02TTUgN2wyLTInIHN0cm9rZT0nI2I5YmNjNCcgc3Ryb2tlLXdpZHRoPScxJy8+PC9zdmc+);",
        "--hatch:url(data:image/svg+xml;base64,PHN2ZyB4bWxucz0naHR0cDovL3d3dy53My5vcmcvMjAwMC9zdmcnIHdpZHRoPSc2JyBoZWlnaHQ9JzYnPjxwYXRoIGQ9J00tMSAxbDItMk0wIDZsNi02TTUgN2wyLTInIHN0cm9rZT0nIzc4Nzg4NCcgc3Ryb2tlLXdpZHRoPScxLjUnLz48L3N2Zz4=)}",
        /* base */
        // Reset, the page and the focus ring every focusable thing shares.
        // The page is the shell's ground (--shell-bg), and keeps the shell's gap (--shell-card-gap) to its edges; the root holds
        // the head and, under it, the one card (capsule: shell). The card's own padding is what the page had besides that gap
        // (16px, 12px under 520px), so what stands in the card keeps the place and the width it had.
        "*{box-sizing:border-box}",
        "body{margin:0;padding:var(--shell-card-gap);font:var(--type-body)/var(--lh-body) -apple-system,BlinkMacSystemFont,\"Segoe UI\",Roboto,sans-serif;color:var(--text-primary);background:var(--shell-bg);-webkit-font-smoothing:antialiased}",
        "#root{display:flex;flex-direction:column;min-width:0}",
        "button{font:inherit;color:var(--text-primary);cursor:pointer}",
        "button:disabled{color:var(--text-disabled);cursor:default}",
        // A button and a summary wear the ring by what they are; a region the keyboard reaches (tabindex) wears .focus-ring.
        "button:focus-visible,summary:focus-visible,.focus-ring:focus-visible{outline:var(--focus-line);outline-offset:var(--ring-offset)}",
        "button svg{flex:none}",
        /* kit: screen-reader */
        // Kept for the screen reader, not drawn. It stays first among the kit: a caption that is also .sr-only takes its margin from here.
        // Tokens it reads: none.
        ".sr-only{position:absolute;width:1px;height:1px;padding:0;margin:-1px;overflow:hidden;clip:rect(0 0 0 0);white-space:nowrap;border:0}",
        /* kit: buttons and pills */
        // One button look for the page button and the segment's option; a part adds only its own differences. The page button
        // (.pill-btn) and the option (.seg-opt) are the two variants of it. The tab of a row of choices wears none of it: it has no plate
        // (kit: mark tab). The button that is its word alone is the kit's next section (kit: text action).
        // Tokens it reads: --space-1h --row-h --space-1 --space-2h --border-w --radius-sm --text-primary --type-meta --lh-ui --inset --edge --weight-semibold
        // --surface --text-disabled --row-h-sm --space-0 --space-2 --tint-2.
        ".pill-btn,.seg-opt{display:inline-flex;align-items:center;justify-content:center;gap:var(--space-1h);min-height:var(--row-h);padding:var(--space-1) var(--space-2h);border:var(--border-w) solid transparent;border-radius:var(--radius-sm);background:transparent;color:var(--text-primary);font-size:var(--type-meta);line-height:var(--lh-ui);position:relative}",
        ".pill-btn:hover,.seg-opt:hover{background:var(--inset)}",
        ".pill-btn.on,.seg-opt.active{background:var(--inset);border-color:var(--edge);font-weight:var(--weight-semibold)}",
        ".pill-btn{border-color:var(--edge);background:var(--surface)}",
        // A page button at its end stays focusable, so the keyboard is not dropped.
        ".pill-btn[aria-disabled=true]{color:var(--text-disabled);background:var(--surface);cursor:default}",
        // The compact size, 26px: the same button where a line of controls or of text has no room for a row of 32. A kit class
        // worn by the part: the chart control of a row, the model button and a scope option wear .btn-compact (2px 8px); the retry
        // of a banner and the schedule's "show all" wear .btn-compact-wide (2px 10px). They share the height.
        // The option of the graph's pill segment is the same size, written with the pill (capsule: chart).
        ".btn-compact,.btn-compact-wide{min-height:var(--row-h-sm)}",
        ".btn-compact{padding:var(--space-0) var(--space-2)}",
        ".btn-compact-wide{padding:var(--space-0) var(--space-2h)}",
        /* kit: text action */
        // The text action. What it is: a button that is its word alone, as the sidebar's version check is: no border, no plate and no
        // padding sideways (on hover either), no icon and no coloured status. The word is in the meta size with the sidebar's tracking.
        // When to use it: for a quiet secondary action in a head or in a meta line (About, Refresh). Never for the primary action of a
        // card, and never where the action must be found at a glance: that is a button with a plate (.pill-btn).
        // How to use it: wordButton(t-key) makes each word; wordRow(buttons, busy) makes a row of two or three words (it wraps as one
        // line of a head would, so a long row could leave a dot at a line's end) with a dot between each pair
        // (busy makes the dots blink); wordDot(busy) is the dot alone. The three stand together in widget.js and nothing else builds a
        // word, a row or a dot, so the classes below (.btn-word, .btn-word-cap, .btn-word-dot, .btn-word-row) are worn through them.
        // A part adds its wiring to a button it gets (data-focus, aria-expanded or aria-pressed, the click) and its place to a row (the head's
        // .action-seg), never a look.
        // States: at rest the word is in --text-secondary. Lit, by :hover, :focus-visible, :active and [aria-expanded=true], and not
        // when it is disabled, the word is in --text-primary and its capital, the first letter (.btn-word-cap), in --accent.
        // A word that is a toggle (aria-pressed=true: what it opens is shown) is in --text-primary at rest too, its capital still the
        // word's own: the accent is for the lit word alone (hover, focus, press), or a red capital would stand on every open plate. Such a
        // word sets aria-pressed and never aria-expanded, which would light it.
        // Busy, a read in the air: the button is disabled AND aria-busy=true, always the two together (the lit rule leaves out only
        // :disabled), the word stays the same word in its rest ink, and the dot beside it blinks (.is-busy: the one class of the blink, written in kit: motion).
        // Motion: the word and its capital fade in colour only (--dur-5 on --ease); the busy dot blinks by the rule .is-busy of kit: motion. Under reduced motion
        // nothing fades, and a busy dot stands still (the same rule's pair).
        // Tokens it reads: --text-secondary --text-primary --accent --type-meta --track-meta --lh-ui --weight-semibold --row-h-sm
        // --tap-h --radius-sm --space-0 --space-1 --space-2 --dur-5 --ease. It writes no colour and no size of its own.
        // Accessibility: the name of the button is its word, so no aria-label and no title (the word is seen); the span of the capital
        // has no attribute, so a screen reader reads the whole word; the dot is aria-hidden; the button is 26px high and 44 by 44 under
        // a finger (kit: touch); its ring is the base ring.
        // A mark: wordButton(key, params, iconName) puts the kit's stroke icon before the word (.btn-word-mark, aria-hidden, the word's ink), and the
        // word itself in a span (.btn-word-body) that the narrow shell (520px and under) folds to a screen reader's (.btn-word-short: the mark stands
        // alone, the name of the button is still its word). Refresh wears it; About has none.
        // Not to do: no plate, no border, no colour of its own (within the text action the accent is the dot's and the lit capital's, nothing else),
        // no other word while busy (one button, one word), no flex or grid on the button itself.
        // The button is an inline-block (a button centres its content by itself), never a flex or grid row: the capital is a span in it,
        // and a span that is a flex item is block-level, which a screen reader names with a gap in the word ("A bout").
        ".btn-word{display:inline-block;flex:none;appearance:none;min-height:var(--row-h-sm);padding:0;border:0;border-radius:var(--radius-sm);background:none;color:var(--text-secondary);font-size:var(--type-meta);line-height:var(--lh-ui);letter-spacing:var(--track-meta);white-space:nowrap;transition:color var(--dur-5) var(--ease)}",
        ".btn-word:is(:hover,:focus-visible,:active,[aria-expanded=true]):not(:disabled){color:var(--text-primary)}",
        ".btn-word[aria-pressed=true]:not(:disabled){color:var(--text-primary)}",
        // The capital: the first letter of the word stands in a span of its own (.btn-word-cap), and the rest of the word follows it as
        // plain text. At rest the capital is the word's ink (it has no colour of its own, it takes the button's); when the word is lit,
        // by the same states and not when disabled, it turns the accent while the rest of the word goes to the primary ink. It fades
        // with the word's own fade (--dur-5, --ease) and nothing but its colour moves. A busy Refresh is disabled, so no red capital.
        ".btn-word-cap{transition:color var(--dur-5) var(--ease)}",
        ".btn-word:is(:hover,:focus-visible,:active,[aria-expanded=true]):not(:disabled) .btn-word-cap{color:var(--accent)}",
        // A word that waits (aria-busy=true) is disabled, and the base's `button:disabled` ink would grey it: that rule (0,1,1) is stronger
        // than .btn-word (0,1,0). This one (0,2,0) wins over it, so the busy word keeps the ink it has at rest; the work is shown by the dot.
        ".btn-word[aria-busy=true]{color:var(--text-secondary)}",
        // The dot between two words: the accent (it and the lit capital are the two things the accent paints within the text action), out of the reading order
        // (aria-hidden) and not to be selected. Its margin and the row's gap (.btn-word-row, below) are the air round it. While a read is
        // in the air it blinks, as the dots of the sidebar's version number do while it checks (the keyframes: blink).
        ".btn-word-dot{margin:0 var(--space-2);color:var(--accent);font-size:var(--type-meta);font-weight:var(--weight-semibold);line-height:var(--lh-ui);letter-spacing:var(--track-meta);-webkit-user-select:none;user-select:none}",
        // The row of words (wordRow): the words and the dots between them on one line, centred on each other. The air round a dot is its
        // margin (--space-2, 8px a side) and this row's small gap (--space-0, 2px a side): about 12px from the ink of a word to the ink of
        // the dot (12.3 and 12.7 measured, the approved air). The row may wrap, so a row that has no room does not run out of its place;
        // a part gives it its place (margin, height, width) and nothing else.
        ".btn-word-row{display:flex;align-items:center;flex-wrap:wrap;gap:var(--space-0)}",
        // Without motion nothing fades (the word and its capital take their ink at once) and nothing blinks; a busy dot would then show
        // nothing, so it stays dimmed to the blink's low end (--blink-dim): the reduced pair of the blink is a still dim dot.
        ".btn-word-mark{display:inline-block;vertical-align:-3px;margin-right:var(--space-1)}",
        ".btn-word-short{position:relative}",
        "@media(max-width:520.98px){.btn-word-short .btn-word-mark{margin-right:0}.btn-word-short .btn-word-body{position:absolute;left:0;top:0;width:1px;height:1px;overflow:hidden;clip:rect(0 0 0 0);white-space:nowrap}}",
        "@media(prefers-reduced-motion:reduce){.btn-word,.btn-word-cap{transition:none}}",
        /* kit: mark tab */
        // The mark tab. What it is: a button that is one choice of a row, told by its mark: the model's mark (a drawing, or the initial where there is
        // none), its name and, when it has a number, that number after the name. It is a quiet capsule: no plate at rest, the neutral veil under the
        // pointer, and the crimson of the pick (--q-sel) when it is picked, as the ring of a limit and the card of an account are: one colour says
        // "picked" everywhere in the widget. When to use it: for a row of choices that each have a mark and of which one is picked (the head's
        // families). Never for a command (that is .pill-btn or a text action), and never for a row of plain words (the segment). How to use it:
        // markTab({key, mark, name, count, pressed}) makes each tab (key: its data-mark-key; mark: a node of the caller's; name: the text; count: a
        // number, or none; pressed: true or false for a choice, none for a tab that is no choice) and markRow(tabs) the row of them. The helpers stand
        // together in widget.js and nothing else builds a tab, a mark, a number or a row, so the classes below (.tab-mark, .tab-mark-icon, .tab-mark-name,
        // .tab-mark-count, .tab-mark-row) are worn through them. A part adds its wiring to a tab (aria-label, title, data-focus, the click) and its place
        // to a row (the head's .harness-seg), never a look. States: at rest the tab is in the secondary ink on no ground; under the pointer or the keyboard
        // the neutral veil (--q-capsule-hover) and the primary ink; picked (aria-pressed=true) the crimson ground, the crimson ink and the semibold weight;
        // the number is the meta ink, and the crimson meta ink on the picked tab. The order of the tabs never changes with the pick: a pick moves no tab.
        // On the narrow shell (520px and under) a tab not picked keeps its mark and its number and folds its name (the name stays its label), so the
        // four families, the picked one named, and the words at the right stand on one line. Motion: the ground and the ink fade (--dur-1 on --ease);
        // nothing moves and nothing grows; under reduced motion nothing fades. Tokens it reads: --row-h --radius-pill --space-0 --space-1 --space-1h --space-2
        // --text-secondary --text-primary --text-meta --q-capsule-hover --q-sel --q-sel-ink --q-sel-meta --type-meta --lh-ui --weight-regular
        // --weight-semibold --weight-medium --mark-px --dur-1 --ease. Accessibility: the pick is aria-pressed; a tab with a number carries an aria-label that
        // says what it counts ("Claude — 2 accounts": the part's wiring); 32px high, 44px under a finger (kit: touch); its ring is the base ring. Not to do:
        // no border, no shadow, no reorder on a pick, no second colour for the pick, no keyframes.
        ".tab-mark-row{display:flex;align-items:center;flex-wrap:wrap;gap:var(--space-0)}",
        ".tab-mark{display:inline-flex;align-items:center;gap:var(--space-1h);min-height:var(--row-h);padding:0 var(--space-2);border:0;border-radius:var(--radius-pill);background:none;appearance:none;color:var(--text-secondary);font-size:var(--type-meta);font-weight:var(--weight-regular);line-height:var(--lh-ui);white-space:nowrap;transition:background-color var(--dur-1) var(--ease),color var(--dur-1) var(--ease)}",
        ".tab-mark:is(:hover,:focus-visible):not([aria-pressed=true]):not(:disabled){background:var(--q-capsule-hover);color:var(--text-primary)}",
        ".tab-mark[aria-pressed=true]{background:var(--q-sel);color:var(--q-sel-ink);font-weight:var(--weight-semibold)}",
        ".tab-mark-icon{flex:none;width:var(--mark-px);height:var(--mark-px)}",
        ".tab-mark-count{color:var(--text-meta);font-weight:var(--weight-regular);font-variant-numeric:tabular-nums}",
        ".tab-mark[aria-pressed=true] .tab-mark-count{color:var(--q-sel-meta);font-weight:var(--weight-medium)}",
        "@media(max-width:520.98px){.tab-mark{gap:var(--space-1);padding:0 var(--space-1h)}.tab-mark[aria-pressed=true]{gap:var(--space-1h);padding:0 var(--space-2)}.tab-mark:not([aria-pressed=true]) .tab-mark-name{display:none}}",
        "@media(prefers-reduced-motion:reduce){.tab-mark{transition:none}}",
        /* kit: ring */
        // The ring. What it is: a gauge of one share, an open ring of 300 degrees with its gap of 60 degrees at the bottom: a track, an arc of the share that is left (round at both ends) and a dot at the end of the arc. It has two forms and the form is all it says: ok (the share is known: track, arc and dot) and unknown (it is not: the track alone, dotted, no arc and no dot). No colour of a state, no red.
        // When to use it: beside a name that has a share to tell (a window of a limit; later an account), where the share must be seen at a glance and its number stands next to it. Never alone: the number is the ring's word, and the ring itself is out of the reading order.
        // The two sizes are both the kit's: the large ring (lg) is the quota plate's; the plain ring (28px, the size left out) is the kit's base size, the form a part that tells a share beside a name takes.
        // How to use it: ringGauge(pct, form, size) is the one builder (pct: the share left, 0 to 100; form: ok or unknown, and a pct that is no number is unknown;
        // size: left out for the plain ring, lg for the large one with the thinner strokes, the quota plate's rings). It makes the svg (viewBox 0 0 184 184, the
        // size --ring-size, or --ring-size-lg for the large one, aria-hidden, not focusable) with its track, arc and dot, the geometry in the svg's own attributes (one path for
        // the track and the arc; pathLength 100 and a dash of pct; the dot's centre at the angle 120 + 3 x pct degrees) and the classes ring, ring-track, ring-arc and ring-dot,
        // which are worn through it and nowhere else. A part that wears the ring may set its size and its colours by its own class (the quota plate's ring is --q-ring wide and
        // green); the ring draws its share at once, with no motion.
        // States: the colours are the tokens', the ink at a strength, and turn with the theme: the track --ring-track, the arc --ring-arc, the dot --ring-dot.
        // Tokens it reads: --ring-size --ring-size-lg --ring-track --ring-arc --ring-dot. Not to do: no colour of a state, no number drawn in the svg (the box the ring stands in
        // may hold one: the quota plate's ring holds its share), no motion (the share is in place from the first frame, and no script runs per frame).
        ".ring{display:block;flex:none;width:var(--ring-size);height:var(--ring-size);overflow:visible}",
        ".ring-lg{width:var(--ring-size-lg);height:var(--ring-size-lg)}",
        ".ring-track{stroke:var(--ring-track)}",
        ".ring-arc{stroke:var(--ring-arc)}",
        ".ring-dot{fill:var(--ring-dot)}",
        /* kit: segmented control */
        // A row of options that look like buttons: the segment, its caption and its option.
        // Tokens it reads: --space-0 --type-meta --text-meta --space-1 --edge.
        ".seg{display:inline-flex;align-items:center;flex-wrap:wrap;gap:var(--space-0)}",
        ".seg-label{font-size:var(--type-meta);color:var(--text-meta);margin-right:var(--space-1)}",
        ".seg-opt{border-color:var(--edge)}",
        /* kit: rows and tables */
        // Rows: the list of limits and the row of one limit (its figure, its bars and the words under them, its chart word).
        // Tokens it reads: --space-3 --space-1 --space-4 --lh-ui --type-section --weight-semibold --text-meta --type-meta --space-2 --space-0 --edge-strong
        // --weight-light --lh-tight --tone-crimson-ink --text-primary --track-meta --dur-nav --nav-spring --weight-regular --focus-line
        // --ring-offset --radius-sm --press --dur-1 --ease --border-w --edge --weight-medium --type-body.
        ".lrows{margin-top:0}",
        // The figure of a row: the account-windows left, its unit beside it. It has no box (display: contents): the number and its unit are
        // the grid items of the fact (capsule: window tabs).
        ".l-fig{display:contents;white-space:nowrap;line-height:var(--lh-ui)}",
        // The figure (.num-strong, the section size at the semibold weight) and the quiet figure beside it (.meta-num, the meta
        // size and ink): both on tabular digits, worn by the figure of a row and by the next reset of the graph.
        ".num-strong{font-size:var(--type-section);font-weight:var(--weight-semibold);font-variant-numeric:tabular-nums}",
        ".meta-num{color:var(--text-meta);font-size:var(--type-meta);font-variant-numeric:tabular-nums}",
        ".cover-link{margin-top:var(--space-2);padding:var(--space-0) 0;border:0;background:none;text-align:left;font-size:var(--type-meta);color:var(--text-meta);text-decoration:underline;text-decoration-color:var(--edge-strong);text-underline-offset:3px}",
        // Narrower still, the figure may wrap.
        "@media(max-width:360px){.l-fig{white-space:normal}}",
        // The figure of the plate is its headline: --type-figure, thin (--weight-light), on both lines of the fact's grid, at the left of the window's name and of the
        // unit's quiet words. It is the plate's alone (the kit's .num-strong stays what the graph reads). Its decimal point is the crimson ink (.wt-dot, a
        // span of its own). With no current reading the figure is a dash in the quiet ink: never a zero.
        ".wt-dot{color:var(--tone-crimson-ink)}",
        // The accounts at the limit now (or what a billing cycle spent beyond its plan), in words and not in a bubble: "At the limit · 3", the names
        // after it (the ink) and when the soonest of them is back (the quiet ink). It takes the keyboard (the accounts are named in its hint), and under the pointer or
        // the keyboard it lights their bars (the plate dims the bars but theirs). Its touch box under a finger is the "N more" button's own (.wt-foot-more);
        // it answers a press with the kit's (--press, the spring back).
        ".wt-limit{grid-column:1/-1;justify-self:start;min-width:0;max-width:100%;color:var(--text-meta);font-size:var(--type-meta);line-height:var(--lh-ui);letter-spacing:var(--track-meta);overflow-wrap:anywhere;transition:transform var(--dur-nav) var(--nav-spring)}",
        ".wt-limit-head{margin-right:var(--space-2);color:var(--text-primary);font-weight:var(--weight-regular);font-variant-numeric:tabular-nums}",
        ".wt-limit[tabindex]{cursor:default}",
        // Backwards, not both: the end of the keyframes is the rest, and a fill held after it would stand over the press (:active) of the bubble.
        "@media(prefers-reduced-motion:reduce){.wt-limit{transition:none}}",
        // Tables: the data tables of the details, the schedule and the facts table; each wears .tbl. One table: the full width,
        // the figures aligned, a cell a line above it in the quiet ink, a head cell and a caption in the primary ink at the
        // medium weight (.emph is that ink and weight for a word as well: a bold figure in a line, a tip's time and value).
        // The cell paddings stay each in its own rule (5px here, the facts table's 6px in capsule: chart, the later): the schedule
        // is drawn inside the Details, where a cell is both and the later rule wins.
        ".tbl{width:100%;border-collapse:collapse;font-size:var(--type-meta);font-variant-numeric:tabular-nums}",
        ".tbl th,.tbl td{border-top:var(--border-w) solid var(--edge);text-align:left;color:var(--text-meta);vertical-align:top}",
        ".schedule th,.schedule td{padding:5px var(--space-2) 5px 0}",
        ".emph,.tbl th{font-weight:var(--weight-medium);color:var(--text-primary)}",
        ".tbl caption{text-align:left;font-weight:var(--weight-medium);color:var(--text-primary)}",
        ".schedule caption{padding-bottom:var(--space-1);font-size:var(--type-body)}",
        ".schedule td.n{text-align:right;color:var(--text-primary)}",
        ".schedule th.n{text-align:right}",
        /* kit: legend */
        // The quiet line, in the meta size and the meta ink: a legend, the notes under a table, the facts of a graph or of an
        // account, a stamp or a note. A part wears .meta-line and adds only its own layout (flex or grid, gap, margin); the
        // legend of the calm graph is the kit's own. A column of quiet lines (the graph's facts, an account's windows) is .line-stack.
        // Tokens it reads: --type-meta --text-meta --space-1 --space-3 --space-2 --space-1h.
        ".meta-line,.calm-legend{font-size:var(--type-meta);color:var(--text-meta)}",
        ".line-stack{display:flex;flex-direction:column;gap:var(--space-1);margin-top:var(--space-1)}",
        // A line of items, each a mark and a few words.
        ".calm-legend{display:flex;flex-wrap:wrap;gap:var(--space-1) var(--space-3);margin-top:var(--space-2)}",
        ".calm-legend .legend-item{align-items:center}",
        ".calm-legend .legend-item svg{margin-top:0}",
        ".legend-item{display:flex;align-items:flex-start;gap:var(--space-1h);overflow-wrap:anywhere}",
        ".legend-item svg{flex:none;margin-top:var(--space-1)}",
        /* kit: panels and cards */
        // A bordered surface: the account card, the account list, the About panel, the model list's panel and the empty card, each
        // with its own padding; and the card standing in a list as a band. The model list's panel and the empty card wear .card.
        // Tokens it reads: --border-w --edge --radius-md --surface --space-3 --space-4 --surface-capsule --radius-tile --elev-rest --space-2 --type-section
        // --weight-semibold.
        ".inspector,.accounts,.about-panel,.card{border:var(--border-w) solid var(--edge);border-radius:var(--radius-md);background:var(--surface)}",
        ".inspector,.accounts,.about-panel{min-width:0;padding:var(--space-3) var(--space-4)}",
        // A white tile: the surface a card wears where it stands on the grey plate of the chart (its schedule, a moment's number, the limit's details), its corner and its soft shadow; the part adds only its own room.
        ".tile{background:var(--surface-capsule);border-radius:var(--radius-tile);box-shadow:var(--elev-rest)}",
        // The selected account's card stands in the list it was chosen from as
        // one more band of it, in the list's own rhythm; under a folded list's
        // header it stays a card.
        ".lrows>.inspector{border-width:0 0 var(--border-w);border-radius:0;padding:var(--space-3) var(--space-2)}",
        // The head of a card: its title across from its controls (.split-row), the title at the section size (.title-section).
        ".split-row{display:flex;align-items:center;justify-content:space-between;gap:var(--space-2);flex-wrap:wrap}",
        ".title-section{margin:0;font-size:var(--type-section);font-weight:var(--weight-semibold)}",
        "@media(max-width:520.98px){.inspector,.accounts,.about-panel{padding:var(--space-3)}}",
        /* kit: badges */
        // A dot of state, and a dot with its label.
        // Tokens it reads: --text-disabled --status-ok --status-warn --status-bad --space-1h --type-meta --text-meta --text-primary.
        ".pip,.state-dot{display:inline-block;flex:none;width:6px;height:6px;border-radius:50%;background:var(--text-disabled)}",
        ".pip.ok,.state-dot.ok{background:var(--status-ok)}",
        ".pip.warn,.state-dot.warn{background:var(--status-warn)}",
        ".pip.bad,.state-dot.bad{background:var(--status-bad)}",
        ".pip.muted,.state-dot.muted{background:var(--text-disabled)}",
        ".dot-label{display:inline-flex;align-items:center;gap:var(--space-1h);font-size:var(--type-meta);color:var(--text-meta)}",
        ".dot-label.strong{color:var(--text-primary)}",
        /* kit: motion */
        // The keyframes a part may borrow, by name: the blink of a dot (the text action's and the stamp's, while a read is in the air). Opacity only.
        // Each animation that wears one, with its reduced-motion pair, is written with the part.
        // The one class a part wears for the blink: .is-busy on a dot (a read is in the air: it dims to --blink-dim and back in --dur-blink on --ease); under
        // reduced motion it does not move and stays at the dim.
        // Tokens it reads: --dur-blink --ease --blink-dim.
        ".is-busy{animation:blink var(--dur-blink) var(--ease) infinite}",
        "@media(prefers-reduced-motion:reduce){.is-busy{animation:none;opacity:var(--blink-dim)}}",
        "@keyframes blink{50%{opacity:var(--blink-dim)}}",
        /* kit: inputs */
        // The rows menu: a fold with a checkbox for each row; a quiet line (.meta-line).
        // Tokens it reads: --space-2.
        ".rows-menu{margin-top:var(--space-2)}",
        ".rows-menu summary{cursor:pointer;min-height:24px}",
        ".rows-menu label{display:flex;gap:var(--space-2);align-items:center;min-height:28px}",
        /* kit: touch */
        // Under a coarse pointer (a finger) a pressable kit part is at least --tap-h high; with a mouse the sizes above stay. A
        // capsule whose own rule sets a smaller height (the graph's pill segment, the Details summary, an account row) lifts it with
        // a line of its own beside that rule.
        // Tokens it reads: --tap-h.
        "@media(pointer:coarse){.pill-btn,.seg-opt,.btn-compact,.btn-compact-wide,.btn-word,.tab-mark,.cover-link,.rows-menu summary,.rows-menu label{min-height:var(--tap-h)}.btn-word{min-width:var(--tap-h)}}",
        /* kit: press */
        // The press of a control that has no plate of its own to sink (:active, mouse and finger): it shrinks to --press of its size in --dur-1 on --ease, and when it is let go
        // the control's own rule takes it back. Written once, for the controls it lists: the window row's limit line that the keyboard reaches, the banner's retry and the scope option. Each has its pair under reduced motion: nothing
        // moves, the press does not shrink it. The press of the rail's ring is the rail's own (kit: rail): the kit of a tab keeps every rule of the tab.
        // Tokens it reads: --press --dur-1 --ease.
        ".wt-limit[tabindex]:active,.banner .banner-retry:active{transform:scale(var(--press));transition:transform var(--dur-1) var(--ease)}",
        "@media(prefers-reduced-motion:reduce){.wt-limit[tabindex]:active,.banner .banner-retry:active{transform:none;transition:none}}",
        /* kit: focus ring */
        // The ring of a thing the keyboard reaches that the base does not reach by what it is (a button and a summary wear it, a region that wears .focus-ring does): the
        // line and its offset are the base's, the corner is the small one so the ring follows the thing, drawn outside it. The second rule draws the same ring inside, for a
        // thing that fills its box to the edge of the card, which would cut it. Written once, for the things they list.
        // Tokens it reads: --focus-line --ring-offset --radius-sm.
        ".wt-limit[tabindex]:focus-visible,.lg .legend-item:focus-visible,.nx[tabindex]:focus-visible,.wt-foot-g:focus-visible{outline:var(--focus-line);outline-offset:var(--ring-offset);border-radius:var(--radius-sm)}",
        /* capsule: shell */
        // The card: everything under the head (banners, About, the limits, the accounts, the empty states) stands in this one
        // card, as the page stands in the app's shell: the shell's tokens for its colour, corner, glow and its gap to the head,
        // no line along its edge. The head stays out, on the ground. Inside, the card keeps the stack the page had (the same gap
        // between its parts) and, with the page's own gap, the room the page had (8px a side and 8px of the page's 16; under
        // 520px 4 of 12): the plate and the panels keep their own surfaces and their width, and read as tiles on it. No
        // overflow is cut here: the focus rings and the chart's tip may reach past a part's edge.
        ".shell-card{display:flex;flex-direction:column;gap:var(--space-4);min-width:0;padding:var(--shell-card-pad);border-radius:var(--shell-card-radius);background:var(--shell-card-bg);box-shadow:var(--shell-card-shadow)}",
        // The frame (.shell) holds the card and, where the limits have windows to list, the rail at its left (capsule: window tabs fills it; the
        // script puts .has-rail on the frame when it does): the rail --rail-w wide and the card the rest, side by side with no gap, the rail on the
        // ground and the card on it. With no rail (nothing yet, an error, no limit) the card has the whole width. Under 860px the same frame is a
        // block: the rail, a row of capsules, over the card, 8px off it.
        ".shell{display:grid;grid-template-columns:minmax(0,1fr);min-width:0}",
        ".shell.has-rail{grid-template-columns:var(--q-rail-w) minmax(0,1fr);column-gap:var(--space-2);align-items:start}",
        ".control-bar+.shell{margin-top:var(--shell-card-gap)}",
        "@media(max-width:520.98px){.shell.has-rail{display:block}.has-rail>.shell-card{margin-top:var(--space-2)}}",
        "@media(max-width:520.98px){.shell-card{padding:var(--shell-card-pad) var(--space-1)}}",
        // On the wide shell (the rail at the side, from 860px) the card has more room inside it, so that its parts do not stand against its edges: --shell-card-pad-y over and under, --shell-card-pad-x
        // at the sides. The row of the bars, the chart and its plate stand inside these fields (the bars' width, --bar-rest, is worked out from the row, so it follows by itself). Under 860px the padding above stands as it was.
        "@media not all and (max-width:859.98px){.shell-card{padding:var(--shell-card-pad-y) var(--shell-card-pad-x)}}",
        // The gray block of the card: the plate of the picked window and what stands under it (the card of a picked account, the chart) are one
        // block at the card's padding, and its four corners are the card's less that padding (concentric with the card's), the first part's
        // two at the top and the last part's two at the foot; the line under the last part is gone, the block's own edge ends it.
        ".lrows>:first-child{border-top-left-radius:var(--shell-card-inner-radius);border-top-right-radius:var(--shell-card-inner-radius)}",
        ".lrows>:last-child{border-bottom-left-radius:var(--shell-card-inner-radius);border-bottom-right-radius:var(--shell-card-inner-radius);border-bottom-color:transparent}",
        /* capsule: head */
        // The control bar: families on the left, About and Refresh on the right, one line where the width allows it (the tabs fold their names on the
        // narrow shell, kit: mark tab; Refresh folds its word, kit: text action), the words on a second line where it does not.
        ".control-bar{display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:var(--space-1) var(--space-2)}",
        // The families are the kit's row of mark tabs (kit: mark tab): the markup wears .tab-mark-row and .harness-seg, and the head
        // gives the row only its place: the room it asks for in the bar (what the tabs need, no more).
        ".harness-seg{flex:0 1 auto;min-width:0}",
        // About and Refresh are the kit's row of words (kit: text action): the markup wears .btn-word-row and .action-seg, and the head
        // gives the row only its place: the right end of the bar, and the height of a row of controls.
        ".action-seg{margin-left:auto;min-height:var(--row-h)}",
        // What the head keeps of the tabs is its own: the initial that stands as a family's mark where the widget has no drawing of it
        // (the kit's tab wears it as its mark). The ghosts it shows before the first answer are tabs of the kit, disabled: the base's
        // disabled ink is theirs, the head adds no look.
        ".harness-initial{display:inline-flex;align-items:center;justify-content:center;width:var(--mark-px);height:var(--mark-px);font-size:var(--type-meta);font-weight:var(--weight-semibold)}",
        /* capsule: banners */
        // Banners: what failed, in the open, above everything it touches.
        ".banners{display:flex;flex-direction:column;gap:var(--space-2)}",
        ".banners:empty{display:none}",
        ".banner{display:flex;align-items:center;gap:var(--space-2);min-height:var(--tap-h);padding:var(--space-2) var(--space-3);border:0;border-radius:var(--radius-row);background:var(--tint-3);color:var(--text-primary);font-size:var(--type-meta);line-height:var(--lh-ui);overflow-wrap:anywhere}",
        // Three tones: an error is the pale crimson, a warning (what is shown is last known) the amber, news the grey of the base.
        ".banner.bad{background:var(--tone-crimson)}",
        ".banner.warn{background:var(--status-warn-bg)}",
        ".banner-icon{display:flex;flex:none;color:var(--text-meta)}",
        ".banner.bad .banner-icon{color:var(--tone-crimson-ink)}",
        ".banner.warn .banner-icon{color:var(--warn-text)}",
        ".banner-text{flex:1 1 auto;min-width:0}",
        // The retry is a capsule, as the graph's "Together": the pill corner, white and raised, the compact height.
        ".banner .banner-retry{margin:0 0 0 auto;padding:var(--space-0) 11px;border-color:transparent;border-radius:var(--radius-pill);color:var(--text-primary);background:var(--surface);box-shadow:var(--shadow-pop);white-space:nowrap;flex:0 0 auto;transition:transform var(--dur-nav) var(--nav-spring)}",
        "@media(prefers-reduced-motion:reduce){.banner .banner-retry{transition:none}}",
        /* capsule: family title */
        // The family's limits.
        // The card's section: its children stand each on a line of their own; the credits of manual resets, where no plate is drawn, take theirs and wrap
        // when they do not fit (the gap between them is a token). With a plate the note of the accounts not read and the credits stand in it (the plate).
        ".reserve{display:flex;flex-wrap:wrap;align-items:baseline;column-gap:var(--space-3);min-width:0}",
        ".reserve>*{flex:0 0 100%;min-width:0}",
        // A text action standing in the card's section is its word, not the whole line: the click and the ring go no further.
        ".reserve>.cover-link{flex:0 0 auto}",
        ".reserve>.family-credits{flex:0 1 auto;margin:0 0 var(--space-2)}",
        ".reserve-head{display:flex;align-items:baseline;justify-content:space-between;gap:var(--space-2) var(--space-3);flex-wrap:wrap;margin-bottom:var(--space-1)}",
        // The stamp says what the picked window's figure stands on ("Read 14 of 16 · 18 min ago"): quiet, in the meta ink, with no lamp
        // and no colour of a state. Where a read failed (the words of that failure) it is in the ink.
        ".reserve-age{display:block;text-align:right;font-variant-numeric:tabular-nums}",
        ".reserve-age.is-ink{color:var(--text-primary)}",
        // Under the window-tabs plate the stamp of a picked window stands in the row of the figure of the plate (capsule: window tabs); here it stands
        // alone where the plate is not drawn: where there are no windows to list, and where every limit is hidden (the line over the note that says why).
        ".reserve-note{margin:var(--space-2) 0}",
        ".reset-credits{font-size:var(--type-meta);color:var(--text-meta);margin:var(--space-2) 0;overflow-wrap:anywhere}",
        // The family's credits in words as the notes of the plate are: the head (its name in the ink, the dot and the count in the accent: .wt-head-rest) at the regular weight, the meta
        // tracking; in the plate (a grid) they take a line of their own, and the row gap is their air.
        ".family-credits{letter-spacing:var(--track-meta)}",
        ".family-credits .emph{margin-right:var(--space-2);font-weight:var(--weight-regular)}",
        "@media(max-width:520.98px){.reserve-head{display:block}.reserve-age{text-align:left;margin-top:var(--space-0)}}",
        /* capsule: bars and legend */
        // The legend of the bars' kinds (About shows it).
        ".strip-legend{display:flex;flex-wrap:wrap;gap:var(--space-2) var(--space-3);margin-top:var(--space-2)}",
        // Under the window-tabs plate, under the notes: two rows in one order in every family, the states, then the reset.
        // The states are the kit's legend (.calm-legend: its flex, gap, size and colour; the meta tracking is added here): an item is its number and its words and nothing
        // else (the bars above are its sample, and each fact stands once on the screen: who is not read is the note's, and the note says the age of the
        // readings itself, so the items of the last-known readings and of the ones at the limit when last read are not drawn where the note is). The reset is a row of cells
        // with a dot at the end of each but the last. A row with nothing to say is not drawn: both take the rows after the named ones, so an absent row leaves no gap
        // behind it. The accent is the one red of the plate: the number of the accounts at the limit now (a billing cycle: the money spent beyond its allowance); the
        // words stay the quiet colour.
        ".lg,.nx{grid-column:1/-1;min-width:0;margin:0;padding:0;line-height:var(--lh-ui);letter-spacing:var(--track-meta)}",
        // Tight (under 640px) the plate is one column in the order of its markup (the bars, the fact, the model list, the stamp, the chart word, the notes; the
        // window-tabs capsule gives the parts of its own their places): the bars span the row and the chart word joins the stamp's row at the right.
        ".lg{list-style:none}",
        ".nx{display:flex;flex-wrap:wrap;gap:var(--space-1)}",
        ".lg b,.nx b{font-weight:var(--weight-medium);color:var(--text-primary);font-variant-numeric:tabular-nums}",
        ".lg .legend-item,.nx>span{white-space:nowrap}",
        ".lg .legend-item,.nx[tabindex]{position:relative;cursor:default}",
        ".lg .legend-item{display:block}",
        // The dot between two cells of the reset stands at the end of the cell before it, on a no-break space, so a line never begins with
        // it. It is drawn, not text: a reader hears no dot (the empty alternative; the line before it is for a browser without one).
        ".nx>span:not(:last-child):after{content:\"\\00a0·\";content:\"\\00a0·\"/\"\"}",
        // Under the window-tabs plate, hover, focus and touch are one thing on a handle (a legend item; the reset row, when the engine says
        // which accounts reset then): its words come forward, and on the strip the
        // bars of the other kinds step back. The ring is the kit's. The bars step back by the
        // kind the plate wears (data-dim, set by the item's own events: dimOnItem), not by a
        // :has() on the item's :hover, which WebKit does not follow (it comes late and stays).
        ".lg .legend-item:is(:hover,:focus-visible),.nx[tabindex]:is(:hover,:focus-visible){color:var(--text-primary)}",
        // Under a finger a handle's words stay the size they are: the 44px is a transparent box round them that its row does not count (the
        // plate does not grow), as the note's "N more" has it.
        "@media(pointer:coarse){.lg .legend-item:after,.nx[tabindex]:after{content:\"\";position:absolute;left:calc(var(--space-1) * -1);right:calc(var(--space-1) * -1);top:50%;height:var(--tap-h);margin-top:calc(var(--tap-h) / -2)}}",
        /* capsule: chart */
        // The timeline: one limit, the record behind now and one future ahead.
        // The calm timeline: graph tabs and the model choice on one line, a
        // headline with the nearest reset, two quiet segments, the plot, one
        // short legend; everything else folded under Details.
        // The plate of the whole chart: grey, four round corners, no line under it. The rows' first/last-child rule (capsule: shell) gives the
        // last part the card's inner corner at the foot and the first part one at the top, and out-ranks the plate's own radius, so the plate names
        // its place in the rows once more and keeps all four corners its own.
        ".tl-block{position:relative;padding:var(--space-3);border-radius:var(--radius-card);background:var(--inset)}",
        ".lrows>.tl-block{border-radius:var(--radius-card)}",
        // Inside the plate of a limit (its last part, the approved composition) the history has no plate of its own: a hairline over it, the plate's
        // own air, its controls and plot on the plate's ground.
        ".q-plate>.tl-wrap{margin-top:var(--space-3)}",
        ".q-plate .tl-block{padding:var(--space-3) var(--space-1) 0;border-top:var(--border-w) solid var(--q-edge);border-radius:0;background:none}",
        ".tl-wrap{display:grid;grid-template-rows:1fr;min-width:0}",
        ".tl-wrap>.tl-fold{min-height:0;min-width:0}",
        ".fold-run{transition:grid-template-rows var(--dur-5) var(--ease)}",
        ".fold-run>.tl-fold{overflow:hidden}",
        ".fold-shut{grid-template-rows:0fr}",
        "@media(prefers-reduced-motion:reduce){.fold-run{transition:none}}",
        ".lrows>.tl-wrap>.tl-fold>.tl-block{border-radius:var(--radius-card)}",
        // The wait of the plate (tl-wait, put by the script from the start of the pick's folds to the end of the line; none at the poll, at a quick second pick, or under reduced motion): the content
        // stands hidden by visibility (the plate keeps its height, the nodes live), and over it a layer of stand-ins of the same layout (.tl-skel, aria-hidden): the row of four capsules, the field of the
        // chart, the line of the legend, the line of Details, grey by --tint-3 and pulsing by opacity. When the line has met the layer fades (.sk-off) and the content comes in (tl-reveal) in --dur-4, nothing moves.
        ".tl-model{flex:none;max-width:48%;color:var(--text-meta)}",
        ".tl-model-k{font-weight:var(--weight-regular)}",
        ".tl-model-v{min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;color:var(--text-primary);font-weight:var(--weight-medium)}",
        ".tl-model-panel{margin:0 0 var(--space-3);padding:var(--space-2) var(--space-3)}",
        ".tl-model-opts{display:flex;flex-wrap:wrap;gap:var(--space-1);margin-top:var(--space-2)}",
        ".tl-model-opts .seg-opt{max-width:100%;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;justify-content:flex-start}",
        ".tl-model-panel .seg{margin-top:var(--space-2)}",
        ".tl-hero{display:flex;justify-content:space-between;align-items:flex-end;flex-wrap:wrap;gap:var(--space-2) var(--space-5)}",
        ".tl-num{font-size:var(--type-display);line-height:var(--lh-tight);font-weight:var(--weight-semibold);letter-spacing:-.01em;font-variant-numeric:tabular-nums}",
        ".tl-unit{margin-left:var(--space-2);font-weight:var(--weight-regular);letter-spacing:0}",
        ".tl-next{text-align:right}",
        ".tl-next-v.dim{color:var(--text-meta);font-weight:var(--weight-regular)}",
        ".tl-body.is-updating .chart-plot{opacity:.5}",
        // While the chart of a window is read, its place has the layout of the drawn block: the plot's canvas, empty, which scales as the plot's does
        // (the same viewBox, .chart-svg) in the room the plot has (the plot's tile is the drawn plot's alone), the line of the legend and the row of Details, so the card does not shrink and grow again beside the rail,
        // and the plate and its stamp stand still. The words stand over the canvas, in its middle.
        ".chart-wait{position:relative;margin-top:var(--space-3)}",
        ".tl-body>.chart-wait{padding:var(--space-2) var(--space-2) 0}",
        ".chart-wait-text{position:absolute;top:0;right:0;bottom:0;left:0;display:flex;align-items:center;justify-content:center;text-align:center}",
        ".chart-hold{min-height:var(--row-h)}",
        "@media(pointer:coarse){.chart-hold{min-height:var(--tap-h)}}",
        ".chart-table>.chart-legend,.chart-table>.tl-facts,.chart-table>.checkpoints,.chart-table>.tl-joint{margin-bottom:var(--space-2)}",
        ".tl-controls{display:flex;align-items:center;flex-wrap:wrap;gap:var(--space-2) var(--space-3)}",
        // The pill variant of the kit's segment, a context and no markup of its own: in the graph's row of controls
        // (.tl-block .tl-controls) the same segment is a pill. The segment is its track, the option the pill (the compact 26px of
        // the kit's buttons, 11px a side), the active one stands raised on the surface.
        ".tl-block .tl-controls .seg{padding:var(--space-0);border-radius:var(--radius-pill);background:var(--tint-3);gap:0}",
        ".tl-block .tl-controls .seg-opt{min-height:var(--row-h-sm);padding:var(--space-0) 11px;border-color:transparent;border-radius:var(--radius-pill);color:var(--text-meta)}",
        // This rule outranks the kit's touch line, so the lift for a finger stands here (the kit: touch).
        "@media(pointer:coarse){.tl-block .tl-controls .seg-opt{min-height:var(--tap-h)}}",
        ".tl-block .tl-controls .seg-opt.active{background:var(--surface);border-color:transparent;color:var(--text-primary);box-shadow:var(--shadow-pop)}",
        // The caption of a segment (Span, Future) is quiet text in its capsule, left of the options: not a button, no border.
        ".tl-block .tl-controls .seg-label{padding-left:var(--space-2)}",
        // An option that for now draws what another one draws (a pace nobody has measured is no new use): told by its dashed outline, not by a pale ink, for it is a live button and keeps the ink of the others.
        // Chosen, it is the segment's active option like any other.
        ".tl-block .tl-controls .seg-opt.idle:not(.active){border-style:dashed;border-color:var(--edge-idle)}",
        ".tl-scope{margin-top:var(--space-2);overflow-wrap:anywhere}",
        ".chart-plot{position:relative;margin-top:var(--space-3);padding:var(--space-2) var(--space-2) 0;border-radius:var(--radius-tile);background:var(--surface);box-shadow:var(--elev-rest)}",
        ".chart-svg{display:block;max-width:100%;height:auto;overflow:visible;touch-action:none}",
        // The field's lines end round: a dash of .1 is a dot, and a line has no sharp end.
        ".chart-svg line{stroke-linecap:round}",
        // The grid and the day lines are dots, the base line one fine solid line; the axis has no ticks.
        ".chart-svg .grid,.chart-svg .day-rule{stroke:var(--edge-strong);stroke-width:var(--ring-w);stroke-dasharray:var(--chart-dash-grid);opacity:.7}",
        ".chart-svg .grid.base{stroke:var(--edge);stroke-width:var(--border-w);stroke-dasharray:none;opacity:1}",
        ".chart-svg .reset-line{stroke:var(--calm-b);stroke-width:var(--ring-w);stroke-dasharray:var(--chart-dash-reset);opacity:.18}",
        ".chart-svg .axis-text{fill:var(--text-meta);font-size:var(--type-meta);font-family:inherit;font-variant-numeric:tabular-nums}",
        ".chart-svg .axis-text.now,.chart-svg .axis-text.day{fill:var(--text-primary);font-weight:var(--weight-medium)}",
        ".chart-svg .future-bg{fill:var(--tint-1);rx:var(--radius-field)}",
        // The corner of the plot area: the rect of the clip that the plot's background layers (the tint of the future, the lines of the axes
        // and of the days, the marks of the resets) are cut by. The script writes the same 10 in the rect's own rx for an engine without CSS geometry.
        ".chart-svg .field-clip{rx:var(--radius-field)}",
        ".chart-svg .now-line{stroke:var(--text-secondary);stroke-width:var(--ring-w)}",
        ".chart-svg .cursor-line{stroke:var(--text-meta);stroke-width:var(--ring-w);stroke-dasharray:var(--chart-dash-cursor);opacity:.8}",
        ".chart-svg .hit{fill:transparent;cursor:crosshair}",
        ".area-observed{fill:url(#qa-area)}",
        ".line-observed{fill:none;stroke:var(--calm-a);stroke-width:var(--chart-line);stroke-linejoin:round;stroke-linecap:round}",
        ".line-carried{fill:none;stroke:var(--calm-a);stroke-width:var(--chart-line);stroke-dasharray:var(--chart-dash-carried);stroke-linejoin:round;stroke-linecap:round}",
        // One hue for the whole curve: solid is recorded, dashed carried, dotted a condition ahead of now.
        ".chart-svg .reset-dot,.calm-legend .reset-dot{fill:var(--calm-b);stroke:var(--surface);stroke-width:var(--ring-w)}",
        // A reset in the field has the radius of the token; the legend's sample keeps the 3.5 the script writes in its 10px box.
        ".chart-svg .reset-dot{r:var(--chart-dot-reset)}",
        ".chart-svg .area-scenario{fill:url(#qa-area);opacity:.4;stroke:none}",
        ".chart-svg .unit-text{fill:var(--text-meta);font-size:var(--type-meta);font-family:inherit}",
        ".tl-joint{display:grid;gap:var(--space-0);margin-top:var(--space-2)}",
        ".tl-joint b{color:var(--text-primary);font-weight:var(--weight-semibold)}",
        ".line-scenario{fill:none;stroke:var(--calm-a);stroke-width:var(--chart-line);stroke-dasharray:var(--chart-dash-scenario);stroke-linejoin:round;stroke-linecap:round;opacity:.9}",
        ".scenario-start{r:var(--chart-dot-start);fill:var(--calm-a);stroke:var(--surface);stroke-width:var(--ring-w)}",
        ".chart-svg .cursor-dot{r:var(--chart-dot-cursor);stroke:var(--surface);stroke-width:var(--chart-line);stroke-dasharray:none}",
        ".chart-svg .cursor-dot.line-observed{fill:var(--calm-a)}",
        ".chart-svg .cursor-dot.line-scenario{fill:var(--calm-a);stroke-dasharray:none}",
        // The tip over the chart: a wrapping pill of the ink (the script puts it over the highest point read, centred on the cursor and held inside the plot):
        // the ink is the pill's ground and the surface its words, so in the dark theme it is a light pill. The moment, the "of N", the short name and the dots between them stand back.
        ".chart-tip{position:absolute;z-index:5;pointer-events:none;display:flex;flex-wrap:wrap;box-sizing:border-box;width:max-content;max-width:100%;align-items:center;gap:var(--space-2);white-space:normal;overflow-wrap:anywhere;padding:var(--space-1) var(--space-3);border-radius:var(--radius-card);background:var(--text-primary);color:var(--surface);box-shadow:var(--elev-rest);font-size:var(--type-meta);transform:translate(-50%,calc(-100% - var(--space-3)))}",
        // Where the room over the point is short, the pill stands under it.
        ".chart-tip.tip-below{transform:translate(-50%,var(--space-3))}",
        ".tip-row{display:flex;flex-wrap:wrap;min-width:0;align-items:center;gap:var(--space-2);font-variant-numeric:tabular-nums}",
        ".tip-row svg{flex:none}",
        ".tip-when{flex-basis:100%}",
        ".tip-sep{display:none}",
        ".tip-val{font-size:var(--type-body);font-weight:var(--weight-semibold)}",
        ".tip-when,.tip-of,.tip-name,.tip-sep{opacity:.72}",
        // Under the chart, one line per line drawn: whose total it is and, for
        // the future, what it assumes — the legend is the only place that says so.
        // The short legend under the plot: each item a flat chip on the tray. It only says what a line is, so it
        // does not stand on a shadow: a shadow is what the pressable controls above it wear.
        ".tl-block .calm-legend{gap:var(--space-2)}",
        ".tl-block .calm-legend:not(.lg) .legend-item{padding:var(--space-1) var(--space-3);border-radius:var(--radius-pill);background:var(--tint-2)}",
        ".chart-legend{display:flex;flex-direction:column;gap:var(--space-1);margin-top:var(--space-2)}",
        ".tl-facts .warn{color:var(--warn-text)}",
        // The schedule stands in a white card (the table is its only child; its caption and its head are the card's first lines). The card
        // scrolls sideways by itself on a narrow screen (the media rule at the end of this section), so the page does not; its shadow is not cut
        // where it is wide. The last row has no line under it: the lines are the cells' tops.
        ".schedule-card{margin-top:var(--space-3);padding:var(--space-1) var(--space-4) var(--space-2)}",
        // "N more resets": a capsule with a soft shadow in place of a frame; the kit's hover and focus stay as they are.
        ".schedule .schedule-more{margin:var(--space-0) 0;border-color:transparent;border-radius:var(--radius-pill);box-shadow:var(--elev-rest)}",
        ".schedule-notes{display:flex;flex-direction:column;gap:var(--space-0);margin-top:var(--space-2)}",
        // What is left at a few moments ahead: a quiet caption over one row of white tiles, one tile for each moment (as many columns as
        // there are tiles; on a narrow screen, the media rule at the end of this section, they stand in one column and the number keeps one line).
        // The number is light with the crimson point (the plate's figure, figureNode); under it the moment and the total.
        ".checkpoints{margin-top:var(--space-3);font-variant-numeric:tabular-nums}",
        ".cp-tiles{display:grid;grid-auto-flow:column;grid-auto-columns:minmax(0,1fr);gap:var(--space-2);margin-top:var(--space-2)}",
        ".cp-tile{min-width:0;padding:var(--space-3) var(--space-4)}",
        ".cp-num{display:block;font-size:var(--type-display);font-weight:var(--weight-light);line-height:var(--lh-tight);white-space:nowrap;color:var(--text-primary)}",
        ".cp-num.none{font-size:var(--type-section);color:var(--text-meta)}",
        ".cp-dot{color:var(--tone-crimson-ink)}",
        ".cp-when{display:block;margin-top:var(--space-0)}",
        // The Details clips what overflows it, so the ring of its summary is drawn inside the summary, not outside it; and the Details has the room for it at its sides (its padding, taken back by its
        // margin: nothing moves at rest): the ring's width and its offset, as much as the other rings of the kit stand off what they go round. The summary spans that room too (its padding gives the
        // room back to the row, so the triangle stands where it stood), and the ring goes round the whole row, the triangle in it, not along its edge.
        ".chart-table{margin-top:var(--space-3);margin-inline:calc((var(--ring-w) + var(--ring-offset)) * -1);padding-inline:calc(var(--ring-w) + var(--ring-offset));overflow-x:auto}",
        ".chart-table summary{cursor:pointer;font-size:var(--type-meta);color:var(--text-primary);min-height:var(--row-h);padding:var(--space-1h) 0}",
        ".chart-table>summary{margin-inline:calc((var(--ring-w) + var(--ring-offset)) * -1);padding-inline:calc(var(--ring-w) + var(--ring-offset))}",
        ".chart-table>summary:focus-visible{outline-offset:calc(var(--ring-offset)*-1)}",
        "@media(pointer:coarse){.chart-table summary{min-height:var(--tap-h)}}",
        // The facts table's own room (the kit's tables have the rest). It stands after the schedule's rules of the kit: the schedule
        // is drawn inside the Details, where a cell and a caption are both and the later rule wins.
        ".chart-table th,.chart-table td{padding:var(--space-1h) var(--space-2) var(--space-1h) 0}",
        ".chart-table caption{padding:var(--space-3) 0 var(--space-1)}",
        // The method under the facts: how the lines are counted, one fold inside the Details.
        ".chart-how{margin-top:var(--space-2)}",
        ".chart-how>summary{color:var(--text-meta)}",
        ".sightings-nav{display:flex;align-items:center;gap:var(--space-2);margin-top:var(--space-2)}",
        ".chart-notes p{margin:0 0 var(--space-2)}",
        // The limit's details in a white card of the tiles' kind: the label in the quiet ink in a column of 180px, the value beside it, a line
        // between the rows (each cell carries it under itself, so it runs across both columns) and none under the last one; on a narrow
        // screen (the media rule at the end of this section) the label stands over its value, and the line is under the value.
        ".reserve-detail{display:grid;grid-template-columns:180px minmax(0,1fr);margin:var(--space-3) 0;padding:var(--space-1) var(--space-4);font-size:var(--type-meta);color:var(--text-primary);font-variant-numeric:tabular-nums}",
        ".reserve-detail dt{padding:var(--space-2) var(--space-4) var(--space-2) 0;border-bottom:var(--border-w) solid var(--edge);color:var(--text-meta)}",
        ".reserve-detail dd{margin:0;min-width:0;padding:var(--space-2) 0;border-bottom:var(--border-w) solid var(--edge);overflow-wrap:anywhere}",
        ".reserve-detail dt:last-of-type,.reserve-detail dd:last-child{border-bottom:0}",
        // Under the window-tabs plate, the graph is the plate's continuation, not a second plate: it has no head of its own (the window
        // is picked in the row above, its figures stand on the plate), so it opens with the kit's row of controls - the span,
        // the future, and Combined (the kit's segment with its one option) - the model choice across from them. The kit
        // spaces that row apart and sets it 12 px under a head: here it stands left, at the block's top, and the model button
        // takes the row's right end. The model list opens under the row; the
        // combined view's headline, the one thing it has that the plate has not, stands between the row and the plot.
        ".tl-block .tl-controls.wt-controls{justify-content:flex-start;margin-top:0}",
        ".wt-controls>.tl-model{margin:0 0 0 auto}",
        ".wt-controls+.tl-model-panel{margin-top:var(--space-2)}",
        ".wt-controls~.tl-hero{margin-top:var(--space-3)}",
        "@media(max-width:520.98px){.tl-block{padding-inline:var(--space-1)}.reserve-detail{grid-template-columns:minmax(0,1fr)}.reserve-detail dt{padding-bottom:0;border-bottom:0}.reserve-detail dd{padding-top:var(--space-0)}.schedule-card{overflow-x:auto}.cp-tiles{grid-auto-flow:row}.cp-tile{padding:var(--space-2) var(--space-3)}.tl-model{max-width:100%}.tl-hero{display:block}.tl-next{display:flex;flex-wrap:wrap;align-items:baseline;gap:0 var(--space-2);margin-top:var(--space-1);text-align:left}.tl-block .tl-controls{gap:var(--space-2)}}",
        /* capsule: account card */
        // On a narrow screen (520px and under) the stamp and the chart word take a row of their own, the words of the stamp and of the reset wrap: the card is narrower than the plate they stood in.
        ".insp-title{display:flex;align-items:center;flex-wrap:wrap;gap:var(--space-1) var(--space-2);min-width:0;font-size:var(--type-body);font-weight:var(--weight-semibold)}",
        // The dot and the name stand in one pair: a narrow line wraps the plan and the next-up mark after the pair, never the dot off its name.
        ".insp-who{display:flex;align-items:center;gap:var(--space-2);min-width:0;max-width:100%}",
        ".insp-name{min-width:0;overflow-wrap:anywhere}",
        // On a narrow screen (520px and under) the name is one word of the strip: never broken by letters, one line, an ellipsis only where it is longer than the room it has.
        "@media(max-width:520.98px){.insp-name{overflow-wrap:normal;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}}",
        ".plan-chip,.next-up{font-size:var(--type-meta);font-weight:var(--weight-regular);color:var(--text-meta)}",
        ".insp-meta{display:flex;align-items:center;gap:var(--space-1) var(--space-2);flex-wrap:wrap;margin-top:var(--space-1);overflow-wrap:anywhere}",
        // The dot between two items stands at the end of the item before it (not at the start of the one after it), so a wrapped line never begins with it.
        // It is drawn, not text: a reader hears no dot (the empty alternative; the line before it is for a browser without one).
        ".insp-meta>span:not(:last-child):after{content:\"\\00b7\";content:\"\\00b7\"/\"\";margin-left:var(--space-2)}",
        ".meta-bad{color:var(--bad-text)}",
        ".quota-primary-row{display:flex;align-items:center;gap:var(--space-2);flex-wrap:wrap;margin-top:var(--space-2);font-size:var(--type-meta)}",
        ".quota-primary-text{color:var(--text-meta)}",
        ".quota-primary-text.exhausted{color:var(--bad-text)}",
        ".quota-primary-text.cooling{color:var(--warn-text)}",
        ".quota-when{display:inline-flex;align-items:center;flex-wrap:wrap;gap:var(--space-1);color:var(--text-meta)}",
        ".quota-cooldown,.quota-exhaustion,.quota-unavailable,.acct-note{display:flex;align-items:flex-start;gap:var(--space-1h);margin-top:var(--space-2);font-size:var(--type-meta);color:var(--warn-text)}",
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
        ".acct-lim{display:grid;grid-template-columns:minmax(88px,150px) 64px 44px minmax(0,1fr);align-items:center;column-gap:var(--space-3);margin-top:var(--space-1h);font-size:var(--type-meta)}",
        ".acct-lim .k{color:var(--text-meta);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}",
        ".acct-lim .m{position:relative;height:5px;border-radius:var(--radius-xs);background:var(--share-track);overflow:hidden}",
        ".acct-lim .m>i{position:absolute;left:0;top:0;bottom:0;background:var(--share-ink)}",
        ".acct-lim .m.last>i{background:var(--share-track) var(--hatch)}",
        ".acct-lim .m.held>i{background:var(--status-warn)}",
        ".acct-lim .m.none{background:transparent}",
        ".acct-lim .p{text-align:right;font-variant-numeric:tabular-nums;color:var(--text-primary)}",
        ".acct-lim .p.bad{color:var(--bad-text);font-weight:var(--weight-semibold)}",
        ".acct-lim .p.meta{color:var(--text-meta)}",
        ".acct-lim .r{min-width:0;color:var(--text-meta);overflow-wrap:anywhere;font-variant-numeric:tabular-nums}",
        ".acct-lim .r .warn{color:var(--warn-text)}",
        ".acct-lim .r .est{color:var(--scenario-ink)}",
        // The strip of a card with no chart over it: the share left, the money, the next reset, one line (a long one wraps between its words).
        ".insp-sum{display:flex;align-items:baseline;flex-wrap:wrap;gap:var(--space-0) var(--space-2);margin-top:var(--space-1);font-size:var(--type-meta);line-height:var(--lh-ui);color:var(--text-meta);font-variant-numeric:tabular-nums}",
        ".insp-sum>span:not(:last-child):after{content:\"\\00b7\";content:\"\\00b7\"/\"\";margin-left:var(--space-2)}",
        ".insp-sum-lead{color:var(--text-primary);font-weight:var(--weight-medium)}",
        ".insp-sum .warn{color:var(--warn-text)}",
        ".insp-diag{margin-top:var(--space-3)}",
        ".insp-diag-body{margin-top:var(--space-2)}",
        ".diag-block{margin-top:var(--space-3)}",
        ".diag-title{font-size:var(--type-meta);color:var(--warn-text)}",
        ".diag-title.muted{color:var(--text-meta)}",
        ".win-line{display:flex;align-items:center;flex-wrap:wrap;gap:var(--space-1) var(--space-2);font-variant-numeric:tabular-nums}",
        ".win-tag{min-width:64px;color:var(--text-primary)}",
        ".win-tag.bad,.win-used.bad,.win-word.bad{color:var(--bad-text)}",
        ".win-tag.warn,.win-word.warn{color:var(--warn-text)}",
        ".win-line.stale .win-tag{color:var(--text-meta)}",
        ".acct-cap{font-size:var(--type-meta);color:var(--text-meta);background:var(--inset);padding:0 var(--space-1);border-radius:var(--radius-xs);max-width:100%;overflow-wrap:anywhere}",
        ".acct-cap.exhausted{color:var(--bad-text);background:var(--status-bad-bg)}",
        ".acct-cap.held{color:var(--warn-text);background:var(--status-warn-bg)}",
        ".acct-pool{max-width:180px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;direction:rtl;text-align:left}",
        // One bar language for a single window: as long as the share left.
        ".meter{position:relative;display:inline-block;width:36px;height:4px;flex:none;box-shadow:0 var(--border-w) 0 var(--edge-strong)}",
        ".meter-fill{position:absolute;left:0;top:0;bottom:0;background:var(--share-ink);border-radius:0 1px 1px 0}",
        ".meter.restricted{box-shadow:0 var(--border-w) 0 var(--status-warn)}",
        ".meter.restricted .meter-fill{background:var(--status-warn)}",
        ".meter.spent{box-shadow:0 2px 0 var(--status-bad)}",
        ".meter.stale .meter-fill{background:var(--tint-5)}",
        // On a narrow screen (520px and under) a window of the card stands in two rows: the name (two lines where it needs them, whole, never cut), the meter and the share; under them the reset, the
        // whole width of the card, wrapped at its spaces (a word is broken only when it alone is wider than the card).
        "@media(max-width:520.98px){.acct-lim{grid-template-columns:72px minmax(32px,1fr) 40px;column-gap:var(--space-2);row-gap:var(--space-0)}.acct-lim .k{white-space:normal;overflow:visible;text-overflow:clip;overflow-wrap:break-word}.acct-lim .r{grid-column:1/-1;overflow-wrap:break-word}}",
        /* capsule: accounts table */
        // The account list: every account, its share left in every limit.
        // The selected account's card stands in this table as one more band of it, as in the limits list (kit: panels and
        // cards), and with no room at its sides.
        // The list stands in the place of the bars, the chart and the card, which fold shut while it is open (.acc-fold: one row, 1fr open and 0fr shut; .acc-run only while it moves, so the
        // shadows and the focus rings are not cut at rest). Its height is that of the place it replaces, measured when it opens (--acc-space); the head of the list holds Rows and the line of
        // coverage, the head of the columns stands, and the body of the table alone scrolls (a thin bar).
        ".acc-strip{display:flex;justify-content:flex-end;align-items:center;padding:0 0 var(--space-1)}",
        ".acc-fold{display:grid;grid-template-rows:1fr;min-width:0}",
        ".acc-fold>.acc-in{min-height:0;min-width:0}",
        ".acc-fold.acc-run{transition:grid-template-rows var(--dur-5) var(--ease)}",
        ".acc-fold.acc-run>.acc-in{overflow:hidden;transition:opacity var(--dur-2) var(--ease)}",
        ".acc-fold.acc-shut{grid-template-rows:0fr}",
        ".acc-fold.acc-shut>.acc-in{overflow:hidden;opacity:0}",
        "@media(prefers-reduced-motion:reduce){.acc-fold.acc-run,.acc-fold.acc-run>.acc-in{transition:none}}",
        ".acc-list{display:flex;flex-direction:column;min-height:calc(var(--row-h) * 6);height:var(--acc-space)}",
        ".acc-list-head{flex:none;display:flex;flex-wrap:wrap;align-items:baseline;justify-content:space-between;gap:var(--space-1) var(--space-4);padding:0 0 var(--space-2)}",
        ".acc-list-head .rows-menu{margin:0;flex:0 0 auto}",
        ".acc-cover-box{flex:1 1 auto;min-width:0;text-align:right}",
        ".acc-cover-box .acc-cover{margin:0}",
        ".acc-list>.accounts{flex:1 1 auto;min-height:0;display:flex;flex-direction:column;margin:0;padding:0;border:0;border-radius:0;background:none}",
        ".acc-th{flex:none;overflow:hidden;scrollbar-gutter:stable;scrollbar-width:thin;font-size:var(--type-meta)}",
        ".acc-list .acc-table{flex:1 1 auto;min-height:0;margin-top:0;overflow-x:hidden;overflow-y:auto;scrollbar-gutter:stable;scrollbar-width:thin;scrollbar-color:var(--edge-strong) transparent}",
        ".acc-table>.inspector{border-width:0 0 var(--border-w);border-radius:0;padding:var(--space-3) 0;font-size:var(--type-body)}",
        ".acc-head+.inspector{margin-top:var(--space-2)}",
        ".acc-cover{margin-top:var(--space-2)}",
        ".acc-table{margin-top:var(--space-2);font-size:var(--type-meta)}",
        ".acc-headrow,.acc-row{display:grid;align-items:center;column-gap:var(--space-2);min-height:var(--row-h)}",
        ".acc-headrow{align-items:end;color:var(--text-meta);border-bottom:var(--border-w) solid var(--edge)}",
        // The name of a column wraps onto a second line where it is wider than the column (no ellipsis); the head is one row of the grid, so every column has the one height, the names standing on its foot.
        ".acc-headrow span{min-width:0;overflow:visible;text-overflow:clip;white-space:normal;overflow-wrap:break-word;line-height:var(--lh-ui)}",
        ".acc-row{width:100%;padding:var(--space-0) 0;border:0;border-bottom:var(--border-w) solid var(--edge);border-radius:0;background:transparent;text-align:left;font-size:var(--type-meta)}",
        "@media(pointer:coarse){.acc-row{min-height:var(--tap-h)}}",
        ".acc-row:hover,.acc-row.active{background:var(--inset)}",
        ".acc-name{display:flex;align-items:center;gap:var(--space-1h);min-width:0;font-size:var(--type-body)}",
        ".acc-name-text{min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}",
        ".acc-cell{min-width:0;max-width:100%;overflow-wrap:anywhere;text-align:right;font-variant-numeric:tabular-nums;color:var(--text-primary)}",
        ".acc-cell.last{color:var(--text-meta);text-decoration:underline dotted;text-underline-offset:3px}",
        ".acc-cell.bad{color:var(--bad-text)}",
        ".acc-cell.held{color:var(--warn-text)}",
        ".acc-cell.none,.acc-cell.unknown{color:var(--text-meta)}",
        ".acc-state{color:var(--text-meta);overflow-wrap:anywhere}",
        ".acc-state.bad{color:var(--bad-text)}",
        ".acc-state.warn{color:var(--warn-text)}",
        ".acc-sec{margin-top:var(--space-3);font-size:var(--type-meta);color:var(--text-meta)}",
        "@media(max-width:520.98px){.acc-headrow{display:none}.acc-row{display:flex;flex-wrap:wrap;gap:var(--space-0) var(--space-2);padding:var(--space-1h) 0}.acc-name{flex:1 1 100%}.acc-cell{text-align:left}.acc-cell:before{content:attr(data-limit) \" \";color:var(--text-meta)}}",
        // On a narrow width the line of coverage stands at the left, not the right: four short lines with a ragged left edge read worse than a flush-left block.
        "@media(max-width:520.98px){.acc-cover-box{text-align:left}}",
        /* capsule: about */
        // About: what the screen is made of, and the state of the machine.
        ".about-items{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(220px,100%),1fr));gap:var(--space-3) var(--space-5);margin-top:var(--space-3);font-size:var(--type-meta);color:var(--text-meta)}",
        ".about-title{font-size:var(--type-body);font-weight:var(--weight-medium);color:var(--text-primary);margin-bottom:var(--space-1)}",
        ".about-items p{margin:0}",
        ".system-state{display:flex;flex-wrap:wrap;gap:var(--space-2) var(--space-4);margin-top:var(--space-2)}",
        /* capsule: empty and errors */
        // The empty card: what stands in the card when there is nothing to draw (the first read under way, no family, no account),
        // an icon, a title, a few words and, where there is one, the notes under them.
        ".empty-card{padding:var(--space-5);text-align:center;display:flex;align-items:center;flex-direction:column;gap:var(--space-2)}",
        ".empty-icon{color:var(--text-meta)}",
        ".empty-title{font-size:var(--type-section);font-weight:var(--weight-medium);margin:0}",
        ".empty-desc{font-size:var(--type-meta);color:var(--text-meta);max-width:360px;margin:0}",
        ".empty-notes{display:flex;gap:var(--space-3);flex-wrap:wrap}",
        /* capsule: window tabs */
        // Reading stamps, limit controls and rail layout for the selected family.
        ".wt-age{overflow-wrap:anywhere}",
        ".wt-n{color:var(--text-primary);font-weight:var(--weight-regular)}",
        ".wt-sep{display:inline-block;width:var(--sep-size);height:var(--sep-size);margin:0 var(--space-1);border-radius:50%;background:currentColor;vertical-align:middle}",
        // The dot that blinks (a read is in the air) is the larger one, so the blink is seen; the blink itself is the one rule of kit: motion.
        ".wt-sep.is-busy{width:var(--dot-size);height:var(--dot-size)}",
        ".wt-age.is-old{color:var(--text-primary);font-weight:var(--weight-regular)}",
        // The note under the number (.wt-foot), in the plate on a line of its own and in words as the tab of a window says them: "No fresh data · 5" (the name in the ink, the dot and
        // the count in the accent), the star the tabs carry before it, and the accounts with no current reading in their groups ("read 5 days ago · name, name"; "window reset after the
        // reading · name"), the names in the ink, the words quiet. A group takes the keyboard and, under the pointer or the keyboard, turns its words to the ink (and dims the plate's
        // bars but its own, written with the plate). A hidden copy of the note of a window that has accounts not read (it wears the star, or it read none and wears the dash) stands in
        // its place where this window has none, so the plate does not jump as the reader moves along the rail.
        ".wt-foot{grid-column:1/-1;min-width:0;margin:0;letter-spacing:var(--track-meta)}",
        ".wt-foot-hold{visibility:hidden}",
        // (a note in the card's summary is still the rail's last row on the pick of a family: it keeps .wt-foot-in's fade after the last window)
        ".wt-foot-head{margin-right:var(--space-2);color:var(--text-primary);font-weight:var(--weight-regular)}",
        // The dot and what follows it in a head of words ("Name · 3"): the accent, on tabular digits. One rule for the heads of the plate's notes (at the limit, not read, over plan, manual resets).
        ".wt-head-rest{color:var(--accent);font-variant-numeric:tabular-nums}",
        ".wt-foot-star{color:var(--text-primary);font-weight:var(--weight-semibold)}",
        // A group is one box (a short one goes to the next line whole) of at most the line's width, in which it wraps between its names;
        // a name is a box too, whole on a line and, only when it is wider than the line itself, broken where it must (a long address
        // does not run out of the card). Top-aligned: a group of two lines does not hang its first line above the line it stands on.
        ".wt-foot-g{display:inline-block;max-width:100%;min-width:0;vertical-align:top;color:var(--text-meta);transition:color var(--dur-5) var(--ease)}",
        ".wt-foot-g:is(:hover,:focus-visible){color:var(--text-primary)}",
        // A name wears the ink of the note it stands in (the meta ink, as the words around it; no colour of its own): a group turns the names to the ink with its words.
        ".wt-who{display:inline-block;max-width:100%;overflow-wrap:anywhere}",
        // "N more" is a button in the middle of the note's line. Under a finger the kit's 44px (kit: touch) would stretch that line, so it
        // keeps the height of its text and takes the 44px from a box around it that the line does not count (a box of its own, drawn by
        // nothing): the note is the height it is under a mouse.
        ".wt-foot-more{position:relative;margin:0 0 0 var(--space-1h);padding:0}",
        "@media(pointer:coarse){.wt-foot-more{min-height:0}.wt-foot-more:after{content:'';position:absolute;left:calc(var(--space-1) * -1);right:calc(var(--space-1) * -1);top:50%;height:var(--tap-h);margin-top:calc(var(--tap-h) / -2)}}",
        // On the narrow shell (the rail is a row of capsules over the card) the note follows the row. The place the hidden copy keeps would be an empty band
        // there, so it is gone: the row and the card stand close, and the card moves by the height of a note only where one is drawn.
        "@media(prefers-reduced-motion:reduce){.wt-foot-g{transition:none}}",
        // The quota plate (the owner's approved composition, 8 Nov 2026): the rail of rings and resets at the left (a row over the plate on a phone), the plate of the
        // picked limit with the opened account and every account's bar, and the history under it. Green is what is left, amber held by another limit, a red base an
        // account at its limit, a hatch last known, "?" not read, grey switched off; the picked ring, step and card are crimson. The tokens are the --q-* ones.
        ".q-rail{display:flex;flex-direction:column;gap:var(--space-1h);min-width:0;padding:var(--space-2) 0 0}",
        ".q-rings{display:flex;flex-direction:column;gap:var(--space-1h)}",
        // A ring on its capsule is one limit: the PR's ring at --q-ring, the value and its % one unit (one size, weight and baseline), the short name under it.
        ".q-ring{display:flex;flex-direction:column;align-items:center;gap:var(--space-0);width:100%;margin:0;padding:var(--space-2) var(--space-1) var(--space-1h);border:0;border-radius:16px;background:var(--q-capsule);box-shadow:var(--q-shadow);color:var(--text-primary);font:inherit;cursor:pointer;transition:background var(--dur-1) var(--ease),color var(--dur-1) var(--ease)}",
        ".q-ring:hover{background:var(--q-capsule-hover)}",
        ".q-ring.on{background:var(--q-sel);color:var(--q-sel-ink)}",
        ".q-ring-hidden{cursor:default;box-shadow:none;background:none;outline:var(--border-w) dashed var(--q-edge);outline-offset:calc(var(--border-w) * -1);color:var(--text-meta)}",
        ".q-ring-hidden:hover{background:none}",
        ".q-gauge{position:relative;display:block;width:var(--q-ring);height:var(--q-ring)}",
        ".q-gauge>.ring{position:absolute;inset:0;width:var(--q-ring);height:var(--q-ring)}",
        ".q-ring .ring-arc{stroke:var(--q-green)}",
        ".q-ring .ring-dot{fill:var(--q-green)}",
        ".q-ring .ring-track{stroke:var(--q-track)}",
        ".q-ring.on .ring-track{stroke:var(--q-sel-rule)}",
        ".q-pc{position:absolute;inset:0;display:flex;align-items:center;justify-content:center;font-size:var(--type-meta);font-weight:var(--weight-medium);line-height:1;letter-spacing:0;font-variant-numeric:tabular-nums}",
        ".q-ring-tx{display:flex;flex-direction:column;align-items:center;max-width:100%;min-width:0}",
        // A long scope ("Claude and GPT 7d") wraps onto a second line and no further (a name of eighty letters ends in an ellipsis there); the ring's title and
        // label say it whole.
        ".q-ring-name{display:-webkit-box;max-width:100%;overflow:hidden;font-size:var(--type-meta);font-weight:var(--weight-semibold);line-height:var(--lh-ui);text-align:center;overflow-wrap:anywhere;-webkit-line-clamp:2;-webkit-box-orient:vertical}",
        ".q-ring-sub{font-size:var(--type-meta);font-weight:var(--weight-medium);line-height:var(--lh-tight);color:var(--text-meta);white-space:nowrap}",
        ".q-ring.on .q-ring-sub{color:var(--q-sel-meta)}",
        ".q-ring-val{display:none}",
        ".q-star{margin-left:var(--border-w);color:var(--accent)}",
        ".q-ring.on .q-star{color:inherit}",
        // The ladder of resets of the picked limit: in how long, and how many account-windows come back if unused.
        ".q-ladder{display:flex;flex-direction:column;margin-top:var(--space-1h);padding:0 var(--space-0)}",
        ".q-ladder-title{margin:0 0 var(--space-1);padding:0 var(--space-1h);font-size:var(--type-meta);font-weight:var(--weight-medium);color:var(--text-meta)}",
        ".q-step{display:grid;grid-template-columns:var(--space-3) auto;column-gap:var(--space-1);align-items:center;width:100%;margin:0;padding:var(--space-1) var(--space-1h);border:0;border-radius:var(--radius-field);background:none;color:var(--text-primary);font:inherit;text-align:left;cursor:pointer}",
        ".q-step:hover{background:var(--q-capsule-hover)}",
        ".q-step.on{background:var(--q-sel);color:var(--q-sel-ink)}",
        ".q-step-dot{grid-row:span 2;width:var(--q-mark);height:var(--q-mark);border-radius:50%;background:var(--q-green);justify-self:center}",
        ".q-step-when{font-size:var(--type-body);font-weight:var(--weight-semibold);line-height:var(--lh-ui);white-space:nowrap;font-variant-numeric:tabular-nums}",
        ".q-step-back{font-size:var(--type-meta);line-height:var(--lh-ui);color:var(--text-meta);white-space:nowrap;font-variant-numeric:tabular-nums}",
        ".q-step.on .q-step-back{color:var(--q-sel-meta)}",
        ".q-ladder-more{align-self:flex-start;margin:var(--space-0) 0 0 18px;padding:var(--space-0) var(--space-1h);border:0;border-radius:var(--radius-md);background:none;color:var(--text-meta);font:inherit;font-size:var(--type-meta);cursor:pointer}",
        ".q-ladder-more:hover{color:var(--text-primary);background:var(--q-capsule-hover)}",
        ".q-ladder-none{padding:var(--space-0) var(--space-1h);font-size:var(--type-meta);color:var(--text-meta)}",
        // The plate of the picked limit: its head (the window, the scope, the stamp), its figure, the notes, then the opened account beside every account's bar.
        ".q-plate{min-width:0;padding:14px 14px var(--space-3);border-radius:var(--radius-card);background:var(--q-plate);box-shadow:var(--q-shadow)}",
        ".q-head{display:flex;flex-wrap:wrap;align-items:baseline;gap:var(--space-1) var(--space-2);min-width:0;margin:0 0 var(--space-2h)}",
        ".q-title{margin:0;font-size:var(--type-section);font-weight:var(--weight-semibold);line-height:var(--lh-ui);white-space:nowrap}",
        ".q-scope{display:inline-flex;align-items:center;height:22px;padding:0 var(--space-2);border-radius:var(--radius-pill);background:var(--q-chip);font-size:var(--type-meta);color:var(--text-meta);white-space:nowrap}",
        ".q-scope.own{background:var(--q-sel);color:var(--q-sel-ink)}",
        ".q-stamp{margin-left:auto;font-size:var(--type-meta);color:var(--text-meta)}",
        // The status line under the head: the figure and the notes, one wrapping line, a short note beside another, a long one on its own; each note
        // keeps its words whole where it can (a box of at most the line's width).
        ".q-status{display:flex;flex-wrap:wrap;align-items:baseline;column-gap:var(--space-3);row-gap:var(--space-0);margin:calc(var(--space-1h) * -1) 0 var(--space-2h)}",
        ".q-status>*{flex:0 1 auto;max-width:100%;min-width:0}",
        ".q-limit-details{margin-top:var(--space-2);font-size:var(--type-meta);color:var(--text-meta)}",
        ".q-limit-details>summary{cursor:pointer;padding:var(--space-1) 0}",
        ".q-limit-details .q-note+.q-note{margin-top:var(--space-1)}",
        ".q-fig{font-size:var(--type-meta);color:var(--text-meta);white-space:nowrap}",
        ".q-fig-num{font-size:var(--type-body);font-weight:var(--weight-semibold);color:var(--text-primary);font-variant-numeric:tabular-nums}",
        ".q-fig-num.none{color:var(--text-meta)}",
        ".q-cov,.q-note{margin:0;font-size:var(--type-meta);letter-spacing:0;line-height:var(--lh-ui);color:var(--text-meta)}",
        ".q-money{margin:0;font-size:var(--type-meta);color:var(--text-meta)}",
        ".q-money-fig{font-size:var(--type-section);font-weight:var(--weight-semibold);color:var(--text-primary);font-variant-numeric:tabular-nums}",
        ".q-plate[data-dim] .q-bar:not([data-lit]){opacity:.35}",
        ".q-plate[data-dim] .q-bar[data-lit]{box-shadow:var(--q-dim-shadow)}",
        ".q-accts{display:grid;grid-template-columns:var(--q-card-w) minmax(0,1fr);gap:var(--space-3);align-items:start}",
        // The opened account: its name and address, its share, its reset, its plan and age, its cooldowns, its bonus resets and credits, its details under one button.
        ".q-card{min-width:0;padding:var(--space-2) var(--space-2h) var(--space-1h);border-radius:var(--radius-tile);background:var(--q-sel);color:var(--q-sel-ink)}",
        ".q-card-none{background:var(--q-chip);color:var(--text-meta)}",
        ".q-card-head{display:flex;align-items:flex-start;gap:var(--space-1)}",
        ".q-an{flex:1 1 auto;min-width:0;font-size:var(--type-body);font-weight:var(--weight-semibold);line-height:var(--lh-ui);overflow-wrap:anywhere}",
        ".q-dot{margin-right:var(--space-1h);vertical-align:var(--space-0)}",
        ".q-clear{flex:none;display:inline-flex;align-items:center;justify-content:center;width:22px;height:22px;margin:calc(var(--border-w) * -1) calc(var(--space-1) * -1) 0 0;padding:0;border:0;border-radius:var(--radius-pill);background:none;color:var(--q-sel-meta);cursor:pointer}",
        ".q-clear:hover{background:var(--q-sel-rule);color:var(--q-sel-ink)}",
        ".q-em{font-size:var(--type-meta);line-height:var(--lh-ui);color:var(--q-sel-meta);overflow-wrap:anywhere}",
        ".q-al{margin-top:var(--space-0);font-size:var(--type-meta);line-height:var(--lh-ui);color:var(--q-sel-meta)}",
        ".q-card-none .q-al{color:var(--text-meta)}",
        ".q-big{font-size:var(--type-body);font-weight:var(--weight-semibold);color:var(--q-sel-ink);font-variant-numeric:tabular-nums}",
        ".q-u{white-space:nowrap}",
        ".q-ck{opacity:.85;font-variant-numeric:tabular-nums}",
        ".q-st{display:inline-block;padding:0 var(--space-1h);border-radius:var(--radius-pill);font-size:var(--type-meta);line-height:18px;font-weight:var(--weight-semibold);vertical-align:var(--border-w)}",
        ".q-st-limit{background:var(--q-limit-bg);color:var(--q-limit-ink)}",
        ".q-st-held{background:var(--q-amber);color:var(--q-held-ink)}",
        ".q-st-last,.q-st-unknown,.q-st-off{background:var(--q-chip);color:var(--q-sel-ink)}",
        ".q-cool{margin-top:var(--space-0);font-size:var(--type-meta);line-height:var(--lh-ui);color:var(--q-sel-ink)}",
        ".q-res{margin-top:var(--space-1h);padding-top:var(--space-1);border-top:var(--border-w) solid var(--q-sel-rule)}",
        ".q-rl{font-size:var(--type-meta);line-height:var(--lh-ui);color:var(--q-sel-ink)}",
        ".q-rk,.q-ra,.q-rl.unk{color:var(--q-sel-meta)}",
        ".q-rl b{font-weight:var(--weight-semibold)}",
        ".q-more{margin-top:var(--space-1h)}",
        ".q-more-btn{min-height:24px;padding:0 var(--space-2);border:0;border-radius:var(--radius-pill);background:var(--q-sel-rule);color:var(--q-sel-ink);font:inherit;font-size:var(--type-meta);cursor:pointer}",
        ".q-more-body{margin-top:var(--space-1h);color:var(--text-primary)}",
        // Every account's bar, with no figure in it (the card says the exact share); the grid is written by qBarsFit.
        ".q-bcol{min-width:0}",
        ".q-bhead{display:flex;align-items:center;min-height:20px;margin-bottom:var(--space-1h);font-size:var(--type-meta);color:var(--text-meta)}",
        ".q-bars{display:grid;grid-template-columns:repeat(var(--q-cols),var(--q-bw));grid-auto-rows:var(--q-bh);gap:var(--space-1h) var(--q-bar-gap);min-width:0}",
        ".q-bar{position:relative;display:block;width:100%;height:100%;margin:0;padding:0;border:0;border-radius:var(--radius-md);background:var(--q-track);overflow:hidden;cursor:pointer}",
        ".q-bar:disabled{cursor:default}",
        ".q-bar:hover{box-shadow:var(--q-edge-shadow)}",
        ".q-fill{position:absolute;left:0;right:0;bottom:0;border-radius:0 0 var(--radius-md) var(--radius-md);background:var(--q-green)}",
        ".q-fill.full{border-radius:var(--radius-md)}",
        ".q-bar.held .q-fill{background:var(--q-amber)}",
        ".q-bar.last .q-fill{background:var(--q-hatch-green)}",
        ".q-bar.last.held .q-fill{background:var(--q-hatch-amber)}",
        ".q-bar.spent::after,.q-bar.was::after{content:'';position:absolute;left:var(--q-base);right:var(--q-base);bottom:var(--q-base);height:var(--q-base);border-radius:var(--space-0);background:var(--q-red)}",
        ".q-bar.was::after{height:0;border-top:var(--q-base) dashed var(--q-red);border-radius:0;background:none}",
        ".q-bar.unknown{background:var(--q-track) var(--q-hatch-grey)}",
        ".q-bar.off .q-fill{background:var(--text-meta);opacity:.45}",
        ".q-unk{position:absolute;left:0;right:0;top:calc(50% - var(--space-2));font-size:var(--type-meta);font-weight:var(--weight-semibold);line-height:var(--space-4);text-align:center;color:var(--text-meta)}",
        ".q-bar.sel{box-shadow:var(--q-sel-shadow);overflow:visible}",
        ".q-bars[data-step] .q-bar:not(.lit){opacity:.35}",
        ".q-bars[data-step] .q-bar.lit{box-shadow:var(--q-lit-shadow)}",
        ".q-keys{display:flex;flex-wrap:wrap;gap:var(--space-0) var(--space-3);margin:var(--space-2) 0 0;font-size:var(--type-meta);color:var(--text-meta)}",
        ".q-key{display:inline-flex;align-items:center;gap:var(--q-bar-gap);white-space:nowrap}",
        ".q-sw{display:inline-block;width:var(--q-swatch);height:var(--q-swatch);border-radius:var(--radius-xs)}",
        // the small bars of About's legend: the plate's bars, 12 by 16
        ".q-swatch{flex:none;display:inline-block;width:var(--space-3);height:var(--space-4);border-radius:var(--radius-xs);cursor:default}",
        ".k-last{background:var(--q-hatch-green)}",
        ".k-unknown{background:var(--q-track) var(--q-hatch-grey)}",
        ".k-held{background:var(--q-amber)}",
        ".k-spent{background:var(--q-track);box-shadow:var(--q-spent-shadow)}",
        ".k-off{background:var(--text-meta);opacity:.45}",
        ".q-hist-title{margin:0 0 var(--space-2);font-size:var(--type-meta);font-weight:var(--weight-medium);color:var(--text-meta)}",
        "@media(prefers-reduced-motion:reduce){.q-ring,.q-step{transition:none}}",
        // A phone: the rings in a row and the ladder in a row under them, over the plate; the card over the bars. A plate of one account stands its card and its
        // bar side by side (the approved composition): the bar's column holds the word over it and the bar, the legend spans both columns under them.
        "@media(max-width:520.98px){.q-rail{padding:0}.q-rings{flex-direction:row;flex-wrap:wrap}.q-ring{flex-direction:row;flex:1 1 auto;width:auto;min-width:0;justify-content:center;gap:var(--q-bar-gap);padding:var(--space-1) var(--space-2h) var(--space-1) var(--space-1);border-radius:var(--radius-pill)}.q-gauge,.q-gauge>.ring{width:var(--q-ring-sm);height:var(--q-ring-sm)}.q-pc{display:none}.q-ring-tx{align-items:flex-start}.q-ring-name{text-align:left}.q-ring-sub{display:none}.q-ring-val{display:block;font-size:var(--type-meta);font-weight:var(--weight-medium);line-height:var(--lh-tight);font-variant-numeric:tabular-nums}.q-ladder{flex-direction:row;flex-wrap:wrap;align-items:center;gap:var(--space-0) var(--space-1);margin:0;padding:0}.q-ladder-title{margin:0 var(--space-0) 0 var(--space-1);padding:0}.q-step{display:inline-flex;align-items:baseline;gap:var(--q-bar-gap);width:auto;padding:var(--border-w) var(--space-2)}.q-step-dot{align-self:center}.q-ladder-more{margin:0}.q-plate{padding:var(--space-3) var(--space-2h) var(--space-2h)}.q-title{font-size:var(--type-body)}.q-accts{display:block}.q-card{margin:0 0 var(--space-2h)}.q-accts-one{display:grid;grid-template-columns:minmax(0,1fr) auto;grid-template-rows:auto 1fr auto;gap:var(--space-1) var(--space-2h);align-items:start}.q-accts-one .q-card{grid-column:1;grid-row:1/3;margin:0}.q-accts-one .q-bcol{display:contents}.q-accts-one .q-bhead{grid-column:2;grid-row:1}.q-accts-one .q-bars{grid-column:2;grid-row:2;grid-template-columns:var(--q-bw)}.q-accts-one .q-keys{grid-column:1/-1;grid-row:3;margin:0}}"
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
        close: ['M6 6l12 12', 'M18 6L6 18']
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

    // The kit's ring (kit: ring; the comment that opens that section is its contract): ringGauge(pct, form) is the one builder of it. An open
    // ring of 300 degrees (the gap of 60 degrees at the bottom) in a box of 184: one path draws the track and the arc, the arc as a dash of
    // pct on a path of length 100, and a dot of radius 22 sits at the end of the arc, at the angle 120 + 3 x pct degrees. The forms are ok
    // (pct is a number: track, arc, dot) and unknown (the track alone, dotted, no arc and no dot). The geometry is the svg's own
    // attributes; the colours are the kit's tokens, worn through the classes ring-track, ring-arc and ring-dot.
    var RING_PATH = 'M57 152.62A70 70 0 1 1 127 152.62';

    // The large ring (size lg, the rail's, 47px, the whole of its box: the hole that holds the share inside it): the same open ring, the radius of the path and
    // the strokes drawn for that size - a wider hole for the number, thinner strokes. The plain ring is 28px and the numbers above.
    var RING_SIZES = {
        plain: { px: 28, r: 70, track: 22, arc: 26, dot: 22 },
        lg: { px: 47, r: 81, track: 7, arc: 9, dot: 10.5 }
    };

    // The path of the ring of radius r: from the angle 120 degrees round to 60 (the gap of 60 degrees at the bottom).
    function ringPath(r) {
        var y = (92 + r * Math.sin(Math.PI / 3)).toFixed(2);
        return 'M' + (92 - r / 2) + ' ' + y + 'A' + r + ' ' + r + ' 0 1 1 ' + (92 + r / 2) + ' ' + y;
    }


    function ringGauge(pct, form, size) {
        var large = size === 'lg';
        var geo = large ? RING_SIZES.lg : RING_SIZES.plain;
        var path = large ? ringPath(geo.r) : RING_PATH;
        var svg = svgCanvas('0 0 184 184', geo.px, geo.px);
        svg.classList.add('ring');
        if (large) svg.classList.add('ring-lg');
        var unknown = form === 'unknown' || typeof pct !== 'number' || !isFinite(pct);
        var track = svgEl('path', { 'class': 'ring-track', d: path, fill: 'none', 'stroke-width': geo.track, 'stroke-linecap': 'round' });
        if (unknown) track.setAttribute('stroke-dasharray', large ? '.5 14' : '1 30');
        svg.appendChild(track);
        if (!unknown) {
            var share = Math.max(0, Math.min(100, pct));
            svg.appendChild(svgEl('path', { 'class': 'ring-arc', d: path, fill: 'none', 'stroke-width': geo.arc, 'stroke-linecap': 'round',
                pathLength: 100, 'stroke-dasharray': share + ' 100' }));
            var angle = (120 + 3 * share) * Math.PI / 180;
            svg.appendChild(svgEl('circle', { 'class': 'ring-dot', r: geo.dot, cx: (92 + geo.r * Math.cos(angle)).toFixed(2),
                cy: (92 + geo.r * Math.sin(angle)).toFixed(2) }));
        }
        return svg;
    }

    // The kit's text action (kit: text action; the comment that opens that section is its contract): three helpers, the only builders
    // of its parts. A part calls them and writes no class, no span and no dot of its own, so the look stays the kit's.
    //   wordButton(key)         the word: a button that is its word alone; key is a t() key; the caller adds its own wiring.
    //   wordDot(busy)           the dot between two words; busy makes it blink.
    //   wordRow(buttons, busy)  a row (.btn-word-row) of the buttons given, one dot between each pair; busy goes to every dot.
    // The head's About and Refresh are its two uses so far: About is wired where the head is drawn, Refresh is refreshButton below.
    // wordButton: a button that is its word alone, its name the word the reader sees (no label and no title).
    // The word comes from t(key). Its first character stands in a span of its own (.btn-word-cap), which the kit turns to the accent
    // when the word is lit, and the rest follows it as a plain text node. The span carries no aria attribute: the button's name stays
    // the whole word.
    // With iconName the kit's stroke icon stands before the word (.btn-word-mark, out of the reading order) and the word in a span of its own
    // (.btn-word-body) that the narrow shell folds (.btn-word-short): the button's name is still the word.
    function wordButton(key, params, iconName) {
        var word = t(key, params);
        var btn = el('button', 'btn-word' + (iconName ? ' btn-word-short' : ''));
        btn.setAttribute('type', 'button');
        var first = Array.from(word)[0] || '';
        var home = btn;
        if (iconName) {
            var mark = icon(iconName, 12);
            mark.classList.add('btn-word-mark');
            btn.appendChild(mark);
            home = el('span', 'btn-word-body');
            btn.appendChild(home);
        }
        home.appendChild(el('span', 'btn-word-cap', first));
        home.appendChild(document.createTextNode(word.slice(first.length)));
        return btn;
    }

    // The dot between two words: the accent, out of the reading order (aria-hidden). busy makes it blink (.is-busy): a read is in the
    // air, and the word does not change, so the dot is what says that the screen is being read.
    function wordDot(busy) {
        var dot = el('span', 'btn-word-dot' + (busy ? ' is-busy' : ''), '·');
        dot.setAttribute('aria-hidden', 'true');
        return dot;
    }

    // A row of words: the buttons given, in their order, with one dot between each pair (none before the first or after the last; one
    // button makes a row with no dot). busy is passed to every dot. The row is no group and has no name of its own: the words are
    // their own names. The part that draws it gives it its place by a class of its own (the head: .action-seg).
    function wordRow(buttons, busy) {
        var row = el('div', 'btn-word-row');
        buttons.forEach(function (btn, at) {
            if (at) row.appendChild(wordDot(busy));
            row.appendChild(btn);
        });
        return row;
    }

    // Refresh: the one control that asks the host for a live reading (it never repeats the ordinary read, Retry does that). Its word
    // never changes: inFlight is also set by every passive read (the 30 s poll and the like), so a word that followed it would flicker
    // on a read nobody asked for. While a request is in the air the button is disabled and says so to a screen reader (aria-busy=true;
    // at rest the attribute is absent), the kit keeps the word's rest ink (.btn-word[aria-busy=true]), and the dot beside it blinks.
    function refreshButton() {
        var btn = wordButton('btn.refresh', null, 'refresh');
        btn.disabled = inFlight;
        if (inFlight) btn.setAttribute('aria-busy', 'true');
        btn.setAttribute('data-focus', 'refresh');
        btn.addEventListener('click', function () {
            if (inFlight) return;
            refreshQuota();
        });
        return btn;
    }

    // The kit's mark tab (kit: mark tab; the comment that opens that section is its contract): the two helpers that build a row of choices
    // told by a mark. They stand together here and nothing else builds a tab, a mark, a number or a row, so the look stays the kit's. A part
    // calls them and writes no look of its own on what they make: only its wiring and its place. A pick redraws the row in the same order:
    // the picked tab is told by its ground (aria-pressed), nothing slides and nothing grows.
    //   markTab({key, mark, name, count, pressed})  one tab; the caller adds its wiring (aria-label, title, data-focus, the click).
    //   markRow(tabs, tablist)                      the row (.tab-mark-row) of the tabs given, in their order: a group, or a tablist.
    // The head's families are its use (renderHarnessSeg): the tabs it draws, and the ghosts it shows before the first answer.
    // markTab: key is the tab's own key (data-mark-key; a tab that is never picked has none); mark is a node of the caller's (the model's
    // drawing, or the initial where it has none), which wears the kit's class here (a drawing takes its class through classList, as an svg's
    // className is no string); name is the text; count, when it is a number, draws the number after the name, and when it is anything else
    // draws none; pressed, true or false, is the choice's state (aria-pressed, which the kit's look reads), and left out it is a tab that is no
    // choice (a ghost). The tab is a button of type "button" and nothing else is set on it: its name, its title, its focus key and its click
    // are the caller's.
    function markTab(spec) {
        var tab = el('button', 'tab-mark');
        tab.setAttribute('type', 'button');
        if (spec.key !== undefined && spec.key !== null) tab.setAttribute('data-mark-key', String(spec.key));
        if (typeof spec.pressed === 'boolean') tab.setAttribute('aria-pressed', spec.pressed ? 'true' : 'false');
        if (typeof spec.selected === 'boolean') {
            tab.setAttribute('role', 'tab');
            tab.setAttribute('aria-selected', spec.selected ? 'true' : 'false');
        }
        if (spec.mark) {
            spec.mark.classList.add('tab-mark-icon');
            tab.appendChild(spec.mark);
        }
        var nameNode = el('span', 'tab-mark-name', spec.name);
        var figure = typeof spec.count === 'number' && isFinite(spec.count) ? String(spec.count)
            : (typeof spec.count === 'string' ? spec.count : '');
        tab.appendChild(nameNode);
        if (figure !== '') tab.appendChild(el('span', 'tab-mark-count', figure));
        return tab;
    }

    // markRow: the row of the tabs given, in their order (the order never changes with the pick). It is a group (role="group"), or, when the
    // tabs are tabs of a tablist (selected), a tablist (tablist true); its name is the part's to give (aria-label), and so is its place (a class
    // of its own beside .tab-mark-row: the head's .harness-seg), never a look.
    function markRow(tabs, tablist) {
        var row = el('div', 'tab-mark-row');
        row.setAttribute('role', tablist ? 'tablist' : 'group');
        tabs.forEach(function (tab) { row.appendChild(tab); });
        return row;
    }

    // A custom property on a node's own style (the bars' grid reads its numbers from them: --q-cols, --q-bw, --q-bh). A page whose style has
    // no setProperty takes it as a key of the style.
    function setVar(node, name, value) {
        if (!node || !node.style) return;
        if (typeof node.style.setProperty === 'function') node.style.setProperty(name, value);
        else node.style[name] = value;
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
       Words. Every phrase the widget says comes from t(key, params).

       The English is the skill's strings_en.json, served by the i18n route
       together with the install's translation of it (the install-wide
       language, made by the model of Ouroboros itself, Telegram's way); this
       file keeps no copy of the table, because a copy drifts. What it keeps
       is MIN_EN, the few phrases of the screen drawn while that table has not
       arrived (a test holds them equal to the table's). English is also what
       any phrase the translation lacks is said in.
       ------------------------------------------------------------------ */

    var MIN_EN = {
        'min.title': 'Claudexor Quotas',
        'min.loading': 'The widget text is loading…',
        'min.failed': 'The widget text could not be loaded. It is asked for again with the next reading.',
        'btn.retry': 'Retry'
    };

    // lang: the install's language tag ('' = English); tr: its translation of our keys;
    // select: the plural map of the language as the install recorded it; asked: the
    // route has answered (or failed to) at least once.
    var i18n = { en: null, tr: {}, lang: '', rev: 0, select: null, asked: false };
    var nameCache = {};

    function hasOwn(obj, key) {
        return !!obj && Object.prototype.hasOwnProperty.call(obj, key);
    }

    // The CLDR category of a whole number in the install's language: from the map the
    // install recorded for it (0..100 and its period), else from the browser's own rules.
    function pluralCategory(n) {
        if (typeof n !== 'number' || !isFinite(n) || n % 1 !== 0) return 'other';
        var abs = Math.abs(n);
        var select = i18n.select;
        var map = select && typeof select === 'object' ? (select.map || select) : null;
        if (map && typeof map === 'object') {
            if (hasOwn(map, String(abs))) return String(map[String(abs)]);
            var period = select.period;
            if (period && hasOwn(map, String(abs % period))) return String(map[String(abs % period)]);
        }
        try {
            return new Intl.PluralRules(i18n.lang || 'en').select(abs);
        } catch (err) {
            return 'other';
        }
    }

    // English: a string, or {one, other} for a phrase that counts ("{n} account" / "{n} accounts").
    function enForm(entry, n) {
        if (typeof entry === 'string') return entry;
        if (!entry || typeof entry !== 'object') return null;
        var text = n === 1 && typeof entry.one === 'string' ? entry.one : entry.other;
        return typeof text === 'string' ? text : null;
    }

    // A translation: a string, or {forms: {one, few, many, other…}} chosen by the count.
    function trForm(entry, n) {
        if (typeof entry === 'string') return entry;
        if (!entry || typeof entry !== 'object' || !entry.forms || typeof entry.forms !== 'object') return null;
        var forms = entry.forms;
        var text = forms[pluralCategory(n)];
        if (typeof text !== 'string') text = forms.other;
        if (typeof text !== 'string') text = forms.many;
        if (typeof text !== 'string') text = forms[Object.keys(forms)[0]];
        return typeof text === 'string' ? text : null;
    }

    function fillIn(text, params) {
        if (!params) return text;
        return text.replace(/\{([A-Za-z_][A-Za-z0-9_]*)\}/g, function (all, name) {
            return params[name] === undefined || params[name] === null ? all : String(params[name]);
        });
    }

    // The phrase for `key` with its {placeholders} filled. `n` picks the plural form.
    function t(key, params) {
        var n = params && typeof params.n === 'number' ? params.n : null;
        var text = null;
        if (i18n.lang && hasOwn(i18n.tr, key)) text = trForm(i18n.tr[key], n);
        if (text === null && i18n.en && hasOwn(i18n.en, key)) text = enForm(i18n.en[key], n);
        if (text === null && hasOwn(MIN_EN, key)) text = MIN_EN[key];
        return fillIn(text === null ? key : text, params);
    }

    function cap(text) {
        return text ? text.charAt(0).toUpperCase() + text.slice(1) : text;
    }

    // A cell of the reset row (`pre`, the bold value `b`, `post`) from a phrase: the value
    // stands wherever the language puts it.
    function boldAt(text, bold) {
        var at = bold ? text.indexOf(bold) : -1;
        return at < 0 ? { pre: text } : { pre: text.slice(0, at), b: bold, post: text.slice(at + bold.length) };
    }

    // What the route answered, if it answered a text table: the language, our keys in it
    // and the plural map. Anything else (a failed read, another shape) changes nothing.
    function adoptI18n(answer) {
        i18n.asked = true;
        if (!answer || typeof answer !== 'object' || Array.isArray(answer) || typeof answer.language !== 'string') return;
        var en = answer.en;
        if (en && typeof en === 'object' && !Array.isArray(en) && Object.keys(en).length) i18n.en = en;
        var lang = /^en(?:[-_]|$)/i.test(answer.language) ? '' : answer.language;
        var strings = answer.strings && typeof answer.strings === 'object' && !Array.isArray(answer.strings)
            ? answer.strings : {};
        i18n.lang = lang;
        i18n.tr = lang ? strings : {};
        i18n.rev = typeof answer.revision === 'number' ? answer.revision : 0;
        i18n.select = lang && answer.plural_select && typeof answer.plural_select === 'object'
            ? answer.plural_select : null;
    }

    function readI18n(mine) {
        return request(I18N_ROUTE, { method: 'GET' }, I18N_TIMEOUT_MS).then(function (result) {
            var answer = null;
            if (result.ok) {
                try { answer = JSON.parse(result.body); } catch (err) { answer = null; }
            }
            if (stopped || mine !== generation) return;
            adoptI18n(answer);
            // The first screen waits for its text: it is drawn as soon as the text is here.
            if (i18n.en && !currentView && !stopped) rerender();
        })['catch'](function () { if (!stopped && mine === generation) i18n.asked = true; });
    }

    // Before the text table has come (or when it could not): the one card the screen can be
    // said with, in MIN_EN, and its Retry.
    function drawNoText(failed) {
        rebuilding = true;
        try { root.textContent = ''; } finally { rebuilding = false; }
        // No head yet, but the card stands all the same: the one card says it, on the shell's ground.
        var shell = shellOf(root);
        var card = el('div', 'card empty-card');
        card.appendChild(el('h4', 'empty-title', t('min.title')));
        card.appendChild(el('p', 'empty-desc', failed ? t('min.failed') : t('min.loading')));
        if (failed) card.appendChild(retryButton());
        shell.appendChild(card);
    }

    // Month and weekday names: ours in English, the browser's own in the install's language.
    function dateName(kind, i) {
        var english = kind === 'm' ? MONTHS : WEEKDAYS;
        if (!i18n.lang) return english[i];
        var id = i18n.lang + '|' + kind + i;
        if (nameCache[id] === undefined) {
            var name = '';
            try {
                var sample = kind === 'm' ? new Date(2021, i, 15, 12) : new Date(2021, 0, 3 + i, 12);
                name = new Intl.DateTimeFormat(i18n.lang, kind === 'm' ? { month: 'short' } : { weekday: 'short' })
                    .format(sample).replace(/\.$/, '');
            } catch (err) {
                name = '';
            }
            nameCache[id] = name || english[i];
        }
        return nameCache[id];
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
        return at.getDate() + ' ' + dateName('m', at.getMonth()) + ', ' + pad2(at.getHours()) + ':' + pad2(at.getMinutes());
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
        if (mins <= 1) return future ? t('rel.moment_in') : t('rel.moment_ago');
        if (mins < 60) return future ? t('rel.min_in', { v: mins }) : t('rel.min_ago', { v: mins });
        if (mins < 2880) {
            var hours = Math.round(mins / 60);
            return future ? t('rel.h_in', { v: hours }) : t('rel.h_ago', { v: hours });
        }
        var days = Math.round(mins / 1440);
        return future ? t('rel.d_in', { v: days }) : t('rel.d_ago', { v: days });
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
        return dateName('d', at.getDay()) + ' ' + at.getDate() + ' ' + dateName('m', at.getMonth()) + ' '
            + pad2(at.getHours()) + ':' + pad2(at.getMinutes());
    }

    // The whole moment, for a tooltip or a table: "Sat 27 Sep, 14:05".
    function momentWords(seconds) {
        var at = new Date(seconds * 1000);
        if (isNaN(at.getTime())) return '';
        return dateName('d', at.getDay()) + ' ' + at.getDate() + ' ' + dateName('m', at.getMonth())
            + ', ' + pad2(at.getHours()) + ':' + pad2(at.getMinutes());
    }

    // The moment in the tip over the chart, short: the clock alone on the chart's today ("01:19"), with the short day on any other ("Wed 01:19").
    function tipMoment(seconds, nowSeconds) {
        var at = new Date(seconds * 1000);
        if (isNaN(at.getTime())) return '';
        var clock = pad2(at.getHours()) + ':' + pad2(at.getMinutes());
        var today = new Date(nowSeconds * 1000);
        return !isNaN(today.getTime()) && at.toDateString() === today.toDateString() ? clock : dateName('d', at.getDay()) + ' ' + clock;
    }

    // How long ago, as a kind and a number — the words are said by agePhrase, in any language.
    function agePart(iso) {
        var at = Date.parse(String(iso || ''));
        if (!isFinite(at)) return null;
        var mins = Math.round(Math.max(0, Date.now() - at) / 60000);
        if (mins < 2) return { k: 'now', v: 0 };
        if (mins < 90) return { k: 'min', v: mins };
        if (mins < 2880) return { k: 'h', v: Math.round(mins / 60) };
        return { k: 'd', v: Math.round(mins / 1440) };
    }

    function agePhrase(part) {
        if (part.k === 'now') return t('age.just_now');
        if (part.k === 'min') return t('age.min', { v: part.v });
        if (part.k === 'h') return t('age.h', { v: part.v });
        return t('age.d', { v: part.v });
    }

    function ageWords(iso) {
        var part = agePart(iso);
        return part ? agePhrase(part) : '';
    }

    // "<age> ago", or just "just now".
    function agoWords(iso) {
        var part = agePart(iso);
        if (!part) return '';
        return part.k === 'now' ? t('age.just_now') : t('age.ago', { age: agePhrase(part) });
    }

    function minutesWords(seconds) {
        if (typeof seconds !== 'number' || !isFinite(seconds)) return '';
        var mins = Math.round(seconds / 60);
        return mins < 90 ? t('age.min', { v: mins }) : t('age.h', { v: Math.round(mins / 6) / 10 });
    }

    // "33 min", "30–33 min", "1.2–2 h": one unit said once.
    function spanWords(lo, hi) {
        var a = minutesWords(lo);
        var b = minutesWords(hi);
        if (a === b) return a;
        if (typeof lo === 'number' && typeof hi === 'number' && isFinite(lo) && isFinite(hi)) {
            var mlo = Math.round(lo / 60);
            var mhi = Math.round(hi / 60);
            if (mlo < 90 && mhi < 90) return t('age.min', { v: mlo + '–' + mhi });
            if (mlo >= 90 && mhi >= 90) {
                return t('age.h', { v: Math.round(mlo / 6) / 10 + '–' + Math.round(mhi / 6) / 10 });
            }
        }
        return a + '–' + b;
    }

    // "in 1 h", "in 6 h", "in 3 days": a checkpoint's distance from now.
    function offsetWords(seconds) {
        if (seconds % 86400 === 0 && seconds >= 86400) {
            var days = seconds / 86400;
            return days === 1 ? t('offset.h', { v: 24 }) : t('offset.days', { n: days });
        }
        return t('offset.h', { v: Math.round(seconds / 3600) });
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
        return t('n.' + word.replace(/ /g, '_'), { n: n });
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
        if (state === 'ok') return t('facet.read');
        if (state === 'not_read') return t('facet.not_read');
        if (state === 'failed') return t('facet.failed');
        return t('facet.indeterminate');
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

    // The small meter of an account's row (the window plate's bars, barStrip, speak a language of their own: forms and the ink, no state
    // colour): as long as the share LEFT on one 0–100% scale, in one neutral ink over a hairline base. Amber is a share held back now,
    // a share at its limit has no fill but a red base, and a last-known reading is muted and claims neither.
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

    // The readings held as last known that carry at least one window (a reading with no window in it has nothing to show).
    function staleWindows(account) {
        return (((account && account.quota) || {}).stale || []).filter(function (snap) { return !!(snap && snap.constraints && snap.constraints.length); });
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
        // Only positive evidence says signed out: null is the engine not saying.
        if (account.signed_in === false) return 'signed_out';
        if (account.enabled === false) return 'disabled';
        if (verificationFailed(account)) return 'failed';
        return '';
    }

    // signed_out, disabled or failed, in words.
    function inactiveWord(reason) {
        return t('state.' + reason);
    }

    // An account that calls for a look: a spent shared window, a cooldown or a
    // failed check. Switched off and signed out are states, not alarms.
    function isAlertAccount(account) {
        var quota = account.quota || {};
        var cooling = liveWindows(account).some(function (c) { return !isModelWindow(c) && isCooling(c); });
        return quota.state === 'exhausted' || quota.state === 'cooling' || verificationFailed(account) || cooling;
    }

    function isActive(account) {
        return account.signed_in !== false && account.enabled !== false;
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
            return { tone: 'bad', word: inactiveWord(reason) + ' · ' + inactiveWord('failed') };
        }
        if (reason) return { tone: reason === 'failed' ? 'bad' : 'muted', word: inactiveWord(reason) };
        var quota = account.quota || {};
        if (quota.state === 'exhausted') return { tone: 'bad', word: t('state.limit_reached') };
        if (quota.state === 'cooling' || cooldownsOf(quota, 'account').length) {
            return { tone: 'warn', word: t('state.cooling') };
        }
        if (verificationFailed(account)) return { tone: 'bad', word: t('state.failed') };
        if (quota.absence) {
            // Cursor has no time window to read, only its budget per billing cycle: a standing
            // "no source" with that budget current is its normal state, not a quota that is unavailable.
            if (budgetOnly(account)) {
                return { tone: 'muted', word: t('state.budget_only'),
                         full: 'No time window is reported for this account, only its included usage per billing cycle.' };
            }
            var action = absenceAction(account, quota.absence);
            return { tone: 'warn', word: t('state.quota_unavailable') + (action ? ' · ' + action : '') };
        }
        if (liveWindows(account).some(function (c) { return isModelWindow(c) && isSpent(c); })) {
            return { tone: 'warn', word: t('state.model_at_limit') };
        }
        var held = modelHoldWords(quota);
        if (held) return { tone: 'warn', word: held.text, full: held.full };
        if (hasSpentModelCap(account)) return { tone: 'warn', word: t('state.model_held') };
        if (quota.state === 'not_checked') return { tone: 'muted', word: t('state.not_checked') };
        if (quota.state === 'no_fresh_window' || quota.state === 'not_current') {
            return { tone: 'muted', word: t('state.no_current') };
        }
        if (!worstWindow(account)) return { tone: 'muted', word: liveWindows(account).length ? t('state.no_ratio') : t('state.no_reading') };
        var tone = accountTone(account, facets);
        return { tone: tone, word: tone === 'warn' ? t('state.nearly_used') : (tone === 'muted' ? t('state.last_known') : t('state.ready')) };
    }

    // The model holds the skill carried apart from the windows, in words:
    // one is named with its model and its kind; several are counted, and the
    // full sentence names each scope, its end and its provenance — two
    // scopes are never one "model cooldown".
    function modelHoldWords(quota) {
        var holds = modelHolds(quota);
        if (!holds.length) return null;
        function kindWords(h) { return h.kind === 'cooldown' ? 'model cooldown' : 'model limit reached'; }
        function kindLabel(h) { return h.kind === 'cooldown' ? t('state.model_cooldown') : t('state.model_limit'); }
        function sentence(h) {
            var when = formatResetAt(h.until);
            var end = when ? ' until ' + when
                : (h.kind === 'cooldown' ? ', ' + t(COOLDOWN_END[h.note] || COOLDOWN_END.not_reported) : '');
            return kindWords(h) + ': ' + (modelLabel(h.models, h.omitted) || 'models not named') + end
                + (h.stale ? ', reported by a stale reading' : '') + ' (' + scopeList(h.models, h.omitted) + ')';
        }
        if (holds.length === 1) {
            return { text: kindLabel(holds[0]) + ': ' + (modelLabel(holds[0].models, holds[0].omitted) || t('state.models_not_named')),
                     full: sentence(holds[0]) };
        }
        var kinds = {};
        holds.forEach(function (h) { kinds[h.kind] = true; });
        var noun = kinds.cooldown && kinds.limit ? 'model holds' : (kinds.cooldown ? 'model cooldowns' : 'model limits reached');
        var count = kinds.cooldown && kinds.limit ? t('state.holds_both', { n: holds.length })
            : (kinds.cooldown ? t('state.holds_cooldowns', { n: holds.length }) : t('state.holds_limits', { n: holds.length }));
        return { text: count, full: holds.length + ' ' + noun + ': ' + holds.map(sentence).join('; ') };
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
    // widget carries one, the initial in its box when it does not — never
    // somebody else's logo. The head hands this node to the kit's markTab as the
    // tab's mark, and the kit puts its own class on it (what the picked tab turns
    // the accent and grows).
    function familyMark(group) {
        return BRAND_MARKS[group.harness_id]
            ? brandIcon(group.harness_id, FAMILY_MARK_PX)
            : el('span', 'harness-initial', ((familyName(group) || '?').charAt(0) || '?').toUpperCase());
    }

    function appendHarnessNotes(parent, group, facets) {
        if (group.harness_status && group.harness_status !== 'ok') {
            parent.appendChild(dotLabel(t('note.harness_status', { status: group.harness_status }), 'warn'));
        }
        if (group.harness_enabled === false) {
            parent.appendChild(dotLabel(t('note.harness_disabled'), 'warn'));
        }
        if (group.catalog_known === false && !catalogOmitted(currentView, 'catalog')) {
            parent.appendChild(dotLabel(t('note.catalog', { state: facetWord(facets.catalog) }), facetTone(facets.catalog)));
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
        var box = el('div', 'line-stack win-lines');
        poolGroups(constraints).forEach(function (group) {
            windowTags(group.windows).forEach(function (m) {
                var out = stale ? null : windowOut(m, quota);
                var tone = out ? (out.kind === 'spent' ? 'bad' : 'warn') : '';
                var line = el('div', 'meta-line win-line' + (stale ? ' stale' : ''));
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
        unreadable: 'cooldown.end_unreadable',
        not_reported: 'cooldown.end_not_reported',
        passed: 'cooldown.end_passed'
    };

    // One sentence per cooldown: what it holds, until when (or why not known)
    // and whether a stale reading reported it. Never "Limit reached".
    function renderCooldowns(parent, quota) {
        cooldownsOf(quota).forEach(function (c) {
            var row = el('div', 'quota-cooldown');
            row.appendChild(icon('warn', 12));
            var body = el('span', 'quota-cooldown-body');
            row.appendChild(body);
            var what = c.scope === 'models' ? (modelLabel(c.models, c.models_omitted) || t('cooldown.some_models')) : t('cooldown.whole_account');
            body.appendChild(el('span', null, t('cooldown.label') + ' · ' + what));
            var said = [t('cooldown.label'), what];
            var until = el('span', 'quota-when', ' · ' + t('cooldown.until', { when: '' }));
            if (c.until && appendStamp(until, c.until, 'warn', true)) {
                body.appendChild(until);
                said.push(t('cooldown.until', { when: formatResetAt(c.until) + ' ' + relTime(c.until) }));
            } else {
                var end = t(COOLDOWN_END[c.until_note] || COOLDOWN_END.not_reported);
                body.appendChild(el('span', 'quota-when', ' · ' + end));
                said.push(end);
            }
            if (c.freshness && c.freshness !== 'fresh') {
                var seen = relTime(c.observed_at);
                var from = t('cooldown.stale') + (seen ? ' ' + t('cooldown.observed', { when: seen }) : '');
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
            var until = el('span', 'quota-when', unnamed ? ' · reported until ' : ' · ' + t('cooldown.until', { when: '' }));
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
                var from = t('cooldown.stale') + (seen ? ' ' + t('cooldown.observed', { when: seen }) : '');
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
        return t('absence.retry', { n: Math.max(1, Math.ceil((at - Date.now()) / 60000)) });
    }

    // Only the approved generic actions; never raw reason or detail text.
    function absenceAction(account, absence) {
        if (!absence) return '';
        if (absence.action_kind === 'sign_in_if_unverified') return account && account.verified_live ? '' : t('absence.sign_in');
        if (absence.action_kind === 'source_missing') return t('absence.no_source');
        if (absence.action_kind === 'retry') return retryAction(absence.retry_at);
        return '';
    }

    function renderAbsence(parent, account, absence) {
        if (!absence) return;
        var row = el('div', 'quota-unavailable');
        row.appendChild(icon('warn', 13));
        row.appendChild(el('span', null, absence.message || t('absence.unavailable')));
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
        p.appendChild(el('span', quotaClass(quota.state), quota.label ? engineWord(String(quota.label)) : t('acc.no_quota_data')));
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
       adds, averages or projects anything of its own — except the one budget
       a family with no time window reports per billing cycle (cycleBudget).
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
        var hidden = hiddenRows(currentView);
        groups = groups.filter(function (g) { return !hidden[g.key]; });
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
            // The combined same-account view reads more history; it is asked
            // for with the chart, never on its own.
            parts.push('joint=1');
            if (modelChoice) parts.push('model=' + encodeURIComponent(modelChoice));
            if (kindChoice !== 'text') parts.push('kind=' + encodeURIComponent(kindChoice));
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
        return !chart || chart.group_key !== key || chart.horizon !== horizonFor(groupByKey(groups, key))
            || scopeStale(chart);
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
        if (!newest) return { text: t('obs.none'), inline: t('obs.none'), old: true, detail: t('obs.none') };
        var pa = agePart(oldest);
        var pb = agePart(newest);
        var a = pa ? agePhrase(pa) : '';
        var b = pb ? agePhrase(pb) : '';
        var inline;
        if (a === b) {
            inline = pa && pa.k === 'now' ? t('obs.now') : t('obs.ago', { age: a });
        } else if (pb && pb.k === 'now') {
            inline = t('obs.up_to', { age: a });
        } else {
            var span = pa && pb && pa.k === pb.k ? t('age.' + pa.k, { v: pb.v + '–' + pa.v }) : b + '–' + a;
            inline = t('obs.range', { span: span });
        }
        var text = cap(inline);
        var seen = clockAt(oldest) + (clockAt(oldest) !== clockAt(newest) ? '–' + clockAt(newest) : '');
        var detail = t('obs.detail', { span: seen })
            + (summary.status_read_at ? ' · ' + t('obs.detail_status', { time: clockAt(summary.status_read_at) }) : '')
            + ' · ' + t('obs.detail_zone', { zone: tzWords() });
        return { text: text, inline: inline, old: Date.now() - Date.parse(oldest) > OLD_READING_MS, detail: detail };
    }

    // The phrase of each flag in the Details' "Restricted now" line: its count, its words and its
    // windows in one phrase, so a language can decline it by the count.
    var FLAG_WORDS = {
        disabled: 'dv.flag_disabled',
        signed_out: 'dv.flag_signed_out',
        auth_failed: 'dv.flag_auth_failed',
        cooling: 'dv.flag_cooling',
        model_exhausted: 'dv.flag_model_exhausted',
        other_limit_spent: 'dv.flag_other_limit_spent',
        account_state_unknown: 'dv.flag_account_state_unknown'
    };
    // The named reasons a bar is held, in the order the groups stand in; the word of each is the string flag.<name>.
    var FLAG_ORDER = ['disabled', 'signed_out', 'auth_failed', 'cooling', 'model_exhausted', 'other_limit_spent', 'account_state_unknown'];

    // The word of a held bar's reason: the translated string of a named one, the raw value of any other. The legend, the group lines, the
    // summary word and the chart's tip all say a reason through this, so they say it in one language.
    function flagWord(f) {
        return FLAG_ORDER.indexOf(f) >= 0 ? t('flag.' + f) : f;
    }

    var LENGTH_NAMES = { week: 'Weekly', day: 'Daily', hour: 'Hourly', minute: 'Per-minute' };

    // "5 hours" → "5-hour", "week" → "Weekly".
    function lengthName(seconds) {
        var words = windowLength(seconds);
        if (!words) return t('len.none');
        if (LENGTH_NAMES[words]) return t('len.' + words);
        var found = /^(\d+) (\w+?)s$/.exec(words);
        return found ? t('len.n_' + found[2], { n: Number(found[1]) }) : words;
    }

    // A model group's name as the engine writes it with a window length: the id
    // "Gemini-5h", "Claude and GPT-weekly" (Antigravity's fallback "<group>-<window>"),
    // or a label of the same shape. The length alone ("5h", "7 day") names no group.
    var GROUP_WINDOW = new RegExp('^(.*\\S)(?:\\s*[-_:\\u00b7]\\s*|\\s+)(?:\\d+(?:\\.\\d+)?\\s*'
        + '(?:h|hr|hrs|hours?|d|days?|w|wks?|weeks?|m|mins?|minutes?)|hourly|daily|weekly|monthly)$', 'i');

    // The group a model-scoped limit belongs to, or ''. Only a limit tied to models
    // has a group; its id is read first, then its label.
    function groupName(g) {
        if (!(g.models || []).length && (!g.model_scope || g.model_scope === 'none')) return '';
        var byId = GROUP_WINDOW.exec(String(g.meaning || '').trim());
        var byLabel = GROUP_WINDOW.exec(String(g.label || '').trim());
        // An id that is a bare machine name ("claude_gpt_5h") yields to the label's own words ("Claude and GPT · 5 hours") where the label has them.
        var bare = function (m) { return m[1].replace(/\s*[-_:\u00b7]$/, '').trim(); };
        if (byId && byLabel && /^[a-z0-9_]+$/.test(bare(byId))) return bare(byLabel);
        var found = byId || byLabel;
        return found ? bare(found) : '';
    }

    // What tells a limit apart in its family besides its length: its group's name
    // when the engine names one (short, and the same on the scope row and in the
    // graph), else its pool or its models' common stem.
    function limitTag(g) {
        var group = groupName(g);
        if (group) return group;
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
            out.push(t(FLAG_WORDS[flag] || 'dv.flag_other', { n: slot.accounts, flag: flag, windows: num(slot.windows, 2) }));
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
        if (c.stale_only) out.push(t('dv.stale', { n: c.stale_only }));
        if (c.invalid) out.push(t('dv.unreadable', { n: c.invalid }));
        if (c.conflicting) out.push(t('dv.disagree', { n: c.conflicting }));
        if (c.reset_passed) out.push(t('dv.reset_since', { n: c.reset_passed }));
        var lk = (g.last_known || {}).accounts;
        if (lk) out.push(t('dv.last_known', { n: lk }));
        var other = applicabilityUnknown(g);
        if (other) out.push(t('dv.other_accounts', { n: other }));
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
        return a ? String(a.label || a.email || key) : t('account.some');
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
        not_read: 'reading.why_not_read',
        unreadable: 'reading.why_unreadable',
        sources_disagree: 'reading.why_sources_disagree',
        reset_passed: 'reading.why_reset_passed',
        too_old: 'reading.why_too_old',
        no_reading_in_answer: 'reading.why_no_reading_in_answer'
    };

    function originWords(origin) {
        if (origin === 'history') return t('chart.tip_origin_history');
        if (origin === 'screen') return t('chart.tip_origin_screen');
        if (origin === 'cached') return t('chart.tip_origin_cached');
        return t('chart.tip_origin_stale');
    }

    // What one bar says to a screen reader (its aria-label), and under the pointer where it has no opening of its own (a bar of no account). A bar of the
    // billing-cycle budget (cycleBudget) carries its own words: they are money.
    function barWords(bar, index, rowName) {
        if (typeof bar.words === 'string' && bar.words) return bar.words;
        var name = accountName(index, bar.account);
        if (bar.state === 'unknown') {
            var last = bar.last_reading;
            return t('bar.unknown', { name: name, row: rowName, why: t(UNKNOWN_WORDS[bar.why] || 'reading.why_not_read') })
                + (last && typeof last.left === 'number' ? '; ' + t('bar.last_read', {
                    pct: exactPct(last.left * 100, last.left <= 0), when: relTime(last.observed_at) || '' }) : '')
                + ' — ' + t('bar.not_counted');
        }
        var left = Math.max(0, Math.min(1, bar.left));
        var spent = !!bar.at_limit;
        if (bar.state === 'last_known') {
            var then = t('bar.share_last', { name: name, pct: exactPct(left * 100, spent) })
                + (spent ? ' (' + t('bar.limit_then') + ')' : '') + (bar.restricted ? ' — ' + t('aria_bar.restricted_now') : '');
            if (bar.resets_at) then += ' · ' + t('bar.reported_reset', { when: whenWords(bar.resets_at) });
            return then + ' · ' + t('bar.dated', { when: relTime(bar.observed_at) || t('reading.time_unknown'), origin: originWords(bar.origin) });
        }
        var words = t('bar.share', { name: name, pct: exactPct(left * 100, spent) })
            + (spent ? ' — ' + t('aria_bar.at_limit') : '') + (bar.restricted ? ' — ' + t('aria_bar.restricted_now') : '');
        if (bar.resets_at) words += ' · ' + t(spent ? 'bar.until' : 'bar.resets', { when: whenWords(bar.resets_at) });
        if (bar.observed_at) words += ' · ' + t('budget.read', { when: relTime(bar.observed_at) });
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


    // The chart is drawn to the room its card has (chartSize). The size it was drawn to is kept, and a window that changes its width afterwards draws it anew:
    // once in CHART_FIT_MS while the width keeps changing, and once more when it stops, the drawing being the one the poll makes (the pick, the focus and the chart's cursor are carried over).
    // It answers no pick, so it moves nothing (fitDrawing: render puts no motion on it): the bars, the rail and the rings do not come in again as the window is dragged.
    var CHART_FIT_MS = 120;
    var chartDrawn = null;
    var chartFitTimer = 0;
    var fitDrawing = false;
    function chartFit() {
        if (stopped || chartFitTimer) return;
        chartFitTimer = setTimeout(function () {
            chartFitTimer = 0;
            if (stopped || !chartDrawn) return;
            var size = chartSize();
            if (size.W === chartDrawn.W && size.H === chartDrawn.H) return;
            fitDrawing = true;
            try {
                rerender();
            } finally {
                fitDrawing = false;
            }
        }, CHART_FIT_MS);
    }

    // A bar that has opened (kit: bars) by the keyboard on it, or as the picked one, or as the first of those a pressed step of the ladder opened, is brought into the room of its row where its opening reaches past it (the row
    // goes sideways where it cannot hold the opening): inline 'nearest' and only then, block 'nearest' so that a page that shows the row does not jump. The edges are worked out after.

    // The list of accounts opens or shuts (the word over the bars, a row of the list, the "N more" of the notes). It is the one place that sets accountsOpen: opening measures the place the
    // list replaces now, and the drawing that follows plays the fold (accountsRun). The caller draws.
    function accountsSet(open) {
        if (accountsOpen === open) return;
        if (open) {
            var place = root && typeof root.querySelector === 'function' ? root.querySelector('.acc-main') : null;
            // Not rounded down: a pixel lost here is a pixel the widget loses while the list stands (rounded up to a hundredth).
            accountsSpace = place && typeof place.getBoundingClientRect === 'function' ? Math.ceil(place.getBoundingClientRect().height * 100) / 100 : 0;
            // The word leaves the plate for the strip over the list (the plate is folded away while the list stands): the list is that much shorter, so the
            // widget keeps its one height.
            try {
                var word = root.querySelector('[data-focus="accounts"]');
                var air = parseFloat(window.getComputedStyle(document.documentElement).getPropertyValue('--space-1')) || 0;
                if (word && accountsSpace) accountsSpace = Math.max(0, Math.floor((accountsSpace - word.getBoundingClientRect().height - air) * 100) / 100);
            } catch (err) {
                // A layout that cannot be read keeps the place's own height.
            }
        }
        accountsOpen = open;
        accountsRun = open ? 'open' : 'shut';
    }

    // A fold of the accounts place (.acc-main: the bars, the chart and the card; .acc-listfold: the list): one row, 1fr open and 0fr shut, over the node it holds (.acc-in).
    function accFold(kind, shut) {
        var fold = el('div', 'acc-fold acc-' + kind + (shut ? ' acc-shut' : ''));
        fold.appendChild(el('div', 'acc-in'));
        return fold;
    }

    // `limitKey` is the limit whose bar selected the account; a row of the
    // account list passes none.
    function selectAccount(key, opener, limitKey) {
        if ((key || '') !== selectedAccountKey) cardReset();
        selectedAccountKey = key || '';
        selectionOpener = selectedAccountKey ? (opener || '') : '';
        selectionLimit = selectedAccountKey ? (limitKey || '') : '';
        // The reader's own pick (or the pick let go): the widget picks no more for it, in the window on screen, until the window or the family changes (pickDecide).
        pickAuto = false;
        pickDue = false;
        pickHome = selectedAccountKey ? pickAt : '';
        pickBare = selectedAccountKey ? '' : pickAt;
        diagnosticsOpen = false;
        rerender();
    }

    // The top bar of a limit: the first (the row goes fullest first) with a current reading, something left and no hold on it. A last-known bar (hatched: its value is not
    // today's) is not taken at the top, nor an unread one or one at the limit. With no such bar, the first bar that names an account; none that does, none (''). `family`, where
    // given, keeps the pick to the accounts of that family (the ones the card can be drawn for).
    function topAccount(g, family) {
        var bars = g ? barsOf(g) : [];
        var accounts = family && family.accounts ? family.accounts : null;
        var first = '';
        for (var i = 0; i < bars.length; i++) {
            var bar = bars[i];
            if (!bar || !bar.account) continue;
            if (accounts && !accountIn(accounts, bar.account)) continue;
            if (!first) first = bar.account;
            var known = bar.state !== 'unknown' && typeof bar.left === 'number' && isFinite(bar.left);
            if (known && bar.state !== 'last_known' && bar.left > 0 && !bar.at_limit && !(bar.restricted && heldNow(bar))) return bar.account;
        }
        return first;
    }

    function accountIn(accounts, key) {
        for (var i = 0; key && i < accounts.length; i++) if (accounts[i] && accounts[i].key === key) return accounts[i];
        return null;
    }

    // Which account is picked in the window on screen (`g`, the limit of the plate; `here`, its place as 'family|limit'), decided in renderReserve before the plate is built (barStrip reads
    // the pick), so the bar is open and red and the chart stands under it from the first drawing. The reader's own pick stays where its window is the one it was made in, or its account
    // has a bar in the new window; the widget's own stays while its window is the one it was made in and its account is still in the row. Else the top bar is picked (topAccount), unless
    // the reader let the pick go in this very window (pickBare). The poll moves nothing: its window is the same, so what stands stands. The keyboard is not touched: only the opener
    // is set, for Clear to give it back to the bar. Returns the card slot of the pick (the one given, a new one where the pick or its limit changed, none where nothing is picked).
    function pickDecide(family, g, here, card, view) {
        var moved = here !== pickAt;
        pickAt = here;
        if (pickBare && pickBare !== here) pickBare = '';
        var accounts = family && family.accounts ? family.accounts : [];
        var key = selectedAccountKey;
        var inRow = !!key && !!barOf(g, key);
        var keep = !!key && !!accountIn(accounts, key) && (pickAuto ? pickHome === here && inRow : pickHome === here || inRow);
        var rebuilt = false;
        if (keep) {
            // A pick made on a bar follows the reader to the window where its account has a bar too: the card stands under that plate.
            if (!pickAuto && inRow && selectionLimit && selectionLimit !== g.key) {
                selectionLimit = g.key;
                selectionOpener = 'bar:' + g.key + ':' + key;
                rebuilt = true;
            }
        } else {
            var next = pickBare === here ? '' : topAccount(g, family);
            rebuilt = !!next;
            // The details opened belong to the account the reader opened them on: another account, picked for the reader, starts shut.
            if (next !== selectedAccountKey) diagnosticsOpen = false;
            selectedAccountKey = next;
            pickAuto = !!next;
            selectionOpener = next ? 'bar:' + g.key + ':' + next : '';
            selectionLimit = next ? g.key : '';
            if (!next) card = null;
        }
        pickHome = selectedAccountKey ? here : '';
        if (rebuilt && selectedAccountKey) card = inspectorSlot(family, accountIn(accounts, selectedAccountKey), (view && view.facets) || {});
        // The line of the pick is drawn once for a pick that is new to the window on screen: at the opening, at a pick of a window or a family.
        if (moved && selectedAccountKey) {
            pickDue = true;
        }
        if (!selectedAccountKey) pickDue = false;
        return card;
    }

    function barPickWords(isSel) {
        return ' — ' + t(isSel ? 'pick.selected' : 'pick.select');
    }

    function rowPickWords(isOn) {
        return isOn ? ' · ' + t('pick.selected') : '';
    }

    function foldOf(kind, node, shut) {
        var wrap = el('div', kind + '-wrap' + (shut ? ' fold-run fold-shut' : ''));
        var fold = el('div', kind + '-fold');
        fold.appendChild(node);
        wrap.appendChild(fold);
        return wrap;
    }

    // The bars of a plate and the bar of them that is picked (.sel).
    function plateBars(plate) {
        return plate && typeof plate.querySelector === 'function' ? plate.querySelector('.q-bars') : null;
    }

    // The pin, the pointer and the focus all let go (another account, another window or family): the card is at rest.
    function cardReset() {
        clearTimeout(cardLeave.hover);
        clearTimeout(cardLeave.focus);
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

    // Nearby reset times form one ladder step; see ladderOf for display grouping.
    var RESET_CLUSTER_SEC = 120;
    // The figure of the plate, a number: its decimal point is the crimson ink, a span of its own (the same trick as the amount of a step of
    // the ladder), and the text of the node is the number whole. No point to colour (a dash, "<0.01", a word) and nothing is red.
    // A part that draws the same figure in its own classes (the chart's tiles) names them in `own` ({num, dot}); the plate's are the default.
    function figureNode(text, none, own) {
        var numClass = own ? own.num : 'num-strong wt-num';
        var found = none ? null : /^([^<\d]*\d+)\.(\d+)$/.exec(String(text));
        if (!found) return el('b', numClass + (none ? ' none' : ''), text);
        var node = el('b', numClass);
        node.appendChild(document.createTextNode(found[1]));
        node.appendChild(el('span', own ? own.dot : 'wt-dot', '.'));
        node.appendChild(document.createTextNode(found[2]));
        return node;
    }

    // A head in words, "Name · 3": a node whose text is the whole of it, as it was written (a reader and a copy have it so); the dot and what follows
    // the last " · " stand in a span of their own, which the stylesheet turns to the accent (.wt-head-rest), and the name keeps the ink of its part.
    function headNode(tag, cls, text) {
        var node = el(tag, cls);
        var words = String(text);
        var at = words.lastIndexOf(' · ');
        if (at < 0) {
            node.textContent = words;
            return node;
        }
        node.appendChild(document.createTextNode(words.slice(0, at)));
        node.appendChild(el('span', 'wt-head-rest', words.slice(at)));
        return node;
    }

    // A hint of one line for each account, to eight and then how many more; `note`,
    // when there is one, says what the kind of account means, after them.
    function hintOf(lines, note) {
        var shown = lines.slice(0, 8);
        if (lines.length > 8) shown.push(t('hint.more', { n: lines.length - 8 }));
        if (note) shown.push(note);
        return shown.join('\n');
    }

    // Hover, focus and touch are one thing on a handle (a legend item, the reset row, a group of the note under the window row): while the
    // reader is on it, the plate wears its kind (data-dim) and the stylesheet steps the bars of the other kinds back. The
    // pointer sets it on entering and drops it on leaving (keeping the one the keyboard holds);
    // the keyboard sets it while the ring shows (:focus-visible: the mouse's focus does not dim)
    // and drops it on leaving the item. A finger sets it when it lands and drops it when it lifts (a touch sets no :focus-visible and
    // sends no mouseleave; and the mouse events a touch makes come after its lift, so the ones within a moment of it are not the
    // pointer's: they would light the plate for good). It is the events that set it, not a :has() on :hover:
    // WebKit does not follow that one. It lives on the plate, which every redraw builds anew, so
    // none outlives the item it was set from (render() clears what a kept screen still wears). The plate is the list's parent, or, for an
    // item that stands elsewhere (a group of the note under the window row), the one `plateOf` returns.
    var TOUCH_MOUSE_MS = 800;
    function dimOnItem(li, list, kind, plateOf, accounts) {
        var held = false;
        var touched = 0;
        var ringed = function (node) {
            try { return node.matches(':focus-visible'); } catch (err) { return false; }
        };
        // An item may name the accounts it stands for (a group of the note under the row): the plate then dims to the group and the bars
        // of those accounts alone stay (litBars); the item keeps them in its data-accounts, for the sibling the keyboard still holds.
        var accountsOf = function (node) {
            try { return JSON.parse(node.getAttribute('data-accounts') || 'null'); } catch (err) { return null; }
        };
        var dim = function (to, names) {
            var plate = plateOf ? plateOf() : list.parentNode;
            if (!plate || !plate.setAttribute) return;
            if (to) plate.setAttribute('data-dim', names ? 'group' : to);
            else plate.removeAttribute('data-dim');
            if (plate.childNodes) litBars(plate, to ? names : null);
        };
        li.addEventListener('mouseenter', function () {
            if (Date.now() - touched > TOUCH_MOUSE_MS) dim(kind, accounts);
        });
        // Whether a node stands inside another (the walk up from it; a browser has contains, the tests' elements do not).
        var within = function (node, ancestor) {
            for (; node; node = node.parentNode) if (node === ancestor) return true;
            return false;
        };
        li.addEventListener('mouseleave', function () {
            // The keyboard may hold a handle of this list or of the same plate (the legend and the reset row are two lists of one plate):
            // the pointer leaving one gives the plate back to the one the keyboard holds, whichever of them it is.
            var on = document.activeElement;
            var plate = plateOf ? plateOf() : list.parentNode;
            var other = on && on.getAttribute && on.getAttribute('data-k') && (on.parentNode === list || (plate && within(on, plate)))
                && ringed(on) ? on : null;
            dim(other ? other.getAttribute('data-k') : '', other ? accountsOf(other) : null);
        });
        li.addEventListener('focus', function () {
            if (!ringed(li)) return;
            held = true;
            dim(kind, accounts);
        });
        li.addEventListener('blur', function () {
            if (!held) return;
            held = false;
            dim('');
        });
        li.addEventListener('pointerdown', function (e) {
            if (!e || e.pointerType !== 'touch') return;
            touched = Date.now();
            dim(kind, accounts);
        });
        var lifted = function (e) {
            if (!e || e.pointerType !== 'touch') return;
            touched = Date.now();
            dim('');
        };
        li.addEventListener('pointerup', lifted);
        li.addEventListener('pointercancel', lifted);
    }

    // The bars of the accounts named wear data-lit, the others do not (the stylesheet steps the others back while the plate is dimmed to a
    // group); no names (null) take it off every bar. The strip is the plate's child that wears .bars, found by walking its children.
    function litBars(plate, names) {
        var strip = plateBars(plate);
        if (!strip) return;
        for (var j = 0; j < strip.childNodes.length; j++) {
            var bar = strip.childNodes[j];
            var key = bar.getAttribute ? bar.getAttribute('data-account') : null;
            if (names && key && names.indexOf(key) >= 0) bar.setAttribute('data-lit', '1');
            else if (bar.removeAttribute) bar.removeAttribute('data-lit');
        }
    }

    // The words under a plate's bars (limitLegend): a list in the kit's own legend, an item for each state — its number and its
    // words (`extra`, a quiet value, after them), no drawing: the bars above are its sample. An item takes the keyboard (hover, focus
    // and touch are one thing: the bars of the other kinds step back, dimOnItem; an item that names its accounts lights theirs alone)
    // and keeps it through a redraw by its focus key. The number is bold and in no colour of its own: the red of the plate is the figure's
    // point and the bubble of the accounts at the limit, never a digit of the legend. Null when there are no states.
    function legendNode(items, limitKey) {
        if (!items.length) return null;
        var list = el('ul', 'calm-legend lg');
        items.forEach(function (item, i) {
            // Whitespace between the items: text reads "3 at the limit 1 disabled", not run together.
            if (i) list.appendChild(document.createTextNode(' '));
            var li = el('li', 'legend-item');
            li.setAttribute('tabindex', '0');
            li.setAttribute('data-k', item.k);
            li.setAttribute('data-focus', 'legend:' + limitKey + ':' + item.k + ':' + item.word);
            if (item.accounts) li.setAttribute('data-accounts', JSON.stringify(item.accounts));
            li.title = hintOf(item.lines, item.note);
            dimOnItem(li, list, item.k, null, item.accounts || null);
            li.appendChild(el('b', null, String(item.n)));
            li.appendChild(document.createTextNode(' ' + item.word + (item.extra ? ' · ' + item.extra : '')));
            list.appendChild(li);
        });
        return list;
    }

    // The reset under the legend (limitReset): cells in a row, the figure of each in bold and in no colour of its own. The dot between two
    // cells is the stylesheet's. `title` is the hint of the whole row; a cell may carry its own.
    function resetNode(cells, title) {
        var row = el('div', 'meta-line nx');
        if (title) row.title = title;
        cells.forEach(function (cell, i) {
            if (i) row.appendChild(document.createTextNode(' '));
            var part = el('span');
            if (cell.title) part.title = cell.title;
            if (cell.pre) part.appendChild(document.createTextNode(cell.pre));
            if (cell.b) part.appendChild(el('b', null, cell.b));
            if (cell.post) part.appendChild(document.createTextNode(cell.post));
            row.appendChild(part);
        });
        return row;
    }

    // What the picked window's figure stands on: of the accounts the window applies to (its slots), how many have a current reading, who
    // has not, and how old the oldest reading is. A limit of the engine says it in its own fields (measured, slots, bars, observed); the
    // billing-cycle budget (cycleBudget) gives its status in the same shape. Counted over accounts, not over limits.
    function windowReading(g) {
        var bars = barsOf(g);
        var unread = bars.filter(function (b) { return b.state !== 'current'; });
        var measured = g.measured && typeof g.measured.accounts === 'number' ? g.measured.accounts : bars.length - unread.length;
        var read = Math.max(0, measured);
        return { read: read, total: Math.max(slotCount(g), read), unread: unread, oldest: (g.observed && g.observed.oldest_at) || '' };
    }

    // Whether a window's figure rests on a part of the accounts it applies to: at least one is read and not all of them (the dash says "none"
    // by itself). The star on its tab is for it. The billing-cycle budget (cycleBudget) is told the same way: its tab's figure is the mean
    // share over the accounts read with a cap, and the accounts it could not read stand in the status the tab keeps.
    function windowIncomplete(g) {
        if (!g) return false;
        var r = windowReading(g.cycle ? g.status : g);
        var counted = g.cycle ? ((g.measured || {}).accounts || 0) : r.read;
        return counted >= 1 && r.read < r.total;
    }

    // Whether the read the section stands on failed, and when the last good one was: the whole status (a read that failed, or a screen
    // kept after one) or the quota facet alone. Then the stamp says so, the figures are dashes and nothing is told of who is not read:
    // all of them are not. null while the reading is the plain one.
    function readFailure(view, summary) {
        var cached = (view && view.cached) || summary.cached || {};
        var cachedAt = cached.quota || cached.accounts || cached.catalog || '';
        if (view && (view.transport_error || view.kept) && cachedAt) return { status: true, at: cachedAt };
        if (cached.quota) return { status: false, at: cached.quota };
        return null;
    }

    // The stamp across from the windows: of the picked window, how many accounts are read and how old the oldest reading is ("Read 14 of 16 ·
    // 18 min ago"). Quiet: the meta ink, no lamp and no colour of a state; the number read is darker when it is not all of them, and the age
    // when it is old. Where the read itself failed it says so, in the ink. `g` is the picked window (null: no window at all).
    function windowStamp(view, summary, g) {
        var failure = readFailure(view, summary);
        var stamp = el('span', 'meta-line reserve-age');
        if (failure) {
            stamp.className += ' is-ink';
            stamp.textContent = t(failure.status ? 'status.unread' : 'status.quota_unread', { when: relTime(failure.at) || clockAt(failure.at) });
            stamp.title = t(failure.status ? 'stamp.title_failed' : 'stamp.title_quota');
            return stamp;
        }
        var age = readingAge(summary, g ? [g] : []);
        if (!g || !barsOf(g).length) {
            stamp.textContent = age.text;
            stamp.title = t('stamp.title_none');
            return stamp;
        }
        var r = windowReading(g);
        // One line: the word, the count (the ink, the semibold weight), the dot and the age.
        stamp.appendChild(el('span', 'wt-read', t('stamp.read_word')));
        // (a space between the word and the count: the blocks of the rail ignore it, a line on the narrow shell and a copy of the text keep it)
        stamp.appendChild(document.createTextNode(' '));
        stamp.appendChild(el('span', r.read < r.total ? 'wt-n lack' : 'wt-n', t('stamp.read_of', { n: r.read, m: r.total })));
        var when = r.oldest ? agoWords(r.oldest) : '';
        if (when) {
            var dot = el('span', refreshing ? 'wt-sep is-busy' : 'wt-sep');
            // The dot is a drawing (kit: capsule: window tabs, the stamp): the sign it stood for stays in it for a reader, unseen.
            dot.appendChild(el('span', 'sr-only', ' · '));
            stamp.appendChild(dot);
            stamp.appendChild(el('span', age.old ? 'wt-age is-old' : 'wt-age', when));
        }
        stamp.title = t('stamp.title', { n: r.read, m: r.total, observed: age.inline });
        return stamp;
    }

    // Why an account of a window has no current reading, in the words of the note under the row; a reason the widget has no words for is
    // said as "not read".
    var FOOT_WHY = {
        not_read: 'foot.why_not_read',
        unreadable: 'foot.why_unreadable',
        sources_disagree: 'foot.why_sources_disagree',
        reset_passed: 'foot.why_reset_passed',
        too_old: 'foot.why_too_old',
        no_reading_in_answer: 'foot.why_no_reading_in_answer'
    };
    var FOOT_NAMES = 2;

    // The accounts of a window that have no current reading, in groups: those read before (last known) first, then those with none, a group
    // for each reason. `k` is the kind of bar the group's accounts are drawn with (last, unknown): what the plate dims to.
    function footGroups(unread) {
        var order = ['last_known'].concat(Object.keys(FOOT_WHY));
        var groups = [];
        unread.forEach(function (b) {
            var kind = b.state === 'last_known' ? 'last_known' : (FOOT_WHY[b.why] ? b.why : 'not_read');
            var group = groups.filter(function (x) { return x.kind === kind; })[0];
            if (!group) {
                group = { kind: kind, k: kind === 'last_known' ? 'last' : 'unknown', bars: [] };
                groups.push(group);
            }
            group.bars.push(b);
        });
        return groups.sort(function (a, b) { return order.indexOf(a.kind) - order.indexOf(b.kind); });
    }

    // The plate of the picked window, which the note's groups dim by kind, as a legend item does: found from a node of the section both stand
    // in (the note), by walking the tree, which a page and the test's fake page both have.
    function platePanel(from) {
        var section = from;
        while (section && !hasAnyClass(section, ['reserve', 'shell'])) section = section.parentNode;
        var found = null;
        (function visit(node) {
            for (var i = 0; i < node.childNodes.length && !found; i++) {
                var child = node.childNodes[i];
                if (hasClass(child, 'q-plate')) found = child;
                else visit(child);
            }
        })(section || from);
        return found;
    }

    // The note over the plate, for the picked window that has accounts with no current reading: "* Not read · 3 last known (3 h) —
    // claude5, claude8 · unreadable — claude12" (the head counts them). The star is there when the picked tab wears it; the groups take the keyboard (a group, with the
    // words and the names as its name) and dim the plate's bars but their own; a group of more than FOOT_NAMES names says the first and then
    // "N more", which opens the account list. Null when every account is read. The last readings are dated by their bars, or, where the
    // bars do not date them (the billing-cycle budget's), by the window's own oldest last-known reading. `hold` draws the same note as a
    // copy that is only a place (hidden from the eye by the stylesheet and from a reader by aria-hidden): no id, no role, nothing to
    // focus and nothing that answers a pointer; the row keeps it where another window has the star and the picked one none.
    function windowFoot(g, index, starred, hold) {
        var unread = windowReading(g).unread;
        if (!unread.length) return null;
        var foot = el('div', 'meta-line wt-foot');
        if (hold) {
            foot.className += ' wt-foot-hold';
            foot.setAttribute('aria-hidden', 'true');
        } else {
            foot.setAttribute('role', 'note');
            foot.setAttribute('id', 'quotas-foot');
        }
        if (starred) {
            var star = el('span', 'wt-foot-star', '*');
            star.setAttribute('aria-hidden', 'true');
            foot.appendChild(star);
            foot.appendChild(document.createTextNode(' '));
        }
        // The head of the note counts the accounts it names: "Not read · 2", then the reasons and the names.
        foot.appendChild(headNode('span', 'wt-foot-head', t('foot.not_read', { n: unread.length })));
        foot.appendChild(document.createTextNode(' '));
        footGroups(unread).forEach(function (group, i) {
            var words;
            if (group.kind === 'last_known') {
                var dated = group.bars.map(function (b) { return b.observed_at; }).filter(Boolean).sort();
                var since = dated[0] || (g.last_known && g.last_known.oldest_observed_at) || '';
                var oldest = agePart(since);
                // The age is the oldest reading's. Read within the last two minutes it is "just now" (never "just now ago"); accounts read at ages that
                // are said differently make it "up to" that one.
                var seenAges = {};
                dated.forEach(function (iso) { seenAges[ageWords(iso)] = true; });
                if (!oldest) words = t('state.last_known');
                else if (oldest.k === 'now') words = t('foot.read_just_now');
                else words = t(Object.keys(seenAges).length > 1 ? 'foot.read_up_to' : 'foot.read_ago', { age: agePhrase(oldest) });
            } else {
                words = t(FOOT_WHY[group.kind]);
            }
            var span = el('span', 'wt-foot-g');
            span.setAttribute('data-k', group.k);
            // (the dot before a second group stands inside the group: a box, so it never opens a line alone)
            span.appendChild(document.createTextNode((i ? '· ' : '') + words + ' · '));
            var named = group.bars.filter(function (b) { return !!b.account; });
            // Few accounts are named; more are counted (the hint names them all, and the group lights their bars).
            var shown = named.length <= FOOT_NAMES && named.length === group.bars.length ? named : [];
            var said = shown.map(function (b) { return accountName(index, b.account); });
            // The comma between names stands inside the name before it (a name is a box, and a line may break before a box, so a comma
            // outside would open a line): "name," then a space, and the last name has none.
            said.forEach(function (name, j) {
                if (j) span.appendChild(document.createTextNode(' '));
                span.appendChild(el('span', 'wt-who', j < said.length - 1 ? name + ',' : name));
            });
            if (!shown.length) {
                said = [plural(group.bars.length, 'account')];
                span.appendChild(el('span', 'wt-who', said[0]));
            }
            if (!hold) {
                span.setAttribute('tabindex', '0');
                span.setAttribute('role', 'group');
                span.setAttribute('aria-label', words + ' — ' + said.join(', '));
                span.setAttribute('data-focus', 'foot:' + group.kind);
                // The plate dims to the group's own accounts when the engine named every one of them; else to the kind of bar they are.
                var own = named.length === group.bars.length ? group.bars.map(function (b) { return b.account; }) : null;
                if (own) span.setAttribute('data-accounts', JSON.stringify(own));
                dimOnItem(span, foot, group.k, function () { return platePanel(foot); }, own);
            }
            if (i) foot.appendChild(document.createTextNode(' '));
            foot.appendChild(span);
        });
        return foot;
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
        return t('cover.some');
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
        var text = credits.observed_at ? t('cooldown.observed', { when: whenWords(credits.observed_at) }) : t('credits.time_unknown');
        if (credits.source) text += ' · ' + t('credits.source', { source: credits.source });
        if (credits.label) text += ' · ' + t('credits.reported', { label: credits.label });
        return text + '. ' + t('credits.no_limit');
    }

    // The family's manual-reset credits as a node (the plate takes it under its notes), or null when the family reports none.
    function familyCreditsNode(family) {
        var c = family && family.reset_credits;
        if (!c || !(c.current_accounts || c.last_known_accounts || c.unreadable_accounts || c.conflict_accounts)) return null;
        var row = el('div', 'reset-credits family-credits');
        // The head carries the count of the credits reported now ("Manual resets · 2"; with none reported now, the head alone); the parts that
        // follow say on how many accounts and what is not counted in it (last known, unreadable, in conflict, unknown).
        row.appendChild(headNode('strong', 'emph', c.current_accounts && creditCount(c.count)
            ? t('credits.family_n', { count: String(c.count) }) : t('credits.family')));
        var parts = [];
        if (c.current_accounts && creditCount(c.count)) {
            parts.push(t('credits.on_accounts', { n: c.current_accounts }));
        }
        if (c.last_known_accounts && creditCount(c.last_known_count)) {
            var age = agoWords(c.last_known_oldest_observed_at);
            parts.push(age
                ? t('credits.last_known', { count: c.last_known_count, accounts: plural(c.last_known_accounts, 'account'), age: age })
                : t('credits.last_known_noage', { count: c.last_known_count, accounts: plural(c.last_known_accounts, 'account') }));
        }
        if (c.unreadable_accounts) parts.push(t('credits.unreadable', { n: c.unreadable_accounts }));
        if (c.conflict_accounts) parts.push(t('credits.conflict', { n: c.conflict_accounts }));
        if (c.unknown_accounts) parts.push(t('credits.unknown', { n: c.unknown_accounts }));
        row.appendChild(document.createTextNode(' ' + (parts.join(' · ') || t('credits.not_reported'))));
        row.title = 'Provider-reported manual reset credits, separate from automatic resets. Unknown, unreadable '
            + 'and last-known counts are not included in the current count. No refill amount or currency is assumed.';
        return row;
    }

    // The same, in a section that has no plate to hold it.
    function renderFamilyCredits(parent, family) {
        var row = familyCreditsNode(family);
        if (row) parent.appendChild(row);
    }

    // The engine's resource facets of one account (balances, spending, manual
    // reset offers), each as it dated them: current, last known (dated, never
    // current) or not reported (never 0). A team figure says it is the team's,
    // a zero included amount says it is not a block, and a reset offer is
    // information only — nothing here spends it.
    // An amount as the engine sent it: a decimal string, never a float. In
    // unit "minor" it is the currency's smallest unit and decimal_places says
    // the scale (2345678 with 2 places is 23,456.78): the point is placed in
    // the digits themselves, so no value is rounded or lost. Without a scale
    // the minor figure is shown as it is and said to be unscaled; any other unit
    // is shown as sent. Returns '' for an amount not reported — never 0.
    function moneyWords(raw, item) {
        if (raw === null || raw === undefined) return '';
        var text = String(raw).trim();
        var cur = item.currency || '';
        var unit = item.unit || '';
        var parts = /^(-?)(\d+)$/.exec(text);
        if (unit === 'minor') {
            if (!parts || typeof item.decimal_places !== 'number' || item.decimal_places % 1 !== 0
                    || item.decimal_places < 0 || item.decimal_places > 12) {
                return t('money.unscaled', { amount: text + (cur ? ' ' + cur : ''), scale: item.decimal_places === null || item.decimal_places === undefined ? t('money.scale_unknown') : t('money.scale_raw', { n: item.decimal_places }) });
            }
            var places = item.decimal_places;
            var digits = parts[2].length > places ? parts[2] : new Array(places - parts[2].length + 2).join('0') + parts[2];
            var whole = digits.slice(0, digits.length - places).replace(/\B(?=(\d{3})+(?!\d))/g, ',');
            var frac = places ? '.' + digits.slice(digits.length - places) : '';
            return parts[1] + whole + frac + (cur ? ' ' + cur : '');
        }
        // A unit this widget has no scale rule for (an older engine's "cents", or none): the figure as
        // sent, with the unit, currency and scale as sent — never read as a major-unit amount.
        var said = [unit || t('money.unit_unknown')];
        if (cur && cur.toLowerCase() !== unit.toLowerCase()) said.push(cur);
        if (item.decimal_places !== null && item.decimal_places !== undefined) said.push(t('money.places', { n: item.decimal_places }));
        return text + ' ' + said.join(' · ');
    }

    // The words the engine names its rows with, in English: the ones known go through the table, the rest stand as they came (the same rule as VERIFY_WORDS).
    var ENGINE_WORDS = {
        'Personal included remaining': 'eng.personal_included_remaining',
        'Personal on-demand remaining': 'eng.personal_on_demand_remaining',
        'Personal included usage': 'eng.personal_included_usage',
        'Personal on-demand spending': 'eng.personal_on_demand_spending',
        'Personal bonus usage': 'eng.personal_bonus_usage',
        'Personal total usage': 'eng.personal_total_usage',
        'Granted reset': 'eng.granted_reset',
        'No quota window reported for this account': 'eng.no_quota_window',
        'fee treatment not stated': 'eng.fee_not_stated',
        'Internal dashboard schema may change; numeric resources do not establish execution availability': 'eng.dashboard_schema',
        'The aggregate source does not identify fee inclusion': 'eng.fee_inclusion_unknown'
    };
    var USAGE_KINDS = { included: 'res.kind_included', on_demand: 'res.kind_on_demand' };

    function engineWord(text) {
        return hasOwn(ENGINE_WORDS, text) ? t(ENGINE_WORDS[text]) : text;
    }

    function renderAccountResources(parent, resources) {
        if (!resources) return;
        var box = el('div', 'account-resources');
        box.setAttribute('role', 'group');
        box.setAttribute('aria-label', t('res.label'));
        var any = false;
        ['balances', 'spending', 'resets', 'diagnostics'].forEach(function (name) {
            var facet = resources[name];
            if (!facet || (name === 'diagnostics' && !(facet.rows || []).length)) return;
            any = true;
            var row = el('div', 'reset-credits');
            row.appendChild(el('strong', 'emph', t('res.' + name)));
            box.appendChild(row);
            if (facet.state === 'not_reported') {
                row.appendChild(document.createTextNode(' · ' + t('res.not_reported')));
            } else {
                var kept = facet.state === 'last_known';
                var said = [];
                if (kept) said.push(t('res.last_known'));
                if (facet.observed_at) said.push(t('res.observed', { when: whenWords(facet.observed_at) }));
                if (said.length) row.appendChild(document.createTextNode(' · ' + said.join(' · ') + (kept ? ', ' + t('res.not_current') : '')));
                (facet.rows || []).forEach(function (item) {
                    var text;
                    var label = engineWord(String(item.label || ''));
                    if (name === 'balances') {
                        text = label + ': ' + (item.unlimited ? t('cycle.unlimited') : item.amount === null ? t('res.amount_unknown')
                            : moneyWords(item.amount, item)) + (item.usage_kind ? ' ('
                            + (hasOwn(USAGE_KINDS, item.usage_kind) ? t(USAGE_KINDS[item.usage_kind]) : item.usage_kind.replace('_', '-')) + ')' : '');
                    } else if (name === 'spending') {
                        text = label + ': ' + (item.used === null ? t('res.not_reported') : moneyWords(item.used, item))
                            + (item.limit ? ' ' + t('res.of', { limit: moneyWords(item.limit, item) }) : '')
                            + (item.enabled === false ? ' (' + t('res.off') + ')' : '');
                    } else if (name === 'resets') {
                        // A count is "usable now" only where the engine says so; else it is granted, not usable now or of unknown usability (never "available").
                        text = label + ': ' + offerWords(item).v;
                    } else {
                        text = item.code + (item.detail ? ' — ' + engineWord(String(item.detail)) : '');
                    }
                    var line = el('div', 'meta-line tl-scope', text + (item.note ? ' · ' + engineWord(String(item.note)) : ''));
                    box.appendChild(line);
                });
            }
        });
        if (any) parent.appendChild(box);
    }

    function renderAccountCredits(parent, quota) {
        var c = quota && quota.reset_credits;
        if (!c || c.reason === 'not_reported') return;
        var row = el('div', 'reset-credits account-credits');
        row.appendChild(el('strong', 'emph', t('credits.title')));
        var text;
        if ((c.state === 'current' || c.state === 'last_known') && creditCount(c.count)) {
            var lastKnown = c.state === 'last_known';
            text = c.observed_at
                ? t(lastKnown ? 'credits.acc_lk_obs' : 'credits.acc_cur_obs', { count: c.count, when: relTime(c.observed_at) || whenWords(c.observed_at) })
                : t(lastKnown ? 'credits.acc_lk_unk' : 'credits.acc_cur_unk', { count: c.count });
        } else if (c.state === 'unreadable') {
            text = c.label ? t('credits.acc_unreadable_label', { label: c.label }) : t('credits.acc_unreadable');
        } else if (c.state === 'conflict') {
            text = t('credits.acc_conflict');
        } else {
            text = c.reason === 'not_reported' ? t('credits.not_reported') : t('credits.acc_unknown');
        }
        row.appendChild(document.createTextNode(' · ' + text));
        row.title = creditProvenance(c);
        parent.appendChild(row);
    }

    /* ------------------------------------------------------------------
       The window row (capsule: window tabs).

       Three places, in one order — 5-hour, Weekly, 30-day — filled by length
       from the windows the skill reports and by nothing else; a window of
       another length stands as one more tab at the end. A family has a tab
       only for a window it reports: a place it has no window for is not drawn
       (no grey place, no hint). A window the reader hid in Rows is the
       family's still, and keeps a dotted tab that says so. A family with no
       window at all has no rail, except one that has a budget per billing
       cycle (Cursor): it has one tab, the 30-day place, on that budget
       (below).
       Under the row stands the plate of the window picked: the limit
       row of the stack, once. The pick is the one the plate's chart control
       keeps (chartKeys), so a tab, a scope and the graph under the plate are
       one state.
       ------------------------------------------------------------------ */

    var WINDOW_PLACES = [18000, 604800, 2592000];

    function reducedMotion() {
        return mediaIs('(prefers-reduced-motion: reduce)');
    }

    // The family as its chip says it: "Claude Code" is "Claude".
    function shortFamily(group) {
        return familyName(group).replace(/ (?:CLI|Code)$/, '');
    }

    // The short name of a window length for the rail: a count of days, hours or minutes, the largest of the three that holds it whole (a week
    // is 7 days): "5H", "7D", "30D" in English, "5ч", "7д", "30д" in Russian. The full name (lengthName) stays in the tab's label and title.
    function shortLength(seconds) {
        if (typeof seconds !== 'number' || !(seconds > 0)) return t('win.short.none');
        if (seconds % 86400 === 0) return t('win.short.d', { n: seconds / 86400 });
        if (seconds % 3600 === 0) return t('win.short.h', { n: seconds / 3600 });
        if (seconds % 60 === 0) return t('win.short.m', { n: seconds / 60 });
        return t('win.short.none');
    }

    // The tabs of a family's limits: the three places, then any other length.
    // A tab holds its limits, the one with no scope of its own ("All models")
    // first, the others by the name the scope row shows. A limit the reader hid
    // in Rows is counted, not drawn: its place says so.
    function windowTabs(groups, hidden, names) {
        var byLength = {};
        var order = [];
        groups.forEach(function (g) {
            var seconds = typeof g.window_seconds === 'number' && g.window_seconds > 0 ? g.window_seconds : 0;
            if (!byLength[seconds]) {
                byLength[seconds] = { seconds: seconds, name: lengthName(seconds), groups: [], hidden: 0 };
                order.push(seconds);
            }
            if (hidden[g.key]) byLength[seconds].hidden++;
            else byLength[seconds].groups.push(g);
        });
        var tabs = WINDOW_PLACES.map(function (seconds) {
            return byLength[seconds] || { seconds: seconds, name: lengthName(seconds), groups: [], hidden: 0 };
        });
        order.filter(function (seconds) {
            return WINDOW_PLACES.indexOf(seconds) < 0 && byLength[seconds].groups.length;
        }).sort(function (a, b) { return (a || Infinity) - (b || Infinity); }).forEach(function (seconds) {
            tabs.push(byLength[seconds]);
        });
        tabs.forEach(function (tab) {
            var general = tab.groups.filter(function (g) { return names[g.key] === tab.name; });
            var scoped = tab.groups.filter(function (g) { return general.indexOf(g) < 0; });
            var label = function (g) { return scopeLabel(g, names, tab); };
            scoped.sort(function (a, b) {
                return compareText(label(a).toLowerCase(), label(b).toLowerCase())
                    || compareText(label(a), label(b)) || compareText(a.key || '', b.key || '');
            });
            tab.groups = general.concat(scoped);
        });
        return tabs;
    }

    // What a scope option says: the limit's own tag, as its short name carries
    // it (limitTag); the limit with none is the whole family's, "All models".
    function scopeLabel(g, names, tab) {
        var full = names[g.key] || '';
        if (full === tab.name) return t('scope.all_models');
        return full.indexOf(tab.name + ' · ') === 0 ? full.slice(tab.name.length + 3) : full;
    }

    // The number a tab carries: the average share left of the window. It is the tab's alone: the plate under it does not say it
    // again — "—" with no current reading, never a 0.
    function tabAverage(g) {
        var m = g.measured || {};
        var pct = m.accounts ? leftPct(m.average_remaining_pct) : '';
        return pct ? pct + '%' : '—';
    }

    /* ------------------------------------------------------------------
       The billing-cycle budget (capsule: window tabs).

       A family the engine reports no time window for — Cursor — still reports
       money: what each account's included usage has left of its billing cycle
       (resources, balances and spending, in minor units of a currency). The
       30-day place of its window row opens on that budget and says what it is,
       an allowance per cycle and not a window of time (the tab's hint); it is
       the one tab of the row, 5-hour and Weekly are not drawn. These are the
       only figures of the screen that are the widget's own: an account's share
       is what it has left ÷ what its cycle allows (the engine's two numbers),
       the plate's figure is the sum of those amounts over the accounts read
       now, the tab's number the mean of the shares. Money is added only within
       one currency and scale; a team pool is never an account's own; an
       unlimited pool has no share; a last-known
       reading is dated and never counted; an amount not reported is unknown,
       never 0. The engine says these figures do not establish that a request can
       run, so no tone, status or alert is set from them. It keeps no history of
       money, so there is no graph, and the plate says so.
       ------------------------------------------------------------------ */

    var CYCLE_PLACE = WINDOW_PLACES[2];
    var CURRENCY_MARKS = Object.assign(Object.create(null), { USD: '$', EUR: '€', GBP: '£' });

    // An amount as the engine sends it, a non-negative decimal string, as a
    // number of its unit; anything else is not an amount.
    function minorOf(raw) {
        if (typeof raw !== 'string' || !/^(?:0|[1-9]\d*)(?:\.\d+)?$/.test(raw)) return null;
        var n = Number(raw);
        return isFinite(n) ? n : null;
    }

    // The scale a money row states, or null when this widget has no rule for it
    // (moneyWords says such a figure as sent, never as a major-unit amount).
    function moneyScale(item) {
        var places = item.decimal_places;
        if (item.unit !== 'minor' || !item.currency || typeof places !== 'number' || places % 1 !== 0
                || places < 0 || places > 6) return null;
        return { currency: item.currency, places: places };
    }

    function sameScale(a, b) {
        return !!a && !!b && a.currency === b.currency && a.places === b.places;
    }

    // Money for a figure: "$16.40". The number is the minor-unit amount in the
    // scale the engine states; a currency with no mark here keeps its code.
    function moneyShort(minor, scale) {
        var parts = (minor / Math.pow(10, scale.places)).toFixed(scale.places).split('.');
        parts[0] = parts[0].replace(/\B(?=(\d{3})+(?!\d))/g, ',');
        var text = parts.join('.');
        return CURRENCY_MARKS[scale.currency] ? CURRENCY_MARKS[scale.currency] + text : text + ' ' + scale.currency;
    }

    // The personal row of one usage kind in a resource facet, and whether the
    // facet and the row are current. A team pool is the team's, never an account's own.
    function usageRow(facet, kind) {
        var rows = facet && facet.rows;
        if (!Array.isArray(rows)) return null;
        for (var i = 0; i < rows.length; i++) {
            var row = rows[i];
            if (row && row.scope === 'personal' && row.usage_kind === kind) {
                return { row: row, current: facet.state === 'current' && row.state !== 'last_known' };
            }
        }
        return null;
    }

    // What one account's resources say of its included usage in the billing
    // cycle; null when they say nothing of it. `state`: current (counted),
    // last_known (dated, never counted), unlimited (no share) or unknown (a pool
    // is named, but no share can be read from it).
    function cycleOf(account) {
        var res = account && account.resources;
        if (!res) return null;
        var bal = usageRow(res.balances, 'included');
        var spend = usageRow(res.spending, 'included');
        if (!bal && !spend) return null;
        // A screen the widget keeps because the last read failed is nothing current.
        var kept = !!(currentView && currentView.kept);
        var scale = moneyScale((bal || spend).row);
        if (bal && spend && !sameScale(moneyScale(bal.row), moneyScale(spend.row))) scale = null;
        var left = bal && scale ? minorOf(bal.row.amount) : null;
        var limit = spend && scale ? minorOf(spend.row.limit) : null;
        var seen = [bal ? res.balances.observed_at : '', spend ? res.spending.observed_at : ''].filter(Boolean)
            .sort(function (a, b) { return Date.parse(a) - Date.parse(b); });
        var ends = function (r) { return (r && ((r.row.period && r.row.period.ends_at) || r.row.resets_at)) || null; };
        var out = { key: account.key, label: String(account.label || account.key), scale: scale, left: left,
                    limit: limit, share: null, state: 'unknown', observed_at: seen[0] || null,
                    ends_at: ends(bal) || ends(spend), demand: null };
        if (bal && bal.row.unlimited === true) {
            out.state = 'unlimited';
            out.last_known = kept || !bal.current;
        } else if (left !== null && limit !== null && limit > 0) {
            out.share = Math.max(0, Math.min(1, left / limit));
            out.state = !kept && bal.current && spend.current ? 'current' : 'last_known';
        }
        // On-demand spending stands beside the included usage, never inside it.
        var demand = usageRow(res.spending, 'on_demand');
        if (demand && demand.current && !kept && scale && sameScale(moneyScale(demand.row), scale)) {
            out.demand = minorOf(demand.row.used);
        }
        return out;
    }

    // One account's bar on the plate: its share left, with its money in the words.
    function budgetBar(c) {
        if (c.state === 'unlimited') {
            return { account: c.key, state: 'unlimited', left: null, last_known: !!c.last_known,
                     observed_at: c.observed_at, resets_at: c.ends_at,
                     words: t('budget.unlimited_words', { name: c.label }) };
        }
        if (c.state === 'unknown') return { account: c.key, state: 'unknown', left: null, why: 'not_read' };
        var ends = c.ends_at ? whenWords(c.ends_at) : '';
        var words = t(c.state === 'last_known' ? 'budget.share_last' : 'budget.share', { name: c.label, pct: exactPct(c.share * 100, c.share <= 0), left: moneyShort(c.left, c.scale), limit: moneyShort(c.limit, c.scale) })
            + (ends ? ' · ' + t('budget.ends', { when: ends }) : '')
            + (c.state === 'last_known' ? ' · ' + t('budget.last_read', { when: relTime(c.observed_at) || t('reading.time_unknown') })
                : (c.observed_at ? ' · ' + t('budget.read', { when: relTime(c.observed_at) }) : ''));
        // Never at the limit: an included allowance used up is not a block (the pay-as-you-go spend
        // goes on), and the engine says its figures do not decide whether a request can run.
        return { account: c.key, state: c.state, left: c.share, at_limit: false, restricted: false,
                 observed_at: c.observed_at, resets_at: c.ends_at, words: words };
    }

    // The billing-cycle budget of a family with no time window, or null when no
    // account reports an included pool (then the family has no window to draw and no rail). One bar
    // per account that has a pool, or is switched on and has none read (unknown).
    function cycleBudget(family, hid, famName) {
        var reads = [];
        ((family && family.accounts) || []).forEach(function (a) {
            if (!a || !a.key) return;
            var c = cycleOf(a);
            if (c) {
                reads.push(c);
            } else if (a.enabled !== false) {
                reads.push({ key: a.key, label: String(a.label || a.key), scale: null, share: null, state: 'unknown',
                             observed_at: null, ends_at: null, demand: null });
            }
        });
        if (!reads.some(function (c) { return c.state !== 'unknown'; })) return null;
        // One scale for the sum: the first account's. An account in another one is not added.
        var scale = null;
        reads.forEach(function (c) {
            if (!scale && c.scale && (c.state === 'current' || (c.state === 'unlimited' && !c.last_known))) scale = c.scale;
        });
        reads.forEach(function (c) {
            if (c.state === 'current' && !sameScale(c.scale, scale)) {
                c.state = 'unknown';
                c.share = null;
            }
        });
        var by = function (state) { return reads.filter(function (c) { return c.state === state; }); };
        var current = by('current');
        var unlimited = by('unlimited').filter(function (c) { return !c.last_known; });
        var last = by('last_known').concat(by('unlimited').filter(function (c) { return c.last_known; }));
        var unknown = by('unknown');
        var left = 0;
        var limit = 0;
        var mean = 0;
        current.forEach(function (c) {
            left += c.left;
            limit += c.limit;
            mean += c.share;
        });
        var avg = current.length ? mean / current.length : null;
        // Fullest first, as the windows' bars are; the last known after, then unlimited, then unknown.
        var rank = { current: 0, last_known: 1, unlimited: 2, unknown: 3 };
        var bars = reads.map(function (c, i) { return { c: c, i: i }; }).sort(function (a, b) {
            return (rank[a.c.state] - rank[b.c.state]) || ((b.c.share || 0) - (a.c.share || 0)) || (a.i - b.i);
        }).map(function (x) { return budgetBar(x.c); });

        // Under the bars, as under a window's: the accounts not counted, each kind with its number
        // (the legend; it has nothing to say while every account is counted, and a budget is never
        // at the limit or held; an account with no usable reading has no item: its bar is a "?" and the
        // note under the windows names it), then the cycle and its reset (the reset row). The accounts are named
        // in the hint of the kind they are in, not beside its number.
        var legend = [];
        if (unlimited.length) {
            legend.push({ k: 'unlimited', n: unlimited.length, word: t('cycle.unlimited'),
                lines: unlimited.map(function (c) { return t('budget.unlimited_name', { name: c.label }); }),
                note: t('budget.unlimited_note') });
        }
        if (last.length) {
            legend.push({ k: 'last', n: last.length, word: t('cycle.last_not_counted'),
                lines: last.map(function (c) {
                    return t('reading.last_name', { name: c.label, when: relTime(c.observed_at) || t('reading.time_unknown') });
                }),
                note: t('budget.last_note') });
        }
        var reset = [{ pre: t('cycle.billing_cycle'),
                       title: t('budget.renewal') }];
        var soonest = null;
        var same = 0;
        current.forEach(function (c) {
            var at = Date.parse(c.ends_at || '');
            if (!isFinite(at) || at <= Date.now()) return;
            if (soonest === null || at < soonest) {
                soonest = at;
                same = 1;
            } else if (at === soonest) {
                same++;
            }
        });
        if (soonest !== null) {
            var endsHint = t('budget.soonest');
            var soonWhen = whenWords(new Date(soonest).toISOString());
            reset.push(Object.assign(boldAt(t('reset.next', { when: soonWhen }), soonWhen), { title: endsHint }));
            if (same > 1) reset.push(Object.assign(boldAt(t('reset.accounts', { n: same }), String(same)), { title: endsHint }));
        }
        var demand = 0;
        var payers = 0;
        reads.forEach(function (c) {
            if ((c.state === 'current' || c.state === 'unlimited') && typeof c.demand === 'number' && sameScale(c.scale, scale)) {
                demand += c.demand;
                payers++;
            }
        });
        // What the accounts spent beyond the plan this cycle (on demand): the plate's bubble says it (cyclePlate), not a cell of the reset row.
        var onDemand = null;
        if (scale && demand > 0) {
            onDemand = { amount: moneyShort(demand, scale),
                         title: t('budget.demand_title', { accounts: plural(payers, 'account') }) };
        }

        // What the status line of the section reads, as it does from a window's bars.
        var times = current.concat(unlimited).map(function (c) { return c.observed_at; }).filter(Boolean).sort();
        var lastTimes = last.map(function (c) { return c.observed_at; }).filter(Boolean).sort();
        var key = 'cycle:' + hid;
        var status = {
            bars: reads.map(function (c) { return { account: c.key, state: c.state === 'unlimited' ? (c.last_known ? 'last_known' : 'current') : c.state }; }),
            observed: { newest_at: times[times.length - 1] || '', oldest_at: times[0] || '' },
            last_known: { oldest_observed_at: lastTimes[0] || '' }
        };
        return {
            key: key, name: lengthName(CYCLE_PLACE), note: t('cycle.note', { family: famName }), scale: scale,
            counted: current.length, left: left, limit: limit, avg: avg,
            unlimited: unlimited, lastKnown: last.length, unknown: unknown.length,
            bars: bars, legend: legend, reset: reset, onDemand: onDemand, status: status,
            // One more column of the account list, in a window's own shape.
            group: { key: key, bars: bars },
            // What the 30-day tab reads, in a window's own shape: the mean share on its face.
            tab: { cycle: true, key: key, harness: hid, note: t('cycle.note', { family: famName }), status: status,
                   measured: { accounts: current.length, average_remaining_pct: avg === null ? null : avg * 100 } }
        };
    }

    // Cursor reports its included usage per billing cycle and no time window; the
    // engine's way of saying so is a standing "no source" for every healthy account.
    // With the budget current that is not a fault, and it is not drawn as one.
    function budgetOnly(account) {
        var quota = (account && account.quota) || {};
        if (!quota.absence || quota.absence.action_kind !== 'source_missing' || (quota.constraints || []).length) return false;
        var c = cycleOf(account);
        return !!c && (c.state === 'current' || (c.state === 'unlimited' && !c.last_known));
    }

    // Where a window's timeline stands, the budget says there is none.
    function cycleNote() {
        var box = el('div', 'tl-block');
        var note = el('div', 'meta-line reserve-note', t('cycle.no_timeline'));
        note.title = t('budget.no_history');
        box.appendChild(note);
        return box;
    }

    // The reader's own pick of a window or a scope: the state the plate's
    // chart control keeps, so there is one, not two. It is answered with motion,
    // and it takes the graph back to this window from the combined view (that
    // choice is the graph's own: graphMode, which the window row never holds).
    function pickWindow(g) {
        // The pick of another limit moves the ladder of resets (it stands on the window picked); a press on the one that stands moves nothing.
        // The ladder of another limit starts with no step lit and its few steps (qLit, qLadderAll).
        if (chartKeys[g.harness] !== g.key) {
            qLit = null;
            qLadderAll = false;
        }
        if (chartKeys[g.harness] !== g.key) scheduleAll = false;
        chartKeys[g.harness] = g.key;
        graphMode = 'window';
        chartAsked = '';
        chartCursor = null;
        rerender();
    }

    // Whether a click on a window or scope that is already picked still means
    // something: with the graph on the combined view it takes the graph back.
    function pickable(on) {
        return !on || graphMode === 'joint';
    }

    // Ladder groups current account resets within 120 seconds, then merges adjacent
    // steps with the same countdown label. Its sums are display groups, which can
    // exceed the server's next_reset_returns or individual chart schedule events.
    // The chart and next-reset row retain the server's original grouping.
    function ladderOf(g) {
        if (!g || g.cycle) return null;
        var now = Date.now();
        var rows = [];
        barsOf(g).forEach(function (b) {
            if (b.state !== 'current' || !b.account || typeof b.left !== 'number') return;
            var at = Date.parse(b.resets_at || '');
            if (isFinite(at) && at >= now - 1000) rows.push({ bar: b, at: at });
        });
        rows.sort(function (a, b) { return a.at - b.at; });
        var steps = [];
        for (var i = 0; i < rows.length;) {
            var first = rows[i];
            var step = { at: first.at, iso: first.bar.resets_at, accounts: [], back: 0, last: first.at, lastIso: first.bar.resets_at, count: 1 };
            for (; i < rows.length && rows[i].at - first.at <= RESET_CLUSTER_SEC * 1000; i++) {
                step.accounts.push(rows[i].bar.account);
                step.back += 1 - Math.max(0, Math.min(1, rows[i].bar.left));
                if (rows[i].at >= step.last) {
                    step.last = rows[i].at;
                    step.lastIso = rows[i].bar.resets_at;
                }
            }
            // The steps come soonest first and the words are the same or longer step by step, so the ones that say the same stand side by side: glued to the step before.
            var before = steps[steps.length - 1];
            if (before && untilWords(before.at, now) === untilWords(step.at, now)) {
                step.accounts.forEach(function (key) {
                    if (before.accounts.indexOf(key) < 0) before.accounts.push(key);
                });
                before.back += step.back;
                before.count += 1;
                if (step.last >= before.last) {
                    before.last = step.last;
                    before.lastIso = step.lastIso;
                }
                continue;
            }
            steps.push(step);
        }
        return { steps: steps, now: now };
    }

    // The most steps the ladder draws, if the room allows; the rest of the resets (the steps over that number) are counted in "N more".
    var LADDER_STEPS = 4;

    // How long to a moment, said from the whole remainder and rounded down like a countdown: under a minute "<1m", under an hour the minutes ("40m"), under a day the hours
    // alone ("5h"), a day or more the days and the hours after them ("2d 5h"; "2d" when the hours are none). Written as the names of the windows are (5h, 7d: no space inside
    // the number and its unit). A remainder is the same in every zone, so nothing here reads one; the moment itself, in the reader's local time, is whenWords (the title of a step).
    // Every drawing says it again from Date.now(): the 30 s poll is the only clock. `now` (ms) is the clock of a drawing that says several moments (the ladder: the glue of its steps and the words
    // on them stand on one clock, so the milliseconds between two calls cannot part them); without it, Date.now().
    function untilWords(at, now) {
        if (typeof at !== 'number' || !isFinite(at)) return '';
        var mins = Math.floor((at - (typeof now === 'number' && isFinite(now) ? now : Date.now())) / 60000);
        // The sign is no word of a language (the text table refuses markup characters): "<" and one minute, "<1m" / "<1м".
        if (mins < 1) return '<' + t('ladder.in_min', { v: 1 });
        if (mins < 60) return t('ladder.in_min', { v: mins });
        var hours = Math.floor(mins / 60);
        if (hours < 24) return t('ladder.in_hour', { v: hours });
        var days = Math.floor(hours / 24);
        var rest = hours - days * 24;
        return rest ? t('ladder.in_day_hour', { d: days, h: rest }) : t('ladder.in_day', { v: days });
    }

    // The classes a node wears, as one string: an html node (and the tests' nodes) keeps them in className; an svg node's className is an object, so its class attribute is read.
    function classText(node) {
        var text = node ? node.className : '';
        if (text && typeof text === 'object' && typeof node.getAttribute === 'function') text = node.getAttribute('class');
        return String(text || '');
    }

    function hasClass(node, name) {
        return classText(node).split(/\s+/).indexOf(name) >= 0;
    }

    // Whether the node wears any of the classes named.
    function hasAnyClass(node, names) {
        return names.some(function (name) { return hasClass(node, name); });
    }

    // Take a class on or off a node (the tests' nodes have no classList; an svg node takes it by its class attribute).
    function classSet(node, name, on) {
        var names = classText(node).split(/\s+/).filter(function (c) { return c && c !== name; });
        if (on) names.push(name);
        if (node.className && typeof node.className === 'object' && typeof node.setAttribute === 'function') node.setAttribute('class', names.join(' '));
        else node.className = names.join(' ');
    }


    // Whether a media query matches now (false where the page cannot ask).
    function mediaIs(query) {
        try {
            return !!(window.matchMedia && window.matchMedia(query).matches);
        } catch (err) {
            return false;
        }
    }

    // Every node of the page that answers a selector: none where the root cannot be asked (a bare test node).
    function rootAll(selector) {
        return root && typeof root.querySelectorAll === 'function' ? root.querySelectorAll(selector) : [];
    }

    /* ------------------------------------------------------------------
       The quota plate (the owner's approved composition, 8 Nov 2026).

       Left, the rail: one ring per limit of the family — a window and, where
       the engine scopes a limit to models, that scope ("5h", "7d", "Fable 7d",
       "Gemini 5h") — on a capsule that is a button; the picked one is crimson.
       The figure in the ring is the limit's average share left over the
       accounts read now (the skill's average_remaining_pct; "—" with none,
       never 0), digits and % one unit. Under the rings, the ladder of resets
       of the picked limit (ladderOf): when, and how many account-windows come
       back if unused. On a phone the rings stand in a row and the ladder in a
       row under them.

       Right, the plate of the picked limit: its window and scope said once, the
       opened account's card at the left and every account's bar (no figure in a
       bar: the card says the exact share), then the history and the details.
       The bars keep one meaning at any number: --bar-max wide while they fit,
       narrower down to --bar-min, then another row (qBarsFit); the opened bar
       is outlined and moves nothing. Nothing here sums unlike limits, turns an
       unknown into 0 or says a quota is a dispatch.
       ------------------------------------------------------------------ */

    // The step of the ladder the reader lit ({limit, at}), and whether the ladder shows every step.
    var qLit = null;
    var qLadderAll = false;

    // A ring's own short name: the window ("5h", "7d") and, for a limit scoped to models, that scope before it ("Fable 7d").
    function ringName(g, tab, names) {
        if (g.cycle) return t('ring.cycle');
        var len = shortLength(tab.seconds);
        var scope = scopeLabel(g, names, tab);
        // (the window stays on the line of the scope's last word: a name that wraps breaks before "Claude and", never before "7d")
        return scope && scope !== t('scope.all_models') ? t('ring.scoped', { scope: scope, len: len }).replace(/ (\S+)$/, '\u00a0$1') : len;
    }

    // The rings of every limit drawn (hidden ones stay in Rows), in the window order, the family-wide limit of a window first.
    function ringLimits(tabs) {
        var out = [];
        tabs.forEach(function (tab) {
            tab.groups.forEach(function (g) { out.push({ g: g, tab: tab }); });
            // A window whose every limit the reader hid in Rows is the family's still: its place stays, dotted, and says so; a place it has no window for is
            // not drawn at all (no grey place).
            if (!tab.groups.length && tab.hidden) out.push({ g: null, tab: tab });
        });
        return out;
    }

    function limitRail(hid, tabs, shown, ctx, failed) {
        var rail = el('nav', 'q-rail');
        rail.setAttribute('aria-label', t('rail.label'));
        var rings = el('div', 'q-rings');
        rings.setAttribute('role', 'group');
        rings.setAttribute('aria-label', t('win.row'));
        // How many accounts the family's widest window applies to: a limit that applies to fewer says so to a reader.
        var widest = Math.max.apply(null, [0].concat(ringLimits(tabs).map(function (item) { return item.g && !item.g.cycle ? slotCount(item.g) : 0; })));
        ringLimits(tabs).forEach(function (item) {
            var g = item.g;
            if (!g) {
                var hidden = el('button', 'q-ring q-ring-hidden');
                hidden.setAttribute('type', 'button');
                hidden.setAttribute('aria-disabled', 'true');
                hidden.setAttribute('tabindex', '-1');
                var said = item.tab.name + ' — ' + t('win.hidden_in_rows');
                hidden.setAttribute('aria-label', said);
                hidden.title = said;
                var hg = el('span', 'q-gauge');
                hg.appendChild(ringGauge(NaN, 'unknown', 'lg'));
                hidden.appendChild(hg);
                var htx = el('span', 'q-ring-tx');
                htx.appendChild(el('span', 'q-ring-name', shortLength(item.tab.seconds)));
                htx.appendChild(el('span', 'q-ring-sub', t('win.hidden_in_rows')));
                hidden.appendChild(htx);
                rings.appendChild(hidden);
                return;
            }
            var on = g === shown;
            var avg = tabAverage(g);
            var name = ringName(g, item.tab, ctx.names);
            var b = el('button', 'q-ring' + (on ? ' on' : ''));
            b.setAttribute('type', 'button');
            b.setAttribute('aria-pressed', on ? 'true' : 'false');
            b.setAttribute('data-focus', 'ring:' + hid + ':' + g.key);
            b.setAttribute('data-limit', g.key);
            var r = windowReading(g.cycle ? g.status : g);
            var full = ctx.names[g.key] || name;
            var spoken = avg === '—' ? t('win.none', { name: full })
                : (r ? t('win.avg_read', { name: full, avg: avg, read: r.read, n: r.total }) : t('win.avg', { name: full, avg: avg }));
            if (r && !g.cycle && widest && r.total < widest) spoken += '; ' + t('win.applies', { m: r.total, n: widest });
            var hint = g.cycle ? g.note : scopeWords(g);
            b.setAttribute('aria-label', hint ? spoken + ' — ' + hint : spoken);
            b.title = hint ? spoken + ' — ' + hint : spoken;
            var gauge = el('span', 'q-gauge');
            var pct = g.measured && g.measured.accounts ? g.measured.average_remaining_pct : NaN;
            gauge.appendChild(ringGauge(pct, typeof pct === 'number' && isFinite(pct) ? 'ok' : 'unknown', 'lg'));
            var pc = el('span', 'q-pc', avg);
            pc.setAttribute('aria-hidden', 'true');
            gauge.appendChild(pc);
            b.appendChild(gauge);
            var tx = el('span', 'q-ring-tx');
            var nameNode = el('span', 'q-ring-name', name);
            // A limit with accounts not read wears the star its note under the plate explains.
            if (!failed && windowIncomplete(g.cycle ? g.status : g)) nameNode.appendChild(el('span', 'q-star', '*'));
            tx.appendChild(nameNode);
            tx.appendChild(el('span', 'q-ring-sub', t('ring.avg_left')));
            tx.appendChild(el('span', 'q-ring-val', avg === '—' ? avg : t('ring.avg_short', { avg: avg })));
            b.appendChild(tx);
            b.addEventListener('click', function (e) {
                e.stopPropagation();
                // A budget is not a limit to pick: its ring is the only one it has.
                if (!g.cycle && pickable(on)) {
                    qLit = null;
                    qLadderAll = false;
                    pickWindow(g);
                    restoreFocus('ring:' + hid + ':' + g.key);
                }
            });
            rings.appendChild(b);
        });
        arrowRow(rings, ['ArrowLeft', 'ArrowRight', 'ArrowUp', 'ArrowDown']);
        rail.appendChild(rings);
        if (shown && !shown.cycle) rail.appendChild(resetLadder(shown, ctx));
        return rail;
    }

    function resetLadder(g, ctx) {
        var box = el('div', 'q-ladder');
        box.setAttribute('role', 'group');
        box.setAttribute('aria-label', t('ladder.label', { name: ctx.names[g.key] || '' }));
        box.appendChild(el('h4', 'q-ladder-title', t('ladder.title')));
        var lad = ladderOf(g);
        var steps = lad ? lad.steps : [];
        // Refresh the selected step from this drawing's ladder, before its bars.
        if (qLit && qLit.limit === g.key) {
            var picked = steps.find(function (st) { return st.at === qLit.at; });
            if (picked) qLit.accounts = picked.accounts.slice();
            else qLit = null;
        }
        if (!steps.length) {
            box.appendChild(el('span', 'q-ladder-none', t('ladder.none')));
            return box;
        }
        var max = qLadderAll ? steps.length : LADDER_STEPS;
        steps.slice(0, max).forEach(function (st) {
            var key = 'step:' + g.key + ':' + st.at;
            var on = !!qLit && qLit.limit === g.key && qLit.at === st.at;
            var names = st.accounts.map(function (a) { return accountName(ctx.index, a); });
            var amount = '+' + st.back.toFixed(st.back >= 10 ? 0 : 1);
            var when = untilWords(st.at, lad.now);
            var b = el('button', 'q-step' + (on ? ' on' : ''));
            b.setAttribute('type', 'button');
            b.setAttribute('aria-pressed', on ? 'true' : 'false');
            b.setAttribute('data-focus', key);
            var said = t('ladder.step', { when: whenWords(st.iso), accounts: plural(st.accounts.length, 'account'), names: names.join(', '), amount: amount });
            b.setAttribute('aria-label', said);
            b.title = said;
            b.appendChild(el('span', 'q-step-dot'));
            b.appendChild(el('span', 'q-step-when', when));
            b.appendChild(el('span', 'q-step-back', st.accounts.length > 1 ? t('ladder.back_n', { amount: amount, n: st.accounts.length }) : amount));
            b.addEventListener('click', function (e) {
                e.stopPropagation();
                qLit = on ? null : { limit: g.key, at: st.at, accounts: st.accounts.slice() };
                if (qLit && st.accounts.length === 1) {
                    selectAccount(st.accounts[0], key, g.key);
                } else {
                    rerender();
                }
                restoreFocus(key);
            });
            box.appendChild(b);
        });
        if (steps.length > LADDER_STEPS) {
            var more = el('button', 'q-ladder-more', qLadderAll ? t('ladder.less') : t('foot.more', { n: steps.length - max }));
            more.setAttribute('type', 'button');
            more.setAttribute('aria-expanded', qLadderAll ? 'true' : 'false');
            more.setAttribute('aria-label', qLadderAll ? t('ladder.less_label') : t('ladder.more_label', { n: steps.length - max }));
            more.setAttribute('data-focus', 'ladder-more:' + g.key);
            more.addEventListener('click', function (e) {
                e.stopPropagation();
                qLadderAll = !qLadderAll;
                rerender();
                restoreFocus('ladder-more:' + g.key);
            });
            box.appendChild(more);
        }
        return box;
    }

    // The accounts of the lit step of this limit, or null.
    function litOf(g) {
        return qLit && qLit.limit === g.key ? qLit.accounts : null;
    }

    function barKindOf(bar) {
        var known = bar.state !== 'unknown' && typeof bar.left === 'number' && isFinite(bar.left);
        return {
            known: known,
            last: known && bar.state === 'last_known',
            spent: known && !!bar.at_limit && bar.state !== 'last_known',
            was: known && !!bar.at_limit && bar.state === 'last_known',
            held: known && !!bar.restricted && heldNow(bar) && !bar.at_limit
        };
    }

    function quotaBars(g, rowName, index, family) {
        var bars = barsOf(g);
        var col = el('div', 'q-bcol');
        var strip = el('div', 'q-bars');
        strip.setAttribute('role', 'group');
        strip.setAttribute('aria-label', t('bars.label', { name: rowName }));
        strip.setAttribute('data-strip', g.key);
        strip.setAttribute('data-n', String(bars.length));
        var lit = litOf(g);
        if (lit) strip.setAttribute('data-step', '1');
        var seen = {};
        bars.forEach(function (bar, i) {
            var k = barKindOf(bar);
            var acc = bar.account && family ? accountIn(family.accounts || [], bar.account) : null;
            var off = !!acc && acc.enabled === false;
            var cls = 'q-bar';
            if (!k.known) cls += ' unknown';
            if (k.last) cls += ' last';
            if (k.spent) cls += ' spent';
            if (k.was) cls += ' was';
            if (k.held) cls += ' held';
            // Restricted is said apart from held: a restriction the engine reports may hold the account now (held) or not.
            if (k.known && bar.restricted) cls += ' restricted';
            if (off) cls += ' off';
            var isSel = !!bar.account && bar.account === selectedAccountKey;
            if (isSel) cls += ' sel';
            if (lit && bar.account && lit.indexOf(bar.account) >= 0) cls += ' lit';
            seen[!k.known ? 'unknown' : k.last ? 'last' : k.spent ? 'spent' : k.held ? 'held' : 'cur'] = true;
            if (off) seen.off = true;
            var b = el('button', cls);
            b.setAttribute('type', 'button');
            b.setAttribute('data-focus', 'bar:' + g.key + ':' + (bar.account || i));
            b.setAttribute('data-state', bar.state || 'current');
            if (bar.account) b.setAttribute('data-account', bar.account);
            if (k.known) {
                var share = Math.max(0, Math.min(1, bar.left));
                b.setAttribute('data-share', String(share));
                if (share > 0) {
                    var f = el('span', 'q-fill' + (share >= 0.999 ? ' full' : ''));
                    f.style.height = (+(share * 100).toFixed(2)) + '%';
                    b.appendChild(f);
                }
            } else {
                var q = el('span', 'q-unk', '?');
                q.setAttribute('aria-hidden', 'true');
                b.appendChild(q);
            }
            var words = barWords(bar, index, rowName) + (off ? ' · ' + t('card.state_off') : '');
            b.setAttribute('aria-label', words + (bar.account ? barPickWords(isSel) : ''));
            b.setAttribute('aria-pressed', isSel ? 'true' : 'false');
            b.title = words;
            if (bar.account) {
                b.addEventListener('click', function (e) {
                    e.stopPropagation();
                    if (qLit && qLit.limit === g.key && qLit.accounts.indexOf(bar.account) < 0) qLit = null;
                    // A press on the picked bar lets the pick go (the card then asks for one); on another bar it picks that account.
                    selectAccount(bar.account === selectedAccountKey ? '' : bar.account, 'bar:' + g.key + ':' + bar.account, g.key);
                    restoreFocus('bar:' + g.key + ':' + bar.account);
                });
            } else {
                b.disabled = true;
            }
            strip.appendChild(b);
        });
        col.appendChild(strip);
        // The legend says only the states that stand in the row: green is what is left (no key for it).
        var keys = el('div', 'q-keys');
        [['held', 'k-held', 'keys.held'], ['spent', 'k-spent', 'keys.spent'], ['last', 'k-last', 'keys.last'],
            ['unknown', 'k-unknown', 'keys.unknown'], ['off', 'k-off', 'keys.off']].forEach(function (x) {
            if (!seen[x[0]]) return;
            var s = el('span', 'q-key');
            var sw = el('i', 'q-sw ' + x[1]);
            sw.setAttribute('aria-hidden', 'true');
            s.appendChild(sw);
            s.appendChild(document.createTextNode(t(x[2])));
            keys.appendChild(s);
        });
        if (keys.firstChild) col.appendChild(keys);
        return col;
    }

    // The selected account’s state; the projection does not name applicable hold causes.
    function cardState(ctx, g, bar, account, k) {
        if (account && account.enabled === false) return ['off', t('card.state_off')];
        if (!k.known) return ['unknown', t('card.state_unknown')];
        if (k.last) return ['last', t('card.state_last')];
        if (k.spent) return ['limit', t('card.state_limit')];
        // The projection marks the hold but does not identify its applicable blockers.
        if (k.held) return ['held', t('card.state_held')];
        return null;
    }

    // One dated line of the card's resources: "Bonus resets: 2 usable now · just now".
    function resLine(box, k, v, tail, cls, title) {
        var d = el('div', 'q-rl' + (cls ? ' ' + cls : ''));
        d.appendChild(el('span', 'q-rk', k));
        d.appendChild(document.createTextNode(' '));
        d.appendChild(el(cls === 'ok' ? 'b' : 'span', 'q-rv', v));
        (tail || []).forEach(function (part) {
            if (!part) return;
            d.appendChild(el('span', 'q-ra', ' · '));
            d.appendChild(el('span', 'q-ra q-u', part));
        });
        if (title) d.title = title;
        box.appendChild(d);
    }

    // What one reset offer says: a count with unknown usability is "granted", never "available"; a reported 0 is a zero.
    function offerWords(o) {
        var n = o.available_count;
        if (typeof n !== 'number' || !isFinite(n)) return { v: t('res.count_unknown'), cls: 'unk' };
        if (n === 0) return { v: t('card.none_zero'), cls: '' };
        if (o.usable_now === true) return { v: t('card.offer_usable', { n: n }), cls: 'ok' };
        if (o.usable_now === false) return { v: t('card.offer_not_usable', { n: n }), cls: '' };
        return { v: t('card.offer_unknown', { n: n }), cls: '' };
    }

    // The account's manual reset offers (resources.resets), else the legacy count (quota.reset_credits): one source, never both.
    function resetLines(account) {
        var r = account && account.resources && account.resources.resets;
        var out = [];
        if (r && r.state && r.state !== 'not_reported' && Array.isArray(r.rows)) {
            var kept = r.state === 'last_known';
            var age = agoWords(r.observed_at) || t('card.age_unknown');
            if (!r.rows.length) {
                out.push({ k: t('card.bonus'), v: t('card.none_offered'), tail: [kept ? t('card.last_known') : '', age], cls: kept ? 'unk' : '' });
                return out;
            }
            r.rows.forEach(function (o) {
                var w = offerWords(o);
                var old = kept || o.state === 'last_known';
                var exp = NaN;
                (o.grants || []).forEach(function (gr) {
                    var at = Date.parse(gr && gr.expires_at || '');
                    if (gr && gr.available_count > 0 && isFinite(at) && at > Date.now() && !(at >= exp)) exp = at;
                });
                if (old && o.usable_now === true) w = { v: t('card.offer_usable_then', { n: o.available_count }), cls: '' };
                var label = r.rows.length > 1 && o.label ? engineWord(String(o.label)) : t('card.bonus');
                out.push({ k: label, v: w.v, cls: old ? 'unk' : w.cls,
                    tail: [o.reason ? String(o.reason) : '', isFinite(exp) && !old ? t('card.expires_in', { when: untilWords(exp) }) : '',
                        old ? t('card.last_known') : '', age],
                    title: t('card.offer_title') });
            });
            return out;
        }
        var c = account && account.quota && account.quota.reset_credits;
        if (c && c.reason !== 'not_reported' && c.state) {
            var last = c.state === 'last_known';
            var cage = c.observed_at ? (agoWords(c.observed_at) || t('card.age_unknown')) : t('card.age_unknown');
            var line = { k: t('card.bonus'), tail: [], cls: 'unk', title: creditProvenance(c) };
            if ((c.state === 'current' || last) && creditCount(c.count)) {
                line.v = c.count === 0 ? t('card.none_zero') : t('card.reported_unknown', { n: c.count });
                line.tail = [last ? t('card.last_known') : '', cage];
                line.cls = last ? 'unk' : '';
            } else if (c.state === 'unreadable') {
                line.v = t('credits.acc_unreadable');
            } else if (c.state === 'conflict') {
                line.v = t('credits.acc_conflict');
            } else {
                line.v = t('card.unknown');
            }
            out.push(line);
        }
        return out;
    }

    // The account's credit balances (resources.balances), each as dated; the included budget is the limit itself and stays off this list.
    function creditLines(account) {
        var f = account && account.resources && account.resources.balances;
        var out = [];
        if (!f || !f.state || f.state === 'not_reported' || !Array.isArray(f.rows)) return out;
        var age = agoWords(f.observed_at) || t('card.age_unknown');
        f.rows.forEach(function (row) {
            if (!row || row.usage_kind === 'included') return;
            var old = f.state === 'last_known' || row.state === 'last_known';
            var amount = row.unlimited ? t('cycle.unlimited') : (row.amount === null || row.amount === undefined ? '' : moneyWords(row.amount, row));
            var label = engineWord(String(row.label || '')) || t('card.credits');
            out.push({ k: label, v: amount || t('res.amount_unknown'), cls: !amount || old ? 'unk' : '',
                tail: [row.scope === 'team' ? t('card.team') : '', old ? t('card.last_known') : '', age],
                title: label + ': ' + (amount || t('res.amount_unknown')) + (row.note ? ' · ' + engineWord(String(row.note)) : '') });
        });
        return out;
    }

    function cardResources(card, account) {
        var lines = resetLines(account).concat(creditLines(account));
        if (!lines.length) return;
        var box = el('div', 'q-res');
        box.setAttribute('role', 'group');
        box.setAttribute('aria-label', t('card.res_label'));
        lines.forEach(function (x) { resLine(box, x.k + ':', x.v, x.tail, x.cls, x.title); });
        card.appendChild(box);
    }

    // The opened account: its name and address, its share of the picked limit, when that limit comes back, its plan and how old the reading is, its bonus resets and
    // credits as dated, and the account's full details under one button (renderInspector, the same card the list opens).
    function accountCard(ctx, g, family) {
        var account = selectedAccountKey && family ? accountIn(family.accounts || [], selectedAccountKey) : null;
        var bar = account ? barOf(g, account.key) : null;
        var card = el('section', 'q-card');
        card.setAttribute('aria-label', t('acc.card_label'));
        card.setAttribute('aria-live', 'polite');
        if (!account) {
            card.className += ' q-card-none';
            card.appendChild(el('div', 'q-al', t('card.pick')));
            return card;
        }
        var k = bar ? barKindOf(bar) : { known: false };
        var head = el('div', 'q-card-head');
        // The account's state, as its full card says it: the dot, and its word for a reader who does not see it.
        var tone = accountTone(account, (currentView && currentView.facets) || {});
        var nm = el('div', 'q-an');
        var dot = stateDot(tone, true);
        classSet(dot, 'q-dot', true);
        nm.appendChild(dot);
        nm.appendChild(el('span', 'sr-only', toneWord(tone)));
        nm.appendChild(document.createTextNode(String(account.label || account.email || account.key)));
        nm.title = String(account.label || '');
        head.appendChild(nm);
        var clear = el('button', 'q-clear');
        clear.setAttribute('type', 'button');
        clear.setAttribute('data-focus', 'inspector-clear');
        clear.setAttribute('aria-label', t('acc.clear_label'));
        clear.title = t('acc.clear_label');
        clear.appendChild(icon('close', 12));
        clear.addEventListener('click', function (e) {
            e.stopPropagation();
            qLit = null;
            clearAccount();
        });
        head.appendChild(clear);
        card.appendChild(head);
        if (account.email && account.email !== account.label) {
            var em = el('div', 'q-em', account.email);
            em.title = account.email;
            card.appendChild(em);
        }
        var l1 = el('div', 'q-al q-share');
        var budget = g.cycle ? cycleOf(account) : null;
        var unlimited = budget && budget.state === 'unlimited';
        if (unlimited) {
            l1.appendChild(el('b', 'q-big', t('cycle.unlimited')));
            l1.appendChild(document.createTextNode(' ' + t('cycle.included_usage')));
        } else if (k.known && budget && budget.scale && budget.left !== null && budget.limit) {
            l1.appendChild(el('b', 'q-big', moneyShort(budget.left, budget.scale)));
            l1.appendChild(document.createTextNode(' ' + t('cycle.left_of', { limit: moneyShort(budget.limit, budget.scale) })));
        } else if (k.known) {
            l1.appendChild(el('b', 'q-big', leftPct(Math.max(0, Math.min(1, bar.left)) * 100) + '%'));
            l1.appendChild(document.createTextNode(' ' + t('card.left')));
        } else {
            l1.appendChild(el('b', 'q-big', '?'));
            l1.appendChild(document.createTextNode(' ' + t('card.no_reading')));
        }
        var tag = unlimited && budget.last_known ? ['last', t('card.state_last')]
            : bar && !unlimited ? cardState(ctx, g, bar, account, k) : null;
        if (tag) {
            l1.appendChild(document.createTextNode(' '));
            l1.appendChild(el('span', 'q-st q-st-' + tag[0], tag[1]));
        }
        card.appendChild(l1);
        var l2 = el('div', 'q-al');
        var at = bar ? Date.parse(bar.resets_at || '') : NaN;
        if ((k.known || unlimited) && isFinite(at) && at > Date.now() - 1000) {
            l2.appendChild(el('span', 'q-u', t(g.cycle ? 'card.cycle_ends_in' : (k.spent ? 'card.back_in' : 'card.resets_in'), { when: untilWords(at) })));
            l2.appendChild(el('span', 'q-ck', ' · ' + clockAt(bar.resets_at)));
            l2.title = whenWords(bar.resets_at);
        } else if (!k.known && !unlimited) {
            l2.textContent = !bar ? t('acc.lim_none') : account.signed_in === false ? t('card.signed_out') : t('card.not_read');
        } else {
            l2.textContent = t('card.no_reset');
        }
        card.appendChild(l2);
        var l3 = el('div', 'q-al');
        if (account.plan) l3.appendChild(el('span', 'q-u', t('card.plan', { plan: planWord(account.plan, family) })));
        var observed = bar && bar.observed_at ? bar.observed_at : quotaObservedAt(account);
        var ago = agoWords(observed);
        if (ago) {
            if (l3.firstChild) l3.appendChild(document.createTextNode(' · '));
            l3.appendChild(el('span', 'q-u', t('card.read', { when: ago })));
        }
        if (l3.firstChild) card.appendChild(l3);
        // A cooldown the engine reports (the whole account's, or a model's) is said on the card's face, with its scope and its end.
        var cool = el('div', 'q-cool');
        renderCooldowns(cool, account.quota || {});
        if (cool.firstChild) card.appendChild(cool);
        cardResources(card, account);
        // Everything else of the account under one button (the same state as its full card's Diagnostics, diagnosticsOpen).
        var more = el('div', 'q-more');
        var moreBtn = el('button', 'q-more-btn', t(diagnosticsOpen ? 'acc.less' : 'acc.more'));
        moreBtn.setAttribute('type', 'button');
        moreBtn.setAttribute('aria-expanded', diagnosticsOpen ? 'true' : 'false');
        moreBtn.setAttribute('aria-label', t('acc.details', { name: account.label }));
        moreBtn.setAttribute('data-focus', 'inspector-details');
        moreBtn.addEventListener('click', function (e) {
            e.stopPropagation();
            diagnosticsOpen = !diagnosticsOpen;
            rerender();
        });
        more.appendChild(moreBtn);
        if (diagnosticsOpen) {
            var body = el('div', 'q-more-body');
            cardDetails(body, family, account, ctx, (currentView && currentView.facets) || {});
            more.appendChild(body);
        }
        card.appendChild(more);
        return card;
    }

    // Everything else the engine says of the account, under the card's one button: its verification and state, every window of it, its resources as the engine dated them
    // (spending and diagnostics too), its absence, cooldowns and model exhaustions, and its readings — the parts of the account's full card (renderInspector).
    function cardDetails(box, family, account, ctx, facets) {
        var quota = account.quota || {};
        var meta = el('div', 'meta-line insp-meta');
        if (account.kind !== 'profile') meta.appendChild(el('span', null, t('acc.vendor_cli')));
        if (account.verification && account.verification.label) {
            meta.appendChild(el('span', verificationFailed(account) ? 'meta-bad' : null, verifyWord(account.verification.label)));
        }
        var reason = inactiveReason(account, facets);
        if (reason && reason !== 'failed') meta.appendChild(el('span', null, inactiveWord(reason)));
        var observed = relTime(quotaObservedAt(account));
        meta.appendChild(el('span', 'quota-observed', observed ? t('acc.quota_observed', { when: observed }) : t('acc.quota_unobserved')));
        box.appendChild(meta);
        var notes = el('div', 'meta-line insp-meta');
        appendHarnessNotes(notes, family, facets);
        if (notes.firstChild) box.appendChild(notes);
        var echoes = quota.state === 'ok' && !!(quota.constraints && quota.constraints.length);
        if (!echoes && !(quota.state === 'cooling' && cooldownsOf(quota, 'account').length)) box.appendChild(renderQuotaVerdict(quota));
        renderAccountCredits(box, quota);
        renderAccountResources(box, account.resources);
        accountLimits(box, ctx, account);
        if (!budgetOnly(account)) renderAbsence(box, account, quota.absence);
        renderExhaustions(box, quota, false);
        accountReadings(box, account);
        if (account.caption) box.appendChild(el('div', 'acct-note muted', t('acc.credential', { caption: account.caption })));
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

    // The bars of a limit by what each says now: at the limit (a current
    // reading), held back now, at the limit when last read (dated, not
    // exhaustion now), read earlier and not at the limit (dated, not held
    // back: the plate says what it had), unknown.
    function limitKinds(g) {
        var bars = barsOf(g);
        return {
            spent: bars.filter(function (b) { return b.state === 'current' && b.at_limit; }),
            held: bars.filter(function (b) { return b.state !== 'unknown' && heldNow(b) && !b.at_limit; }),
            was: bars.filter(function (b) { return b.state === 'last_known' && b.at_limit; }),
            last: bars.filter(function (b) { return b.state === 'last_known' && !b.at_limit && !heldNow(b); }),
            unknown: bars.filter(function (b) { return b.state === 'unknown'; })
        };
    }

    // The held bars by the reason that holds them: one group for each of one or two
    // named reasons (in the order of FLAG_ORDER, the same on every drawing), else all
    // in one — three reasons or more, or a bar whose reason is not named.
    function heldGroups(held) {
        if (!held.length) return [];
        var by = {};
        var unnamed = false;
        held.forEach(function (b) {
            var flags = heldFlags(b);
            if (!flags.length) unnamed = true;
            flags.forEach(function (f) { (by[f] = by[f] || []).push(b); });
        });
        var order = FLAG_ORDER;
        var keys = Object.keys(by).sort(function (a, b) {
            var i = order.indexOf(a);
            var j = order.indexOf(b);
            return (i < 0 ? order.length : i) - (j < 0 ? order.length : j) || compareText(a, b);
        });
        if (unnamed || keys.length > 2) return [{ word: t('legend.restricted'), flag: '', bars: held }];
        return keys.map(function (f) { return { word: flagWord(f), flag: f, bars: by[f] }; });
    }

    // What the words under a limit's bars are made of: an item for each state the limit has that the rest of the screen does not say,
    // in one order — held back (one item for each reason), at the limit when last read, read earlier and not at the limit (with the
    // sum of what those accounts had). An item is its number and its words and nothing else: the bars above are its sample, the
    // figure is the plate's, the accounts at the limit now are the plate's bubble over the bars (limitBubble: said there, so not
    // twice) and the accounts not read are the note's under the windows, so an account with no usable reading has no item here
    // (its bar is a "?"). The accounts an item counts are named in its hint, one line each (`lines`), never beside the number.
    function limitLegend(g, index, rowName) {
        var kinds = limitKinds(g);
        var items = [];
        var who = function (b) { return accountName(index, b.account); };
        heldGroups(kinds.held).forEach(function (group) {
            items.push({ k: 'held', n: group.bars.length, word: group.word,
                lines: group.bars.map(function (b) {
                    return who(b) + ' — ' + (group.flag ? group.word
                        : (heldFlags(b).map(flagWord).join(', ') || t('state.restricted')));
                }) });
        });
        // At the limit when last read: dated and muted, not exhaustion now.
        if (kinds.was.length) {
            items.push({ k: 'was', n: kinds.was.length, word: t('legend.was_at_limit'),
                lines: kinds.was.map(function (b) {
                    return t('reading.was_limit', { name: who(b), when: relTime(b.observed_at) || t('reading.time_unknown') });
                }) });
        }
        // Read earlier, not at the limit then and not held back: not current, so not in the figure, and what it had is said here.
        // The plate lights the bars of these accounts when the engine named every one of them.
        if (kinds.last.length) {
            var had = kinds.last.reduce(function (sum, b) { return sum + Math.max(0, Math.min(1, b.left)); }, 0);
            items.push({ k: 'last', n: kinds.last.length, word: t('legend.last'), extra: num(had, 2),
                accounts: kinds.last.every(function (b) { return !!b.account; }) ? kinds.last.map(function (b) { return b.account; }) : null,
                lines: kinds.last.map(function (b) {
                    return t('reading.last_name', { name: who(b), when: relTime(b.observed_at) || t('reading.time_unknown') });
                }),
                note: t('legend.last_note') });
        }
        return items;
    }

    // The next reported reset as the cells of the row under the legend — the time, how
    // many accounts reset then (when more than one), how much a full refill gives back if
    // nothing more is used before it — with the hint of the whole row; or the one cell that
    // says no time is reported; or null. A cell is `pre`, the value `b` (the figure), `post`.
    function limitReset(g) {
        var m = g.measured || {};
        if (g.next_reset && g.next_reset.at) {
            var n = g.next_reset.accounts || 0;
            var back = g.next_reset_returns >= 0.005 ? amountWords(g.next_reset_returns) : '';
            var when = whenWords(g.next_reset.at);
            var cells = [boldAt(t('reset.next', { when: when }), when)];
            if (n > 1) cells.push(boldAt(t('reset.accounts', { n: n }), String(n)));
            if (back) cells.push(boldAt(t('reset.if_unused', { amount: '+' + back }), '+' + back));
            return { cells: cells, title: back ? t('reset.hint_amount', { amount: back }) : t('reset.hint') };
        }
        if (m.accounts && g.reset_unknown === m.accounts) return { cells: [{ pre: t('reset.none') }], title: '' };
        return null;
    }

    function cellText(cell) {
        return (cell.pre || '') + (cell.b || '') + (cell.post || '');
    }

    // The full phrases of a limit's states and reset, names and times in them, as the
    // plate's note says them aloud (the screen reader's, after the word Chart). The screen carries the numbers;
    // these are what they count.
    function limitSpoken(g, index) {
        var out = [];
        var kinds = limitKinds(g);
        var named = function (b) { return !!(b.account && index[b.account]); };
        var spent = kinds.spent;
        if (spent.length === 1 && named(spent[0])) {
            out.push(t('state.named_limit', { name: accountName(index, spent[0].account) })
                + (spent[0].resets_at ? ' ' + t('bar.until', { when: whenWords(spent[0].resets_at) }) : ''));
        } else if (spent.length) {
            out.push(t('state.named_limit', { name: spent.length }));
        }
        var held = kinds.held;
        if (held.length) {
            var flags = {};
            held.forEach(function (b) { heldFlags(b).forEach(function (f) { flags[f] = true; }); });
            var keys = Object.keys(flags);
            var word = keys.length === 1 ? flagWord(keys[0]) : t('state.restricted');
            out.push((held.length === 1 && named(held[0]) ? accountName(index, held[0].account) : held.length) + ' ' + word);
        }
        var wasSpent = kinds.was;
        if (wasSpent.length) {
            out.push(t('state.named_was_limit', { name: wasSpent.length === 1 && named(wasSpent[0]) ? accountName(index, wasSpent[0].account) : wasSpent.length })
                + (wasSpent.length === 1 ? ' (' + (relTime(wasSpent[0].observed_at) || t('reading.time_unknown')) + ')' : ''));
        }
        if (kinds.unknown.length) out.push(t('state.unknown_count', { n: kinds.unknown.length }));
        var next = limitReset(g);
        if (next) out.push(next.cells.map(cellText).join(' · '));
        return out;
    }

    // The accounts at the limit now, in words under the number row: "At the limit · 3", the first of them by name (to
    // FOOT_NAMES, then "N more", which opens the account list, as the note's does), and in how long the soonest of them is back ("back in 9h";
    // none, no tail). Only when there are such accounts; it takes the keyboard, and pointed at or focused it lights their bars (dimOnItem).
    // Null when none is at the limit now. `spent` are the bars of limitKinds.
    function limitBubble(spent, index, key, plate) {
        if (!spent.length) return null;
        var node = el('div', 'wt-limit');
        var named = spent.filter(function (b) { return !!b.account; });
        // Few are named; more are counted by the head alone (the hint names them all, and the bubble lights their bars).
        var shown = named.length <= FOOT_NAMES && named.length === spent.length ? named : [];
        var said = shown.map(function (b) { return accountName(index, b.account); });
        var soonest = null;
        spent.forEach(function (b) {
            var at = Date.parse(b.resets_at || '');
            if (isFinite(at) && at > Date.now() && (soonest === null || at < soonest)) soonest = at;
        });
        var head = t('plate.at_limit', { n: spent.length });
        var back = '';
        if (soonest !== null) {
            back = t('plate.back', { n: spent.length, 'in': untilWords(soonest) });
        }
        node.appendChild(headNode('b', 'wt-limit-head', head));
        said.forEach(function (name, i) {
            node.appendChild(document.createTextNode(' '));
            node.appendChild(el('span', 'wt-who', i < said.length - 1 ? name + ',' : name));
        });
        node.setAttribute('tabindex', '0');
        node.setAttribute('role', 'group');
        node.setAttribute('aria-label', [head].concat(said, back ? [back] : []).join(', '));
        node.setAttribute('data-k', 'limit');
        node.setAttribute('data-focus', 'atlimit:' + key);
        node.title = hintOf(spent.map(function (b) {
            return accountName(index, b.account) + ' — ' + t('aria_bar.at_limit') + (b.resets_at ? ' ' + t('bar.until', { when: whenWords(b.resets_at) }) : '');
        }));
        // The plate dims to these accounts' bars when the engine named every one of them; else to the bars at the limit.
        var own = named.length === spent.length ? named.map(function (b) { return b.account; }) : null;
        if (own) node.setAttribute('data-accounts', JSON.stringify(own));
        dimOnItem(node, plate, 'limit', function () { return plate; }, own);
        if (back) {
            node.appendChild(document.createTextNode(' · '));
            node.appendChild(el('span', 'wt-limit-back', back));
        }
        return node;
    }

    // "Lowest left" names a ranking, and its words say so. They stand in the hint of the plate's figure and in its spoken note: the plate
    // draws no badge for it (the figure is the plate's, and each fact stands once).
    function tightWords(g) {
        return 'Lowest average share left among this family’s measured limits: a ranking, not a verdict'
            + (scopeWords(g) ? '. It binds only its own ' + scopeWords(g) + ', not the family’s other models' : '')
            + '.';
    }

    // Keep the current figure and coverage on the face; detailed explanations follow the bars, not ahead of the account the reader picked.
    function quotaNotes(plate, key, notes) {
        notes = notes.filter(Boolean);
        if (!notes.length) return;
        var detail = el('details', 'q-limit-details');
        var summary = el('summary', '', t('plate.details'));
        summary.setAttribute('data-focus', 'limit-details:' + key);
        detail.appendChild(summary);
        notes.forEach(function (note) {
            classSet(note, 'q-note', true);
            detail.appendChild(note);
        });
        detail.open = !!limitDetailsOpen[key];
        detail.addEventListener('toggle', function () { limitDetailsOpen[key] = detail.open; });
        plate.appendChild(detail);
    }

    // The plate of the billing-cycle budget (Cursor): the money its accounts read now have left of their cycles, said once, then the opened account and the bars,
    // as a window's plate has them; no history (none of money is kept: cycleNote says so).
    function cycleQuotaPlate(ctx, cycle, family, stamp, foot, credits, accWord) {
        var plate = el('section', 'q-plate q-plate-cycle');
        plate.setAttribute('id', 'quotas-win-panel');
        plate.setAttribute('aria-label', cycle.note);
        var head = el('div', 'q-head');
        head.appendChild(el('h3', 'q-title', t('plate.cycle')));
        var chip = el('span', 'q-scope', t('cycle.included_usage'));
        chip.title = cycle.note;
        head.appendChild(chip);
        if (stamp) {
            classSet(stamp, 'q-stamp', true);
            head.appendChild(stamp);
        }
        plate.appendChild(head);
        var onlyUnlimited = !cycle.counted && cycle.unlimited.length > 0 && !cycle.lastKnown && !cycle.unknown;
        var money = el('div', 'q-money');
        money.appendChild(el('b', 'q-money-fig', cycle.counted ? moneyShort(cycle.left, cycle.scale) : (onlyUnlimited ? t('cycle.unlimited') : '—')));
        money.appendChild(document.createTextNode(' ' + (cycle.counted ? t('cycle.left_of', { limit: moneyShort(cycle.limit, cycle.scale) })
            : (onlyUnlimited ? t('cycle.included_usage') : t('common.no_current_reading')))));
        money.title = t('plate.cycle_title');
        var status = el('div', 'q-status');
        status.appendChild(money);
        if (cycle.onDemand) {
            var over = el('div', 'q-note', t('plate.over_plan', { amount: cycle.onDemand.amount }));
            over.title = cycle.onDemand.title;
            status.appendChild(over);
        }
        plate.appendChild(status);
        var group = { key: cycle.key, cycle: true, bars: cycle.bars };
        var accts = el('div', 'q-accts' + (group.bars.length === 1 ? ' q-accts-one' : ''));
        accts.appendChild(accountCard(ctx, group, family));
        var col = quotaBars({ key: group.key, cycle: true, bars: group.bars.filter(function (b) { return b.state !== 'unlimited'; }) }, cycle.name, ctx.index, family);
        var label = el('div', 'q-bhead');
        label.appendChild(accWord || el('span', 'q-blabel', t('bars.title')));
        col.insertBefore(label, col.firstChild);
        accts.appendChild(col);
        plate.appendChild(accts);
        quotaNotes(plate, family.harness_id + '|' + cycle.key,
            [resetNode(cycle.reset), foot, credits, legendNode(cycle.legend, cycle.key)]);
        return plate;
    }

    // The title of a window's plate: a count of days, hours or minutes with the word window ("7-day window", "5-hour window"), the largest of the three
    // that holds the length whole; a length none of them holds keeps the tab's own name ("Weekly window").
    function plateTitle(tab) {
        var sec = tab.seconds;
        if (typeof sec === 'number' && sec > 0) {
            if (sec % 86400 === 0) return t('plate.len_d', { n: sec / 86400 });
            if (sec % 3600 === 0) return t('plate.len_h', { n: sec / 3600 });
            if (sec % 60 === 0) return t('plate.len_m', { n: sec / 60 });
        }
        return t('plate.window', { len: tab.name });
    }

    // The plate of the picked limit: the head (its window, its scope), the stamp and the note of the accounts not read, the family's reset credits, then the opened account and the bars.
    function quotaPlate(ctx, tab, g, family, stamp, foot, credits, accWord, markTightest) {
        var plate = el('section', 'q-plate');
        plate.setAttribute('id', 'quotas-win-panel');
        plate.setAttribute('data-limit', g.key);
        plate.setAttribute('aria-label', ctx.names[g.key] || '');
        var head = el('div', 'q-head');
        head.setAttribute('aria-describedby', 'quotas-win-say');
        head.appendChild(el('h3', 'q-title', plateTitle(tab)));
        var scope = scopeLabel(g, ctx.names, tab);
        var own = scope && scope !== t('scope.all_models');
        // A limit the engine scopes by model-id prefix ("gemini-") is every model of that family, the ones in no list too: "Gemini models", never a list
        // of three; a limit scoped to named models is "<model> only".
        var prefixes = (((g.scope || {}).prefixes || {}).names || []).filter(Boolean);
        var chip = el('span', 'q-scope' + (own ? ' own' : ''), !own ? t('scope.all_models')
            : prefixes.length ? t('plate.scope_models', { scope: scope }) : t('plate.scope_only', { scope: scope }));
        if (prefixes.length) chip.title = t('plate.covers', { prefixes: prefixes.map(function (x) { return String(x) + '*'; }).join(', ') }) + (scopeWords(g) ? ' · ' + scopeWords(g) : '');
        else if (scopeWords(g)) chip.title = scopeWords(g);
        head.appendChild(chip);
        if (stamp) {
            classSet(stamp, 'q-stamp', true);
            head.appendChild(stamp);
        }
        plate.appendChild(head);
        // The status shows this limit's current account-windows (never a sum of unlike limits). Coverage stays in the head; the full explanations
        // follow the account bars under Limit details, so a long stale/held roster does not push the controls off the phone's first screen.
        var status = el('div', 'q-status');
        plate.appendChild(status);
        var fig = rowFigure(g);
        var figure = el('div', 'l-fig q-fig');
        figure.appendChild(figureNode(fig.text, !fig.any, { num: 'q-fig-num', dot: 'q-fig-dot' }));
        figure.appendChild(el('span', 'meta-num of', ' ' + t('plate.of', { n: fig.of })));
        figure.title = (fig.any ? '' : 'No current reading — not zero. ')
            + 'Account-windows left now: the share each account read now has left of this limit, added up (a '
            + 'full account counts 1), of the ' + plural(fig.of, 'account') + ' it applies to. Last-known and '
            + 'unknown accounts are not counted, never as 0.'
            + (markTightest ? ' ' + tightWords(g) : '');
        if (fig.of > 0) figure.setAttribute('data-of', String(fig.of));
        status.appendChild(figure);
        // A limit the engine scopes by model prefix covers every model whose id starts so, the ones in no list too: said once, from the engine's own prefixes.
        if (prefixes.length) {
            var cov = el('div', 'q-cov', t('plate.covers', { prefixes: prefixes.map(function (p) { return String(p) + '*'; }).join(', ') }));
            if (scopeWords(g)) cov.title = scopeWords(g);
            status.appendChild(cov);
        }
        // What the plate says, whole, to a reader who does not see it: the limit, its account-windows left now, who is at the limit or held, the next reset.
        var spoken = [ctx.names[g.key] || tab.name];
        if (scopeWords(g)) spoken.push(scopeWords(g));
        spoken.push(fig.any ? t('plate.current_words', { amount: fig.text, total: fig.of }) : 'no current reading');
        if (fig.lastKnown) {
            spoken.push(t('plate.last_words', { amount: num(fig.lastKnown.windows, 2), when: relTime(fig.lastKnown.oldest_observed_at) || t('reading.time_unknown') }));
        }
        spoken.push(t('win.say_current', { n: fig.of, current: (g.measured || {}).accounts || 0 }));
        limitSpoken(g, ctx.index).forEach(function (w) { spoken.push(w); });
        restrictionWords(g).forEach(function (w) { spoken.push(w); });
        if (restrictionWords(g).length && num(g.unrestricted_windows, 2)) spoken.push(num(g.unrestricted_windows, 2) + ' unrestricted');
        coverageWords(g).forEach(function (w) { spoken.push(w); });
        if (markTightest) spoken.push('lowest average share left in this family, a ranking, not a verdict');
        var say = el('span', 'sr-only', spoken.join(' — '));
        say.setAttribute('id', 'quotas-win-say');
        plate.appendChild(say);
        plate.setAttribute('aria-describedby', 'quotas-win-say');
        // Who is at the limit now, and when the first of them comes back; who is held and by what (the legend names them; under the pointer their bars light).
        // (each of these notes wears q-note, the plate's one rule for them)
        var atLimit = limitBubble(limitKinds(g).spent, ctx.index, g.key, plate);
        var states = limitLegend(g, ctx.index, ctx.names[g.key] || tab.name);
        if (foot) states = states.filter(function (item) { return item.k !== 'last'; });
        var accts = el('div', 'q-accts' + (barsOf(g).length === 1 ? ' q-accts-one' : ''));
        accts.appendChild(accountCard(ctx, g, family));
        var col = quotaBars(g, ctx.names[g.key] || tab.name, ctx.index, family);
        var label = el('div', 'q-bhead');
        if (accWord) label.appendChild(accWord);
        else label.appendChild(el('span', 'q-blabel', t('bars.title')));
        col.insertBefore(label, col.firstChild);
        accts.appendChild(col);
        plate.appendChild(accts);
        quotaNotes(plate, family.harness_id + '|' + g.key, [atLimit, foot, credits, legendNode(states, g.key)]);
        return plate;
    }

    // The bars' grid: the most columns at --bar-max that fit, narrower down to --bar-min, then another row; every account stays on screen.
    var Q_BAR_MIN = 20;
    // Under a finger (a coarse pointer) a bar is at least this wide: the grid takes another row sooner.
    var Q_BAR_MIN_TOUCH = 28;
    var Q_BAR_MAX = 56;
    var Q_BAR_GAP = 5;
    var qFitBound = false;
    function qBarsFit() {
        if (stopped) return;
        try {
            var strips = rootAll('.q-bars');
            var least = mediaIs('(pointer: coarse)') ? Q_BAR_MIN_TOUCH : Q_BAR_MIN;
            for (var i = 0; i < strips.length; i++) {
                var strip = strips[i];
                var n = Number(strip.getAttribute('data-n')) || 0;
                var w = strip.parentNode ? strip.parentNode.clientWidth : 0;
                // The one bar of a one-account plate on a phone stands in a grid cell of its own (its column has no width of its own to read): one column, the widest bar.
                if (n === 1 && !w) {
                    setVar(strip, '--q-cols', '1');
                    setVar(strip, '--q-bw', Q_BAR_MAX + 'px');
                    setVar(strip, '--q-bh', '96px');
                    continue;
                }
                if (!n || !w) continue;
                var rows = 1;
                var cols = n;
                var bw = Q_BAR_MAX;
                for (; rows <= n; rows++) {
                    cols = Math.ceil(n / rows);
                    bw = (w - (cols - 1) * Q_BAR_GAP) / cols;
                    if (bw >= least) break;
                }
                bw = Math.max(Q_BAR_MIN, Math.floor(Math.min(Q_BAR_MAX, bw)));
                var bh = rows === 1 ? 96 : rows === 2 ? 66 : rows === 3 ? 54 : 48;
                setVar(strip, '--q-cols', String(cols));
                setVar(strip, '--q-bw', bw + 'px');
                setVar(strip, '--q-bh', bh + 'px');
            }
            if (!qFitBound && window.addEventListener) {
                qFitBound = true;
                window.addEventListener('resize', qBarsFit);
                window.addEventListener('resize', chartFit);
            }
        } catch (err) {
            // A layout that cannot be read keeps the stylesheet's grid.
        }
    }

    // The box the plate, the selected account's card and the timeline stand in.
    function limitsBox(family) {
        var box = el('div', 'lrows');
        box.setAttribute('role', 'group');
        box.setAttribute('aria-label', 'Limits of ' + (family ? familyName(family) : 'this family'));
        return box;
    }

    // The family's limits. Returns what the timeline, the selected account
    // and the account list read from — or null when there is no overview.
    // `card` (inspectorSlot) is drawn under the limit whose bar selected the
    // account, if that limit is drawn.
    function renderReserve(parent, reserve, family, view, card) {
        if (!reserve || typeof reserve !== 'object') reserve = {};
        var section = el('section', 'reserve');
        section.setAttribute('aria-label', 'Reserve overview');
        // No visible title line: the family chip above already says whose
        // reserve this is. The heading stays for screen readers; the plate's
        // figure says the unit, About says it in full.
        section.appendChild(el('h3', 'title-section reserve-title sr-only', 'Reserve' + (family ? ' · ' + familyName(family) : '')));
        var summary = reserve.summary;
        if (!summary) {
            renderFamilyCredits(section, family);
            section.appendChild(el('div', 'meta-line reserve-note', reserve.error
                ? t('reserve.unavailable_why', { error: reserve.error }) : t('reserve.unavailable')));
            parent.appendChild(section);
            return null;
        }
        var hid = family ? family.harness_id : '';
        var groups = reserveGroups({ reserve: reserve }, hid);
        var famName = family ? shortFamily(family) : 'this family';
        // A family with no time window may still have a budget per billing cycle for the 30-day place.
        var cycle = groups.length || !family ? null : cycleBudget(family, hid, famName);
        // `top` puts a head (the stamp, where no plate holds it) first in the card; the family's reset credits follow it, right over the plate.
        function top(bar) {
            section.appendChild(bar);
            renderFamilyCredits(section, family);
        }
        // The windows stand in the rail, beside the card and not in it (render() puts the rail in its frame); the note of the accounts not read
        // and the family's reset credits stand in the plate (the plate takes them: windowPlate, cyclePlate).
        function railed(nav) {
            railBox = nav;
        }
        var reads = summary.reads || {};
        var ctx = { reserve: reserve, summary: summary, groups: groups, names: {}, index: accountIndex(view) };
        // The stamp tells a failed read, so no note tells who is not read: none is.
        var failed = !!readFailure(view, summary);
        if (reads.quota && reads.quota !== 'ok' && !(summary.cached || {}).quota && !groups.length) {
            var head = el('div', 'reserve-head');
            head.appendChild(windowStamp(view, summary, null));
            top(head);
            section.appendChild(el('div', 'meta-line reserve-note', t('reserve.not_read')));
            parent.appendChild(section);
            return ctx;
        }
        if (cycle) {
            // No time window, but a budget per billing cycle: it has the one ring (Cycle) and its own plate, and it is the one row: 5-hour and Weekly are not drawn
            // (the family reports no window for them).
            ctx.cycle = cycle;
            ctx.names[cycle.key] = cycle.name;
            var cycleTab = { seconds: CYCLE_PLACE, name: cycle.name, groups: [cycle.tab] };
            var cycleGroup = { key: cycle.key, cycle: true, bars: cycle.bars };
            card = pickDecide(family, cycleGroup, hid + '|' + cycle.key, card, view);
            ctx.card = card;
            ctx.pickGroup = cycleGroup;
            railed(limitRail(hid, [cycleTab], cycle.tab, ctx, failed));
            var cycleBox = limitsBox(family);
            var cycleWord = null;
            if (family && (family.accounts || []).length) {
                cycleWord = wordButton('acc.word', { n: family.accounts.length });
                cycleWord.setAttribute('data-focus', 'accounts');
                cycleWord.setAttribute('aria-pressed', accountsOpen ? 'true' : 'false');
                cycleWord.addEventListener('click', function (e) {
                    e.stopPropagation();
                    accountsSet(!accountsOpen);
                    rerender();
                    restoreFocus('accounts');
                });
            }
            cycleBox.appendChild(cycleQuotaPlate(ctx, cycle, family, windowStamp(view, summary, cycle.status),
                failed ? null : windowFoot(cycle.status, ctx.index, windowIncomplete(cycle.status)), familyCreditsNode(family), cycleWord));
            if (card && !accountsOpen) card.drawn = true;
            // (the note stands in the plate, where a window's history stands)
            cycleBox.childNodes[cycleBox.childNodes.length - 1].appendChild(cycleNote());
            section.appendChild(cycleBox);
            parent.appendChild(section);
            return ctx;
        }
        if (!groups.length) {
            // No window at all, and no budget: there is no rail; the stamp and the note say so.
            var bare = el('div', 'reserve-head');
            bare.appendChild(windowStamp(view, summary, null));
            top(bare);
            section.appendChild(el('div', 'meta-line reserve-note', t('reserve.no_limit',
                { family: family ? familyName(family) : t('reserve.this_family') })));
            parent.appendChild(section);
            return ctx;
        }
        ctx.names = limitNames(groups);
        var chartKey = chartKeyFor(hid, groups);
        var measuredGroups = groups.filter(function (g) { return g.measured && g.measured.accounts; }).length;
        var table = limitsBox(family);
        // The accounts list stands in the place of the bars, the chart and the card: that place (the plate with its card and its timeline) is the box `accPlace`, a fold that shuts while the list
        // is open (accFold; accountsSet). A family with no account has no list, so no word and no fold.
        var listable = !!(family && (family.accounts || []).length);
        var accRun = accountsRun;
        accountsRun = '';
        var accPlace = listable ? accFold('main', false) : null;
        ctx.accRun = accRun;
        // Rows the reader hid are not drawn here — display only: they stay in
        // the reserve, the account view, the history, the tool and the snapshot.
        var hidden = hiddenRows(view);
        // One row of windows over one plate, not a stack of rows: the picked
        // limit is the one the charted row was (chartKeys). A tab opens on the
        // scope the reader last left in its length, else on the family-wide one.
        var tabs = windowTabs(groups, hidden, ctx.names);
        var active = null;
        tabs.forEach(function (tab) { if (groupByKey(tab.groups, chartKey)) active = tab; });
        var shown = active ? groupByKey(active.groups, chartKey) : null;
        // The pick of the window on screen is decided here, before its plate is built (barStrip reads it): the top account unless the reader has picked or let go. The card of the
        // pick is the one this hands back (ctx.card: the drawing hands it on to the account list).
        if (shown) {
            card = pickDecide(family, shown, hid + '|' + shown.key, card, view);
            ctx.card = card;
        }
        // The word over the bars opens the list of accounts (and stays pressed while it is open): it heads the bars of the plate.
        var accWord = null;
        if (listable) {
            accWord = wordButton('acc.word', { n: family.accounts.length });
            accWord.setAttribute('data-focus', 'accounts');
            accWord.setAttribute('aria-pressed', accountsOpen ? 'true' : 'false');
            accWord.addEventListener('click', function (e) {
                e.stopPropagation();
                accountsSet(!accountsOpen);
                rerender();
                restoreFocus('accounts');
            });
        }
        railed(limitRail(hid, tabs, shown, ctx, failed));
        if (shown) {
            // The limit the plate stands for: a row of the list picks its account on this limit's bar (renderAccounts), as the bar does.
            ctx.pickGroup = shown;
            if (hid + '|' + shown.key !== cardWhere) {
                cardWhere = hid + '|' + shown.key;
                cardReset();
            }
            var foot = failed ? null : windowFoot(shown, ctx.index, windowIncomplete(shown));
            // While the list is open the plate is folded away: the word stands over the list then (accStrip, below), so it can shut it.
            var shownPlate = quotaPlate(ctx, active, shown, family, windowStamp(view, summary, shown), foot, familyCreditsNode(family),
                accountsOpen ? null : accWord, shown.tightest && measuredGroups > 1);
            table.appendChild(shownPlate);
            // The plate holds the opened account's card: the list draws its own only while it is open.
            if (card && !accountsOpen) card.drawn = true;
            // The history of the picked limit, the last part of its plate (the approved composition: one plate, the history under the bars).
            var line = timelineOpen ? foldOf('tl', timelineBlock(ctx, shown), false) : null;
            if (line) shownPlate.appendChild(line);
            (accPlace ? accPlace.firstChild : section).appendChild(table);
        } else {
            // Every limit hidden or none picked: no plate to hold the stamp and the credits, so they stand in the section, the stamp in the head, as where there is no window.
            renderFamilyCredits(section, family);
            var gone = el('div', 'reserve-head');
            gone.appendChild(windowStamp(view, summary, null));
            section.appendChild(gone);
            // And no plate means no bar to wear a pick: the pick the widget made itself goes with the plate (the reader's own stays, it is theirs), so the card of an account nobody
            // chose does not hang under Accounts at a view at rest. Unhid, the window is a new place for it (pickAt), and the top bar is picked afresh (pickDecide).
            if (pickAuto && selectedAccountKey) {
                selectedAccountKey = '';
                selectionOpener = '';
                selectionLimit = '';
                pickAuto = false;
                pickHome = '';
                pickAt = '';
                pickDue = false;
                ctx.card = null;
            }
        }
        var rowsNode = rowsMenu(ctx, groups, hidden);
        var keptAt = (view && view.kept) || summary.roster === 'cached'
            ? String((summary.cached || {}).accounts || '') : null;
        // Who stands in no row, by name, is said in the head of the account list.
        ctx.cover = [outsideWords(groups, family, ctx.index, keptAt) || coverSentence(groups), rosterWords(summary)]
            .filter(Boolean);
        if (listable) {
            // The word over the bars, on the right (the text action of the plate's chart word): it opens the list and stays the same word, pressed while the list is open. Rows and the line of
            // coverage go to the head of the list (renderAccounts); with no plate drawn Rows stays here, the one way back to a hidden row.
            var accStrip = el('div', 'acc-strip');
            if (!shown || accountsOpen) accStrip.appendChild(accWord);
            var listFold = accFold('listfold', !accountsOpen);
            var toOpen = accountsOpen;
            // The drawing that follows an opening or a shutting stands in the state the motion starts from, and plays the fold on the next frame.
            if (accRun) {
                classSet(accPlace, 'acc-shut', accRun === 'shut');
                classSet(listFold, 'acc-shut', accRun === 'open');
            } else {
                classSet(accPlace, 'acc-shut', toOpen);
            }
            accPlace.firstChild.inert = toOpen;
            listFold.firstChild.inert = !toOpen;
            ctx.listHost = listFold.firstChild;
            ctx.rowsNode = shown ? rowsNode : null;
            if (accStrip.firstChild) section.appendChild(accStrip);
            section.appendChild(accPlace);
            section.appendChild(listFold);
            if (!shown) section.appendChild(rowsNode);
            if (accRun && typeof window.requestAnimationFrame === 'function') {
                window.requestAnimationFrame(function () {
                    classSet(accPlace, 'acc-run', true);
                    classSet(listFold, 'acc-run', true);
                    void accPlace.offsetHeight;
                    classSet(accPlace, 'acc-shut', toOpen);
                    classSet(listFold, 'acc-shut', !toOpen);
                    setTimeout(function () {
                        classSet(accPlace, 'acc-run', false);
                        classSet(listFold, 'acc-run', false);
                        // Shut, the list leaves nothing behind.
                        if (!toOpen) while (listFold.firstChild.firstChild) listFold.firstChild.removeChild(listFold.firstChild.firstChild);
                    }, 450);
                });
            }
        } else {
            section.appendChild(rowsNode);
        }
        parent.appendChild(section);
        return ctx;
    }

    // Which limits the reader chose not to draw: the skill's saved display
    // set, as the last answer carried it.
    function hiddenRows(view) {
        var set = {};
        ((view && view.display && view.display.hidden) || []).forEach(function (key) { set[key] = true; });
        return set;
    }

    // "Rows": show or hide a limit's row (display only, saved by the skill
    // so it survives a reload; "Show all" undoes every hide).
    function rowsMenu(ctx, groups, hidden) {
        var count = groups.filter(function (g) { return hidden[g.key]; }).length;
        var menu = el('details', 'meta-line rows-menu');
        menu.setAttribute('data-focus', 'rows-menu');
        menu.appendChild(el('summary', null, count ? t('rows.hidden', { n: count }) : t('rows.title')));
        groups.forEach(function (g) {
            var label = el('label', null);
            var box = el('input', null);
            box.setAttribute('type', 'checkbox');
            if (!hidden[g.key]) box.setAttribute('checked', 'checked');
            box.checked = !hidden[g.key];
            box.setAttribute('aria-label', 'Show ' + ctx.names[g.key]);
            box.addEventListener('change', function () {
                var next = Object.keys(hiddenRows(currentView)).filter(function (key) { return key !== g.key; });
                if (!box.checked) next.push(g.key);
                saveHidden(next);
            });
            label.appendChild(box);
            label.appendChild(el('span', null, ctx.names[g.key]));
            menu.appendChild(label);
        });
        if (count) {
            var all = el('button', 'cover-link', t('rows.show_all'));
            all.setAttribute('type', 'button');
            all.addEventListener('click', function () {
                var familyKeys = {};
                groups.forEach(function (g) { familyKeys[g.key] = true; });
                saveHidden(Object.keys(hiddenRows(currentView)).filter(function (key) { return !familyKeys[key]; }));
            });
            menu.appendChild(all);
        }
        if (count || menu.open) menu.setAttribute('open', 'open');
        return menu;
    }

    function saveHidden(list) {
        if (stopped || !currentView) return;
        var seq = ++displaySeq;
        currentView.display = { hidden: list };
        rerender();
        request(DISPLAY_ROUTE, { method: 'POST', headers: { 'Content-Type': 'application/json' },
                                 body: JSON.stringify({ hidden: list, frame: displayFrame, seq: seq }) }, GET_TIMEOUT_MS).then(function (result) {
            var answer = null;
            try { answer = JSON.parse(result.body); } catch (err) { answer = null; }
            if (seq !== displaySeq || stopped || !currentView) return;
            displaySettled = seq;
            if (!answer || answer.error || !answer.display) {
                actionMessage = t('act.rows_unsaved');
                rerender();
                return;
            }
            currentView.display = answer.display;
            rerender();
        });
    }

    /* ------------------------------------------------------------------
       The timeline: the record behind now, one future ahead of it.
       ------------------------------------------------------------------ */

    function scenarioName(key) {
        for (var i = 0; i < SCENARIOS.length; i++) if (SCENARIOS[i].key === key) return t('scn.' + key);
        return key;
    }

    // The scenario drawn: the reader's pick, from the skill's chart. None on
    // a chart with no current reading (a kept screen, or nothing current).
    function scenarioOf(chart) {
        var all = chart && chart.scenarios;
        return all && all[scenario] ? all[scenario] : null;
    }

    // `quiet` keeps the group's name for a screen reader only: the options
    // say what they are. `idle` marks an option that, for now, draws what
    // another one draws: it stays a button that works, only muted.
    function segControl(label, options, current, focusPrefix, onPick, quiet) {
        var seg = el('div', 'seg');
        seg.setAttribute('role', 'group');
        seg.setAttribute('aria-label', label);
        seg.appendChild(el('span', quiet ? 'seg-label sr-only' : 'seg-label', label));
        options.forEach(function (opt) {
            var on = opt.key === current;
            var b = el('button', 'seg-opt' + (on ? ' active' : '') + (opt.idle ? ' idle' : ''), opt.name);
            b.setAttribute('type', 'button');
            b.setAttribute('aria-pressed', on ? 'true' : 'false');
            if (opt.spoken) b.setAttribute('aria-label', opt.spoken);
            if (opt.title) b.title = opt.title;
            if (opt.idle) b.setAttribute('data-idle', 'true');
            b.setAttribute('data-focus', focusPrefix + opt.key);
            b.addEventListener('click', function (e) {
                e.stopPropagation();
                onPick(opt.key);
            });
            seg.appendChild(b);
        });
        return seg;
    }

    // What the plot draws for the chosen view, in the shape drawChart already
    // reads: the selected limit (with its coupled futures when the skill sent
    // them) or the combined same-account view. Nothing is computed here.
    function drawnChart(chart) {
        var analytics = chart.analytics || {};
        var joint = analytics.joint;
        if (graphMode === 'joint' && joint && joint.available) {
            var hist = joint.history || { line: [], details: [], max_accounts: 0 };
            var known = joint.known || 0;
            return Object.assign({}, chart, {
                mode: 'joint', history: hist, scenarios: joint.scenarios, table: joint.table || [],
                y_max: joint.y_max, accounts: known,
                past_accounts: hist.max_accounts || 0, unit_text: jointUnitLeft(joint),
                pace_words: 'the common watched period (up to 7 days)', past: [], points: [],
                assumptions: joint.limitations || [],
                joint_note: joint
            });
        }
        var own = analytics.window;
        return Object.assign({}, chart, {
            mode: 'window', unit_text: 'account-windows left',
            scenarios: own && own.scenarios ? own.scenarios : chart.scenarios,
            table: own && own.scenarios ? own.table || [] : chart.table,
            scope_applies: own ? own.scope_applies : true,
            held_to_other_windows: own ? own.held_to_other_windows : 0,
            pace_words: ''
        });
    }

    // The unit of the combined view in words: the shortest binding window, as
    // "5-hour window equivalents left" (or "… now"). The length is read as
    // lengthName reads it, but each length has a phrase of its own, so a
    // language declines it: no sentence is built from the pieces.
    var JOINT_UNIT = {
        left: { week: 'joint.left_week', day: 'joint.left_day', hour: 'joint.left_hour', minute: 'joint.left_minute',
                none: 'joint.left_none', n_week: 'joint.left_w', n_day: 'joint.left_d', n_hour: 'joint.left_h',
                n_minute: 'joint.left_m' },
        now: { week: 'joint.now_week', day: 'joint.now_day', hour: 'joint.now_hour', minute: 'joint.now_minute',
               none: 'joint.now_none', n_week: 'joint.now_w', n_day: 'joint.now_d', n_hour: 'joint.now_h',
               n_minute: 'joint.now_m' }
    };

    function jointUnit(joint, which) {
        var keys = JOINT_UNIT[which];
        var words = windowLength(joint.unit.window_seconds);
        if (!words) return t(keys.none);
        if (keys[words]) return t(keys[words]);
        var found = /^(\d+) (\w+?)s$/.exec(words);
        return found && keys['n_' + found[2]] ? t(keys['n_' + found[2]], { n: Number(found[1]) }) : t(keys.none);
    }

    function jointUnitLeft(joint) {
        return jointUnit(joint, 'left');
    }

    // The combined view's figures, once: the figure now and whose it is, the
    // conditional hours, who is left out and why, and what the number is not.
    function jointNote(joint) {
        var box = el('div', 'meta-line tl-joint');
        box.setAttribute('role', 'group');
        box.setAttribute('aria-label', t('joint.aria'));
        var line = el('div', null);
        line.appendChild(el('b', null, num(joint.figure, 2)));
        line.appendChild(document.createTextNode(' ' + jointUnit(joint, 'now') + ' · '
            + t('joint.counted', { known: joint.known, of: plural(joint.accounts, 'account') })));
        var hours = joint.hours || {};
        if (hours.value !== null && hours.value !== undefined) {
            line.appendChild(document.createTextNode(' · ' + t('joint.hours', {
                hours: hours.value > 0 && hours.value < .005 ? '<0.01' : num(hours.value, 2),
                known: hours.known, unknown: hours.unknown })));
        }
        box.appendChild(line);
        var left = Object.keys(joint.unknown_reasons || {}).map(function (why) {
            return joint.unknown_reasons[why] + ' ' + why.replace(/_/g, ' ');
        });
        if (left.length) box.appendChild(el('div', null, t('joint.left_out_why', { list: left.join(', ') })));
        var cal = joint.calibration || {};
        box.appendChild(el('div', null, t('joint.windows', { list: (joint.windows || []).map(function (w) { return w.label; }).join(' + ') })
            + (cal.calibrated !== undefined ? ' · ' + t('joint.calibrated', { accounts: plural(cal.calibrated, 'account') }) : '')
            + ' · ' + (joint.limitations || [])[0]));
        return box;
    }

    // What the reader may ask the windows about, from the limits themselves:
    // the model names the family's constraints list, each exactly as sent. A
    // name the skill had to cut (its display bound) is never offered — a cut
    // name is not a model id — and names the skill left out are said, not
    // guessed. Nothing here is a list of models known in advance.
    var MODEL_NAME_BOUND = 80;

    function modelChoices(groups) {
        var seen = {};
        var names = [];
        var cut = false;
        var unlisted = false;
        groups.forEach(function (g) {
            (g.models || []).forEach(function (name) {
                var text = String(name);
                if (text.length >= MODEL_NAME_BOUND) { cut = true; return; }
                if (!seen[text]) { seen[text] = true; names.push(text); }
            });
            if (g.models_omitted) unlisted = true;
        });
        names.sort();
        return { names: names, cut: cut, unlisted: unlisted };
    }

    // Whether any window of the family says which request kinds it governs.
    function kindsVary(groups) {
        return groups.some(function (g) { return !!(g.scope && Array.isArray(g.scope.request_kinds)); });
    }

    // The scope the chart on hand was computed for, against the reader's choice:
    // a differing answer is asked for once (askForChart), never in a loop.
    function scopeStale(chart) {
        var scope = chart && chart.analytics && chart.analytics.scope;
        if (!scope) return false;
        return (scope.model || '') !== modelChoice || (scope.request_kind || 'text') !== kindChoice;
    }

    // Arrow keys move the keyboard along a row of buttons; Enter or Space then
    // chooses, as on any button.
    function arrowRow(box, keys) {
        box.addEventListener('keydown', function (e) {
            var move = keys.indexOf(e.key) < 0 ? 0 : (e.key === 'ArrowRight' || e.key === 'ArrowDown' ? 1 : -1);
            var home = e.key === 'Home' ? 1 : (e.key === 'End' ? 2 : 0);
            if (!move && !home) return;
            // A tab greyed with aria-disabled (a window the reader hid in Rows)
            // is skipped as a disabled one is: it keeps its hint.
            var nodes = Array.prototype.slice.call(box.querySelectorAll('button')).filter(function (n) {
                return !n.disabled && n.getAttribute('aria-disabled') !== 'true';
            });
            var at = nodes.indexOf(document.activeElement);
            if (at < 0 || !nodes.length) return;
            var next = home === 1 ? 0 : (home === 2 ? nodes.length - 1 : (at + move + nodes.length) % nodes.length);
            nodes.forEach(function (n) { if (n.getAttribute('role') === 'tab') n.setAttribute('tabindex', n === nodes[next] ? '0' : '-1'); });
            nodes[next].focus();
            if (e.preventDefault) e.preventDefault();
        });
    }

    // The combined same-account view, when the family has one: the skill sends
    // its figures with the chart (analytics.joint); nothing is computed here.
    function jointOf(analytics) {
        var joint = analytics && analytics.joint;
        return joint && joint.available ? joint : null;
    }

    // The graph's own pick: the plate's window (off) or Combined (on). Combined is
    // the graph's state alone; the window row above and the plate are never
    // touched by it, so the plate does not replay its entry.
    function pickGraph(joint) {
        graphMode = joint ? 'joint' : 'window';
        chartCursor = null;
        rerender();
    }

    // Combined, as one switch among the graph's controls (the window itself is
    // chosen in the window row above, never here): the kit's segment with its one
    // option, pressed when the graph shows the combined view.
    function jointSwitch() {
        var on = graphMode === 'joint';
        var seg = el('div', 'seg');
        var b = el('button', 'seg-opt' + (on ? ' active' : ''), t('chart.combined'));
        b.setAttribute('type', 'button');
        b.setAttribute('aria-pressed', on ? 'true' : 'false');
        b.setAttribute('data-focus', 'graph:joint');
        b.title = t('chart.combined_title');
        b.addEventListener('click', function (e) {
            e.stopPropagation();
            pickGraph(!on);
        });
        seg.appendChild(b);
        return seg;
    }

    // "Model: any": which request the windows are asked about. The choice is
    // one of the names the limits list (or none); the list opens in place and
    // survives a redraw like About does.
    function modelMenu(ctx) {
        var choices = modelChoices(ctx.groups);
        var kinds = kindsVary(ctx.groups);
        if (!choices.names.length && !kinds && !modelChoice && kindChoice === 'text') return null;
        var btn = el('button', 'pill-btn btn-compact tl-model' + (modelMenuOpen ? ' on' : ''));
        btn.setAttribute('type', 'button');
        btn.appendChild(el('span', 'tl-model-k', t('chart.model')));
        btn.appendChild(el('span', 'tl-model-v', modelChoice || t('chart.unspecified')));
        if (kindChoice === 'image') btn.appendChild(el('span', 'tl-model-k', t('chart.image')));
        btn.appendChild(icon('caret', 12));
        btn.setAttribute('aria-expanded', modelMenuOpen ? 'true' : 'false');
        btn.setAttribute('aria-controls', 'quotas-model-panel');
        btn.setAttribute('aria-label', t(kindChoice === 'image' ? 'chart.model_aria_image' : 'chart.model_aria',
            { model: modelChoice || t('chart.no_model_request') }));
        btn.title = btn.getAttribute('aria-label');
        btn.setAttribute('data-focus', 'tl-model');
        btn.addEventListener('click', function (e) {
            e.stopPropagation();
            modelMenuOpen = !modelMenuOpen;
            rerender();
            restoreFocus('tl-model');
        });
        var panel = null;
        if (modelMenuOpen) {
            panel = el('div', 'card tl-model-panel');
            panel.id = 'quotas-model-panel';
            panel.setAttribute('role', 'group');
            panel.setAttribute('aria-label', t('chart.model_panel'));
            panel.appendChild(el('div', 'meta-line tl-scope', t('chart.model_hint')));
            var opts = el('div', 'tl-model-opts');
            var names = choices.names.slice();
            var orphan = modelChoice && names.indexOf(modelChoice) < 0;
            if (orphan) names.unshift(modelChoice);
            [''].concat(names).forEach(function (name) {
                var on = name === modelChoice;
                var o = el('button', 'seg-opt' + (on ? ' active' : ''), name || t('chart.no_model'));
                o.setAttribute('type', 'button');
                o.setAttribute('aria-pressed', on ? 'true' : 'false');
                o.setAttribute('data-focus', 'tl-model:' + name);
                o.title = name ? (name === modelChoice && orphan ? t('chart.model_orphan', { name: name }) : name)
                    : t('chart.model_none_title');
                o.addEventListener('click', function (e) {
                    e.stopPropagation();
                    modelChoice = name;
                    chartAsked = '';
                    chartCursor = null;
                    rerender();
                });
                opts.appendChild(o);
            });
            arrowRow(opts, ['ArrowLeft', 'ArrowRight', 'ArrowUp', 'ArrowDown']);
            panel.appendChild(opts);
            if (kinds) {
                panel.appendChild(segControl(t('chart.request'), [{ key: 'text', name: t('chart.text_kind') }, { key: 'image', name: t('chart.image_kind') }],
                    kindChoice, 'tl-kind:', function (picked) {
                        kindChoice = picked;
                        chartAsked = '';
                        chartCursor = null;
                        rerender();
                    }));
            }
            if (choices.unlisted) panel.appendChild(el('div', 'meta-line tl-scope', t('chart.models_more')));
            if (choices.cut) panel.appendChild(el('div', 'meta-line tl-scope', t('chart.names_cut')));
        }
        return { button: btn, panel: panel };
    }

    // The headline and the nearest reset of the combined view, in a line each:
    // its figure and whose it is, then the soonest reported reset. The window's
    // own figures stand on the plate above, so only Combined draws this: its
    // numbers are the ones the plate does not have.
    function timelineHero(shown, sc) {
        var hero = el('div', 'tl-hero');
        var left = el('div', 'tl-now');
        var joint = shown.joint_note;
        var figure = joint.figure === null || joint.figure === undefined ? '—' : num(joint.figure, 2);
        var unit = jointUnitLeft(joint);
        var sub = t('joint.counted', { known: joint.known, of: plural(joint.accounts, 'account') })
            + (joint.accounts > joint.known ? ' · ' + t('joint.left_out', { n: joint.accounts - joint.known }) : '');
        var next = null;
        var first = sc && (sc.schedule || [])[0];
        if (first) next = { iso: isoOf(first.at), accounts: first.accounts };
        var big = el('div', 'tl-num', figure);
        big.appendChild(el('span', 'meta-line tl-unit', unit));
        left.appendChild(big);
        left.appendChild(el('div', 'meta-num tl-sub', sub));
        hero.appendChild(left);
        var right = el('div', 'tl-next');
        right.appendChild(el('div', 'meta-num tl-sub', t('joint.next_reset')));
        if (next && next.iso) {
            right.appendChild(el('div', 'num-strong tl-next-v', relTime(next.iso) || whenWords(next.iso)));
            right.appendChild(el('div', 'meta-num tl-sub', whenWords(next.iso) + (next.accounts > 1 ? ' · ' + plural(next.accounts, 'account') : '')));
        } else {
            right.appendChild(el('div', 'num-strong tl-next-v dim', t('joint.none_reported')));
        }
        hero.appendChild(right);
        return hero;
    }

    // The calm timeline of the charted limit, right under its plate and the
    // plate's own continuation, not a second plate: it has no head of its own (the
    // window is chosen in the row above and its figures stand on the plate), so it
    // opens with one row of controls — the span, the future, Combined, and the model
    // choice across from them; the combined view's headline with its nearest reset
    // (only there: its numbers are not the plate's); the chart with one short
    // legend. Everything longer — the combined figures' caveats, the long legend,
    // the reset schedule, the details and data — is folded under Details, never
    // thrown away.
    function timelineBlock(ctx, g) {
        var box = el('div', 'tl-block');
        box.setAttribute('role', 'group');
        box.setAttribute('aria-label', t('chart.aria_box', { name: ctx.names[g.key] }));
        var name = ctx.names[g.key];
        var horizon = horizonFor(g);
        var chart = ctx.reserve.chart;
        var onHand = !!chart && chart.group_key === g.key && chart.horizon === horizon;
        // The Combined switch is the chart's own: a chart in hand that is another family's does not lend it to this one while its chart is read
        // (it would stand there for a moment and go, and the row of controls with it).
        var familyOf = function (key) { return String(key || '').indexOf('|') > 0 ? String(key).split('|')[0] : ''; };
        var lent = onHand || !chart || !familyOf(chart.group_key) || !familyOf(g.key) || familyOf(chart.group_key) === familyOf(g.key);
        var joint = jointOf(lent ? chart && chart.analytics : null);
        var menu = modelMenu(ctx);
        // What the plot draws is known before the row of controls is: the row says when a pace nobody has measured
        // makes Recent pace the same line as No new use.
        var shown = onHand ? drawnChart(chart) : null;
        var idle = paceIdle(shown);

        box.appendChild(el('h4', 'q-hist-title', t('chart.history')));
        var controls = el('div', 'tl-controls wt-controls');
        controls.appendChild(segControl(t('chart.span'), HORIZONS.map(function (h) {
            return { key: h, name: h === '24h' ? t('chart.h24') : t('chart.d7'),
                     spoken: t(h === '24h' ? 'chart.span_24h' : 'chart.span_7d') };
        }), horizon, 'horizon:', function (picked) {
            horizonChoice = picked;
            chartCursor = null;
            rerender();
        }));
        controls.appendChild(segControl(t('chart.future'), SCENARIOS.map(function (s) {
            var said = scenarioName(s.key);
            if (s.key === 'recent_pace' && idle) {
                return { key: s.key, name: said, idle: true, title: t('chart.pace_idle_title'),
                         spoken: said + ' — ' + t('chart.pace_idle_title') };
            }
            return { key: s.key, name: said, title: t(s.key === 'no_new_use' ? 'chart.future_title_no_new_use'
                : 'chart.future_title_recent_pace') };
        }), scenario, 'scenario:', function (picked) {
            scenario = picked;
            rerender();
        }));
        if (joint) controls.appendChild(jointSwitch());
        if (menu) controls.appendChild(menu.button);
        box.appendChild(controls);
        if (menu && menu.panel) box.appendChild(menu.panel);

        if (!onHand) {
            // The chart is being read: its place has the layout the drawn block has (capsule: chart) - the plot's own canvas, empty, so it is as high
            // as the plot will be at this width, a line of legend and the row of Details - so the card keeps its height and the foot of the rail stands.
            var size = chartSize();
            chartDrawn = size;
            var holder = el('div', 'tl-body');
            var wait = el('div', 'chart-wait');
            var ghost = svgCanvas('0 0 ' + size.W + ' ' + size.H, size.W, size.H);
            ghost.setAttribute('class', 'chart-svg');
            wait.appendChild(ghost);
            wait.appendChild(el('div', 'meta-line chart-wait-text', t('chart.loading')));
            holder.appendChild(wait);
            var legend = el('div', 'calm-legend');
            legend.setAttribute('aria-hidden', 'true');
            legend.appendChild(el('span', 'legend-item', '\u00a0'));
            holder.appendChild(legend);
            box.appendChild(holder);
            var fold = el('div', 'chart-table chart-hold');
            fold.setAttribute('aria-hidden', 'true');
            box.appendChild(fold);
            return box;
        }
        if (!chart.y_max) {
            box.appendChild(el('div', 'meta-line reserve-note', t('chart.no_account')));
            return box;
        }
        var updating = scopeStale(chart);
        var sc = scenarioOf(shown);
        if (shown.joint_note) box.appendChild(timelineHero(shown, sc));
        var body = el('div', 'tl-body' + (updating ? ' is-updating' : ''));
        if (updating) {
            body.setAttribute('aria-busy', 'true');
            box.appendChild(el('div', 'meta-line reserve-note tl-updating', t('chart.updating', {
                what: (modelChoice || t('chart.no_model_request')) + (kindChoice === 'image' ? ' (' + t('chart.image') + ')' : '') })));
        }
        drawChart(body, name + (shown.mode === 'joint' ? ' · combined' : ''), shown, sc);
        body.appendChild(calmLegend(shown, sc));
        box.appendChild(body);

        // Under Details: everything the calm view folds away.
        var front = [];
        if (scopeWords(g)) front.push(el('div', 'meta-line tl-scope', scopeWords(g)));
        if (shown.joint_note) front.push(jointNote(shown.joint_note));
        var facts = timelineFacts(g, shown, sc, ctx);
        front.push(chartLegend(shown, sc, facts.lead));
        if (facts.shown.length) {
            var said = el('div', 'meta-line line-stack tl-facts');
            facts.shown.forEach(function (f) { said.appendChild(el('div', f[1] || null, f[0])); });
            front.push(said);
        }
        if (sc) {
            var cp = checkpointLine(sc, shown);
            if (cp) front.push(cp);
            front.push(scheduleTable(sc, shown, horizon));
            var note = scheduleNote(g, sc, ctx, horizon);
            if (note) front.push(note);
        }
        box.appendChild(detailsBlock(g, shown, sc, ctx, facts.notes, front));
        return box;
    }

    // Whether the pace line of what the plot draws is no line of its own: no
    // account has a measured pace, so Recent pace holds every account and is
    // the same line as No new use. Read from the scenarios drawn (the window's
    // or Combined's), never from the mode.
    function paceIdle(chart) {
        var pace = chart && chart.scenarios && chart.scenarios.recent_pace;
        return !!pace && pace.at_pace === 0;
    }

    // One short line under the plot: only what is drawn, each in a few words.
    // The long legend, with whose total each line is, stays under Details.
    function calmLegend(chart, sc) {
        var legend = el('div', 'calm-legend');
        function item(swatch, text) {
            var li = el('span', 'legend-item');
            if (swatch) li.appendChild(swatch);
            li.appendChild(el('span', null, text));
            legend.appendChild(li);
        }
        var drawn = recordDrawn(recordLine(chart));
        var carried = !!(chart.history && (chart.history.details || []).some(function (d) { return d && d.carried; }));
        if (drawn) item(legendSwatch('line-observed'), t('chart.recorded'));
        if (carried) item(legendSwatch('line-carried'), t('chart.carried'));
        if (!drawn && !carried) item(null, t('chart.no_record'));
        if (sc) {
            item(legendSwatch('line-scenario'), t(scenario === 'recent_pace' && paceIdle(chart) ? 'chart.condition_idle' : 'chart.condition',
                { name: scenarioName(scenario).toLowerCase() }));
            if ((sc.schedule || []).length) item(resetSwatch(), t('chart.reported_reset'));
        } else {
            item(null, t('chart.no_future'));
        }
        return legend;
    }

    function resetSwatch() {
        var svg = svgCanvas('0 0 10 10', 10, 10);
        svg.appendChild(svgEl('circle', { cx: 5, cy: 5, r: 3.5, 'class': 'reset-dot' }));
        return svg;
    }

    // Who each line sums, one line each under the chart — the record's
    // accounts, and the future's with what it assumes (timelineFacts) — so a
    // step between them at now is read as two bases, never as use. With no
    // future drawn, the second line says why.
    function chartLegend(chart, sc, lead) {
        var legend = el('div', 'meta-line chart-legend');
        function item(cls, text) {
            var li = el('span', 'legend-item');
            if (cls) li.appendChild(legendSwatch(cls));
            li.appendChild(el('span', null, text));
            legend.appendChild(li);
        }
        var recorded = chart.past_accounts || 0;
        if (chart.history && recordDrawn(recordLine(chart))) {
            item('line-observed', t('chart.history_legend'));
        } else if (!chart.history && recorded && recordDrawn(chart.past)) {
            item('line-observed', t(chart.past_basis === 'last_known' ? 'chart.recorded_n_none' : 'chart.recorded_n',
                { accounts: plural(recorded, 'account') }));
        } else {
            item('', t('chart.no_record'));
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
                ? t('fact.no_future_kept', { when: clockAt(isoOf(chart.now)) })
                : chart.history ? t('fact.no_future_history')
                    : t('fact.no_future_record', { accounts: plural(chart.past_accounts || 0, 'account') });
            return { lead: lead, shown: shown, notes: notes };
        }
        var span = chart.pace_words
            || spanWords((g.recent_pace || {}).span_min_seconds, (g.recent_pace || {}).span_max_seconds);
        var scenarioWord = scenarioName(scenario).toLowerCase() + ' · ';
        if (scenario === 'no_new_use') {
            lead = scenarioWord + (sc.accounts === 1
                ? t('fact.keeps_one', { accounts: plural(sc.accounts, 'current account') })
                : t('fact.keeps_many', { accounts: plural(sc.accounts, 'current account') }));
        } else if (!sc.at_pace) {
            lead = scenarioWord + (sc.accounts === 1
                ? t('fact.held_one', { accounts: plural(sc.accounts, 'current account') })
                : t('fact.held_many', { accounts: plural(sc.accounts, 'current account') })) + ' '
                + (chart.scope_applies === false ? t('fact.r_scope_not')
                    : chart.scope_applies === null ? t('fact.r_applic_unknown')
                    : chart.held_to_other_windows ? t('fact.r_binding_unknown')
                    : chart.mode === 'joint' ? t('fact.r_joint')
                    : (g.recent_pace || {}).state === 'ok' ? t('fact.r_no_positive')
                    : t('fact.r_no_pace', { why: paceStateWords(g) }));
        } else {
            lead = scenarioWord + t(chart.pace_words ? 'fact.pace_over' : 'fact.pace_last', { at_pace: sc.at_pace,
                accounts: sc.accounts, span: span, held: sc.held ? ', ' + t('fact.held_count', { n: sc.held }) : '' });
            var runs = runsOutWords(sc, ctx);
            if (runs) shown.push([runs, 'warn']);
            notes.push(t('fact.note_pace'));
            if (sc.zero_growth) {
                notes.push(t('fact.note_zero', { accounts: plural(sc.zero_growth, 'account') }));
            }
        }
        notes.push(t('fact.note_reset'));
        if (notCurrent) {
            notes.push(t(notCurrent === 1 ? 'fact.note_nocur_one' : 'fact.note_nocur_many', { accounts: plural(notCurrent, 'account') }));
        }
        var recorded = chart.past_accounts || 0;
        if (chart.history && !recordDrawn(recordLine(chart))) {
            notes.push(t('fact.note_norecord'));
        } else if (chart.history) {
            notes.push(t('fact.note_history'));
        } else if (chart.past_basis !== 'current' || recorded !== chart.accounts) {
            notes.push(recorded
                ? t('fact.note_sums', { recorded: plural(recorded, 'account'), now: plural(chart.accounts, 'account') })
                : t('fact.note_norecord'));
        }
        return { lead: lead, shown: shown, notes: notes };
    }

    function paceStateWords(g) {
        var pace = g.recent_pace || {};
        if (pace.state === 'warming_up') return t('pace.why_warming');
        if (pace.state === 'unavailable') return t('pace.why_unavailable');
        if (pace.state === 'ok') return t('pace.why_ok');
        return t('pace.why_none');
    }

    // When accounts run out at this pace, inside what is drawn.
    function runsOutWords(sc, ctx) {
        var before = (sc.runs_out || []).filter(function (r) { return !r.after_refill && r.reset_reported; });
        var none = (sc.runs_out || []).filter(function (r) { return !r.reset_reported; });
        var again = (sc.runs_out || []).filter(function (r) { return r.after_refill; });
        var parts = [];
        if (before.length) parts.push(t('runs.before', { n: before.length, when: whenWords(isoOf(before[0].at)) }));
        if (none.length) parts.push(t('runs.none', { n: none.length, when: whenWords(isoOf(none[0].at)) }));
        if (again.length) parts.push(t('runs.again', { n: again.length, when: whenWords(isoOf(again[0].at)) }));
        return parts.length ? t('runs.lead', { parts: parts.join('; ') }) : '';
    }

    // The reset schedule of the scenario drawn: when, how many accounts,
    // what each event gives back in this scenario, and the total after it.
    function scheduleTable(sc, chart, horizon) {
        var table = el('table', 'tbl schedule');
        table.appendChild(el('caption', null, t('sched.caption', { name: scenarioName(scenario).toLowerCase() })));
        var head = el('tr');
        [[t('sched.when'), ''], [t('sched.accounts'), 'n'], [t('sched.back'), 'n'], [t('sched.total_after'), 'n']].forEach(function (h) {
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
            tr.appendChild(el('td', 'n', row.adds === null ? t('common.unknown') : '+' + amountWords(row.adds)));
            tr.appendChild(el('td', 'n', t('sched.total_of', { total: row.total_after === null ? t('common.unknown') : num(row.total_after, 2),
                of: chart.y_max }) + (row.unknown_after ? ' · ' + t('sched.n_unknown', { n: row.unknown_after }) : '')));
            body.appendChild(tr);
        });
        if (rows.length > SCHEDULE_ROWS) {
            var last = rows[rows.length - 1];
            var moreRow = el('tr');
            var moreCell = el('td');
            moreCell.setAttribute('colspan', '4');
            var more = el('button', 'pill-btn btn-compact-wide schedule-more', scheduleAll ? t('sched.show_first', { n: SCHEDULE_ROWS })
                : t('sched.more', { more: plural(rows.length - shown.length, 'more reset'), when: eventWhen(last),
                    total: num(last.total_after, 2), of: chart.y_max }));
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
                ? t('sched.none_reporting')
                : t(horizon === '7d' ? (sc.later ? 'sched.none_7d_later' : 'sched.none_7d')
                    : (sc.later ? 'sched.none_24h_later' : 'sched.none_24h'),
                    sc.later ? { when: whenWords(isoOf(sc.later.at)), accounts: plural(sc.later.accounts, 'account') } : null));
            td.setAttribute('colspan', '4');
            tr.appendChild(td);
            body.appendChild(tr);
        }
        table.appendChild(body);
        // The table stands in a white card of its own (.schedule-card), the table its only child: caption, head and rows are the card's lines.
        var card = el('div', 'schedule-card tile');
        card.appendChild(table);
        return card;
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
            parts.push(t('sched.later_note', { when: whenWords(isoOf(sc.later.at)) }));
        }
        if (sc.no_reset) parts.push(t('sched.no_reset_note', { n: sc.no_reset }));
        var others = ctx.groups.filter(function (x) { return x.key !== g.key; });
        if (others.length && (sc.schedule || []).length) {
            var spent = others.filter(function (x) { return (x.measured || {}).at_limit; });
            parts.push(t(spent.length ? 'sched.other_limit_spent' : 'sched.other_limit', {
                list: spent.map(function (x) { return t('sched.at_limit_item', { name: ctx.names[x.key], n: x.measured.at_limit }); }).join('; ') }));
        }
        if (!parts.length) return null;
        var line = parts.join(' · ');
        return el('div', 'meta-line schedule-notes', line.charAt(0).toUpperCase() + line.slice(1) + '.');
    }

    // Approximately how much is left at a few moments ahead, in this scenario.
    function checkpointLine(sc, chart) {
        var points = sc.checkpoints || [];
        if (!points.length) return null;
        // A caption and one white tile for each moment: the number whole in its node (the point in a span of its own, as the plate's figure
        // has it), under it the moment and what it is of. The caption names the group for a reader that has no eyes: the tiles read in order,
        // "1.47", "in 24 h · of 20", so the scenario, the moment, the value and the total are all said.
        var caption = scenario === 'no_new_use' ? t('fact.left_nothing') : t('fact.left_pace');
        var box = el('div', 'checkpoints');
        box.setAttribute('role', 'group');
        box.setAttribute('aria-label', caption);
        box.appendChild(el('div', 'meta-line cp-cap', caption));
        var tiles = el('div', 'cp-tiles');
        points.forEach(function (cp) {
            var tile = el('div', 'cp-tile tile');
            tile.appendChild(figureNode(cp.value === null ? t('common.unknown') : num(cp.value, 2), cp.value === null, { num: 'cp-num', dot: 'cp-dot' }));
            tile.appendChild(el('span', 'meta-line cp-when', t('fact.tile_when', { when: offsetWords(cp.after_seconds), total: chart.y_max })));
            tiles.appendChild(tile);
        });
        box.appendChild(tiles);
        return box;
    }

    // What the collector's watch vouches for: where the unbroken watch began,
    // or only a lower bound.
    function watchSaid(history) {
        var w = (history || {}).unbroken_watch;
        if (!w || !w.since) return '';
        return t(w.exact ? 'pace.watched' : 'pace.watched_least', { time: clockAt(w.since) });
    }

    function recordWords(history) {
        var h = history || {};
        if (h.state === 'unavailable') return h.error ? t('rec.unavailable_why', { error: h.error }) : t('rec.unavailable');
        if (!h.collecting_since) return t('rec.none');
        return t(h.capped_before ? 'rec.since_capped' : 'rec.since', { when: formatResetAt(h.collecting_since) });
    }

    function paceWords(g, history) {
        var pace = g.recent_pace || {};
        var out = [];
        if (pace.state === 'ok' || pace.state === 'partial') {
            // The number of accounts the pace was asked of picks the form: "from 2 of 3 accounts".
            out.push(t(pace.state === 'ok' ? 'pace.ok' : 'pace.partial', { rate: num(pace.windows_per_hour, 3),
                known: pace.accounts_known, n: pace.of, span: spanWords(pace.span_min_seconds, pace.span_max_seconds) }));
            if (pace.zero_growth) {
                out.push(pace.zero_growth === pace.accounts_known
                    ? t('pace.zero_all')
                    : t('pace.zero_some', { n: pace.zero_growth }));
            }
            if (pace.state === 'partial') out.push(t('pace.rest'));
        } else if (pace.state === 'warming_up') {
            var watched = watchSaid(history);
            out.push(watched ? t('pace.warming_watch', { watched: watched }) : t('pace.warming'));
        } else if (pace.state === 'unavailable') {
            out.push(t('pace.unavailable'));
        } else if (pace.state === 'insufficient') {
            out.push(t('pace.insufficient'));
        }
        if (pace.exhaust_before_reset) {
            out.push(t(pace.earliest_exhaustion_at ? 'pace.runs_out_first' : 'pace.runs_out', {
                n: pace.exhaust_before_reset, when: formatResetAt(pace.earliest_exhaustion_at) }));
        }
        if (pace.reach_limit_no_reported_reset) {
            out.push(t(pace.earliest_no_reset_reach_at ? 'pace.reaches_first' : 'pace.reaches', {
                n: pace.reach_limit_no_reported_reset, when: formatResetAt(pace.earliest_no_reset_reach_at) }));
        }
        return out;
    }

    // A limit's details: every figure the row folds away, one line each.
    function reserveDetail(g, history) {
        var dl = el('dl', 'reserve-detail tile');
        function item(label, value) {
            if (!value || (Array.isArray(value) && !value.length)) return;
            dl.appendChild(el('dt', null, label));
            var dd = el('dd');
            (Array.isArray(value) ? value : [value]).forEach(function (line) { dd.appendChild(el('div', null, line)); });
            dl.appendChild(dd);
        }
        var m = g.measured || {};
        item(t('detail.scope'), scopeWords(g));
        item(t('detail.left_now'), m.accounts
            ? t('dv.left_now', { windows: windowsLeft(m), accounts: plural(m.accounts, 'measured account'),
                pct: leftPct(m.average_remaining_pct) })
            : t('dv.left_none'));
        if (m.at_limit) item(t('detail.at_limit'), plural(m.at_limit, 'account'));
        var limited = restrictionWords(g);
        if (limited.length) {
            item(t('detail.restricted'), limited.join(' · ')
                + (num(g.unrestricted_windows, 2) ? ' · ' + t('dv.unrestricted', { windows: num(g.unrestricted_windows, 2) }) : ''));
        }
        item(t('detail.not_counted'), coverageWords(g).join(' · '));
        if (g.plans && g.plans.mixed) {
            item(t('detail.plans'), [t('dv.mixed_plans')].concat((g.plans.breakdown || []).map(function (p) {
                return t('dv.plan', { plan: p.plan, windows: num(p.windows, 2), accounts: plural(p.accounts, 'account') });
            })));
        }
        if (g.possible_duplicates) item(t('detail.shared'), t('dv.shared', { n: g.possible_duplicates }));
        var resets = [];
        if (g.next_reset && g.next_reset.at) {
            resets.push(g.next_reset.accounts > 1
                ? t('dv.next_n', { when: formatResetAt(g.next_reset.at), accounts: plural(g.next_reset.accounts, 'account') })
                : t('dv.next', { when: formatResetAt(g.next_reset.at) }));
        }
        if (g.reset_unknown) resets.push(t('dv.no_reset', { n: g.reset_unknown }));
        item(t('detail.resets'), resets.join(' · '));
        if (g.pace_to_reset) {
            item(t('detail.even'), t('dv.even', { rate: num(g.pace_to_reset.windows_per_hour, 3),
                accounts: plural(g.pace_to_reset.accounts, 'account') }));
        }
        item(t('detail.pace'), paceWords(g, history));
        return dl;
    }

    // The limit's details, folded under one summary: the diagnostics behind the
    // timeline, never thrown away. The facts come first; the method — the notes,
    // the data table and the sightings — is one fold more, "How it is counted".
    function detailsBlock(g, chart, sc, ctx, extraNotes, front) {
        var details = el('details', 'chart-table');
        if (detailsOpen) details.open = true;
        details.addEventListener('toggle', function () { detailsOpen = !!details.open; });
        var summary = el('summary', null, t('details.title'));
        summary.setAttribute('data-focus', 'chart-table');
        details.appendChild(summary);
        (front || []).forEach(function (node) { details.appendChild(node); });
        if (chart.mode === 'joint') details.appendChild(el('div', 'meta-line tl-scope', t('detail.selected', { name: ctx.names[g.key] })));
        details.appendChild(reserveDetail(g, ctx.summary.history));
        // The fold keeps its own state, so a redraw leaves it as the reader had it; its summary takes the keyboard.
        var how = el('details', 'chart-how');
        if (howOpen) how.open = true;
        how.addEventListener('toggle', function () { howOpen = !!how.open; });
        var howSummary = el('summary', null, t('details.how'));
        howSummary.setAttribute('data-focus', 'chart-how');
        how.appendChild(howSummary);
        var notes = el('div', 'chart-notes');
        (extraNotes || []).forEach(function (line) { notes.appendChild(el('p', 'meta-line', line)); });
        var history = ctx.summary.history || {};
        var watched = watchSaid(history);
        notes.appendChild(el('p', 'meta-line', recordWords(history) + (watched ? '; ' + watched : '') + ' · ' + t('obs.detail_zone', { zone: tzWords() }) + '.'));
        if (chart.history) {
            notes.appendChild(el('p', 'meta-line', 'History starts each account at its first recorded value; older values are '
                + 'never invented. A dashed segment carries dated readings through missing or stale answers. A '
                + 'reported reset passing keeps its dated pre-reset value, not an assumed refill. Hover or use the '
                + 'arrow keys for the count, age and provenance at a moment. Carried values never teach recent pace.'));
            if (chart.history.membership_note) notes.appendChild(el('p', 'meta-line', chart.history.membership_note));
        } else {
            notes.appendChild(el('p', 'meta-line', 'The record sums every account of this limit with a record in this range ('
                + (chart.past_accounts || 0) + ' of ' + (chart.y_max || 0) + '), whatever its reading now, and stops '
                + 'wherever any of them was not vouched for — an outage, a sweep that did not see it, a reset, a stale '
                + 'reading — never drawing a zero there. The scale is every account the limit applies to.'));
        }
        var clippedBefore = chart.history ? chart.history.clipped_before : chart.past_clipped_before;
        if (clippedBefore) {
            notes.appendChild(el('p', 'meta-line', 'Changes before ' + momentWords(clippedBefore)
                + ' are not drawn: more were recorded in this range than one chart holds.'));
        }
        var points = chart.points || [];
        if (points.length && chart.history) {
            notes.appendChild(el('p', 'meta-line', plural(points.length, 'total') + ' seen at one sweep only are listed '
                + 'separately below. A settled value can be carried in history; unsettled sources supply no replacement value.'));
        } else if (points.length) {
            notes.appendChild(el('p', 'meta-line', plural(points.length, 'total') + ' seen at one sweep only — a reading seen '
                + 'once, or sources that disagree at one sweep — are listed in the table, not drawn: each holds for '
                + 'no stretch of time.'));
        }
        (chart.assumptions || []).forEach(function (a) {
            if (chart.history && (a === chart.history.membership_note || /^History:/.test(a))) return;
            notes.appendChild(el('p', 'meta-line', a));
        });
        how.appendChild(notes);
        how.appendChild(dataTable(chart));
        var seen = sightingsTable(chart);
        if (seen) how.appendChild(seen);
        details.appendChild(how);
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
        box.appendChild(el('p', 'chart-notes', t('table.changes', { n: rows.length, total: recorded })));
        var table = el('table', 'tbl');
        var head = el('tr');
        [t('table.time'), t('table.recorded'), t('table.no_new_use'), t('table.recent_pace'), t('table.event')].forEach(function (h) {
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
                event: row.event === 'now' ? t('table.future_starts') : (row.event || '') });
        });
        rows.sort(function (a, b) { return a.at - b.at; });
        var box = el('div');
        box.appendChild(el('p', 'chart-notes', t('table.changes', { n: recorded.length, total: details.length }) + ' ' + t('table.carried')));
        var table = el('table', 'tbl'), thead = el('thead'), head = el('tr');
        [t('table.time'), t('table.history'), t('table.no_new_use'), t('table.recent_pace'), t('table.context')].forEach(function (text) {
            var th = el('th', null, text); th.setAttribute('scope', 'col'); head.appendChild(th);
        });
        thead.appendChild(head); table.appendChild(thead);
        var body = el('tbody');
        rows.forEach(function (row) {
            var d = row.history;
            var tr = el('tr');
            [momentWords(row.at), d ? (d.value === null ? 'gap' : num(d.value, 2) + ' of ' + d.accounts) : '',
             row.noUse === null ? 'unknown' : num(row.noUse, 2),
             row.pace === null ? 'unknown' : num(row.pace, 2), row.event].forEach(function (text) {
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
        var table = el('table', 'tbl');
        table.appendChild(el('caption', null, t('sightings.caption', { lo: lo + 1, hi: hi, n: points.length })));
        var head = el('tr');
        [t('table.time'), t('table.total'), t('table.what_was_seen')].forEach(function (h) {
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
             unsettled ? t('sightings.unsettled') : num(p[1], 2),
             unsettled ? t('sightings.disagree') : t('sightings.only')
            ].forEach(function (text) { tr.appendChild(el('td', null, text)); });
            body.appendChild(tr);
        });
        table.appendChild(body);
        box.appendChild(table);
        if (pages > 1) {
            var nav = el('div', 'sightings-nav');
            nav.appendChild(sightingsTurn(t('sightings.older'), t('sightings.older_label'), 'sightings:older', 1, sightingsBack < pages - 1));
            nav.appendChild(sightingsTurn(t('sightings.newer'), t('sightings.newer_label'), 'sightings:newer', -1, sightingsBack > 0));
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
                value = b[0] === a[0] || t === b[0] ? b[1] : t === a[0] ? a[1]
                    : a[1] === null || b[1] === null ? null
                    : a[1] + (b[1] - a[1]) * (t - a[0]) / (b[0] - a[0]);
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

    function historyWords(detail, secs) {
        if (!detail || (!detail.accounts && !detail.change)) return [];
        var counts = [t('chart.tip_accounts_recorded', { accounts: plural(detail.accounts || 0, 'account'), measured: detail.measured || 0 })];
        if (detail.carried) counts.push(t('chart.tip_carried_n', { carried: detail.carried }));
        if (detail.unknown) counts.push(t('chart.tip_unknown_n', { unknown: detail.unknown }));
        var words = [counts.join(', ')];
        if (detail.unknown) words.push((detail.reasons || {}).sources_disagree
            ? t('chart.tip_sources_disagree') : t('chart.tip_no_settled'));
        if (detail.carried) {
            var age = typeof detail.age_seconds === 'number' ? detail.age_seconds + Math.max(0, secs - detail.at) : null;
            words.push((age !== null ? t('chart.tip_oldest_age', { age: minutesWords(age) }) : t('chart.tip_oldest_unknown'))
                + (detail.oldest_observed_at ? ' · ' + t('chart.tip_observed', { when: whenWords(detail.oldest_observed_at) }) : ''));
            var origins = Object.keys(detail.origins || {}).map(function (origin) { return originWords(origin); });
            if (origins.length) words.push(origins.join(', '));
        }
        if ((detail.sources || []).length) words.push(t('chart.tip_source', { sources: detail.sources.join(', ') }));
        if (detail.reset_passed) words.push(t('chart.tip_reset_passed', { accounts: plural(detail.reset_passed, 'account') }));
        if (detail.change === 'first_recorded') {
            words.push(detail.added ? t('chart.tip_first_recorded_of', { accounts: plural(detail.added, 'account') })
                : t('chart.tip_first_recorded'));
            if (detail.added && detail.accounts > detail.added) words.push(t('chart.tip_membership_note'));
        }
        if (detail.change === 'membership_changed') {
            var changes = [];
            if (detail.added) changes.push(t('chart.tip_added', { accounts: plural(detail.added, 'account') }));
            if (detail.removed) changes.push(t('chart.tip_removed', { accounts: plural(detail.removed, 'account') }));
            words.push(changes.length ? t('chart.tip_membership_changes', { changes: changes.join(', ') }) : t('chart.tip_membership_note'));
        }
        if (detail.removal_time_unknown) words.push(t('chart.tip_removal_unknown'));
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

    function readoutAt(chart, sc, secs, name) {
        var at = valuesAt(chart, sc, secs);
        var words = at.rows.map(function (r) {
            if (r[0] === 'recorded') {
                return r[1] === null ? t('chart.readout_recorded_gap')
                    : t('chart.readout_recorded', { value: num(r[1], 2), accounts: plural(recordedCount(chart, secs), 'account') });
            }
            return r[1] === null ? t('chart.readout_not_drawn', { name: r[0] })
                : t('chart.readout_scenario_value', { name: r[0], value: num(r[1], 2), accounts: plural(sc.accounts, 'current account') });
        });
        if (!words.length) words.push(at.future ? t('chart.readout_no_future') : t('chart.tip_no_record'));
        var provenance = secs <= chart.now ? historyWords(historyDetailAt(chart, secs), secs) : [];
        return momentWords(secs) + (at.future ? ' · ' + t('chart.readout_scenario') : '') + ' — ' + name + ': ' + words.join(', ')
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

    // Runs that meet end to start (a recorded stretch handing over to a carried
    // one at the same value) are one outline for the fill, so no seam of
    // background shows between them; a gap in the record still breaks it.
    function joinRuns(runs) {
        var sorted = runs.filter(function (run) { return run.length; }).slice().sort(function (a, b) { return a[0][0] - b[0][0]; });
        var out = [];
        sorted.forEach(function (run) {
            var last = out.length ? out[out.length - 1] : null;
            var end = last ? last[last.length - 1] : null;
            if (end && Math.abs(end[0] - run[0][0]) < 0.06 && Math.abs(end[1] - run[0][1]) < 0.06) {
                out[out.length - 1] = last.concat(run.slice(1));
            } else {
                out.push(run.slice());
            }
        });
        return out;
    }

    function runArea(runs, base) {
        return runs.filter(function (run) { return run.length > 1; }).map(function (run) {
            var first = run[0], last = run[run.length - 1];
            return 'M' + first[0].toFixed(1) + ' ' + base.toFixed(1) + 'L'
                + run.map(function (pt) { return pt[0].toFixed(1) + ' ' + pt[1].toFixed(1); }).join('L')
                + 'L' + last[0].toFixed(1) + ' ' + base.toFixed(1) + 'Z';
        }).join('');
    }

    // The width a chart may draw in: the card's, less the card's own padding (capsule: shell); the root's while the
    // drawing has no card. Where the page has no layout to ask (a bare test document), 580.
    function chartRoom() {
        var host = shellBox && shellBox.parentNode && shellBox.parentNode.parentNode === root ? shellBox : root;
        var room = (host && host.clientWidth) || 0;
        if (room && host === shellBox && typeof window.getComputedStyle === 'function') {
            var pad = window.getComputedStyle(host);
            room -= (parseFloat(pad.paddingLeft) || 0) + (parseFloat(pad.paddingRight) || 0);
        }
        return room > 0 ? room : 580;
    }

    // The size of the plot: its width by the room of the card, its height by its width (210, and 180 on a narrow card). drawChart draws to it, and
    // the place that holds the plot while it is read (timelineBlock) takes its height from here, so the two cannot differ.
    function chartSize() {
        var avail = chartRoom();
        var W = Math.max(260, Math.min(1000, Math.floor(avail - 2 * 17)));
        return { W: W, H: W < 460 ? 180 : 210 };
    }

    // One plot: a fixed time axis (the chosen span back and ahead, whole
    // hours, local clock) and a fixed value axis (every account the limit
    // applies to). Behind now the record, as a line with its area, broken
    // wherever it is not vouched for; ahead of now the chosen scenario from
    // the row's own figure, with a faint mark at each reported reset.
    // The unit of the chart's figure: ours in the reader's language, or the skill's own words for it.
    function chartUnitText(chart) {
        return !chart.unit_text || chart.unit_text === 'account-windows left' ? t('chart.unit') : chart.unit_text;
    }

    function drawChart(panel, name, chart, sc) {
        var size = chartSize();
        chartDrawn = size;
        var W = size.W;
        var H = size.H;
        var pl = 30, pr = 12, pt = 22, pb = 24;
        var now = chart.now;
        var t0 = Math.floor(chart.start / 3600) * 3600;
        var t1 = t0 + Math.ceil((chart.end - t0) / 3600) * 3600;
        var ymax = Math.max(1, chart.y_max || 1, chart.history ? chart.history.max_accounts || 0 : 0);
        function x(t) { return pl + (t - t0) / (t1 - t0) * (W - pl - pr); }
        function y(v) { return pt + (1 - v / ymax) * (H - pt - pb); }
        var bottom = H - pb;

        var plot = el('div', 'chart-plot focus-ring');
        plot.setAttribute('tabindex', '0');
        plot.setAttribute('data-focus', 'chart-plot');
        plot.setAttribute('role', 'group');
        plot.setAttribute('aria-label', sc
            ? t('chart.aria_plot_ahead', { name: name, unit: chartUnitText(chart), scenario: scenarioName(scenario).toLowerCase() })
            : t('chart.aria_plot', { name: name, unit: chartUnitText(chart) }));
        var svg = svgCanvas('0 0 ' + W + ' ' + H, W, H);
        svg.setAttribute('class', 'chart-svg');
        // One soft fill under the one curve; the unit is said on the plot itself.
        var defs = svgEl('defs', {});
        var fade = svgEl('linearGradient', { id: 'qa-area', x1: 0, y1: 0, x2: 0, y2: 1 });
        fade.appendChild(svgEl('stop', { offset: '0', style: 'stop-color:var(--calm-a);stop-opacity:.30' }));
        fade.appendChild(svgEl('stop', { offset: '1', style: 'stop-color:var(--calm-a);stop-opacity:.02' }));
        defs.appendChild(fade);
        // The plot area's rounded corners: a clip of the area's rectangle (a line wider each way, so a base line and a cap line keep their whole
        // width), which only the background layers wear - the tint of the future, the lines of the axes and of the days, the marks of the
        // resets. The labels, the ticks, the record and the scenario stand outside it, so nothing of them is cut. The corner is the token
        // --radius-field, read in the stylesheet as the rect's rx; the 10 written here is the same, for an engine that has no CSS rx.
        var fieldClip = svgEl('clipPath', { id: 'qa-field' });
        fieldClip.appendChild(svgEl('rect', { x: pl, y: pt - 1, width: W - pl - pr, height: bottom - pt + 2, rx: 10, 'class': 'field-clip' }));
        defs.appendChild(fieldClip);
        var inField = 'url(#qa-field)';
        svg.appendChild(defs);
        var unitText = svgEl('text', { x: pl, y: 11, 'class': 'unit-text' });
        unitText.textContent = chartUnitText(chart);
        svg.appendChild(unitText);
        svg.appendChild(svgEl('rect', { x: x(now), y: pt, width: Math.max(0, x(t1) - x(now)), height: bottom - pt, rx: 10,
            'class': 'future-bg', 'clip-path': inField }));

        var step = niceStep(ymax);
        for (var v = 0; v < ymax - step * 0.35; v += step) {
            svg.appendChild(svgEl('line', { x1: pl, x2: W - pr, y1: y(v), y2: y(v), 'class': v ? 'grid' : 'grid base', 'clip-path': inField }));
            var lab = svgEl('text', { x: pl - 6, y: y(v) + 3.5, 'text-anchor': 'end', 'class': 'axis-text' });
            lab.textContent = String(v);
            svg.appendChild(lab);
        }
        svg.appendChild(svgEl('line', { x1: pl, x2: W - pr, y1: y(ymax), y2: y(ymax), 'class': 'grid cap', 'clip-path': inField }));
        var capLab = svgEl('text', { x: pl - 6, y: y(ymax) + 3.5, 'text-anchor': 'end', 'class': 'axis-text' });
        capLab.textContent = String(ymax);
        svg.appendChild(capLab);

        var lastLabel = -Infinity;
        var nowRoom = chart.kept ? 48 : 34;
        timeTicks(t0, t1, W - pl - pr).forEach(function (tick) {
            var tx = x(tick.t);
            if (tick.midnight) svg.appendChild(svgEl('line', { x1: tx, x2: tx, y1: pt, y2: bottom, 'class': 'day-rule', 'clip-path': inField }));
            if (Math.abs(tx - x(now)) < nowRoom || tx - lastLabel < 46 || tx < pl + 12 || tx > W - pr - 12) return;
            lastLabel = tx;
            var label = svgEl('text', { x: tx, y: H - 6, 'text-anchor': 'middle',
                'class': 'axis-text' + (tick.midnight ? ' day' : '') });
            label.textContent = tick.midnight ? dateName('d', tick.day) + ' ' + tick.date : pad2(tick.hour) + ':00';
            svg.appendChild(label);
        });

        if (sc) {
            (sc.schedule || []).forEach(function (r) {
                svg.appendChild(svgEl('line', { x1: x(r.at), x2: x(r.at), y1: pt, y2: bottom, 'class': 'reset-line', 'clip-path': inField }));
                svg.appendChild(svgEl('circle', { cx: x(r.at).toFixed(1), cy: bottom.toFixed(1), r: 4.5, 'class': 'reset-dot' }));
            });
        }
        svg.appendChild(svgEl('line', { x1: x(now), x2: x(now), y1: pt, y2: bottom + 4, 'class': 'now-line' }));
        var nowLab = svgEl('text', { x: x(now), y: H - 6, 'text-anchor': 'middle', 'class': 'axis-text now' });
        // A kept chart stays on the time axis of its own answer.
        nowLab.textContent = chart.kept ? t('chart.read_at', { time: clockAt(isoOf(now)) }) : t('chart.now');
        svg.appendChild(nowLab);

        var history = historyRuns(chart, x, y, t0);
        var runs = history.recorded;
        var area = runArea(joinRuns(history.recorded.concat(history.carried)), bottom);
        if (area) svg.appendChild(svgEl('path', { d: area, 'class': 'area-observed' }));
        var d = runPath(runs);
        if (d) svg.appendChild(svgEl('path', { d: d, 'class': 'line-observed' }));
        var carried = runPath(history.carried);
        if (carried) svg.appendChild(svgEl('path', { d: carried, 'class': 'line-carried' }));
        if (sc && sc.line && sc.line.length) {
            // Futures can lose a known zero at an uncalibrated refill. Null
            // breaks the line/fill, just as it breaks the record; y(null)
            // would silently paint a numeric zero and the cursor must not
            // interpolate through that unknown stretch either (lineAt).
            var futureRuns = [], currentRun = null;
            sc.line.forEach(function (p) {
                if (p[1] === null || p[1] === undefined) { currentRun = null; return; }
                if (!currentRun) { currentRun = []; futureRuns.push(currentRun); }
                currentRun.push([x(p[0]), y(p[1])]);
            });
            var path = runPath(futureRuns);
            // A faint fill under the condition, fainter than the record's: ahead of now nothing is observed.
            var futureArea = runArea(futureRuns, bottom);
            if (futureArea) svg.appendChild(svgEl('path', { d: futureArea, 'class': 'area-scenario' }));
            if (path) svg.appendChild(svgEl('path', { d: path, 'class': 'line-scenario' }));
            if (sc.line[0][1] !== null) svg.appendChild(svgEl('circle', {
                cx: x(now).toFixed(1), cy: y(sc.line[0][1]).toFixed(1), r: 5, 'class': 'scenario-start' }));
        }

        var cursor = svgEl('line', { x1: 0, x2: 0, y1: pt, y2: bottom, 'class': 'cursor-line', visibility: 'hidden' });
        svg.appendChild(cursor);
        var dots = {
            recorded: svgEl('circle', { cx: 0, cy: 0, r: 6, 'class': 'cursor-dot line-observed', visibility: 'hidden' }),
            scenario: svgEl('circle', { cx: 0, cy: 0, r: 6, 'class': 'cursor-dot line-scenario', visibility: 'hidden' })
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
        function show(secs, via) {
            // The axis is whole hours; what is drawn is the chosen span.
            secs = Math.max(chart.start, Math.min(chart.end, secs));
            chartCursor = { key: cursorKey, t: secs, via: via };
            var cx = x(secs);
            cursor.setAttribute('x1', cx);
            cursor.setAttribute('x2', cx);
            cursor.setAttribute('visibility', 'visible');
            dots.recorded.setAttribute('visibility', 'hidden');
            dots.scenario.setAttribute('visibility', 'hidden');
            var at = valuesAt(chart, sc, secs);
            // The tip is one line over the point read: when, then for each line drawn its sample, its figure, "of N" and its short name.
            // What more there is to say of the moment (the accounts, the age of a reading, the source) is said in Details and to the screen reader (readoutAt).
            tip.textContent = '';
            tip.appendChild(el('span', 'tip-when', tipMoment(secs, chart.now)));
            var peak = null;
            at.rows.forEach(function (r) {
                var row = el('span', 'tip-row');
                var none = r[1] === null || r[1] === undefined;
                var detail = r[0] === 'recorded' ? historyDetailAt(chart, secs) : null;
                row.appendChild(legendSwatch(r[0] === 'recorded' ? (detail && detail.carried ? 'line-carried' : 'line-observed') : 'line-scenario'));
                row.appendChild(el('span', 'tip-val', none ? (r[0] === 'recorded' ? t('chart.tip_no_record') : t('chart.tip_not_drawn')) : num(r[1], 2)));
                if (!none) row.appendChild(el('span', 'tip-of', t('chart.tip_of', { n: ymax })));
                row.appendChild(el('span', 'tip-name', r[0] === 'recorded' ? (detail && detail.carried ? t('chart.tip_carried') : t('chart.recorded')) : r[0]));
                tip.appendChild(el('span', 'tip-sep', '·'));
                tip.appendChild(row);
                if (!none) {
                    var dot = r[0] === 'recorded' ? dots.recorded : dots.scenario;
                    dot.setAttribute('cx', cx);
                    dot.setAttribute('cy', y(r[1]));
                    dot.setAttribute('visibility', 'visible');
                    peak = peak === null ? y(r[1]) : Math.min(peak, y(r[1]));
                }
            });
            tip.className = 'chart-tip';
            tip.style.display = 'flex';
            // Over the highest point read, centred on the cursor, held inside the plot; where the room over it is short, under the point.
            // With no point read, under the top of the field. The plot's own padding is where the svg stands in it (inset, drop).
            var rect = svg.getBoundingClientRect ? svg.getBoundingClientRect() : null;
            var plotBox = plot.getBoundingClientRect ? plot.getBoundingClientRect() : null;
            var scale = rect && rect.width ? rect.width / W : 1;
            var inset = rect && plotBox ? rect.left - plotBox.left : 0;
            var drop = rect && plotBox ? rect.top - plotBox.top : 0;
            var room = plot.clientWidth || W * scale + 2 * inset;
            tip.style.maxWidth = Math.max(0, room - 2 * inset) + 'px';
            var half = (tip.offsetWidth || 170) / 2;
            tip.style.left = Math.max(inset + half, Math.min(room - inset - half, cx * scale + inset)) + 'px';
            tip.style.top = ((peak === null ? pt : peak) * scale + drop) + 'px';
            var tipBox = tip.getBoundingClientRect ? tip.getBoundingClientRect() : null;
            if (tipBox && plotBox && tipBox.height && tipBox.top < plotBox.top) tip.className = 'chart-tip tip-below';
            readout.textContent = readoutAt(chart, sc, secs, name);
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
                line.appendChild(el('span', 'r', t('acc.lim_none')));
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
                parts.push([t(UNKNOWN_WORDS[bar.why] || 'acc.lim_no_usable'), '']);
            } else {
                if (bar.resets_at) {
                    parts.push([t(bar.state === 'last_known' ? 'acc.lim_reported_reset' : (bar.at_limit ? 'acc.lim_until' : 'acc.lim_resets'),
                        { when: whenWords(bar.resets_at) }), '']);
                }
                if (bar.state === 'last_known') {
                    parts.push([t('acc.lim_last_known', { when: relTime(bar.observed_at) || t('acc.lim_unreported') }), 'warn']);
                } else if (bar.pace && bar.pace.reaches_limit_at) {
                    parts.push([t('acc.lim_pace', { when: whenWords(bar.pace.reaches_limit_at) }), 'est']);
                }
                if (heldNow(bar) && !bar.at_limit) {
                    parts.push([heldFlags(bar).map(flagWord).join(', ') || t('acc.lim_restricted'), 'warn']);
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
            var seen = relTime(quotaObservedAt(account));
            current.appendChild(el('div', 'diag-title muted', seen ? t('acc.windows_now_obs', { when: seen }) : t('acc.windows_now')));
            current.appendChild(windowLines(quota.constraints, account, false));
            parent.appendChild(current);
        }
        staleWindows(account).forEach(function (snap) {
            var block = el('div', 'diag-block quota-last-known');
            var when = relTime(snap.observed_at) || t('acc.lim_unreported');
            block.appendChild(withIcon(el('div', 'diag-title', snap.why ? t('acc.stale_why', { why: snap.why, when: when })
                : t('acc.stale_plain', { when: when })), 'warn', 12));
            block.appendChild(windowLines(snap.constraints, account, true));
            parent.appendChild(block);
        });
    }

    // Where the selected account's one card stands. The parts of the screen
    // offer the slot in drawing order and the first that fits draws it, once:
    // right under the limit whose bar selected the account (before that
    // limit's own timeline, whichever limit is charted); under its row of the
    // account list when a row selected it, or when that limit is no longer
    // drawn (a limit the reader hid, a reading without it); under the list's
    // header when the list is folded. Never at the far end of the screen.
    function inspectorSlot(group, account, facets) {
        var slot = {
            limit: selectionLimit, account: account.key, drawn: false,
            draw: function (parent, ctx) {
                slot.drawn = true;
                renderInspector(parent, group, account, ctx, facets);
            }
        };
        return slot;
    }

    // The words the card says of an account's state (the sr-only word of its dot).
    function toneWord(tone) {
        if (tone === 'ok') return t('state.ready');
        var key = { warn: 'acc.tone_warn', bad: 'acc.tone_bad', muted: 'acc.tone_muted' }[tone];
        return key ? t(key) : (TONE_WORD[tone] || tone);
    }

    // The label of a verification, which the engine names in English: the known ones go through the table, the rest stand as they came.
    var VERIFY_WORDS = { 'Verified live': 'acc.verified_live' };

    function verifyWord(label) {
        return hasOwn(VERIFY_WORDS, label) ? t(VERIFY_WORDS[label]) : label;
    }

    // The one control that opens the account's sheet, whichever card holds it (it is the same state, diagnosticsOpen): a button of the kit's pill that flips it and draws the page anew, and under it,
    // while it is open, the body that `fill` draws. `text` is the button's word, `label` what the screen reader says, `focus` the key that keeps the keyboard on it through the drawing.
    function diagFold(text, label, focus, fill) {
        var fold = el('div', 'insp-diag');
        var toggle = el('button', 'pill-btn' + (diagnosticsOpen ? ' on' : ''), text);
        toggle.setAttribute('type', 'button');
        toggle.setAttribute('aria-expanded', diagnosticsOpen ? 'true' : 'false');
        toggle.setAttribute('aria-label', label);
        toggle.setAttribute('data-focus', focus);
        toggle.addEventListener('click', function (e) {
            e.stopPropagation();
            diagnosticsOpen = !diagnosticsOpen;
            rerender();
        });
        fold.appendChild(toggle);
        if (diagnosticsOpen) {
            var body = el('div', 'insp-diag-body');
            fill(body);
            fold.appendChild(body);
        }
        return fold;
    }

    // The card with no chart over it: a strip (the head; under it the account's share left, its money and its next reset where its budget is read, else the account's own verdict) and everything else of the card
    // (what it says of the account, its resources, readings, diagnostics, the credential) in a fold of its own under one button, shut at the start. diagnosticsOpen holds the fold; the account picked, the
    // family or the pick made for the reader lets it go (selectAccount, pickDecide, the family tab).
    function plainFold(card, head, details, account, verdict) {
        card.appendChild(head);
        var c = cycleOf(account);
        var parts = [];
        if (c && c.state === 'unlimited') {
            parts.push([t('cycle.unlimited') + ' ' + t('cycle.included_usage'), 'insp-sum-lead']);
            if (c.last_known) parts.push([t('acc.lim_last_known', { when: relTime(c.observed_at) || t('acc.lim_unreported') }), 'warn']);
        } else if (c && c.share !== null) {
            parts.push([leftPct(c.share * 100) + '%', 'insp-sum-lead']);
            if (c.scale && c.left !== null && c.limit) {
                parts.push([moneyShort(c.left, c.scale) + ' ' + t('cycle.left_of', { limit: moneyShort(c.limit, c.scale) }), '']);
            }
            if (c.demand && c.scale) parts.push([t('plate.over_plan', { amount: moneyShort(c.demand, c.scale) }), '']);
            if (c.ends_at) parts.push([t(c.left === 0 ? 'acc.lim_until' : 'acc.lim_resets', { when: whenWords(c.ends_at) }), '']);
            if (c.state === 'last_known') parts.push([t('acc.lim_last_known', { when: relTime(c.observed_at) || t('acc.lim_unreported') }), 'warn']);
        }
        if (parts.length) {
            var sum = el('div', 'insp-sum');
            parts.forEach(function (part) { sum.appendChild(el('span', part[1] || null, part[0])); });
            card.appendChild(sum);
        } else {
            // No budget speaks for the account: its own verdict is not put under the fold.
            if (verdict) card.appendChild(verdict);
        }
        card.appendChild(diagFold(t(diagnosticsOpen ? 'acc.less' : 'acc.more'), t('acc.details', { name: account.label }), 'inspector-details', function (body) { body.appendChild(details); }));
        return card;
    }

    // Whether the account reads any limit window at all, a current one or one held as last known. Cursor reports only its budget per billing cycle, so it has none.
    function hasLimitWindows(account) {
        return !!account && (liveWindows(account).length > 0 || staleWindows(account).length > 0);
    }

    // The account's full card as it stands under its row of the list: the head, then everything it says, at once (no strip that rises under the pointer).
    function flatCard(card, head, box) {
        card.appendChild(head);
        card.appendChild(box);
        return card;
    }

    function renderInspector(parent, group, account, ctx, facets) {
        var quota = account.quota || {};
        var overview = !!(ctx && ctx.groups.length);
        // No limit window to draw means no chart over the card (Cursor reports only its budget per billing cycle): the card stands whole under the budget's plate, so it is a strip and a fold (plainFold).
        // The strip speaks of the budget, so it is the card of an account that reads a budget and no window. An account that has windows but no overview to chart them keeps its whole card, and so does one that reads neither.
        var plain = !overview && !hasLimitWindows(account) && !!cycleOf(account);
        var card = el('section', 'inspector');
        var box = el('div', 'kt-pad');
        card.setAttribute('aria-label', t('acc.card_label'));
        var head = el('div', 'split-row insp-head');
        var title = el('div', 'insp-title');
        var tone = accountTone(account, facets);
        // The dot and the name are one pair that never parts on a narrow line (the plan and the next-up mark wrap after them); the name whole is its title, for a name the line cuts.
        var who = el('span', 'insp-who');
        who.appendChild(stateDot(tone, true));
        who.appendChild(el('span', 'sr-only', toneWord(tone)));
        var nameNode = el('span', 'insp-name', account.label);
        nameNode.title = String(account.label);
        who.appendChild(nameNode);
        title.appendChild(who);
        if (account.plan) {
            var chip = el('span', 'plan-chip', planWord(account.plan, group));
            chip.title = account.plan;
            title.appendChild(chip);
        }
        if (account.next_up) title.appendChild(el('span', 'next-up', '· ' + t('acc.next_up')));
        head.appendChild(title);
        var clear = el('button', 'pill-btn insp-clear', t('acc.clear'));
        clear.insertBefore(icon('close', 12), clear.firstChild);
        clear.setAttribute('type', 'button');
        clear.setAttribute('data-focus', 'inspector-clear');
        clear.setAttribute('aria-label', t('acc.clear_label'));
        clear.addEventListener('click', function (e) {
            e.stopPropagation();
            clearAccount();
        });
        head.appendChild(clear);

        var meta = el('div', 'meta-line insp-meta');
        if (account.email && account.email !== account.label) meta.appendChild(el('span', null, account.email));
        if (account.kind !== 'profile') meta.appendChild(el('span', null, t('acc.vendor_cli')));
        if (account.verification && account.verification.label) {
            meta.appendChild(el('span', verificationFailed(account) ? 'meta-bad' : null, verifyWord(account.verification.label)));
        }
        var reason = inactiveReason(account, facets);
        if (reason && reason !== 'failed') meta.appendChild(el('span', null, inactiveWord(reason)));
        var observed = relTime(quotaObservedAt(account));
        meta.appendChild(el('span', 'quota-observed', observed ? t('acc.quota_observed', { when: observed })
            : t('acc.quota_unobserved')));
        box.appendChild(meta);
        var notes = el('div', 'meta-line insp-meta');
        appendHarnessNotes(notes, group, facets);
        if (notes.firstChild) box.appendChild(notes);

        // The account's own verdict where it says something the lines below
        // cannot: a limit reached, a facet unread, no window at all. A cooling
        // account has its one cooldown sentence instead of a second verdict.
        var echoes = quota.state === 'ok' && !!(quota.constraints && quota.constraints.length);
        var verdict = null;
        if (!echoes && !(quota.state === 'cooling' && cooldownsOf(quota, 'account').length)) {
            verdict = renderQuotaVerdict(quota);
            box.appendChild(verdict);
        }
        renderAccountCredits(box, quota);
        renderAccountResources(box, account.resources);
        if (overview) accountLimits(box, ctx, account);
        if (!budgetOnly(account)) renderAbsence(box, account, quota.absence);
        renderCooldowns(box, quota);
        var modelProblems = (quota.constraints || []).filter(function (c) { return isModelWindow(c) && isOut(c); });
        var named = {};
        modelProblems.forEach(function (c) { if (isSpent(c) && c.scope_key) named[c.scope_key] = true; });
        // Brief only with an overview, whose Diagnostics hold the rest; without
        // one there is no Diagnostics, so every report — the passed and unnamed
        // ones too — is shown here.
        renderExhaustions(box, quota, overview, named);
        var aside = staleWindows(account).filter(function (s) { return !!s.why; });
        if (aside.length) {
            var whys = [];
            aside.forEach(function (s) { if (whys.indexOf(s.why) < 0) whys.push(s.why); });
            box.appendChild(withIcon(el('div', 'acct-note', t('acc.fresh_aside', { n: aside.length, why: whys.join('; ') })), 'warn', 12));
        }

        // Without an overview the account's readings are all there is, and are
        // shown whole — everything Diagnostics would hold; with one they wait
        // under Diagnostics.
        if (!overview) {
            accountReadings(box, account);
            if (account.caption) box.appendChild(el('div', 'acct-note muted', t('acc.credential', { caption: account.caption })));
            parent.appendChild(plain ? plainFold(card, head, box, account, verdict) : flatCard(card, head, box));
            return;
        }
        box.appendChild(diagFold(t(diagnosticsOpen ? 'acc.hide_diag' : 'acc.diag'), t(diagnosticsOpen ? 'acc.diag_hide_label' : 'acc.diag_show_label'), 'inspector-diag', function (body) {
            accountReadings(body, account);
            // Every reported model exhaustion, the passed and unnamed ones too.
            var all = el('div');
            renderExhaustions(all, quota, false);
            if (all.firstChild) body.appendChild(all);
            if (account.caption) body.appendChild(el('div', 'acct-note muted', t('acc.credential', { caption: account.caption })));
        }));
        parent.appendChild(flatCard(card, head, box));
    }

    /* ------------------------------------------------------------------
       The account list.
       ------------------------------------------------------------------ */

    // Every account of the family, in the engine's order, with its share left
    // in every limit (from the same bars) and its state; the ones that cannot
    // run anything follow under their reasons. A row selects the account —
    // the same selection a bar makes. `card` (inspectorSlot) is drawn under
    // the selected account's row, or under the header of a folded list.
    function renderAccounts(parent, group, ctx, facets, forceOpen, card) {
        var accounts = group.accounts || [];
        if (!accounts.length) return;
        var groups = ctx ? ctx.groups : [];
        var names = ctx ? ctx.names : {};
        if (ctx && ctx.cycle) {
            // The billing-cycle budget stands in the 30-day place, and as one more column of the list.
            groups = groups.concat([ctx.cycle.group]);
            names = Object.assign({}, names);
            names[ctx.cycle.group.key] = ctx.cycle.name;
        }
        var running = [];
        var idle = [];
        accounts.forEach(function (a) { (inactiveReason(a, facets) ? idle : running).push(a); });
        var open = accountsOpen || forceOpen;
        // The list stands in the reserve's place of the bars, the chart and the card (renderReserve) with the word over the bars as its one control: no head and no toggle of its own, and
        // it stays drawn while it folds shut (ctx.accRun).
        var inPlace = !!(ctx && ctx.listHost);
        var box = el('section', 'accounts');
        box.setAttribute('aria-label', 'Accounts of ' + familyName(group));
        var head = el('div', 'split-row acc-head');
        var summaryText = plural(accounts.length, 'account') + (idle.length ? ' · ' + t('acc.not_running', { n: idle.length }) : '');
        if (inPlace) {
            // Nothing: the word is drawn with the limits.
        } else if (forceOpen) {
            head.appendChild(el('h4', 'title-section acc-title', t('acc.title', { summary: summaryText })));
        } else {
            var toggle = el('button', 'pill-btn acc-toggle' + (open ? ' on' : ''));
            toggle.appendChild(withIcon(el('span', 'pill-icon'), 'caret', 12));
            toggle.appendChild(el('span', null, t('acc.title', { summary: summaryText })));
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
        if (!inPlace) box.appendChild(head);
        if (!open && !(inPlace && ctx.accRun === 'shut')) {
            // Shut, the list is no more than the card of an account no limit drew.
            if (card && !card.drawn) card.draw(box, ctx);
            if (!inPlace || box.firstChild) parent.appendChild(box);
            return;
        }
        var coverBox = null;
        if (inPlace) {
            coverBox = el('div', 'acc-cover-box');
            ((ctx && ctx.cover) || []).forEach(function (line) { coverBox.appendChild(el('div', 'meta-line acc-cover', line)); });
        } else {
            ((ctx && ctx.cover) || []).forEach(function (line) { box.appendChild(el('div', 'meta-line acc-cover', line)); });
        }
        var template = 'minmax(0,1fr)' + (groups.length ? ' repeat(' + groups.length + ',minmax(52px,72px))' : '')
            + ' minmax(96px,128px)';
        var table = el('div', 'acc-table');
        table.setAttribute('role', 'group');
        table.setAttribute('aria-label', t('accounts.table', { name: familyName(group) }));
        var headRow = el('div', 'acc-headrow');
        headRow.style.gridTemplateColumns = template;
        headRow.setAttribute('aria-hidden', 'true');
        headRow.appendChild(el('span', null, t('acc.account')));
        groups.forEach(function (g) {
            var h = el('span', 'acc-cell', names[g.key]);
            h.title = t('accounts.share_hint', { name: names[g.key] });
            headRow.appendChild(h);
        });
        headRow.appendChild(el('span', null, t('acc.state')));
        // In the list's place the head of the columns stands over the body, which scrolls alone.
        var columns = null;
        if (inPlace) {
            columns = el('div', 'acc-th');
            columns.appendChild(headRow);
        } else {
            table.appendChild(headRow);
        }
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
            var nameText = el('span', 'acc-name-text', account.label);
            nameText.title = String(account.label);
            nameCell.appendChild(nameText);
            btn.appendChild(nameCell);
            var said = [account.label, state.word];
            groups.forEach(function (g) {
                var bar = barOf(g, account.key);
                var text = '—';
                var cls = 'acc-cell none';
                var spoken = t('accounts.no_reading', { name: names[g.key] });
                if (bar && bar.state !== 'unknown' && typeof bar.left === 'number') {
                    text = leftPct(bar.left * 100) + '%';
                    cls = 'acc-cell' + (bar.state === 'last_known' ? ' last'
                        : (bar.at_limit ? ' bad' : (heldNow(bar) ? ' held' : '')));
                    spoken = t('accounts.left', { name: names[g.key], share: text }) + (bar.state === 'last_known'
                        ? ', ' + t('accounts.last_read', { when: relTime(bar.observed_at) || t('reading.time_unknown') }) : '');
                } else if (bar && bar.state === 'unlimited') {
                    text = t('cycle.unlimited') + (bar.last_known ? ' · ' + t('card.last_known') : '');
                    cls = 'acc-cell' + (bar.last_known ? ' last' : '');
                    spoken = t('accounts.unlimited', { name: names[g.key] })
                        + (bar.last_known ? ', ' + t('accounts.last_read', { when: relTime(bar.observed_at) || t('reading.time_unknown') }) : '');
                } else if (bar) {
                    text = '?';
                    cls = 'acc-cell unknown';
                    spoken = t('accounts.unknown', { name: names[g.key], why: t(UNKNOWN_WORDS[bar.why] || 'reading.why_not_read') });
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
            btn.setAttribute('data-said', said.join(' · '));
            btn.setAttribute('aria-label', said.join(' · ') + rowPickWords(isOn));
            btn.addEventListener('click', function (e) {
                e.stopPropagation();
                if (inPlace) {
                    // A row of the list picks its account, as its bar does, and the list shuts: the bars and the chart come back with that account picked.
                    accountsSet(false);
                    var onBar = !!ctx.pickGroup && barsOf(ctx.pickGroup).some(function (b) { return b.account === account.key; });
                    if (onBar) selectAccount(account.key, 'bar:' + ctx.pickGroup.key + ':' + account.key, ctx.pickGroup.key);
                    else selectAccount(account.key, 'acct:' + account.key);
                    // A pick played in place draws nothing anew: the list is shut by a drawing of its own.
                    if (accountsRun) rerender();
                    restoreFocus('accounts');
                    return;
                }
                selectAccount(isOn ? '' : account.key, 'acct:' + account.key);
                restoreFocus('acct:' + account.key);
            });
            return btn;
        }
        function put(account) {
            table.appendChild(row(account));
            if (card && !card.drawn && card.account === account.key) card.draw(table, ctx);
        }
        running.forEach(put);
        if (idle.length) {
            table.appendChild(el('div', 'acc-sec', t('acc.not_running_sec')));
            idle.forEach(put);
        }
        if (columns) box.appendChild(columns);
        box.appendChild(table);
        if (inPlace) {
            // The head of the list: Rows on the left, the line of coverage on the right; the columns and the body under it.
            var list = el('div', 'acc-list');
            if (accountsSpace > 0) setVar(list, '--acc-space', accountsSpace + 'px');
            var listHead = el('div', 'acc-list-head');
            if (ctx.rowsNode) listHead.appendChild(ctx.rowsNode);
            if (coverBox.firstChild) listHead.appendChild(coverBox);
            list.appendChild(listHead);
            list.appendChild(box);
            ctx.listHost.appendChild(list);
            return;
        }
        parent.appendChild(box);
    }

    /* ------------------------------------------------------------------
       About: what the screen is made of, and the state of the machine.
       ------------------------------------------------------------------ */

    function stripLegend() {
        var legend = el('div', 'meta-line strip-legend');
        legend.setAttribute('aria-hidden', 'true');
        [['', t('about.lg_share')],
         [' last', t('about.lg_last')],
         [' held', t('about.lg_held')],
         [' spent', t('about.lg_spent')],
         [' unknown', t('about.lg_unknown')]].forEach(function (k) {
            var item = el('span', 'legend-item');
            // A small bar of the plate's own look (.q-bar): the same fill, hatch, base and "?" the bars of the plate wear.
            var cell = el('span', 'q-bar q-swatch' + k[0]);
            if (k[0] !== ' unknown' && k[0] !== ' spent') {
                var f = el('span', 'q-fill');
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
        box.setAttribute('aria-label', t('about.label'));
        var facets = view.facets || {};
        var daemon = view.daemon || {};
        var daemonDown = !!(daemon.state && daemon.state !== 'running');
        box.appendChild(el('div', 'about-title', t('about.system')));
        box.appendChild(el('div', 'meta-line reserve-note', t(view.kept ? 'about.kept' : 'about.answered')));
        var state = el('div', 'system-state');
        var lastRead = relTime(daemon.read_at) || clockAt(daemon.read_at);
        var dRow = daemon.state
            ? dotLabel(t('about.daemon', { state: daemon.state }) + (daemon.engine_version ? ' · ' + daemon.engine_version : ''),
                daemonDown ? 'bad' : 'ok')
            : (daemon.last_state
                ? dotLabel(t('about.daemon_last', { state: daemon.last_state }) + (lastRead ? ' (' + lastRead + ')' : ''), 'muted')
                : dotLabel(t('about.daemon_none'), 'muted'));
        dRow.className = 'dot-label strong';
        state.appendChild(dRow);
        FACET_ORDER.forEach(function (f) {
            var s = facets[f] || 'indeterminate';
            var fname = t('about.facet_' + f);
            state.appendChild(catalogOmitted(view, f)
                ? dotLabel(t('about.catalog_omitted'), 'muted')
                : dotLabel(s === 'ok' ? fname : fname + ' ' + facetWord(s), facetTone(s)));
        });
        // "next up" is a fact about routing: with no verdict on the wire the
        // widget shows no marker, and says so here.
        var mute = (view.groups || []).filter(function (g) { return g.routing_read === false; });
        if (mute.length) state.appendChild(dotLabel(t('about.rotation_mute', { names: mute.map(familyName).join(', ') }), 'warn'));
        box.appendChild(state);
        if (statusProblem) {
            box.appendChild(el('div', 'meta-line reserve-note', t('about.facet_note')));
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
        item(t('about.t_windows'), t('about.b_windows'));
        item(t('about.t_bars'), t('about.b_bars'), stripLegend());
        item(t('about.t_figure'), t('about.b_figure', { note: t('foot.not_read', { n: 0 }).split(' · ')[0],
            scoped: scoped ? ' — ' + t('about.b_figure_scoped', { limit: names[scoped.key], models: scopeWords(scoped).replace(/^models?: /, '') }) : '' }));
        item(t('about.t_timeline'), t('about.b_timeline'));
        item(t('about.t_never'), t('about.b_never'));
        if (summary) {
            var age = readingAge(summary, groups);
            var history = summary.history || {};
            var watched = watchSaid(history);
            item(t('about.t_readings'), age.detail.charAt(0).toUpperCase() + age.detail.slice(1) + '. '
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
    // `withText`: the reading also asks for the text table (the plugin gives it with the view), for the screen whose table did not come by its own route.
    function readView(reuse, withText, mine) {
        var displayAtStart = displaySeq;
        var displayPending = displaySeq !== displaySettled;
        return request(reserveUrl(reuse) + (withText ? '&text=1' : ''), { method: 'GET' }, GET_TIMEOUT_MS).then(function (result) {
            var view = null;
            if (result.ok) {
                try { view = JSON.parse(result.body); } catch (err) { view = null; }
            }
            if (!view || typeof view !== 'object' || Array.isArray(view)) {
                return { view: null, why: result.timedOut ? t('why.timeout', { s: Math.round(GET_TIMEOUT_MS / 1000) })
                    : (result.error ? t('why.bridge', { error: result.error })
                        : (result.ok ? t('why.unreadable') : t('why.http', { status: result.status }))) };
            }
            // An answer that reports itself as not ok normally carries its own
            // reason; with none and nothing to draw it is not a screen.
            var table = view.text;
            delete view.text;
            if (withText && !stopped && mine === generation) adoptI18n(table);
            var unexplained = !view.ok && !view.transport_error && !view.facet_note;
            if (unexplained && !hasContent(view)) return { view: null, why: t('why.incomplete') };
            if (currentView && (displayAtStart !== displaySeq || displayPending || displaySeq !== displaySettled)) {
                view.display = currentView.display;
            }
            return { view: view, why: unexplained ? t('why.incomplete') : '' };
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

    // The mark of a read in the air, set on the screen that stands as a drawing would set it (wordDot, refreshButton, retryButton): the dots of the head blink, and Refresh and Retry wait.
    function busyShow() {
        var dots = rootAll('.btn-word-dot');
        for (var i = 0; i < dots.length; i++) if (!hasClass(dots[i], 'is-busy')) classSet(dots[i], 'is-busy', true);
        var buttons = rootAll('[data-focus="refresh"],[data-focus="retry"]');
        for (i = 0; i < buttons.length; i++) {
            buttons[i].disabled = true;
            if (buttons[i].getAttribute('data-focus') === 'refresh') buttons[i].setAttribute('aria-busy', 'true');
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
        // A read for the chart (reuse) is asked for by the drawing that has just been made (askForChart): the mark is set on that screen (busyShow), and the page is drawn once more only when the chart has come.
        if (reuse) busyShow();
        else rerender();
        var mine = ++generation;
        // The text table is asked for with every ordinary reading (not with a chart or limit
        // switch): the language may have been changed, or a batch of the translation landed.
        Promise.all([readView(!!reuse, false, mine), reuse ? null : readI18n(mine)]).then(function (both) {
            // The table did not come by its own route (an error, nothing, an empty table): the reading is made once more with the table asked for in it, so the numbers are
            // never held back for the words. The plugin answers both routes; where it has no English at all there is nothing to say them with (below).
            if (i18n.en || stopped || mine !== generation) return both;
            return readView(!!reuse, true, mine).then(function (again) { return [again, null]; });
        }).then(function (both) {
            var got = both[0];
            if (mine !== generation || stopped) return;
            inFlight = false;
            if (!i18n.en) {
                drawNoText(true);
                return;
            }
            land(got, got.view ? '' : actionMessage);
        })['catch'](function (err) {
            if (stopped || mine !== generation) return;
            inFlight = false;
            failNow(err);
        });
    }

    function failNow(err) {
        try { console.error('claudexor quotas: reading not handled', err); } catch (e) { /* no console */ }
        actionMessage = t('act.failed_handle', { err: String((err && err.message) || err) });
        rerender();
    }

    function unknownOutcome() {
        return t('act.unknown_outcome');
    }

    // The owner's foreground refresh: one POST, then — once the host says it
    // ran — one read of the whole projection, so the rows, the timeline and
    // the accounts all come from a reading made after it. A POST with no
    // readable answer may still have run: its outcome is unknown, said so,
    // and it is never sent again on its own. If the read after a refresh
    // fails, the screen stays the coherent one it was, said as kept.
    // inFlight is set by every read (the 30 s poll, a return to the page, a
    // chart or limit switch), so it cannot say who asked; `refreshing` is set
    // here alone, for as long as this request is in the air, and it is what
    // the stamp's dot blinks for (windowStamp): a read nobody asked for does
    // not move the screen.
    function refreshQuota() {
        if (stopped || inFlight) return;
        inFlight = true;
        refreshing = true;
        actionMessage = '';
        rerender();
        var mine = ++generation;
        request(REFRESH_ROUTE, { method: 'POST' }, REFRESH_TIMEOUT_MS).then(function (result) {
            if (mine !== generation || stopped) return null;
            function end(message) {
                inFlight = false;
                refreshing = false;
                actionMessage = message;
                rerender();
                return null;
            }
            if (result.timedOut) {
                return end(t('act.timeout', { s: Math.round(REFRESH_TIMEOUT_MS / 1000) }));
            }
            var response = null;
            try { response = JSON.parse(result.body); } catch (err) { response = null; }
            // Only the route's own answer — an object that says ok true or
            // false — can say what happened.
            var readable = !!response && typeof response === 'object' && typeof response.ok === 'boolean';
            if ((readable && response.outcome_unknown === true) || (result.ok && !readable)
                    || result.bodyLost || result.status === 0) {
                return end(unknownOutcome());
            }
            if (!result.ok || !response.ok) {
                return end(response && response.compatibility_error
                    ? t('act.needs_newer')
                    : t('act.refresh_failed'));
            }
            var ranAt = clockAt(new Date().toISOString());
            chartAsked = '';
            return readView(false).then(function (got) {
                if (mine !== generation || stopped) return;
                inFlight = false;
                refreshing = false;
                land(got, got.view ? '' : t('act.after_unread', { time: ranAt }));
            });
        })['catch'](function (err) {
            if (mine === generation) {
                inFlight = false;
                refreshing = false;
            }
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
                ? t('banner.fault_kept', { fault: drawFault })
                : t('banner.fault', { fault: drawFault }));
            // First in the card under the head, like every banner; with nothing kept the card is made here.
            var shell = shellOf(root);
            shell.insertBefore(note, shell.firstChild);
            // The kept screen keeps saying what it is until an answer is drawn.
            if (redrawn) staleMessage = before.stale || t('why.not_drawn');
            return false;
        }
    }

    // The card under the head (capsule: shell): every screen the widget draws stands in one, the head — where there is
    // one — above it. Made here, attached by the drawing.
    function shellCard() {
        shellBox = el('div', 'shell-card');
        return shellBox;
    }

    // The frame (capsule: shell): the card, and before it the rail where the limits have one (render puts it there and marks the frame).
    function shellFrame() {
        var frame = el('div', 'shell');
        frame.appendChild(shellCard());
        return frame;
    }

    // The card standing in `parent` (the screen kept after a failed drawing has one, in its frame); else a new frame with a card, attached to it.
    function shellOf(parent) {
        var nodes = parent.childNodes || [];
        for (var i = 0; i < nodes.length; i++) {
            if (!hasClass(nodes[i], 'shell')) continue;
            var inner = nodes[i].childNodes || [];
            for (var j = 0; j < inner.length; j++) {
                if (hasClass(inner[j], 'shell-card')) {
                    shellBox = inner[j];
                    return inner[j];
                }
            }
        }
        parent.appendChild(shellFrame());
        return shellBox;
    }

    function faultBanner(text) {
        // A failure to draw is an error, so the pale crimson tone (.bad); it looks as every banner does, outside .banners.
        var node = el('div', 'banner bad draw-fault');
        node.setAttribute('role', 'status');
        node.appendChild(withIcon(el('span', 'banner-icon'), 'warn'));
        node.appendChild(el('span', 'banner-text', text));
        node.appendChild(retryButton(true));
        return node;
    }

    // Retry is the ordinary read, asked for now: it never repeats a refresh. In a banner it is the kit's compact button.
    function retryButton(inBanner) {
        var btn = el('button', inBanner ? 'pill-btn btn-compact-wide banner-retry' : 'pill-btn banner-retry', t('btn.retry'));
        btn.setAttribute('type', 'button');
        btn.setAttribute('data-focus', 'retry');
        btn.disabled = inFlight;
        btn.addEventListener('click', function (e) {
            e.stopPropagation();
            load();
        });
        return btn;
    }

    // A banner has one of three tones: 'bad' (an error: the pale crimson), 'warn' (the amber: what is shown is last known) or 'info' (grey).
    function banner(parent, iconName, text, tone) {
        var node = el('div', 'banner ' + tone);
        node.setAttribute('role', 'status');
        node.appendChild(withIcon(el('span', 'banner-icon'), iconName));
        node.appendChild(el('span', 'banner-text', text));
        parent.appendChild(node);
        return node;
    }

    function emptyCard(parent, iconName, title, desc) {
        var card = el('div', 'card empty-card');
        card.appendChild(withIcon(el('div', 'empty-icon'), iconName, 26));
        card.appendChild(el('h4', 'empty-title', title));
        card.appendChild(el('p', 'empty-desc', desc));
        parent.appendChild(card);
        return card;
    }

    // The family on screen: the reader's, while it exists; else the first
    // family with accounts. The selected account stays only while it is
    // still an account of that family. It picks none itself: the top account of the window on screen is picked by renderReserve (pickDecide), before the plate.
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

    // The family tab: the kit's mark tab (markTab) with the head's wiring. The name is the family's short name; the number is the
    // accounts switched on (the ones that can carry work), drawn only when the family has accounts, and the tab's label says how many
    // there are, as the number alone says too little to a reader who does not see the dot.
    function familyTab(group, selected) {
        var isOn = !!(selected && group.harness_id === selected.harness_id);
        var count = (group.accounts || []).length;
        var name = familyName(group);
        var active = (group.accounts || []).filter(function (a) { return a.enabled !== false; }).length;
        var btn = markTab({ key: group.harness_id, mark: familyMark(group), name: name.replace(/ (?:CLI|Code)$/, ''),
            count: count ? active : null, pressed: isOn });
        if (!count) btn.className += ' empty';
        var say = name + ' — ' + (count ? count + (count === 1 ? ' account' : ' accounts') : 'no accounts')
            + (groupTrouble(group) ? ', harness ' + (group.harness_enabled === false ? 'disabled' : group.harness_status) : '');
        btn.setAttribute('aria-label', say);
        btn.setAttribute('data-focus', 'harness:' + group.harness_id);
        btn.title = say;
        btn.addEventListener('click', function (e) {
            e.stopPropagation();
            if (selectedHarness !== group.harness_id) {
                selectedHarness = group.harness_id;
                selectedAccountKey = '';
                diagnosticsOpen = false;
                // Another family is another place: the top account of its window is picked afresh, and its line is drawn (pickDecide).
                pickAuto = false;
                pickBare = '';
                pickAt = '';
                chartCursor = null;
                // The ladder of another family starts with no step lit and its few steps.
                qLit = null;
                qLadderAll = false;
                // The models a family lists are its own: another family starts on "any model".
                modelChoice = '';
                kindChoice = 'text';
                modelMenuOpen = false;
            }
            rerender();
        });
        // No pip for the worst account: one spent account is not the
        // family's state. A harness that is down or off is.
        if (groupTrouble(group)) btn.appendChild(el('span', 'pip warn seg-pip'));
        return btn;
    }

    // The families in the order the head shows them (familyOrder): the ones kept in the order they were seen, the ones not seen yet after them in
    // the order of the answer. A pick moves no tab: the order is the same whichever family is on screen, so the reader finds a family where it was.
    function orderedFamilies(groups) {
        var byId = {};
        groups.forEach(function (group) { byId[group.harness_id] = group; });
        var ids = familyOrder.filter(function (id) { return !!byId[id]; });
        groups.forEach(function (group) { if (ids.indexOf(group.harness_id) < 0) ids.push(group.harness_id); });
        familyOrder = ids;
        return ids.map(function (id) { return byId[id]; });
    }

    // The head's families: the kit's row of mark tabs (markRow), given the place the head wants for it (.harness-seg) and its name.

    function renderHarnessSeg(parent, groups, selected, hasAnswer, facets) {
        var seg;
        if (!groups.length && !hasAnswer) {
            // Before the first answer: three marks shown faint and dead, a shape waiting to be filled — not a claim about the reader's
            // families. They are tabs of the kit as well, disabled and no choice (no aria-pressed): nothing is picked and nothing lights.
            seg = markRow(['codex', 'claude', 'cursor'].map(function (name) {
                var ghost = markTab({ mark: brandIcon(name, FAMILY_MARK_PX), name: name.charAt(0).toUpperCase() + name.slice(1) });
                ghost.disabled = true;
                return ghost;
            }));
            seg.setAttribute('aria-label', 'Reading agent families');
        } else {
            // The families in the order they were seen (the order in the markup is the order to read); the picked one wears its ground.
            seg = markRow(orderedFamilies(groups).map(function (group) { return familyTab(group, selected); }), false);
            seg.setAttribute('aria-label', 'Agent family');
        }
        seg.className += ' harness-seg';
        parent.appendChild(seg);
    }

    function render(view, staleText, actionText) {
        chartDrawn = null;
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

        // A dim is the reader's pointer or keyboard, never the data's: a drawing drops it, so the
        // screen kept after a drawing that failed does not come back wearing one nothing lifts.
        var dimmed = rootAll('[data-dim]');
        for (var d = 0; d < dimmed.length; d++) dimmed[d].removeAttribute('data-dim');

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
        // The right end of the head is two words and a dot between them, "About · Refresh" (the kit's text action): each word is its
        // button alone (wordButton), no icon and no plate, and its capital turns the accent when the word is lit; wordRow draws the row
        // and puts the dot between them (the accent too, out of the reading order, blinking while a read is in the air, inFlight); the
        // row is no group and has no name of its own (the two words are their names). It wears the kit's .btn-word-row and, besides it,
        // the head's own .action-seg, its place. Refresh keeps its word while it waits, disabled and aria-busy, and its capital
        // stays the word's ink. About carries no status: what is wrong is said in the card, not on the button
        // — a daemon that is down, an unread facet and a failed read are banners above everything (2. below), and the status line
        // of the limits says how much of the picked window is read and how old it is (windowStamp). The name of each button is its word.
        var aboutBtn = wordButton('btn.about');
        aboutBtn.setAttribute('aria-expanded', aboutOpen ? 'true' : 'false');
        aboutBtn.setAttribute('aria-controls', 'quotas-about');
        aboutBtn.setAttribute('data-focus', 'about');
        aboutBtn.addEventListener('click', function (e) {
            e.stopPropagation();
            aboutOpen = !aboutOpen;
            rerender();
        });
        var actionSeg = wordRow([aboutBtn, refreshButton()], inFlight);
        actionSeg.className += ' action-seg';
        controlBar.appendChild(actionSeg);
        root.appendChild(controlBar);

        /* The card (capsule: shell): everything under the head — the banners, About, the
           limits, the accounts and the empty states — stands in this one card, in its frame, which
           holds the rail of the limits' windows beside it where there is one. */
        var frame = shellFrame();
        root.appendChild(frame);
        var shell = shellBox;
        railBox = null;

        /* 2. Banners: a daemon that is down and a facet that did not answer
           say so in the open, for every family. */
        var banners = el('div', 'banners');
        if (daemonDown) {
            banner(banners, 'warn', t('banner.daemon', { state: daemon.state }), 'warn');
        }
        var cachedFacets = view.cached || {};
        var cachedNames = FACET_ORDER.filter(function (f) { return !!cachedFacets[f]; });
        if (staleMessage) {
            banner(banners, 'warn', t('banner.stale', { why: staleMessage,
                when: relSince(lastGoodAt) || t('banner.earlier') }), 'warn').appendChild(retryButton(true));
        }
        if (actionMessage) banner(banners, 'warn', actionMessage, 'bad');
        if (view.transport_error && !staleMessage) {
            var lastAt = cachedNames.length ? cachedFacets[cachedNames[0]] : '';
            // The raw error stays off the screen: it can carry local paths.
            banner(banners, 'error', lastAt
                ? t('banner.unread_known', { when: relTime(lastAt) || clockAt(lastAt) })
                : t('banner.unreachable'), 'bad').appendChild(retryButton(true));
        } else if (view.facet_note && facetProblem) {
            var keptFacets = unreadFacets.filter(function (f) { return !!cachedFacets[f]; });
            banner(banners, 'info', keptFacets.length
                ? t('banner.facets_kept', { note: view.facet_note, facets: keptFacets.join(', '),
                    when: relTime(cachedFacets[keptFacets[0]]) || clockAt(cachedFacets[keptFacets[0]]) })
                : t('banner.facets_unread', { note: view.facet_note }), 'info');
        }
        shell.appendChild(banners);

        var summary = view.reserve && view.reserve.summary;
        if (aboutOpen && hasAnswer) {
            renderAbout(shell, view, summary, selection.group ? reserveGroups(view, selection.group.harness_id) : [],
                statusProblem);
        }

        /* 3. The family's limits and the timeline of one; its accounts; the
           selected account's card in the one place that belongs to it
           (inspectorSlot): under the limit whose bar selected it, or under
           its row of the list. */
        var ctx = null;
        var card = selection.account ? inspectorSlot(selection.group, selection.account, facets) : null;
        if (hasAnswer && selection.group) ctx = renderReserve(shell, view.reserve, selection.group, view, card);
        // The pick may have been made by the limits (pickDecide, before their plate): the card of it is the one they hand on, to the list of accounts too.
        if (ctx && ctx.card !== undefined) card = ctx.card;
        // The rail the limits made, if they made one, stands before the card in the frame.
        if (railBox) {
            frame.insertBefore(railBox, shell);
            frame.className = 'shell has-rail';
        }
        if (selection.group && (selection.group.accounts || []).length) {
            renderAccounts(shell, selection.group, ctx, facets, !(ctx && ctx.groups.length), card);
        }
        if (!hasAnswer) {
            emptyCard(shell, 'refresh', t('empty.reading_title'), t('empty.reading_text'));
        } else if (!selection.group) {
            // No family came back at all: not "you have no accounts".
            emptyCard(shell, 'warn', t('empty.nofamily_title'), view.transport_error
                ? t('empty.nofamily_error')
                : t('empty.nofamily'));
        } else if (!(selection.group.accounts || []).length) {
            var card = emptyCard(shell, 'info', t('empty.noaccounts_title', { family: familyName(selection.group) }),
                catalogOmitted(view, 'catalog')
                    ? t('empty.noaccounts_omitted')
                    : t('empty.noaccounts'));
            var notes = el('div', 'empty-notes');
            appendHarnessNotes(notes, selection.group, facets);
            if (notes.childNodes.length) card.appendChild(notes);
        }

        if (focusWas) restoreFocus(focusWas);
        restoreScroll(scrollWas);
        // The bars' grid, from the room the plate leaves them (one read of the layout).
        qBarsFit();
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
        // to the summary of the fold they were in, "How it is counted".
        else if (key.indexOf('sightings:') === 0) restoreFocus('chart-how');
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
        if (!i18n.en) {
            drawNoText(i18n.asked);
            return;
        }
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
        var nodes = rootAll('.chart-tip');
        for (var i = 0; i < nodes.length; i++) nodes[i].style.display = 'none';
        var lines = rootAll('.cursor-line,.cursor-dot');
        for (var j = 0; j < lines.length; j++) lines[j].setAttribute('visibility', 'hidden');
    }

    // Escape closes About, else clears the selected account — each returning
    // the keyboard to the control that opened it.
    function onDocumentKey(e) {
        if (e.key !== 'Escape') return;
        if (modelMenuOpen) {
            modelMenuOpen = false;
            rerender();
            restoreFocus('tl-model');
        } else if (aboutOpen) {
            aboutOpen = false;
            rerender();
            restoreFocus('about');
        } else if (selectedAccountKey) {
            clearAccount();
        } else if (qLit) {
            // No pick to let go: the step lit is what Escape lets go of.
            qLit = null;
            rerender();
        }
    }

    function stop() {
        stopped = true;
        if (chartFitTimer) { clearTimeout(chartFitTimer); chartFitTimer = 0; }
        if (qFitBound && window.removeEventListener) {
            window.removeEventListener('resize', qBarsFit);
            window.removeEventListener('resize', chartFit);
            qFitBound = false;
        }
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
        refreshing = false;
        start();
    });
    document.addEventListener('visibilitychange', function () {
        if (document.visibilityState === 'visible' && !stopped) load();
    });

    start();
})();
