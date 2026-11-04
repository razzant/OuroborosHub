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
    // The pick the widget makes itself (topAccount, pickDecide): the top bar of the window on screen is picked from the first drawing, so the bar is open and red and the
    // chart stands under it at once. `pickAuto` is whether the pick that stands is the widget's own (the reader's is never moved by it); `pickHome` the window ('family|limit') the pick
    // stands in; `pickBare` the window where the reader let the pick go (no pick of the widget's there until the window or the family changes); `pickAt` the window the last drawing
    // was of; `pickDue` the line of the pick (traceStart) is owed once: at the opening, at a pick of a window or a family, never at the poll.
    var pickAuto = false;
    var pickHome = '';
    var pickBare = '';
    var pickAt = '';
    var pickDue = false;
    // What the last drawing was made from (the family, the reserve's context, the facets): a pick on the plate draws the account's card from it
    // with no drawing of the page (selectInPlace). None while a drawing is under way or has failed.
    var drawnFrom = null;
    var accountsOpen = false;
    // The accounts list stands in the place of the bars, the chart and the card (renderReserve, renderAccounts). accountsRun is 'open' or 'shut' for the one drawing that plays the fold
    // after the list was opened or shut (accountsSet), else ''; accountsSpace is the height of the place the list replaces, in px, measured at the moment it opens.
    var accountsRun = '';
    var accountsSpace = 0;
    var diagnosticsOpen = false;
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
    // The picked account's card under the chart: up (the whole card, in the place of the chart) or at rest (a strip). It is up while it is pressed (cardOpen, the pin: it lives through the poll; another
    // bar puts it down, selectAccount, and it is let go at a change of window or family, cardWhere), while the pointer is on it (cardHover) and while the keyboard focus is in it (cardFocus).
    var cardOpen = false;
    var cardHover = false;
    var cardFocus = false;
    // The timers of the pointer's and the focus's going off the card (a beat is kept so the edge does not flicker); the card the script drew last.
    var cardLeave = { hover: 0, focus: 0 };
    var cardLast = null;
    // The height of the place the chart holds in the plate, in px, measured as the card is pressed (cardPress): the body of the pressed card is no taller than it (0: no chart stood there).
    var cardRoom = 0;
    var cardYield = 0;
    var cardWhere = '';
    var chartKeys = {};
    // The window row (capsule: window tabs): the limit last shown in each
    // window length of a family, so a tab opens on the scope the reader left
    // it on; whether the next drawing answers the reader's own pick of a
    // window, scope or family (only that one moves — a redraw of fresh data
    // never does); and the shared underline: where it stood, and the row and
    // tab the drawing in hand wants it under, placed once the row is attached.
    var scopePicks = {};
    var motionPending = false;
    var drawMotion = false;
    var drawElapsed = 0;
    var motionAt = 0;
    // The head's family tabs, which are the kit's mark tabs (kit: mark tab): the family the reader has just left by picking another,
    // kept while that pick's motion runs, so the mark of the family left can start from the look it had as the mark picked starts from
    // the look it had. Only a pick of a family moves the marks: the pick of a window drops it. Whether the tab picked was showing its
    // preview at the click (under the pointer, or with the keyboard on it): its mark was red then, so its start is that red (markLit).
    // Both are the head's to keep and the kit's markStart to read. And the marks of the drawing in hand that wear a start state (a class
    // that holds the look they had a moment ago), which the kit's settleMarks takes off at the next frame.
    var markFrom = '';
    var markLit = false;
    var markStarts = [];
    // The window row's tabs are the kit's number tabs (kit: mark tab, the number is the mark): the window the reader has just left by picking
    // another (its place, as the tab keys it) and whether the tab picked was showing its preview at the click, kept while that pick's motion
    // runs, as the family's are (markFrom, markLit). The head's marks and the window row's never move for each other's pick.
    var winFrom = '';
    var winLit = false;
    // And whether the pick came by a press of the pointer (the mouse button or a finger: the ring of the tab was shrunk to --press at the click):
    // the ring starts from that press, so it goes from the press to its picked size on the spring and does not leap through the preview's size.
    var winPressed = false;
    // The reader's own pick of a family moves the rail in (kit: rail, motion): the family tabs slide to their new places (markWas,
    // markFlip and markSlide), the thread runs from the picked family's mark down to the first ring, the windows come down one after another and the
    // foot of the rail comes last. railPending is set by the pick; the drawing it asks for is inside the movement (railMotion) and so is
    // every drawing within RAIL_MOTION_MS of it (the chart's read), which carries the movement on from where it was (railElapsed). A
    // window's pick, the 30 s poll and Refresh never move it, and nothing moves under reduced motion. The shifts of the tabs (railFlips: by the
    // family's key, how far each tab comes, which one rides, which ones give way) are worked out once, at the pick, from the places the tabs are
    // seen at, and kept until the movement is over: a drawing inside the span puts the same shifts on its new tabs and reads no place.
    var railPending = false;
    var railMotion = false;
    var railElapsed = 0;
    var railMotionAt = 0;
    var railFlips = {};
    // The movement goes on from where it was by the page's own clock when the page has one: the time of the pick on document.timeline (railStart)
    // is kept with the movement, and the animations of the pick and of every drawing inside its span are given it as their start time
    // (clockSet, once per drawing), so a drawing inside the span puts no delay of its own (railShift stays 0) and the animations that
    // replace the ones it removes stand exactly where those stood. A page with no such clock goes on by the delay (--rail-elapsed) alone.
    var railStart = null;
    var railClock = false;
    var railShift = 0;
    // The sweep of the rings (kit: ring, motion): the arc of a ring is drawn from nothing to its share when the reader picks a window (the ring of the
    // window picked alone: sweepWho is its key) or a family (every ring of the rail, each as its row comes down: 'all'). sweepPending is set by the
    // pick; the drawing it asks for wears the sweep (sweepOn), and so does every drawing within SWEEP_MS of it (the chart's read, a second drawing), which
    // carries the sweep on from where it was by the page's own clock (sweepStart: the time of the pick on document.timeline, given to the animations by
    // clockSet) or, with no such clock, by the delay (sweepShift, --sweep-elapsed). The 30 s poll and Refresh never sweep, and nothing sweeps under
    // reduced motion.
    var sweepPending = '';
    var sweepWho = '';
    var sweepAt = 0;
    var sweepStart = null;
    var sweepOn = false;
    var sweepClock = false;
    var sweepShift = 0;
    // After the pick of a family the head's tabs show no preview by the pointer until the pointer moves (kit: mark tab, .tab-mark-still): the
    // tabs slide under a pointer that stands still, and the one that comes under it would turn red as if the reader had pointed at it. The
    // flag lives here, in the widget's memory, so a drawing keeps it (the row is drawn wearing it); the first pointer that really moves takes it
    // off the row (releaseStill). stillAt is where the click was, so a move that is none (the same place) does not count. The keyboard's
    // preview is not touched.
    var stillHold = false;
    var stillRow = null;
    var stillAt = null;
    // The same for the bars: a pick of a bar moves the bars under a pointer that stands where the click was (the bar that was open shuts, the one picked slides from under the pointer), and the bar
    // that comes under it would open by the pointer as if the reader had pointed at it, pushing the bars on its right back and forth. Until the pointer really moves the rows wear .bars-still
    // and the pointer opens nothing (the pick, the keyboard and the ladder's step open as ever). The flag lives here so a drawing keeps it (barStrip); barStillAt is where the click was.
    var barStill = false;
    var barStillAt = null;
    // The order of the head's family tabs (the ids): the family the reader picked last stands first and the others keep the order they were
    // seen in, so a pick moves one tab to the front and the tabs it passes shift one place, all the same way (a pick of a third family does not
    // send the second back to where it was). It lives here, in the widget's memory, and is kept nowhere: a widget just drawn has the family on
    // screen first and the rest in the order the answer lists them.
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
    // The note of the accounts not read (windowFoot) that windowRow made for the plate: renderReserve hands it to the plate it draws (windowPlate, cyclePlate),
    // which puts it under its number row, with the family's credits.
    var railNote = null;
    // The ladder of resets under the rings (kit: rail; ladderOf, ladderNode): the zone windowRow made for the drawing in hand, which render()
    // fits once at its end (ladderFit: the one read of the layout), and whether this drawing has a ladder, so that the plate's row of the
    // next reset (limitRow) wears the mark that hides it on the wide shell (ladderLine is that row, kept to take the mark off again if nothing of the ladder fits).
    var ladderZone = null;
    var ladderDrawn = false;
    var ladderLine = null;
    // The reader's own pick of a window (or of a scope) moves the ladder (kit: rail, motion): the steps that stood fade at once (ladderOut: the
    // node of the old ladder, kept for that pick's drawing alone) and the new ones come down one after another. ladderPending is set by the pick;
    // the drawing it asks for wears the movement (ladderMotion), and so does every drawing within LADDER_MS of it, which carries it on from where it
    // was by the page's own clock (ladderStart, the time of the pick on document.timeline, given to the animations by clockSet) or, with no
    // such clock, by the delay (ladderShift, --ladder-elapsed). The pick of a family moves the ladder with the rail (railMotion); the 30 s poll and
    // Refresh never move it, and nothing moves under reduced motion.
    var ladderPending = false;
    var ladderMotion = false;
    var ladderAt = 0;
    var ladderStart = null;
    var ladderClock = false;
    var ladderShift = 0;
    var ladderOut = null;
    // The ladder open (the word "N more" pressed, and the window came out): its steps all stand in a window that scrolls, and the place of the scroll is kept here
    // (ladderTop), so the 30 s poll, which draws the ladder again, gives the new node the scroll it had. It is open for the limit it was pressed on (ladderKey, against the
    // key of the limit drawn): a pick of a window or of a family, a limit that gives way to another with no pick (a row hidden, an answer that names none) and a drawing with
    // no ladder fold it (ladderFold); so does a drawing with no room for the window, and it does not open again by itself.
    var ladderOpen = false;
    var ladderTop = 0;
    var ladderKey = '';
    // The step of the ladder pressed (kit: rail): {key: the step's data-focus, limit: the key of the limit it stands on, account: the one account of a step of one}, or null. A step of one account
    // is the pick of that account; a step of several only lights their bars (data-step-lit; the others step back) and picks none. It stands until it is pressed again, another step is pressed, any
    // pick of an account (a bar, a row of the list, Clear, Escape) or of a window or a family lets it go (selectAccount, stepClear), or the answer no longer has the step (stepAccounts). It lives
    // here, not in a node: the 30 s drawing builds the tree anew and puts it back (barStrip: data-step-lit, ladderNode: aria-pressed).
    var stepOn = null;
    // The ladder the last drawing made (its node, detached by the next drawing): the one that fades when a pick of a window draws the new one.
    var ladderLast = null;
    // The old ladder kept for the whole span of the movement: a drawing inside the span (the chart's read) puts it over its new ladder again, so the
    // fade is never cut short (it stands at opacity 0 once its animation is over, and the clock of the pick carries the animation on).
    var ladderKept = null;

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
        "--share-track:rgba(var(--neutral-rgb),.07);--calm-a:#ee8a62;--calm-b:#f2a65a;",
        // colour: the accent, the one red of the shell (the sidebar's: the same in both themes); it paints the dot between About and Refresh and the
        // capital of a lit word (kit: text action), and, in the kit's mark tab, the dot and the number of a tab with a mark and the mark of the tab
        // that is picked or previewed, and the dot and the number of the number tab that is picked (the number is its mark)
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
        "--type-meta:12px;--type-body:14px;--type-section:16px;--type-display:28px;--type-figure:44px;--track-meta:.06em;",
        "--weight-light:300;--weight-regular:400;--weight-medium:500;--weight-semibold:600;",
        "--lh-body:1.5;--lh-ui:1.35;--lh-tight:1.15;",
        // size: a row of controls, a compact one, a touch target, the dot of the stamp (the blinking one, and the quiet one between its words), the row of the bars (its height, the gap, the least and the most a bar is wide, the width one opens to and the most it opens to, and the width it has at rest, which the script writes on the row: barsFit; the least height a box keeps while the folds in it go their way, --fold-hold, which the script writes on the box: foldHold; and how far under the top of a fill over 80% the foot of the number stands, --bar-v-in: the number's own line, at --type-ladder, and the step the number keeps off the top of a lower fill, so it stands in the fill as far from its top as it stands over a lower one)
        "--row-h:32px;--row-h-sm:26px;--tap-h:44px;--dot-size:5px;--sep-size:2px;--bar-h:88px;--bar-gap:5px;--bar-min:20px;--bar-max:56px;--bar-open:172px;--bar-rest:var(--bar-max);--fold-hold:0px;--bar-v-in:calc(var(--type-ladder) + var(--space-1h));",
        // space: a ladder of 2 4 6 8 10 12 16 24 (-1h is the half step between 4 and 8, -2h the one between 8 and 12)
        "--space-0:2px;--space-1:4px;--space-1h:6px;--space-2:8px;--space-2h:10px;--space-3:12px;--space-4:16px;--space-5:24px;",
        "--kt-strip:52px;",
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
        // rail: the rail at the side of the card (its width: what a ring needs on the axis of the widest tab of the head's row, --rail-tab-w: the axis of the
        // family picked is the middle of its tab, so at the most it is half of the widest tab off the edge of the rail, and from there the rail holds half of the
        // ring, the growth of the ring that is picked (--ring-grow) and the air at the card (--rail-air); so it is as wide as the rings ask and the same for
        // every family; 114px is the width of the widest tab until the script measures the row (railFit); and the room over its first line), the ring (the box
        // it stands in, its size, and the small one of a capsule on the narrow shell)
        "--rail-tab-w:114px;--rail-air:var(--space-2);--rail-w:calc(var(--rail-tab-w) / 2 + var(--ring-box) / 2 + var(--ring-grow) + var(--rail-air));--rail-top:17.5px;--ring-box:var(--ring-size-lg);--ring-size:28px;--ring-size-lg:47px;--ring-size-sm:22px;",
        // rail rows: a row is as high as its ring, the badge under it and the thread between two rings need. The thread keeps --thread-gap under the
        // badge above (the star hangs a hair under the badge's edge, so it keeps about 1.5px) and --thread-gap from the box of the ring below, whichever
        // ring is picked: a ring that is picked has grown by --ring-grow at its top and at its bottom (the thread over it ends that much higher) and
        // its badge has gone down by --badge-grow (the thread under it starts that much lower). So the thread is the room that is left: --thread-len at rest,
        // less the growth of the ring below, or of the badge above, when one of them is picked. 74px is the least row whose shortest thread (the badge
        // above picked: 14.7 - 6.36 = 8.34px) is not under 8px. The rail's top is such that the box of the first ring (and the thread to it from
        // the head) stands where it stood with the rings of 56px.
        "--rail-row-h:74px;--thread-gap:var(--space-0);--thread-len:calc(var(--rail-row-h) - var(--ring-box) / 2 - var(--badge-drop) - 2 * var(--thread-gap));",
        "--thread-top:calc(var(--thread-gap) + var(--badge-drop) - var(--rail-row-h) / 2);--ring-grow:calc((var(--mark-on) - 1) * var(--ring-box) / 2);--badge-grow:calc((var(--mark-on) - 1) * var(--badge-drop));",
        // the badge in the gap of the ring (kit: ring): drawn in the svg's own units (viewBox 184), so it is the ring's size over 184 for one unit;
        // its outer radius (the circle of 41 and half of the 5 of the stroke: 43.5 units), the height of its centre from the top of the box (the
        // path's lowest point: 92 + 81 = 173 units) and how far its bottom stands under the centre of the box: the thread between two rings starts there
        "--ring-unit:calc(var(--ring-size-lg) / 184);--badge-r:calc(var(--ring-unit) * 43.5);--badge-cy:calc(var(--ring-unit) * 173);",
        "--badge-drop:calc(var(--badge-cy) - var(--ring-size-lg) / 2 + var(--badge-r));--type-badge:7.5px;--type-star:9px;",
        // the share inside the ring of the rail: its size, and the size of the sign (%) after it
        "--type-ring:11px;--type-unit:8px;",
        // the ladder of resets under the rings (kit: rail): the head of it (the hairline on the axis, 22px wide, the air, the word "Resets" at --type-ladder), a step
        // (the thread's room over the bead, the bead, how long to the reset at --type-meta and the amount at --type-ladder, one line each) and the word that
        // counts the steps that did not fit; the heights are such that the lines of the text fit them (the head: 12 + 1 + 8 + one line of 11px at 1.35; a step: 12 + 7 + 4 + 15
        // + 13.75 = 51.75), so the script reads them as they are and reckons how many steps stand in the room the rail leaves. The bead grows to --ladder-bead-grow under
        // the pointer or the keyboard; at rest it is the tertiary grey (the ink at a strength, it turns with the theme), and the first step's, the nearest reset, is the crimson ink.
        "--ladder-title-h:36px;--ladder-step-h:52px;--ladder-more-h:30px;--ladder-bead:7px;--ladder-bead-grow:1.4;--ladder-hair:22px;--type-ladder:11px;",
        "--ladder-bead-rest:rgba(var(--neutral-rgb),.38);",
        // the open ladder: the window of the steps is --ladder-window steps high (the half of the next one under the haze says there are more), the haze at its edges --ladder-haze
        "--ladder-window:5.5;--ladder-haze:22px;--ladder-haze-ink:#000;",
        // the haze itself: the mask of the window of steps (down: the whole, at its start, at its end) and of the row of bars (the same, sideways), each written once and worn by its rule
        "--ladder-haze-y:linear-gradient(to bottom,transparent 0,var(--ladder-haze-ink) var(--ladder-haze),var(--ladder-haze-ink) calc(100% - var(--ladder-haze)),transparent 100%);--ladder-haze-y-start:linear-gradient(to bottom,var(--ladder-haze-ink) 0,var(--ladder-haze-ink) 0,var(--ladder-haze-ink) calc(100% - var(--ladder-haze)),transparent 100%);--ladder-haze-y-end:linear-gradient(to bottom,transparent 0,var(--ladder-haze-ink) var(--ladder-haze),var(--ladder-haze-ink) 100%,var(--ladder-haze-ink) 100%);--ladder-haze-x:linear-gradient(to right,transparent 0,var(--ladder-haze-ink) var(--ladder-haze),var(--ladder-haze-ink) calc(100% - var(--ladder-haze)),transparent 100%);--ladder-haze-x-start:linear-gradient(to right,var(--ladder-haze-ink) 0,var(--ladder-haze-ink) 0,var(--ladder-haze-ink) calc(100% - var(--ladder-haze)),transparent 100%);--ladder-haze-x-end:linear-gradient(to right,transparent 0,var(--ladder-haze-ink) var(--ladder-haze),var(--ladder-haze-ink) 100%,var(--ladder-haze-ink) 100%);",
        // motion: the ladder when a window is picked (kit: rail, motion): the steps that stood fade in --dur-ladder-out, the new ones come down from --ladder-at, a row
        // after another (--rail-row-step); --ladder-elapsed is what the script sets where a drawing inside the movement has no clock of the page to go on by
        "--dur-ladder-out:120ms;--ladder-at:100ms;--ladder-elapsed:0ms;--j:0;",
        // the star of a window not read whole: on the wide rail it stands on the axis, a bead on the lower edge of the badge: the middle of its glyph
        // --star-rise over the edge (so it hangs about half a pixel under it), the top of its box such that the middle of the glyph is there (the middle
        // of the star is .38 of its font size under the top of a box of one line, in the system face)
        "--star-rise:1.2px;--star-top:calc(var(--badge-cy) + var(--badge-r) - var(--star-rise) - var(--type-star) * .38);",
        // the room a share keeps at the end of a capsule on the narrow shell (the widest it is: 100%)
        "--share-w:32px;",
        // axis: the one vertical of the shell, from the left edge of the head and of the rail: the middle of the whole tab of the family picked (the
        // first tab of the head, which stands at the edge of the widget, as far from it as from the top: --shell-card-gap), and the rings of the rail,
        // the thread from the head, the ladder and its seams stand on it (the head and the rail derive their air from it). The script puts the middle
        // of the tab picked here (railFit); until it does the axis is the middle of the widest tab (--rail-tab-w, halved). The
        // size of a family's mark (FAMILY_MARK_PX in the script), the height of the head's line, and the thread that runs down the axis from the
        // head to the first ring: it starts a hair under the mark (picked, so grown by --mark-on) and ends a hair over the ring's box
        "--rail-axis:calc(var(--rail-tab-w) / 2);--mark-px:14px;--head-h:var(--row-h);",
        // the ladder of the rail stands on the axis, as wide as the rail leaves round it: the same at both sides, and --ladder-air short of the card where the axis is
        // nearer the card than the middle of the rail (a wide tab's), so a step, the outline of its focus and its words are never cut at the rail's edge
        "--ladder-air:var(--space-1);--ladder-w:calc(min(var(--rail-axis), var(--rail-w) - var(--rail-axis) - var(--ladder-air)) * 2);",
        "--lead-top:calc(var(--mark-px) * var(--mark-on) / 2 + var(--space-0) - var(--shell-card-gap) - var(--head-h) / 2);",
        "--lead-end:calc(var(--rail-top) + (var(--rail-row-h) - var(--ring-box)) / 2 - var(--thread-gap));",
        // axis: where the thread ends over a first ring that is picked: the ring's box grows to --mark-on about its centre, so its top has gone up
        // by half of the growth, and the thread ends that much higher (the same hair over the ring as when it is not picked)
        "--lead-end-on:calc(var(--lead-end) - var(--ring-grow));",
        // colour: the ring and the thread between two rows of the rail are the ink at a strength (built on --neutral-rgb, they turn with the
        // theme): the track, the arc and the dot of a ring, the arc and the dot of the one picked, the thread
        "--ring-track:rgba(var(--neutral-rgb),.16);--ring-arc:rgba(var(--neutral-rgb),.55);--ring-dot:rgba(var(--neutral-rgb),.72);",
        "--ring-arc-on:rgba(var(--neutral-rgb),.82);--ring-dot-on:rgba(var(--neutral-rgb),.9);--rail-thread:rgba(var(--neutral-rgb),.16);",
        // colour: a step darker for a row under the pointer (not picked): the arc, the dot and the edge of the badge, between the rest and the picked
        "--ring-arc-hover:rgba(var(--neutral-rgb),.68);--ring-dot-hover:rgba(var(--neutral-rgb),.8);",
        // colour: the edge of the badge in the gap of the ring: stronger than the track (the ground inside is the page's own, so a track's tone does not read
        // on a dark one) and quieter than the arc; the light theme sets its own below
        "--ring-badge-edge:rgba(var(--neutral-rgb),.3);--ring-badge-edge-hover:rgba(var(--neutral-rgb),.44);",
        // colour: the app's own (ui.css): the capsule of a window on the narrow shell, its elevation at rest, and the crimson tone of the capsule picked
        "--surface-capsule:rgba(44,34,52,.62);--elev-rest:0 1px 2px rgba(0,0,0,.5),0 8px 24px rgba(0,0,0,.35);",
        "--tone-crimson:rgba(240,90,106,.18);--tone-crimson-ink:#ffa0aa;",
        // line: the hairline of an edge and the ring of the focus (--ring-inset: the ring drawn inside the box, for a part whose neighbours clip what is outside it)
        "--border-w:1px;--ring-w:2px;--ring-offset:2px;--ring-inset:calc(var(--ring-w) * -1);--focus-line:var(--ring-w) solid var(--focus-accent-border);",
        // motion: durations from the shortest and the curve
        "--dur-1:120ms;--dur-2:140ms;--dur-4:180ms;--dur-5:220ms;",
        "--ease:cubic-bezier(.2,.8,.2,1);",
        // motion: the bars' opening (kit: bars): the basis, the share of the free room and the most of a bar move in --dur-bars on --ease-bars, a longer and more even run than --dur-5 on --ease, so the pair that shuts and opens does not leap in its first frames
        "--dur-bars:300ms;--ease-bars:cubic-bezier(.4,0,.2,1);",
        // motion: the blink of a dot, as the sidebar's dots blink while it checks: it dims to --blink-dim and back in --dur-blink (.15, .7s)
        "--dur-blink:700ms;--blink-dim:.15;",
        // motion: the step the other kinds of bar take back to, while a kind of the legend is held (--dim-others)
        "--dim-others:.35;",
        // motion: the sidebar's spring and the time of the ring that grows when its row is picked
        "--nav-spring:cubic-bezier(.34,1.5,.64,1);--dur-nav:500ms;",
        // motion: the press of a row of the rail (the app's: a state changes colour or size, never the height): under the finger or the button the ring
        // shrinks to --press of its size in --dur-1 and comes back on the spring (--nav-spring, --dur-nav) when it is let go
        "--press:.94;",
        // motion: the reader's pick of a family (kit: rail, motion): the tabs slide (--dur-flip on --ease-flip: a soft start, so no tab leaps in
        // its first frames, and no bounce on a long way; the sine's in-out, whose 70% of the way is at about 280 ms of the 450; the tabs that give
        // way dim to --yield-dim in a quarter of that time, stay so to its four fifths and come back), the thread from the
        // mark down to the first ring is drawn (--dur-rail-thread, from --rail-thread-at: when the tabs have gone about 70% of the way), the windows
        // come down (--dur-rail-row, from --rail-row-at, --rail-row-step apart), and the note under them (the accounts not read) and the foot of the
        // rail fade in after the last one (--dur-rail-foot); all of it ends within RAIL_MOTION_MS (1240 ms) in the script
        "--dur-flip:450ms;--ease-flip:cubic-bezier(.37,0,.63,1);--yield-dim:.3;--dur-rail-thread:200ms;--rail-thread-at:280ms;",
        "--dur-rail-row:280ms;--rail-row-at:380ms;--rail-row-step:70ms;--dur-rail-foot:240ms;",
        // motion: the arc of a ring that is picked is drawn from nothing to its share (kit: ring, motion): --dur-sweep on --ease-sweep, a soft landing
        // with no rebound on the arc; the dot at the end of the arc runs round the ring with it in the same time and on the same curve
        "--dur-sweep:650ms;--ease-sweep:cubic-bezier(.3,.5,.2,1);",
        // motion: the line of a pick (capsule: chart): the stem runs from the foot of the picked bar to the chart's plate, then the two arms run round the plate's edge and meet
        // opposite the stem; the two times together (--dur-trace-stem, --dur-trace-arms) are the time of the whole line, which the script shares out by the lengths (on its own svg) so that
        // the line runs at one speed, on --ease-trace (linear). --trace-w is the line's width, the one width of the picked bar's ring, the stem and the plate's outline (--shadow-trace: that
        // width as a ring drawn inside); the script (traceWidth) puts it to whole device pixels on the root
        "--dur-trace-stem:160ms;--dur-trace-arms:320ms;--ease-trace:linear;--trace-w:1.5px;--shadow-trace:inset 0 0 0 var(--trace-w) var(--accent);",
        // motion: the stand-ins inside the plate while its line is drawn (capsule: chart): they dim to --skel-dim and back in --dur-skel (skel-pulse, kit: motion)
        "--dur-skel:1100ms;--skel-dim:.45;",
        // layer: the tab that rides to the front stands over the tabs it passes while it slides
        "--z-flip:1;--z-plate:2;--z-card:1;",
        // motion: what the script sets on a node's own style for it (the row's number, the count of rows, how far a drawing inside the span
        // has gone, the place a tab slides from: worked out once, at the pick, and the look a tab that gives way starts dimming from when it is
        // a second pick inside the movement), declared here at their rest values
        "--i:0;--rail-rows:3;--rail-elapsed:0ms;--flip-x:0px;--flip-y:0px;--yield-from:1;--kt-room:none;--kt-gap:var(--space-2);--acc-space:auto;",
        // motion: and what the script sets on a ring that sweeps (the share it sweeps to, the time it waits, how far a drawing inside the span has
        // gone), declared at their rest values
        "--ring-share:0;--sweep-at:0ms;--sweep-elapsed:0ms;",
        // motion: the picked tab's model mark, a little bigger: the scale it grows to (the kit's mark tab: .tab-mark-icon, the picked one and the one just left)
        "--mark-on:1.2;--mark-hover:1.12}",
        ":root[data-theme=light]{color-scheme:light;--surface:#fff;--inset:#f3f3f5;",
        "--text-primary:#26262b;--text-secondary:#626977;--text-meta:#60606b;--text-disabled:#90909a;",
        "--edge:#e2e2e7;--edge-strong:#91919d;--edge-idle:#8a8a96;--neutral-rgb:38,38,43;--ring-badge-edge:rgba(var(--neutral-rgb),.26);--ring-badge-edge-hover:rgba(var(--neutral-rgb),.4);",
        "--status-ok:#38704f;--status-warn:#845713;--status-warn-bg:#fbf5e9;",
        "--status-bad:#a73c49;--status-bad-bg:#fcf0f1;",
        "--focus-accent-border:#b62f42;--share-ink:#787884;--scenario-ink:#365eac;--calm-a:#cf5636;--calm-b:#d9822b;",
        "--shell-bg:#f5f6f8;--shell-card-bg:#fff;--shell-card-shadow:-8px 0 22px rgba(24,16,32,.07),0 0 12px rgba(24,16,32,.055);",
        "--surface-capsule:#fff;--elev-rest:0 1px 2px rgba(28,33,51,.05),0 6px 18px rgba(28,33,51,.06);",
        "--tone-crimson:rgba(201,53,69,.1);--tone-crimson-ink:#b52b3b;",
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
        // The quiet hover of an option: the ink comes forward on a faint tint (.hover-quiet, worn by a quieter option).
        ".hover-quiet:hover{color:var(--text-primary);background:var(--tint-2)}",
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
        // --tap-h --radius-sm --space-0 --space-2 --dur-5 --ease. It writes no colour and no size of its own.
        // Accessibility: the name of the button is its word, so no aria-label and no title (the word is seen); the span of the capital
        // has no attribute, so a screen reader reads the whole word; the dot is aria-hidden; the button is 26px high and 44 by 44 under
        // a finger (kit: touch); its ring is the base ring.
        // Not to do: no icon, no plate, no border, no colour of its own (within the text action the accent is the dot's and the lit capital's, nothing else),
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
        "@media(prefers-reduced-motion:reduce){.btn-word,.btn-word-cap{transition:none}}",
        /* kit: mark tab */
        // The mark tab. What it is: a button that is one choice of a row, told by its mark: the model's mark (a drawing, or the initial where
        // there is none), its name and, when it has a number, a small dot and that number. It is no plate: no border, no ground and no shadow
        // in any state, and its text keeps the meta size and the regular weight in every one, so a pick changes no word. It has a second kind,
        // the number tab: no mark of a model, its number (with the dot before it) is its mark, told by its colour alone: it never grows.
        // When to use it: for a row of choices that each have a mark and of which one is picked, where the pick must be seen at a glance and
        // must move no word (the head's families). Never for a command (that is a button with a plate, .pill-btn, or a text action), and never
        // for a row of plain words (that is the tabs, or the segment). The number tab is for a row of tabs that have no model to draw but
        // each a figure that is what the tab tells (the windows of a family's limits, each with the average left): the figure is the mark.
        // How to use it: markTab({key, mark, name, count, pressed}) makes each tab (key: what markStart finds it by; mark: a node of the
        // caller's; name: the text; count: a number, which draws the dot and the number, or none; pressed: true or false for a choice, none for
        // a tab that is no choice) and markRow(tabs) the row of them. The number tab (the rail's) is made by markTab({key, name, count, note, selected, ring})
        // (no mark, a ring of ringGauge, and a tab with neither is none of the kit's: count, a number or a short text such as 68% or a dash, is the mark; note: a sign that stands after the number, aria-hidden,
        // such as * for a figure that rests on a part of what it counts; selected: true or false for a tab of a tablist, role=tab and
        // aria-selected) and its row by markRow(tabs, true), a tablist. When the reader picks a tab, markStart(row, fromKey, toKey, lit) tells the
        // row which tab was left and which was picked (lit: whether the one picked was showing its preview at the click, which
        // markPreviewed(tab) says; and pressed: whether the pick came by a press of the pointer, which markPressed(event) says: a rail row's ring then
        // starts from the press and not from the preview's size), and settleMarks() ends it at the next frame. The helpers stand together in widget.js and nothing else
        // builds a tab, a mark, a dot, a number or a row, so the classes below (.tab-mark, .tab-mark-icon, .tab-mark-name, .tab-mark-dot,
        // .tab-mark-count, .tab-mark-row, .tab-mark-num, .tab-mark-note and the start states .tab-mark-was-off, .tab-mark-was-lit, .tab-mark-was-press,
        // .tab-mark-was-on) are worn through them.
        // A part adds its wiring to a tab it gets (aria-label, title, data-focus, aria-controls, the click) and its place to a row (the head's
        // .harness-seg), never a look.
        // States: at rest the mark is in the tab's ink (--text-primary), at its size. Previewed, a tab not picked under the pointer or with
        // the keyboard on it (:hover, :focus-visible; not when it is disabled) shows the same mark in --accent, not grown: a promise of the pick.
        // Picked (aria-pressed=true) the mark is in --accent and grown to --mark-on. The dot and the number are in --accent in every state.
        // A row that is still (.tab-mark-still, worn through markRow's third argument) shows no preview by the pointer: a pick has moved its
        // tabs under a pointer that stands still, and the tab that has come under it must not turn red as if it were pointed at. The part takes
        // the class off at the first real move of the pointer; the keyboard's preview (:focus-visible) is never held.
        // The number tab is the other way round: its name is in --text-primary, the ink, in every state, and its dot and number tell the pick
        // alone: at rest they are in --text-meta; previewed (not picked, :hover or :focus-visible, not aria-disabled) they go to --text-primary,
        // not red; picked (aria-selected=true) they are in --accent. Its note (the star) is in the ink and the semibold weight at rest and
        // previewed, and in --accent when the tab is picked, with the number. Nothing grows. A tab that is aria-disabled previews nothing.
        // Its text never wraps (white-space: nowrap): a name does not break at its hyphen, and a row with no room wraps whole tabs (markRow).
        // Motion: the mark's colour and size fade together by the mark's own transition (--dur-5 on --ease), and nothing else of the tab
        // moves. A pick redraws the row, so a new mark has no look to fade from: markStart draws the two marks that change in the look they
        // had a moment ago (a start state: the mark picked in the tab's ink at its size, or in the preview's red at its size when the tab was
        // previewed, so that only the size moves; the mark left in the picked look), and the next frame takes the class off after a read of
        // the look, which the browser holds as the look the change starts from. It is a start state and a transition, never keyframes: the
        // canon lets a keyframe move transform and opacity alone (kit: motion), so a keyframe could not fade the red. Under reduced motion
        // nothing fades and nothing moves (the picked mark is simply drawn at its size) and no start state is drawn.
        // The number tab fades its number and its note in colour alone (--dur-5 on --ease) and starts a pick from the same classes on
        // the number and on the note (markStart puts them): the one picked from its quiet look (was-off) or, when it showed its
        // preview, from the ink (was-lit), the one left from the picked red (was-on). Its ring starts from the preview's size or, for a press of the
        // pointer, from the press (kit: rail).
        // Tokens it reads: --text-primary --text-meta --accent --mark-on --type-meta --lh-ui --weight-regular
        // --weight-semibold --row-h --tap-h --radius-sm --space-1 --space-1h --dur-5 --ease. It writes no colour of its own and no size off
        // the scale (the 5px between a tab's parts is the tab's own air, the one literal).
        // Accessibility: the pick is aria-pressed (the colour tells nothing alone) and the row is a group, which the part gives a name; a tab
        // that has a number carries an aria-label that says what it counts ("Claude — 2 accounts": the part's wiring), so the bare number is
        // not read as it is seen, and the dot is aria-hidden; the tab is 32px high, 44px under a finger (kit: touch); its ring is the base
        // ring; a tab that is disabled previews nothing. The number tab is a tab of a tablist (role=tab, aria-selected) and its row a tablist
        // the part names; the tab's own wiring (aria-controls, aria-describedby, the roving tabindex, the arrow keys) is the part's, and so
        // is the aria-disabled of a place with nothing to tell (a tab with no number).
        // Not to do: no plate, no border, no shadow and no ground in any state; no change of the text's weight or size on a pick or a
        // preview (only the mark moves); no colour of its own (within the mark tab the accent is the mark's, the dot's and the number's,
        // nothing else); no dot without a number; no keyframes; no start state drawn by the part; the number tab changes colour only, never
        // its size or its weight, and shows the red on a pick alone, never on a preview.
        // The row (markRow): a flex line, the tabs centred on each other with a small gap; it may wrap, so a row with no room does not run
        // out of its place. A part gives it its place (margin, width, the room it asks for in a bar) and nothing else.
        ".tab-mark-row{display:flex;align-items:center;flex-wrap:wrap;gap:var(--space-1)}",
        // The tab: an inline flex line of its parts, 5px between them, the row's height and a little air sideways; no border and no ground
        // (so no state can fill them), the ink, the size and the regular weight of its text. The ring is the base's.
        ".tab-mark{display:inline-flex;align-items:center;gap:5px;min-height:var(--row-h);padding:var(--space-1) var(--space-1h);border:0;border-radius:var(--radius-sm);background:none;appearance:none;color:var(--text-primary);font-size:var(--type-meta);font-weight:var(--weight-regular);line-height:var(--lh-ui)}",
        // The mark (.tab-mark-icon: the model's drawing, or its initial where the widget has no drawing) is in the tab's ink. Picked
        // (aria-pressed=true), it turns the accent and grows to --mark-on; a tab not picked shows the same red, without the growth, under
        // the pointer or the keyboard, as a preview of the pick (a tab that is disabled shows none). Nothing else of the tab moves: not the
        // name, not the number. The colour and the size fade together, by the mark's one transition (--dur-5 on --ease). The reader's own
        // pick redraws the row, so a new mark has no look to fade from: markStart draws the two marks that change in the look they had a
        // moment ago (the start state) and settleMarks takes that off at the next frame, so the transition carries each to its look now.
        // The mark of the tab picked wears .tab-mark-was-off (the tab's ink, its size; heavier than the picked rule, so it wins by weight),
        // or .tab-mark-was-lit when the tab was already showing its preview at the click (its mark was red then: the start is that red at
        // the tab's size, so only the size moves and the mark does not go through the ink), and the mark of the tab left wears
        // .tab-mark-was-on (the picked look, in one rule with the picked mark). The classes hold a look and move nothing; there are no
        // keyframes. Under reduced motion nothing fades and nothing grows, and no start state is drawn (the last rule of this section).
        ".tab-mark-icon{transition:color var(--dur-5) var(--ease),transform var(--dur-5) var(--ease)}",
        ".tab-mark[aria-pressed=true] .tab-mark-icon,.tab-mark-icon.tab-mark-was-on{color:var(--accent);transform:scale(var(--mark-on))}",
        // The preview by the pointer is off in a row that is still (.tab-mark-still: a pick has moved its tabs under a pointer that stands
        // still, and the first move of the pointer takes the class off); the keyboard's preview is not.
        ".tab-mark:focus-visible:not([aria-pressed=true]):not(:disabled) .tab-mark-icon,.tab-mark-row:not(.tab-mark-still) .tab-mark:hover:not([aria-pressed=true]):not(:disabled) .tab-mark-icon{color:var(--accent)}",
        ".tab-mark[aria-pressed=true] .tab-mark-icon.tab-mark-was-off{color:var(--text-primary);transform:none}",
        ".tab-mark[aria-pressed=true] .tab-mark-icon.tab-mark-was-lit{color:var(--accent);transform:none}",
        // The dot between the name and the number (drawn only with a number, out of the reading order) and the number: the accent. The
        // dot is the semibold weight, the number the regular one on tabular digits.
        ".tab-mark-dot{color:var(--accent);font-weight:var(--weight-semibold)}",
        ".tab-mark-count{color:var(--accent);font-weight:var(--weight-regular);font-variant-numeric:tabular-nums}",
        // The number that stands after the dot (a tab with a mark; the ring's share has no dot) holds the room of two digits (tabular, so a digit is one width) whatever it
        // counts: the tab, and the rail's width and axis that railFit reads from the tabs, do not move when the count goes from 9 to 10; the digits stand in the middle of that room.
        ".tab-mark-dot+.tab-mark-count{min-width:2ch;text-align:center}",
        // The number tab (.tab-mark-num: no mark, the number is it; it stands in the rail's ring, no dot). Its name is the ink (the tab's own --text-primary) in every state: the
        // pick is told by the number alone. It is --text-meta at rest and turns --accent when it is picked. A tab not picked
        // shows, under the pointer or the keyboard (not when it is aria-disabled), the ink on its number, never the red: the red is
        // the pick's alone. Nothing grows: only the colour moves, by the number's own transition (--dur-5 on --ease), from the
        // start state of a pick (was-off: the quiet look; was-lit: the preview's ink; was-on, on the tab left: the picked red), which wears
        // the classes of the mark's and is heavier than the picked rule, so it wins by weight. The text of the tab never wraps (white-space:nowrap), so the tab keeps its height; a row with no room wraps
        // whole tabs, which the row's own flex-wrap does.
        ".tab-mark-num{white-space:nowrap}",
        ".tab-mark-num .tab-mark-count{color:var(--text-meta);transition:color var(--dur-5) var(--ease)}",
        ".tab-mark-num[aria-selected=true] .tab-mark-count,.tab-mark-num .tab-mark-count.tab-mark-was-on{color:var(--accent)}",
        ".tab-mark-num:is(:hover,:focus-visible):not([aria-selected=true]):not([aria-disabled=true]) .tab-mark-count{color:var(--text-primary)}",
        ".tab-mark-num[aria-selected=true] .tab-mark-count.tab-mark-was-off{color:var(--text-meta)}",
        ".tab-mark-num[aria-selected=true] .tab-mark-count.tab-mark-was-lit{color:var(--text-primary)}",
        // The note after the number (.tab-mark-note, the star): a hair of air before it, the semibold weight and the ink at rest and under
        // the pointer; the number's red with the number when the tab is picked. The weight is the sign's own in every state: it is no word
        // that a pick or a preview changes (the contract's "no change of weight" is about the words). It fades with the number: its start
        // state is its own class, put by markStart as on the number (was-off, was-lit: the ink; was-on: the picked red), so the star changes
        // its colour with the number and not at once.
        ".tab-mark-note{margin-left:1px;font-weight:var(--weight-semibold);color:var(--text-primary);transition:color var(--dur-5) var(--ease)}",
        ".tab-mark-num[aria-selected=true] .tab-mark-note,.tab-mark-num .tab-mark-note.tab-mark-was-on{color:var(--accent)}",
        ".tab-mark-num[aria-selected=true] .tab-mark-note:is(.tab-mark-was-off,.tab-mark-was-lit){color:var(--text-primary)}",
        "@media(prefers-reduced-motion:reduce){.tab-mark-icon,.tab-mark-num .tab-mark-count,.tab-mark-note{transition:none}}",
        /* kit: ring */
        // The ring. What it is: a gauge of one share, an open ring of 300 degrees with its gap of 60 degrees at the bottom: a track, an arc of the share that is left (round at both ends) and a dot at the end of the arc. It has two forms and the form is all it says: ok (the share is known: track, arc and dot) and unknown (it is not: the track alone, dotted, no arc and no dot). No colour of a state, no red.
        // When to use it: beside a name that has a share to tell (a window of a limit; later an account), where the share must be seen at a glance and its number stands next to it. Never alone: the number is the ring's word, and the ring itself is out of the reading order.
        // The two sizes are both the kit's: the large ring (lg, 47px) is the rail's; the plain ring (28px, the size left out) is the kit's base size for the cards of the accounts; no part asks for it yet, and it stays as the form a part that tells a share beside a name takes.
        // How to use it: ringGauge(pct, form, size, badge, sweep) is the one builder (pct: the share left, 0 to 100; form: ok or unknown, and a pct that is no number is unknown;
        // size: left out for the plain ring, lg for the large one with the thinner strokes that holds a number inside it, the rail's). It makes the svg (viewBox 0 0 184 184, the
        // size --ring-size, or --ring-size-lg for the large one, aria-hidden, not focusable) with its track, arc and dot, the geometry in the svg's own attributes (one path for
        // the track and the arc; pathLength 100 and a dash of pct; the dot's centre at the angle 120 + 3 x pct degrees) and the classes ring, ring-track, ring-arc and ring-dot,
        // which are worn through it and nowhere else. badge (true, with the large ring alone, the rail's) sets a small circle in the gap of the ring: its centre on the lowest
        // point of the ring's path, a stroke thinner than the track's (5 units against 7) in the badge's own edge tone (--ring-badge-edge: stronger than the track, quieter than
        // the arc), the fill the ground under the rail (class ring-badge, drawn last); the short name of the window is written over it by the part (kit: rail). sweep (true, with
        // a ring of the form ok) makes the ring draw its arc from nothing to its share when it appears: it wears the class ring-sweep and the property --ring-share (the share,
        // the number the keyframes read), and the arc and the dot play ring-sweep-arc and ring-sweep-dot (kit: motion) in --dur-sweep on --ease-sweep, the dot running round the
        // ring at the end of the arc, in step with it; the part that asks for it gives the time to wait (--sweep-at: the class ring-sweep-row waits for the row the ring stands
        // in, as the row comes down, kit: rail) and, where the page has no clock of its own to carry a drawing inside the span on, how far the drawing has gone
        // (--sweep-elapsed). Ask for it for the ring of a pick of the reader's, never for a redraw of fresh data (the poll, Refresh): the share then changes with no show. It is
        // the kit's one motion in the ring, written for every part that wears a ring: the rail's now, the cards of the accounts later.
        // States: the colours are the tokens', the ink at a strength, and turn with the theme: the track --ring-track, the arc --ring-arc, the dot --ring-dot. A part that is picked darkens the arc and the dot (--ring-arc-on, --ring-dot-on) and grows the box the ring stands in, never the ring.
        // Tokens it reads: --ring-size --ring-size-lg --ring-track --ring-arc --ring-dot --ring-badge-edge --shell-bg, and for the sweep --dur-sweep --ease-sweep --ring-share --sweep-at --sweep-elapsed, and for the rings that run one after another --i --rail-row-at --rail-row-step. Not to do: no colour of a state, no number drawn in the svg (the box the ring stands in may hold one: kit: rail), no motion but the sweep (which counts no number: the share inside the ring is in place from the first frame, and no script runs per frame).
        ".ring{display:block;flex:none;width:var(--ring-size);height:var(--ring-size);overflow:visible}",
        ".ring-lg{width:var(--ring-size-lg);height:var(--ring-size-lg)}",
        ".ring-track{stroke:var(--ring-track)}",
        ".ring-arc{stroke:var(--ring-arc)}",
        ".ring-dot{fill:var(--ring-dot)}",
        ".ring-badge{fill:var(--shell-bg);stroke:var(--ring-badge-edge)}",
        // The sweep: a ring that is asked to (ringGauge's sweep) draws its arc from nothing to its share and the dot runs round the ring with it, both
        // `backwards` (the end state is the plain ring, so nothing stays empty if an animation never starts). The dot turns about the centre of the box
        // of the svg (view-box, 50% 50%) from the angle of the start of the arc to its own: the whole of the arc's turn, 3 degrees to a share point, back.
        // It waits --sweep-at, less --sweep-elapsed; a ring that stands in a row of the rail waits for its row (ring-sweep-row).
        ".ring-sweep .ring-arc{animation:ring-sweep-arc var(--dur-sweep) var(--ease-sweep) backwards;animation-delay:calc(var(--sweep-at) - var(--sweep-elapsed))}",
        ".ring-sweep .ring-dot{transform-box:view-box;transform-origin:50% 50%;animation:ring-sweep-dot var(--dur-sweep) var(--ease-sweep) backwards;animation-delay:calc(var(--sweep-at) - var(--sweep-elapsed))}",
        ".ring-sweep-row{--sweep-at:calc(var(--rail-row-at) + var(--i) * var(--rail-row-step))}",
        "@media(prefers-reduced-motion:reduce){.ring-sweep .ring-arc,.ring-sweep .ring-dot{animation:none}}",
        /* kit: rail */
        // The rail. What it is: a column at the side of a card that lists the choices of the card (the windows of a limit), on the ground and not in the card: the rows (a
        // tablist of the kit's number tabs that wear a ring: markTab with a ring, .tab-mark-rail), anything a part adds under them (the ladder of resets). The stamp of the
        // picked window is not in the rail (it stands in the plate's row of the figure). It is --rail-w wide (what the ring needs at the axis of the widest tab of the head, with
        // --rail-air at the card; the same for every family), with no air at its sides: the rows keep their air from the edge themselves. The rows stand on the axis
        // (--rail-axis, the middle of the whole tab of the family picked), and from the head a thread (.rail-lead) runs down the axis to the first ring, one line with the
        // threads between the rings. On a narrow shell (under 860px) it is the same markup drawn as a row of capsules over the card: there is no thread from the head, the rows
        // stand in a line that wraps onto the next one (no scroll inside it, no fade: every window is in sight and the picked one is never cut), each a capsule of the kit's
        // height.
        // A row of the rail: the box of the ring (--ring-box) and nothing at its right: the share in the middle of the ring, the short name of the window in the badge set in the
        // gap of the ring (semibold, tabular, --type-badge, the meta ink; the ink when picked), the star, a bead on the axis at the lower edge of the badge, where the share
        // rests on a part of what it counts (--type-star, --star-top); the row is --rail-row-h high (the ring and the badge under it, and the thread). The full name and the
        // share are in the row's label and title. Between two rows runs a thread on the axis of the rings: it starts --thread-gap under the badge of the ring above
        // (--badge-drop; the star hangs a hair under it) and ends --thread-gap over the box of the ring below, so it is the room that is left (--thread-len at rest, 8px at least
        // when the ring above is picked). A row that is picked takes the ink for its word (its weight does not change), the accent for its share (a dash too) and its star, and
        // its ring grows to --mark-on in its box and darkens (the arc and the dot); under the pointer or the keyboard a row that is not picked grows its ring to --mark-hover,
        // darkens the arc, the dot and the edge of the badge a step (--ring-arc-hover, --ring-dot-hover, --ring-badge-edge-hover) and brings its word to the ink. Pressed
        // (:active, the mouse and the finger) its ring shrinks to --press of its size in --dur-1 and, let go, springs back (--nav-spring). The red is the share's alone: no red
        // arc, no plate. A row with nothing to tell (aria-disabled) keeps the quiet ink of a disabled word and a dotted ring.
        // Motion: the ring's box moves by transform alone (--dur-nav on --nav-spring; the press, --dur-1 on --ease), the colours of the ring and of the badge's name by a colour transition (--dur-5 on --ease); a pick redraws the rail, so markStart draws the two rings that change in the size they had a moment ago (the start states, as the mark tab's), and settleMarks takes them off at the next frame. Under reduced motion nothing moves, nothing fades and a press does not shrink the ring.
        // Tokens it reads: --rail-w --rail-tab-w --rail-row-h --star-top --badge-cy --badge-grow --thread-len --thread-top --ring-grow --type-badge --type-star --type-ring --type-unit --share-w --rail-top --ring-box --ring-size-sm --rail-thread --mark-on --mark-hover --press --ring-arc-on --ring-dot-on --ring-arc-hover --ring-dot-hover --ring-badge-edge-hover --nav-spring --dur-nav --dur-1 --dur-5 --ease --surface-capsule --elev-rest --tone-crimson --tone-crimson-ink. Not to do: no red ring, no plate on a row, no JS that rebuilds the rail by width (the narrow shell is CSS alone).
        // Motion of the reader's pick of a family (.rail-moving on the rail, put by the script only for that pick and for the drawings inside its span): the thread is drawn from
        // the mark down (scaleY, from the top), the rows come down one after another (opacity and a short fall), the thread between two rows with its row, and then the note
        // under them (the accounts not read: .wt-foot) fades in, after the last row, so nothing stands alone in an empty rail. The thread ends over a first ring that is picked
        // as high as the ring has grown (.rail-lead-on, --lead-end-on); the tab picked rides over the tabs it passes while it slides (.tab-mark-ride), and the tabs that give way
        // dim while it does (.tab-mark-yield). Only transform and opacity; the keyframes are the kit's (kit: motion); a row's number is --i, their count --rail-rows, and
        // --rail-elapsed carries a drawing inside the span on from where the movement was (the tabs of the head too: their shifts are worked out once, at the pick). The end
        // state is the plain style: a row whose animation never starts stays as it is. Under reduced motion there is no movement.
        // Tokens it reads, besides: --i --rail-rows --rail-elapsed --rail-axis --lead-top --lead-end --lead-end-on --dur-rail-thread --rail-thread-at --dur-rail-row --rail-row-at --rail-row-step --dur-rail-foot --dur-flip --ease-flip --yield-dim --yield-from --z-flip --ease, and for the ladder and the rows of the steps --accent --border-w --j --lh-ui --radius-pill --radius-row --ring-inset --row-h --space-0 --space-1 --space-1h --space-2 --space-2h --space-3 --tap-h --weight-medium --weight-semibold.
        ".rail{position:relative;display:flex;flex-direction:column;min-width:0;padding:var(--rail-top) 0 var(--space-2)}",
        ".rail-lead{position:absolute;left:calc(var(--rail-axis) - var(--border-w) / 2);top:var(--lead-top);width:var(--border-w);height:calc(var(--lead-end) - var(--lead-top));background:var(--rail-thread);pointer-events:none}",
        ".rail-lead-on{height:calc(var(--lead-end-on) - var(--lead-top))}",
        ".rail .tab-mark-row{flex-direction:column;flex-wrap:nowrap;align-items:stretch;gap:0}",
        ".tab-mark-rail{position:relative;display:flex;align-items:center;width:100%;min-height:var(--rail-row-h);padding:0 var(--space-2) 0 calc(var(--rail-axis) - var(--ring-box) / 2);border-radius:var(--radius-row);color:var(--text-secondary);text-align:left}",
        "@media(pointer:coarse){@media not all and (max-width:859.98px){.tab-mark-rail.tab-mark-num{min-height:var(--rail-row-h)}}}",
        ".tab-mark-rail[aria-selected=true]{color:var(--text-primary)}",
        ".tab-mark-rail:is(:hover,:focus-visible):not([aria-selected=true]):not([aria-disabled=true]){color:var(--text-primary)}",
        ".tab-mark-rail .tab-mark-name{min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}",
        ".tab-mark-rail .tab-mark-count{font-size:var(--type-ring);font-weight:var(--weight-semibold);line-height:1;white-space:nowrap}",
        ".tab-mark-rail .tab-mark-unit{font-size:var(--type-unit)}",
        ".tab-mark-rail .tab-mark-note{font-size:var(--type-meta)}",
        // The short name of the window is the badge in the gap of the ring (wide rail): in the box of the ring, over the badge circle drawn by the
        // ring (.ring-badge), its centre on the lowest point of the ring's path, so the box's one transform carries ring, share, badge and star
        // together. Semibold, tabular, in the meta ink (the ink when the window is picked or under the pointer). The star of a window not read
        // whole is a bead on the axis, on the lower edge of the badge (--star-top). On the narrow shell the name is the plain word of the capsule
        // (the rule is wide-only).
        "@media not all and (max-width:859.98px){.tab-mark-ring .tab-mark-name{position:absolute;left:50%;top:var(--badge-cy);overflow:visible;text-overflow:clip;transform:translate(-50%,-50%);color:var(--text-meta);font-size:var(--type-badge);font-weight:var(--weight-semibold);line-height:1;font-variant-numeric:tabular-nums;transition:color var(--dur-5) var(--ease)}.tab-mark-ring .tab-mark-note{position:absolute;left:50%;top:var(--star-top);margin:0;transform:translateX(-50%);font-size:var(--type-star);line-height:1}}",
        "@media not all and (max-width:859.98px){.tab-mark-rail[aria-selected=true] .tab-mark-name,.tab-mark-rail:is(:hover,:focus-visible):not([aria-selected=true]):not([aria-disabled=true]) .tab-mark-name{color:var(--text-primary)}}",
        "@media(max-width:859.98px){.ring-badge{display:none}}",
        ".tab-mark-rail~.tab-mark-rail::before{content:'';position:absolute;left:calc(var(--rail-axis) - var(--border-w) / 2);top:var(--thread-top);width:var(--border-w);height:var(--thread-len);background:var(--rail-thread);pointer-events:none}",
        "@media not all and (max-width:859.98px){.tab-mark-rail[aria-selected=true]::before{height:calc(var(--thread-len) - var(--ring-grow))}.tab-mark-rail[aria-selected=true]+.tab-mark-rail::before{top:calc(var(--thread-top) + var(--badge-grow));height:calc(var(--thread-len) - var(--badge-grow))}}",
        ".tab-mark-ring{position:relative;display:grid;flex:none;place-items:center;width:var(--ring-box);height:var(--ring-box);transition:transform var(--dur-nav) var(--nav-spring)}",
        ".tab-mark-ring>*{grid-area:1/1}",
        ".tab-mark-rail[aria-selected=true] .tab-mark-ring,.tab-mark-ring.tab-mark-was-on{transform:scale(var(--mark-on))}",
        ".tab-mark-rail:is(:hover,:focus-visible):not([aria-selected=true]):not([aria-disabled=true]) .tab-mark-ring{transform:scale(var(--mark-hover))}",
        ".tab-mark-rail[aria-selected=true] .tab-mark-ring.tab-mark-was-off{transform:none}",
        ".tab-mark-rail[aria-selected=true] .tab-mark-ring.tab-mark-was-lit{transform:scale(var(--mark-hover))}",
        // A row picked by a press of the pointer starts from the press (the ring at --press, as it was when the button came up), so the spring takes it from there to
        // --mark-on and the ring does not leap through the preview's size. The row of the mouse and of the finger; a pick by the keyboard has no press.
        ".tab-mark-rail[aria-selected=true] .tab-mark-ring.tab-mark-was-press{transform:scale(var(--press))}",
        ".tab-mark-rail[aria-selected=true] .ring-arc{stroke:var(--ring-arc-on)}",
        ".tab-mark-rail[aria-selected=true] .ring-dot{fill:var(--ring-dot-on)}",
        // The parts of the ring take a darker step under the pointer or the keyboard (a row not picked), by a colour transition (--dur-5 on --ease)
        ".tab-mark-rail :is(.ring-arc,.ring-dot,.ring-badge){transition:stroke var(--dur-5) var(--ease),fill var(--dur-5) var(--ease)}",
        ".tab-mark-rail:is(:hover,:focus-visible):not([aria-selected=true]):not([aria-disabled=true]) .ring-arc{stroke:var(--ring-arc-hover)}",
        ".tab-mark-rail:is(:hover,:focus-visible):not([aria-selected=true]):not([aria-disabled=true]) .ring-dot{fill:var(--ring-dot-hover)}",
        ".tab-mark-rail:is(:hover,:focus-visible):not([aria-selected=true]):not([aria-disabled=true]) .ring-badge{stroke:var(--ring-badge-edge-hover)}",
        // The press (mouse and finger, :active): the box of the ring shrinks to --press of the size it has (the picked ring: of --mark-on) in --dur-1, and
        // when it is let go the base rule's spring (--dur-nav on --nav-spring) takes it back to its size. It is as strong as the preview's rule and stands after it.
        ".tab-mark-rail:not([aria-selected=true]):not([aria-disabled=true]):active .tab-mark-ring{transform:scale(var(--press));transition:transform var(--dur-1) var(--ease)}",
        ".tab-mark-rail[aria-selected=true]:active .tab-mark-ring{transform:scale(calc(var(--mark-on) * var(--press)));transition:transform var(--dur-1) var(--ease)}",
        // Under reduced motion nothing fades and nothing moves: the press does not shrink the ring (the picked one stays at --mark-on)
        "@media(prefers-reduced-motion:reduce){.tab-mark-ring,.tab-mark-ring .ring,.tab-mark-rail :is(.ring-arc,.ring-dot,.ring-badge),.tab-mark-ring .tab-mark-name{transition:none}.tab-mark-rail:not([aria-selected=true]):not([aria-disabled=true]):active .tab-mark-ring{transform:none;transition:none}.tab-mark-rail[aria-selected=true]:active .tab-mark-ring{transform:scale(var(--mark-on));transition:none}}",
        // The slide of the row of marks when the pick puts the tab picked first (markWas, markFlip, markSlide): the shifts of the tabs are worked out
        // once, at the pick (--flip-x and --flip-y, from where each tab was seen), and every tab that stands elsewhere now wears .tab-mark-slide, whose
        // keyframes (rail-slide, kit: motion) draw it at its old place and carry it to its own by the transform alone (--dur-flip on --ease-flip). A drawing
        // inside the span of the movement puts the same shifts on its new tabs and the keyframes go on from where they were (a negative delay, --rail-elapsed),
        // so the slide is never started again. The tabs that give way (.tab-mark-yield) dim to --yield-dim in a quarter of the slide, stay dim to its four
        // fifths and come back (rail-yield, opacity alone: held for as long as the tab picked passes over their words), and the tab picked (.tab-mark-ride) stands over them for as long as it slides. Nothing else moves, and no will-change is held; the end
        // state is the plain style; under reduced motion the tab is at its place at once and nothing dims.
        ".tab-mark-slide{animation:rail-slide var(--dur-flip) var(--ease-flip) backwards;animation-delay:calc(0ms - var(--rail-elapsed))}",
        ".tab-mark-slide.tab-mark-yield{animation:rail-slide var(--dur-flip) var(--ease-flip) backwards,rail-yield var(--dur-flip) var(--ease-flip) backwards;animation-delay:calc(0ms - var(--rail-elapsed))}",
        ".tab-mark-ride{z-index:var(--z-flip)}",
        "@media(prefers-reduced-motion:reduce){.tab-mark-slide,.tab-mark-slide.tab-mark-yield{animation:none}}",
        ".rail-moving .rail-lead{transform-origin:top;animation:rail-draw var(--dur-rail-thread) var(--ease) backwards;animation-delay:calc(var(--rail-thread-at) - var(--rail-elapsed))}",
        ".rail-moving .tab-mark-rail{animation:rail-drop var(--dur-rail-row) var(--ease) backwards;animation-delay:calc(var(--rail-row-at) + var(--i) * var(--rail-row-step) - var(--rail-elapsed))}",
        "@media(prefers-reduced-motion:reduce){.rail-moving .rail-lead,.rail-moving .tab-mark-rail{animation:none}}",
        // The ladder of resets (kit: rail, its second part; the comment here is its contract). What it is: under the rings (the rail has no foot: the stamp stands in the plate), the steps at which the picked window gives
        // accounts back, soonest first, on the rail's axis: a hairline on the axis (22px), the word "Resets" (11px, semibold, the quiet ink; no button), and for each step a bead on the
        // axis (7px; the first step's, the nearest reset, in the crimson ink, the others in the tertiary grey), under it how long to the reset (12px semibold, the ink; untilWords: "40m", "5h", "2d 5h", "<1m"; said again at every drawing from Date.now())
        // and the amount that comes back, "+2.70" (11px, the meta ink; its decimal point in the crimson ink, a span of its own, the text of the amount stays
        // "+2.70"), a thread between two steps as between two rings. The whole moment (the day, the date and the hour in the reader's local time) is in the step's title and aria-label
        // (whenWords; steps that say the same in how long and stand side by side are ONE step, glued by ladderOf: the earliest moment, the accounts of all, the sum of what they give back, and the title says the first and the last moment,
        // "Sun 1 Nov 12:31 – Sun 1 Nov 12:54", so the first step may be wider than the plate's row of the next reset: the same moment, the amount and the accounts no fewer); the step itself says no date. Every line is one line and fits the 64px of the grey field (the axis the middle of it). It is no part of the card: it takes the room that is
        // left between the last ring and the foot of the card (.rail-ladder-zone: it grows from nothing, it is cut at its edge, and it adds nothing to the height of the rail, so it never
        // stretches the card); the steps that do not stand whole in that room are not drawn (.rail-fit-off, put by the script from one read of the layout at the end of a drawing)
        // and the word "N more" (.rail-more, a button) counts them; it comes down last, after the last step that stands. The word opens the ladder DOWNWARD IN THE RAIL ITSELF (aria-expanded; it reads "less"
        // while open and folds it): the steps stand in a window (.rail-scroll) of --ladder-window steps (5.5: the half of the next one under the haze says there is more), never higher than the room of the zone
        // (the capsule is not stretched), that scrolls by itself: no bar, the page is not dragged (overscroll-behavior), the edges fade by one haze (.rail-scroll-start / .rail-scroll-end, put by one passive
        // listener of the scroll only where an edge is reached or left). Pressing it touches NOTHING else: not the plate, the chart, the Details or the schedule; the right part does not change by a pixel.
        // The word is drawn only where its press opens the window (a step stands in it) and counts all the steps less those that stand. Folded again, it goes back to the steps that stand and to the start of the scroll; open and the place of the scroll
        // outlive the 30 s poll and any redraw (ladderOpen, ladderTop), for the limit they were pressed on (ladderKey); a pick of a window or a family, another limit drawn, a drawing with no ladder and a drawing with no room for the window fold it, and it does not open again by itself.
        // The stamp ("Read 18 of 20 · 2 days ago") is no part of the rail: it stands in the plate, in the row of the figure (see the stamp, wt-stamp). A step is a tab stop of the keyboard like the items of the legend (tabindex, role group, a label that says the
        // accounts by name); under the pointer or the keyboard its bead grows to --ladder-bead-grow and takes the crimson ink, its amount the ink, and the bars of its accounts stand
        // out on the plate (the script: dimOnItem); pressed, the bead shrinks to --press of its size and springs back, as a ring does. The red at rest is the first bead and the points of the amounts, and
        // nothing else. On the narrow shell (under 860px) there is no
        // ladder (display: none) and the plate keeps its own row of the next reset, which the wide shell hides (.nx-in-ladder) while it has the ladder.
        // Motion: the pick of a family moves the ladder with the rail (.rail-moving: its head and its steps come down after the rings by the rail's own keyframes, --i counting on from the
        // rings); the pick of a window moves it alone (.rail-ladder-moving: the old steps fade in --dur-ladder-out, .rail-ladder-out, and the new ones come down, ladder-drop, from --ladder-at, a
        // row after another, the word "N more" the last of them); the poll and Refresh move nothing, the numbers change where they stand. Transform and opacity only; under reduced motion everything stands in its place at once.
        // How to use it: ladderOf(g) is what the ladder stands on (the steps of a window, soonest first, neighbours that say the same in how long glued, the accounts of each and what they give back; or none), ladderNode(g, index, base) the
        // markup (the zone, the ladder in it, the head, the steps and the word that counts the steps that did not fit), and ladderLayout(z, clicked) the one read of the layout that reckons how many steps stand or how high the open window is (ladderFit calls it at the end of a drawing, ladderToggle at the press of the word; ladderEdges sets the haze).
        // Tokens it reads: --ladder-window --ladder-haze --ladder-haze-ink --ladder-haze-y --ladder-haze-y-start --ladder-haze-y-end --ladder-haze-x --ladder-haze-x-start --ladder-haze-x-end --ladder-title-h --ladder-step-h --ladder-more-h --ladder-bead --ladder-bead-grow --ladder-bead-rest --ladder-hair --type-ladder --type-meta --dur-ladder-out --ladder-at
        // --ladder-elapsed --rail-axis --ladder-w --rail-thread --text-primary --text-secondary --text-meta --tone-crimson-ink --press --nav-spring --dur-nav --dur-1 --dur-5 --ease --i --rail-row-step.
        // Not to do: no observer of the size and no script by frames (one read of the layout for a drawing), no step that stretches the card, no red beyond the first bead and the points, no second row of the next reset beside it on the wide shell, no press of the word "N more" that touches the chart, the Details or any part of the card.
        ".rail-ladder-zone{position:relative;flex:1 1 0;min-height:0;overflow:hidden}",
        // The window of the steps (open: it scrolls). No bar of scroll (the page has none anywhere: scrollbar-width, ::-webkit-scrollbar), so nothing moves the axis; the page is not dragged along
        // at its ends (overscroll-behavior); its edges fade into the ground by a mask, none over the start while it stands at its start and none under the end while it stands at its end
        // (the script puts .rail-scroll-start and .rail-scroll-end only where an edge is reached or left). Folded, it does not scroll (the steps that do not stand are not drawn).
        // The keyboard (Tab to a step past the window) scrolls to it with the haze's width to spare (scroll-padding-block), so the step in focus stands out of the haze and not faded at the edge.
        ".rail-scroll{width:100%;overflow:hidden;overscroll-behavior:contain;scrollbar-width:none;scroll-padding-block:var(--ladder-haze);-webkit-mask-image:var(--ladder-haze-y);mask-image:var(--ladder-haze-y)}",
        ".rail-scroll::-webkit-scrollbar{display:none;width:0;height:0}",
        ".rail-scroll-on{overflow-y:auto}",
        ".rail-scroll-start{-webkit-mask-image:var(--ladder-haze-y-start);mask-image:var(--ladder-haze-y-start)}",
        ".rail-scroll-end{-webkit-mask-image:var(--ladder-haze-y-end);mask-image:var(--ladder-haze-y-end)}",
        ".rail-scroll-start.rail-scroll-end{-webkit-mask-image:none;mask-image:none}",
        // The same haze drawn to the right, for the row of the bars (kit: bars) where it goes sideways: .rail-scroll-x is worn while the row scrolls, and
        // -start / -end where the row stands at an edge (the script puts them with it and takes all three off a row that fits, so no haze stands over a row that does not scroll).
        ".rail-scroll-x{-webkit-mask-image:var(--ladder-haze-x);mask-image:var(--ladder-haze-x)}",
        ".rail-scroll-x-start{-webkit-mask-image:var(--ladder-haze-x-start);mask-image:var(--ladder-haze-x-start)}",
        ".rail-scroll-x-end{-webkit-mask-image:var(--ladder-haze-x-end);mask-image:var(--ladder-haze-x-end)}",
        ".rail-scroll-x-start.rail-scroll-x-end{-webkit-mask-image:none;mask-image:none}",
        ".rail-ladder{display:flex;flex-direction:column;align-items:center;width:var(--ladder-w);margin-left:calc(var(--rail-axis) - var(--ladder-w) / 2)}",
        ".rail-ladder-title{position:relative;flex:none;width:100%;height:var(--ladder-title-h);padding-top:calc(var(--space-3) + var(--border-w) + var(--space-2));color:var(--text-secondary);font-size:var(--type-ladder);font-weight:var(--weight-semibold);line-height:var(--lh-ui);text-align:center;white-space:nowrap}",
        ".rail-ladder-title::before{content:'';position:absolute;left:calc(50% - var(--ladder-hair) / 2);top:var(--space-3);width:var(--ladder-hair);height:var(--border-w);background:var(--rail-thread)}",
        ".rail-step{position:relative;display:flex;flex:none;flex-direction:column;align-items:center;width:100%;height:var(--ladder-step-h);padding-top:var(--space-3);color:var(--text-meta);text-align:center;white-space:nowrap}",
        ".rail-step~.rail-step::before{content:'';position:absolute;left:calc(50% - var(--border-w) / 2);top:0;width:var(--border-w);height:var(--space-3);background:var(--rail-thread);pointer-events:none}",
        ".rail-bead{flex:none;width:var(--ladder-bead);height:var(--ladder-bead);margin-bottom:var(--space-1);border-radius:var(--radius-pill);background:var(--ladder-bead-rest);transition:transform var(--dur-nav) var(--nav-spring),background-color var(--dur-5) var(--ease)}",
        ".rail-step-first .rail-bead{background:var(--tone-crimson-ink)}",
        ".rail-step-time{color:var(--text-primary);font-size:var(--type-meta);font-weight:var(--weight-semibold);line-height:1.25;font-variant-numeric:tabular-nums;transition:color var(--dur-5) var(--ease)}",
        ".rail-step-amount{font-size:var(--type-ladder);line-height:1.25;font-variant-numeric:tabular-nums;transition:color var(--dur-5) var(--ease)}",
        ".rail-step-dot{color:var(--tone-crimson-ink)}",
        ".rail-step-none{max-width:calc(100% - var(--space-1));color:var(--text-meta);font-size:var(--type-ladder);line-height:1.25;white-space:normal;text-wrap:balance}",
        // A step with accounts is a button (role, aria-pressed: the script): the pointer shows it, and while it is pressed (its bars are lit, the others step back) its time stands in the accent, nothing else of it changes.
        ".rail-step[aria-pressed]{cursor:pointer}",
        ".rail-step[aria-pressed=true] .rail-step-time{color:var(--accent)}",
        ".rail-step:is(:hover,:focus-visible) .rail-bead{transform:scale(var(--ladder-bead-grow));background:var(--tone-crimson-ink)}",
        ".rail-step:is(:hover,:focus-visible) .rail-step-amount{color:var(--text-primary)}",
        ".rail-step:active .rail-bead{transform:scale(calc(var(--ladder-bead-grow) * var(--press)));transition:transform var(--dur-1) var(--ease)}",
        ".rail-more{display:block;flex:none;width:100%;height:var(--ladder-more-h);padding:0;border:0;background:none;color:var(--text-meta);font-size:var(--type-ladder);text-decoration:underline dotted;text-underline-offset:var(--space-0)}",
        ".rail-more:is(:hover,:focus-visible){color:var(--text-primary)}",
        // The window and the zone cut what is outside them: the ring of a step and of the word stands inside their boxes, so it is seen whole.
        ".rail-step:focus-visible,.rail-more:focus-visible{outline-offset:var(--ring-inset)}",
        ".rail-fit-off{display:none}",
        ".rail-more-void{visibility:hidden}",
        ".rail .rail-ladder .rail-step.rail-step-in{animation:ladder-drop var(--dur-rail-row) var(--ease) backwards;animation-delay:calc(var(--j) * var(--rail-row-step))}",
        "@media(pointer:coarse){.rail-more{min-height:var(--tap-h)}}",
        "@media(prefers-reduced-motion:reduce){.rail-bead,.rail-step-time,.rail-step-amount{transition:none}.rail-step:active .rail-bead{transform:scale(var(--ladder-bead-grow));transition:none}}",
        ".rail-moving .rail-ladder-title,.rail-moving .rail-step,.rail-moving .rail-more{animation:rail-drop var(--dur-rail-row) var(--ease) backwards;animation-delay:calc(var(--rail-row-at) + var(--i) * var(--rail-row-step) - var(--rail-elapsed))}",
        ".rail-ladder-moving .rail-ladder-title,.rail-ladder-moving .rail-step,.rail-ladder-moving .rail-more{animation:ladder-drop var(--dur-rail-row) var(--ease) backwards;animation-delay:calc(var(--ladder-at) + var(--i) * var(--rail-row-step) - var(--ladder-elapsed))}",
        ".rail-ladder-out{position:absolute;top:0;left:0;opacity:0;pointer-events:none;animation:ladder-leave var(--dur-ladder-out) var(--ease) backwards;animation-delay:calc(0ms - var(--ladder-elapsed))}",
        "@media(prefers-reduced-motion:reduce){.rail-moving .rail-ladder-title,.rail-moving .rail-step,.rail-moving .rail-more,.rail-ladder-moving .rail-ladder-title,.rail-ladder-moving .rail-step,.rail-ladder-moving .rail-more,.rail-ladder-out,.rail .rail-ladder .rail-step.rail-step-in{animation:none}}",
        "@media not all and (max-width:859.98px){.nx-in-ladder.nx-in-ladder{display:none}}",
        "@media(max-width:859.98px){.rail-ladder-zone{display:none}}",
        "@media(max-width:859.98px){.rail{padding:0}.rail-lead{display:none}}",
        "@media(max-width:859.98px){.rail .tab-mark-row{flex-direction:row;flex-wrap:wrap;align-items:center;gap:var(--space-1h)}}",
        "@media(max-width:859.98px){.tab-mark-rail{flex:0 0 auto;display:flex;width:auto;max-width:100%;min-height:var(--row-h);padding:0 calc(var(--space-2h) + var(--share-w)) 0 0;border-radius:var(--radius-pill);font-size:var(--type-meta)}.tab-mark-rail>*,.tab-mark-ring>*{position:relative}.tab-mark-rail .tab-mark-name{font-weight:var(--weight-medium)}.tab-mark-rail .ring{width:var(--ring-size-sm);height:var(--ring-size-sm)}}",
        // The same markup on the narrow shell: the box of the ring is no box (display: contents), so the ring (22px, in the room of the kit's
        // 30px box) grows and shrinks by itself, and the share, out of the line (it is not reordered: the tab keeps its reading order), stands at the end of
        // the capsule in the room the capsule keeps for it (--share-w).
        "@media(max-width:859.98px){.tab-mark-ring{display:contents}.tab-mark-ring .ring{margin:0 calc(var(--space-1h) + var(--space-1)) 0 var(--space-1);transition:transform var(--dur-nav) var(--nav-spring)}.tab-mark-rail .tab-mark-count{position:absolute;top:0;right:var(--space-2h);bottom:0;display:flex;align-items:center;font-size:var(--type-meta)}.tab-mark-rail .tab-mark-unit{font-size:inherit}}",
        "@media(max-width:859.98px){.tab-mark-rail[aria-selected=true] .ring,.tab-mark-ring.tab-mark-was-on .ring{transform:scale(var(--mark-on))}.tab-mark-rail:is(:hover,:focus-visible):not([aria-selected=true]):not([aria-disabled=true]) .ring{transform:scale(var(--mark-hover))}.tab-mark-rail[aria-selected=true] .tab-mark-ring.tab-mark-was-off .ring{transform:none}.tab-mark-rail[aria-selected=true] .tab-mark-ring.tab-mark-was-lit .ring{transform:scale(var(--mark-hover))}.tab-mark-rail[aria-selected=true] .tab-mark-ring.tab-mark-was-press .ring{transform:scale(var(--press))}.tab-mark-rail:not([aria-selected=true]):not([aria-disabled=true]):active .ring{transform:scale(var(--press));transition:transform var(--dur-1) var(--ease)}.tab-mark-rail[aria-selected=true]:active .ring{transform:scale(calc(var(--mark-on) * var(--press)));transition:transform var(--dur-1) var(--ease)}}",
        "@media(max-width:859.98px) and (prefers-reduced-motion:reduce){.tab-mark-rail:not([aria-selected=true]):not([aria-disabled=true]):active .ring{transform:none;transition:none}.tab-mark-rail[aria-selected=true]:active .ring{transform:scale(var(--mark-on));transition:none}}",
        "@media(max-width:859.98px){.tab-mark-rail::before,.tab-mark-rail~.tab-mark-rail::before{content:'';position:absolute;left:0;right:0;top:50%;width:auto;height:var(--row-h);margin-top:calc(var(--row-h) / -2);border-radius:var(--radius-pill);background:var(--surface-capsule);box-shadow:var(--elev-rest);pointer-events:none}}",
        "@media(max-width:859.98px){.tab-mark-rail[aria-selected=true]{color:var(--tone-crimson-ink)}.tab-mark-rail[aria-selected=true]::before{background:var(--tone-crimson)}}",
        /* kit: segmented control */
        // A row of options that look like buttons: the segment, its caption and its option.
        // Tokens it reads: --space-0 --type-meta --text-meta --space-1 --edge.
        ".seg{display:inline-flex;align-items:center;flex-wrap:wrap;gap:var(--space-0)}",
        ".seg-label{font-size:var(--type-meta);color:var(--text-meta);margin-right:var(--space-1)}",
        ".seg-opt{border-color:var(--edge)}",
        /* kit: rows and tables */
        // Rows: the list of limits and the row of one limit (its figure, its bars and the words under them, its chart word).
        // Tokens it reads: --space-3 --space-1 --space-4 --lh-ui --type-section --weight-semibold --text-meta --type-meta --space-2 --space-0 --edge-strong
        // --type-figure --weight-light --lh-tight --tone-crimson-ink --text-primary --track-meta --dur-nav --nav-spring --weight-regular --focus-line
        // --ring-offset --radius-sm --press --dur-1 --ease --dur-4 --border-w --edge --weight-medium --type-body.
        ".lrows{margin-top:0}",
        // Every row is a plate (.lrow.wt-plate): a grid with no box of its own (no ground, no padding, no line under it: it stands on the card's own
        // surface), a hair of air between its rows and the room the graph under it needs; its columns and areas are below, under the window-tabs plate.
        ".lrow{display:grid;column-gap:var(--space-3);row-gap:var(--space-1);padding:0 0 var(--space-4)}",
        // The figure of a row: the account-windows left, its unit beside it. It has no box (display: contents): the number and its unit are
        // the grid items of the fact (capsule: window tabs).
        ".l-fig{display:contents;white-space:nowrap;line-height:var(--lh-ui)}",
        // The figure (.num-strong, the section size at the semibold weight) and the quiet figure beside it (.meta-num, the meta
        // size and ink): both on tabular digits, worn by the figure of a row and by the next reset of the graph.
        ".num-strong{font-size:var(--type-section);font-weight:var(--weight-semibold);font-variant-numeric:tabular-nums}",
        ".meta-num{color:var(--text-meta);font-size:var(--type-meta);font-variant-numeric:tabular-nums}",
        ".cover-link{margin-top:var(--space-2);padding:var(--space-0) 0;border:0;background:none;text-align:left;font-size:var(--type-meta);color:var(--text-meta);text-decoration:underline;text-decoration-color:var(--edge-strong);text-underline-offset:3px}",
        "@media(max-width:520.98px){.lrow{column-gap:var(--space-2)}}",
        // Narrower still, the figure may wrap.
        "@media(max-width:360px){.l-fig{white-space:normal}}",
        // Under the window-tabs plate (every row is one): the picked window's row, bare on the card. Two rows of named areas: the bars over the whole
        // width and, under them, the fact (its number on two lines) at the left, the model list (the scope row, when the length has several limits) at
        // the left of the room that is left, and at the right the stamp and the chart word (the kit's text action, in the row's own slot). The notes under
        // that row (the accounts at the limit, the accounts not read, the manual resets, the legend, the reset) have no area: each takes the whole width in
        // the row the flow gives it. An area nobody fills has no place, and the others keep theirs.
        ".lrow.wt-plate{position:relative;grid-template-columns:auto minmax(0,1fr) auto auto;grid-template-areas:\"strip strip strip strip\" \"fact scope stamp ctl\";align-items:center}",
        // The parts of the number's row keep the air the notes stand from it (--space-3 in all: the row gap and this).
        ".wt-plate>.l-chart{grid-area:ctl;justify-self:end;margin-bottom:var(--space-2)}",
        // The figure of the plate is its headline: --type-figure, thin (--weight-light), on both lines of the fact's grid, at the left of the window's name and of the
        // unit's quiet words. It is the plate's alone (the kit's .num-strong stays what the graph reads). Its decimal point is the crimson ink (.wt-dot, a
        // span of its own). With no current reading the figure is a dash in the quiet ink: never a zero.
        ".wt-plate .wt-num{grid-column:1;grid-row:1/3;font-size:var(--type-figure);font-weight:var(--weight-light);line-height:var(--lh-tight)}",
        ".wt-plate .wt-num.none{color:var(--text-meta)}",
        ".wt-dot{color:var(--tone-crimson-ink)}",
        // The fact has no bubble: a grid of two columns, the figure at the left over both lines and, at its right, the window's name (the ink) over the unit
        // ("of 20 accounts", the quiet ink), both in the meta type and its tracking.
        ".wt-fact{grid-area:fact;justify-self:start;align-self:center;display:grid;grid-template-columns:auto minmax(0,1fr);column-gap:var(--space-3);align-items:center;min-width:0;max-width:100%;margin-bottom:var(--space-2)}",
        ".wt-fact-cap{grid-column:2;grid-row:1;align-self:end;color:var(--text-primary);font-size:var(--type-meta);line-height:var(--lh-ui);letter-spacing:var(--track-meta)}",
        ".wt-fact .of{grid-column:2;grid-row:2;align-self:start;letter-spacing:var(--track-meta)}",
        // The accounts at the limit now (or what a billing cycle spent beyond its plan), in words and not in a bubble: "At the limit · 3", the names
        // after it (the ink) and when the soonest of them is back (the quiet ink). It takes the keyboard (the accounts are named in its hint), and under the pointer or
        // the keyboard it lights their bars (the plate dims the bars but theirs). Its touch box under a finger is the "N more" button's own (.wt-foot-more);
        // it answers a press with the kit's (--press, the spring back).
        ".wt-limit{grid-column:1/-1;justify-self:start;min-width:0;max-width:100%;color:var(--text-meta);font-size:var(--type-meta);line-height:var(--lh-ui);letter-spacing:var(--track-meta);overflow-wrap:anywhere;transition:transform var(--dur-nav) var(--nav-spring)}",
        ".wt-limit-head{margin-right:var(--space-2);color:var(--text-primary);font-weight:var(--weight-regular);font-variant-numeric:tabular-nums}",
        ".wt-limit[tabindex]{cursor:default}",
        // Under the window-tabs plate, for the reader's own pick (.wt-in): the fact and the bubble rise in
        // (the keyframes are the kit's: motion).
        ".wt-in>.wt-fact{animation:wt-rise var(--dur-4) var(--ease) both}",
        // Backwards, not both: the end of the keyframes is the rest, and a fill held after it would stand over the press (:active) of the bubble.
        ".wt-in>.wt-limit{animation:wt-rise var(--dur-4) var(--ease) backwards}",
        "@media(prefers-reduced-motion:reduce){.wt-in>.wt-fact,.wt-in>.wt-limit{animation:none}.wt-limit{transition:none}}",
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
        /* kit: bars */
        // One bar per account, as tall as its share left on one 0–100% scale.
        // One track height in every row, charted or not: the same share is
        // the same height wherever it stands.
        // The row is the plate's first line, the card's whole width and --bar-h high: a bar is a tube (the corner of a row, the track for its ground, the fill
        // with no corner of its own, for the tube cuts it), as wide as the room gives it between --bar-min and --bar-max. Where the bars do not fit at the least
        // the row goes sideways (no bar of scroll, the page has none anywhere; its edges fade: .rail-scroll-x, the rail's haze drawn to the right; the keyboard brings a bar in focus out of the haze: scroll-padding-inline), and with few bars
        // they stand at the left at their widest. There is no line under the row.
        // What a bar is told by is its form and its tone, never a colour of a state (no pink, no amber): a bar that is not in the line has its
        // ink (the bar's colour) on its base, 3px (at the limit now it is an empty track with it; held back, the fill stays with it: the words
        // under the bars say why); a dated value keeps the hatch, and its base is dashed when it was at the limit; an unknown one is a thin ring in
        // the track's tone with a "?". The pointer turns the fill to the bar's ink; the picked bar's ink is the accent (its fill, its number and a hairline ring
        // drawn inside). Picking a bar does not change its width (the row scrolls, and a bar that grew by it would be cut by it); the pointer or the keyboard on a bar
        // opens it to the side, and the picked bar is the open one on every pointer (the rules after the number, below).
        // Tokens it reads: --bar-gap --bar-h --space-3 --ladder-haze --bar-min --bar-max --radius-row --share-track --ring-inset --share-ink --dur-5 --ease
        // --dur-bars --hatch --border-w --text-meta --type-meta --accent --trace-w --text-primary --type-ladder --weight-semibold --surface --bar-rest
        // --bar-open --space-2 --space-0 --lh-ui --dur-4 --weight-medium --dur-1.
        ".bars{grid-area:strip;display:flex;align-items:stretch;gap:var(--bar-gap);width:100%;min-width:0;height:var(--bar-h);margin-bottom:var(--space-3);overflow-x:auto;overscroll-behavior-x:contain;scroll-padding-inline:var(--ladder-haze);scrollbar-width:none}",
        ".bars::-webkit-scrollbar{display:none;width:0;height:0}",
        ".bar{position:relative;display:block;flex:1 1 0;min-width:var(--bar-min);max-width:var(--bar-max);height:100%;min-height:0;padding:0;border:0;border-radius:var(--radius-row);overflow:hidden;background:var(--share-track)}",
        // The tube and the row cut what is outside them: the ring of the keyboard stands inside the bar.
        ".bar:focus-visible{outline-offset:var(--ring-inset)}",
        ".bar>.fill{position:absolute;left:0;right:0;bottom:0;width:100%;border-radius:0;background:var(--share-ink);transition:background var(--dur-5) var(--ease),width 0s linear var(--dur-bars)}",
        ".bar:is(:hover:not(:disabled):not(:where(.bars-still>.bar)),.sel,.bar-on)>.fill{background:currentColor}",
        ".bar.last>.fill{background:var(--share-track) var(--hatch);box-shadow:inset 0 1.5px 0 var(--share-ink)}",
        ".bar.last:is(:hover:not(:disabled):not(:where(.bars-still>.bar)),.sel,.bar-on)>.fill{background:var(--share-track) var(--hatch);box-shadow:inset 0 1.5px 0 currentColor}",
        ".bar.spent:after,.bar.held:after{content:\"\";position:absolute;left:0;right:0;bottom:0;height:3px;background:currentColor}",
        ".bar.unknown{background:transparent;box-shadow:inset 0 0 0 var(--border-w) var(--share-track)}",
        ".bar.unknown:after{content:\"?\";position:absolute;left:0;right:0;bottom:4px;text-align:center;color:var(--text-meta);font-size:var(--type-meta);line-height:1}",
        // A value dated at the limit (was at the limit when last read; held back and last read) has a dashed base in the share ink
        // (as the graph's dashed "carried: last known"), not the ink base: it is not exhaustion now.
        // A dashed border, not a gradient: the style system has none.
        ".bar.was:after,.bar.held.last:after{content:\"\";position:absolute;left:0;right:0;bottom:0;height:0;background:none;border-bottom:3px dashed var(--share-ink)}",
        ".bar:is(.sel,.bar-on).was:after,.bar:is(.sel,.bar-on).held.last:after{border-bottom-color:currentColor}",
        // The picked bar: its ink is the accent, its fill and its number with it, and a ring drawn inside in the same ink, of the width of the line of the pick (--trace-w). An unknown one keeps its
        // "?" and turns it to the same ink.
        ".bar:is(.sel,.bar-on){color:var(--accent);box-shadow:inset 0 0 0 var(--trace-w) currentColor}",
        ".bar:is(.sel,.bar-on).unknown:after{color:currentColor}",
        // A bar of no account is a disabled button, and its ink is the ink all the same (its base and the pointer are drawn in it).
        ".bar:disabled{cursor:default;color:var(--text-primary)}",
        // The number over the fill (.bar-v, hidden from a reader: the bar's own words say it): the share left in percent, in the quiet ink, a step above the top of the fill (the
        // script sets where); a bar at the limit now has it in the accent, semibold; where the fill is over 80% (.in) it stands in the fill, in the ink the fill is read against
        // (the hatched fill of a dated value is the track's, so the primary ink there).
        ".bar-v{position:absolute;left:0;right:0;text-align:center;color:var(--text-meta);font-size:var(--type-ladder);line-height:1;font-variant-numeric:tabular-nums;pointer-events:none}",
        ".bar.spent>.bar-v{color:var(--accent);font-weight:var(--weight-semibold)}",
        ".bar:is(.sel,.bar-on)>.bar-v{color:var(--accent)}",
        ".bar>.bar-v.in{color:var(--surface)}",
        ".bar.last>.bar-v.in{color:var(--text-primary)}",
        // A bar opens to the side (the pointer over it or the keyboard on it; under a finger, the picked one): its basis and its most are the width it has at rest (--bar-rest, written on the row by
        // barsFit) and --bar-open, the room of its words, added in the rule (the sum cannot stand on :root, the rest is the row's); the open bar does not shrink, so a row that cannot hold it goes
        // sideways (barBring brings the bar into the room of the row). Its words (.bar-i: drawn by the script for a bar of an account, hidden from a reader, who has the bar's own words) show at the
        // right of the liquid, after a short delay, in a room of the same width whatever they say; a line that is longer than the room is cut with an ellipsis. The liquid does not
        // stretch: the fill, the number and the base keep the width the bar has at rest. As the bar closes they keep it for as long as the move takes
        // (the width goes back to 100% after that time, in no time of its own), so the liquid does not splash; as it opens there is no delay. A bar of no account is a disabled button and stays shut.
        // The step of the ladder of resets (kit: rail) that is pressed opens no bar and wears none in the accent: a step of one account is the pick of that account (.sel; the script keeps .bar-on on its bar, the same look), a
        // step of several lights their bars alone (data-step-lit, below: the others step back as they do under the pointer).
        // The first line of the words is the account's name (.bar-n), a line in the accent (.bar-a) says the bar is at the limit or held back now. The move of the basis and of the most
        // is the transition of the bars' one rule (capsule: bars and legend, below), together with the fade of the dimming.
        ".bar>.bar-v,.bar:after{width:100%;transition:width 0s linear var(--dur-bars)}",
        ".bar-i{position:absolute;top:0;bottom:0;left:calc(var(--bar-rest) + var(--space-3));width:calc(var(--bar-open) - var(--space-3) - var(--space-2));display:flex;flex-direction:column;justify-content:center;gap:var(--space-0);text-align:left;white-space:nowrap;color:var(--text-meta);font-size:var(--type-meta);line-height:var(--lh-ui);opacity:0;pointer-events:none;transition:opacity var(--dur-4) var(--ease)}",
        ".bar-i>*{min-width:0;overflow:hidden;text-overflow:ellipsis}",
        ".bar-n{color:var(--text-primary);font-weight:var(--weight-medium)}",
        ".bar:is(.sel,.bar-on) .bar-n{color:var(--accent)}",
        ".bar-a{color:var(--accent)}",
        // A row that is still (.bars-still, the script: barStill) lets the pointer open and light nothing: a pick moves the bars under a pointer that stands where the click was, and the bar
        // that comes under it would open at once and push the bars on its right back and forth (the pointer still opens a bar, but not under a pointer that has not moved).
        // The pick, the keyboard and the step of the ladder open as they did; the first real move of the pointer takes the class off (releaseBarStill). :where() keeps the specificity of the rule.
        "@media(pointer:fine){.bars>.bar:is(:hover:not(:disabled):not(:where(.bars-still>.bar)),:focus-visible,.sel,.bar-on){flex-basis:calc(var(--bar-rest) + var(--bar-open));flex-grow:0;flex-shrink:0;max-width:calc(var(--bar-rest) + var(--bar-open))}.bars>.bar:is(:hover:not(:disabled):not(:where(.bars-still>.bar)),:focus-visible,.sel,.bar-on)>:is(.fill,.bar-v),.bars>.bar:is(:hover:not(:disabled):not(:where(.bars-still>.bar)),:focus-visible,.sel,.bar-on):after{right:auto;width:var(--bar-rest);transition-delay:0s}.bars>.bar:is(:hover:not(:disabled):not(:where(.bars-still>.bar)),:focus-visible,.sel,.bar-on)>.bar-i{opacity:1;transition-delay:var(--dur-1)}}",
        "@media(pointer:coarse){.bars>.bar:is(.sel,.bar-on){flex-basis:calc(var(--bar-rest) + var(--bar-open));flex-grow:0;flex-shrink:0;max-width:calc(var(--bar-rest) + var(--bar-open))}.bars>.bar:is(.sel,.bar-on)>:is(.fill,.bar-v),.bars>.bar:is(.sel,.bar-on):after{right:auto;width:var(--bar-rest);transition-delay:0s}.bars>.bar:is(.sel,.bar-on)>.bar-i{opacity:1;transition-delay:var(--dur-1)}}",
        "@media(prefers-reduced-motion:reduce){.bar>.fill,.bar>.bar-v,.bar:after,.bar-i{transition:none}}",
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
        ".lrows>.inspector,.lrows>.ins-wrap>.ins-fold>.inspector{border-width:0 0 var(--border-w);border-radius:0;padding:var(--space-3) var(--space-2)}",
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
        // The keyframes a part may borrow, by name: the blink of a dot (the text action's and the stamp's, while a read is in the air), the rise and
        // the growth of the window-tabs plate (and the rise of the note under their row), and the rail's five (rail-draw, rail-drop, rail-fade, rail-slide, rail-yield), and the ring's two (ring-sweep-arc, ring-sweep-dot). Transform and opacity only, but for the one exception the canon names (test_style_canons.py): the arc of a ring draws by stroke-dashoffset, in ring-sweep-arc alone, on a ring of 47px (a repaint the size of the ring, cheap). What they move, and
        // its reduced-motion pair, is written with the part.
        // The one class a part wears for the blink: .is-busy on a dot (a read is in the air: it dims to --blink-dim and back in --dur-blink on --ease); under
        // reduced motion it does not move and stays at the dim.
        // Tokens it reads: --dur-blink --ease --blink-dim --ring-share --skel-dim --flip-x --flip-y --yield-from --yield-dim.
        ".is-busy{animation:blink var(--dur-blink) var(--ease) infinite}",
        "@media(prefers-reduced-motion:reduce){.is-busy{animation:none;opacity:var(--blink-dim)}}",
        "@keyframes blink{50%{opacity:var(--blink-dim)}}",
        // The ring's sweep (kit: ring): the arc is drawn by its dash offset, from the whole share (nothing seen) to none (the share, as the ring stands); the dot
        // turns back by the angle of the share (3 degrees to a point) and comes to its own place. Same time and same curve, so the dot is at the end of the arc at every frame.
        "@keyframes ring-sweep-arc{from{stroke-dashoffset:var(--ring-share)}to{stroke-dashoffset:0}}",
        "@keyframes ring-sweep-dot{from{transform:rotate(calc(var(--ring-share) * -3deg))}to{transform:rotate(0deg)}}",
        "@keyframes wt-rise{from{opacity:0;transform:translateY(4px)}to{opacity:1;transform:none}}",
        "@keyframes wt-grow{from{opacity:.4;transform:scaleY(.6)}to{opacity:1;transform:none}}",
        // The wait of the chart's plate while the line of a pick is drawn (capsule: chart): the stand-ins pulse by opacity alone (skel-pulse), and the content comes in by opacity alone (tl-reveal) when the line has met.
        "@keyframes skel-pulse{50%{opacity:var(--skel-dim)}}",
        "@keyframes tl-reveal{from{opacity:0}to{opacity:1}}",
        // The rail's, for the reader's pick of a family: the thread drawn from the top down, a window coming down into its place, the foot fading in.
        "@keyframes rail-draw{from{transform:scaleY(0)}to{transform:none}}",
        "@keyframes rail-drop{from{opacity:0;transform:translateY(-8px)}to{opacity:1;transform:none}}",
        "@keyframes rail-fade{from{opacity:0}to{opacity:1}}",
        // And the ladder of resets, for the reader's pick of a window (kit: rail): a step comes down into its place as a window of the rail does, and the steps that stood fade (the node
        // of the old ladder stands at opacity 0, so the keyframes say only where it begins).
        "@keyframes ladder-drop{from{opacity:0;transform:translateY(-8px)}to{opacity:1;transform:none}}",
        "@keyframes ladder-leave{from{opacity:1}}",
        // And the head's tabs: a tab goes from its old place (the shifts the script sets on it, --flip-x and --flip-y) to its own, and a tab that gives
        // way dims to --yield-dim half way and comes back, in the same time.
        "@keyframes rail-slide{from{transform:translate(var(--flip-x),var(--flip-y))}to{transform:none}}",
        // The tabs that give way dim quickly (a quarter of the time), stay dim for as long as the tab picked passes over them (to about four
        // fifths) and come back; a second pick inside the movement starts the dimming from the look the tab has then (--yield-from, set by the
        // script from one read of its opacity; 1 otherwise).
        "@keyframes rail-yield{0%{opacity:var(--yield-from)}25%,80%{opacity:var(--yield-dim)}100%{opacity:1}}",
        /* kit: inputs */
        // The rows menu: a fold with a checkbox for each row; a quiet line (.meta-line).
        // Tokens it reads: --space-2.
        ".rows-menu{margin-top:var(--space-2)}",
        ".rows-menu summary{cursor:pointer;min-height:24px}",
        ".rows-menu label{display:flex;gap:var(--space-2);align-items:center;min-height:28px}",
        /* kit: touch */
        // Under a coarse pointer (a finger) a pressable kit part is at least --tap-h high; with a mouse the sizes above stay. A
        // capsule whose own rule sets a smaller height (the graph's pill segment, the Details summary, an account row) lifts it with
        // a line of its own beside that rule. The row of bars is --tap-h high, and under a finger the gap between two bars is not a gap: the row has none
        // (gap:0) and each bar of it wears half of --bar-gap as a transparent border at either side (the ground is clipped to the padding box, so the bar is drawn as
        // wide as before), so a touch between two bars lands on one of them. The border is part of the width (border-box): the least and the most a bar is wide
        // grow by --bar-gap with it. A bar is as wide as the card allows for the number of its accounts (at --bar-min on a phone with many accounts, and the
        // row goes sideways): the width of the target is the number's, not a thing a rule can lift. There is no pointer over a bar under a finger: the picked bar is the
        // one that stands open (kit: bars).
        // Tokens it reads: --tap-h --bar-gap --bar-min --bar-max.
        "@media(pointer:coarse){.pill-btn,.seg-opt,.btn-compact,.btn-compact-wide,.btn-word,.tab-mark,.cover-link,.rows-menu summary,.rows-menu label{min-height:var(--tap-h)}.btn-word{min-width:var(--tap-h)}.bars{min-height:var(--tap-h);gap:0}.bars>.bar{border-inline:calc(var(--bar-gap) / 2) solid transparent;background-clip:padding-box;min-width:calc(var(--bar-min) + var(--bar-gap));max-width:calc(var(--bar-max) + var(--bar-gap))}}",
        // The head's line is as high as its tabs: a finger's height, too (the thread from the mark to the first ring reads it).
        "@media(pointer:coarse){:root{--head-h:var(--tap-h)}}",
        /* kit: press */
        // The press of a control that has no plate of its own to sink (:active, mouse and finger): it shrinks to --press of its size in --dur-1 on --ease, and when it is let go
        // the control's own rule takes it back. Written once, for the controls it lists: the window row's limit line that the keyboard reaches, the banner's retry and the scope option. Each has its pair under reduced motion: nothing
        // moves, the press does not shrink it. The press of the rail's ring is the rail's own (kit: rail): the kit of a tab keeps every rule of the tab.
        // Tokens it reads: --press --dur-1 --ease.
        ".wt-limit[tabindex]:active,.banner .banner-retry:active,.wt-scope .wt-scope-opt:active{transform:scale(var(--press));transition:transform var(--dur-1) var(--ease)}",
        "@media(prefers-reduced-motion:reduce){.wt-limit[tabindex]:active,.banner .banner-retry:active,.wt-scope .wt-scope-opt:active{transform:none;transition:none}}",
        /* kit: focus ring */
        // The ring of a thing the keyboard reaches that the base does not reach by what it is (a button and a summary wear it, a region that wears .focus-ring does): the
        // line and its offset are the base's, the corner is the small one so the ring follows the thing, drawn outside it. The second rule draws the same ring inside, for a
        // thing that fills its box to the edge of the card, which would cut it. Written once, for the things they list.
        // Tokens it reads: --focus-line --ring-offset --radius-sm.
        ".wt-limit[tabindex]:focus-visible,.lg .legend-item:focus-visible,.nx[tabindex]:focus-visible,.wt-foot-g:focus-visible{outline:var(--focus-line);outline-offset:var(--ring-offset);border-radius:var(--radius-sm)}",
        ".wt-plate>.tl-wrap>.tl-strip:focus-visible,.wt-plate .kt-toggle:focus-visible{outline:var(--focus-line);outline-offset:calc(var(--ring-offset)*-1)}",
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
        ".shell.has-rail{grid-template-columns:var(--rail-w) minmax(0,1fr)}",
        ".control-bar+.shell{margin-top:var(--shell-card-gap)}",
        "@media(max-width:859.98px){.shell.has-rail{display:block}.has-rail>.shell-card{margin-top:var(--space-2)}}",
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
        // The control bar: families on the left, About and Refresh on the right.
        ".control-bar{display:flex;align-items:flex-start;justify-content:space-between;flex-wrap:wrap;gap:var(--space-2)}",
        // The families are the kit's row of mark tabs (kit: mark tab): the markup wears .tab-mark-row and .harness-seg, and the head
        // gives the row only its place: the room it asks for in the bar.
        ".harness-seg{flex:1 1 280px}",
        // The row keeps no air of its own at its left (the first tab, the family picked, stands at the edge of the widget, --shell-card-gap from it,
        // as far as from the top): the rail's axis is the middle of that tab (--rail-axis, railFit).
        // About and Refresh are the kit's row of words (kit: text action): the markup wears .btn-word-row and .action-seg, and the head
        // gives the row only its place: the right end of the bar, and the height of a row of controls.
        ".action-seg{margin-left:auto;min-height:var(--row-h)}",
        // What the head keeps of the tabs is its own: the initial that stands as a family's mark where the widget has no drawing of it
        // (the kit's tab wears it as its mark). The ghosts it shows before the first answer are tabs of the kit, disabled: the base's
        // disabled ink is theirs, the head adds no look.
        ".harness-initial{display:inline-flex;align-items:center;justify-content:center;width:var(--mark-px);height:var(--mark-px);font-size:var(--type-meta);font-weight:var(--weight-semibold)}",
        // About wears no state: a trouble of the system is said by the banners and the status line of the card, not by the word.
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
        ".lrow>.family-credits{grid-column:1/-1;margin:0}",
        ".wt-plate .kt-sum>.family-credits{margin:var(--space-1) 0 0}",
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
        "@media(max-width:639.98px){.wt-plate.wt-plate>.l-chart{grid-area:auto;grid-column:2;justify-self:end}.wt-plate.wt-plate>.bars{grid-area:auto;grid-column:1/-1}}",
        ".lg{list-style:none}",
        ".nx{display:flex;flex-wrap:wrap;gap:var(--space-1)}",
        ".lg b,.nx b{font-weight:var(--weight-medium);color:var(--text-primary);font-variant-numeric:tabular-nums}",
        ".lg .legend-item,.nx>span{white-space:nowrap}",
        // In the summary of the account card (cardTake): under the note, and, 520px and under, its words wrap, the card being narrower than the plate.
        ".wt-plate .kt-sum .nx,.wt-plate .kt-sum .lg{margin:var(--space-1) 0 0}",
        ".wt-in .kt-sum .nx{animation:wt-rise var(--dur-4) var(--ease) both}",
        "@media(prefers-reduced-motion:reduce){.wt-in .kt-sum .nx{animation:none}}",
        "@media(max-width:520.98px){.wt-plate .nx>span{white-space:normal}}",
        ".lg .legend-item,.nx[tabindex]{position:relative;cursor:default}",
        ".lg .legend-item{display:block}",
        // The dot between two cells of the reset stands at the end of the cell before it, on a no-break space, so a line never begins with
        // it. It is drawn, not text: a reader hears no dot (the empty alternative; the line before it is for a browser without one).
        ".nx>span:not(:last-child):after{content:\"\\00a0·\";content:\"\\00a0·\"/\"\"}",
        // The small bar of the About legend: the kit's bar at the size of a mark, 12 x 16.
        ".strip-legend .bar{flex:none;display:inline-block;width:12px;min-width:0;height:16px;border-radius:var(--radius-sm)}",
        // Under the window-tabs plate, hover, focus and touch are one thing on a handle (a legend item; the reset row, when the engine says
        // which accounts reset then): its words come forward, and on the strip the
        // bars of the other kinds step back. The ring is the kit's. The bars step back by the
        // kind the plate wears (data-dim, set by the item's own events: dimOnItem), not by a
        // :has() on the item's :hover, which WebKit does not follow (it comes late and stays).
        ".lg .legend-item:is(:hover,:focus-visible),.nx[tabindex]:is(:hover,:focus-visible){color:var(--text-primary)}",
        // Under a finger a handle's words stay the size they are: the 44px is a transparent box round them that its row does not count (the
        // plate does not grow), as the note's "N more" has it.
        "@media(pointer:coarse){.lg .legend-item:after,.nx[tabindex]:after{content:\"\";position:absolute;left:calc(var(--space-1) * -1);right:calc(var(--space-1) * -1);top:50%;height:var(--tap-h);margin-top:calc(var(--tap-h) / -2)}}",
        // The one transition of a bar: the fade of the dimming, the move of the pick and the move of its opening (the basis and the most, kit: bars); its pair is under reduced motion, at the end.
        ".bars .bar{transition:opacity var(--dur-1),transform var(--dur-5) var(--ease),flex-basis var(--dur-bars) var(--ease-bars),flex-grow var(--dur-bars) var(--ease-bars),max-width var(--dur-bars) var(--ease-bars)}",
        // A row drawn anew wears .bars-fitting until the script has written the width its bars rest at (barsFit): its first styles are the token's, and a bar that is open would travel from that width to the row's own, so the row moves nothing until it is set.
        ".bars.bars-fitting .bar{transition:none}",
        ".wt-plate[data-dim=limit] .bars .bar:not(.spent){opacity:var(--dim-others)}",
        ".wt-plate[data-dim=held] .bars .bar:not(.held){opacity:var(--dim-others)}",
        ".wt-plate[data-dim=was] .bars .bar:not(.was){opacity:var(--dim-others)}",
        // The item "last known" counts the dated readings that are neither at the limit (.was) nor held back (.held): both of those wear .last
        // too, and, with no names to light, step back with the rest.
        ".wt-plate[data-dim=last] .bars .bar:is(:not(.last),.was,.held){opacity:var(--dim-others)}",
        ".wt-plate[data-dim=unknown] .bars .bar:not(.unknown){opacity:var(--dim-others)}",
        // The note under the row (a group of the accounts not read) lights the bars of its own accounts and steps the others back: the
        // plate wears data-dim=group and the bars of the group's accounts wear data-lit (litBars, set by the group's own events).
        ".wt-plate[data-dim=group] .bars .bar:not([data-lit]){opacity:var(--dim-others)}",
        // The step of the ladder pressed (a step of several accounts) holds the same light after the pointer has gone: the bars of its accounts (data-step-lit, the script) stand, the others step back, the picked bar
        // stays; a handle under the pointer or the keyboard (data-dim) has the plate meanwhile.
        ".wt-plate:not([data-dim]) .bars[data-step] .bar:not([data-step-lit],.sel){opacity:var(--dim-others)}",
        // Under the window-tabs plate, for the reader's own pick (.wt-in): the two rows rise in and the bars grow from their
        // base a step apart (the keyframes are the kit's: motion). The bars' growth holds nothing after its
        // end (backwards, not both), or it would hold the opacity the dimming above takes.
        ".wt-in>.lg,.wt-in>.nx{animation:wt-rise var(--dur-4) var(--ease) both}",
        ".wt-in>.bars>.bar{transform-origin:bottom;animation:wt-grow var(--dur-2) var(--ease) backwards}",
        // The picked bar keeps its grown look: the growth of the pick would end in a step back to the size it has not got.
        ".wt-in>.bars>.bar.sel{animation:none}",
        "@media(prefers-reduced-motion:reduce){.wt-in>.lg,.wt-in>.nx,.wt-in>.bars>.bar{animation:none}.bars .bar{transition:none}}",
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
        // The timeline (.tl-wrap) and the account's card (.ins-wrap) each stand in a fold of one row, 1fr open and 0fr shut: the one node is moved from under the plate
        // to its place right under the bars (.tl-top, the plate's area `chart`) and back, and opens and shuts by the row's height (selectInPlace). The fold clips only while
        // it moves (.fold-run), so the shadow of a part and the focus ring of Details are not cut at rest.
        ".tl-wrap,.ins-wrap{display:grid;grid-template-rows:1fr;min-width:0}",
        ".tl-wrap>.tl-fold,.ins-wrap>.ins-fold{min-height:0;min-width:0}",
        ".fold-run{transition:grid-template-rows var(--dur-5) var(--ease)}",
        ".fold-run>.tl-fold,.fold-run>.ins-fold{overflow:hidden}",
        ".fold-shut{grid-template-rows:0fr}",
        // The box that holds the folds of a pick keeps most of its height while they go their way (.fold-held, the script: foldHold): the page does not fall to the plate and rise again.
        ".fold-held{min-height:var(--fold-hold)}",
        "@media(prefers-reduced-motion:reduce){.fold-run{transition:none}}",
        ".lrows>.tl-wrap>.tl-fold>.tl-block{border-radius:var(--radius-card)}",
        ".lrows>:last-child>.ins-fold>.inspector{border-bottom-color:transparent}",
        // The card of the picked account under the chart's plate (.wt-plate>.ins-wrap): a second, narrower card whose top goes under the plate (16px in, the plate above it). At rest a strip
        // (the dot, the name, the plan; the lead window at the right); under the pointer or the keyboard (hover, focus) one more row of the windows; pressed (.kt-open) the whole card. Where
        // there is no chart over it (Chart folded, no plate) the card stands in its own place whole: the strip, the row and the button below are the plate's alone.
        ".wt-plate>.tl-wrap{position:relative;z-index:var(--z-plate);transition:grid-template-rows var(--dur-5) var(--ease-flip),padding-bottom var(--dur-5) var(--ease-flip)}",
        // The card up (.kt-open) takes the place of the chart, and the chart folds into a strip as tall as the card's own (--kt-strip): its content row goes to 0fr (its words out of the page by visibility once it has)
        // while the wrap's own bottom padding grows to the strip, on the same curve, so the row is the strip plus what is left of the chart and the card's body (the place less the strip and the gap, --kt-gap: 0 on a narrow screen, where the card keeps its own 12px) always
        // fills the rest. The wrap wears the plate's ground and corners then (the same grey as the chart's), and the strip's word stands on it (.tl-strip); the chart opens again as the card is let go. The state
        // is the card's own class, so a drawing that builds the card up builds the chart folded; .tl-yield only holds the clip while the row moves (cardPress).
        ".wt-plate:has(>.ins-wrap>.ins-fold>.inspector.kt-open)>.tl-wrap{grid-template-rows:0fr;padding-bottom:var(--kt-strip);border-radius:var(--radius-card);background:var(--inset)}",
        ".wt-plate>.tl-wrap>.tl-fold{transition:opacity var(--dur-2) var(--ease)}",
        ".wt-plate>.tl-wrap.tl-yield>.tl-fold{overflow:hidden}",
        ".wt-plate:has(>.ins-wrap>.ins-fold>.inspector.kt-open)>.tl-wrap>.tl-fold{overflow:hidden;opacity:0;visibility:hidden;transition:opacity var(--dur-2) var(--ease),visibility 0s linear var(--dur-5)}",
        // The strip's word: a button the size of the strip, over the wrap's top, hidden while the chart stands open and shown once the chart's own words have faded (--dur-2); the pointer, the keyboard focus
        // or a press on it gives the chart back (chartStrip).
        ".wt-plate>.tl-wrap>.tl-strip{position:absolute;inset:0 0 auto;display:flex;align-items:center;height:var(--kt-strip);margin:0;padding:0 var(--space-4);border:0;border-radius:var(--radius-card);background:transparent;color:var(--text-meta);font-family:inherit;font-size:var(--type-body);font-weight:var(--weight-regular);line-height:var(--lh-ui);text-align:start;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;cursor:pointer;opacity:0;visibility:hidden;transition:opacity var(--dur-2) var(--ease),visibility 0s linear var(--dur-2)}",
        ".wt-plate:has(>.ins-wrap>.ins-fold>.inspector.kt-open)>.tl-wrap>.tl-strip{opacity:1;visibility:visible;transition:opacity var(--dur-2) var(--ease) var(--dur-2),visibility 0s linear var(--dur-2)}",
        "@media(prefers-reduced-motion:reduce){.wt-plate>.tl-wrap,.wt-plate>.tl-wrap>.tl-fold,.wt-plate>.tl-wrap>.tl-strip,.wt-plate:has(>.ins-wrap>.ins-fold>.inspector.kt-open)>.tl-wrap>.tl-fold,.wt-plate:has(>.ins-wrap>.ins-fold>.inspector.kt-open)>.tl-wrap>.tl-strip{transition:none}}",
        ".wt-plate>.ins-wrap{grid-column:1/-1;position:relative;z-index:var(--z-card);margin:calc(-1*(var(--space-2) + var(--space-1) + var(--space-4))) var(--space-4) var(--space-4);transition:margin var(--dur-5) var(--ease-flip)}",
        // Nothing after the card in the plate (the fact, the notes and the stamp stand in its summary, and there is no model list): the card is the last of it and leaves no margin, no empty row of the
        // grid and no padding of the plate under it.
        ".wt-plate:has(>.ins-wrap):not(:has(>.wt-scope))>.ins-wrap{margin-bottom:0}",
        ".lrow.wt-plate:has(>.ins-wrap):not(:has(>.wt-scope)){padding-bottom:0}",
        ".wt-plate>.ins-wrap.fold-run{transition:grid-template-rows var(--dur-5) var(--ease),margin var(--dur-5) var(--ease)}",
        ".wt-plate>.ins-wrap.fold-shut{margin-top:0;margin-bottom:0}",
        // The card up (.kt-open: pressed, under the pointer or with the focus in it) takes the place of the chart and its width, no longer hung under the plate: its top is one gap (--space-2) under the chart's strip (it takes back the 8px and 4px
        // the folded chart leaves above it, less that gap), no 16px off each side, no padding for the part that stood under the plate, all four corners its own (520px and under: the card is already the plate's width, only the corners and the body change). The margins and the corners move as the chart folds and the body opens (--dur-5), so the head rises with the chart's fold and the sum of the heights stays. A shut
        // fold keeps its zero margins (it is the stronger rule); the bottom margin is the rest's (none where nothing follows the card).
        "@media not all and (max-width:520.98px){.wt-plate>.ins-wrap:not(.fold-shut):has(>.ins-fold>.inspector.kt-open){margin-top:calc(-1*var(--space-1));margin-inline:0}}",
        "@media(prefers-reduced-motion:reduce){.wt-plate>.ins-wrap,.wt-plate>.ins-wrap.fold-run{transition:none}}",
        "@media not all and (max-width:639.98px){.lrow.wt-plate.wt-up:has(>.ins-wrap){grid-template-areas:\"strip strip strip strip\" \"chart chart chart chart\" \"card card card card\" \"fact scope stamp ctl\"}.wt-plate>.ins-wrap{grid-area:card}}",
        "@media not all and (max-width:639.98px){.lrow.wt-plate.wt-up:has(>.ins-wrap):not(:has(>.wt-scope)){grid-template-areas:\"strip strip strip strip\" \"chart chart chart chart\" \"card card card card\"}}",
        ".wt-plate>.ins-wrap>.ins-fold>.inspector{padding:var(--space-4) 0 0;border:0;border-radius:0 0 var(--radius-card) var(--radius-card);background:var(--surface-capsule);box-shadow:var(--elev-rest);transition:padding var(--dur-5) var(--ease-flip),border-radius var(--dur-5) var(--ease-flip)}",
        ".wt-plate>.ins-wrap>.ins-fold>.inspector.kt-open{padding-top:0;border-radius:var(--radius-card)}",
        "@media(prefers-reduced-motion:reduce){.wt-plate>.ins-wrap>.ins-fold>.inspector{transition:none}}",
        // On a narrow screen (520px and under) the card is not a tab hung under the plate: it is as wide as the plate (no 16px off each side, so the text has the room), it does not go under the
        // plate (no negative top margin, no room kept for it in the card's padding) and wears all four corners. The shut fold above keeps its zero margins (it is the stronger rule).
        "@media(max-width:520.98px){.wt-plate>.ins-wrap{margin:0 0 var(--space-4);--kt-gap:0px}.wt-plate>.ins-wrap>.ins-fold>.inspector{padding:0;border-radius:var(--radius-card)}}",
        // The line of a pick (the stem, then the arms round the plate, drawn by the script on a layer of its own: .plate-trace, taken off at the end) ends as the
        // plate's outline (.tl-traced on the chart's block: the same line, at the same place, as a ring drawn inside the chart's wrap in its :after: an inset shadow of --trace-w) and the stem under the picked bar
        // (.bar-arrow: a vertical bar, no tip). The outline sits on the wrap, not on the block: the wrap is the chart at rest and the strip it folds into under the card, so the outline shrinks and grows with the chart,
        // whole, and stands through the card as the stem does. Every size and place of the layer is worked out in whole device pixels (traceGeo), so the live line and the outline lie on the same pixels. The line is drawn
        // by the kit's ring-sweep-arc (a path of pathLength 100 and a dash of 100, --ring-share 100: the dash offset is the keyframes' alone), linear, so the speed is one.
        // The outline and the stem come with no fade and go in --dur-1; under reduced motion there is no movement: the script draws the end state.
        ".wt-plate>.tl-wrap:after{content:\"\";position:absolute;inset:0;border-radius:var(--radius-card);box-shadow:var(--shadow-trace);opacity:0;pointer-events:none;transition:opacity var(--dur-1) var(--ease)}",
        ".wt-plate:has(>.tl-wrap>.tl-fold>.tl-block.tl-traced)>.tl-wrap:after{opacity:1;transition:none}",
        ".plate-trace,.bar-arrow{position:absolute;overflow:visible;pointer-events:none}",
        ".plate-trace{width:100%;height:100%}",
        ".plate-trace path{fill:none;stroke:var(--accent);stroke-width:var(--trace-w)}",
        ".bar-arrow rect{fill:var(--accent)}",
        ".bar-arrow{opacity:0;transition:opacity var(--dur-1) var(--ease)}",
        ".bar-arrow.on{opacity:1;transition:none}",
        ".trace-stem,.trace-arm{--ring-share:100}",
        ".trace-stem{animation:ring-sweep-arc var(--dur-trace-stem) var(--ease-trace) backwards}",
        ".trace-arm{animation:ring-sweep-arc var(--dur-trace-arms) var(--ease-trace) var(--dur-trace-stem) backwards}",
        "@media(prefers-reduced-motion:reduce){.wt-plate>.tl-wrap:after,.bar-arrow{transition:none}.trace-stem,.trace-arm{animation:none}}",
        // The wait of the plate (tl-wait, put by the script from the start of the pick's folds to the end of the line; none at the poll, at a quick second pick, or under reduced motion): the content
        // stands hidden by visibility (the plate keeps its height, the nodes live), and over it a layer of stand-ins of the same layout (.tl-skel, aria-hidden): the row of four capsules, the field of the
        // chart, the line of the legend, the line of Details, grey by --tint-3 and pulsing by opacity. When the line has met the layer fades (.sk-off) and the content comes in (tl-reveal) in --dur-4, nothing moves.
        ".tl-skel{position:absolute;inset:var(--space-3);display:flex;flex-direction:column;gap:var(--space-3);pointer-events:none;opacity:1;transition:opacity var(--dur-4) var(--ease)}",
        ".tl-skel.sk-off{opacity:0}",
        ".sk-caps{display:flex;gap:var(--space-2)}",
        ".sk-cap{flex:0 0 18%;height:var(--row-h-sm);border-radius:var(--radius-pill)}",
        ".sk-field{flex:1 1 0;min-height:0;border-radius:var(--radius-field)}",
        ".sk-legend{width:60%;height:var(--row-h-sm);border-radius:var(--radius-sm)}",
        ".sk-detail{width:36%;height:var(--row-h);border-radius:var(--radius-sm)}",
        ".sk-cap,.sk-field,.sk-legend,.sk-detail{background:var(--tint-3);animation:skel-pulse var(--dur-skel) var(--ease) infinite}",
        ".tl-block.tl-wait>:not(.tl-skel){visibility:hidden}",
        ".tl-block.tl-reveal>:not(.tl-skel){animation:tl-reveal var(--dur-4) var(--ease) backwards}",
        "@media(max-width:520.98px){.tl-skel{inset-inline:var(--space-1)}}",
        "@media(prefers-reduced-motion:reduce){.tl-skel{transition:none}.sk-cap,.sk-field,.sk-legend,.sk-detail{animation:none}.tl-block.tl-reveal>:not(.tl-skel){animation:none}}",
        ".wt-plate>.tl-top{grid-column:1/-1;margin-bottom:var(--space-2)}",
        "@media not all and (max-width:639.98px){.lrow.wt-plate.wt-up{grid-template-areas:\"strip strip strip strip\" \"chart chart chart chart\" \"fact scope stamp ctl\"}.wt-plate>.tl-top{grid-area:chart}}",
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
        ".tl-block .tl-controls .seg,.wt-plate .seg{padding:var(--space-0);border-radius:var(--radius-pill);background:var(--tint-3);gap:0}",
        ".tl-block .tl-controls .seg-opt,.wt-plate .seg-opt{min-height:var(--row-h-sm);padding:var(--space-0) 11px;border-color:transparent;border-radius:var(--radius-pill);color:var(--text-meta)}",
        // This rule outranks the kit's touch line, so the lift for a finger stands here (the kit: touch).
        "@media(pointer:coarse){.tl-block .tl-controls .seg-opt,.wt-plate .seg-opt{min-height:var(--tap-h)}}",
        ".tl-block .tl-controls .seg-opt.active,.wt-plate .seg-opt.active{background:var(--surface);border-color:transparent;color:var(--text-primary);box-shadow:var(--shadow-pop)}",
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
        // The tip over the chart: a pill of the ink, one line (the script puts it over the highest point read, centred on the cursor and held inside the plot):
        // the ink is the pill's ground and the surface its words, so in the dark theme it is a light pill. The moment, the "of N", the short name and the dots between them stand back.
        ".chart-tip{position:absolute;z-index:5;pointer-events:none;display:flex;align-items:center;gap:var(--space-2);white-space:nowrap;padding:var(--space-1) var(--space-3);border-radius:var(--radius-pill);background:var(--text-primary);color:var(--surface);box-shadow:var(--elev-rest);font-size:var(--type-meta);transform:translate(-50%,calc(-100% - var(--space-3)))}",
        // Where the room over the point is short, the pill stands under it.
        ".chart-tip.tip-below{transform:translate(-50%,var(--space-3))}",
        ".tip-row{display:flex;align-items:center;gap:var(--space-2);font-variant-numeric:tabular-nums}",
        ".tip-row svg{flex:none}",
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
        // The selected account.
        ".kt-top{position:relative}",
        ".kt-right{display:grid;align-items:center;justify-items:end;flex:0 0 auto}",
        ".kt-right>*{grid-area:1/1}",
        ".kt-lead,.kt-toggle{display:none}",
        ".wt-plate .kt-lead{display:block;font-size:var(--type-meta);line-height:var(--lh-ui);color:var(--text-meta);font-variant-numeric:tabular-nums;transition:opacity var(--dur-2) var(--ease) var(--dur-2)}",
        ".wt-plate .kt-open .kt-lead{opacity:0;transition-delay:0s}",
        ".wt-plate .kt-toggle{display:block;position:absolute;inset:0;margin:0;padding:0;border:0;border-radius:inherit;background:transparent;cursor:pointer}",
        ".wt-plate .kt-more{display:grid;grid-template-rows:0fr;transition:grid-template-rows var(--dur-5) var(--ease-flip)}",
        ".wt-plate .kt-clip{min-height:0;min-width:0;overflow:hidden;max-height:var(--kt-room);visibility:hidden;transition:visibility 0s linear var(--dur-5)}",
        ".wt-plate .kt-open .kt-more{grid-template-rows:1fr}",
        // The body of the pressed card is exactly as tall as the place of the chart it took (--kt-room, measured as it is pressed): the pad inside is no shorter than it and the clip no taller, so a short
        // body fills the place and a long one scrolls (a thin bar), its padding with it. The size stands on the pad, not on the clip: a grid item with a set height cannot fold to 0fr. Both rules hold through the
        // fold both ways, so the chart's row and the body fold at the same sizes and the sum of the two stays the place.
        ".wt-plate .kt-open .kt-clip{visibility:visible;transition-delay:0s;overflow-y:auto;scrollbar-width:thin;scrollbar-color:var(--edge-strong) transparent}",
        ".wt-plate .kt-pad{min-height:var(--kt-room);padding:0 var(--space-4) var(--space-4)}",
        ".wt-plate .kt-pad>:first-child{margin-top:0}",
        "@media(prefers-reduced-motion:reduce){.wt-plate .kt-more,.wt-plate .kt-clip,.wt-plate .kt-lead{transition:none}}",
        ".wt-plate .kt-top>.insp-head{min-height:var(--kt-strip);padding:0 var(--space-4);flex-wrap:nowrap}",
        // The name, the plan and the next window stand in one line of the strip only where there is room for all three; on a narrow screen (520px and under) the title wraps as it does outside the plate,
        // so the name keeps the width of its own text and is never squeezed to nothing by the words after it.
        "@media not all and (max-width:520.98px){.wt-plate .kt-top .insp-title{flex-wrap:nowrap}}",
        // The card in the plate draws no "Clear": the pick is kept always (the top account is picked of itself); outside the plate the button is the card's own.
        ".wt-plate .kt-right .insp-clear{display:none}",
        // The number and the notes of the plate stand in the card (cardTake), the real nodes. At rest the strip carries, on the right, the account's share and after it the number
        // again, a copy (.kt-mini, "5.53 of 16"); pressed, the pair dissolves (the plate's card has no "Clear" to take its place). Under the pointer the row of windows carries,
        // on its right, the head of the note ("At the limit · 3", a copy). Pressed, the summary stands first in the card: the fact on the left, the stamp and the chart word
        // on the right, the note and the reset under them, and a rule between it and the rest.
        ".wt-plate .kt-pair{display:inline-flex;align-items:baseline;gap:var(--space-3);white-space:nowrap}",
        ".wt-plate .kt-mini{display:inline-flex;align-items:baseline;gap:var(--space-1h);transition:opacity var(--dur-2) var(--ease) var(--dur-2)}",
        ".wt-plate .kt-mini .wt-num{grid-column:auto;grid-row:auto;font-size:var(--type-section);font-weight:var(--weight-light);line-height:var(--lh-ui);color:var(--text-primary)}",
        ".wt-plate .kt-of{font-size:var(--type-meta);line-height:var(--lh-ui);color:var(--text-meta)}",
        ".wt-plate .kt-open .kt-mini{opacity:0;transition-delay:0s}",
        ".wt-plate .kt-sum{padding:var(--space-1) 0 0}",
        ".wt-plate .kt-row{display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:var(--space-2) var(--space-4);min-width:0}",
        ".wt-plate .kt-sum .wt-fact{margin:0;justify-self:auto;align-self:auto}",
        ".wt-plate .kt-side{display:flex;align-items:center;gap:var(--space-4);flex:0 1 auto;min-width:0;margin-left:auto}",
        ".wt-plate .kt-sum .l-chart{margin:0;justify-self:auto}",
        ".wt-plate .kt-sum .wt-limit{margin:var(--space-2) 0 0;max-width:100%}",
        // The reader's pick still rises the fact and the bubble once the card has taken them (the plate keeps wt-in; the delay of a drawing in hand stays on the nodes: plateMotion runs before cardTake).
        ".wt-in .kt-sum .wt-fact{animation:wt-rise var(--dur-4) var(--ease) both}",
        ".wt-in .kt-sum .wt-limit{animation:wt-rise var(--dur-4) var(--ease) backwards}",
        "@media(prefers-reduced-motion:reduce){.wt-in .kt-sum .wt-fact,.wt-in .kt-sum .wt-limit{animation:none}}",
        ".wt-plate .kt-rule{margin:var(--space-3) 0;border-top:var(--border-w) solid var(--edge)}",
        // What follows the row of the fact in the summary (the note, the notes of the window, the states, the reset) takes one gap off it, and stands one gap under the one before.
        ".wt-plate .kt-sum>.kt-row+*{margin-top:var(--space-2)}",
        // On a narrow screen (520px and under) the stamp and the chart word take a row of their own, the words of the stamp and of the reset wrap: the card is narrower than the plate they stood in.
        "@media(max-width:520.98px){.wt-plate .kt-side{flex:1 1 100%;margin-left:0;justify-content:space-between}}",
        "@media(prefers-reduced-motion:reduce){.wt-plate .kt-mini{transition:none}}",
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
        // The windows of a limit and the plate of the picked one, built on the kit's own parts: the windows stand in the rail (kit: rail), at the
        // left of the card, on the ground: the kit's row of number tabs that wear a ring (.tab-mark-rail:
        // name, ring, the average left, the note * where it rests on part of the accounts) and, under them, the ladder of resets. The note that names who is not read stands in the
        // card, over the plate. The plate stands in the card: .lrow with its figure, the stamp of the reading, its chart word, bars and, under them, the
        // states (.calm-legend) and the reset; the scope row is the model list (.wt-scope: the kit's pill segment, its ellipsis on .wt-scope-opt). What is
        // written here is only what differs, in kit variables, and only for the classes of this capsule, under one prefix: wt-. What the plate
        // changes in the other parts' classes (the list and the row's columns and areas, the two rows under the bars - they keep the short
        // names lg and nx - the bars' dimming and the bar's new mark, the word was, the graph's row of controls) is written with those parts,
        // under the window-tabs plate.
        // The row of tabs is the kit's tablist in the rail: a column of its own height, and the tab a hair of its own, no more.
        ".wt-row{flex:0 0 auto;min-width:0}",
        // A tab is the kit's number tab with a ring (kit: rail): the word, the ring and the average left, and the note where it rests on part
        // of the accounts. A place the family has no window for has no tab; a window the reader hid in Rows keeps its tab, grey, with its hint:
        // its ring is the dotted one and its figure a dash.
        ".wt-tab{min-width:0}",
        ".wt-tab[aria-disabled=true]{color:var(--text-disabled);cursor:default}",
        // The stamp (the picked window's: of the accounts the window applies to, how many are read and how old the oldest reading is) stands in the row of the figure
        // of the plate, on its right, before the chart word: one line, "Read 18 of 20 · 2 days ago", in the meta type (12 on 16), the meta ink and the regular weight, but
        // the number read ("18 of 20") in the ink; the age is in the ink too when the oldest reading is old (past the half hour). All at the regular weight.
        // It stands on the baseline of the figure ("of 20 accounts"). The dot is a disc (--sep-size: as quiet as the " · " of the notes; --dot-size while it blinks) of the stamp's own ink and not a sign: the words a reader and a copy of the
        // text have stay in it, unseen. It blinks while a read the reader asked for (Refresh) is in the air, not on the 30 s poll (a read nobody asked for). No colour of a state:
        // nothing here is green, amber or red. It is no part of the rail. Where the row is tight (under 640px) it goes to a line of its own under the figure, at the left. It moves with the plate
        // (.wt-in>.wt-stamp rises as the figure does) and stands still on the poll.
        ".wt-stamp{grid-area:stamp;justify-self:end;align-self:center;position:relative;margin:0 0 var(--space-2);min-width:0;font-size:var(--type-meta);line-height:var(--lh-ui);font-weight:var(--weight-regular);color:var(--text-meta);text-align:right;white-space:nowrap}",
        // The budget's plate has no chart word: the stamp ends the row.
        ".wt-plate.wt-plate.wt-plate-cycle{grid-template-columns:auto minmax(0,1fr) auto;grid-template-areas:\"strip strip strip\" \"fact scope stamp\"}",
        // On the narrowest screens the figure of a billing cycle (the money, with its decimals) leaves the unit's words no column beside it: they stand under it, on one line ("Period left of $40.00"),
        // the fact's own width and not the figure's column (the figure first, a line of its own); the words take a line of their own only when they do not fit beside the name.
        "@media(max-width:279.98px){.wt-plate-cycle .wt-fact{display:flex;flex-wrap:wrap;align-items:baseline;column-gap:var(--space-1)}.wt-plate-cycle .wt-num{order:-1;flex:1 0 100%}.wt-plate-cycle .wt-fact-cap{align-self:baseline;min-width:0}}",
        // Tight (under 640px): one column, top down in the order of the markup — the bars (the rule is with the bars'), the fact, the model list, then the line of the stamp (at the left,
        // it may wrap: never cut, never a scroll across) and the chart word (its rule is with the bars' too), then the notes, the legend and the reset, which span the row as they do on
        // the wide plate. No named areas: the flow puts each part in the next row, and the chart word follows the stamp, so it joins its row at the right.
        "@media(max-width:639.98px){.wt-plate.wt-plate,.wt-plate.wt-plate.wt-plate-cycle{grid-template-columns:minmax(0,1fr) auto;grid-template-areas:none}.wt-plate.wt-plate>.wt-fact{grid-area:auto;grid-column:1/-1;justify-self:stretch}.wt-plate.wt-plate>.wt-scope{grid-area:auto;grid-column:1/-1}.wt-plate.wt-plate>.wt-stamp{grid-area:auto;grid-column:1;justify-self:start;align-self:center;text-align:left;white-space:normal}.wt-plate.wt-plate.wt-plate-cycle>.wt-stamp{grid-column:1/-1}}",
        ".wt-in>.wt-stamp{animation:wt-rise var(--dur-4) var(--ease) both}",
        // In the summary of the account card (cardTake marks what it moves with wt-carded, so the selector holds no class of the card): beside the fact, one line; 520px and under, in a row of its own with the chart word, its words wrap from the left.
        ".wt-stamp.wt-carded{margin:0;justify-self:auto;align-self:auto;white-space:nowrap}",
        "@media(max-width:520.98px){.wt-stamp.wt-carded{text-align:left;white-space:normal}}",
        // The stamp rises again in the card's summary (cardTake moves it there): no .kt-sum in the selector, this capsule styles only its own wt- classes.
        ".wt-in .wt-stamp{animation:wt-rise var(--dur-4) var(--ease) both}",
        "@media(prefers-reduced-motion:reduce){.wt-in .wt-stamp{animation:none}}",
        "@media(prefers-reduced-motion:reduce){.wt-in>.wt-stamp{animation:none}}",
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
        ".wt-foot.wt-carded{margin:var(--space-1) 0 0}",
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
        // The scope row stands at the right of the fact, on its first line, when the length has several limits: the kit's segment in its pill
        // variant (the graph's "Period"): the grey track, the picked option white and raised. Many models wrap to a second line, whole; an
        // option wider than the line is cut with an ellipsis.
        ".wt-scope{grid-area:scope;justify-self:start;min-width:0;max-width:100%;margin-top:0}",
        ".wt-scope .wt-scope-opt{display:inline-block;max-width:100%;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;transition:transform var(--dur-nav) var(--nav-spring)}",
        "@media(prefers-reduced-motion:reduce){.wt-scope .wt-scope-opt{transition:none}}",
        // Motion — transform and opacity only, and only for the reader's own pick (.wt-in):
        // the content rises in, the bars grow from their base a step apart (140 ms and
        // up to 80 ms of steps: under 220 ms in all). The keyframes are the kit's (kit: motion); what they move
        // in the other parts' classes (the figure and its line, the two rows, the bars) is
        // written with those parts, under the window-tabs plate.
        ".wt-in>.wt-scope{animation:wt-rise var(--dur-4) var(--ease) both}",
        // The note under the rows comes in the same way, and only for the reader's own pick: no motion on the 30 seconds' redraw.
        ".wt-in>.wt-foot:not(.wt-foot-in){animation:wt-rise var(--dur-4) var(--ease) both}",
        // And once the card has taken the note into its summary (cardTake marks it wt-carded, so the selector holds no class of the card) it rises the same way; the plate keeps wt-in.
        ".wt-in .wt-foot.wt-carded:not(.wt-foot-in){animation:wt-rise var(--dur-4) var(--ease) both}",
        "@media(prefers-reduced-motion:reduce){.wt-in .wt-foot.wt-carded:not(.wt-foot-in){animation:none}}",
        // And the pick of a family moves it with the rail, not on its own (the script puts .wt-foot-in on it; the plate it stands in wears .wt-in for that pick too, so the plate's
        // rise is not put on a note that wears it: .wt-in>.wt-foot:not(.wt-foot-in)): it fades in after the last window, as the foot of the rail does (kit: rail), so it is not seen alone
        // in an empty rail while the windows are still coming down.
        ".wt-foot-in{animation:rail-fade var(--dur-rail-foot) var(--ease) backwards;animation-delay:calc(var(--rail-row-at) + var(--rail-rows) * var(--rail-row-step) - var(--rail-elapsed))}",
        // On the narrow shell (the rail is a row of capsules over the card) the note follows the row. The place the hidden copy keeps would be an empty band
        // there, so it is gone: the row and the card stand close, and the card moves by the height of a note only where one is drawn.
        "@media(prefers-reduced-motion:reduce){.wt-in>.wt-scope,.wt-foot-in,.wt-in>.wt-foot:not(.wt-foot-in){animation:none}.wt-foot-g{transition:none}}"
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

    // The badge (the rail's, the large ring's option): a small circle set in the gap of the ring, its centre on the lowest point of the ring's
    // path (the angle 90 degrees: x 92, y 92 + r), the radius BADGE_R in the svg's units, a stroke of BADGE_STROKE (thinner than the track's 7: the
    // room inside is for the name, "30D" at 7.5px with a pixel of air, and the circle stands over the ends of the arc by a pixel or so), in the tone of
    // the badge's edge, the fill the ground under the rail (so it reads as set into the gap). It is drawn last, over the ends of the arc, and holds no
    // text: the short name of the window is the part's (an html node over it, kit: rail). Drawn here and nowhere else.
    var BADGE_R = 41;
    var BADGE_STROKE = 5;

    function ringGauge(pct, form, size, badge, sweep) {
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
            // The sweep (kit: ring, motion): the arc is drawn from nothing to its share and the dot runs round with it; the keyframes read the share.
            if (sweep) {
                svg.classList.add('ring-sweep');
                setVar(svg, '--ring-share', String(share));
            }
        }
        if (large && badge) {
            svg.appendChild(svgEl('circle', { 'class': 'ring-badge', r: BADGE_R, cx: 92, cy: 92 + geo.r, 'stroke-width': BADGE_STROKE }));
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
    function wordButton(key, params) {
        var word = t(key, params);
        var btn = el('button', 'btn-word');
        btn.setAttribute('type', 'button');
        var first = Array.from(word)[0] || '';
        btn.appendChild(el('span', 'btn-word-cap', first));
        btn.appendChild(document.createTextNode(word.slice(first.length)));
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
        var btn = wordButton('btn.refresh');
        btn.disabled = inFlight;
        if (inFlight) btn.setAttribute('aria-busy', 'true');
        btn.setAttribute('data-focus', 'refresh');
        btn.addEventListener('click', function () {
            if (inFlight) return;
            refreshQuota();
        });
        return btn;
    }

    // The kit's mark tab (kit: mark tab; the comment that opens that section is its contract): the helpers that build a row of choices
    // told by a mark and move the marks when the reader picks one. They stand together here and nothing else builds a tab, a mark, a
    // dot, a number or a row, so the look stays the kit's. A part calls them and writes no look of its own on what they make: only its wiring and its place.
    //   markTab({key, mark, name, count, pressed})  one tab; the caller adds its wiring (aria-label, title, data-focus, the click).
    //   markTab({key, name, count, note, selected, ring}) one number tab (no mark: its number is it; a ring of ringGauge, and a tab with neither is
    //                                               none of the kit's), a tab of a tablist: the rail's tab (kit: rail). Its number stands
    //                                               inside the box of the ring (.tab-mark-ring > .tab-mark-count, the sign % in .tab-mark-unit; a
    //                                               dash is the same span, with no unit), its short name as the badge in the gap of the ring (the
    //                                               plain word of the chip in the narrow shell), its star on the lower edge of the badge (all
    //                                               inside the box of the ring, so one transform moves them with it), and there is no dot.
    //   markRow(tabs, tablist, still)               the row (.tab-mark-row) of the tabs given, in their order: a group, or a tablist; still: the
    //                                               row shows no preview by the pointer (.tab-mark-still) until the part takes the class off.
    //   markStill(row, still)                       puts the row's preview by the pointer off (still) or gives it back: the part does it at the first move.
    //   markPreviewed(tab)                          whether the tab shows its preview now: the pointer on it (not in a still row), or the keyboard on it.
    //   markPressed(e)                              whether a click is a press of the pointer (a mouse button, a finger), not the keyboard's.
    //   markStart(row, fromKey, toKey, lit, pressed) the start state of a pick, on the marks of the tab left and the tab picked (pressed: the
    //                                               ring of a rail row starts from the press).
    //   settleMarks()                               takes the start state off at the next frame (render calls it after every drawing).
    //   markWas(scope, rowSel, dim)                 the places of the tabs of the row the capsule names (rowSel: its selector, a row of markRow, in
    //                                               the scope; they are found by key) before a drawing: one read of the layout (dim: and of
    //                                               the opacity of the tabs that give way, for a second pick inside the movement).
    //   markFlip(scope, rowSel, was)                after the drawing: one read, and each tab that moved slides from its old place to the new.
    //   markSlide(scope, rowSel, flips, elapsed, shift) puts the shifts on the tabs of a drawing (kit: motion).
    //   setVar(node, name, value)                   a custom property on a node's style (a plain style object takes it as a key).
    // The head's families are the first use (renderHarnessSeg): the tabs it draws, and the ghosts it shows before the first answer; the
    // window row's tabs (windowRow) are the number tab's.
    // markTab: key is what markStart finds the tab by (the part's own name for it; a tab that is never picked has none); mark is a node
    // of the caller's (the model's drawing, or the initial where it has none), which wears the kit's class here (a drawing takes its class
    // through classList, as an svg's className is no string); name is the text; count, when it is a number, draws the dot (out of the
    // reading order) and the number, and when it is anything else draws neither; pressed, true or false, is the choice's state (it is
    // aria-pressed, which the kit's look reads), and left out it is a tab that is no choice (a ghost). The tab is a button of type
    // "button" and nothing else is set on it: its name, its title, its focus key and its click are the caller's.
    // The number tab (kit: mark tab, the second kind): markTab({key, name, count, note, selected, ring}) with no mark. Its number is its mark
    // (no dot): count is a number or a short text (68%, a dash), and draws the number (anything else draws none); note, a
    // sign (aria-hidden) that stands after the number, is the part's to ask for (* where the figure rests on a part of what it counts);
    // selected, true or false, makes the tab a tab of a tablist (role="tab", aria-selected) and not a button of a group (aria-pressed: pressed).
    // Such a tab wears .tab-mark-num besides .tab-mark.
    function markTab(spec) {
        var tab = el('button', 'tab-mark');
        tab.setAttribute('type', 'button');
        if (spec.key !== undefined && spec.key !== null) tab.setAttribute('data-mark-key', String(spec.key));
        if (typeof spec.pressed === 'boolean') tab.setAttribute('aria-pressed', spec.pressed ? 'true' : 'false');
        if (typeof spec.selected === 'boolean') {
            tab.setAttribute('role', 'tab');
            tab.setAttribute('aria-selected', spec.selected ? 'true' : 'false');
        }
        var box = null;
        if (spec.mark) {
            spec.mark.classList.add('tab-mark-icon');
            tab.appendChild(spec.mark);
        } else if (spec.ring) {
            // The rail's tab (kit: rail), the number tab: its ring (ringGauge) stands in a box that holds the share, the name and the star too. A tab with
            // no mark and no ring is no tab of the kit (the number tab without a ring was never drawn, and is gone).
            tab.className += ' tab-mark-num tab-mark-rail';
            box = el('span', 'tab-mark-ring');
            box.appendChild(spec.ring);
            tab.appendChild(box);
        }
        var nameNode = el('span', 'tab-mark-name', spec.name);
        var figure = typeof spec.count === 'number' && isFinite(spec.count) ? String(spec.count)
            : (typeof spec.count === 'string' ? spec.count : '');
        if (box) {
            // The rail's tab: the number is drawn inside the box of the ring (centred over it, so the box's one transform moves both), the
            // sign % smaller after the digits; a dash (nothing read) is the same span with no unit, and wears the colours of a number (the meta
            // ink at rest, the accent when picked). The short name (the badge in the gap of the ring) and the star (a bead on the lower edge of
            // the badge) stand in the same box, so its one transform moves all of them. The row holds that box alone: no dot, no word after it.
            if (figure !== '') {
                var inside = /%$/.test(figure);
                var share = el('span', 'tab-mark-count');
                if (inside) {
                    share.appendChild(document.createTextNode(figure.slice(0, -1)));
                    share.appendChild(el('span', 'tab-mark-unit', '%'));
                } else {
                    share.appendChild(document.createTextNode(figure));
                }
                box.appendChild(share);
            }
            box.appendChild(nameNode);
            if (spec.note) {
                var star = el('span', 'tab-mark-note', spec.note);
                star.setAttribute('aria-hidden', 'true');
                box.appendChild(star);
            }
            return tab;
        }
        tab.appendChild(nameNode);
        if (figure !== '') {
            var dot = el('span', 'tab-mark-dot', '·');
            dot.setAttribute('aria-hidden', 'true');
            tab.appendChild(dot);
            tab.appendChild(el('span', 'tab-mark-count', figure));
        }
        return tab;
    }

    // markRow: the row of the tabs given, in their order. It is a group (role="group"), or, when the tabs are tabs of a tablist (selected),
    // a tablist (tablist true); its name is the part's to give (aria-label), and so is its place (a class of its own beside
    // .tab-mark-row: the head's .harness-seg, the window row's .wt-row), never a look.
    function markRow(tabs, tablist, still) {
        var row = el('div', 'tab-mark-row');
        if (still) row.className += ' tab-mark-still';
        row.setAttribute('role', tablist ? 'tablist' : 'group');
        tabs.forEach(function (tab) { row.appendChild(tab); });
        return row;
    }

    // markStill: a row of the kit's gets its preview by the pointer back (still false: the class .tab-mark-still goes) or loses it again
    // (still true). The part that put the row still takes it off at the first real move of the pointer.
    function markStill(row, still) {
        if (!row || !row.classList) return;
        if (still) row.classList.add('tab-mark-still');
        else row.classList.remove('tab-mark-still');
    }

    // Whether a tab shows its preview now: the pointer on it (:hover) or the keyboard on it (:focus-visible), the two ways the kit's
    // stylesheet turns the mark of a tab not picked red. Each is asked on its own, and a page that cannot answer one says no to it. In a row
    // that is still (markRow) the pointer shows no preview, so :hover is not asked.
    function markPreviewed(tab) {
        if (!tab || typeof tab.matches !== 'function') return false;
        var row = tab.parentNode;
        var still = !!(row && row.classList && typeof row.classList.contains === 'function' && row.classList.contains('tab-mark-still'));
        return (still ? [':focus-visible'] : [':hover', ':focus-visible']).some(function (state) {
            try {
                return !!tab.matches(state);
            } catch (err) {
                return false;
            }
        });
    }

    // Whether a click is a press of the pointer (the mouse button or a finger): the tab was shown pressed when it came, and a rail row's ring
    // was shrunk to --press. A click of the keyboard has no count (detail 0). markStart's `pressed` is this: the ring starts from that press.
    function markPressed(e) {
        return !!e && typeof e.detail === 'number' && e.detail > 0;
    }

    // The reader's own pick of a tab: the two marks that change are drawn in the look they had a moment ago - the mark of the tab
    // picked (toKey) in the tab's ink (tab-mark-was-off) or, when lit says it was showing its preview at the click, in the preview's red at
    // its size (tab-mark-was-lit), the mark of the tab left (fromKey) in the picked look (tab-mark-was-on) - and settleMarks takes that
    // off at the next frame, so the mark's own transition (colour and size) carries each to its look now. A redraw inside the pick's span
    // draws them so again, the transition starting as far on as the span has gone (the negative delay, as the window row's underline
    // does); any other drawing (the 30 seconds', a window's pick) gives them none: fromKey is then empty. The picked tab's look itself
    // is the markup's (aria-pressed). A rail row (kit: rail) picked by a press of the pointer (pressed, markPressed) starts its ring from the
    // press (tab-mark-was-press, the ring at --press: it was shrunk when the button came up) and not from the preview's size, so the spring
    // takes it from the press to its picked size in one go; its number and star start from the ink or the quiet look as the rest. A page with no frames to
    // take it off with draws no start state: a mark must never stay in a look that is not its own. Under reduced motion drawMotion is off, and nothing is drawn either.
    function markStart(row, fromKey, toKey, lit, pressed) {
        if (!drawMotion || !fromKey || typeof window.requestAnimationFrame !== 'function') return;
        for (var i = 0; i < row.childNodes.length; i++) {
            var tab = row.childNodes[i];
            var key = tab.getAttribute ? tab.getAttribute('data-mark-key') : null;
            if (key === null) continue;
            var was = key === toKey ? (lit ? 'tab-mark-was-lit' : 'tab-mark-was-off') : (key === fromKey ? 'tab-mark-was-on' : '');
            if (!was) continue;
            // The mark of a tab is its drawing (the first); the number tab has none, and its dot and its number are the mark.
            var drawing = [];
            var number = [];
            for (var j = 0; j < tab.childNodes.length; j++) {
                var part = tab.childNodes[j];
                if (!part.classList) continue;
                if (part.classList.contains('tab-mark-icon') || part.classList.contains('tab-mark-ring')) {
                    drawing.push(part);
                    // The share and the star of a rail tab stand in the box of its ring (kit: rail): they are the tab's number all the same.
                    for (var k = 0; k < part.childNodes.length; k++) {
                        var inner = part.childNodes[k];
                        if (inner.classList && (inner.classList.contains('tab-mark-count') || inner.classList.contains('tab-mark-note'))) number.push(inner);
                    }
                } else if (part.classList.contains('tab-mark-dot') || part.classList.contains('tab-mark-count')) number.push(part);
            }
            // A rail tab (kit: rail) moves its ring (the size) and its number and star (the ink) together; any other tab, its drawing, or its number where it has none.
            var railTab = !!tab.classList && tab.classList.contains('tab-mark-rail');
            (railTab ? drawing.concat(number) : (drawing.length ? drawing : number)).forEach(function (mark) {
                mark.classList.add(pressed && key === toKey && drawing.indexOf(mark) >= 0 ? 'tab-mark-was-press' : was);
                if (drawElapsed) mark.style.transitionDelay = -drawElapsed + 'ms';
                markStarts.push(mark);
            });
        }
    }

    // A custom property on a node's own style: the kit's motion reads its numbers from them (--i, --rail-rows, --rail-elapsed, --flip-x,
    // --flip-y). A page whose style has no setProperty takes it as a key of the style.
    function setVar(node, name, value) {
        if (!node || !node.style) return;
        if (typeof node.style.setProperty === 'function') node.style.setProperty(name, value);
        else node.style[name] = value;
    }

    // The same for taking a custom property off (a page whose style cannot take it off has nothing to take).
    function varDrop(node, name) {
        if (node && node.style && typeof node.style.removeProperty === 'function') node.style.removeProperty(name);
    }

    // The slide of the head's tabs when the reader's pick of a family puts the family picked first (FLIP). The places of the tabs are read
    // twice and no more, and only at the pick: markWas(scope, rowSel) before the drawing (where each tab of the row stands, by its key, with what it is
    // moving by now: a second pick in the middle of the slide starts from where the tab is seen) and markFlip(scope, rowSel, was) after it (how far each
    // tab that stands elsewhere now has to come, kept by its key: {x, y, ride}; the tab picked rides, the others give way). The shifts are the
    // pick's own and live in railFlips until the movement is over (a second pick inside the movement also reads, once, the opacity of the
    // tabs that are dimming, markWas's dim: the next dimming starts from it, --yield-from). markSlide(scope, rowSel, flips, elapsed, shift) puts them on the tabs of a drawing: the
    // shifts as --flip-x and --flip-y and the class .tab-mark-slide, whose keyframes (kit: motion) draw the tab at its old place and carry it to
    // its own; the tab picked wears .tab-mark-ride (over the tabs it passes, until its own slide ends), the tabs that give way
    // .tab-mark-yield (they dim for as long as it slides). A drawing inside the span of the movement (the chart's read, the poll) puts the same
    // shifts on its new tabs and carries the slide on from where it was, reading no place at all: by the page's own clock when it has one
    // (clockSet gives the animations the start time of the pick; shift is then 0), else by --rail-elapsed (shift: a negative delay). The slide
    // is never started again, and once it has run its time (FLIP_MS) a drawing puts nothing. A page that cannot read a place slides nothing: a
    // tab must never stay at a place that is not its own.
    // The tabs of the row the capsule names (rowSel, its selector of a row of markRow) in a scope (by their key), or none where the scope cannot
    // be asked: markWas, markFlip and markSlide read the same list. The kit names no class of a part: the row is the capsule's to name.
    function markTabs(scope, rowSel) {
        return scope && scope.querySelectorAll && rowSel ? scope.querySelectorAll(rowSel + ' [data-mark-key]') : [];
    }

    function markWas(scope, rowSel, dim) {
        var was = {};
        try {
            var tabs = markTabs(scope, rowSel);
            for (var i = 0; i < tabs.length; i++) {
                if (typeof tabs[i].getBoundingClientRect !== 'function') continue;
                var box = tabs[i].getBoundingClientRect();
                was[tabs[i].getAttribute('data-mark-key')] = { left: box.left, top: box.top, dim: dim ? dimOf(tabs[i]) : undefined };
            }
        } catch (err) { /* no places to read: nothing slides */ }
        return was;
    }

    // The look a tab that gives way has now (dim, the one read of an opacity the movement makes, and only for a second pick inside it): its
    // opacity, as the next dimming starts from it (--yield-from). Only a tab that is dimming is asked; undefined is no say (it starts from 1).
    function dimOf(tab) {
        try {
            if (!tab.classList || !tab.classList.contains('tab-mark-yield')) return undefined;
            var seen = parseFloat(window.getComputedStyle(tab).opacity);
            return isFinite(seen) ? String(Math.round(seen * 1000) / 1000) : undefined;
        } catch (err) {
            return undefined;
        }
    }

    function markFlip(scope, rowSel, was) {
        var flips = {};
        try {
            var tabs = markTabs(scope, rowSel);
            for (var i = 0; i < tabs.length; i++) {
                var key = tabs[i].getAttribute('data-mark-key');
                var from = was[key];
                if (!from || typeof tabs[i].getBoundingClientRect !== 'function') continue;
                var to = tabs[i].getBoundingClientRect();
                var dx = from.left - to.left;
                var dy = from.top - to.top;
                if (Math.abs(dx) < 0.5 && Math.abs(dy) < 0.5) continue;
                // The tab picked is the one that rides to the front; the others that move are the ones that give way.
                flips[key] = { x: dx.toFixed(1) + 'px', y: dy.toFixed(1) + 'px', ride: tabs[i].getAttribute('aria-pressed') === 'true', dim: from.dim };
            }
        } catch (err) { flips = {}; /* a place that could not be read: no tab slides */ }
        return flips;
    }

    function markSlide(scope, rowSel, flips, elapsed, shift) {
        if (elapsed >= FLIP_MS) return;
        try {
            var tabs = markTabs(scope, rowSel);
            for (var i = 0; i < tabs.length; i++) {
                var flip = flips[tabs[i].getAttribute('data-mark-key')];
                if (!flip) continue;
                setVar(tabs[i], '--flip-x', flip.x);
                setVar(tabs[i], '--flip-y', flip.y);
                if (shift) setVar(tabs[i], '--rail-elapsed', shift + 'ms');
                tabs[i].classList.add('tab-mark-slide');
                if (!flip.ride) {
                    tabs[i].classList.add('tab-mark-yield');
                    // A second pick inside the movement: the dimming starts from the look the tab had when it was picked.
                    if (flip.dim !== undefined) setVar(tabs[i], '--yield-from', flip.dim);
                    continue;
                }
                tabs[i].classList.add('tab-mark-ride');
                // The end of its own slide gives the tab back to its layer; a slide that is cut short (the tab is drawn again) takes the class
                // with the node, and the drawing puts it on its new one.
                (function (tab) {
                    tab.addEventListener('animationend', function ended(e) {
                        // The tab's own slide (not an animation of a part of it, which is heard here too).
                        if (e && ((e.animationName && e.animationName !== 'rail-slide') || (e.target && e.target !== tab))) return;
                        tab.classList.remove('tab-mark-ride');
                        if (typeof tab.removeEventListener === 'function') tab.removeEventListener('animationend', ended);
                    });
                })(tabs[i]);
            }
        } catch (err) { /* the tabs stand at their own places */ }
    }

    // The marks of the drawing that wear a start state lose it at the next frame. Their look is read first, while they still wear
    // it, so the browser holds it as the look the change starts from (a mark born a moment ago has none, and would take its look
    // now, at once); then the class goes, and the mark's own transition (colour and size) carries it to its look now. A page that
    // cannot be read here still loses the class: no mark stays in a look that is not its own. A mark that was given a delay (a
    // drawing inside the pick's span) loses that one a frame later, when its transition has begun with it: the mark's later
    // changes (the preview of its tab) run with the sheet's own timing.
    function settleMarks() {
        var marks = markStarts;
        markStarts = [];
        if (!marks.length) return;
        window.requestAnimationFrame(function () {
            try {
                marks.forEach(function (mark) {
                    var look = window.getComputedStyle(mark);
                    void look.transform;
                    void look.color;
                });
            } catch (err) { /* no look to hold: the class goes all the same */ }
            marks.forEach(function (mark) {
                mark.classList.remove('tab-mark-was-off');
                mark.classList.remove('tab-mark-was-lit');
                mark.classList.remove('tab-mark-was-press');
                mark.classList.remove('tab-mark-was-on');
            });
            var delayed = marks.filter(function (mark) { return !!mark.style.transitionDelay; });
            if (delayed.length) {
                window.requestAnimationFrame(function () {
                    delayed.forEach(function (mark) { mark.style.transitionDelay = ''; });
                });
            }
        });
    }

    // The width of the rail and its axis (kit: rail), the one read of the head's tabs at the end of a drawing and when the fonts are in (the words of a
    // tab are wider or narrower by the font): the widest tab of the row is read into --rail-tab-w, and the rail's width (--rail-w) is what a ring needs at
    // the axis of that tab, the same for every family, so the edge of the card does not move with the pick; the axis is the middle of the whole tab picked
    // (--rail-axis), which stands first in the row, at the edge of the widget. The number of a tab holds two digits' room, so neither moves with the count
    // of accounts (up to 99). Only the width of a tab is read, as the layout width of its box (offsetWidth: a slide or a start state moves
    // and scales a tab, they never widen it, and the places of the tabs are not asked again: markWas and markFlip read them twice at the pick, no more).
    // It is read right after markFlip, before the slide writes its classes, so the layout the pick's own read has just worked out answers it, and a
    // drawing that is not a pick (the poll, the chart's read, a window's pick) asks for the layout once, as the fits that follow do. A page that cannot be
    // asked, or tabs with no width (hidden), leave the tokens at their own values: the widest tab known, the axis in its middle.
    var railFontsBound = false;

    function railFit() {
        try {
            var tabs = markTabs(root, HEAD_ROW);
            var html = document.documentElement;
            var widest = 0;
            var picked = 0;
            for (var i = 0; i < tabs.length; i++) {
                var w = tabs[i].offsetWidth;
                if (!(w > 0)) continue;
                if (w > widest) widest = w;
                if (tabs[i].getAttribute('aria-pressed') === 'true') picked = w;
            }
            if (widest > 0) setVar(html, '--rail-tab-w', widest.toFixed(2) + 'px');
            if (picked > 0) setVar(html, '--rail-axis', (picked / 2).toFixed(2) + 'px');
            if (!railFontsBound && document.fonts && document.fonts.ready && typeof document.fonts.ready.then === 'function') {
                railFontsBound = true;
                document.fonts.ready.then(railFit);
            }
        } catch (err) {
            // A layout that cannot be read leaves the rail at its own width and the axis in the middle of the widest tab.
        }
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

    function readI18n() {
        return request(I18N_ROUTE, { method: 'GET' }, I18N_TIMEOUT_MS).then(function (result) {
            var answer = null;
            if (result.ok) {
                try { answer = JSON.parse(result.body); } catch (err) { answer = null; }
            }
            adoptI18n(answer);
            // The first screen waits for its text: it is drawn as soon as the text is here.
            if (i18n.en && !currentView && !stopped) rerender();
        })['catch'](function () { i18n.asked = true; });
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
                : (h.kind === 'cooldown' ? ', ' + (COOLDOWN_END[h.note] || COOLDOWN_END.not_reported) : '');
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
        var found = GROUP_WINDOW.exec(String(g.meaning || '').trim()) || GROUP_WINDOW.exec(String(g.label || '').trim());
        return found ? found[1].trim() : '';
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
        not_read: 'no usable reading',
        unreadable: 'its reading could not be read',
        sources_disagree: 'its sources disagree',
        reset_passed: 'its window reset after the last reading',
        too_old: 'its last reading is older than the window',
        no_reading_in_answer: 'no reading in this answer'
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

    // The row of the bars goes sideways where its bars do not fit at their least width (kit: bars). Where it stands is kept by the limit's key through
    // the redrawing (the whole tree is rebuilt every 30 seconds) and put back when the drawing is done (barsFit). Its edges fade, as the ladder's window of
    // the rail does (kit: rail, the haze): the classes are put by the script where an edge is reached or left, and a row that fits wears none.
    var barScroll = {};
    var barsBound = false;

    // The chart is drawn to the room its card has (chartSize). The size it was drawn to is kept, and a window that changes its width afterwards draws it anew:
    // once in CHART_FIT_MS while the width keeps changing, and once more when it stops, the drawing being the one the poll makes (the pick, the focus and the chart's cursor are carried over).
    // It answers no pick, so it moves nothing (fitDrawing: render puts no motion on it): the bars, the rail and the rings do not come in again as the window is dragged.
    var CHART_FIT_MS = 120;
    var chartDrawn = null;
    var chartFitTimer = 0;
    var fitDrawing = false;
    function chartFit() {
        if (chartFitTimer) return;
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

    function barEdges(strip) {
        var max = strip.scrollWidth - strip.clientWidth;
        var over = max > 1;
        var at = strip.scrollLeft || 0;
        // A class is put on or taken off only where its state changes.
        [['rail-scroll-x', over], ['rail-scroll-x-start', over && at <= 1], ['rail-scroll-x-end', over && at >= max - 1]].forEach(function (flag) {
            if (hasClass(strip, flag[0]) !== flag[1]) classSet(strip, flag[0], flag[1]);
        });
    }

    // A bar that has opened (kit: bars) by the keyboard on it, or as the picked one, or as the first of those a pressed step of the ladder opened, is brought into the room of its row where its opening reaches past it (the row
    // goes sideways where it cannot hold the opening): inline 'nearest' and only then, block 'nearest' so that a page that shows the row does not jump. The edges are worked out after.
    var barSeen = '';

    function barBring(strip, bar) {
        try {
            // The first of the bars a pressed step opens is the one brought in (the others follow it in the row, which goes sideways where they do not fit).
            var by = bar && bar.matches && (bar.matches(':focus-visible') || bar.classList.contains('sel')
                || (bar.classList.contains('bar-on') && strip.querySelector('.bar-on') === bar));
            if (by && bar.scrollIntoView) {
                var room = strip.getBoundingClientRect();
                var box = bar.getBoundingClientRect();
                if (box.left < room.left - 0.5 || box.right > room.right + 0.5) bar.scrollIntoView({ block: 'nearest', inline: 'nearest' });
            }
        } catch (err) {
            // A layout that cannot be read leaves the row where it is: the keyboard's own scroll to the focus has brought the bar as it was.
        }
        barEdges(strip);
    }

    // The width a bar of the row has at rest (kit: bars, --bar-rest), written on the row: the liquid of a bar that opens keeps it. It is worked out from the row, not read
    // off a bar (one may be open): the room less the gaps, shared by the bars, between --bar-min and --bar-max (each with the border a finger's bar wears, which
    // is not drawn in).
    function barRest(strip) {
        try {
            var style = window.getComputedStyle(strip);
            var bars = strip.children || [];
            var edge = bars.length ? window.getComputedStyle(bars[0]) : null;
            var border = edge ? (parseFloat(edge.borderLeftWidth) || 0) + (parseFloat(edge.borderRightWidth) || 0) : 0;
            var least = parseFloat(style.getPropertyValue('--bar-min'));
            var most = parseFloat(style.getPropertyValue('--bar-max'));
            if (!bars.length || !isFinite(least) || !isFinite(most)) return;
            var share = (strip.clientWidth - (parseFloat(style.columnGap) || 0) * (bars.length - 1)) / bars.length;
            var rest = Math.max(0, Math.min(Math.max(share, least + border), most + border) - border);
            setVar(strip, '--bar-rest', rest.toFixed(2) + 'px');
        } catch (err) {
            // A layout that cannot be read leaves the token at its own value (the widest a bar rests): a bar that opens then keeps that.
        }
    }

    // The one read of the layout for the rows of the bars, at the end of a drawing (with the ladder's fit) and when the width changes: the place each row
    // had is put back, the width a bar rests at is written, and the edges are worked out again. No observer and no script by frames.
    function barsFit() {
        // The shell narrowed under 860px: the ladder is not drawn there, so a step pressed has nothing to be pressed again by: it lets go of its bars.
        if (stepOn && narrowShell()) stepClear();
        try {
            var strips = rootAll('.bars');
            for (var i = 0; i < strips.length; i++) {
                var key = strips[i].getAttribute('data-strip');
                if (key && barScroll[key] && strips[i].scrollLeft !== barScroll[key]) strips[i].scrollLeft = barScroll[key];
                barRest(strips[i]);
                // The row drawn anew moves nothing until its width is written: the styles are read once with it in place, and then it is let go.
                if (hasClass(strips[i], 'bars-fitting')) {
                    void strips[i].offsetWidth;
                    classSet(strips[i], 'bars-fitting', false);
                }
                barEdges(strips[i]);
                // Under a finger the picked bar is the open one: where the pick is new it is brought in (a redrawing alone does not take the row from where the reader left it).
                if (selectedAccountKey && selectedAccountKey !== barSeen) {
                    var picked = strips[i].querySelector('.bar.sel');
                    if (picked) barBring(strips[i], picked);
                }
            }
            barSeen = selectedAccountKey || '';
            arrowFit();
            if (!barsBound && window.addEventListener) {
                barsBound = true;
                window.addEventListener('resize', barsFit);
                window.addEventListener('resize', chartFit);
            }
        } catch (err) {
            // The haze and the kept place are a comfort: a layout that cannot be read leaves the row as it is.
        }
    }

    // What a bar of an account opens to (kit: bars): hidden from a reader, who has the bar's aria-label. The account's name first, then by the state of the bar (the order
    // of the classes it wears: unknown, was, spent, held (a held bar that is dated too, .held.last, says it is held back now, not the date alone), last, and a current one) what its limit is doing and when it was read, as the note and the ladder say it (untilWords,
    // the age phrases of foot.read_ago and foot.read_just_now for a dated value). A line with nothing to say (no reset reported, or one that has passed; no time of reading) is not drawn.
    function barOpened(bar, index, known) {
        var box = el('span', 'bar-i');
        box.setAttribute('aria-hidden', 'true');
        var dated = bar.state === 'last_known';
        var at = Date.parse(String(bar.resets_at || ''));
        var until = isFinite(at) && at > Date.now() ? untilWords(at) : '';
        var part = agePart(bar.observed_at);
        var read = '';
        if (part) {
            if (dated) read = part.k === 'now' ? t('foot.read_just_now') : t('foot.read_ago', { age: agePhrase(part) });
            else read = t('bar.read', { age: agoWords(bar.observed_at) });
        }
        var reset = until ? t('bar.reset_in', { 'in': until }) : '';
        var lines = [[accountName(index, bar.account), 'bar-n']];
        if (!known) lines.push([t('bar.no_data'), '']);
        else if (bar.at_limit && dated) lines.push([t('bar.was_at_limit'), ''], [read, '']);
        else if (bar.at_limit) lines.push([t('bar.at_limit'), 'bar-a'], [until ? t('plate.back', { n: 1, 'in': until }) : '', '']);
        else if (bar.restricted && heldNow(bar)) lines.push([t('bar.restricted_now'), 'bar-a'], [reset, '']);
        else if (dated) lines.push([read, ''], [reset, '']);
        else lines.push([reset, ''], [read, '']);
        lines.forEach(function (line) {
            if (!line[0]) return;
            var node = el('span', line[1], line[0]);
            // The name of the account whole, for a name the opening cuts.
            if (line[1] === 'bar-n') node.title = line[0];
            box.appendChild(node);
        });
        return box;
    }

    // One bar per account the limit applies to, as tall as its share left,
    // fullest first in the skill's order: hatched for a dated last-known
    // value, the ink on its base when a restriction holds it back now or it
    // is at the limit now (a dashed base when it was at the limit when last
    // read), an outlined "?" (never a zero) for no usable value. A bar selects its
    // account — the same selection the account list makes — and the bars of
    // that account are marked in every window shown. A bar of an account opens to the
    // side under the pointer (barOpened): it carries no title of its own, the opening
    // would be covered by one in English; a bar of no account has nothing to open and keeps its title.
    function barStrip(g, rowName, index) {
        var bars = barsOf(g);
        var strip = el('div', barStill ? 'bars bars-fitting bars-still' : 'bars bars-fitting');
        strip.setAttribute('role', 'group');
        strip.setAttribute('aria-label', rowName + ': each account’s share left, fullest first');
        strip.setAttribute('data-strip', g.key);
        strip.addEventListener('scroll', function () {
            // A scroll that lands after a redraw took this row out must not write its place over the new row's (a page always has the property;
            // a stand-in page may not, and is then taken as connected).
            if (strip.isConnected === false) return;
            barScroll[g.key] = strip.scrollLeft;
            barEdges(strip);
            arrowFit();
        }, { passive: true });
        // A bar that opens (or shuts) may take the row past its room (or give it back): where the move ends the edges are worked out again, on the end of the move alone.
        strip.addEventListener('transitionend', function (e) {
            if (strip.isConnected === false) return;
            if (e && (e.propertyName === 'flex-basis' || e.propertyName === 'max-width')) {
                barBring(strip, e.target);
                arrowFit();
            }
        }, { passive: true });
        // The keyboard on a bar opens it: where nothing moves (reduced motion) it is brought in at once, otherwise at the end of its move (above).
        strip.addEventListener('focusin', function (e) {
            if (strip.isConnected === false) return;
            barBring(strip, e && e.target);
        }, { passive: true });
        // The step of the ladder that is pressed (stepAccounts): a step of one account is that account's pick (its bar wears .bar-on with .sel); a step of several lights their bars from the building
        // (data-step-lit on them, data-step on the row) and opens none.
        var opened = stepAccounts(g);
        var lights = !!stepOn && !stepOn.account && opened.length > 0;
        if (lights) strip.setAttribute('data-step', '1');
        bars.forEach(function (bar, i) {
            var known = bar.state !== 'unknown' && typeof bar.left === 'number' && isFinite(bar.left);
            var cls = 'bar';
            if (!known) cls += ' unknown';
            if (bar.state === 'last_known') cls += ' last';
            // A current reading at the limit is an empty track on the ink base (spent); a last-known zero stays
            // hatched and dated, never current exhaustion — and wears its own
            // mark, a dashed base (was), so it is told from an empty bar. No bar wears a colour of a state.
            if (known && bar.at_limit && bar.state !== 'last_known') cls += ' spent';
            if (known && bar.at_limit && bar.state === 'last_known') cls += ' was';
            if (known && bar.restricted) cls += heldNow(bar) ? ' held restricted' : ' restricted';
            var isSel = !!bar.account && bar.account === selectedAccountKey;
            if (isSel) cls += ' sel';
            var held = !!bar.account && opened.indexOf(bar.account) >= 0;
            if (held && !lights) cls += ' bar-on';
            var b = el('button', cls);
            b.setAttribute('type', 'button');
            if (held && lights) b.setAttribute('data-step-lit', '1');
            if (!known) b.appendChild(el('span', 'sr-only', '?'));
            b.setAttribute('data-focus', 'bar:' + g.key + ':' + (bar.account || i));
            b.setAttribute('data-state', bar.state || 'current');
            if (bar.account) b.setAttribute('data-account', bar.account);
            if (known) {
                var left = Math.max(0, Math.min(1, bar.left));
                var pct = +(left * 100).toFixed(4);
                if (left > 0) {
                    var f = el('span', 'fill');
                    f.style.height = pct + '%';
                    b.appendChild(f);
                }
                // The number over the fill: the share left in percent, "<1" where it is something and rounds to nothing; the accounts' words say it for a reader.
                var rounded = Math.round(left * 100);
                // (and "99" where it is a little short of all and rounds to 100: the full bar alone says 100)
                var value = el('span', 'bar-v' + (pct > 80 ? ' in' : ''), left > 0 && rounded === 0 ? '<1' : String(left < 1 && rounded >= 100 ? 99 : rounded));
                value.setAttribute('aria-hidden', 'true');
                value.style.bottom = pct > 80 ? 'calc(' + pct + '% - var(--bar-v-in))' : 'calc(' + pct + '% + var(--space-1h))';
                b.appendChild(value);
            }
            var words = barWords(bar, index, rowName);
            // The words of the opening (a bar of an account) stand where a title would, and in the reader's language; a bar of no account does not open and keeps its title.
            if (bar.account) {
                b.appendChild(barOpened(bar, index, known));
                b.setAttribute('data-said', words);
            } else {
                b.title = words;
            }
            b.setAttribute('aria-label', words + (bar.account ? barPickWords(isSel) : ''));
            b.setAttribute('aria-pressed', isSel ? 'true' : 'false');
            if (bar.account) {
                b.addEventListener('click', function (e) {
                    e.stopPropagation();
                    holdBarStill(e);
                    selectAccount(bar.account === selectedAccountKey ? '' : bar.account,
                        'bar:' + g.key + ':' + bar.account, g.key);
                    restoreFocus('bar:' + g.key + ':' + bar.account);
                });
            } else {
                b.disabled = true;
            }
            strip.appendChild(b);
        });
        return strip;
    }

    // The list of accounts opens or shuts (the word over the bars, a row of the list, the "N more" of the notes). It is the one place that sets accountsOpen: opening measures the place the
    // list replaces now, and the drawing that follows plays the fold (accountsRun). The caller draws.
    function accountsSet(open) {
        if (accountsOpen === open) return;
        if (open) {
            var place = root && typeof root.querySelector === 'function' ? root.querySelector('.acc-main') : null;
            // Not rounded down: a pixel lost here is a pixel the widget loses while the list stands (rounded up to a hundredth).
            accountsSpace = place && typeof place.getBoundingClientRect === 'function' ? Math.ceil(place.getBoundingClientRect().height * 100) / 100 : 0;
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
        // Another account (a bar, a row of the list, a step of the rail, or the pick let go) lets the pressed card go: the chart leads, so the new card stands as a strip and the chart
        // opens under its bar (foldPlay hands the card over). The same account (the poll, a second press of its bar) leaves the press as it is.
        if ((key || '') !== selectedAccountKey) cardReset();
        // A pick of an account (or the pick let go) lets go of a step of several accounts that was lit; the step of one account (stepPress sets it before) is the pick itself.
        if (stepOn && !stepOn.account) stepOn = null;
        selectedAccountKey = key || '';
        selectionOpener = selectedAccountKey ? (opener || '') : '';
        selectionLimit = selectedAccountKey ? (limitKey || '') : '';
        // The reader's own pick (or the pick let go): the widget picks no more for it, in the window on screen, until the window or the family changes (pickDecide).
        pickAuto = false;
        pickDue = false;
        pickHome = selectedAccountKey ? pickAt : '';
        pickBare = selectedAccountKey ? '' : pickAt;
        diagnosticsOpen = false;
        if (!selectInPlace(limitKey)) rerender();
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
            if (stepOn && stepOn.account && stepOn.account !== selectedAccountKey) stepOn = null;
            if (!next) card = null;
        }
        pickHome = selectedAccountKey ? here : '';
        if (rebuilt && selectedAccountKey) card = inspectorSlot(family, accountIn(accounts, selectedAccountKey), (view && view.facets) || {});
        // The line of the pick is drawn once for a pick that is new to the window on screen: at the opening, at a pick of a window or a family.
        if (moved && selectedAccountKey) {
            pickDue = true;
            // The line is not owed for ever: from here the chart has WAIT_MAX_MS to come (waitArm), then the end state stands.
            waitBy = Date.now() + WAIT_MAX_MS;
        }
        if (!selectedAccountKey) pickDue = false;
        return card;
    }

    // Whether the chart is yet to come (a read in the air, or the chart of another limit that the drawing will ask for): the line of the pick waits for it, for a drawing in the middle of the
    // line would take the layer off and show the end at once.
    function chartPending() {
        return inFlight || (!!currentView && chartMissing(currentView) && reserveUrl(true) !== chartAsked);
    }

    // At the end of a drawing that owes the line of the pick (pickDue): the arrow stays off (traceHold) while the line waits for the chart, then the line is drawn, once, by the plate that
    // wears the pick. Where the chart is not over the plate (folded, or nothing picked in it), nothing is owed.
    function pickTraceHold() {
        if (pickDue) traceHold = true;
    }

    function pickTraceRun() {
        if (!pickDue) return;
        var plate = null;
        var wrap = null;
        try {
            plate = root && root.querySelector ? root.querySelector('.wt-plate') : null;
            var strip = plate ? plateBars(plate) : null;
            wrap = plate ? plateKid(plate, 'tl-top') : null;
            if (!plate || !strip || !wrap || !pickedIn(strip) || !blockOf(wrap)) {
                pickDue = false;
                traceHold = false;
                waitStop(false);
                return;
            }
            // The keyboard came into the plate (the focus restored by the drawing): the content keeps it, no stand-ins.
            if (heldIn(waitBlock)) waitStop(false);
            if (chartPending()) {
                // The chart is yet to come: the line waits, but not past WAIT_MAX_MS (waitExpire), with stand-ins or without them.
                if (!waitDeadline) waitArm();
                return;
            }
            pickDue = false;
            traceStart(plate, wrap, true);
        } catch (err) {
            // A layout that cannot be read owes nothing: the end state stands (the outline, the arrow).
            pickDue = false;
            traceHold = false;
            try {
                if (plate && wrap && blockOf(wrap)) traceRest(plate, wrap);
            } catch (inner) { /* the next drawing builds it */ }
        }
    }

    // What a bar of an account, or a row of the list, says besides its words: the pick it wears. The words stand apart in its `data-said` and the aria-label is the two put together,
    // so a pick in its place (selectInPlace) changes the pick alone, whatever language the words are in. The one place the pick words are made, for a drawing (barStrip,
    // renderAccounts) and for a pick in its place.
    function barPickWords(isSel) {
        return isSel ? ' — selected' : ' — select this account';
    }

    function rowPickWords(isOn) {
        return isOn ? ' · selected' : '';
    }

    // The folds the timeline (.tl-wrap > .tl-fold) and the account's card (.ins-wrap > .ins-fold) stand in: a node in a fold of one row, 1fr open and 0fr shut. `shut` makes it
    // already shut and ready to open (it opens when .fold-shut is taken off in the next frame, selectInPlace).
    function foldOf(kind, node, shut) {
        var wrap = el('div', kind + '-wrap' + (shut ? ' fold-run fold-shut' : ''));
        var fold = el('div', kind + '-fold');
        fold.appendChild(node);
        wrap.appendChild(fold);
        return wrap;
    }

    // The first child of a node that wears a class, read from the children alone (null when there is none).
    function plateKid(node, name) {
        for (var i = 0; node && i < node.childNodes.length; i++) if (hasClass(node.childNodes[i], name)) return node.childNodes[i];
        return null;
    }

    // The bars of a plate and the bar of them that is picked (.sel).
    function plateBars(plate) {
        return plateKid(plate, 'bars');
    }

    function pickedIn(strip) {
        return plateKid(strip, 'sel');
    }

    // The motion of the folds belongs to the last pick: a newer pick, and any drawing of the page, cancels what is under way (the token, the timer), and the screen is the
    // state the last pick (or drawing) left.
    var foldToken = 0;
    var foldTimer = 0;

    function foldCancel(keepHold) {
        foldToken++;
        if (foldTimer) {
            clearTimeout(foldTimer);
            foldTimer = 0;
        }
        foldUnhook();
        traceStop();
        if (!keepHold) foldLetAll();
    }

    // While the folds of a pick go their way (the chart and the card shut where they stand, then open where they are to stand) the box that holds them keeps most of its height (.fold-held, its
    // least is --fold-hold): the page does not fall to the plate and rise again, which a host that fits its frame to the page shows as everything under the widget going up and down. What is kept is
    // FOLD_HOLD of the height the box had: the state the folds end in is not found under it (a pick lets the chart and the card stand at heights that differ by a tenth), so the hold
    // is let go at the end with nothing moving. A pick on a pick goes on holding the first height; the hold goes with the motion (foldCancel without keepHold: a drawing of the page, which builds the box anew).
    var FOLD_HOLD = 0.85;

    function foldHold(box) {
        if (!box || hasClass(box, 'fold-held')) return;
        var height = box.getBoundingClientRect().height;
        if (!(height > 0)) return;
        setVar(box, '--fold-hold', (height * FOLD_HOLD).toFixed(1) + 'px');
        classSet(box, 'fold-held', true);
    }

    function foldLet(box) {
        if (!box || !hasClass(box, 'fold-held')) return;
        classSet(box, 'fold-held', false);
        varDrop(box, '--fold-hold');
        // The hold was the only thing in the style attribute: the emptied attribute goes too, so the markup of the box is what a drawing makes anew (the same before a pick and after it).
        if (typeof box.getAttribute === 'function' && typeof box.removeAttribute === 'function' && !box.getAttribute('style')) box.removeAttribute('style');
    }

    function foldLetAll() {
        var held = rootAll('.fold-held');
        for (var i = 0; i < held.length; i++) foldLet(held[i]);
    }

    // The one listener of the fold that is going its way (transitionend on its wrap): it is taken off when it has fired, when the timer ends the wait, and when a newer pick or a drawing cancels
    // the motion (foldCancel), so a wrap that lives through many picks wears one at most, never a heap.
    var foldHook = null;

    function foldUnhook() {
        var hook = foldHook;
        foldHook = null;
        if (!hook) return;
        try { hook.node.removeEventListener('transitionend', hook.fn); } catch (err) { /* it goes with the node */ }
    }

    // `then` once the fold `node` has gone its way (its row's transition ends; a short timer ends it where the transition never starts), if no newer pick has come.
    function foldWait(node, token, then) {
        var over = false;
        foldUnhook();
        function end() {
            if (over) return;
            over = true;
            foldUnhook();
            if (foldTimer) {
                clearTimeout(foldTimer);
                foldTimer = 0;
            }
            if (token === foldToken) then();
            landPoke();
        }
        function onEnd(e) {
            if (e.target === node && e.propertyName === 'grid-template-rows') end();
        }
        try {
            node.addEventListener('transitionend', onEnd);
            foldHook = { node: node, fn: onEnd };
        } catch (err) { /* the timer ends it */ }
        foldTimer = setTimeout(end, 320);
    }

    // A reading of the 30-second poll that lands while a fold or the line is moving is held until the motion has ended (a drawing in the middle of it would jump the plate's height), and no longer
    // than LAND_MAX_MS. Only the moment of the drawing moves: the poll reads as it did. A newer reading, a stop, or a read of a chart or a limit (reuse, which is the reader's own) drops or passes it.
    var LAND_MAX_MS = 1000;
    var landHeld = null;
    var landTimer = 0;

    // The line a drawing of the page owes (the opening, a pick of a window or a family: traceStart with `byPage`) does not hold the poll: it moves no height (a layer over the plate),
    // and a family's slide has the poll drawn in the middle of it (the drawing takes the layer off and the end state stands, as the chart's arrival does). The folds and the line of the
    // reader's press on a bar hold it.
    var traceByPage = false;

    function motionOn() {
        return !!foldTimer || (!traceByPage && (!!traceTimer || !!traceLive));
    }

    function landRelease(go) {
        if (landTimer) {
            clearTimeout(landTimer);
            landTimer = 0;
        }
        var held = landHeld;
        landHeld = null;
        if (go && held && held.mine === generation && !stopped) held.run();
    }

    function landWhenStill(run, mine) {
        landRelease(false);
        if (!motionOn()) {
            run();
            return;
        }
        landHeld = { run: run, mine: mine };
        landTimer = setTimeout(function () {
            landTimer = 0;
            landRelease(true);
        }, LAND_MAX_MS);
    }

    function landPoke() {
        if (landHeld && !motionOn()) landRelease(true);
    }

    // The line of a pick (capsule: chart, kit: motion): from the foot of the picked bar a stem (a bar, no tip) runs down to the top edge of the chart's plate, and from that point
    // two arms run round the plate's edge (half the line's width in, the middle of the line, with the plate's own corners) and meet at the point of the foot opposite the stem, the arms of one length. It is drawn on a layer of its own over the plate
    // (.plate-trace, aria-hidden) by the kit's ring-sweep-arc, and at its end the layer is taken off and, in the same turn, the plate wears the same line as its outline (.tl-traced) and the bar
    // the stem (.bar-arrow): no jump. A pick of the reader's draws it (a press on a bar, traceStart at the end of the folds) and so does the page's own pick (pickTraceRun: the opening,
    // a pick of a window or a family; traceStart with `byPage`); any other drawing of the page builds the end state (the build, and barsFit for the arrow). A newer pick, and any
    // drawing, takes the layer and the timer off (traceStop). `traceHold` keeps the arrow off while the folds of a pick are moving.
    var traceHold = false;
    var traceLive = null;
    var traceTimer = 0;

    // The arrows of a plate (svg.bar-arrow, children of the plate), all of them, never the first alone (hasClass reads an svg node by its class attribute).
    function arrowsOf(plate) {
        var out = [];
        for (var i = 0; plate && i < plate.childNodes.length; i++) if (hasClass(plate.childNodes[i], 'bar-arrow')) out.push(plate.childNodes[i]);
        return out;
    }

    // The plate of the timeline (.tl-block) in its fold (.tl-wrap > .tl-fold).
    function blockOf(wrap) {
        var fold = wrap && wrap.firstChild;
        var block = fold && fold.firstChild;
        return block && hasClass(block, 'tl-block') ? block : null;
    }

    function tokenNum(node, name, rest) {
        try {
            var ms = parseFloat(window.getComputedStyle(node).getPropertyValue(name));
            return isFinite(ms) ? ms : rest;
        } catch (err) {
            return rest;
        }
    }

    // The wait of the plate while its line is drawn (capsule: chart): the plate wears .tl-wait (its content hidden by visibility, the height the same, the nodes alive) and a layer of
    // stand-ins of the same layout (.tl-skel, aria-hidden) stands over it. It is put before the folds of the pick open (foldPlay) or in the drawing that owes the line (renderReserve), and
    // ends where the line ends (traceRest, `soft`): the layer fades and the content comes in by opacity (.sk-off, .tl-reveal, --dur-4), with no shift. A quicker pick, any drawing (the poll
    // owes no line, so builds none) and reduced motion leave none: traceStop takes it off at once. Where the keyboard is inside the plate, none is put (the content keeps its focus).
    var waitBlock = null;
    var waitSkel = null;
    var waitFade = null;
    var waitTimer = 0;

    function heldIn(node) {
        try {
            return !!node && !!document.activeElement && node !== document.activeElement && typeof node.contains === 'function' && node.contains(document.activeElement);
        } catch (err) {
            return false;
        }
    }

    function waitOn(block) {
        try {
            if (!block || waitBlock === block || reducedMotion() || heldIn(block)) return;
            waitStop(false);
            var skel = el('div', 'tl-skel');
            skel.setAttribute('aria-hidden', 'true');
            var caps = el('div', 'sk-caps');
            for (var i = 0; i < 4; i++) caps.appendChild(el('span', 'sk-cap'));
            skel.appendChild(caps);
            skel.appendChild(el('div', 'sk-field'));
            skel.appendChild(el('div', 'sk-legend'));
            skel.appendChild(el('div', 'sk-detail'));
            block.appendChild(skel);
            classSet(block, 'tl-wait', true);
            waitBlock = block;
            waitSkel = skel;
            waitArm();
        } catch (err) {
            // A layer that cannot be built is not put: the content stands.
        }
    }

    // The stand-ins (and the line that is owed) do not hang for ever: where the chart has not come in WAIT_MAX_MS (a read in the air, a chart asked for), they are taken off, the content shows,
    // and the plate wears the end state of the line (the outline, the arrow). The count is from the pick (waitBy) for a line that is owed, else from the moment the wait is put.
    var WAIT_MAX_MS = 4000;
    var waitDeadline = 0;
    var waitBy = 0;

    function waitArm() {
        if (waitDeadline) clearTimeout(waitDeadline);
        var by = pickDue && waitBy ? waitBy : Date.now() + WAIT_MAX_MS;
        waitDeadline = setTimeout(waitExpire, Math.max(0, by - Date.now()));
    }

    // The wait and the line that is owed end now: the stand-ins fade off, the outline and the arrow stand. Where the line is being drawn (traceLive) it ends the wait itself in a beat.
    function waitExpire() {
        if (waitDeadline) {
            clearTimeout(waitDeadline);
            waitDeadline = 0;
        }
        if (!pickDue && !waitBlock) return;
        if (traceLive) return;
        pickDue = false;
        traceHold = false;
        waitBy = 0;
        try {
            var plate = root && root.querySelector ? root.querySelector('.wt-plate') : null;
            var wrap = plate ? plateKid(plate, 'tl-top') : null;
            var strip = plate ? plateBars(plate) : null;
            if (plate && wrap && blockOf(wrap) && strip && pickedIn(strip)) {
                traceRest(plate, wrap, true);
                return;
            }
        } catch (err) { /* a layout that cannot be read: the wait is simply taken off */ }
        waitStop(true);
    }

    // The wait ends: at once, or `soft` (the line has met): the layer fades and the content comes in in --dur-4, then the layer is taken off.
    function waitStop(soft) {
        if (waitDeadline) {
            clearTimeout(waitDeadline);
            waitDeadline = 0;
        }
        waitDrop();
        var block = waitBlock;
        var skel = waitSkel;
        waitBlock = null;
        waitSkel = null;
        if (!block) return;
        classSet(block, 'tl-wait', false);
        if (!soft || !skel || reducedMotion()) {
            if (skel && skel.parentNode) skel.parentNode.removeChild(skel);
            return;
        }
        classSet(skel, 'sk-off', true);
        classSet(block, 'tl-reveal', true);
        waitFade = { block: block, skel: skel };
        waitTimer = setTimeout(waitDrop, tokenNum(block, '--dur-4', 180) + 30);
    }

    // What is left of a soft end (the layer going out, the content coming in) is taken off at once.
    function waitDrop() {
        if (waitTimer) {
            clearTimeout(waitTimer);
            waitTimer = 0;
        }
        if (!waitFade) return;
        if (waitFade.skel.parentNode) waitFade.skel.parentNode.removeChild(waitFade.skel);
        classSet(waitFade.block, 'tl-reveal', false);
        waitFade = null;
    }

    // `keep`: the wait stands (the line is drawn over it: traceStart, traceRest, which ends it itself).
    function traceStop(keep) {
        if (traceTimer) {
            clearTimeout(traceTimer);
            traceTimer = 0;
        }
        if (traceLive && traceLive.parentNode) traceLive.parentNode.removeChild(traceLive);
        traceLive = null;
        traceHold = false;
        if (!keep) waitStop(false);
    }

    // The device pixel. Every place and size of the line is worked out in whole device pixels (devSnap), so the live line and the plate's outline lie on the same pixels (DPR 1, 2, ...).
    function devRatio() {
        var d = window.devicePixelRatio;
        return isFinite(d) && d > 0 ? d : 1;
    }

    function devSnap(v, d) {
        return Math.round(v * d) / d;
    }

    // The width of the line (--trace-w) in whole device pixels, written on the root, where the bar's ring, the stem and the outline read it: 1.5px of the token is 2 pixels at DPR 1 and 3 at DPR 2.
    var traceUnit = { d: 0, w: 0 };

    function traceWidth() {
        var d = devRatio();
        if (traceUnit.d === d && traceUnit.w) return traceUnit.w;
        var w = 1.5;
        try {
            var page = document.documentElement;
            varDrop(page, '--trace-w');
            var base = parseFloat(window.getComputedStyle(document.documentElement).getPropertyValue('--trace-w'));
            if (isFinite(base) && base > 0) w = base;
            w = Math.max(1, Math.round(w * d)) / d;
            setVar(page, '--trace-w', w + 'px');
        } catch (err) { /* the token stands as the style sheet has it */ }
        traceUnit = { d: d, w: w };
        return w;
    }

    // Where the line goes, read once, on the device pixel grid (devSnap) and in the layer's own pixels: the layer's origin is the plate's padding box moved to a whole device pixel (`ox`, `oy`: where the layer
    // is put in the plate), so a whole number of device pixels in the layer is one on the screen. `cx` is the middle of the stem on the screen. The stem (`x`, from `y0` to `y1`) is the middle of the picked
    // bar's liquid: it begins inside the bottom edge of the bar (its ring's width in) and ends inside the top line of the chart's plate (that line's width in), so no gap and nothing out of the line.
    // The plate's line: the edges and corners of the plate (`l`, `r`, `t`, `b`: the middle of the line, half its width in; the radii less half of it). Null where the plate has no height or stands above the foot.
    function traceGeo(plate, bar, block) {
        var d = devRatio();
        var w = traceWidth();
        var pr = plate.getBoundingClientRect();
        var ox = pr.left + plate.clientLeft;
        var oy = pr.top + plate.clientTop;
        var sx = devSnap(ox, d);
        var sy = devSnap(oy, d);
        var br = bar.getBoundingClientRect();
        var kr = block.getBoundingClientRect();
        var rest = parseFloat(bar.parentNode.style.getPropertyValue('--bar-rest'));
        rest = isFinite(rest) && rest > 0 ? Math.min(rest, br.width) : br.width;
        var left = devSnap(br.left + rest / 2 - w / 2, d);
        var top = devSnap(kr.top, d);
        var bottom = devSnap(kr.bottom, d);
        var half = w / 2;
        var style = window.getComputedStyle(block);
        function radius(name) { return Math.max(0, (parseFloat(style.getPropertyValue(name)) || 0) - half); }
        var geo = {
            ox: sx - ox, oy: sy - oy, w: w, cx: left + half,
            x: left + half - sx, y0: devSnap(br.bottom, d) - w - sy, y1: top + w - sy,
            l: devSnap(kr.left, d) - sx + half, r: devSnap(kr.right, d) - sx - half, t: top - sy + half, b: bottom - sy - half,
            tl: radius('border-top-left-radius'), tr: radius('border-top-right-radius'),
            br: radius('border-bottom-right-radius'), bl: radius('border-bottom-left-radius')
        };
        return kr.height > 0 && geo.y1 > geo.y0 ? geo : null;
    }

    // The two arms of the line from the foot of the stem (xs, on the top edge) round the plate's edge, as paths, and their length. The meeting point (m, on the foot) is where the two lengths are equal
    // (counted along the sides and the arcs of the corners, not the middle of the foot), so the arms are of one length and, run in one time, at one speed. The left arm goes anticlockwise, the right one is its mirror.
    function traceArms(geo) {
        var q = Math.PI / 2;
        var xs = Math.max(geo.l + geo.tl, Math.min(geo.r - geo.tr, geo.x));
        var h = geo.b - geo.t;
        var a0 = (xs - geo.l - geo.tl) + q * geo.tl + (h - geo.tl - geo.bl) + q * geo.bl - (geo.l + geo.bl);
        var b0 = (geo.r - geo.tr - xs) + q * geo.tr + (h - geo.tr - geo.br) + q * geo.br + (geo.r - geo.br);
        var d = devRatio();
        var m = Math.max(geo.l + geo.bl, Math.min(geo.r - geo.br, devSnap((b0 - a0) / 2, d)));
        var left = 'M' + xs + ' ' + geo.t + 'H' + (geo.l + geo.tl) + 'A' + geo.tl + ' ' + geo.tl + ' 0 0 0 ' + geo.l + ' ' + (geo.t + geo.tl)
            + 'V' + (geo.b - geo.bl) + 'A' + geo.bl + ' ' + geo.bl + ' 0 0 0 ' + (geo.l + geo.bl) + ' ' + geo.b + 'H' + m;
        var right = 'M' + xs + ' ' + geo.t + 'H' + (geo.r - geo.tr) + 'A' + geo.tr + ' ' + geo.tr + ' 0 0 1 ' + geo.r + ' ' + (geo.t + geo.tr)
            + 'V' + (geo.b - geo.br) + 'A' + geo.br + ' ' + geo.br + ' 0 0 1 ' + (geo.r - geo.br) + ' ' + geo.b + 'H' + m;
        return { left: left, right: right, long: a0 + m };
    }

    // The one place the stem is made and taken (.bar-arrow, an svg of its own in the plate: a bar of the line's width, no tip; shown with no fade and taken in --dur-1). With `geo` (traceGeo): every arrow that stands in the
    // plate is taken first, then the one new arrow is made, placed by the bar's liquid, shown where `on`. Without `geo`: every arrow is taken at once, or, with `fade`, put out (--dur-1)
    // and taken at the end of the fade (its transitionend; a short timer where none comes). So a plate never has more than one, and none where nothing is picked.
    var arrowTimer = 0;

    function arrowSet(plate, geo, on, fade) {
        if (arrowTimer) {
            clearTimeout(arrowTimer);
            arrowTimer = 0;
        }
        var old = arrowsOf(plate);
        function take() {
            if (arrowTimer) {
                clearTimeout(arrowTimer);
                arrowTimer = 0;
            }
            old.forEach(function (a) { if (a.parentNode) a.parentNode.removeChild(a); });
        }
        if (!geo && fade && old.length) {
            old.forEach(function (a) {
                classSet(a, 'on', false);
                try {
                    a.addEventListener('transitionend', function (e) {
                        if (e.target === a && e.propertyName === 'opacity') take();
                    });
                } catch (err) { /* the timer takes it */ }
            });
            arrowTimer = setTimeout(take, tokenNum(plate, '--dur-1', 120) + 60);
            return null;
        }
        take();
        if (!geo) return null;
        var long = geo.y1 - geo.y0;
        var arrow = svgEl('svg', { 'class': on ? 'bar-arrow on' : 'bar-arrow', 'aria-hidden': 'true', focusable: 'false', width: geo.w, height: long });
        arrow.appendChild(svgEl('rect', { 'class': 'arrow-stem', x: 0, y: 0, width: geo.w, height: long }));
        arrow.style.left = (geo.ox + geo.x - geo.w / 2).toFixed(3) + 'px';
        arrow.style.top = (geo.oy + geo.y0).toFixed(3) + 'px';
        plate.appendChild(arrow);
        return arrow;
    }

    // The arrow under the picked bar: there while the plate wears its outline, placed by the bar's liquid. Worked out again (arrowSet) at the end of a drawing and a resize (barsFit),
    // when the row scrolls and when a bar of the row has gone its way. Hidden where the bar is scrolled out of its row; taken out where nothing is picked or the chart is not over
    // the plate; kept (shown off) while the layer is drawn or the folds are moving.
    function arrowFit() {
        try {
            var plate = root && root.querySelector ? root.querySelector('.wt-plate') : null;
            if (!plate) return;
            var strip = plateBars(plate);
            var bar = strip ? pickedIn(strip) : null;
            var block = blockOf(plateKid(plate, 'tl-top'));
            var geo = bar && block && !traceHold && !traceLive ? traceGeo(plate, bar, block) : null;
            if (!geo) {
                if (traceHold || traceLive) arrowsOf(plate).forEach(function (a) { classSet(a, 'on', false); });
                else arrowSet(plate, null, false, false);
                return;
            }
            var room = strip.getBoundingClientRect();
            arrowSet(plate, geo, geo.cx >= room.left && geo.cx <= room.right, false);
        } catch (err) {
            // A layout that cannot be read leaves the arrow as it stands.
        }
    }

    // The end state of the line: the plate wears its outline, the arrow stands (the layer, if any, is taken off in this turn). `soft`: the line has met (it was drawn, or it could not be), so the
    // wait of the plate ends in the same turn by its fade (waitStop); else (a drawing, a pick on a pick) it ends at once.
    function traceRest(plate, wrap, soft) {
        traceStop(true);
        waitStop(!!soft);
        var block = blockOf(wrap);
        if (block) classSet(block, 'tl-traced', true);
        arrowFit();
    }

    // The line of a pick that is let go, or of the pick that goes (a switch): the outline and the arrow are put out (they fade in --dur-1), and the arrow is taken at the end of the fade.
    function traceFade(plate, wrap) {
        var block = blockOf(wrap);
        if (block) classSet(block, 'tl-traced', false);
        arrowSet(plate, null, false, true);
    }

    // The pick is let go and the chart is back under the plate: nothing of the line stands, no arrow either (all of them are taken).
    function traceClear(plate, wrap) {
        traceStop();
        var block = blockOf(wrap);
        if (block) classSet(block, 'tl-traced', false);
        arrowSet(plate, null, false, false);
    }

    // The layer of the line, from the places worked out (traceGeo): the stem from the foot of the bar down to the plate's top line, then the two arms from the foot of the stem round the plate, meeting opposite it
    // (traceArms). `total` is the time of the whole line, shared out by length on this svg's own style, so the stem and the arms run at one speed (linear), and the arms in one time.
    function traceLayer(plate, geo, total, cls) {
        var svg = svgEl('svg', { 'class': cls, 'aria-hidden': 'true', focusable: 'false' });
        svg.style.left = geo.ox.toFixed(3) + 'px';
        svg.style.top = geo.oy.toFixed(3) + 'px';
        var line = function (name, d) {
            return svgEl('path', { 'class': name, d: d, pathLength: 100, 'stroke-dasharray': '100 100' });
        };
        var arms = traceArms(geo);
        var stemLong = geo.y1 - geo.y0;
        var stemMs = total * stemLong / (stemLong + arms.long);
        setVar(svg, '--dur-trace-stem', stemMs.toFixed(2) + 'ms');
        setVar(svg, '--dur-trace-arms', (total - stemMs).toFixed(2) + 'ms');
        svg.appendChild(line('trace-stem', 'M' + geo.x + ' ' + geo.y0 + 'V' + geo.y1));
        svg.appendChild(line('trace-arm', arms.left));
        svg.appendChild(line('trace-arm', arms.right));
        return svg;
    }

    // The line of a pick, drawn: the reader's (foldPlay, at the end of the folds) or the page's own (pickTraceRun, `byPage`: it holds no poll, motionOn). Where it cannot be drawn
    // (reduced motion, a layout that cannot be read), the end state at once.
    function traceStart(plate, wrap, byPage) {
        traceStop(true);
        traceByPage = !!byPage;
        var geo = null;
        try {
            var block = blockOf(wrap);
            var bar = pickedIn(plateBars(plate));
            geo = !reducedMotion() && block && bar && typeof document.createElementNS === 'function' ? traceGeo(plate, bar, block) : null;
            if (geo) {
                // The wait stands before the line (foldPlay, renderReserve); where nothing put it, it is put now (waitOn does nothing where it stands).
                waitOn(block);
                var total = tokenNum(plate, '--dur-trace-stem', 160) + tokenNum(plate, '--dur-trace-arms', 320);
                var svg = traceLayer(plate, geo, total, 'plate-trace');
                plate.appendChild(svg);
                traceLive = svg;
                // The layer ends with the arms and a beat more; then the end state, in the same turn.
                traceTimer = setTimeout(function () {
                    traceTimer = 0;
                    if (traceLive === svg) traceRest(plate, wrap, true);
                    landPoke();
                }, total + 30);
                return;
            }
        } catch (err) {
            // A layer that cannot be built is not drawn: the end state.
        }
        traceRest(plate, wrap, true);
    }

    // The number and the notes of the plate stand in the card of the account picked, where the card stands in the plate (right under the chart: plate > .ins-wrap > .ins-fold > .inspector;
    // not on the plate of the billing-cycle budget). The real nodes move, none is copied (their handlers, their data-focus and dimOnItem go with them): the fact, the stamp, the chart word,
    // the note of the accounts at the limit, the notes of the window (the accounts not read, the manual resets, the states) and the reset row stand in the card's summary (.kt-sum, first in
    // .kt-pad), in the order of the markup; the strip wears a copy of the number ("5.53 of 16", aria-hidden) and, in the row of windows, a copy of the note's head. The scope stays in the plate,
    // and so does the spoken summary (sr-only), so a reader hears the number with the card at rest. Where the card does not stand in the plate (Chart folded, a pick from the list, the budget plate) nothing moves. Where the card goes (a bar let go, another bar,
    // a drawing), the nodes go back to the plate, to the place of the markup of limitRow.
    var CARD_CLASS = { fact: 'wt-fact', stamp: 'wt-stamp', btn: 'l-chart', limit: 'wt-limit', foot: 'wt-foot', credits: 'family-credits', legend: 'lg', nx: 'nx' };
    // The place of each class a child of the plate wears, in the order of the markup (cardRank reads it): the bars and the folds first, the scope and the spoken summary (sr-only)
    // among the rest. A class not named here is one of the other notes, at the rank of the foot; the foot and the credits are read last, so a node that wears one of them and a class above keeps the class above.
    var CARD_RANK = { 'bars': 0, 'tl-wrap': 0, 'ins-wrap': 0, 'wt-fact': 1, 'wt-scope': 2, 'wt-stamp': 3, 'l-chart': 4, 'sr-only': 5, 'wt-limit': 6, 'lg': 8, 'nx': 9, 'wt-foot': 7, 'family-credits': 7 };

    function cardChild(parent, name) {
        for (var i = 0; parent && i < parent.childNodes.length; i++) {
            var n = parent.childNodes[i];
            if (n.nodeType === 1 && hasClass(n, name)) return n;
        }
        return null;
    }

    // The card that stands in the plate, or null.
    function cardInPlate(plate) {
        var w = cardChild(plate, 'ins-wrap');
        var f = w && cardChild(w, 'ins-fold');
        return f ? cardChild(f, 'inspector') : null;
    }

    // The place of a child of the plate in the order of the markup: the bars and the folds, the fact, the model list, the stamp, the chart word, the spoken summary, the note, the other notes
    // (the foot, the credits), the legend, the reset row, and what the plate is given later (the line of the pick) last.
    function cardRank(n) {
        if (n.nodeType !== 1) return 0;
        if (String(n.tagName).toLowerCase() === 'svg') return 99;
        var names = Object.keys(CARD_RANK);
        for (var i = 0; i < names.length; i++) if (hasClass(n, names[i])) return CARD_RANK[names[i]];
        return CARD_RANK[CARD_CLASS.foot];
    }

    function cardBack(plate, node, kind) {
        for (var i = 0; i < plate.childNodes.length; i++) {
            if (cardRank(plate.childNodes[i]) > CARD_RANK[CARD_CLASS[kind]]) {
                plate.insertBefore(node, plate.childNodes[i]);
                return;
            }
        }
        plate.appendChild(node);
    }

    // The height of the place the chart holds in the plate, in px (0: none stands there, or it cannot be measured).
    function cardRoomNow(card) {
        try {
            var plate = card && typeof card.closest === 'function' ? card.closest('.wt-plate') : null;
            var wrap = plate ? cardChild(plate, 'tl-wrap') : null;
            // Not rounded down: the body must fill the place to the pixel (rounded up to a hundredth).
            return wrap && typeof wrap.getBoundingClientRect === 'function' ? Math.ceil(wrap.getBoundingClientRect().height * 100) / 100 : 0;
        } catch (err) {
            return 0;
        }
    }

    // The card is up: pressed (the pin), under the pointer or with the focus in it. It stands in the place of the chart then (.kt-open, which the CSS reads).
    function cardUp() {
        return cardOpen || cardHover || cardFocus;
    }

    // The pin, the pointer and the focus all let go (another account, another window or family): the card is at rest.
    function cardReset() {
        clearTimeout(cardLeave.hover);
        clearTimeout(cardLeave.focus);
        cardOpen = false;
        cardHover = false;
        cardFocus = false;
    }

    // The body of an up card is as tall as the place of the chart less the strip the chart folds into and the gap under it (--kt-room, which the CSS reads): longer, it scrolls.
    function cardRoomSet(card) {
        try {
            // Let go, the card keeps the measure while its body folds away, so the body holds its height to the end of the fold.
            if (cardUp() && cardRoom > 0) setVar(card, '--kt-room', 'max(0px, calc(' + cardRoom + 'px - var(--kt-strip) - var(--kt-gap)))');
            else if (cardUp()) varDrop(card, '--kt-room');
        } catch (err) {
            // A card that cannot say its room has no limit.
        }
    }

    // The card goes up or down by its state (cardUp), `was` being what it was. Going up measures the place of the chart now, before the chart folds shut by the card's own state (the chart capsule);
    // while the chart's row is still moving (a card let go a moment ago) the measure it has stays. A drawing that builds the card up carries the measure on (cardFold).
    function cardShow(card, was) {
        var up = cardUp();
        var plate = null;
        var wrap = null;
        try {
            plate = typeof card.closest === 'function' ? card.closest('.wt-plate') : null;
            wrap = plate ? cardChild(plate, 'tl-wrap') : null;
        } catch (err) {
            // Nothing moves where the chart cannot be found.
        }
        if (up && !was) {
            var room = wrap && hasClass(wrap, 'tl-yield') && cardRoom > 0 ? cardRoom : cardRoomNow(card);
            if (room > 0 || !wrap) cardRoom = room;
        }
        // The line of the pick stands through the card (its outline folds with the chart into the strip, the stem stays under the bar); a line that is being drawn when the card comes is brought to its end state at once.
        if (up && !was && plate && wrap && traceLive) traceRest(plate, wrap, true);
        classSet(card, 'kt-open', up);
        cardRoomSet(card);
        // The chart's row moves by the card's state: its fold clips while it does (.tl-yield), and no longer.
        if (wrap && up !== was) {
            classSet(wrap, 'tl-yield', true);
            clearTimeout(cardYield);
            cardYield = setTimeout(function () { classSet(wrap, 'tl-yield', false); }, tokenNum(wrap, '--dur-5', 300) + 60);
        }
        var tg = typeof card.querySelector === 'function' ? card.querySelector('.kt-toggle') : null;
        if (tg) tg.setAttribute('aria-expanded', up ? 'true' : 'false');
    }

    // The card is pressed or let go (the strip's button, the chart word of its summary). `keep`: the strip's own button lets go the pin only, a pointer or a focus still on the card holds it up (it goes down
    // as they go); the chart word and another bar let it go for good.
    function cardPress(card, open, keep) {
        var was = cardUp();
        cardOpen = open;
        if (!open && !keep) {
            clearTimeout(cardLeave.hover);
            clearTimeout(cardLeave.focus);
            cardHover = false;
            cardFocus = false;
        }
        cardShow(card, was);
    }

    // The keyboard leaves the strip of the folded chart (it hides as the chart opens): the picked bar has it, and where the row has none picked, the plate itself (made focusable for the moment).
    function chartStripLeave(plate) {
        var bar = typeof plate.querySelector === 'function' ? plate.querySelector('.bars .bar.sel') : null;
        if (bar && !bar.disabled) {
            bar.focus();
            return;
        }
        plate.setAttribute('tabindex', '-1');
        plate.addEventListener('blur', function () { plate.removeAttribute('tabindex'); }, { once: true });
        plate.focus();
    }

    // The strip the chart folds into while the account's card stands in its place (a button over the wrap's top, shown by the CSS once the card is up): the pointer on it, the keyboard focus in it or a press
    // gives the chart back at once and lets the card's pin go, so the card goes down into its own strip and the chart opens again with its outline (cardPress). A finger and a narrow screen press only.
    function chartStrip(name, wrap) {
        var btn = el('button', 'tl-strip', t('chart.strip', { name: name }));
        btn.setAttribute('type', 'button');
        btn.setAttribute('aria-label', t('chart.strip_label', { name: name }));
        btn.setAttribute('data-focus', 'chart-strip');
        function back() {
            try {
                var plate = typeof btn.closest === 'function' ? btn.closest('.wt-plate') : null;
                var card = plate ? cardInPlate(plate) : null;
                if (card && cardUp()) {
                    // The strip hides as the card goes down: a keyboard that is on it goes to the picked bar first (else the plate), so it is never left on the page.
                    var held = document.activeElement === btn;
                    cardPress(card, false);
                    if (held) chartStripLeave(plate);
                }
            } catch (err) {
                // A strip that cannot find its card gives nothing back.
            }
        }
        btn.addEventListener('click', function (e) {
            e.stopPropagation();
            back();
        });
        btn.addEventListener('pointerenter', function (e) {
            if (e.pointerType !== 'touch' && cardHoverOk()) back();
        });
        btn.addEventListener('focusin', function (e) {
            var seen = false;
            try {
                seen = !!(e.target && typeof e.target.matches === 'function' && e.target.matches(':focus-visible'));
            } catch (err) {
                seen = false;
            }
            if (seen) back();
        });
        // The keyboard that comes on the chart itself while the card stands over it (in the beat before the strip shows, the fold still takes the focus, and it hides under the focus
        // as it goes) gives the chart back as the strip does, so the focus stays on the chart and is not lost with the fold.
        if (wrap && typeof wrap.addEventListener === 'function') {
            wrap.addEventListener('focusin', function (e) {
                var seen = false;
                try {
                    seen = e.target !== btn && !!(e.target && typeof e.target.matches === 'function' && e.target.matches(':focus-visible'));
                } catch (err) {
                    seen = false;
                }
                if (seen) back();
            });
        }
        return btn;
    }

    // A pointer that hovers and a screen wide enough for it (a finger and a narrow screen, up to HOVER_CARD_NARROW_MAX, press only).
    function cardHoverOk() {
        return mediaIs('(hover: hover) and (not (max-width: ' + HOVER_CARD_NARROW_MAX + 'px))');
    }

    // The pointer (kind 'hover') or the keyboard focus (kind 'focus') comes on the card or goes off it. On: the card is up at once; off: a beat of --dur-2 is kept, and a return within it cancels the going.
    // Only the card in the plate goes up (outside it the CSS shows the card whole).
    function cardHold(card, kind, on) {
        try {
            if (typeof card.closest !== 'function' || !card.closest('.wt-plate')) return;
            clearTimeout(cardLeave[kind]);
            if (on) {
                if (kind === 'hover' && !cardHoverOk()) return;
                var was = cardUp();
                if (kind === 'hover') cardHover = true;
                else cardFocus = true;
                cardShow(card, was);
                return;
            }
            cardLeave[kind] = setTimeout(function () {
                // The card may have been drawn again meanwhile (the poll): the live one is the one to go down.
                var live = cardLast && cardLast.isConnected ? cardLast : card;
                var wasUp = cardUp();
                if (kind === 'hover') cardHover = false;
                else cardFocus = false;
                cardShow(live, wasUp);
            }, tokenNum(card, '--dur-2', 120));
        } catch (err) {
            // A card that cannot be told of the pointer is pressed only.
        }
    }

    // The card goes (`fold` is its .ins-wrap): what it carries goes back to the plate. Called before the card is taken out, so the nodes never stand nowhere.
    function cardGive(plate, fold) {
        var sum = fold && typeof fold.querySelector === 'function' ? fold.querySelector('.kt-sum') : null;
        if (!sum || !plate) return;
        Object.keys(CARD_CLASS).forEach(function (kind) {
            var node = sum.querySelector('.' + CARD_CLASS[kind]);
            if (node && !cardChild(plate, CARD_CLASS[kind])) {
                classSet(node, 'wt-carded', false);
                cardBack(plate, node, kind);
            }
        });
    }

    // The card stands in the plate: it takes the nodes, once (a card that has its summary is left as it is).
    function cardTake(plate) {
        try {
            if (!plate || hasClass(plate, 'wt-plate-cycle')) return;
            var card = cardInPlate(plate);
            if (!card || card.querySelector('.kt-sum')) return;
            var top = cardChild(card, 'kt-top');
            var pad = card.querySelector('.kt-pad');
            var right = top && top.querySelector('.kt-right');
            if (!top || !pad || !right) return;
            var fact = cardChild(plate, 'wt-fact');
            var stamp = cardChild(plate, 'wt-stamp');
            var btn = cardChild(plate, 'l-chart');
            var limit = cardChild(plate, 'wt-limit');
            var foot = cardChild(plate, 'wt-foot');
            var credits = cardChild(plate, 'family-credits');
            var legend = cardChild(plate, 'lg');
            var nx = cardChild(plate, 'nx');
            // The strip: the share and the number in one cell, where "Clear" takes the place of both.
            var pair = el('span', 'kt-pair');
            var mini = el('span', 'kt-mini');
            mini.setAttribute('aria-hidden', 'true');
            var num = fact && fact.querySelector('.wt-num');
            if (num) mini.appendChild(num.cloneNode(true));
            var count = fact ? parseInt(fact.getAttribute('data-of'), 10) : 0;
            if (count > 0) mini.appendChild(el('span', 'kt-of', t('acc.strip_of', { n: count })));
            var lead = right.querySelector('.kt-lead');
            right.insertBefore(pair, right.firstChild);
            if (lead) pair.appendChild(lead);
            pair.appendChild(mini);
            // The summary: the fact, and on its right the stamp and the chart word, then the note, the notes of the window and the reset row, then a rule.
            // The mark of what stands in the card (cardGive takes it off): the capsule of the window tabs styles the stamp there by it, with no class of the card in its selectors.
            [fact, stamp, btn, limit, foot, credits, legend, nx].forEach(function (n) { if (n) classSet(n, 'wt-carded', true); });
            var sum = el('div', 'kt-sum');
            var row = el('div', 'kt-row');
            var side = el('div', 'kt-side');
            if (fact) row.appendChild(fact);
            if (stamp) side.appendChild(stamp);
            if (btn) side.appendChild(btn);
            if (side.firstChild) row.appendChild(side);
            sum.appendChild(row);
            [limit, foot, credits, legend, nx].forEach(function (n) { if (n) sum.appendChild(n); });
            pad.insertBefore(el('div', 'kt-rule'), pad.firstChild);
            pad.insertBefore(sum, pad.firstChild);
        } catch (err) {
            // A card that cannot take them leaves them where they are.
        }
    }

    // The timeline and the card of a pick on the plate. `wrap` is the timeline's fold (none where the chart is not shown), `olds` the folds of the cards that stand, `fresh` the new
    // card's node (none for a pick let go). The timeline goes between under the plate and under the bars (.tl-top, and .wt-up on the plate for the area `chart`); the bars are
    // already changed (they swap at once), so: what is to go shuts where it stands (--dur-5), then it is moved and what is new opens. A timeline under the bars that another
    // bar is picked under shuts and opens again. With reduced motion, at once. A pick on a pick: the timers of the first are cancelled, the second goes on from the height it finds.
    function foldPlay(plate, strip, wrap, olds, fresh, picked, prevKey, key) {
        foldCancel(true);
        var token = foldToken;
        var table = plate.parentNode;
        var up = !!wrap && wrap.parentNode === plate;
        var goes = !!wrap && up !== picked;
        var swaps = !!wrap && !goes && picked && prevKey !== key;
        var calm = reducedMotion();
        // The line of the pick (traceStart): the timeline moves, so the arrow stays off until the folds have gone their way; what stands of the line (outline, arrow) fades first (--dur-1),
        // and then the folds begin. At the end of the folds: the line is drawn for a pick, and what is left of it is cleared for a pick let go.
        var moves = !!wrap && (goes || swaps);
        var lit = moves && !calm && (arrowsOf(plate).some(function (a) { return hasClass(a, 'on'); }) || (!!blockOf(wrap) && hasClass(blockOf(wrap), 'tl-traced')));
        if (moves) traceHold = true;
        function settle() {
            if (!moves) return;
            if (picked) traceStart(plate, wrap);
            else traceClear(plate, wrap);
        }
        // The card that stands is pressed (the chart is shut under it) and another account is picked: the one motion of a card let go, so the document holds its height. The new card
        // takes the old card's place in its fold, pressed with the old measure, and is let go at once: its body folds as the chart opens (the chart's row and the card's body move by
        // the same --dur-5, the body as tall as the place of the chart), where a shut and an open fold one after the other would drop the page by the card and lift it back.
        var hand = swaps && !!fresh && olds.length === 1 && !hasClass(olds[0], 'fold-shut') && !!olds[0].querySelector('.inspector.kt-open');
        var shutting = [];
        if (!calm && !hand) {
            if ((goes || swaps) && !hasClass(wrap, 'fold-shut')) shutting.push(wrap);
            olds.forEach(function (box) { if (!hasClass(box, 'fold-shut')) shutting.push(box); });
        }
        // What shuts is held up by the box it stands in until what opens has opened (a pick on a pick goes on from the hold the first one made).
        if (shutting.length) foldHold(table);
        function letGo() {
            foldLet(table);
        }
        function handOver() {
            var oldBox = olds[0];
            var oldCard = oldBox.querySelector('.inspector');
            var foldBox = oldCard ? oldCard.parentNode : null;
            if (!foldBox || !hasClass(foldBox, 'ins-fold') || !fresh) return false;
            cardGive(plate, oldBox);
            classSet(fresh, 'kt-open', true);
            var room = oldCard.style.getPropertyValue('--kt-room');
            if (room) setVar(fresh, '--kt-room', room);
            foldBox.replaceChild(fresh, oldCard);
            cardTake(plate);
            void plate.offsetHeight;
            // The line of the pick comes to the chart with the stand-ins, as it does for a pick on a pick (traceRest).
            if (!calm) waitOn(blockOf(wrap));
            cardPress(fresh, false);
            // The line is drawn by settle (a pick on a pick).
            if (calm) {
                settle();
                letGo();
            } else {
                foldWait(wrap, token, function () {
                    settle();
                    letGo();
                });
            }
            return true;
        }
        function open() {
            if (hand && handOver()) return;
            var opening = [];
            // What the card that goes carries goes back to the plate first; the card that comes takes it from there (cardTake).
            olds.forEach(function (box) {
                cardGive(plate, box);
                if (box.parentNode) box.parentNode.removeChild(box);
            });
            if (wrap && goes) {
                if (picked) {
                    classSet(wrap, 'tl-top', true);
                    classSet(plate, 'wt-up', true);
                    plate.insertBefore(wrap, strip.nextSibling);
                } else {
                    classSet(wrap, 'tl-top', false);
                    classSet(plate, 'wt-up', false);
                    table.insertBefore(wrap, plate.nextSibling);
                }
            }
            if (wrap && (goes || swaps)) opening.push(wrap);
            // The wait of the plate is put before its fold opens: the stand-ins are there as the plate comes, and the content comes in when the line has met (traceRest).
            if (wrap && moves && picked && !calm) waitOn(blockOf(wrap));
            if (fresh) {
                var box = foldOf('ins', fresh, !calm);
                if (wrap && wrap.parentNode === plate) plate.insertBefore(box, wrap.nextSibling);
                else table.insertBefore(box, plate.nextSibling);
                cardTake(plate);
                opening.push(box);
            }
            if (calm) {
                if (wrap) {
                    classSet(wrap, 'fold-run', false);
                    classSet(wrap, 'fold-shut', false);
                }
                settle();
                letGo();
                return;
            }
            if (!opening.length) {
                letGo();
                return;
            }
            opening.forEach(function (node) {
                classSet(node, 'fold-run', true);
                classSet(node, 'fold-shut', true);
            });
            void plate.offsetHeight;
            opening.forEach(function (node) { classSet(node, 'fold-shut', false); });
            foldWait(opening[0], token, function () {
                opening.forEach(function (node) { classSet(node, 'fold-run', false); });
                settle();
                letGo();
            });
        }
        function begin() {
            if (shutting.length) {
                shutting.forEach(function (node) {
                    classSet(node, 'fold-run', true);
                    classSet(node, 'fold-shut', true);
                });
                foldWait(shutting[0], token, open);
            } else {
                open();
            }
        }
        if (lit) {
            traceFade(plate, wrap);
            foldTimer = setTimeout(function () {
                foldTimer = 0;
                if (token === foldToken) begin();
            }, tokenNum(plate, '--dur-1', 120));
        } else {
            begin();
        }
        // A motion that never began (nothing to fold) leaves a held reading of the poll no reason to wait.
        landPoke();
    }

    // The pick of an account on the plate of the window on screen (a bar, a step of one account), or the pick let go, with no drawing of the page: the bars wear .sel and their
    // words, the step and the account's row wear their pressed, and the account's card stands (or is taken out) right under the plate, the same card renderInspector draws and in the
    // place a drawing gives it, so the 30-second drawing builds the same screen. Returns false where the screen is not the one this reads (no plate, the pick from a row of the list, a card
    // under a row, a bar not in the window, another family, a failed drawing, a layout that cannot be walked): the caller draws the page anew, as it always did. Nothing here moves.
    function selectInPlace(limit) {
        var key = selectedAccountKey;
        var i;
        try {
            if (stopped || !drawnFrom || !root || typeof root.querySelector !== 'function' || !i18n.en) return false;
            var group = drawnFrom.group;
            if (!group || group.harness_id !== selectedHarness) return false;
            var plate = root.querySelector('.wt-plate');
            if (!plate || !plate.parentNode || hasClass(plate, 'wt-plate-cycle')) return false;
            var strip = null;
            for (i = 0; i < plate.childNodes.length; i++) if (hasClass(plate.childNodes[i], 'bars')) strip = plate.childNodes[i];
            if (!strip) return false;
            var account = null;
            if (key) {
                // The pick came from a bar of this window's row (or its step): else the card stands elsewhere and the page is drawn anew.
                if (!limit || limit !== strip.getAttribute('data-strip')) return false;
                (group.accounts || []).forEach(function (a) { if (a && a.key === key) account = a; });
                var found = false;
                for (i = 0; i < strip.childNodes.length; i++) if (strip.childNodes[i].getAttribute('data-account') === key) found = true;
                if (!account || !found) return false;
            }
            // The cards that stand now are the ones under this plate, each in its fold (two while a last pick is still going out), or none (a card under a row of the list is drawn anew).
            var olds = [];
            var cards = root.querySelectorAll('section.inspector');
            for (i = 0; i < cards.length; i++) {
                var cardBox = cards[i].parentNode && cards[i].parentNode.parentNode;
                if (!cardBox || !hasClass(cardBox, 'ins-wrap') || (cardBox.parentNode !== plate.parentNode && cardBox.parentNode !== plate)) return false;
                olds.push(cardBox);
            }
            var wrap = root.querySelector('.tl-wrap');
            var picked0 = pickedIn(strip);
            var prevKey = picked0 ? String(picked0.getAttribute('data-account') || '') : '';
            // The card of the account picked is made first: a card that cannot be drawn leaves the page to be drawn anew, before anything moves.
            var fresh = null;
            if (account) {
                var holder = el('div', null);
                renderInspector(holder, group, account, drawnFrom.ctx, drawnFrom.facets);
                fresh = holder.firstChild;
            }
            // What syncSelection does at a drawing: the step of one account stands only while its account is the one picked.
            if (stepOn && stepOn.account && stepOn.account !== key) stepOn = null;

            var bars = root.querySelectorAll('.bar');
            for (i = 0; i < bars.length; i++) {
                var bar = bars[i];
                var owner = bar.getAttribute('data-account');
                if (!owner) continue;
                var picked = !!key && owner === key;
                if (hasClass(bar, 'sel') !== picked) classSet(bar, 'sel', picked);
                bar.setAttribute('aria-pressed', picked ? 'true' : 'false');
                bar.setAttribute('aria-label', String(bar.getAttribute('data-said') || '') + barPickWords(picked));
                // The bar a step of one account holds is that account's bar alone (a step of several was let go by selectAccount, which called this).
                if (!stepOn || stepOn.account) {
                    var lit = !!stepOn && owner === stepOn.account && !!bar.parentNode && bar.parentNode.getAttribute('data-strip') === stepOn.limit;
                    if (hasClass(bar, 'bar-on') !== lit) classSet(bar, 'bar-on', lit);
                    // (and the light of a step of several accounts, let go by this pick, goes out)
                    if (bar.removeAttribute) bar.removeAttribute('data-step-lit');
                }
            }
            if ((!stepOn || stepOn.account) && strip.removeAttribute) strip.removeAttribute('data-step');
            var steps = root.querySelectorAll('.rail-step');
            for (i = 0; i < steps.length; i++) {
                if (steps[i].getAttribute('aria-pressed') !== null) {
                    steps[i].setAttribute('aria-pressed', stepOn && steps[i].getAttribute('data-focus') === stepOn.key ? 'true' : 'false');
                }
            }
            var rows = root.querySelectorAll('.acc-row');
            for (i = 0; i < rows.length; i++) {
                var on = !!key && rows[i].getAttribute('data-focus') === 'acct:' + key;
                if (hasClass(rows[i], 'active') !== on) classSet(rows[i], 'active', on);
                rows[i].setAttribute('aria-pressed', on ? 'true' : 'false');
                rows[i].setAttribute('aria-label', String(rows[i].getAttribute('data-said') || '') + rowPickWords(on));
            }
            // The timeline goes to its place (under the bars, or under the plate) and the card comes and goes, by the fold: shut where it stood, open where it stands.
            foldPlay(plate, strip, wrap, olds, fresh, !!account, prevKey, key);
            // The place the bars were brought to, and the edges of the rows, are worked out again (the picked bar is brought into its row).
            barsFit();
            return true;
        } catch (err) {
            // A page that cannot be walked, or a card that cannot be drawn: the page is drawn anew by the caller, which reads the same state.
            return false;
        }
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
                        : (heldFlags(b).map(flagWord).join(', ') || 'restricted'));
                }) });
        });
        // At the limit when last read: dated and muted, not exhaustion now.
        if (kinds.was.length) {
            items.push({ k: 'was', n: kinds.was.length, word: t('legend.was_at_limit'),
                lines: kinds.was.map(function (b) {
                    return who(b) + ' — at the limit when last read (' + (relTime(b.observed_at) || 'at an unreported time') + ')';
                }) });
        }
        // Read earlier, not at the limit then and not held back: not current, so not in the figure, and what it had is said here.
        // The plate lights the bars of these accounts when the engine named every one of them.
        if (kinds.last.length) {
            var had = kinds.last.reduce(function (sum, b) { return sum + Math.max(0, Math.min(1, b.left)); }, 0);
            items.push({ k: 'last', n: kinds.last.length, word: t('legend.last'), extra: num(had, 2),
                accounts: kinds.last.every(function (b) { return !!b.account; }) ? kinds.last.map(function (b) { return b.account; }) : null,
                lines: kinds.last.map(function (b) {
                    return who(b) + ' — last known, read ' + (relTime(b.observed_at) || 'at an unreported time');
                }),
                note: 'Read earlier and dated on its bar: not current, so not in the figure, never as 0.' });
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
            return { cells: cells, title: 'The soonest reported reset of a current account'
                + (back ? ': a full refill then gives back the ' + back + ' account-windows '
                    + (n === 1 ? 'it uses' : 'they use') + ' now, if nothing more is used before it' : '') + '.' };
        }
        if (m.accounts && g.reset_unknown === m.accounts) return { cells: [{ pre: t('reset.none') }], title: '' };
        return null;
    }

    function cellText(cell) {
        return (cell.pre || '') + (cell.b || '') + (cell.post || '');
    }

    // The accounts that reset at the soonest moment the row says ("next reset … · 3 accounts · +2.70 if unused"): of the bars, the
    // current ones whose reset time can be read and falls in the engine's own group of the soonest: from the soonest on, to
    // RESET_CLUSTER_SEC later (quota_summary.RESET_TOLERANCE_SEC: the engine counts the accounts, and what they give back, over that
    // whole span, not over one instant). The bars carry their reset to the second, cut down, and the engine's moment carries the
    // fraction of one, so a second before it is let in too.
    // The row points at them only when they are the ones it counts: how many they are, and what a full refill gives back, 1 − left
    // added over them, must be what the engine said (to a hundredth). Anything else, and the row says its words and points at no bar.
    // Null then, and also when the row names no amount or no time.
    var RESET_CLUSTER_SEC = 120;
    function nextReturn(g) {
        var next = g.next_reset;
        if (!next || !next.at || !(g.next_reset_returns >= 0.005)) return null;
        var soonest = Date.parse(next.at);
        if (!isFinite(soonest)) return null;
        var hits = barsOf(g).filter(function (b) {
            if (b.state !== 'current' || !b.account || typeof b.left !== 'number') return false;
            var at = Date.parse(b.resets_at || '');
            return isFinite(at) && at - soonest >= -1000 && at - soonest <= RESET_CLUSTER_SEC * 1000;
        });
        if (!hits.length || (typeof next.accounts === 'number' && next.accounts !== hits.length)) return null;
        var back = hits.reduce(function (sum, b) { return sum + (1 - Math.max(0, Math.min(1, b.left))); }, 0);
        if (Math.abs(back - g.next_reset_returns) > 0.01) return null;
        return { accounts: hits.map(function (b) { return b.account; }) };
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
            out.push(accountName(index, spent[0].account) + ' at the limit'
                + (spent[0].resets_at ? ' until ' + whenWords(spent[0].resets_at) : ''));
        } else if (spent.length) {
            out.push(spent.length + ' at the limit');
        }
        var held = kinds.held;
        if (held.length) {
            var flags = {};
            held.forEach(function (b) { heldFlags(b).forEach(function (f) { flags[f] = true; }); });
            var keys = Object.keys(flags);
            var word = keys.length === 1 ? flagWord(keys[0]) : 'restricted';
            out.push((held.length === 1 && named(held[0]) ? accountName(index, held[0].account) : held.length) + ' ' + word);
        }
        var wasSpent = kinds.was;
        if (wasSpent.length) {
            out.push((wasSpent.length === 1 && named(wasSpent[0]) ? accountName(index, wasSpent[0].account)
                : wasSpent.length) + ' at the limit when last read'
                + (wasSpent.length === 1 ? ' (' + (relTime(wasSpent[0].observed_at) || 'at an unreported time') + ')' : ''));
        }
        if (kinds.unknown.length) out.push(kinds.unknown.length + ' unknown — not counted');
        var next = limitReset(g);
        if (next) out.push(next.cells.map(cellText).join(' · '));
        return out;
    }

    // "Lowest left" names a ranking, and its words say so. They stand in the hint of the plate's figure and in its spoken note: the plate
    // draws no badge for it (the figure is the plate's, and each fact stands once).
    function tightWords(g) {
        return 'Lowest average share left among this family’s measured limits: a ranking, not a verdict'
            + (scopeWords(g) ? '. It binds only its own ' + scopeWords(g) + ', not the family’s other models' : '')
            + '.';
    }

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

    // The fact of the plate, with no bubble: the window's name ("Weekly") and, with it, the figure (`figure`: l-fig, the number and its unit),
    // which stands in the fact's own grid; the plate keeps both for its motion.
    function factBubble(caption, figure) {
        var bubble = el('div', 'wt-fact');
        bubble.appendChild(el('div', 'wt-fact-cap', caption));
        bubble.appendChild(figure);
        return bubble;
    }

    // The accounts at the limit now, in words under the number row: "At the limit · 3", the first of them by name (to
    // FOOT_NAMES, then "N more", which opens the account list, as the note's does), and in how long the soonest of them is back ("back in 9h";
    // none, no tail). Only when there are such accounts; it takes the keyboard, and pointed at or focused it lights their bars (dimOnItem).
    // Null when none is at the limit now. `spent` are the bars of limitKinds.
    function limitBubble(spent, index, key, plate) {
        if (!spent.length) return null;
        var node = el('div', 'wt-limit');
        var named = spent.filter(function (b) { return !!b.account; });
        var shown = named.slice(0, FOOT_NAMES);
        var said = shown.map(function (b) { return accountName(index, b.account); });
        var nameless = spent.length - named.length;
        // As the note's: the nameless are counted among the ones not named, but only when some are named (else the bubble has no names to follow).
        var more = named.length - shown.length + (shown.length ? nameless : 0);
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
            return accountName(index, b.account) + ' — at the limit' + (b.resets_at ? ' until ' + whenWords(b.resets_at) : '');
        }));
        // The plate dims to these accounts' bars when the engine named every one of them; else to the bars at the limit.
        var own = named.length === spent.length ? named.map(function (b) { return b.account; }) : null;
        if (own) node.setAttribute('data-accounts', JSON.stringify(own));
        dimOnItem(node, plate, 'limit', function () { return plate; }, own);
        if (more > 0) {
            var rest = el('button', 'cover-link wt-foot-more', t('foot.more', { n: more }));
            rest.setAttribute('type', 'button');
            rest.setAttribute('data-focus', 'atlimit-more:' + key);
            rest.setAttribute('aria-label', t('foot.more_label', { n: more }));
            rest.addEventListener('click', function (e) {
                e.stopPropagation();
                accountsSet(true);
                rerender();
                restoreFocus('accounts');
            });
            node.appendChild(document.createTextNode(' '));
            node.appendChild(rest);
        }
        if (back) {
            node.appendChild(document.createTextNode(' · '));
            node.appendChild(el('span', 'wt-limit-back', back));
        }
        return node;
    }

    // One row per limit: its bars, its fact, the words of what is not in the line and the next reported reset under them — and one control,
    // in the same slot of every row, that shows its chart below (on the charted row, hides it).
    // The row is the plate of the window row (windowPlate), the only one drawn, and the charted limit's: it stands under its window's tab, which
    // already says the name and the average left. It has no ground and no box of its own. In the order of its markup (the order of the keyboard): the bars over
    // the whole width; the fact (factBubble: the figure and its unit, with the window's name beside them), the model list (the scope row, windowPlate's, when
    // the length has several limits), the stamp (how many accounts are read, and how old) and the chart word, in one row; then the notes, each on a line of its own: the
    // accounts at the limit now (limitBubble), the accounts not read (windowFoot's note) and the family's manual resets (extras), the states and the reset.
    // Its control is the kit's text action, the word "Chart", pressed while the chart is shown; the whole spoken summary is a note the word is described by.
    // `caption` is the window's name for the fact's caption; `extras` are the parts windowPlate hands to it: scope, foot, credits.
    function limitRow(g, name, chartKey, index, markTightest, stamp, caption, extras) {
        var inChart = timelineOpen && g.key === chartKey;
        var row = el('div', 'lrow' + (inChart ? ' sel' : '') + ' wt-plate');
        var fig = rowFigure(g);
        var m = g.measured || {};
        row.appendChild(barStrip(g, name, index));

        // The word is its own name (no aria-label, no title: kit: text action); aria-pressed says whether the chart is shown.
        var chartBtn = wordButton('plate.chart');
        chartBtn.className += ' l-chart';
        chartBtn.setAttribute('data-focus', 'limit:' + g.key);
        chartBtn.setAttribute('aria-pressed', inChart ? 'true' : 'false');
        chartBtn.setAttribute('aria-describedby', 'quotas-win-say');
        chartBtn.addEventListener('click', function (e) {
            e.stopPropagation();
            // In a pressed card the word lets the card go and shows the chart (the chart folds shut under the pressed card by its state alone).
            var held = typeof chartBtn.closest === 'function' ? chartBtn.closest('.kt-open') : null;
            if (held) {
                // The word hides as the card lets go: a keyboard that is on it goes to the picked bar first (else the plate), so it is never left on the page.
                var plateHeld = null;
                try {
                    if (document.activeElement === chartBtn && typeof chartBtn.closest === 'function') plateHeld = chartBtn.closest('.wt-plate');
                } catch (err) {
                    plateHeld = null;
                }
                var chartWas = timelineOpen;
                timelineOpen = true;
                chartAsked = '';
                chartCursor = null;
                cardPress(held, false);
                if (!chartWas) rerender();
                if (plateHeld) {
                    // A drawing makes the plate anew and puts the keyboard back by its key; where the keyboard is still on the page or on the node that went, it goes by the rule above.
                    var kept = document.activeElement;
                    if (plateHeld.isConnected === false) plateHeld = root.querySelector('.wt-plate');
                    if (plateHeld && (focusLost(kept) || kept === chartBtn)) chartStripLeave(plateHeld);
                }
                return;
            }
            // The plate is the charted limit's: its word folds the timeline or unfolds it. Another window is picked in the window row
            // (pickWindow), never here.
            timelineOpen = !timelineOpen;
            chartAsked = '';
            chartCursor = null;
            rerender();
        });
        // What the plate says in full, for a reader who hears it: a reader tabbing through the limits hears each one, then what the
        // word does.
        var spoken = [name];
        if (scopeWords(g)) spoken.push(scopeWords(g));
        spoken.push(fig.any ? fig.text + ' of ' + fig.of + ' account-windows left now' : 'no current reading');
        if (fig.lastKnown) {
            spoken.push('last known ' + num(fig.lastKnown.windows, 2) + ' account-windows, read '
                + (relTime(fig.lastKnown.oldest_observed_at) || 'at an unreported time') + ', not counted');
        }
        spoken.push(t('win.say_current', { n: fig.of, current: m.accounts || 0 }));
        limitSpoken(g, index).forEach(function (w) { spoken.push(w); });
        restrictionWords(g).forEach(function (w) { spoken.push(w); });
        if (restrictionWords(g).length && num(g.unrestricted_windows, 2)) {
            spoken.push(num(g.unrestricted_windows, 2) + ' unrestricted');
        }
        coverageWords(g).forEach(function (w) { spoken.push(w); });
        if (markTightest) spoken.push('lowest average share left in this family, a ranking, not a verdict');
        var say = el('span', 'sr-only', spoken.join(' — '));
        say.setAttribute('id', 'quotas-win-say');

        // The figure says its unit by itself: account-windows of the accounts the limit applies to — "10.35 of 19 accounts".
        // With no current reading it is a dash in the quiet ink, never a zero (the hint says so).
        var figure = el('div', 'l-fig');
        figure.appendChild(figureNode(fig.text, !fig.any));
        figure.appendChild(el('span', 'meta-num of', ' ' + t('plate.of', { n: fig.of })));
        figure.title = (fig.any ? '' : 'No current reading — not zero. ')
            + 'Account-windows left now: the share each account read now has left of this limit, added up (a '
            + 'full account counts 1), of the ' + plural(fig.of, 'account') + ' it applies to. Last-known and '
            + 'unknown accounts are not counted, never as 0.'
            + (markTightest ? ' ' + tightWords(g) : '');
        var factNode = factBubble(caption || name, figure);
        // The count the figure is of, for the copy of the number in the account card's strip ("5.53 of 16", cardTake): the caption is never parsed.
        if (fig.of > 0) factNode.setAttribute('data-of', String(fig.of));
        row.appendChild(factNode);
        // The model list (windowPlate's scope row) stands after the fact, in the area beside it.
        if (extras && extras.scope) row.appendChild(extras.scope);
        // The stamp of the window (windowStamp) stands in the line of the model list, on its right, before the chart word.
        if (stamp) {
            stamp.className += ' wt-stamp';
            row.appendChild(stamp);
        }
        row.appendChild(chartBtn);
        row.appendChild(say);
        var atLimit = limitBubble(limitKinds(g).spent, index, g.key, row);
        if (atLimit) row.appendChild(atLimit);
        // Under the number row, the notes: who is not read (the note, its hidden copy where this window has none) and the family's manual resets.
        if (extras && extras.foot) row.appendChild(extras.foot);
        if (extras && extras.credits) row.appendChild(extras.credits);

        // Then the states, then the reset — each only when it has something to say. The note says the names and the age of the readings that are
        // dated (last known), so where the note stands that item is not said again. "At the limit when last read" stays: it is another meaning,
        // and it explains the dashed base of a bar, which the note does not.
        var states = limitLegend(g, index, name);
        if (extras && extras.foot) states = states.filter(function (item) { return item.k !== 'last'; });
        var legend = legendNode(states, g.key);
        if (legend) row.appendChild(legend);
        var next = limitReset(g);
        if (next) {
            var reset = resetNode(next.cells, next.title);
            var back = nextReturn(g);
            if (back) {
                // The row is the handle of the accounts that reset then: pointed at, focused or touched, it lights their bars. It keeps the
                // keyboard through a redraw by its focus key, and says the names aloud.
                reset.setAttribute('tabindex', '0');
                reset.setAttribute('role', 'group');
                reset.setAttribute('aria-label', next.cells.map(cellText).join(' · ') + ' — '
                    + back.accounts.map(function (key) { return accountName(index, key); }).join(', '));
                reset.setAttribute('data-k', 'reset');
                reset.setAttribute('data-focus', 'nx:' + g.key);
                reset.setAttribute('data-accounts', JSON.stringify(back.accounts));
                dimOnItem(reset, row, 'reset', function () { return row; }, back.accounts);
            }
            // The ladder under the rings says the same on the wide shell: while it is drawn the row gives way to it (the mark; on the narrow shell the row stays).
            if (ladderDrawn) {
                reset.className += ' nx-in-ladder';
                ladderLine = reset;
            }
            row.appendChild(reset);
        }
        return row;
    }

    // A hint of one line for each account, to eight and then how many more; `note`,
    // when there is one, says what the kind of account means, after them.
    function hintOf(lines, note) {
        var shown = lines.slice(0, 8);
        if (lines.length > 8) shown.push('+' + (lines.length - 8) + ' more');
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
    var FOOT_NAMES = 3;

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
                if (hasClass(child, 'wt-plate')) found = child;
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
            if (i) foot.appendChild(document.createTextNode(' · '));
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
            span.appendChild(document.createTextNode(words + ' · '));
            var named = group.bars.filter(function (b) { return !!b.account; });
            var shown = named.slice(0, FOOT_NAMES);
            var said = shown.map(function (b) { return accountName(index, b.account); });
            // The comma between names stands inside the name before it (a name is a box, and a line may break before a box, so a comma
            // outside would open a line): "name," then a space, and the last name has none.
            said.forEach(function (name, j) {
                if (j) span.appendChild(document.createTextNode(' '));
                span.appendChild(el('span', 'wt-who', j < said.length - 1 ? name + ',' : name));
            });
            // Bars the engine did not name (an older answer) are counted, not named.
            var nameless = group.bars.length - named.length;
            if (!shown.length) {
                said = [plural(nameless, 'account')];
                span.appendChild(el('span', 'wt-who', said[0]));
            }
            if (!hold) {
                span.setAttribute('tabindex', '0');
                span.setAttribute('role', 'group');
                span.setAttribute('aria-label', words + ' — ' + said.join(', '));
                span.setAttribute('data-focus', 'foot:' + group.kind);
                // The plate dims to the group's own accounts when the engine named every one of them; else to the kind of bar they are.
                var own = nameless ? null : group.bars.map(function (b) { return b.account; });
                if (own) span.setAttribute('data-accounts', JSON.stringify(own));
                dimOnItem(span, foot, group.k, function () { return platePanel(foot); }, own);
            }
            foot.appendChild(span);
            var more = named.length - shown.length + (shown.length ? nameless : 0);
            if (more > 0) {
                var rest = el('button', 'cover-link wt-foot-more', t('foot.more', { n: more }));
                rest.setAttribute('type', 'button');
                if (!hold) {
                    rest.setAttribute('data-focus', 'foot-more:' + group.kind);
                    rest.setAttribute('aria-label', t('foot.more_label', { n: more }));
                    rest.addEventListener('click', function (e) {
                        e.stopPropagation();
                        accountsSet(true);
                        rerender();
                        restoreFocus('accounts');
                    });
                }
                foot.appendChild(rest);
            }
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
        var text = credits.observed_at ? 'observed ' + whenWords(credits.observed_at) : 'observation time unavailable';
        if (credits.source) text += ' · source ' + credits.source;
        if (credits.label) text += ' · reported “' + credits.label + '”';
        return text + '. The count does not say which limit a manual reset restores.';
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
                return text + (cur ? ' ' + cur : '') + ' minor units ('
                    + (item.decimal_places === null || item.decimal_places === undefined ? 'scale not reported'
                        : 'reported scale ' + item.decimal_places + ', not formatted') + ')';
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
                        text = label + ': ' + (item.available_count === null ? t('res.count_unknown') : t('res.available', { n: item.available_count }))
                            + ' (' + (item.usable_now === false ? t('res.usable_no') : item.usable_now === true
                                ? t('res.usable_yes') : t('res.usable_unknown')) + ')';
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
    // The longest part of the motion: the numbers' fade, and the bars' steps and growth together.
    var MOTION_MS = 220;
    // The whole movement of a family's pick ends within this many ms (kit: rail, motion): the tabs' slide (--dur-flip), the thread, the
    // windows one after another, the ladder of resets after them (its head, up to four steps and the word "N more", numbered on from the windows), and the note and the
    // note after the last row (the tokens of the motion group): 380 + 8 x 70 + 280 = 1220 ms with three windows, four steps and the word (the word's row is the note's). The stamp is not in the rail.
    var RAIL_MOTION_MS = 1240;
    // The slide of the head's tabs runs this long (--dur-flip, kit: motion): a drawing inside the span of the movement puts the slide on its tabs
    // (markSlide) only while it is still running.
    var FLIP_MS = 450;
    // The keyframes of the movement (kit: motion): the animations of a drawing that clockSet gives the start time of the pick.
    var RAIL_KEYFRAMES = ['rail-slide', 'rail-yield', 'rail-draw', 'rail-drop', 'rail-fade'];
    // The sweep of the rings ends within this many ms of the pick (kit: ring, motion): the last row of a family comes down (--rail-row-at and
    // --rail-row-step apart, up to five rows) and its ring sweeps for --dur-sweep: 380 + 4 x 70 + 650 = 1310 ms. A drawing inside the span keeps the sweep.
    var SWEEP_MS = 1400;
    var SWEEP_KEYFRAMES = ['ring-sweep-arc', 'ring-sweep-dot'];

    // The page's own clock (document.timeline) now, in ms; null where the page has none (or it is not running).
    function timelineNow() {
        try {
            var now = document.timeline ? document.timeline.currentTime : null;
            return typeof now === 'number' && isFinite(now) ? now : null;
        } catch (err) {
            return null;
        }
    }

    // Once per drawing of a movement, after it is made: every animation of the movement (by its keyframes' name, so no transition is
    // touched) takes the time of the pick as its start time. The animations the drawing replaced were on that clock, so those that
    // stand in their place are at the same moment of their course: no step, no restart, and nothing is run by a frame of ours. The rail, the sweep
    // of the rings and the ladder each call it with their keyframes and the time of their own pick.
    function clockSet(keyframes, start) {
        try {
            var running = document.getAnimations();
            for (var i = 0; i < running.length; i++) {
                if (keyframes.indexOf(running[i].animationName) >= 0) running[i].startTime = start;
            }
        } catch (err) { /* the animations go on by their own clock */ }
    }

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
            out.state = !kept && bal.current ? 'unlimited' : 'unknown';
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
            return { account: c.key, state: 'unlimited', left: null,
                     words: c.label + ': unlimited included usage — no share to show' };
        }
        if (c.state === 'unknown') return { account: c.key, state: 'unknown', left: null, why: 'not_read' };
        var ends = c.ends_at ? whenWords(c.ends_at) : '';
        var words = c.label + ': ' + exactPct(c.share * 100, c.share <= 0) + '% left'
            + (c.state === 'last_known' ? ' when last read' : '')
            + ' — ' + moneyShort(c.left, c.scale) + ' of ' + moneyShort(c.limit, c.scale)
            + (ends ? ' · cycle ends ' + ends : '')
            + (c.state === 'last_known'
                ? ' · last known, read ' + (relTime(c.observed_at) || 'at an unreported time') + ', not current'
                : (c.observed_at ? ' · read ' + relTime(c.observed_at) : ''));
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
            if (!scale && c.scale && (c.state === 'current' || c.state === 'unlimited')) scale = c.scale;
        });
        reads.forEach(function (c) {
            if (c.state === 'current' && !sameScale(c.scale, scale)) {
                c.state = 'unknown';
                c.share = null;
            }
        });
        var by = function (state) { return reads.filter(function (c) { return c.state === state; }); };
        var current = by('current');
        var unlimited = by('unlimited');
        var last = by('last_known');
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
                lines: unlimited.map(function (c) { return c.label + ' — unlimited included usage'; }),
                note: 'Unlimited included usage has no share: it is not in the figure, the average or the bars.' });
        }
        if (last.length) {
            legend.push({ k: 'last', n: last.length, word: t('cycle.last_not_counted'),
                lines: last.map(function (c) {
                    return c.label + ' — last known, read ' + (relTime(c.observed_at) || 'at an unreported time');
                }),
                note: 'Read earlier and dated on its bar: not current, so not in the figure or the average, never as 0.' });
        }
        var reset = [{ pre: t('cycle.billing_cycle'),
                       title: 'Included usage renews when an account’s own billing cycle ends — a cycle’s allowance, not a window of time.' }];
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
            var endsHint = 'The soonest billing-cycle end among the accounts read now; each account’s own is on its bar and its card.';
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
                         title: 'Personal on-demand spending this cycle, added over ' + plural(payers, 'account')
                             + '. It is spent beyond the included usage, not part of it; a team pool is not counted.' };
        }

        // What the status line of the section reads, as it does from a window's bars.
        var times = current.concat(unlimited).map(function (c) { return c.observed_at; }).filter(Boolean).sort();
        var lastTimes = last.map(function (c) { return c.observed_at; }).filter(Boolean).sort();
        var key = 'cycle:' + hid;
        var status = {
            bars: reads.map(function (c) { return { account: c.key, state: c.state === 'unlimited' ? 'current' : c.state }; }),
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
        return !!c && (c.state === 'current' || c.state === 'unlimited');
    }

    // The plate of the budget, in the plate of a window: the figure, the accounts'
    // bars, the words under them. It has no chart word: there is no history of money
    // to draw. The mean share left is the tab's alone, as a window's is.
    function cyclePlate(cycle, place, index, stamp, foot, credits) {
        var plate = el('div', 'lrow wt-plate wt-plate-cycle');
        plate.setAttribute('id', 'quotas-win-panel');
        plate.setAttribute('role', 'tabpanel');
        plate.setAttribute('aria-labelledby', 'quotas-win-' + place.seconds);
        var drawn = cycle.bars.filter(function (b) { return b.state !== 'unlimited'; });
        if (drawn.length) plate.appendChild(barStrip({ key: cycle.key, bars: drawn }, cycle.name + ' billing cycle', index));
        var onlyUnlimited = !cycle.counted && cycle.unlimited.length > 0 && !cycle.lastKnown && !cycle.unknown;
        var figure = el('div', 'l-fig');
        figure.appendChild(figureNode(
            cycle.counted ? moneyShort(cycle.left, cycle.scale) : (onlyUnlimited ? t('cycle.unlimited') : '—'),
            !(cycle.counted || onlyUnlimited)));
        figure.appendChild(el('span', 'meta-num of', ' ' + (cycle.counted ? t('cycle.left_of', { limit: moneyShort(cycle.limit, cycle.scale) })
            : (onlyUnlimited ? t('cycle.included_usage') : t('common.no_current_reading')))));
        figure.title = (cycle.counted || onlyUnlimited ? '' : 'No current reading — not zero. ')
            + 'Included usage left now: the money each account read now has left of its billing cycle, added up, of what '
            + 'their cycles allow (' + plural(cycle.counted, 'account') + '). It is a cycle’s allowance, not a window of '
            + 'time, and it does not say whether a request can run. Last-known, unknown and unlimited accounts are not '
            + 'counted, never as 0. The mean of each counted account’s share left (what it has left ÷ what its cycle allows) is on the tab.';
        plate.appendChild(factBubble(t('cycle.period'), figure));
        if (stamp) {
            stamp.className += ' wt-stamp';
            plate.appendChild(stamp);
        }
        // What is spent beyond the plan stands in the bubble of the accounts at the limit (the pale crimson), not as a red cell of the reset row.
        if (cycle.onDemand) {
            var over = el('div', 'wt-limit');
            over.appendChild(headNode('b', 'wt-limit-head', t('plate.over_plan', { amount: cycle.onDemand.amount })));
            over.title = cycle.onDemand.title;
            plate.appendChild(over);
        }
        if (foot) plate.appendChild(foot);
        if (credits) plate.appendChild(credits);
        // The budget's legend is whole: it says "not counted" (money), which the note's words do not.
        var legend = legendNode(cycle.legend, cycle.key);
        if (legend) plate.appendChild(legend);
        plate.appendChild(resetNode(cycle.reset));
        plateMotion(plate);
        return plate;
    }

    // Where a window's timeline stands, the budget says there is none.
    function cycleNote() {
        var box = el('div', 'tl-block');
        var note = el('div', 'meta-line reserve-note', t('cycle.no_timeline'));
        note.title = 'Money is read as it stands now. No history of it is kept, so there is nothing to draw.';
        box.appendChild(note);
        return box;
    }

    // The reader's own pick of a window or a scope: the state the plate's
    // chart control keeps, so there is one, not two. It is answered with motion,
    // and it takes the graph back to this window from the combined view (that
    // choice is the graph's own: graphMode, which the window row never holds).
    // A pick of a tab of the row says which place it left (`from`, the tab's key), whether it was showing its preview at the click (`lit`) and
    // whether the pointer held it pressed (`pressed`, markPressed), for the kit's start state of the ring and the numbers; a pick of a scope
    // (the same tab) says none of it, and moves no number.
    function pickWindow(g, from, lit, pressed, sweep) {
        // The pick of another limit moves the ladder of resets (it stands on the window picked); a press on the one that stands moves nothing.
        if (chartKeys[g.harness] !== g.key) {
            ladderPending = true;
            // The ladder of another window starts folded, at its start, with no step pressed.
            ladderFold();
            stepClear();
        }
        if (chartKeys[g.harness] !== g.key) scheduleAll = false;
        chartKeys[g.harness] = g.key;
        graphMode = 'window';
        chartAsked = '';
        chartCursor = null;
        markFrom = '';
        markLit = false;
        winFrom = from || '';
        winLit = !!lit;
        winPressed = !!pressed;
        // The ring of the window picked draws its arc anew (kit: ring, sweep): a pick of a tab other than the one that stands, not of a scope.
        sweepPending = sweep || '';
        motionPending = true;
        rerender();
    }

    // Whether a click on a window or scope that is already picked still means
    // something: with the graph on the combined view it takes the graph back.
    function pickable(on) {
        return !on || graphMode === 'joint';
    }

    // The ladder of resets under the rings (kit: rail; the comment that opens that part of the section is its contract). It stands on the steps of the window
    // picked, `g`, soonest first: of the bars, the current ones that name their account, have a number left and a reset still to come (a second back is let in:
    // the clock of the reading and ours differ by that); a step is the soonest of those not taken yet and all that reset within RESET_CLUSTER_SEC after it (the
    // engine's own group, as nextReturn takes it), with the accounts of it and what they give back, 1 - left added over them (the plate's "if unused"): the first
    // step is the row of the next reset on the plate, in time and in amount. Then the NEIGHBOURING steps that say the same in how long (untilWords, taken from this very `now`
    // as the filter is) are glued into one: the earliest moment (at, iso), the accounts of all (each once), the sum of what they give back, and the latest moment (last, lastIso; count
    // is how many steps went into it); the first step may then be wider than the plate's row of the next reset (the same moment, the amount and the accounts no fewer). Null where the
    // family has no window of bars to stand on (the billing-cycle budget of Cursor); {steps: [], now} where the window has bars and no reset to come.
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
    // The movement of the ladder for the reader's pick of a window ends within this many ms of the pick (kit: rail, motion): --ladder-at, the head, four steps and
    // the word that counts what did not fit, a row after another (--rail-row-step), the last of them coming down for --dur-rail-row: 100 + 5 x 70 + 280 = 730 ms.
    var LADDER_MS = 800;
    var LADDER_KEYFRAMES = ['ladder-drop', 'ladder-leave'];

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

    // The amount of a step ("+2.70") with the decimal point in the crimson ink (a span of its own); the text of the node stays the amount.
    function ladderAmount(amount) {
        var node = el('span', 'rail-step-amount');
        var at = amount.indexOf('.');
        if (at < 0) {
            node.appendChild(document.createTextNode(amount));
            return node;
        }
        node.appendChild(document.createTextNode(amount.slice(0, at)));
        node.appendChild(el('span', 'rail-step-dot', '.'));
        node.appendChild(document.createTextNode(amount.slice(at + 1)));
        return node;
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

    // The widths the script mirrors from the stylesheet's @media, the only places it writes them: the shell is the narrow one up to NARROW_SHELL_MAX (the ladder is not drawn there), and the
    // card of an account opens by the hover of a pointer on any screen that is not the narrow one up to HOVER_CARD_NARROW_MAX (a finger and a narrow screen press only). That one is asked as the
    // stylesheet's own max-width, negated, so that no width falls between the two.
    var NARROW_SHELL_MAX = 859.98;
    var HOVER_CARD_NARROW_MAX = 520.98;

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

    // The ladder's markup (kit: rail): the zone (the room the rail leaves under the rings), the ladder in it with its head, the window of its steps (every step of the
    // picked window, each a tab stop whose pointer, keyboard and touch light the bars of its accounts on the plate, as the row of the next reset does) and the word under
    // the window ("N more", or "less" while it is open). `base` is the number of the first row for the movement of a family's pick (--i): the rings' count, so the ladder
    // comes down after them. It keeps what the fit needs in ladderZone (ladderFit reads the layout once, at the end of the drawing) and returns the zone, or null when the
    // window has no ladder. Until the fit has run (a bare document has no layout) the first LADDER_STEPS stand and the word counts the rest. The word opens the ladder in
    // its own place (ladderToggle): the steps stand in a window that scrolls (ladderOpen, ladderTop outlive the drawing, for the limit they were pressed on: ladderKey);
    // nothing else on the page is touched.
    function ladderNode(g, index, base) {
        // The open ladder is the pressed limit's: the limit drawn is another (a row hidden, an answer that names no limit), so it stands folded.
        if (ladderOpen && ladderKey !== g.key) ladderFold();
        var plan = ladderOf(g);
        if (!plan) return null;
        var zone = el('div', 'rail-ladder-zone');
        var ladder = el('div', 'rail-ladder');
        ladder.setAttribute('role', 'group');
        ladder.setAttribute('aria-label', t('ladder.title'));
        var head = el('div', 'rail-ladder-title', t('ladder.title'));
        head.setAttribute('aria-hidden', 'true');
        setVar(head, '--i', String(base));
        ladder.appendChild(head);
        var scroll = el('div', 'rail-scroll rail-scroll-start rail-scroll-end');
        var steps = [];
        var none = false;
        var total = plan.steps.length;
        var extra = Math.max(0, total - LADDER_STEPS);
        // Open across a drawing; the fit says again whether the window comes out.
        var open = ladderOpen && total > 0;
        if (!total) {
            none = true;
            var empty = el('div', 'rail-step rail-step-first');
            empty.title = t('reset.none');
            empty.appendChild(el('span', 'rail-step-none', t('ladder.none')));
            setVar(empty, '--i', String(base + 1));
            scroll.appendChild(empty);
            steps.push(empty);
        }
        plan.steps.forEach(function (step, k) {
            var node = el('div', 'rail-step focus-ring' + (k ? '' : ' rail-step-first') + (k >= LADDER_STEPS && !open ? ' rail-fit-off' : ''));
            var amount = '+' + step.back.toFixed(2);
            // Several moments glued into one: the first and the last of them ("Sun 1 Nov 12:31 – Sun 1 Nov 12:54"); one that reads the same at both ends stands as one.
            var when = whenWords(step.iso);
            var till = step.count > 1 ? whenWords(step.lastIso) : '';
            if (till && till !== when) when += ' \u2013 ' + till;
            var words = t('ladder.step', { when: when, accounts: plural(step.accounts.length, 'account'),
                names: step.accounts.map(function (key) { return accountName(index, key); }).join(', '), amount: amount });
            node.setAttribute('tabindex', '0');
            node.setAttribute('aria-label', words);
            node.title = words;
            node.setAttribute('data-k', 'reset');
            // The key follows the step's earliest moment, not its place in the list: the gluing of neighbours changes with the clock, the places shift, the moment stays.
            node.setAttribute('data-focus', stepKey(g, step));
            node.setAttribute('data-accounts', JSON.stringify(step.accounts));
            // A step with accounts is a button that lights the bars of its accounts on the plate, or picks the one account (Enter and Space as a button's; the second press, or another step, lets go of them).
            if (step.accounts.length) {
                node.setAttribute('role', 'button');
                node.setAttribute('aria-pressed', stepOn && stepOn.key === stepKey(g, step) ? 'true' : 'false');
                node.addEventListener('click', function (e) {
                    e.stopPropagation();
                    stepPress(g, step, node);
                });
                node.addEventListener('keydown', function (e) {
                    if (!e || e.repeat || (e.key !== 'Enter' && e.key !== ' ' && e.key !== 'Spacebar')) return;
                    // (Space would scroll the page; Enter would reach the keys the page itself keeps)
                    e.preventDefault();
                    e.stopPropagation();
                    stepPress(g, step, node);
                });
            } else {
                node.setAttribute('role', 'group');
            }
            // The steps past the ones that stand at first come with the last of them (the delay has a ceiling).
            setVar(node, '--i', String(base + 1 + Math.min(k, LADDER_STEPS)));
            var bead = el('span', 'rail-bead');
            bead.setAttribute('aria-hidden', 'true');
            node.appendChild(bead);
            node.appendChild(el('span', 'rail-step-time', untilWords(step.at, plan.now)));
            node.appendChild(ladderAmount(amount));
            // The plate is found when the pointer or the keyboard comes (the ladder is drawn before it). The list of the step is the window it stands in.
            dimOnItem(node, scroll, 'reset', function () { return platePanel(node); }, step.accounts);
            scroll.appendChild(node);
            steps.push(node);
        });
        ladder.appendChild(scroll);
        // The word under the window: "N more" for the resets that did not fit (there for the resets over LADDER_STEPS at once, the fit sets the rest), "less" while the
        // ladder is open. A button that opens and folds the ladder in its own place; the keyboard stays on it (the same node).
        var more = el('button', 'rail-more' + (extra ? '' : ' rail-more-void'), open ? t('ladder.less') : t('foot.more', { n: extra || 1 }));
        more.setAttribute('type', 'button');
        more.setAttribute('data-focus', 'rs-more');
        more.setAttribute('aria-expanded', open ? 'true' : 'false');
        // It comes down last, after the last step that stands (the fit puts the number again, if some steps did not fit).
        setVar(more, '--i', String(base + 1 + (none ? 1 : Math.min(total, LADDER_STEPS))));
        if (extra) more.setAttribute('aria-label', open ? t('ladder.less_label') : t('ladder.more_label', { n: extra }));
        var state = { box: zone, head: head, scroll: scroll, steps: steps, more: more, total: total, key: g.key, none: none, base: base, rows: 1 + Math.min(steps.length, LADDER_STEPS),
            n: Math.min(total, LADDER_STEPS), max: 0 };
        more.addEventListener('click', function (e) {
            e.stopPropagation();
            ladderToggle(state);
        });
        // The edges of the window (the haze) follow the scroll: a class changes only where an edge is reached or left, and nothing runs by frames.
        scroll.addEventListener('scroll', function () {
            var top = scroll.scrollTop;
            if (typeof top !== 'number' || !ladderOpen) return;
            ladderTop = top;
            ladderEdges(state, top);
        }, { passive: true });
        ladder.appendChild(more);
        zone.appendChild(ladder);
        ladderLast = ladder;
        // The reader's pick of a window: the steps that stood fade at once, over the new ones (the old ladder's node, kept by render()).
        if (ladderOut) {
            ladderOut.className = 'rail-ladder rail-ladder-out';
            ladderOut.setAttribute('aria-hidden', 'true');
            ladderOut.setAttribute('inert', '');
            if (ladderShift) setVar(ladderOut, '--ladder-elapsed', ladderShift + 'ms');
            zone.appendChild(ladderOut);
            ladderDrop(ladderOut, Math.max(0, LADDER_MS - ladderShift));
            ladderOut = null;
        }
        if (ladderMotion && !railMotion) {
            ladder.className += ' rail-ladder-moving';
            if (ladderShift) setVar(ladder, '--ladder-elapsed', ladderShift + 'ms');
        }
        ladderZone = state;
        return zone;
    }

    // The old ladder that fades over the new one leaves the page when its fade is over, or at the end of the span of the movement if the page runs no animation: its steps are out of the keyboard
    // and the reader meanwhile (inert, aria-hidden), but a browser with no inert would stop at them. A drawing inside the span puts the node again and sets the end for the same moment.
    function ladderDrop(node, ms) {
        var drop = function () {
            if (ladderKept === node) ladderKept = null;
            if (node.parentNode) node.parentNode.removeChild(node);
        };
        if (typeof node.addEventListener === 'function' && !node.getAttribute('data-leaving')) {
            node.setAttribute('data-leaving', '1');
            node.addEventListener('animationend', function (e) {
                if (e && ((e.animationName && e.animationName !== 'ladder-leave') || (e.target && e.target !== node))) return;
                drop();
            });
        }
        setTimeout(drop, ms);
    }

    // Whether the shell is the narrow one (under 860px): the ladder is not drawn there and there is nothing to fit.
    function narrowShell() {
        return mediaIs('(max-width: ' + NARROW_SHELL_MAX + 'px)');
    }

    // The haze of the window's edges: none over the start while the window stands at its start, none under the end while it stands at its end (classes the
    // stylesheet reads; a class is put on or taken off only where the edge changes).
    function ladderEdges(z, top) {
        var start = top <= 1;
        var end = top >= z.max - 1;
        if (hasClass(z.scroll, 'rail-scroll-start') !== start) classSet(z.scroll, 'rail-scroll-start', start);
        if (hasClass(z.scroll, 'rail-scroll-end') !== end) classSet(z.scroll, 'rail-scroll-end', end);
    }

    // The data-focus key of a step: it follows the step's earliest moment, not its place in the list (the gluing of neighbours changes with the clock, the moment stays).
    function stepKey(g, step) {
        return 'rs:' + g.key + '@' + step.iso;
    }

    // The accounts of the step that is pressed, for the limit `g`, from the steps the ladder stands on now: none where no step is pressed, the press was on another limit, the step has
    // gone (its reset has come, or it was glued to an earlier one) or the shell is narrow (there is no ladder to press again). It reads and changes nothing: a step that a fresh answer no longer has is let go
    // for good where the answer arrives (stepRelease), so it does not come back with a step of the same moment; an answer that failed says nothing of it, and the press stays.
    function stepAccounts(g) {
        if (!stepOn || stepOn.limit !== g.key || narrowShell()) return [];
        var plan = ladderOf(g);
        var found = [];
        if (plan) plan.steps.forEach(function (step) { if (stepKey(g, step) === stepOn.key) found = step.accounts; });
        return found;
    }

    // The let-go that stepAccounts only reads for, made where the answer arrives (land): the press whose step a fresh, whole answer no longer has is dropped. An answer that failed, a kept one and a
    // limit the answer does not hold say nothing of it.
    function stepRelease(view) {
        if (!stepOn || narrowShell() || !view || view.transport_error || view.kept || view.ok === false) return;
        var summary = view.reserve && view.reserve.summary;
        var g = groupByKey((summary && summary.groups) || [], stepOn.limit);
        if (g && !g.cycle && !stepAccounts(g).length) stepOn = null;
    }

    // Let go of the step pressed (a pick of a window or of a family, the shell narrowed): the drawing that follows builds the bars shut.
    function stepClear() {
        stepOn = null;
        try {
            var lit = rootAll('.bar-on');
            for (var i = 0; i < lit.length; i++) classSet(lit[i], 'bar-on', false);
            var lights = rootAll('[data-step],[data-step-lit]');
            for (var k = 0; k < lights.length; k++) {
                lights[k].removeAttribute('data-step');
                lights[k].removeAttribute('data-step-lit');
            }
            var steps = rootAll('.rail-step[aria-pressed=true]');
            for (var j = 0; j < steps.length; j++) steps[j].setAttribute('aria-pressed', 'false');
        } catch (err) {
            // A page that cannot be walked is drawn anew by the next drawing, which reads stepOn.
        }
    }

    // The press of a step of several accounts, in its place, with no drawing of the page (the chart and the Details are not touched, and no bar opens: the light is the opacity of the others): the step
    // pressed is let go by the second press, and the press on another step hands the light over. The steps wear aria-pressed, the plate's bars of the accounts named wear data-step-lit and their row data-step.
    function stepShow(node, accounts) {
        var steps = node.parentNode ? node.parentNode.childNodes : [];
        for (var i = 0; i < steps.length; i++) {
            if (steps[i].getAttribute && steps[i].getAttribute('aria-pressed') !== null) {
                steps[i].setAttribute('aria-pressed', stepOn && steps[i].getAttribute('data-focus') === stepOn.key ? 'true' : 'false');
            }
        }
        var plate = platePanel(node);
        if (!plate || !plate.childNodes) return;
        var strip = plateBars(plate);
        if (!strip) return;
        for (var k = 0; k < strip.childNodes.length; k++) {
            var bar = strip.childNodes[k];
            var on = !!bar.getAttribute && accounts.indexOf(bar.getAttribute('data-account')) >= 0;
            // (the .bar-on of a step of one account, pressed before, goes with the hand-over; the pick stays)
            if (hasClass(bar, 'bar-on')) classSet(bar, 'bar-on', false);
            if (on) bar.setAttribute('data-step-lit', '1');
            else if (bar.removeAttribute) bar.removeAttribute('data-step-lit');
        }
        if (accounts.length) strip.setAttribute('data-step', '1');
        else strip.removeAttribute('data-step');
    }

    // A step of one account is the press on that account's bar: the same selectAccount (the bar picked is open and red, as a press on the bar makes it), the step held in stepOn with the account
    // (syncSelection lets it go as soon as the selection is another, or none). The press on it while its bar is picked lets the bar go. A step of several accounts only lights their bars and picks
    // none; the next pick of an account (a bar, a row of the list, Clear, Escape: selectAccount) lets the step go.
    function stepPress(g, step, node) {
        var key = node.getAttribute('data-focus');
        if (step.accounts.length === 1) {
            var account = step.accounts[0];
            var holds = selectedAccountKey === account;
            stepOn = holds ? null : { key: key, limit: g.key, account: account };
            selectAccount(holds ? '' : account, 'bar:' + g.key + ':' + account, g.key);
            restoreFocus(key);
            return;
        }
        var pressed = !(stepOn && stepOn.key === key);
        stepOn = pressed ? { key: key, limit: g.key } : null;
        stepShow(node, pressed ? step.accounts : []);
    }

    // The ladder folded: no open state, the scroll at its start, and no limit it stands open for.
    function ladderFold() {
        ladderOpen = false;
        ladderTop = 0;
        ladderKey = '';
    }

    // Put the text of a node only when it changes (the same words are not written again at a drawing).
    function textSet(node, text) {
        if (node.textContent !== text) node.textContent = text;
    }

    // The word under the window pressed: the ladder opens or folds where it stands (no drawing of the page: the plate, the graph and the Details are not touched). It folds back
    // to the first steps and to the start of its scroll; it opens by the steps past the ones that stood coming down one after another (only the ones in sight). It is open only
    // if the layout brings the window out (ladderLayout folds it again where there is no room).
    function ladderToggle(z) {
        if (ladderOpen) {
            // (the node's offset goes with its steps in a browser; it is put back to the start here for the nodes that keep it)
            if (ladderTop > 0) z.scroll.scrollTop = 0;
            ladderFold();
        } else {
            ladderOpen = true;
            ladderKey = z.key;
            ladderTop = 0;
        }
        ladderLayout(z, true);
    }

    // The layout of the ladder (kit: rail), for a drawing (ladderFit) and for the word under the window (ladderToggle): ONE read of the layout (the height of the zone and of the
    // parts, all in the same pass) says how many steps stand whole in the room between the last ring and the foot of the card, and how high the open window can be: at most
    // --ladder-window steps, and never more than the room (the word under it keeps its own); the classes are put first, so the read sees every part, and written after it, with no
    // second read. The word "N more" counts the steps that do not stand (all of them less those that do) and is shown only where its press opens the window: where not even one step
    // stands in it the word is not drawn, and a press that finds no room folds the state again (it does not open by itself when the room is back). Open, the window shows every step.
    // The ladder never stretches the card: the zone adds nothing to the height of the rail and cuts what is past its edge. Where not even one step and the word stand, the ladder is
    // not drawn, and where no step stands the plate keeps its own row of the next reset. No observer of the size and no script by frames: the 30 s poll draws again and fits again.
    // Where nothing is measured (the narrow shell has no ladder, a bare document has no layout) the state and the place of the scroll are kept as they are.
    function ladderLayout(z, clicked) {
        var wanted = ladderOpen;
        // Put every part the read needs on: the head, the steps that can stand folded (one measures them all) and the word.
        z.steps.forEach(function (node, i) { classSet(node, 'rail-fit-off', i >= LADDER_STEPS); classSet(node, 'rail-step-in', false); });
        classSet(z.head, 'rail-fit-off', false);
        classSet(z.more, 'rail-fit-off', false);
        z.scroll.style.height = '';
        var room = z.box.clientHeight;
        var measured = typeof room === 'number' && room > 0 && !narrowShell();
        var one = 0, win = 0, n = z.none ? z.steps.length : Math.min(z.total, LADDER_STEPS);
        var showMore = !z.none && z.total > n, drawn = true, open = false;
        if (measured) {
            var avail = room - z.head.offsetHeight;
            one = z.steps.length ? z.steps[0].offsetHeight : 0;
            var tail = z.more.offsetHeight;
            var fits = function (space) { return one > 0 ? Math.max(0, Math.floor(space / one)) : 0; };
            // The window the word opens: 5.5 steps, or the room under the head and over the word; it comes out only if one step stands in it.
            var per = 5.5;
            try { per = parseFloat(getComputedStyle(z.box).getPropertyValue('--ladder-window')) || per; } catch (err) { /* the tests' nodes */ }
            win = Math.floor(Math.min(per * one, avail - tail));
            var opens = one > 0 && win >= one;
            n = Math.min(z.steps.length, fits(avail));
            showMore = false;
            if (!z.none && (n < z.steps.length || z.total > LADDER_STEPS)) {
                n = Math.min(z.steps.length, fits(avail - tail));
                showMore = avail - n * one >= tail;
            }
            if (!z.none) n = Math.min(n, LADDER_STEPS);
            // The word counts what does not stand, and only where it can open it.
            showMore = showMore && opens && z.total > n;
            open = wanted && opens && !z.none && z.total > n;
            drawn = open || (avail >= 0 && (n > 0 || showMore));
            // The window did not come out (no room, or no step left to open): the state is folded, and the next drawing does not open it by itself.
            if (wanted && !open) ladderFold();
        } else {
            open = wanted && showMore;
        }
        var left = z.total - n;
        z.n = n;
        z.max = open && measured ? Math.max(0, z.total * one - win) : 0;
        if (open && measured) z.scroll.style.height = win + 'px';
        z.steps.forEach(function (node, i) { classSet(node, 'rail-fit-off', open ? !drawn : (i >= n || !drawn)); });
        classSet(z.head, 'rail-fit-off', !drawn);
        classSet(z.scroll, 'rail-scroll-on', open);
        classSet(z.more, 'rail-fit-off', !(drawn && (open || showMore)));
        classSet(z.more, 'rail-more-void', false);
        z.more.setAttribute('aria-expanded', open ? 'true' : 'false');
        // The word comes down after the last step that stands (its number in the movement of the rail).
        setVar(z.more, '--i', String(z.base + 1 + n));
        if (open) {
            textSet(z.more, t('ladder.less'));
            z.more.setAttribute('aria-label', t('ladder.less_label'));
        } else if (drawn && showMore) {
            textSet(z.more, t('foot.more', { n: left }));
            z.more.setAttribute('aria-label', t('ladder.more_label', { n: left }));
        }
        // The window keeps the place it had (the new node of a drawing: the kept one) and the haze follows. The place is written only to an open window that has one, and never
        // read back; where nothing is measured it is not touched (the narrow shell and back keeps it).
        var top = 0;
        if (measured) {
            top = open ? Math.max(0, Math.min(ladderTop, z.max)) : 0;
            ladderTop = top;
            if (open && top > 0) z.scroll.scrollTop = top;
        }
        ladderEdges(z, top);
        if (clicked && open && !reducedMotion()) {
            // The ones in sight below the steps that stood (a step and a half more at most), one after another.
            for (var i = z.n, j = 0; i < z.steps.length && j < 3; i++, j++) {
                setVar(z.steps[i], '--j', String(j));
                classSet(z.steps[i], 'rail-step-in', true);
            }
        }
        // The row of the next reset on the plate gives way to the ladder only while the ladder shows the next reset.
        if (!clicked && !(drawn && (n > 0 || open)) && ladderLine) classSet(ladderLine, 'nx-in-ladder', false);
    }

    // The fit of the ladder (kit: rail), at the end of a drawing and once for it. A drawing with no ladder leaves an open state nothing to stand on: it is folded.
    function ladderFit() {
        var z = ladderZone;
        ladderZone = null;
        if (!z) {
            ladderFold();
            return;
        }
        ladderLayout(z, false);
    }

    // The rail of windows (kit: rail; it returns the rail, a nav): the thread from the mark of the picked family (in the head) down to the first
    // ring, the kit's row of number tabs with a ring (kit: mark tab; markTab, markRow, ringGauge) and, under it, the ladder of resets (the stamp and the note are not in the rail: the stamp
    // stands in the plate, the note is returned through railNote and stands in the plate, under its number row). `groupOf(tab)` is the limit a tab opens on (null: the family reports no window at that place and no row is drawn for it; a window the
    // reader hid in Rows is the family's still, and keeps a dotted row that says so); `parts` are what stands beside the tabs: `ladder` (the picked limit, which the ladder of resets
    // stands on; none for the billing-cycle budget), `foot` (the note under the row of the picked window, or null), `accounts` (how many accounts the family has), `index` (the accounts by
    // their key, for the names) and `failed` (the read failed: no tab wears the star, and nothing is told of who is not read).
    function windowRow(hid, tabs, active, groupOf, parts) {
        var rail = el('nav', 'rail');
        rail.setAttribute('aria-label', t('rail.label'));
        // The thread from the picked family's mark down to the first ring (kit: rail): out of the reading order, and on the wide shell only.
        var lead = el('span', 'rail-lead');
        lead.setAttribute('aria-hidden', 'true');
        rail.appendChild(lead);
        var items = [];
        var spare = null;
        var firstOn = false;
        tabs.forEach(function (tab) {
            var g = groupOf(tab);
            // Only the windows the family reports have a row: a place it has no window for is not drawn at all (no grey place). A window
            // the reader hid in Rows is reported still, so its row stays.
            if (!g && !tab.hidden) return;
            var on = !!g && tab === active;
            var avg = g ? tabAverage(g) : '';
            var incomplete = !parts.failed && windowIncomplete(g);
            // A window that has accounts not read draws a note (windowFoot) whether it wears the star or not: the one that read none of
            // them ("—", no star) has its note too, so the place is kept for it as well.
            if (!parts.failed && g && !parts.foot && windowReading(g.cycle ? g.status : g).unread.length) {
                // The longest of the notes the other windows would draw: the place the picked one keeps, unseen.
                var copy = windowFoot(g.cycle ? g.status : g, parts.index, incomplete, true);
                if (copy && (!spare || copy.textContent.length > spare.textContent.length)) spare = copy;
            }
            // The rail says the window short ("7D", "7д"); its full name is in the tab's label and title below.
            // The ring sweeps (kit: ring) for the reader's pick: the ring of the window picked, or every ring as the rows of a family come down.
            var sweeps = sweepOn && !!g && avg !== '—' && (sweepWho === 'all' || sweepWho === String(tab.seconds));
            var ring = ringGauge(parseFloat(avg), g && avg !== '—' ? 'ok' : 'unknown', 'lg', true, sweeps);
            if (sweeps) {
                // A ring of a family's pick waits for its row (--i, the row's number, below); a drawing inside the span, with no clock, has gone on.
                if (sweepWho === 'all') ring.classList.add('ring-sweep-row');
                if (sweepShift) setVar(ring, '--sweep-elapsed', sweepShift + 'ms');
            }
            var btn = markTab({ key: tab.seconds, name: shortLength(tab.seconds), count: g ? avg : '—', note: incomplete ? '*' : '', selected: on,
                ring: ring });
            btn.className += ' wt-tab';
            // The row's number in the rail: the windows come down one after another by it (kit: rail, motion).
            setVar(btn, '--i', String(items.length));
            btn.setAttribute('id', 'quotas-win-' + tab.seconds);
            if (!g) {
                // A window the reader hid in Rows: its row stays, dotted, with its hint. aria-disabled, not disabled: a disabled button shows no hint.
                var said = tab.name + ' — ' + t('win.hidden_in_rows');
                btn.setAttribute('aria-disabled', 'true');
                btn.setAttribute('tabindex', '-1');
                btn.setAttribute('aria-label', said);
                btn.title = said;
            } else {
                // The billing-cycle budget (Cursor) is counted over its status, in a window's own shape: the same "N of M read" as the stamp's.
                var r = g.cycle ? (g.status ? windowReading(g.status) : null) : windowReading(g);
                // What the tab says to a reader who does not see it: the average, and over how many accounts it was read.
                var spoken = avg === '—' ? t('win.none', { name: tab.name })
                    : (r ? t('win.avg_read', { name: tab.name, avg: avg, read: r.read, n: r.total }) : t('win.avg', { name: tab.name, avg: avg }));
                if (r && parts.accounts && r.total < parts.accounts) spoken += '; ' + t('win.applies', { m: r.total, n: parts.accounts });
                btn.setAttribute('tabindex', on ? '0' : '-1');
                btn.setAttribute('aria-controls', 'quotas-win-panel');
                // The place of a billing-cycle budget (cycleBudget) says what it is, not a window of time.
                var hint = g.cycle ? g.note : '';
                btn.setAttribute('aria-label', hint ? spoken + ' — ' + hint : spoken);
                btn.setAttribute('data-focus', 'win:' + hid + ':' + tab.seconds);
                btn.title = hint || spoken;
                if (on && parts.foot) btn.setAttribute('aria-describedby', 'quotas-foot');
                btn.addEventListener('click', function (e) {
                    e.stopPropagation();
                    // A budget is not a limit to pick: its place is the only one it has.
                    if (!g.cycle && pickable(on)) pickWindow(g, on || !active ? '' : String(active.seconds), markPreviewed(btn), markPressed(e), on ? '' : String(tab.seconds));
                });
            }
            if (!items.length) firstOn = on;
            items.push(btn);
        });
        // A first ring that is picked has grown (--mark-on): the thread from the mark ends over it, as high as it has grown.
        if (firstOn) lead.className += ' rail-lead-on';
        var row = markRow(items, true);
        row.className += ' wt-row';
        row.setAttribute('aria-label', t('win.row'));
        arrowRow(row, ['ArrowLeft', 'ArrowRight']);
        // The reader's own pick of a window (winFrom: the one left): the kit draws the rings and numbers that change in the look they had a
        // moment ago, and render() has settleMarks take that off at the next frame.
        markStart(row, winFrom, active ? String(active.seconds) : '', winLit, winPressed);
        rail.appendChild(row);
        // The ladder of resets (kit: rail) stands under the rings, last in the rail (no foot): the window picked has it, the billing-cycle budget has not. The pick of a family
        // brings it down after the rings (its rows are numbered on from theirs).
        ladderZone = null;
        ladderDrawn = false;
        ladderLine = null;
        var ladder = parts.ladder ? ladderNode(parts.ladder, parts.index, railMotion ? items.length : 0) : null;
        if (ladder) {
            rail.appendChild(ladder);
            ladderDrawn = true;
        }
        // The note under the row is the picked window's. Where another window of the family has a note (accounts not read: the star on its
        // tab, or the dash of a window that read none) and this one does not, the place of the note stays, unseen (a hidden copy of the
        // longest note the others would draw), so the plate does not jump as the reader moves along the row; with no note in the row
        // there is none.
        // It does not fit the narrow rail (the names of the accounts are long), so it stands in the plate, under the number row (railNote: renderReserve
        // hands it to the plate).
        var foot = parts.foot || spare;
        railNote = foot || null;
        if (foot) {
            // Only the reader's own pick moves it: it rises in, and a redraw inside the pick's span carries it on from where it was. The pick
            // of a family moves it with the rail (it fades in after the last window, kit: rail), not on its own; it is out of the rail's
            // tree, so it carries the rail's numbers (--rail-rows, --rail-elapsed) itself.
            if (drawMotion && parts.foot && !railMotion) {
                if (drawElapsed) foot.style.animationDelay = -drawElapsed + 'ms';
            } else if (railMotion && parts.foot) {
                foot.className += ' wt-foot-in';
                setVar(foot, '--rail-rows', String(items.length + (ladderZone ? ladderZone.rows : 0)));
                if (railShift) setVar(foot, '--rail-elapsed', railShift + 'ms');
            }
        }
        // How many of the window's accounts are read, and how old the reading is (the stamp), is not in the rail: it stands in the row of the figure of the plate (limitRow).
        // The reader's own pick of a family (railMotion): the thread is drawn, the windows come down one after another and the foot comes
        // last (kit: rail, motion, which reads --i of each row and --rail-rows). A drawing inside the movement's span carries it on from
        // where it was (--rail-elapsed). Any other drawing wears none of it.
        if (railMotion) {
            rail.className += ' rail-moving';
            setVar(rail, '--rail-rows', String(items.length + (ladderZone ? ladderZone.rows : 0)));
            if (railShift) setVar(rail, '--rail-elapsed', railShift + 'ms');
        }
        return rail;
    }

    // Which limit of the length is shown, among the length's own: the scope
    // row is a filter of the plate, not another step. It is the kit's segment in its pill variant, the one the graph's "Period" is: the picked
    // option stands white on the segment's grey.
    function scopeRow(ctx, tab, g) {
        var box = el('div', 'seg wt-scope');
        box.setAttribute('role', 'group');
        box.setAttribute('aria-label', t('scope.row'));
        tab.groups.forEach(function (sg) {
            var on = sg.key === g.key;
            var btn = el('button', 'seg-opt btn-compact hover-quiet wt-scope-opt' + (on ? ' active' : ''), scopeLabel(sg, ctx.names, tab));
            btn.setAttribute('type', 'button');
            btn.setAttribute('aria-pressed', on ? 'true' : 'false');
            btn.setAttribute('data-focus', 'scope:' + sg.key);
            if (scopeWords(sg)) btn.title = scopeWords(sg);
            btn.addEventListener('click', function (e) {
                e.stopPropagation();
                if (pickable(on)) pickWindow(sg);
            });
            box.appendChild(btn);
        });
        return box;
    }

    // The plate of the picked window: its limit row, once. With motion the
    // content comes in (the bars grow from their base, a step apart).
    function windowPlate(ctx, tab, g, chartKey, markTightest, stamp, foot, credits) {
        var scope = tab.groups.length > 1 ? scopeRow(ctx, tab, g) : null;
        var plate = limitRow(g, ctx.names[g.key], chartKey, ctx.index, markTightest, stamp, tab.name,
            { scope: scope, foot: foot, credits: credits });
        plate.setAttribute('id', 'quotas-win-panel');
        plate.setAttribute('role', 'tabpanel');
        plate.setAttribute('aria-labelledby', 'quotas-win-' + tab.seconds);
        if (scope) plate.className += ' wt-has-scope';
        plateMotion(plate);
        return plate;
    }

    // The plate's coming in, for the reader's own pick (drawMotion): its fact, its bubble of the accounts at the limit, the model list, the stamp and
    // the two rows under the bars rise and the bars grow from their base, a step apart. A redraw nobody asked for (the poll) moves nothing.
    // The plate of the billing-cycle budget comes in the same way.
    function plateMotion(plate) {
        if (!drawMotion) return;
        plate.className += ' wt-in';
        Array.prototype.forEach.call(plate.childNodes, function (part) {
            if (hasClass(part, 'bars')) {
                Array.prototype.forEach.call(part.childNodes, function (bar, i) {
                    bar.style.animationDelay = (Math.min(i * 12, 80) - drawElapsed) + 'ms';
                });
            } else if (drawElapsed && hasAnyClass(part, ['l-fig', 'wt-fact', 'wt-limit', 'lg', 'nx', 'wt-scope', 'wt-stamp'])) {
                part.style.animationDelay = -drawElapsed + 'ms';
            }
        });
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
            // No time window, but a budget per billing cycle: it takes the 30-day place and says what it is, and it is the one row:
            // 5-hour and Weekly are not drawn (the family reports no window for them).
            var places = windowTabs([], {}, {});
            var place = places[WINDOW_PLACES.indexOf(CYCLE_PLACE)];
            ctx.cycle = cycle;
            railed(windowRow(hid, places, place, function (tab) {
                return tab === place ? cycle.tab : null;
            }, {
                accounts: 0, index: ctx.index, failed: failed,
                foot: failed ? null : windowFoot(cycle.status, ctx.index, windowIncomplete(cycle.tab)) }));
            var cycleBox = limitsBox(family);
            cycleBox.appendChild(cyclePlate(cycle, place, ctx.index, windowStamp(view, summary, cycle.status), railNote, familyCreditsNode(family)));
            // The card of the account a bar of the budget selected stands right under the plate.
            if (card && !card.drawn && card.limit === cycle.key) card.draw(cycleBox, ctx);
            cycleBox.appendChild(cycleNote());
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
        if (shown) scopePicks[hid + ':' + active.seconds] = shown.key;
        // The pick of the window on screen is decided here, before its plate is built (barStrip reads it): the top account unless the reader has picked or let go. The card of the
        // pick is the one this hands back (ctx.card: the drawing hands it on to the account list).
        if (shown) {
            card = pickDecide(family, shown, hid + '|' + shown.key, card, view);
            ctx.card = card;
        }
        railed(windowRow(hid, tabs, active, function (tab) {
            return tab === active ? shown
                : (groupByKey(tab.groups, scopePicks[hid + ':' + tab.seconds]) || tab.groups[0] || null);
        }, {
            index: ctx.index, failed: failed, ladder: shown,
            foot: shown && !failed ? windowFoot(shown, ctx.index, windowIncomplete(shown)) : null,
            // How many accounts the family's widest window applies to: a window with fewer says so in its tab's label.
            accounts: Math.max.apply(null, [0].concat(groups.map(slotCount)))
        }));
        if (shown) {
            // The limit the plate stands for: a row of the list picks its account on this limit's bar (renderAccounts), as the bar does.
            ctx.pickGroup = shown;
            var shownPlate = windowPlate(ctx, active, shown, chartKey, shown.tightest && measuredGroups > 1, windowStamp(view, summary, shown),
                railNote, familyCreditsNode(family));
            table.appendChild(shownPlate);
            // The timeline of the charted limit is one node in a fold (foldOf): right under its plate at rest; where a bar of the plate is picked
            // it stands in the plate, right under the bars (.tl-top, the area `chart`) and the card of the account stands under the plate. Folded
            // (the plate's word "Chart" not pressed), it leaves nothing behind. The place is worked out here from the pick, with no motion: the
            // poll and a drawing nobody asked for build the same screen a pick on the plate (selectInPlace) arrives at.
            var line = timelineOpen ? foldOf('tl', timelineBlock(ctx, shown), false) : null;
            // The strip the chart folds into under the account's card (chartStrip), after the fold: the CSS shows it only while the card is up.
            if (line) line.appendChild(chartStrip(ctx.names[shown.key] || '', line));
            var barsOfPlate = plateBars(shownPlate);
            var lifted = !!line && !!barsOfPlate && !!pickedIn(barsOfPlate);
            if (lifted) {
                line.className += ' tl-top';
                shownPlate.className += ' wt-up';
                // The plate wears the line of the pick (its outline) from the build; the arrow comes with barsFit. Where the line is yet to be drawn (pickDue: the opening, a pick of a window
                // or a family) it wears none: the line draws it (pickTraceRun).
                if (blockOf(line)) classSet(blockOf(line), 'tl-traced', !pickDue);
                // And it waits behind the stand-ins until the line has met (waitOn; pickTraceRun / traceStart end it): the poll owes no line, so shows none.
                if (blockOf(line) && pickDue) waitOn(blockOf(line));
                shownPlate.insertBefore(line, barsOfPlate.nextSibling);
            }
            // The card of the account a bar of this limit selected stands right under the plate, in a fold of its own: under the chart in the plate (the plate's area `card`), and where
            // there is no chart over it (Chart folded) in its own place under the plate, whole.
            if (hid + '|' + shown.key !== cardWhere) {
                cardWhere = hid + '|' + shown.key;
                cardReset();
            }
            if (card && !card.drawn && card.limit === shown.key) {
                var cardHolder = el('div', null);
                card.draw(cardHolder, ctx);
                if (cardHolder.firstChild) {
                    var cardBox = foldOf('ins', cardHolder.firstChild, false);
                    if (lifted) {
                        shownPlate.insertBefore(cardBox, line.nextSibling);
                        cardTake(shownPlate);
                    } else {
                        table.appendChild(cardBox);
                    }
                }
            }
            if (line && !lifted) table.appendChild(line);
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
                if (stepOn && stepOn.account) stepOn = null;
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
            var accWord = wordButton('acc.word', { n: family.accounts.length });
            accWord.setAttribute('data-focus', 'accounts');
            accWord.setAttribute('aria-pressed', accountsOpen ? 'true' : 'false');
            accWord.addEventListener('click', function (e) {
                e.stopPropagation();
                accountsSet(!accountsOpen);
                rerender();
                restoreFocus('accounts');
            });
            accStrip.appendChild(accWord);
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
            section.appendChild(accStrip);
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
        box.appendChild(el('p', 'chart-notes', 'The table lists ' + rows.length + ' of ' + recorded
            + ' recorded changes; the chart and its cursor use all of them.'));
        var table = el('table', 'tbl');
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
        var table = el('table', 'tbl'), thead = el('thead'), head = el('tr');
        ['Time', 'History', 'No new use', 'Recent pace', 'Context'].forEach(function (text) {
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
            var half = (tip.offsetWidth || 170) / 2;
            var room = plot.clientWidth || W * scale + 2 * inset;
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
                parts.push([UNKNOWN_WORDS[bar.why] || t('acc.lim_no_usable'), '']);
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

    // The account's share left in each window of its family, from the same bars accountLimits draws: the strip's lead (the first) and the row of windows shown under the pointer.
    function peekOf(ctx, account) {
        var out = [];
        ((ctx && ctx.groups) || []).forEach(function (g) {
            var bar = barOf(g, account.key);
            if (!bar || bar.state === 'unknown' || typeof bar.left !== 'number') return;
            out.push({ k: String(ctx.names[g.key] || ''), p: leftPct(bar.left * 100) + '%' });
        });
        return out;
    }

    // The card as the chart's plate holds it: a strip (.kt-top: the head, the lead window, the row of windows under the pointer, and the button over it all, which presses it open) and the
    // rest of the card in a fold of its own (.kt-more). The press is kept in cardOpen, so a drawing builds the card as it stood; another account (selectAccount) lets it go, the window or the family changing too, so a new card is a strip and the chart stands under it. Outside the plate the CSS shows it whole and the button is hidden.
    function cardFold(card, head, clear, pad, account, ctx) {
        var top = el('div', 'kt-top');
        var peek = peekOf(ctx, account);
        var right = el('div', 'kt-right');
        var lead = el('span', 'kt-lead');
        if (peek.length) {
            lead.textContent = peek[0].p;
            lead.title = peek[0].k + ' ' + peek[0].p;
        }
        right.appendChild(lead);
        right.appendChild(clear);
        head.appendChild(right);
        top.appendChild(head);
        var tg = el('button', 'kt-toggle');
        tg.setAttribute('type', 'button');
        tg.setAttribute('aria-expanded', cardUp() ? 'true' : 'false');
        tg.setAttribute('aria-label', t('acc.details', { name: account.label }));
        tg.setAttribute('data-focus', 'inspector-toggle');
        tg.addEventListener('click', function (e) {
            e.stopPropagation();
            cardPress(card, !cardOpen, true);
        });
        // A click on the up card's own ground (not on a button, a link, a field, a summary, and no selection made) pins it too: the pointer that came to the strip finds the strip gone up by the click.
        card.addEventListener('click', function (e) {
            try {
                if (cardOpen || !cardUp()) return;
                var hit = e.target && typeof e.target.closest === 'function' ? e.target.closest('a,button,input,select,textarea,summary,label,[role=button],[tabindex]') : null;
                var chosen = window.getSelection ? String(window.getSelection()) : '';
                if (!hit && !chosen) cardPress(card, true, true);
            } catch (err) {
                // A card that cannot be asked is pinned by its strip only.
            }
        });
        // Under the pointer or with the keyboard focus in it the card goes up (a finger and a narrow screen: the strip's press only). The focus counts when the keyboard put it there (:focus-visible).
        card.addEventListener('pointerenter', function (e) {
            if (e.pointerType !== 'touch') cardHold(card, 'hover', true);
        });
        card.addEventListener('pointerleave', function (e) {
            if (e.pointerType !== 'touch') cardHold(card, 'hover', false);
        });
        card.addEventListener('focusin', function (e) {
            var seen = false;
            try {
                seen = !!(e.target && typeof e.target.matches === 'function' && e.target.matches(':focus-visible'));
            } catch (err) {
                seen = false;
            }
            if (seen) cardHold(card, 'focus', true);
        });
        card.addEventListener('focusout', function () {
            if (cardFocus) cardHold(card, 'focus', false);
        });
        top.appendChild(tg);
        var more = el('div', 'kt-more');
        var clip = el('div', 'kt-clip');
        clip.appendChild(pad);
        more.appendChild(clip);
        card.appendChild(top);
        card.appendChild(more);
        cardRoomSet(card);
        cardLast = card;
        // A card drawn again under a pointer that has gone meanwhile (the poll) gets no leave event: after a moment the pointer is asked, and a card it is not on goes down.
        if (cardHover) {
            setTimeout(function () {
                try {
                    if (cardHover && cardLast === card && card.isConnected && !card.matches(':hover')) cardHold(card, 'hover', false);
                } catch (err) {
                    // Nothing to ask: the pointer's leave event ends it.
                }
            }, 400);
        }
        return card;
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

    function renderInspector(parent, group, account, ctx, facets) {
        var quota = account.quota || {};
        var overview = !!(ctx && ctx.groups.length);
        // No limit window to draw means no chart over the card (Cursor reports only its budget per billing cycle): the card stands whole under the budget's plate, so it is a strip and a fold (plainFold).
        // The strip speaks of the budget, so it is the card of an account that reads a budget and no window. An account that has windows but no overview to chart them keeps its whole card, and so does one that reads neither.
        var plain = !overview && !hasLimitWindows(account) && !!cycleOf(account);
        var card = el('section', 'inspector' + (!plain && cardUp() ? ' kt-open' : ''));
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
            parent.appendChild(plain ? plainFold(card, head, box, account, verdict) : cardFold(card, head, clear, box, account, ctx));
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
        parent.appendChild(cardFold(card, head, clear, box, account, ctx));
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
        table.setAttribute('aria-label', 'Accounts of ' + familyName(group) + ', share left in each limit');
        var headRow = el('div', 'acc-headrow');
        headRow.style.gridTemplateColumns = template;
        headRow.setAttribute('aria-hidden', 'true');
        headRow.appendChild(el('span', null, t('acc.account')));
        groups.forEach(function (g) {
            var h = el('span', 'acc-cell', names[g.key]);
            h.title = names[g.key] + ' — share left';
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
                var spoken = names[g.key] + ' no reading, may not apply';
                if (bar && bar.state !== 'unknown' && typeof bar.left === 'number') {
                    text = leftPct(bar.left * 100) + '%';
                    cls = 'acc-cell' + (bar.state === 'last_known' ? ' last'
                        : (bar.at_limit ? ' bad' : (heldNow(bar) ? ' held' : '')));
                    spoken = names[g.key] + ' ' + text + ' left' + (bar.state === 'last_known'
                        ? ', last known, read ' + (relTime(bar.observed_at) || 'at an unreported time') : '');
                } else if (bar && bar.state === 'unlimited') {
                    text = t('cycle.unlimited');
                    cls = 'acc-cell';
                    spoken = names[g.key] + ' unlimited, no share';
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
    function readView(reuse, withText) {
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
            if (withText) adoptI18n(table);
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
            // A read that failed brings no chart: the stand-ins and the line that is owed end now, not at the deadline.
            waitExpire();
            return;
        }
        stepRelease(got.view);
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
        // The busy mark of a poll that begins in the middle of a fold or the line is not drawn at once: that drawing would take the page anew and cut the motion (landWhenStill lands the reading after it).
        // A read for the chart (reuse) is asked for by the drawing that has just been made (askForChart): the mark is set on that screen (busyShow), and the page is drawn once more only when the chart has come.
        if (reuse) busyShow();
        else if (!motionOn()) rerender();
        var mine = ++generation;
        // The text table is asked for with every ordinary reading (not with a chart or limit
        // switch): the language may have been changed, or a batch of the translation landed.
        Promise.all([readView(!!reuse), reuse ? null : readI18n()]).then(function (both) {
            // The table did not come by its own route (an error, nothing, an empty table): the reading is made once more with the table asked for in it, so the numbers are
            // never held back for the words. The plugin answers both routes; where it has no English at all there is nothing to say them with (below).
            if (i18n.en || stopped || mine !== generation) return both;
            return readView(!!reuse, true).then(function (again) { return [again, null]; });
        }).then(function (both) {
            var got = both[0];
            if (mine !== generation || stopped) return;
            inFlight = false;
            if (!i18n.en) {
                drawNoText(true);
                return;
            }
            // A reading of the poll (not a chart or limit switch, which the reader asked for) lands after the motion that is under way has ended, in a second at most (landWhenStill).
            function apply() {
                land(got, got.view ? '' : actionMessage);
            }
            if (reuse) apply();
            else landWhenStill(apply, mine);
        })['catch'](function (err) {
            if (mine === generation) inFlight = false;
            failNow(err);
        });
    }

    function failNow(err) {
        try { console.error('claudexor quotas: reading not handled', err); } catch (e) { /* no console */ }
        actionMessage = t('act.failed_handle', { err: String((err && err.message) || err) });
        rerender();
        waitExpire();
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
            if (stepOn && stepOn.account) stepOn = null;
            return { group: null, account: null };
        }
        selectedHarness = group.harness_id;
        var account = null;
        (group.accounts || []).forEach(function (a) { if (a && a.key === selectedAccountKey) account = a; });
        if (!account) selectedAccountKey = '';
        // The step of one account (stepPress) stands only while its account is the one picked.
        if (stepOn && stepOn.account && stepOn.account !== selectedAccountKey) stepOn = null;
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
                // The family the reader leaves: its mark gives way as the mark of the one picked comes in. The tab picked may be
                // showing its preview this moment (the pointer on it, or the keyboard): its mark is red, and starts from that red.
                markFrom = selectedHarness;
                markLit = markPreviewed(btn);
                // The tabs are about to move under a pointer that stands still: no preview by the pointer until it moves (the keyboard's stays).
                stillHold = true;
                stillAt = e && typeof e.clientX === 'number' && typeof e.clientY === 'number' ? { x: e.clientX, y: e.clientY } : null;
                // Only the family's marks move for it: the window row's numbers stay.
                winFrom = '';
                winLit = false;
                winPressed = false;
                selectedHarness = group.harness_id;
                selectedAccountKey = '';
                diagnosticsOpen = false;
                // Another family is another place: the top account of its window is picked afresh, and its line is drawn (pickDecide).
                pickAuto = false;
                pickBare = '';
                pickAt = '';
                chartCursor = null;
                // The reader's own pick of a family: its window row and the tabs' marks answer with motion.
                motionPending = true;
                // And the rail moves in (kit: rail, motion): the tabs slide, the thread is drawn, the windows come down.
                railPending = true;
                // The ladder of another family starts folded, at its start, with no step pressed.
                ladderFold();
                stepClear();
                // Every ring of the rail sweeps as its row comes down (kit: ring, sweep).
                sweepPending = 'all';
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
    // the order of the answer, and the family picked moved to the front. What it returns is kept as the order the head shows now.
    function orderedFamilies(groups, selected) {
        var byId = {};
        groups.forEach(function (group) { byId[group.harness_id] = group; });
        var ids = familyOrder.filter(function (id) { return !!byId[id]; });
        groups.forEach(function (group) { if (ids.indexOf(group.harness_id) < 0) ids.push(group.harness_id); });
        var at = selected ? ids.indexOf(selected.harness_id) : -1;
        if (at > 0) ids.unshift(ids.splice(at, 1)[0]);
        familyOrder = ids;
        return ids.map(function (id) { return byId[id]; });
    }

    // The head's families: the kit's row of mark tabs (markRow), given the place the head wants for it (.harness-seg) and its name. The row's
    // selector is the head's to give the kit's slide (markWas, markFlip, markSlide find the tabs of the row by it).
    var HEAD_ROW = '.harness-seg';

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
            // The family picked stands first, the others after it in the order they had (the order in the markup is the order to read).
            seg = markRow(orderedFamilies(groups, selected).map(function (group) { return familyTab(group, selected); }), false, stillHold);
            stillRow = stillHold ? seg : null;
            seg.setAttribute('aria-label', 'Agent family');
            // The reader's own pick of a family (markFrom: the family left): the kit draws the marks that change in the look they had a
            // moment ago, and render() has settleMarks take that off at the next frame.
            markStart(seg, markFrom, selected ? selected.harness_id : '', markLit);
        }
        seg.className += ' harness-seg';
        parent.appendChild(seg);
    }

    function render(view, staleText, actionText) {
        traceWidth();
        chartDrawn = null;
        currentView = view;
        staleMessage = staleText;
        actionMessage = actionText || '';
        // Only a drawing that answers the reader's own pick moves; the redraw
        // of fresh data (every 30 seconds) never does.
        // The read the pick itself asks for (the chart of the new window) redraws
        // within the motion's span: that drawing carries it on from where it was.
        var stamp = Date.now();
        // A drawing for a new width (chartFit) is nobody's pick: it moves nothing, and a movement in hand ends where it stands.
        var calmDraw = reducedMotion() || fitDrawing;
        drawMotion = false;
        drawElapsed = 0;
        if (!calmDraw) {
            if (motionPending) {
                drawMotion = true;
                motionAt = stamp;
            } else if (motionAt && stamp - motionAt < MOTION_MS) {
                drawMotion = true;
                drawElapsed = stamp - motionAt;
            }
        }
        motionPending = false;
        markStarts = [];
        // The movement of a family's pick (railMotion): this drawing is the pick's own, or inside the span of the movement it began.
        railMotion = false;
        railElapsed = 0;
        var railPick = false;
        // A pick inside the span of the movement of the one before it (a quick second pick).
        var railAgain = false;
        if (!calmDraw) {
            if (railPending) {
                railMotion = true;
                railPick = true;
                railAgain = !!railMotionAt && stamp - railMotionAt < RAIL_MOTION_MS;
                railMotionAt = stamp;
                // The moment of the pick on the page's own clock, kept with the movement (clockSet).
                railStart = timelineNow();
            } else if (railMotionAt && stamp - railMotionAt < RAIL_MOTION_MS) {
                railMotion = true;
                railElapsed = stamp - railMotionAt;
            }
        }
        // By the page's own clock a drawing of the movement puts no delay of its own (its animations are given the start time of the pick);
        // a page with no clock, or no way to give it, goes on by the delay.
        railClock = railMotion && railStart !== null && typeof document.getAnimations === 'function';
        railShift = railClock ? 0 : railElapsed;
        // The sweep of the rings (kit: ring, motion): this drawing is the pick's own, or inside the span of the sweep it began.
        sweepOn = false;
        var sweepElapsed = 0;
        if (!calmDraw) {
            if (sweepPending) {
                sweepOn = true;
                sweepWho = sweepPending;
                sweepAt = stamp;
                sweepStart = timelineNow();
            } else if (sweepAt && stamp - sweepAt < SWEEP_MS) {
                sweepOn = true;
                sweepElapsed = stamp - sweepAt;
            }
        }
        sweepPending = '';
        sweepClock = sweepOn && sweepStart !== null && typeof document.getAnimations === 'function';
        sweepShift = sweepClock ? 0 : sweepElapsed;
        // The movement of the ladder of resets (kit: rail, motion): this drawing is the pick of a window's own, or inside the span of the movement it began.
        // The poll and Refresh draw none of it.
        ladderMotion = false;
        var ladderElapsed = 0;
        if (!calmDraw) {
            if (ladderPending) {
                ladderMotion = true;
                ladderAt = stamp;
                ladderStart = timelineNow();
            } else if (ladderAt && stamp - ladderAt < LADDER_MS) {
                ladderMotion = true;
                ladderElapsed = stamp - ladderAt;
            }
        }
        // The pick's own drawing keeps the node of the ladder that stood, to fade it over the new one (ladderNode); a drawing inside the span puts it again.
        if (ladderPending && ladderMotion) ladderKept = ladderLast;
        else if (!ladderMotion) ladderKept = null;
        ladderOut = ladderKept;
        ladderLast = null;
        ladderPending = false;
        ladderClock = ladderMotion && ladderStart !== null && typeof document.getAnimations === 'function';
        ladderShift = ladderClock ? 0 : ladderElapsed;
        // The shifts of the tabs belong to the movement: when it is over (or there is none) they go.
        if (!railMotion) railFlips = {};
        railPending = false;

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

        // The places of the family tabs before the drawing (one read), for the slide of the movement: the pick's own drawing alone reads them (the
        // shifts are worked out once and kept); a drawing inside the span of the movement reads no place.
        var flipWas = railPick ? markWas(root, HEAD_ROW, railAgain) : null;

        // The drawing under way is not the screen a pick in its place (selectInPlace) may read; it is set again when this one has been drawn.
        drawnFrom = null;
        // The motion of the folds (selectInPlace) ends with the drawing: this one builds the state the pick left, with no motion.
        foldCancel();
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
        railNote = null;
        ladderZone = null;
        ladderDrawn = false;
        ladderLine = null;

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
        // The pick works the shifts out (the places again, one read) and every drawing of the movement puts them on its tabs.
        if (flipWas) railFlips = markFlip(root, HEAD_ROW, flipWas);
        // The rail's width and axis from the tabs as drawn (before the slide writes its classes, so the layout markFlip has read answers it).
        railFit();
        if (railMotion) markSlide(root, HEAD_ROW, railFlips, railElapsed, railShift);
        settleMarks();
        if (railClock) clockSet(RAIL_KEYFRAMES, railStart);
        if (sweepClock) clockSet(SWEEP_KEYFRAMES, sweepStart);
        if (ladderClock) clockSet(LADDER_KEYFRAMES, ladderStart);
        // What this drawing was made from, for a pick on the plate with no drawing (selectInPlace).
        drawnFrom = ctx && selection.group ? { group: selection.group, ctx: ctx, facets: facets } : null;
        // The bars' rows: their kept places put back and their edges worked out (one read of the layout, as the ladder's fit). The line of a pick the widget made (pickDue) keeps the arrow
        // off until it is drawn (pickTraceRun, once the chart is in).
        pickTraceHold();
        barsFit();
        pickTraceRun();
        // The ladder's fit: the one read of the layout of the drawing (the card is in place now), after the movement is set up.
        ladderFit();
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

    // The pointer's press on a bar (not the keyboard's, which has a detail of 0): the rows of bars are still until the pointer moves (barStill), so the bar that slides under the pointer as the pick moves
    // the others does not open by it. The rows that stand wear the class now; a row drawn anew wears it from the flag (barStrip).
    function holdBarStill(e) {
        if (!e || !(e.detail > 0) || !root || typeof root.querySelectorAll !== 'function') return;
        barStill = true;
        barStillAt = typeof e.clientX === 'number' && typeof e.clientY === 'number' ? { x: e.clientX, y: e.clientY } : null;
        var rows = rootAll('.bars');
        for (var i = 0; i < rows.length; i++) classSet(rows[i], 'bars-still', true);
    }

    // The pointer may open the bars again: the flag goes and the rows that stand wear no more.
    function releaseBarStill() {
        barStill = false;
        barStillAt = null;
        var rows = rootAll('.bars-still');
        for (var i = 0; i < rows.length; i++) classSet(rows[i], 'bars-still', false);
    }

    // The head's tabs wear their preview by the pointer again (kit: mark tab, a row that is still): the flag goes and the row drawn wears no more.
    function releaseStill() {
        stillHold = false;
        stillAt = null;
        markStill(stillRow, false);
        stillRow = null;
    }

    // A redraw under a resting pointer keeps the chart's cursor; once the
    // pointer moves anywhere but the chart, the cursor goes with it.
    function onDocumentPointer(e) {
        // The first real move of the pointer after a pick of a family gives the head's tabs their preview back; a move that is none (the
        // pointer where the click was) does not.
        if (stillHold && !(stillAt && e && e.clientX === stillAt.x && e.clientY === stillAt.y)) releaseStill();
        if (barStill && !(barStillAt && e && e.clientX === barStillAt.x && e.clientY === barStillAt.y)) releaseBarStill();
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
        } else if (stepOn) {
            // No pick to let go: the step pressed is what Escape lets go of.
            stepClear();
        }
    }

    function stop() {
        stopped = true;
        if (themeOff) { themeOff(); themeOff = null; }
        generation++;
        landRelease(false);
        foldUnhook();
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
