/* Memory Atlas — module widget (classic entry, self-contained).
 *
 * Consumes only the frozen read-only API in docs/api-contract.md through
 * OuroborosWidget.fetch on its own prefix. No CDN, no eval, no storage, no
 * parent DOM access, no innerHTML: every node that carries memory text is
 * built with createElement/createTextNode, so untrusted document content is
 * never parsed as HTML. That is why no third-party sanitizer is vendored —
 * there is no HTML parse step to sanitize.
 */
(function () {
  'use strict';

  var PREFIX = '/api/extensions/memory-atlas';
  var CATALOG_LIMIT = 100;
  var CATALOG_MAX_PAGES = 20;
  var DOC_LIMIT = 16384;
  var HISTORY_LIMIT = 50;
  var SEARCH_LIMIT = 25;
  var GRAPH_LIMIT = 50;
  var DIALOGUE_LIMIT = 10;
  var REQUEST_TIMEOUT_MS = 20000;
  var NARROW_PX = 640;

  /* One entry per catalogue `family`. `desc` is the plain-English sentence shown
   * under the section heading; `note` is the honest nature note that says what
   * the source is *not*, so nothing here has to be guessed from its name. */
  var FAMILIES = [
    { key: 'identity', label: 'Identity', scope: 'global', hue: '#7aa2c9',
      desc: 'Who I am, written down and kept.',
      note: '' },
    { key: 'scratchpad', label: 'Working memory', scope: 'global', hue: '#c9a87a',
      desc: 'Short-lived working notes that are added to and evicted as work goes on.',
      note: '' },
    { key: 'knowledge', label: 'Knowledge', scope: 'global', hue: '#8fbf9f',
      desc: 'Notes written on purpose and kept for the long term.',
      note: '' },
    { key: 'dialogue', label: 'Dialogue chronicle', scope: 'global', hue: '#9d9ed6',
      desc: 'Consolidated summary blocks of past conversation.',
      note: 'This is not raw chat. The raw chat log is not a memory source and is not shown here.' },
    { key: 'dialogue_legacy', label: 'Dialogue summary (legacy)', scope: 'global', hue: '#8b8b93',
      desc: 'An older file kept only because it exists.',
      note: 'Nothing writes to it any more, so it is not current.' },
    { key: 'world', label: 'World profile', scope: 'global', hue: '#7fb8bf',
      desc: 'Generated environment profile.',
      note: 'Generated, not authored knowledge.' },
    { key: 'registry', label: 'Memory source registry', scope: 'global', hue: '#b9a6c9',
      desc: 'The map of memory sources with trust and gap annotations.',
      note: '' },
    { key: 'deep_review', label: 'Latest self-review', scope: 'global', hue: '#c3b27a',
      desc: 'The most recent self-review text.',
      note: 'Overwritten in place, so only the latest text exists and there is no '
        + 'evolution timeline for it.' },
    { key: 'reflections', label: 'Task reflections', scope: 'global', hue: '#a3bd8f',
      desc: 'Recorded execution history of finished tasks — what was done, what it cost.',
      note: 'This is a record of execution, not hidden reasoning.' },
    { key: 'project_knowledge', label: 'Project knowledge', scope: 'project', hue: '#a99fd0',
      desc: 'Notes kept for this project rather than for me as a whole.',
      note: '' },
    { key: 'project_workpad', label: 'Workpad', scope: 'project', hue: '#c9b0a0',
      desc: 'The scratch surface for work in progress on this project.',
      note: '' },
    { key: 'project_journal', label: 'Journal', scope: 'project', hue: '#9fb6c9',
      desc: 'Dated entries recording what happened on this project.',
      note: '' },
    { key: 'project_reflections', label: 'Task reflections', scope: 'project', hue: '#a3bd8f',
      desc: 'Recorded execution history of finished tasks — what was done, what it cost.',
      note: 'This is a record of execution, not hidden reasoning.' }
  ];
  var OTHER_FAMILY = { key: '_other', label: 'Other sources', scope: 'global', hue: '#8b8b93',
    desc: 'Sources the catalogue lists under a family this reader does not know by name.',
    note: 'They are listed exactly as the catalogue reports them.' };

  /* Stated exclusion policy. Everything here is left out on purpose, with the
   * reason spelled out, so an absence cannot be mistaken for a hidden store. */
  var EXCLUDED = [
    ['Raw chat log and its archives',
     'these are the input the summaries were made from, not a memory source'],
    ['Owner mailbox',
     'messages to and from the owner are correspondence, not remembered content'],
    ['Control and queue state',
     'scheduling and run bookkeeping is machinery, not memory'],
    ['Settings',
     'configuration says how things run, not what is remembered'],
    ['Secrets',
     'credentials are never displayed anywhere in this reader'],
    ['Tool and execution logs',
     'transient run output, kept for debugging rather than as memory']
  ];

  var CSS = [
    'html,body{height:100%;margin:0;padding:0;background:#0d0b0f;}',
    '#memory-atlas-root{position:absolute;inset:0;display:flex;flex-direction:column;',
    'background:#0d0b0f;color:#e2e8f0;font:14px/1.55 ui-sans-serif,-apple-system,"Segoe UI",Roboto,sans-serif;',
    '-webkit-font-smoothing:antialiased;overflow:hidden;}',
    '#memory-atlas-root *{box-sizing:border-box;}',
    /* the outline follows whatever radius the control already has, so a pill
     * control does not square off the moment it takes focus */
    '#memory-atlas-root :focus-visible{outline:2px solid #f07a86;outline-offset:2px;}',
    '.ma-hair{border:0;border-top:1px solid rgba(255,255,255,.08);margin:0;}',
    '.ma-meta{color:rgba(255,255,255,.68);font-size:12px;}',
    '.ma-sr{position:absolute;width:1px;height:1px;overflow:hidden;clip:rect(0 0 0 0);white-space:nowrap;}',
    /* top bar */
    '.ma-top{display:flex;align-items:center;gap:10px;row-gap:8px;flex-wrap:wrap;padding:8px 14px;',
    'border-bottom:1px solid rgba(255,255,255,.08);background:rgba(255,255,255,.02);flex:0 0 auto;}',
    '.ma-brand{display:flex;align-items:center;gap:8px;font-size:16px;font-weight:600;',
    'letter-spacing:.2px;white-space:nowrap;flex:0 0 auto;}',
    '.ma-brand .ma-mark{color:#f07a86;font-size:16px;}',
    '.ma-spacer{flex:1 1 auto;}',
    '.ma-field{display:flex;align-items:center;gap:6px;min-width:0;}',
    '.ma-input,.ma-select{background:rgba(255,255,255,.04);color:#e2e8f0;font:12px/1.4 inherit;',
    'border:1px solid rgba(255,255,255,.12);border-radius:9999px;padding:4px 10px;min-width:0;}',
    '.ma-input{width:150px;min-width:80px;}',
    '.ma-select{width:220px;max-width:260px;}',
    '.ma-input::placeholder{color:rgba(255,255,255,.42);}',
    '.ma-btn{background:rgba(255,255,255,.04);color:#e2e8f0;font:12px/1.4 inherit;cursor:pointer;',
    'border:1px solid rgba(255,255,255,.12);border-radius:9999px;padding:4px 11px;',
    'white-space:nowrap;flex:0 0 auto;transition:background .15s ease;}',
    '.ma-btn:hover{background:rgba(255,255,255,.09);}',
    '.ma-btn[disabled]{opacity:.42;cursor:default;}',
    '.ma-btn[aria-pressed="true"]{background:rgba(201,53,69,.22);border-color:#c93545;color:#f4c9cd;}',
    '.ma-btn-accent{border-color:rgba(240,122,134,.5);color:#f4c9cd;}',
    /* body */
    '.ma-body{flex:1 1 auto;display:flex;min-height:0;}',
    '.ma-rail{flex:0 0 200px;min-width:0;border-right:1px solid rgba(255,255,255,.08);',
    'overflow:auto;padding:10px 8px 16px;background:rgba(255,255,255,.015);}',
    '.ma-rail[hidden]{display:none;}',
    '.ma-rail h2{font-size:12px;font-weight:600;text-transform:none;color:rgba(255,255,255,.68);',
    'margin:12px 6px 6px;letter-spacing:.3px;}',
    '.ma-rail h2:first-child{margin-top:0;}',
    '.ma-navitem{display:flex;align-items:center;gap:7px;width:100%;text-align:left;background:none;',
    'border:0;border-radius:6px;color:#e2e8f0;font:13px/1.4 inherit;padding:5px 7px;cursor:pointer;}',
    '.ma-navitem:hover{background:rgba(255,255,255,.06);}',
    '.ma-navitem[aria-current="true"]{background:rgba(201,53,69,.18);color:#fff;}',
    '.ma-dot{flex:0 0 auto;width:7px;height:7px;border-radius:50%;}',
    '.ma-navlabel{overflow:hidden;text-overflow:ellipsis;white-space:nowrap;}',
    '.ma-stage{flex:1 1 auto;min-width:0;min-height:0;display:flex;flex-direction:column;}',
    /* narrow: the rail stacks above the reader as a disclosure strip, and the
     * toolbar wraps onto further rows instead of being clipped sideways */
    '#memory-atlas-root.ma-narrow .ma-body{flex-direction:column;}',
    '#memory-atlas-root.ma-narrow .ma-rail{flex:0 0 auto;width:100%;max-height:45%;',
    'border-right:0;border-bottom:1px solid rgba(255,255,255,.08);padding:8px;}',
    '#memory-atlas-root.ma-narrow .ma-disclosure summary{cursor:pointer;list-style:none;}',
    '#memory-atlas-root.ma-narrow .ma-disclosure summary::-webkit-details-marker{display:none;}',
    '#memory-atlas-root.ma-narrow .ma-disclosure summary::before{content:"▸";margin-right:2px;}',
    '#memory-atlas-root.ma-narrow .ma-disclosure[open] summary::before{content:"▾";}',
    '#memory-atlas-root.ma-narrow .ma-brand{font-size:15px;}',
    '#memory-atlas-root.ma-narrow .ma-spacer{flex:1 1 0;min-width:0;}',
    '#memory-atlas-root.ma-narrow .ma-input{width:auto;flex:1 1 90px;}',
    '#memory-atlas-root.ma-narrow .ma-field{flex:1 1 160px;}',
    '#memory-atlas-root.ma-narrow .ma-scroll{padding:12px 12px 16px;}',
    '#memory-atlas-root.ma-narrow .ma-strip,#memory-atlas-root.ma-narrow .ma-gaps{padding-left:12px;padding-right:12px;}',
    /* narrow: there is no room to keep the header on two lines, so let it wrap
     * rather than clip the document title or the completeness meta away */
    '#memory-atlas-root.ma-narrow .ma-strip{overflow:visible;}',
    '#memory-atlas-root.ma-narrow .ma-strip-lead,',
    '#memory-atlas-root.ma-narrow .ma-strip-controls{flex-wrap:wrap;}',
    '#memory-atlas-root.ma-narrow .ma-crumb{flex-wrap:wrap;overflow:visible;}',
    '#memory-atlas-root.ma-narrow .ma-gaps{max-height:56px;}',
    /* atlas */
    '.ma-scroll{flex:1 1 auto;min-height:0;overflow:auto;padding:12px 16px 16px;}',
    '.ma-h1{font-size:24px;line-height:1.25;font-weight:600;margin:0 0 4px;}',
    '.ma-sub{margin:0 0 6px;color:rgba(255,255,255,.82);font-size:14px;max-width:78ch;}',
    '.ma-group{margin:18px 0 0;}',
    '.ma-group-head{display:flex;align-items:baseline;gap:8px;}',
    '.ma-group-title{font-size:16px;font-weight:600;margin:0;}',
    '.ma-group-desc{margin:2px 0 8px;font-size:12px;color:rgba(255,255,255,.68);max-width:78ch;}',
    '.ma-rows{display:flex;flex-direction:column;gap:6px;}',
    '.ma-row{display:flex;align-items:center;gap:10px;width:100%;text-align:left;',
    'background:rgba(255,255,255,.03);border:1px solid rgba(255,255,255,.10);',
    'border-left:3px solid var(--ma-hue,#8b8b93);border-radius:8px;padding:8px 11px;',
    'color:#e2e8f0;font:13px/1.4 inherit;cursor:pointer;transition:background .15s ease;}',
    '.ma-row:hover{background:rgba(255,255,255,.07);}',
    '.ma-row[aria-current="true"]{background:rgba(255,255,255,.09);border-color:rgba(255,255,255,.34);}',
    '.ma-row-title{flex:1 1 auto;min-width:0;overflow:hidden;text-overflow:ellipsis;',
    'white-space:nowrap;font-weight:600;}',
    '.ma-row-when{flex:0 0 auto;}',
    '.ma-disclose{margin:20px 0 0;border:1px solid rgba(255,255,255,.10);border-radius:8px;',
    'background:rgba(255,255,255,.02);padding:8px 12px;}',
    '.ma-disclose summary{cursor:pointer;font-size:14px;font-weight:600;}',
    '.ma-disclose ul{margin:8px 0 0;padding-left:20px;font-size:13px;}',
    '.ma-disclose li{margin:3px 0;}',
    '.ma-legend{margin:12px 0;border:1px solid rgba(255,255,255,.10);border-radius:8px;',
    'background:rgba(255,255,255,.02);padding:8px 12px;font-size:12px;}',
    '.ma-legend h3{font-size:12px;font-weight:600;margin:0 0 5px;color:rgba(255,255,255,.82);}',
    '.ma-legend dl{margin:0;display:grid;grid-template-columns:auto 1fr;gap:2px 10px;}',
    '.ma-legend dt{font-weight:600;white-space:nowrap;}',
    '.ma-legend dd{margin:0;color:rgba(255,255,255,.68);}',
    '.ma-crumb{display:flex;align-items:center;gap:6px;flex-wrap:nowrap;font-size:12px;',
    'color:rgba(255,255,255,.68);margin:0;flex:1 1 auto;min-width:0;overflow:hidden;}',
    '.ma-crumb b{font-weight:600;color:#e2e8f0;}',
    '.ma-crumb>*{flex:0 0 auto;}',
    '.ma-crumb .ma-title{flex:0 1 auto;color:#fff;}',
    '.ma-crumb-sep{color:rgba(255,255,255,.42);}',
    '.ma-crumb-family{display:inline-flex;align-items:center;gap:5px;white-space:nowrap;cursor:help;}',
    '.ma-linkbtn{background:none;border:0;padding:0;color:#f4c9cd;font:inherit;cursor:pointer;text-align:left;}',
    '.ma-linkbtn:hover{text-decoration:underline;}',
    /* dialogue chronicle blocks */
    '.ma-block{border:1px solid rgba(255,255,255,.10);border-radius:8px;padding:10px 12px;',
    'margin:0 0 10px;background:rgba(255,255,255,.02);border-left:3px solid #9d9ed6;}',
    '.ma-block-summary{border-left-color:#9d9ed6;}',
    '.ma-block-era{border-left-color:#c3b27a;border-style:solid;background:rgba(201,178,122,.07);}',
    '.ma-block-gap{border-left-color:#7fb8bf;border-left-style:dashed;background:rgba(127,184,191,.07);}',
    '.ma-block-unknown{border-left-color:#8b8b93;background:rgba(255,255,255,.04);}',
    '.ma-block-head{display:flex;align-items:center;gap:8px;flex-wrap:wrap;}',
    '.ma-kind{font-size:12px;font-weight:600;border:1px solid rgba(255,255,255,.14);',
    'border-radius:999px;padding:1px 9px;background:rgba(255,255,255,.04);}',
    '.ma-block-note{margin:4px 0 6px;font-size:12px;color:rgba(255,255,255,.68);}',
    '.ma-block-facts{font-size:12px;color:rgba(255,255,255,.68);margin:0 0 6px;}',
    /* reader — the header is exactly two ultra-compact rows so that the text of
     * the document, not the chrome around it, owns the height of the widget */
    '.ma-strip{flex:0 0 auto;padding:5px 16px 6px;border-bottom:1px solid rgba(255,255,255,.08);',
    'background:rgba(255,255,255,.02);display:flex;flex-direction:column;gap:5px;overflow:hidden;}',
    '.ma-strip-row{display:flex;align-items:center;gap:8px;flex-wrap:wrap;}',
    '.ma-strip-lead{flex-wrap:nowrap;gap:9px;min-width:0;}',
    '.ma-strip-controls{flex-wrap:nowrap;gap:8px;min-width:0;}',
    '.ma-strip-meta{display:flex;align-items:center;gap:6px;flex:0 1 auto;min-width:0;',
    'overflow:hidden;}',
    /* every fact keeps its own line: the row may be clipped, never re-wrapped,
     * because a wrapped fact would silently turn the header into three rows */
    '.ma-strip-meta>*{flex:0 0 auto;white-space:nowrap;}',
    '.ma-title{font-size:14px;font-weight:600;margin:0;min-width:0;',
    'overflow:hidden;text-overflow:ellipsis;white-space:nowrap;}',
    /* segmented pill controls, the shared Ouroboros control shape */
    '.ma-seg{display:inline-flex;align-items:center;gap:2px;flex:0 0 auto;padding:2px;',
    'border:1px solid rgba(255,255,255,.10);border-radius:9999px;background:rgba(255,255,255,.03);}',
    '.ma-seg-btn{background:none;border:1px solid transparent;border-radius:9999px;',
    'color:rgba(255,255,255,.68);font:12px/1.4 inherit;padding:2px 11px;cursor:pointer;',
    'white-space:nowrap;transition:background .15s ease,color .15s ease;}',
    '.ma-seg-btn:hover{background:rgba(255,255,255,.06);color:#e2e8f0;}',
    '.ma-seg-btn[aria-selected="true"],.ma-seg-btn[aria-pressed="true"]{',
    'background:rgba(201,53,69,.22);border-color:rgba(240,122,134,.42);color:#fff;}',
    '.ma-tabs{display:inline-flex;}',
    '.ma-loadstate{flex:0 1 auto;min-width:0;overflow:hidden;text-overflow:ellipsis;',
    'white-space:nowrap;}',
    '.ma-back{padding:3px 10px;}',
    '.ma-chip{display:inline-flex;align-items:center;gap:5px;font-size:12px;color:rgba(255,255,255,.68);',
    'border:1px solid rgba(255,255,255,.10);border-radius:999px;padding:2px 9px;background:rgba(255,255,255,.02);}',
    '.ma-chip-warn{border-color:rgba(240,122,134,.42);color:#f4c9cd;}',
    '.ma-doc{max-width:68ch;}',
    '.ma-src{max-width:none;}',
    '.ma-notice{border:1px solid rgba(255,255,255,.10);border-left:3px solid #c93545;border-radius:7px;',
    'padding:9px 12px;margin:0 0 12px;font-size:13px;background:rgba(201,53,69,.10);}',
    '.ma-empty{color:rgba(255,255,255,.68);font-size:13px;padding:10px 0;}',
    /* Gaps footer. One compact line: it is a disclosure, not a second panel, and
     * it must never crowd the document above it. Anything past the first line
     * scrolls within the strip instead of stealing height from the reader. */
    '.ma-gaps{flex:0 0 auto;max-height:30px;overflow-y:auto;overflow-x:hidden;',
    'border-top:1px solid rgba(255,255,255,.08);padding:4px 16px;',
    'display:flex;align-items:center;gap:6px;flex-wrap:wrap;background:rgba(255,255,255,.02);font-size:12px;}',
    '.ma-gaps .ma-chip{padding:0 8px;}',
    '.ma-gaps[hidden]{display:none;}',
    /* history */
    '.ma-ev{display:flex;gap:9px;align-items:flex-start;padding:7px 0;border-bottom:1px solid rgba(255,255,255,.07);}',
    '.ma-ev-main{flex:1 1 auto;min-width:0;}',
    '.ma-ev-sum{font-size:13px;}',
    '.ma-fields{margin:4px 0 0;font-size:12px;color:rgba(255,255,255,.68);}',
    '.ma-fields div{overflow:hidden;text-overflow:ellipsis;}',
    '.ma-diff{font:12px/1.5 ui-monospace,SFMono-Regular,Menlo,monospace;white-space:pre-wrap;',
    'word-break:break-word;border:1px solid rgba(255,255,255,.10);border-radius:7px;overflow:hidden;}',
    '.ma-diff div{padding:1px 8px;}',
    '.ma-diff .ma-add{background:rgba(143,191,159,.14);}',
    '.ma-diff .ma-del{background:rgba(201,53,69,.16);}',
    '.ma-diff .ma-ctx{color:rgba(255,255,255,.68);}',
    /* graph */
    '.ma-graph{border:1px solid rgba(255,255,255,.08);border-radius:8px;background:rgba(255,255,255,.02);}',
    '.ma-graph text{font:12px ui-sans-serif,sans-serif;fill:#e2e8f0;}',
    '.ma-graph .ma-edge{stroke:rgba(255,255,255,.42);stroke-width:1;}',
    '.ma-graph .ma-edge-dotted{stroke-dasharray:2 4;stroke:rgba(255,255,255,.55);}',
    '.ma-structure{margin:16px 0 0;border-top:1px solid rgba(255,255,255,.08);padding-top:10px;}',
    '.ma-structure h2{font-size:14px;font-weight:600;margin:0 0 2px;}',
    /* markdown */
    '.md h1,.md h2,.md h3,.md h4,.md h5,.md h6{font-weight:600;line-height:1.3;margin:20px 0 8px;}',
    '.md h1{font-size:24px;} .md h2{font-size:16px;} .md h3{font-size:14px;}',
    '.md h4,.md h5,.md h6{font-size:14px;color:rgba(255,255,255,.82);}',
    '.md>:first-child{margin-top:0;}',
    '.md p{margin:0 0 12px;}',
    '.md ul,.md ol{margin:0 0 12px;padding-left:22px;}',
    '.md li{margin:2px 0;}',
    '.md li.md-task{list-style:none;margin-left:-18px;display:flex;gap:7px;align-items:flex-start;}',
    '.md li.md-task input{margin-top:4px;}',
    '.md blockquote{margin:0 0 12px;padding:2px 0 2px 12px;border-left:2px solid rgba(255,255,255,.16);',
    'color:rgba(255,255,255,.82);}',
    '.md code{font:12px/1.5 ui-monospace,SFMono-Regular,Menlo,monospace;background:rgba(255,255,255,.06);',
    'border-radius:4px;padding:1px 5px;}',
    '.md pre{margin:0 0 12px;padding:10px 12px;background:rgba(255,255,255,.04);',
    'border:1px solid rgba(255,255,255,.08);border-radius:7px;overflow:auto;}',
    '.md pre code{background:none;padding:0;display:block;white-space:pre;}',
    '.md .md-lang{display:block;font-size:12px;color:rgba(255,255,255,.68);margin-bottom:5px;}',
    '.md table{border-collapse:collapse;margin:0 0 12px;font-size:13px;display:block;overflow:auto;max-width:100%;}',
    '.md th,.md td{border:1px solid rgba(255,255,255,.12);padding:4px 9px;}',
    '.md th{background:rgba(255,255,255,.04);font-weight:600;}',
    '.md hr{border:0;border-top:1px solid rgba(255,255,255,.12);margin:16px 0;}',
    '.md a{color:#f4c9cd;text-decoration:underline;text-underline-offset:2px;}',
    '.md .md-unresolved{color:rgba(255,255,255,.68);border-bottom:1px dotted rgba(255,255,255,.3);cursor:help;}',
    '.md .md-image{color:rgba(255,255,255,.68);font-style:italic;}',
    '.md .md-xref{background:none;border:0;padding:0;color:#f4c9cd;font:inherit;cursor:pointer;',
    'text-decoration:underline;text-underline-offset:2px;}',
    '.md mark{background:rgba(240,122,134,.32);color:#fff;border-radius:2px;}',
    '.ma-srclines{font:12px/1.55 ui-monospace,SFMono-Regular,Menlo,monospace;white-space:pre-wrap;',
    'word-break:break-word;margin:0;}',
    '.ma-srclines .ma-ln{display:flex;gap:12px;}',
    '.ma-srclines .ma-no{flex:0 0 44px;text-align:right;color:rgba(255,255,255,.42);user-select:none;}',
    '.ma-srclines .ma-hit{background:rgba(240,122,134,.18);}',
    '@media (prefers-reduced-motion: reduce){#memory-atlas-root *{transition:none !important;animation:none !important;}}'
  ].join('');

  /* ---------- tiny DOM helpers (no innerHTML anywhere) ---------- */

  function el(tag, cls, text) {
    var node = document.createElement(tag);
    if (cls) { node.className = cls; }
    if (text !== undefined && text !== null) { node.appendChild(document.createTextNode(String(text))); }
    return node;
  }
  function clear(node) { while (node.firstChild) { node.removeChild(node.firstChild); } }

  /* Listener bookkeeping is scoped to the subtree that owns the node. Every
   * render throws away a subtree, so the handlers registered for it are removed
   * and forgotten at the same moment; otherwise one long-lived array would keep
   * every detached node of every past render alive. */
  var listenerScopes = { shell: [], rail: [], stage: [] };
  var sink = listenerScopes.shell;

  function on(target, type, fn, opts) {
    target.addEventListener(type, fn, opts);
    sink.push([target, type, fn, opts]);
    return fn;
  }
  function detachScope(name) {
    listenerScopes[name].splice(0).forEach(function (entry) {
      try { entry[0].removeEventListener(entry[1], entry[2], entry[3]); }
      catch (err) { /* node already gone */ }
    });
  }
  function scoped(name, build) {
    detachScope(name);
    var previous = sink;
    sink = listenerScopes[name];
    try { build(); } finally { sink = previous; }
  }

  /* ---------- API client ---------- */

  var pending = [];

  function AtlasError(message, code, status, revision) {
    this.name = 'AtlasError';
    this.message = message || 'request failed';
    this.code = code || 'request_failed';
    this.status = status || 0;
    this.revision = revision || null;
  }
  AtlasError.prototype = Object.create(Error.prototype);

  function query(params) {
    var parts = [];
    Object.keys(params).forEach(function (key) {
      var value = params[key];
      if (value === undefined || value === null || value === '') { return; }
      parts.push(encodeURIComponent(key) + '=' + encodeURIComponent(String(value)));
    });
    return parts.length ? '?' + parts.join('&') : '';
  }

  function request(path, params) {
    var url = PREFIX + path + query(params || {});
    var controller = typeof AbortController === 'function' ? new AbortController() : null;
    if (controller) { pending.push(controller); }
    var bridge = window.OuroborosWidget && typeof window.OuroborosWidget.fetch === 'function'
      ? window.OuroborosWidget.fetch.bind(window.OuroborosWidget)
      : null;
    if (!bridge) {
      return Promise.reject(new AtlasError(
        'The widget bridge OuroborosWidget.fetch is unavailable.', 'no_bridge', 0));
    }
    var init = { method: 'GET', timeoutMs: REQUEST_TIMEOUT_MS };
    if (controller) { init.signal = controller.signal; }
    return bridge(url, init).then(function (response) {
      return response.text().then(function (raw) {
        var body = null;
        try { body = JSON.parse(raw); } catch (err) { body = null; }
        if (!body || typeof body !== 'object') {
          throw new AtlasError('The response was not valid JSON.', 'bad_response', response.status);
        }
        if (body.ok !== true) {
          var error = body.error || {};
          throw new AtlasError(error.message || 'request failed',
            error.code || 'request_failed', response.status, error.revision || null);
        }
        return { data: body.data, gaps: Array.isArray(body.gaps) ? body.gaps : [] };
      });
    }).then(function (result) {
      drop(controller);
      return result;
    }, function (err) {
      drop(controller);
      if (err && err.name === 'AbortError') { throw err; }
      if (err instanceof AtlasError) { throw err; }
      throw new AtlasError(err && err.message ? String(err.message) : 'network error',
        'network_error', 0);
    });
  }

  function drop(controller) {
    var index = pending.indexOf(controller);
    if (index >= 0) { pending.splice(index, 1); }
  }

  /* ---------- Markdown -> DOM ----------
   * Block + inline subset of CommonMark/GFM: ATX headings, fenced and indented
   * code, blockquotes, thematic breaks, ordered/unordered/nested/task lists,
   * pipe tables with alignment, paragraphs with hard breaks; inline code,
   * strong, emphasis, strikethrough, escapes, autolinks, links and images.
   * Raw HTML in memory text is rendered as literal characters, never parsed.
   */

  var SAFE_SCHEME = /^(https?|mailto):/i;
  var ESCAPABLE = '\\`*_{}[]()#+-.!>~|"\'';
  var RE_ITEM = /^(\s*)([-*+]|\d{1,9}[.)])(\s+)(.*)$/;
  var RE_FENCE = /^(\s{0,3})(`{3,}|~{3,})\s*([^`]*)$/;
  var RE_HEADING = /^\s{0,3}(#{1,6})\s+(.*?)\s*#*\s*$/;
  var RE_HR = /^\s{0,3}([-*_])(?:\s*\1){2,}\s*$/;
  var RE_ALIGN = /^\s{0,3}\|?\s*:?-{1,}:?\s*(\|\s*:?-{1,}:?\s*)*\|?\s*$/;

  function renderMarkdown(text, options) {
    var frag = document.createDocumentFragment();
    var lines = String(text === undefined || text === null ? '' : text)
      .replace(/\r\n?/g, '\n').replace(/\t/g, '    ').split('\n');
    renderBlocks(lines, frag, options || {});
    return frag;
  }

  function renderBlocks(lines, parent, opts) {
    var i = 0;
    while (i < lines.length) {
      var line = lines[i];
      if (!line.trim()) { i++; continue; }

      var fence = RE_FENCE.exec(line);
      if (fence) { i = blockFence(lines, i, fence, parent); continue; }
      var heading = RE_HEADING.exec(line);
      if (heading) {
        var h = el('h' + heading[1].length);
        renderInline(heading[2], h, opts);
        parent.appendChild(h);
        i++; continue;
      }
      if (RE_HR.test(line)) { parent.appendChild(el('hr')); i++; continue; }
      if (/^\s{0,3}>/.test(line)) { i = blockQuote(lines, i, parent, opts); continue; }
      if (RE_ITEM.test(line)) { i = blockList(lines, i, parent, opts); continue; }
      if (line.indexOf('|') >= 0 && i + 1 < lines.length && RE_ALIGN.test(lines[i + 1])
        && lines[i + 1].indexOf('|') >= 0) {
        i = blockTable(lines, i, parent, opts); continue;
      }
      if (/^ {4,}\S/.test(line)) { i = blockIndentedCode(lines, i, parent); continue; }
      i = blockParagraph(lines, i, parent, opts);
    }
  }

  function blockFence(lines, start, fence, parent) {
    var marker = fence[2].charAt(0);
    var width = fence[2].length;
    var lang = fence[3].trim().split(/\s+/)[0] || '';
    var body = [];
    var i = start + 1;
    var closed = false;
    for (; i < lines.length; i++) {
      var end = new RegExp('^\\s{0,3}' + (marker === '`' ? '`' : '~') + '{' + width + ',}\\s*$');
      if (end.test(lines[i])) { closed = true; i++; break; }
      body.push(lines[i]);
    }
    var pre = el('pre');
    if (lang) { pre.appendChild(el('span', 'md-lang', lang)); }
    var code = el('code', null, body.join('\n'));
    if (lang) { code.setAttribute('data-language', lang); }
    pre.appendChild(code);
    if (!closed) { pre.appendChild(el('span', 'md-lang', 'Unclosed code fence in source.')); }
    parent.appendChild(pre);
    return i;
  }

  function blockIndentedCode(lines, start, parent) {
    var body = [];
    var i = start;
    for (; i < lines.length; i++) {
      if (/^ {4,}/.test(lines[i])) { body.push(lines[i].slice(4)); }
      else if (!lines[i].trim()) { body.push(''); }
      else { break; }
    }
    while (body.length && !body[body.length - 1].trim()) { body.pop(); }
    var pre = el('pre');
    pre.appendChild(el('code', null, body.join('\n')));
    parent.appendChild(pre);
    return i;
  }

  function blockQuote(lines, start, parent, opts) {
    var inner = [];
    var i = start;
    for (; i < lines.length; i++) {
      if (/^\s{0,3}>/.test(lines[i])) { inner.push(lines[i].replace(/^\s{0,3}>\s?/, '')); }
      else if (lines[i].trim() && inner.length) { inner.push(lines[i]); }
      else { break; }
    }
    var quote = el('blockquote');
    renderBlocks(inner, quote, opts);
    parent.appendChild(quote);
    return i;
  }

  function blockParagraph(lines, start, parent, opts) {
    var i = start;
    var buffer = [];
    for (; i < lines.length; i++) {
      var line = lines[i];
      if (!line.trim() || RE_HEADING.test(line) || RE_HR.test(line) || RE_FENCE.test(line)
        || RE_ITEM.test(line) || /^\s{0,3}>/.test(line)) { break; }
      buffer.push(line);
    }
    if (!buffer.length) { return start + 1; }
    var p = el('p');
    buffer.forEach(function (line, index) {
      renderInline(line.replace(/\s+$/, ''), p, opts);
      if (index < buffer.length - 1) {
        if (/(\s{2}|\\)$/.test(line)) { p.appendChild(el('br')); }
        else { p.appendChild(document.createTextNode(' ')); }
      }
    });
    parent.appendChild(p);
    return i;
  }

  function splitRow(line) {
    var trimmed = line.trim().replace(/^\|/, '').replace(/\|$/, '');
    var cells = [];
    var buffer = '';
    for (var i = 0; i < trimmed.length; i++) {
      var ch = trimmed.charAt(i);
      if (ch === '\\' && i + 1 < trimmed.length) { buffer += ch + trimmed.charAt(i + 1); i++; continue; }
      if (ch === '|') { cells.push(buffer); buffer = ''; continue; }
      buffer += ch;
    }
    cells.push(buffer);
    return cells.map(function (cell) { return cell.trim(); });
  }

  function blockTable(lines, start, parent, opts) {
    var header = splitRow(lines[start]);
    var aligns = splitRow(lines[start + 1]).map(function (spec) {
      var left = spec.charAt(0) === ':';
      var right = spec.charAt(spec.length - 1) === ':';
      if (left && right) { return 'center'; }
      if (right) { return 'right'; }
      if (left) { return 'left'; }
      return '';
    });
    var table = el('table');
    var thead = el('thead');
    var hrow = el('tr');
    header.forEach(function (cell, index) {
      var th = el('th');
      if (aligns[index]) { th.style.textAlign = aligns[index]; }
      renderInline(cell, th, opts);
      hrow.appendChild(th);
    });
    thead.appendChild(hrow);
    table.appendChild(thead);
    var tbody = el('tbody');
    var i = start + 2;
    for (; i < lines.length; i++) {
      if (!lines[i].trim() || lines[i].indexOf('|') < 0) { break; }
      var cells = splitRow(lines[i]);
      var row = el('tr');
      for (var c = 0; c < header.length; c++) {
        var td = el('td');
        if (aligns[c]) { td.style.textAlign = aligns[c]; }
        renderInline(cells[c] === undefined ? '' : cells[c], td, opts);
        row.appendChild(td);
      }
      tbody.appendChild(row);
    }
    table.appendChild(tbody);
    parent.appendChild(table);
    return i;
  }

  function blockList(lines, start, parent, opts) {
    var first = RE_ITEM.exec(lines[start]);
    var baseIndent = first[1].length;
    var ordered = /\d/.test(first[2]);
    var list = el(ordered ? 'ol' : 'ul');
    if (ordered) {
      var startNumber = parseInt(first[2], 10);
      if (startNumber !== 1 && !isNaN(startNumber)) { list.setAttribute('start', String(startNumber)); }
    }
    var i = start;
    var current = null;
    var blanks = 0;
    while (i < lines.length) {
      var line = lines[i];
      if (!line.trim()) {
        blanks++;
        if (blanks > 1) { break; }
        if (current) { current.push(''); }
        i++;
        continue;
      }
      var item = RE_ITEM.exec(line);
      var indent = line.length - line.replace(/^\s*/, '').length;
      if (item && item[1].length <= baseIndent + 1) {
        if (/\d/.test(item[2]) !== ordered && item[1].length === baseIndent) { break; }
        current = [item[4]];
        list.appendChild(makeItem(current, opts));
        list.lastChild.__raw = current;
        blanks = 0;
        i++;
        continue;
      }
      if (!current) { break; }
      if (indent <= baseIndent && !item) { break; }
      current.push(line.slice(Math.min(indent, baseIndent + first[2].length + first[3].length)));
      blanks = 0;
      i++;
    }
    // second pass: re-render each item body now that continuations are collected
    var kids = Array.prototype.slice.call(list.childNodes);
    kids.forEach(function (li) { fillItem(li, li.__raw, opts); delete li.__raw; });
    parent.appendChild(list);
    return i;
  }

  function makeItem() { return el('li'); }

  function fillItem(li, raw, opts) {
    var body = (raw || []).slice();
    while (body.length && !body[body.length - 1].trim()) { body.pop(); }
    var task = /^\[( |x|X)\]\s+/.exec(body[0] || '');
    if (task) {
      li.className = 'md-task';
      var box = el('input');
      box.type = 'checkbox';
      box.disabled = true;
      box.checked = task[1] !== ' ';
      box.setAttribute('aria-label', box.checked ? 'Done' : 'Not done');
      li.appendChild(box);
      body[0] = body[0].slice(task[0].length);
      var label = el('span');
      renderItemBody(body, label, opts);
      li.appendChild(label);
      return;
    }
    renderItemBody(body, li, opts);
  }

  function renderItemBody(body, target, opts) {
    var simple = body.length === 1
      || body.every(function (line, index) { return index === 0 || !line.trim(); });
    if (simple) { renderInline((body[0] || '').replace(/\s+$/, ''), target, opts); return; }
    var frag = document.createDocumentFragment();
    renderBlocks(body, frag, opts);
    if (frag.childNodes.length === 1 && frag.firstChild.nodeName === 'P') {
      while (frag.firstChild.firstChild) { target.appendChild(frag.firstChild.firstChild); }
      return;
    }
    target.appendChild(frag);
  }

  function matchLink(text) {
    if (text.charAt(0) !== '[') { return null; }
    var depth = 0;
    var i = 0;
    var label = null;
    for (; i < text.length; i++) {
      var ch = text.charAt(i);
      if (ch === '\\') { i++; continue; }
      if (ch === '[') { depth++; }
      else if (ch === ']') { depth--; if (!depth) { label = text.slice(1, i); i++; break; } }
    }
    if (label === null || text.charAt(i) !== '(') { return null; }
    var start = i + 1;
    depth = 1;
    for (i = start; i < text.length; i++) {
      var c = text.charAt(i);
      if (c === '\\') { i++; continue; }
      if (c === '(') { depth++; }
      else if (c === ')') { depth--; if (!depth) { break; } }
    }
    if (depth) { return null; }
    var inner = text.slice(start, i).trim();
    var dest = inner;
    var titled = /^(\S*)\s+["'(]/.exec(inner);
    if (titled) { dest = titled[1]; }
    dest = dest.replace(/^<(.*)>$/, '$1');
    return { label: label, dest: dest, len: i + 1 };
  }

  /* A wiki link is the second authored link form the backend turns into an
   * edge, so the reader has to understand it too or the two tabs disagree about
   * the same document. `[[target]]`, `[[target|label]]` and `[[target#part]]`
   * are the written shapes; the target is resolved through the wiki resolver,
   * which adds only the bare-name `.md` convention. */
  var WIKI_LINK = /^\[\[([^\]|#]+)(?:([|#])([^\]]*))?\]\]/;

  function wikiNode(match, opts) {
    var target = match[1].trim();
    var label = match[2] === '|' && match[3] && match[3].trim()
      ? match[3].trim() : match[1].trim();
    var id = opts && typeof opts.resolveWiki === 'function'
      ? opts.resolveWiki(target) : null;
    if (id) {
      var button = el('button', 'md-xref');
      button.type = 'button';
      button.setAttribute('data-source-id', id);
      button.title = 'Wiki link — open ' + id;
      button.appendChild(document.createTextNode(label));
      if (opts.onSource) { on(button, 'click', function () { opts.onSource(id); }); }
      return button;
    }
    var span = el('span', 'md-unresolved', label);
    span.title = 'Unresolved link: ' + target;
    return span;
  }

  function linkNode(label, dest, opts) {
    if (SAFE_SCHEME.test(dest)) {
      var a = el('a');
      a.href = dest;
      a.target = '_blank';
      a.rel = 'noopener noreferrer nofollow';
      renderInline(label, a, opts);
      return a;
    }
    if (opts && typeof opts.resolveSource === 'function') {
      var sourceId = opts.resolveSource(dest);
      if (sourceId) {
        var button = el('button', 'md-xref');
        button.type = 'button';
        button.setAttribute('data-source-id', sourceId);
        renderInline(label, button, opts);
        if (opts.onSource) { on(button, 'click', function () { opts.onSource(sourceId); }); }
        return button;
      }
    }
    var span = el('span', 'md-unresolved');
    span.title = 'Unresolved link: ' + dest;
    renderInline(label, span, opts);
    return span;
  }

  /* Locate the closing delimiter run for emphasis. `strict` applies the
   * underscore rule: a run may only close when it is not followed by a word
   * character, so text such as window.__name stays literal. */
  function findClose(rest, ch, run, strict) {
    var delimiter = new Array(run + 1).join(ch);
    var from = run;
    while (from < rest.length) {
      var index = rest.indexOf(delimiter, from);
      if (index < 0) { return -1; }
      var before = rest.charAt(index - 1);
      var after = rest.charAt(index + run);
      if (index <= run || before === ' ' || before === '\t' || before === ch
        || (run === 1 && after === ch)
        || (strict && after && /[A-Za-z0-9]/.test(after))) {
        from = index + 1;
        continue;
      }
      return index;
    }
    return -1;
  }

  function renderInline(text, parent, opts) {
    opts = opts || {};
    var source = String(text === undefined || text === null ? '' : text);
    var i = 0;
    var buffer = '';
    function flush() {
      if (buffer) { parent.appendChild(document.createTextNode(buffer)); buffer = ''; }
    }
    while (i < source.length) {
      var rest = source.slice(i);
      var ch = source.charAt(i);
      if (ch === '\\' && ESCAPABLE.indexOf(source.charAt(i + 1)) >= 0) {
        buffer += source.charAt(i + 1); i += 2; continue;
      }
      if (ch === '`') {
        var code = /^(`+)([\s\S]*?[^`])\1(?!`)/.exec(rest) || /^(`+)([^`]*)\1(?!`)/.exec(rest);
        if (code) {
          flush();
          parent.appendChild(el('code', null, code[2].replace(/^ (.*) $/, '$1')));
          i += code[0].length; continue;
        }
      }
      if (ch === '<') {
        var auto = /^<((?:https?|mailto):[^>\s]+)>/i.exec(rest);
        if (auto) { flush(); parent.appendChild(linkNode(auto[1], auto[1], opts)); i += auto[0].length; continue; }
      }
      if (ch === '!' && source.charAt(i + 1) === '[') {
        var image = matchLink(rest.slice(1));
        if (image) {
          flush();
          parent.appendChild(el('span', 'md-image', '[image: ' + (image.label || image.dest) + ']'));
          i += 1 + image.len; continue;
        }
      }
      if (ch === '[' && source.charAt(i + 1) === '[') {
        var wiki = WIKI_LINK.exec(rest);
        if (wiki) {
          flush();
          parent.appendChild(wikiNode(wiki, opts));
          i += wiki[0].length; continue;
        }
      }
      if (ch === '[') {
        var link = matchLink(rest);
        if (link) { flush(); parent.appendChild(linkNode(link.label, link.dest, opts)); i += link.len; continue; }
      }
      if (ch === '~' && source.charAt(i + 1) === '~') {
        var strike = /^~~([\s\S]+?)~~/.exec(rest);
        if (strike) {
          flush();
          var del = el('del'); renderInline(strike[1], del, opts); parent.appendChild(del);
          i += strike[0].length; continue;
        }
      }
      if (ch === '*' || ch === '_') {
        var strict = ch === '_';   // underscores never emphasise inside a word
        var previous = i > 0 ? source.charAt(i - 1) : '';
        if (!(strict && /[A-Za-z0-9]/.test(previous))) {
          var run = source.charAt(i + 1) === ch ? 2 : 1;
          var opener = source.charAt(i + run);
          if (opener && !/\s/.test(opener) && opener !== ch) {
            var close = findClose(rest, ch, run, strict);
            if (close > 0) {
              flush();
              var node = el(run === 2 ? 'strong' : 'em');
              renderInline(rest.slice(run, close), node, opts);
              parent.appendChild(node);
              i += close + run;
              continue;
            }
          }
        }
      }
      buffer += ch;
      i++;
    }
    flush();
  }

  /* ---------- state ---------- */

  var state = {
    ready: false,
    fatal: null,
    catalog: [],
    catalogGaps: [],
    catalogTruncated: false,
    // Prototype-less: these are keyed by authored text, so an inherited name
    // like `constructor` or `toString` must not answer a lookup. With a plain
    // object literal, `[note](constructor)` rendered as an active
    // cross-reference the backend never reported an edge for.
    byId: Object.create(null),
    // Root-relative catalogue path -> source id, so an authored relative link
    // resolves in the reader exactly as it does in the graph.
    byPath: Object.create(null),
    projects: [],
    // null means GLOBAL memory. A cold start stays here: only an explicit
    // choice in the scope switcher ever selects a project.
    scope: null,
    view: 'home',           // home | reader | search
    selectedId: null,
    tab: 'read',            // read | history | relations
    readerMode: 'rendered', // rendered | source
    focusLine: null,
    focusQuery: '',
    doc: null,
    dialogue: null,
    history: null,
    compare: null,
    graph: null,
    search: null,
    busy: 0
  };

  var ui = {};
  var timers = [];
  var scrollMemory = {};
  var disposed = false;

  function scrollKey() {
    return [state.view, state.selectedId || '-', state.tab, state.readerMode].join('|');
  }

  function setBusy(delta) {
    state.busy = Math.max(0, state.busy + delta);
    if (ui.refresh) { ui.refresh.disabled = state.busy > 0; }
  }

  function announce(message) {
    if (ui.status) { ui.status.textContent = message; }
  }

  function familyOf(key) {
    for (var i = 0; i < FAMILIES.length; i++) {
      if (FAMILIES[i].key === key) { return FAMILIES[i]; }
    }
    return OTHER_FAMILY;
  }

  function projectOf(item) {
    if (!item || typeof item.id !== 'string' || item.id.indexOf('project:') !== 0) { return null; }
    var parts = item.id.split(':');
    return parts.length > 1 && parts[1] ? parts[1] : null;
  }

  function inScope(item) {
    var project = projectOf(item);
    if (!project) { return true; }
    return project === state.scope;
  }

  function scopedItems() {
    return state.catalog.filter(inScope);
  }

  function formatTime(ns) {
    var value = Number(ns);
    if (!isFinite(value) || value <= 0) { return null; }
    var date = new Date(value / 1e6);
    if (isNaN(date.getTime())) { return null; }
    var pad = function (n) { return (n < 10 ? '0' : '') + n; };
    return date.getFullYear() + '-' + pad(date.getMonth() + 1) + '-' + pad(date.getDate())
      + ' ' + pad(date.getHours()) + ':' + pad(date.getMinutes());
  }

  function formatEventTime(ts) {
    if (ts === undefined || ts === null || ts === '') { return null; }
    var text = String(ts);
    var numeric = Number(text);
    var date;
    if (/^\d+(\.\d+)?$/.test(text)) {
      date = new Date(numeric > 1e12 ? numeric : numeric * 1000);
    } else {
      date = new Date(text);
    }
    if (isNaN(date.getTime())) { return text; }
    var pad = function (n) { return (n < 10 ? '0' : '') + n; };
    return date.getFullYear() + '-' + pad(date.getMonth() + 1) + '-' + pad(date.getDate())
      + ' ' + pad(date.getHours()) + ':' + pad(date.getMinutes());
  }

  function formatBytes(bytes) {
    if (bytes === null || bytes === undefined || bytes === '') { return 'unknown size'; }
    var value = Number(bytes);
    if (!isFinite(value) || value < 0) { return 'unknown size'; }
    if (value < 1024) { return value + ' B'; }
    if (value < 1024 * 1024) { return (value / 1024).toFixed(value < 10240 ? 1 : 0) + ' KiB'; }
    return (value / (1024 * 1024)).toFixed(1) + ' MiB';
  }

  /* Mirrors the catalogue `history` field: `none` means nothing was recorded for
   * this kind of source, while `unavailable` means the store this source would
   * use could not be read. They are different facts and must not both render as
   * "History unknown". */
  var HISTORY_LABEL = {
    none: 'History unavailable',
    unavailable: 'History could not be read',
    activity: 'Activity events only',
    snapshot: 'Full snapshots',
    mixed: 'Snapshots and activity events'
  };
  var HISTORY_MEANING = {
    none: 'nothing was recorded',
    unavailable: 'the store this source would use could not be opened',
    activity: 'we know it changed, not what it said',
    snapshot: 'the earlier text itself is stored',
    mixed: 'some earlier text is stored, the rest is only a record that it changed'
  };

  function historyMissing(item) {
    return item.history === 'none' || item.history === 'unavailable';
  }

  /* `compare_supported: false` means the backend keeps no earlier text for this
   * source at all, so a version diff would be a fiction. The affordance is not
   * shown, and the reason is stated in one sentence instead. */
  var NO_COMPARE_REASON = 'Version comparison is not offered for this document: it is '
    + 'written by merging, and only fingerprints of earlier versions are kept, so there '
    + 'is no earlier text to compare against.';

  function comparable(item) {
    return !item || item.compare_supported !== false;
  }

  /* One vocabulary for `representation`, used in the rows and in the legend. */
  var REPRESENTATION_LABEL = {
    snapshot: 'Full snapshot',
    digest_preview: 'Digest only',
    activity: 'Activity event'
  };
  var REPRESENTATION_MEANING = {
    snapshot: 'the earlier text itself is stored',
    digest_preview: 'just a fingerprint survives, the old text is gone',
    activity: 'we know it changed, not what it said'
  };

  function historyLegend() {
    var box = el('section', 'ma-legend');
    box.appendChild(el('h3', null, 'What the history labels mean'));
    var dl = el('dl');
    ['snapshot', 'digest_preview', 'activity'].forEach(function (key) {
      dl.appendChild(el('dt', null, REPRESENTATION_LABEL[key]));
      dl.appendChild(el('dd', null, REPRESENTATION_MEANING[key]));
    });
    dl.appendChild(el('dt', null, 'History unavailable'));
    dl.appendChild(el('dd', null, 'nothing was recorded'));
    dl.appendChild(el('dt', null, 'History could not be read'));
    dl.appendChild(el('dd', null,
      'a history store exists for this kind of source, but it could not be opened'));
    box.appendChild(dl);
    return box;
  }

  function scopeName() {
    return state.scope ? state.scope : 'Global memory';
  }

  function isDialogue(item) {
    return !!item && item.family === 'dialogue';
  }

  /* ---------- loading ---------- */

  /* Every asynchronous load carries an identity: the catalogue uses a
   * generation counter, and the per-source loads use the state object they were
   * started for. A response is applied only when that identity is still the
   * current one and the widget has not been disposed, so a slow reply for an
   * earlier request can never overwrite newer state — including when the same
   * source is opened, refreshed or re-queried a second time. */
  var catalogGeneration = 0;

  function stale(token) { return disposed || token !== catalogGeneration; }

  function loadCatalog() {
    var token = ++catalogGeneration;
    setBusy(1);
    announce('Loading the catalogue.');
    var items = [];
    var gaps = [];
    var truncated = false;

    function page(cursor, depth) {
      return request('/catalog', { cursor: cursor, limit: CATALOG_LIMIT })
        .then(function (result) {
          if (stale(token)) { return null; }
          items = items.concat(result.data && result.data.items ? result.data.items : []);
          gaps = gaps.concat(result.gaps);
          var next = result.data ? result.data.next_cursor : null;
          if (next && depth + 1 < CATALOG_MAX_PAGES) { return page(next, depth + 1); }
          if (next) { truncated = true; }
          return null;
        });
    }

    return page(null, 0).then(function () {
      if (stale(token)) { setBusy(-1); return false; }
      state.catalog = items;
      state.catalogGaps = gaps;
      state.catalogTruncated = truncated;
      state.byId = Object.create(null);
      state.byPath = Object.create(null);
      items.forEach(function (item) {
        state.byId[item.id] = item;
        if (typeof item.path === 'string' && item.path) { state.byPath[item.path] = item.id; }
      });
      var projects = [];
      items.forEach(function (item) {
        var project = projectOf(item);
        if (project && projects.indexOf(project) < 0) { projects.push(project); }
      });
      projects.sort();
      state.projects = projects;
      // Global (`scope === null`) is the resting state. A cold start must stay
      // there whatever the catalogue happens to contain, and a project that has
      // disappeared falls back to global rather than to some other project.
      if (state.scope !== null && projects.indexOf(state.scope) < 0) {
        state.scope = null;
        if (state.selectedId && !inScope(state.byId[state.selectedId])) {
          state.selectedId = null;
          state.view = 'home';
        }
      }
      state.ready = true;
      state.fatal = null;
      setBusy(-1);
      announce(items.length + ' sources catalogued.');
      renderAll();
      return true;
    }, function (err) {
      setBusy(-1);
      if (err && err.name === 'AbortError') { return false; }
      if (stale(token)) { return false; }
      state.ready = true;
      state.fatal = err;
      renderAll();
      return false;
    });
  }

  function selectSource(id, options) {
    options = options || {};
    if (!state.byId[id]) { return; }
    state.selectedId = id;
    state.view = 'reader';
    state.tab = options.tab || 'read';
    // The dialogue chronicle is stored as JSON but is *read* as blocks, so it
    // opens in the chronicle view rather than as raw source.
    state.readerMode = options.readerMode
      || (isDialogue(state.byId[id]) || state.byId[id].media_type === 'text/markdown'
        ? 'rendered' : 'source');
    state.focusLine = options.line || null;
    state.focusQuery = options.query || '';
    state.doc = null;
    state.dialogue = null;
    state.history = null;
    state.compare = null;
    state.graph = null;
    renderAll();
    loadTab();
  }

  function loadTab() {
    if (state.tab === 'read') { loadRead(); }
    else if (state.tab === 'history') { loadHistory(false); }
    else if (state.tab === 'relations') { loadGraph(); }
  }

  /* The dialogue chronicle is read from /dialogue as blocks; every other source
   * — and the dialogue's own "Source text" view — is read from /document. */
  function loadRead() {
    var item = state.byId[state.selectedId];
    if (isDialogue(item) && state.readerMode === 'rendered') { loadDialogue(false); }
    else { loadDocument(false); }
  }

  function loadDocument(more) {
    var id = state.selectedId;
    if (!id) { return; }
    if (!more) {
      state.doc = { id: id, revision: null, text: '', complete: false, cursor: null,
        bytes: 0, gaps: [], loading: true, error: null };
    } else {
      if (!state.doc || !state.doc.cursor || state.doc.loading) { return; }
      state.doc.loading = true;
    }
    var doc = state.doc;
    renderAll();
    setBusy(1);
    var params = { id: id, limit: DOC_LIMIT };
    if (more) { params.cursor = doc.cursor; params.revision = doc.revision; }
    request('/document', params).then(function (result) {
      setBusy(-1);
      if (disposed || state.doc !== doc || state.selectedId !== id) { return; }
      var data = result.data || {};
      doc.revision = data.revision || null;
      doc.text += typeof data.content === 'string' ? data.content : '';
      doc.bytes += Number(data.content_bytes) || 0;
      doc.complete = data.complete === true;
      doc.cursor = data.next_cursor || null;
      doc.gaps = more ? doc.gaps.concat(result.gaps) : result.gaps;
      doc.loading = false;
      announce(doc.complete ? 'Complete document loaded.'
        : 'Partial document loaded, ' + formatBytes(doc.bytes) + ' so far.');
      renderAll();
    }, function (err) {
      setBusy(-1);
      if (err && err.name === 'AbortError') { return; }
      if (disposed || state.doc !== doc || state.selectedId !== id) { return; }
      doc.loading = false;
      doc.error = err;
      renderAll();
    });
  }

  /* ---------- dialogue chronicle ---------- */

  /* Same guarded pattern as the document reader: the page is applied only when
   * the state object it was started for is still the current one. */
  function loadDialogue(more) {
    var id = state.selectedId;
    if (!id) { return; }
    if (!more) {
      state.dialogue = { id: id, revision: null, blocks: [], cursor: null, meta: null,
        gaps: [], loading: true, error: null };
    } else {
      if (!state.dialogue || !state.dialogue.cursor || state.dialogue.loading) { return; }
      state.dialogue.loading = true;
    }
    var chronicle = state.dialogue;
    renderAll();
    setBusy(1);
    var params = { limit: DIALOGUE_LIMIT };
    if (more) { params.cursor = chronicle.cursor; params.revision = chronicle.revision; }
    request('/dialogue', params).then(function (result) {
      setBusy(-1);
      if (disposed || state.dialogue !== chronicle || state.selectedId !== id) { return; }
      var data = result.data || {};
      chronicle.revision = data.revision || null;
      chronicle.blocks = chronicle.blocks.concat(Array.isArray(data.blocks) ? data.blocks : []);
      chronicle.cursor = data.next_cursor || null;
      chronicle.meta = data.meta && typeof data.meta === 'object' ? data.meta : null;
      chronicle.gaps = more ? chronicle.gaps.concat(result.gaps) : result.gaps;
      chronicle.loading = false;
      announce(chronicle.blocks.length + ' dialogue blocks loaded.');
      renderAll();
    }, function (err) {
      setBusy(-1);
      if (err && err.name === 'AbortError') { return; }
      if (disposed || state.dialogue !== chronicle || state.selectedId !== id) { return; }
      chronicle.loading = false;
      chronicle.error = err;
      renderAll();
    });
  }

  /* ---------- history and comparison ---------- */

  function loadHistory(more) {
    var id = state.selectedId;
    if (!id) { return; }
    // The catalogue already says whether a history store exists; asking anyway
    // would only produce an error the owner cannot act on.
    if (state.byId[id] && state.byId[id].history === 'none') { renderAll(); return; }
    if (!more) {
      state.history = { id: id, revision: null, items: [], cursor: null, gaps: [],
        loading: true, error: null, selected: [] };
    } else {
      if (!state.history || !state.history.cursor || state.history.loading) { return; }
      state.history.loading = true;
    }
    var history = state.history;
    renderAll();
    setBusy(1);
    var params = { id: id, limit: HISTORY_LIMIT };
    if (more) { params.cursor = history.cursor; params.revision = history.revision; }
    request('/history', params).then(function (result) {
      setBusy(-1);
      if (disposed || state.history !== history || state.selectedId !== id) { return; }
      var data = result.data || {};
      history.revision = data.revision || null;
      history.items = history.items.concat(Array.isArray(data.items) ? data.items : []);
      history.cursor = data.next_cursor || null;
      history.gaps = more ? history.gaps.concat(result.gaps) : result.gaps;
      history.loading = false;
      announce(history.items.length + ' history entries available.');
      renderAll();
    }, function (err) {
      setBusy(-1);
      if (err && err.name === 'AbortError') { return; }
      if (disposed || state.history !== history || state.selectedId !== id) { return; }
      history.loading = false;
      history.error = err;
      renderAll();
    });
  }

  function toggleCompare(eventId) {
    var chosen = state.history.selected;
    var index = chosen.indexOf(eventId);
    if (index >= 0) { chosen.splice(index, 1); }
    else {
      chosen.push(eventId);
      if (chosen.length > 2) { chosen.shift(); }
    }
    state.compare = null;
    renderAll();
  }

  /* The *first* page is bound to the history revision the listing was read at,
   * so the backend rejects a snapshot that no longer belongs to the events on
   * screen, and every later page carries the same revision. A served revision
   * that disagrees is reported as drift instead of being stitched into one
   * body from two different states of the store. */
  function fetchSnapshot(id, eventId, boundRevision) {
    var text = '';
    var revision = boundRevision || null;
    var gaps = [];
    function page(cursor, guard) {
      var params = { id: id, event_id: eventId, limit: DOC_LIMIT };
      if (revision) { params.revision = revision; }
      if (cursor) { params.cursor = cursor; }
      return request('/history/event', params).then(function (result) {
        var data = result.data || {};
        var served = data.revision || null;
        if (!revision) { revision = served; }
        else if (served && served !== revision) {
          throw new AtlasError(
            'The history store changed while this snapshot was being read.',
            'revision_drift', 409, served);
        }
        text += typeof data.content === 'string' ? data.content : '';
        gaps = gaps.concat(result.gaps);
        var complete = data.complete === true;
        if (data.next_cursor && guard < 40) { return page(data.next_cursor, guard + 1); }
        return { text: text, revision: revision, complete: complete,
          truncated: !!data.next_cursor, gaps: gaps };
      });
    }
    return page(null, 0);
  }

  function historyRevision() {
    return state.history && state.history.revision ? state.history.revision : null;
  }

  function openSnapshot(eventId) {
    var id = state.selectedId;
    var compare = { mode: 'single', loading: true, error: null, ids: [eventId], sides: [] };
    state.compare = compare;
    renderAll();
    setBusy(1);
    fetchSnapshot(id, eventId, historyRevision()).then(function (snapshot) {
      setBusy(-1);
      if (disposed || state.compare !== compare || state.selectedId !== id) { return; }
      compare.loading = false;
      compare.sides = [{ eventId: eventId, snapshot: snapshot }];
      renderAll();
    }, function (err) {
      setBusy(-1);
      if (err && err.name === 'AbortError') { return; }
      if (disposed || state.compare !== compare) { return; }
      compare.loading = false;
      compare.error = err;
      renderAll();
    });
  }

  function runCompare() {
    var id = state.selectedId;
    var chosen = state.history.selected.slice();
    if (chosen.length !== 2) { return; }
    // Always diff older -> newer, whatever order the history list is served in.
    var ordered = state.history.items
      .filter(function (item) { return chosen.indexOf(item.event_id) >= 0; })
      .sort(function (a, b) {
        var left = Date.parse(String(a.ts));
        var right = Date.parse(String(b.ts));
        if (isNaN(left) || isNaN(right)) { return 0; }
        return left - right;
      })
      .map(function (item) { return item.event_id; });
    var compare = { mode: 'diff', loading: true, error: null, ids: ordered, sides: [] };
    state.compare = compare;
    renderAll();
    setBusy(1);
    var bound = historyRevision();
    Promise.all(ordered.map(function (eventId) {
      return fetchSnapshot(id, eventId, bound).then(function (snapshot) {
        return { eventId: eventId, snapshot: snapshot };
      });
    })).then(function (sides) {
      setBusy(-1);
      if (disposed || state.compare !== compare || state.selectedId !== id) { return; }
      compare.loading = false;
      // Two snapshots are only comparable as versions of one thing when they
      // were read from the same state of the history store.
      if (sides.length === 2 && sides[0].snapshot.revision !== sides[1].snapshot.revision) {
        compare.error = new AtlasError(
          'The two snapshots were read from different states of the history store, so '
          + 'they cannot be compared as versions of the same source.',
          'revision_drift', 409, null);
        renderAll();
        return;
      }
      compare.sides = sides;
      announce('Comparison ready.');
      renderAll();
    }, function (err) {
      setBusy(-1);
      if (err && err.name === 'AbortError') { return; }
      if (disposed || state.compare !== compare) { return; }
      compare.loading = false;
      compare.error = err;
      renderAll();
    });
  }

  var DIFF_MAX_LINES = 1200;

  function diffLines(oldText, newText) {
    var a = String(oldText).replace(/\r\n?/g, '\n').split('\n');
    var b = String(newText).replace(/\r\n?/g, '\n').split('\n');
    var head = [];
    var tail = [];
    while (a.length && b.length && a[0] === b[0]) { head.push(a.shift()); b.shift(); }
    while (a.length && b.length && a[a.length - 1] === b[b.length - 1]) {
      tail.unshift(a.pop()); b.pop();
    }
    var rows = head.map(function (line) { return { op: ' ', text: line }; });
    if (a.length > DIFF_MAX_LINES || b.length > DIFF_MAX_LINES) {
      rows.push({ op: '!', text: 'Changed region is too large to align line by line ('
        + a.length + ' removed lines, ' + b.length + ' added lines); showing them in full.' });
      a.forEach(function (line) { rows.push({ op: '-', text: line }); });
      b.forEach(function (line) { rows.push({ op: '+', text: line }); });
    } else {
      var n = a.length;
      var m = b.length;
      var lcs = [];
      for (var i = 0; i <= n; i++) { lcs.push(new Int32Array(m + 1)); }
      for (i = n - 1; i >= 0; i--) {
        for (var j = m - 1; j >= 0; j--) {
          lcs[i][j] = a[i] === b[j] ? lcs[i + 1][j + 1] + 1
            : Math.max(lcs[i + 1][j], lcs[i][j + 1]);
        }
      }
      i = 0; j = 0;
      while (i < n && j < m) {
        if (a[i] === b[j]) { rows.push({ op: ' ', text: a[i] }); i++; j++; }
        else if (lcs[i + 1][j] >= lcs[i][j + 1]) { rows.push({ op: '-', text: a[i] }); i++; }
        else { rows.push({ op: '+', text: b[j] }); j++; }
      }
      while (i < n) { rows.push({ op: '-', text: a[i] }); i++; }
      while (j < m) { rows.push({ op: '+', text: b[j] }); j++; }
    }
    tail.forEach(function (line) { rows.push({ op: ' ', text: line }); });
    return rows;
  }

  /* ---------- relationships ---------- */

  function loadGraph() {
    var id = state.selectedId;
    if (!id) { return; }
    var graph = { id: id, nodes: [], edges: [], gaps: [], loading: true, error: null };
    state.graph = graph;
    renderAll();
    setBusy(1);
    // Only the documented parameters. There is no model, no guess and no
    // opt-in layer: the backend returns provable, authored references only.
    request('/graph', { focus: id, limit: GRAPH_LIMIT })
      .then(function (result) {
        setBusy(-1);
        if (disposed || state.graph !== graph || state.selectedId !== id) { return; }
        var data = result.data || {};
        graph.nodes = Array.isArray(data.nodes) ? data.nodes : [];
        graph.edges = Array.isArray(data.edges) ? data.edges : [];
        graph.revision = data.revision || null;
        graph.gaps = result.gaps;
        graph.loading = false;
        announce(graph.edges.length + ' links found.');
        renderAll();
      }, function (err) {
        setBusy(-1);
        if (err && err.name === 'AbortError') { return; }
        if (disposed || state.graph !== graph || state.selectedId !== id) { return; }
        graph.loading = false;
        graph.error = err;
        renderAll();
      });
  }

  /* ---------- search ---------- */

  function runSearch(more) {
    var text = ui.search ? ui.search.value.trim() : '';
    if (!more) {
      if (!text) {
        state.search = null;
        state.view = 'home';
        renderAll();
        return;
      }
      state.search = { query: text, items: [], cursor: null, revision: null, gaps: [],
        loading: true, error: null };
      state.view = 'search';
    } else {
      if (!state.search || !state.search.cursor || state.search.loading) { return; }
      state.search.loading = true;
    }
    var search = state.search;
    renderAll();
    setBusy(1);
    var params = { q: search.query, limit: SEARCH_LIMIT, case_sensitive: 'false' };
    if (more) { params.cursor = search.cursor; params.revision = search.revision; }
    request('/search', params).then(function (result) {
      setBusy(-1);
      if (disposed || state.search !== search) { return; }
      var data = result.data || {};
      search.revision = data.revision || null;
      search.items = search.items.concat(Array.isArray(data.items) ? data.items : []);
      search.cursor = data.next_cursor || null;
      search.gaps = more ? search.gaps.concat(result.gaps) : result.gaps;
      search.loading = false;
      announce(search.items.length + ' matches for ' + search.query + '.');
      renderAll();
    }, function (err) {
      setBusy(-1);
      if (err && err.name === 'AbortError') { return; }
      if (disposed || state.search !== search) { return; }
      search.loading = false;
      search.error = err;
      renderAll();
    });
  }

  /* ---------- shell ---------- */

  function buildShell() {
    ui.style = el('style');
    ui.style.textContent = CSS;
    document.head.appendChild(ui.style);

    ui.root = el('div');
    ui.root.id = 'memory-atlas-root';

    var top = el('header', 'ma-top');
    var brand = el('div', 'ma-brand');
    brand.appendChild(el('span', 'ma-mark', '◈'));
    brand.appendChild(el('span', null, 'Memory Atlas'));
    top.appendChild(brand);
    top.appendChild(el('div', 'ma-spacer'));

    var scopeField = el('div', 'ma-field');
    var scopeLabel = el('label', 'ma-meta', 'Viewing');
    scopeLabel.setAttribute('for', 'ma-scope');
    ui.scope = el('select', 'ma-select');
    ui.scope.id = 'ma-scope';
    ui.scope.title = 'Choose what you are looking at: all global memory, or one '
      + 'project’s own sources. Global memory is the starting point.';
    on(ui.scope, 'change', function () {
      // Only an explicit choice here ever leaves global memory.
      state.scope = ui.scope.value || null;
      if (state.selectedId && !inScope(state.byId[state.selectedId])) {
        state.selectedId = null;
        state.view = 'home';
      }
      renderAll();
    });
    scopeField.appendChild(scopeLabel);
    scopeField.appendChild(ui.scope);
    top.appendChild(scopeField);

    var searchField = el('div', 'ma-field');
    var searchLabel = el('label', 'ma-sr', 'Search memory text');
    searchLabel.setAttribute('for', 'ma-search');
    ui.search = el('input', 'ma-input');
    ui.search.id = 'ma-search';
    ui.search.type = 'search';
    ui.search.placeholder = 'Find exact words…';
    ui.search.autocomplete = 'off';
    ui.search.title = 'Type words that appear literally in the text, then press Enter. '
      + 'This is an exact substring search — not fuzzy and not semantic.';
    on(ui.search, 'keydown', function (event) {
      if (event.key === 'Enter') { event.preventDefault(); runSearch(false); }
    });
    var searchBtn = el('button', 'ma-btn', 'Search');
    searchBtn.type = 'button';
    searchBtn.title = 'Search every readable source for that exact text.';
    on(searchBtn, 'click', function () { runSearch(false); });
    searchField.appendChild(searchLabel);
    searchField.appendChild(ui.search);
    searchField.appendChild(searchBtn);
    top.appendChild(searchField);

    ui.refresh = el('button', 'ma-btn', 'Refresh');
    ui.refresh.type = 'button';
    ui.refresh.title = 'Read the list of sources again and reload whatever is open.';
    on(ui.refresh, 'click', function () {
      state.doc = null; state.dialogue = null; state.history = null;
      state.graph = null; state.compare = null;
      loadCatalog().then(function (applied) {
        if (applied && !disposed && state.view === 'reader') { loadTab(); }
      });
    });
    top.appendChild(ui.refresh);
    ui.root.appendChild(top);

    var body = el('div', 'ma-body');
    ui.rail = el('nav', 'ma-rail');
    ui.rail.setAttribute('aria-label', 'Memory navigation');
    body.appendChild(ui.rail);
    ui.stage = el('main', 'ma-stage');
    body.appendChild(ui.stage);
    ui.root.appendChild(body);

    ui.gaps = el('footer', 'ma-gaps');
    ui.gaps.setAttribute('aria-label', 'Completeness');
    ui.root.appendChild(ui.gaps);

    ui.status = el('div', 'ma-sr');
    ui.status.setAttribute('role', 'status');
    ui.status.setAttribute('aria-live', 'polite');
    ui.root.appendChild(ui.status);

    document.body.appendChild(ui.root);

    on(document, 'keydown', onKeydown);
    on(window, 'resize', function () {
      if (ui.rail) { applyRailWidth(); }
    });
    applyRailWidth();
  }

  function editable(node) {
    if (!node) { return false; }
    var tag = node.nodeName;
    return tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT' || node.isContentEditable;
  }

  function onKeydown(event) {
    if (event.defaultPrevented) { return; }
    if (event.key === '/' && !editable(event.target)) {
      event.preventDefault();
      if (ui.search) { ui.search.focus(); ui.search.select(); }
      return;
    }
    if (event.key === 'Escape' && !editable(event.target) && state.view !== 'home') {
      event.preventDefault();
      state.view = 'home';
      renderAll();
    }
  }

  function applyRailWidth() {
    if (!ui.root) { return; }
    var narrow = ui.root.clientWidth > 0 && ui.root.clientWidth < NARROW_PX;
    if (ui.railNarrow === narrow) { return; }
    ui.railNarrow = narrow;
    ui.root.className = narrow ? 'ma-narrow' : '';
    renderRail();
  }

  function railButton(item) {
    var family = familyOf(item.family);
    var button = el('button', 'ma-navitem');
    button.type = 'button';
    button.setAttribute('data-source-id', item.id);
    var current = state.view === 'reader' && state.selectedId === item.id;
    if (current) { button.setAttribute('aria-current', 'true'); }
    var dot = el('span', 'ma-dot');
    dot.style.background = family.hue;
    button.appendChild(dot);
    if (current) {
      // A visible current-item marker, not colour alone.
      var marker = el('span', 'ma-sr', 'Currently open: ');
      button.appendChild(marker);
    }
    button.appendChild(el('span', 'ma-navlabel', shortTitle(item)));
    button.title = (current ? 'Currently open — ' : 'Open ') + item.title
      + ' (' + family.label + ')';
    on(button, 'click', function () { selectSource(item.id); });
    return button;
  }

  function shortTitle(item) {
    var title = String(item.title || item.id);
    var slash = title.indexOf(' / ');
    if (slash >= 0 && item.id.indexOf('project:') === 0) { return title.slice(slash + 3); }
    return title;
  }

  function renderRail() {
    if (!ui.rail) { return; }
    scoped('rail', function () {
      clear(ui.rail);
      ui.rail.hidden = false;
      if (ui.railNarrow) {
        // Narrow: the rail becomes a disclosure strip stacked above the reader.
        var details = el('details');
        details.className = 'ma-disclosure';
        var summary = el('summary', 'ma-navitem', 'Browse sources');
        summary.title = 'Show or hide the list of sources in ' + scopeName() + '.';
        details.appendChild(summary);
        var inner = el('div');
        fillRail(inner);
        details.appendChild(inner);
        ui.rail.appendChild(details);
        ui.rail.style.flexBasis = 'auto';
        return;
      }
      ui.rail.style.flexBasis = '200px';
      fillRail(ui.rail);
    });
  }

  /* The current scope, grouped by family, in the same order as the first
   * screen, so the rail and the main list never disagree about what is here. */
  function groupItems(items) {
    var groups = [];
    var placed = {};
    FAMILIES.forEach(function (family) {
      var members = items.filter(function (item) { return item.family === family.key; });
      if (!members.length) { return; }
      members.forEach(function (item) { placed[item.id] = true; });
      groups.push({ family: family, items: members });
    });
    var rest = items.filter(function (item) { return !placed[item.id]; });
    if (rest.length) { groups.push({ family: OTHER_FAMILY, items: rest }); }
    return groups;
  }

  function fillRail(target) {
    var items = scopedItems();
    target.appendChild(el('h2', null, scopeName()));
    if (!items.length) {
      target.appendChild(el('p', 'ma-meta', state.ready ? 'No sources here.' : 'Loading…'));
      return;
    }
    groupItems(items).forEach(function (group) {
      target.appendChild(el('h2', null, group.family.label));
      group.items.forEach(function (item) { target.appendChild(railButton(item)); });
    });
  }

  function renderScope() {
    if (!ui.scope) { return; }
    var current = state.scope || '';
    var values = [''].concat(state.projects);
    var matches = ui.scope.options.length === values.length;
    var index;
    for (index = 0; matches && index < values.length; index += 1) {
      if (ui.scope.options[index].value !== values[index]) { matches = false; }
    }
    // The scope control is part of the persistent shell. Recreate its native
    // options only when the catalogued project set actually changed: otherwise
    // a loading render would replace the focused select and its open/selected
    // native state while the owner is choosing a scope.
    if (!matches) {
      clear(ui.scope);
      var none = el('option', null, 'Global memory');
      none.value = '';
      ui.scope.appendChild(none);
      state.projects.forEach(function (project) {
        var option = el('option', null, project);
        option.value = project;
        ui.scope.appendChild(option);
      });
    }
    if (ui.scope.value !== current) { ui.scope.value = current; }
    ui.scope.disabled = !state.projects.length;
  }

  function activeGaps() {
    var gaps = state.catalogGaps.slice();
    if (state.view === 'search' && state.search) { gaps = gaps.concat(state.search.gaps); }
    if (state.view === 'reader') {
      if (state.tab === 'read' && state.doc) { gaps = gaps.concat(state.doc.gaps); }
      if (state.tab === 'read' && state.dialogue) {
        gaps = gaps.concat(state.dialogue.gaps || []);
      }
      if (state.tab === 'history' && state.history) { gaps = gaps.concat(state.history.gaps); }
      if (state.tab === 'relations' && state.graph) { gaps = gaps.concat(state.graph.gaps); }
      if (state.compare) {
        state.compare.sides.forEach(function (side) {
          gaps = gaps.concat(side.snapshot.gaps || []);
        });
      }
    }
    var merged = {};
    gaps.forEach(function (gap) {
      if (!gap || typeof gap !== 'object') { return; }
      // U+0000 (written as an escape, never as a literal control byte in
      // this file) delimits the pair: it cannot occur in a scope or a reason,
      // so two distinct pairs can never merge into one chip.
      var key = String(gap.scope) + '\u0000' + String(gap.reason);
      merged[key] = (merged[key] || 0) + (Number(gap.count) || 1);
    });
    return Object.keys(merged).map(function (key) {
      var parts = key.split('\u0000');
      return { scope: parts[0], reason: parts[1], count: merged[key] };
    });
  }

  function renderGaps() {
    if (!ui.gaps) { return; }
    clear(ui.gaps);
    var gaps = activeGaps();
    if (state.catalogTruncated) {
      gaps.unshift({ scope: 'catalog', reason: 'page_limit_reached', count: 1 });
    }
    if (!gaps.length) {
      ui.gaps.hidden = true;
      return;
    }
    ui.gaps.hidden = false;
    var gapsLabel = el('span', 'ma-meta', 'Known gaps:');
    gapsLabel.title = 'Places where the backend told us something could not be read or '
      + 'was cut short. They are listed rather than quietly dropped.';
    ui.gaps.appendChild(gapsLabel);
    gaps.slice(0, 12).forEach(function (gap) {
      var chip = el('span', 'ma-chip ma-chip-warn',
        gap.scope + ' · ' + String(gap.reason).replace(/_/g, ' ')
        + (gap.count > 1 ? ' ×' + gap.count : ''));
      ui.gaps.appendChild(chip);
    });
    if (gaps.length > 12) {
      ui.gaps.appendChild(el('span', 'ma-meta', 'and ' + (gaps.length - 12) + ' more'));
    }
  }

  function errorNotice(err, retry) {
    var box = el('div', 'ma-notice');
    var code = err && err.code ? err.code.replace(/_/g, ' ') : 'error';
    box.appendChild(el('strong', null, 'Unavailable — ' + code));
    box.appendChild(el('div', null, err && err.message ? err.message : 'The request failed.'));
    if (err && err.code === 'revision_drift') {
      box.appendChild(el('div', 'ma-meta',
        'The source changed while it was being read. Reload to continue from the new revision.'));
    }
    if (retry) {
      var button = el('button', 'ma-btn ma-btn-accent', 'Reload');
      button.type = 'button';
      button.title = 'Try this request again.';
      button.style.marginTop = '8px';
      on(button, 'click', retry);
      box.appendChild(button);
    }
    return box;
  }

  function renderAll() {
    if (disposed || !ui.root) { return; }
    var key = scrollKey();
    var scroller = ui.stage.querySelector('.ma-scroll');
    if (scroller && ui.lastScrollKey) { scrollMemory[ui.lastScrollKey] = scroller.scrollTop; }
    renderScope();
    renderRail();
    renderStage();
    renderGaps();
    var next = ui.stage.querySelector('.ma-scroll');
    if (next && scrollMemory[key] !== undefined) { next.scrollTop = scrollMemory[key]; }
    ui.lastScrollKey = key;
  }

  /* ---------- views ---------- */

  function renderStage() {
    scoped('stage', function () { renderStageBody(); });
  }

  function renderStageBody() {
    clear(ui.stage);
    if (state.fatal) {
      var box = el('div', 'ma-scroll');
      box.appendChild(errorNotice(state.fatal, function () { loadCatalog(); }));
      ui.stage.appendChild(box);
      return;
    }
    if (!state.ready) {
      var wait = el('div', 'ma-scroll');
      wait.appendChild(el('p', 'ma-empty', 'Reading the catalogue…'));
      ui.stage.appendChild(wait);
      return;
    }
    if (state.view === 'search') { renderSearch(); return; }
    if (state.view === 'reader' && state.selectedId) { renderReader(); return; }
    renderHome();
  }

  /* One calm row per source: what it is called, when the file last changed, and
   * what kind of past is kept for it. No sparkline, no invented timeline. */
  function sourceRow(item) {
    var family = familyOf(item.family);
    var button = el('button', 'ma-row');
    button.type = 'button';
    button.style.setProperty('--ma-hue', family.hue);
    button.setAttribute('data-source-id', item.id);
    if (state.selectedId === item.id) { button.setAttribute('aria-current', 'true'); }
    button.appendChild(el('span', 'ma-row-title', shortTitle(item)));
    var stamp = formatTime(item.modified_ns);
    var when = el('span', 'ma-meta ma-row-when',
      stamp ? 'Last changed ' + stamp : 'No modification time');
    when.title = 'The file’s last modification time — a single marker, not a record of '
      + 'how far this source’s past goes back.';
    button.appendChild(when);
    var chip = el('span', 'ma-chip', HISTORY_LABEL[item.history] || 'History unknown');
    chip.title = 'Earlier versions: ' + (HISTORY_MEANING[item.history]
      || 'the catalogue did not say what is kept');
    button.appendChild(chip);
    if (item.read_error) {
      button.appendChild(el('span', 'ma-chip ma-chip-warn', 'Content could not be read'));
    }
    button.setAttribute('aria-label', 'Open ' + item.title + ', ' + family.label + ', '
      + (stamp ? 'last changed ' + stamp : 'no modification time') + ', '
      + (HISTORY_LABEL[item.history] || 'history unknown'));
    button.title = 'Open ' + item.title + ' — ' + formatBytes(item.bytes) + '.';
    on(button, 'click', function () { selectSource(item.id); });
    return button;
  }

  /* The first screen. One heading naming where you are, one sentence saying
   * what that means, then the sources grouped by what they are, then the
   * stated exclusion policy. Nothing else. */
  function renderHome() {
    var scroll = el('div', 'ma-scroll');
    var items = scopedItems();

    scroll.appendChild(el('h1', 'ma-h1', scopeName()));
    scroll.appendChild(el('p', 'ma-sub', state.scope
      ? 'Memory belonging to the project “' + state.scope + '”, together with the '
        + 'global sources that are always in view — ' + items.length + ' sources in all.'
      : 'Everything I keep about myself and my work, outside any single project — '
        + items.length + ' sources.'));
    if (state.projects.length) {
      scroll.appendChild(el('p', 'ma-meta', state.scope
        ? 'Use the “Viewing” menu at the top to return to global memory.'
        : 'There are ' + state.projects.length + ' project(s) as well. Use the '
          + '“Viewing” menu at the top to look inside one.'));
    }

    scroll.appendChild(historyLegend());

    if (!items.length) {
      scroll.appendChild(el('p', 'ma-empty', 'No sources are listed here.'));
      scroll.appendChild(excludedSection());
      ui.stage.appendChild(scroll);
      return;
    }

    groupItems(items).forEach(function (group) {
      var section = el('section', 'ma-group');
      var head = el('div', 'ma-group-head');
      var dot = el('span', 'ma-dot');
      dot.style.background = group.family.hue;
      head.appendChild(dot);
      head.appendChild(el('h2', 'ma-group-title', group.family.label));
      head.appendChild(el('span', 'ma-meta', group.items.length + ''));
      section.appendChild(head);
      var desc = group.family.desc || '';
      if (group.family.note) { desc = desc + ' ' + group.family.note; }
      section.appendChild(el('p', 'ma-group-desc', desc));
      var rows = el('div', 'ma-rows');
      group.items.slice().sort(function (a, b) {
        return String(a.title).localeCompare(String(b.title));
      }).forEach(function (item) { rows.appendChild(sourceRow(item)); });
      section.appendChild(rows);
      scroll.appendChild(section);
    });

    scroll.appendChild(excludedSection());
    ui.stage.appendChild(scroll);
  }

  /* A stated policy, not an omission: what this reader deliberately leaves out
   * and why. */
  function excludedSection() {
    var box = el('details', 'ma-disclose');
    var summary = el('summary', null, 'What is not shown here');
    summary.title = 'The parts of the system this reader deliberately leaves out, '
      + 'with the reason for each.';
    box.appendChild(summary);
    box.appendChild(el('p', 'ma-meta', 'These are left out on purpose. Each one is a '
      + 'decision, not a missing piece:'));
    var list = el('ul');
    EXCLUDED.forEach(function (entry) {
      var li = el('li');
      li.appendChild(el('b', null, entry[0]));
      li.appendChild(document.createTextNode(' — ' + entry[1] + '.'));
      list.appendChild(li);
    });
    box.appendChild(list);
    return box;
  }

  /* One line, right-aligned in the header, saying how much of the open tab has
   * actually been read. It replaces the banner that used to sit inside the
   * document panel and push the text down. */
  function loadStateChip(item) {
    function chip(text, warn, why) {
      var node = el('span', 'ma-chip ma-loadstate' + (warn ? ' ma-chip-warn' : ''), text);
      if (why) { node.title = why; }
      return node;
    }
    if (state.tab === 'read') {
      if (isDialogue(item) && state.readerMode === 'rendered') {
        var chronicle = state.dialogue;
        if (!chronicle || (chronicle.loading && !chronicle.blocks.length)) {
          return chip('Loading the chronicle…', false,
            'The consolidated blocks are still being read.');
        }
        if (chronicle.cursor) {
          return chip('Partial · ' + chronicle.blocks.length + ' blocks loaded', true,
            'Only the blocks requested so far are here. Use “Load more blocks…” at the '
            + 'end of the list for the rest.');
        }
        return chip('Complete chronicle · ' + chronicle.blocks.length + ' blocks', false,
          'Every stored block has been read into this view.');
      }
      var doc = state.doc;
      if (!doc || (doc.loading && !doc.text)) {
        return chip('Loading document…', false, 'The text of this source is being read.');
      }
      if (!doc.text) { return null; }
      return doc.complete
        ? chip('Complete document · ' + formatBytes(doc.bytes), false,
          'The whole file has been read into this view.')
        : chip('Partial · ' + formatBytes(doc.bytes) + ' of ' + formatBytes(item.bytes)
          + ' loaded', true,
          'Only the first part has been read so far. Use “Load more of this document” '
          + 'at the end of the text for the rest.');
    }
    if (state.tab === 'history') {
      if (!state.history || state.history.loading) {
        return chip('Loading version history…', false,
          'What is kept about earlier versions is being read.');
      }
      return null;
    }
    if (!state.graph || state.graph.loading) {
      return chip('Loading links…', false,
        'The sources this document refers to are being read.');
    }
    return null;
  }

  function renderReader() {
    var item = state.byId[state.selectedId] || { id: state.selectedId, title: state.selectedId };
    var family = familyOf(item.family);

    var strip = el('div', 'ma-strip');

    /* Row 1 — where you are, what is open, and what is known about the file.
     * The breadcrumb carries the document heading itself, so the title is
     * written once rather than twice, and the category description lives on
     * the category as a tooltip instead of as a second paragraph. */
    var lead = el('div', 'ma-strip-row ma-strip-lead');
    var back = el('button', 'ma-btn ma-back', '← Back');
    back.type = 'button';
    back.title = 'Close this document and go back to ' + scopeName() + '.';
    on(back, 'click', function () { state.view = 'home'; renderAll(); });
    lead.appendChild(back);

    var crumb = el('nav', 'ma-crumb');
    crumb.setAttribute('aria-label', 'You are here');
    var crumbHome = el('button', 'ma-linkbtn', scopeName());
    crumbHome.type = 'button';
    crumbHome.title = 'Back to the list of sources in ' + scopeName() + '.';
    on(crumbHome, 'click', function () { state.view = 'home'; renderAll(); });
    crumb.appendChild(crumbHome);
    crumb.appendChild(el('span', 'ma-crumb-sep', '/'));
    var famNode = el('span', 'ma-crumb-family');
    var dot = el('span', 'ma-dot');
    dot.style.background = family.hue;
    famNode.appendChild(dot);
    famNode.appendChild(document.createTextNode(family.label));
    famNode.title = family.desc + (family.note ? ' ' + family.note : '');
    crumb.appendChild(famNode);
    crumb.appendChild(el('span', 'ma-crumb-sep', '/'));
    var title = el('h1', 'ma-title', item.title || item.id);
    title.title = String(item.title || item.id);
    crumb.appendChild(title);
    lead.appendChild(crumb);

    var meta = el('div', 'ma-strip-meta');
    meta.appendChild(el('span', 'ma-meta',
      'Last modified ' + (formatTime(item.modified_ns) || 'unknown')));
    meta.appendChild(el('span', 'ma-meta', '· ' + formatBytes(item.bytes)));
    var histChip = el('span', 'ma-chip' + (historyMissing(item) ? ' ma-chip-warn' : ''),
      HISTORY_LABEL[item.history] || 'History unknown');
    histChip.title = 'Earlier versions: ' + (HISTORY_MEANING[item.history]
      || 'the catalogue did not say what is kept');
    meta.appendChild(histChip);
    if (item.read_error) {
      meta.appendChild(el('span', 'ma-chip ma-chip-warn',
        'Content could not be read for the catalogue — '
        + String(item.read_error).replace(/_/g, ' ') + '; no revision digest'));
    }
    if (item.revision) {
      var rev = el('span', 'ma-meta', '· revision ' + String(item.revision).slice(0, 12));
      rev.title = String(item.revision);
      meta.appendChild(rev);
    }
    lead.appendChild(meta);
    strip.appendChild(lead);

    /* Row 2 — the two segmented controls that change what the panel below
     * shows, and one honest statement of how much of it has been read. */
    var controls = el('div', 'ma-strip-row ma-strip-controls');
    var tabs = el('div', 'ma-tabs ma-seg');
    tabs.setAttribute('role', 'tablist');
    [['read', 'Read', 'The text of this source as it is stored now.'],
     ['history', 'Version history', 'What is kept about earlier versions of this source.'],
     ['relations', 'Links', 'Other sources this document actually refers to, with the '
       + 'evidence for each link.']].forEach(function (entry) {
      var tab = el('button', 'ma-tab ma-seg-btn', entry[1]);
      tab.type = 'button';
      tab.setAttribute('role', 'tab');
      tab.setAttribute('data-tab', entry[0]);
      tab.title = entry[2];
      tab.setAttribute('aria-selected', state.tab === entry[0] ? 'true' : 'false');
      on(tab, 'click', function () {
        if (state.tab === entry[0]) { return; }
        state.tab = entry[0];
        state.compare = null;
        renderAll();
        if (entry[0] === 'read' && !state.doc && !state.dialogue) { loadRead(); }
        if (entry[0] === 'history' && !state.history) { loadHistory(false); }
        if (entry[0] === 'relations' && !state.graph) { loadGraph(); }
      });
      tabs.appendChild(tab);
    });
    controls.appendChild(tabs);
    if (state.tab === 'read') { controls.appendChild(modeBar(item)); }
    controls.appendChild(el('div', 'ma-spacer'));
    var status = loadStateChip(item);
    if (status) { controls.appendChild(status); }
    strip.appendChild(controls);
    ui.stage.appendChild(strip);

    var scroll = el('div', 'ma-scroll');
    scroll.setAttribute('role', 'tabpanel');
    if (state.tab === 'read') { readPanel(scroll, item); }
    else if (state.tab === 'history') { historyPanel(scroll, item); }
    else { relationsPanel(scroll, item); }
    ui.stage.appendChild(scroll);
  }

  /* Resolve a written path against a parent directory, lexically only, using
   * the same refusals the backend applies in _lexical_path_target: schemes,
   * absolute paths, backslashes, colons and traversal above the root are
   * refused. Nothing is opened; the result is only ever looked up in the
   * catalogue, which is why a resolved string can never become a capability. */
  function resolveRelativePath(parentDir, href) {
    var target = String(href === undefined || href === null ? '' : href)
      .split('#')[0].trim();
    if (!target) { return null; }
    if (target.charAt(0) === '/' || target.indexOf('\\') >= 0
      || target.indexOf(':') >= 0) { return null; }
    var parts = [];
    var segments = String(parentDir || '').split('/').concat(target.split('/'));
    for (var i = 0; i < segments.length; i++) {
      var part = segments[i];
      if (part === '' || part === '.') { continue; }
      if (part === '..') {
        if (!parts.length) { return null; }
        parts.pop();
        continue;
      }
      parts.push(part);
    }
    return parts.length ? parts.join('/') : null;
  }

  /* The directory of the document currently open, root-relative. */
  function openDocumentDir() {
    var item = state.byId[state.selectedId];
    var path = item && typeof item.path === 'string' ? item.path : '';
    var cut = path.lastIndexOf('/');
    return cut > 0 ? path.slice(0, cut) : '';
  }

  /* One vocabulary for the reader and the graph: an exact catalogue id, or a
   * path that lexically resolves to a catalogued source. A link that yields a
   * graph edge therefore also opens here, and vice versa. */
  function resolveSourceRef(dest, bareNameFallback) {
    var target = String(dest === undefined || dest === null ? '' : dest).trim();
    if (!target) { return null; }
    if (state.byId[target]) { return target; }
    var dir = openDocumentDir();
    var rel = resolveRelativePath(dir, target);
    if (rel && state.byPath[rel]) { return state.byPath[rel]; }
    if (bareNameFallback && !/\.md$/.test(target)) {
      var withExt = resolveRelativePath(dir, target + '.md');
      if (withExt && state.byPath[withExt]) { return state.byPath[withExt]; }
    }
    return null;
  }

  function markdownOptions() {
    return {
      resolveSource: function (dest) { return resolveSourceRef(dest, false); },
      resolveWiki: function (dest) { return resolveSourceRef(dest, true); },
      onSource: function (id) { selectSource(id); }
    };
  }

  /* Rendered / Source text, with the words changed for the dialogue chronicle,
   * where "source text" means the raw stored JSON. It is a segmented control in
   * the header row, next to the tabs — the two things that change what the
   * panel shows sit together. */
  function modeBar(item) {
    var bar = el('div', 'ma-modebar ma-seg');
    bar.setAttribute('role', 'group');
    bar.setAttribute('aria-label', 'How to show this source');
    var chronicle = isDialogue(item);
    [['rendered', chronicle ? 'Chronicle' : 'Rendered',
      chronicle ? 'Show the consolidated blocks as readable cards.'
        : 'Show this document formatted, the way it was written.'],
     ['source', 'Source text',
      chronicle ? 'Show the raw stored JSON exactly as it is kept on disk.'
        : 'Show the raw characters of the file, with line numbers.']]
      .forEach(function (entry) {
        var button = el('button', 'ma-seg-btn', entry[1]);
        button.type = 'button';
        button.title = entry[2];
        button.setAttribute('aria-pressed', state.readerMode === entry[0] ? 'true' : 'false');
        on(button, 'click', function () {
          if (state.readerMode === entry[0]) { return; }
          state.readerMode = entry[0];
          renderAll();
          if (chronicle && entry[0] === 'rendered') {
            if (!state.dialogue) { loadDialogue(false); }
          } else if (!state.doc) { loadDocument(false); }
        });
        bar.appendChild(button);
      });
    return bar;
  }

  function readPanel(scroll, item) {
    if (isDialogue(item) && state.readerMode === 'rendered') {
      dialoguePanel(scroll, item);
      return;
    }
    documentPanel(scroll, item);
  }

  /* ---------- dialogue chronicle panel ---------- */

  var BLOCK_KIND = {
    summary: { label: 'Summary block',
      note: 'a consolidated summary of a stretch of conversation.' },
    era: { label: 'Era block (compressed)',
      note: 'several older summary blocks folded into one. Detail was lost when they '
        + 'were compressed.' },
    gap: { label: 'Gap',
      note: 'a stretch that was never consolidated. Gaps are kept permanently and are '
        + 'never covered over by compression.' },
    unknown: { label: 'Unrecognised block kind', note: 'shown as stored.' }
  };

  function blockKindOf(type) {
    return BLOCK_KIND[type] || BLOCK_KIND.unknown;
  }

  function dialogueFingerprint(signature) {
    if (typeof signature === 'string') {
      return { value: signature, title: signature };
    }
    if (!signature || typeof signature !== 'object') {
      return { value: null, title: '' };
    }
    if (typeof signature.first_line_sha256 === 'string') {
      return { value: signature.first_line_sha256, title: signature.first_line_sha256 };
    }

    var seen = [];
    var namedHash = null;
    var digest = null;
    function visit(value) {
      if (!value || typeof value !== 'object' || seen.indexOf(value) !== -1) { return; }
      seen.push(value);
      Object.keys(value).some(function (key) {
        var child = value[key];
        if (key === 'first_line_sha256' && typeof child === 'string') {
          namedHash = child;
          return true;
        }
        if (typeof child === 'string' && /^[a-f0-9]{16,}$/i.test(child) && !digest) {
          digest = child;
        } else if (child && typeof child === 'object') {
          visit(child);
        }
        return Boolean(namedHash);
      });
    }
    visit(signature);
    var value = namedHash || digest;
    return value ? { value: value, title: value } : { value: null, title: '' };
  }

  function dialogueMetaPanel(meta) {
    var box = el('section', 'ma-legend');
    box.appendChild(el('h3', null, 'Why the chronicle stops where it stops'));
    if (!meta || typeof meta !== 'object') {
      box.appendChild(el('p', 'ma-meta',
        'The consolidation record was not returned, so nothing is claimed about how far '
        + 'this chronicle has been brought up to date.'));
      return box;
    }
    if (meta.available === false) {
      box.appendChild(el('p', 'ma-meta', 'Consolidation record unavailable — '
        + (meta.reason ? String(meta.reason).replace(/_/g, ' ')
          : 'no reason was given')
        + '. No consolidation position is claimed.'));
      return box;
    }
    var offset = meta.last_consolidated_offset;
    var ran = meta.last_consolidated_at;
    var text;
    if (offset === null || offset === undefined || offset === '') {
      text = 'The position in the chat log was not recorded, so how much has been '
        + 'consolidated is unknown.';
    } else {
      text = 'Consolidated up to entry ' + String(offset) + ' of the chat log';
      text += ran ? ', last run ' + (formatEventTime(ran) || String(ran)) + '.' : '.';
    }
    box.appendChild(el('p', 'ma-meta', text));
    box.appendChild(el('p', 'ma-meta',
      'Anything said after that point has not been summarised yet, so it is not here. '
      + 'This is provenance only — it is not itself a readable document.'));
    if (meta.chat_log_signature) {
      var fingerprint = dialogueFingerprint(meta.chat_log_signature);
      var sigText = fingerprint.value
        ? 'Chat log fingerprint: ' + fingerprint.value.slice(0, 16)
        : 'Chat log fingerprint is recorded in an unrecognised format.';
      var sig = el('p', 'ma-meta', sigText);
      sig.title = fingerprint.title;
      box.appendChild(sig);
    }
    return box;
  }

  function dialogueBlockCard(block) {
    var type = typeof block.type === 'string' ? block.type : 'unknown';
    var known = BLOCK_KIND[type] ? type : 'unknown';
    var kind = blockKindOf(type);
    var card = el('article', 'ma-block ma-block-' + known);
    card.setAttribute('data-block-type', known);
    var head = el('div', 'ma-block-head');
    head.appendChild(el('span', 'ma-kind', kind.label));
    if (block.ts) {
      head.appendChild(el('span', 'ma-meta', formatEventTime(block.ts) || String(block.ts)));
    }
    if (block.block_id) {
      var idChip = el('span', 'ma-meta', String(block.block_id));
      idChip.title = 'Identifier of this stored block.';
      head.appendChild(idChip);
    }
    card.appendChild(head);
    card.appendChild(el('p', 'ma-block-note', kind.label + ' — ' + kind.note));

    var facts = [];
    if (block.range !== undefined && block.range !== null && block.range !== '') {
      facts.push('Covers ' + (typeof block.range === 'string'
        ? block.range : JSON.stringify(block.range)));
    }
    if (block.message_count !== undefined && block.message_count !== null) {
      facts.push(String(block.message_count) + ' messages');
    }
    if (block.gap_id) { facts.push('Gap id ' + String(block.gap_id)); }
    if (block.content_bytes !== undefined && block.content_bytes !== null) {
      facts.push(formatBytes(block.content_bytes));
    }
    if (facts.length) { card.appendChild(el('div', 'ma-block-facts', facts.join(' · '))); }
    if (block.truncated === true) {
      card.appendChild(el('p', 'ma-chip ma-chip-warn', 'This block is longer than shown'));
    }
    var body = el('div', 'md');
    body.appendChild(renderMarkdown(
      typeof block.content === 'string' ? block.content : '', markdownOptions()));
    card.appendChild(body);
    if (typeof block.content !== 'string' || !block.content) {
      card.appendChild(el('p', 'ma-empty', 'This block carries no text.'));
    }
    return card;
  }

  function dialoguePanel(scroll, item) {
    var chronicle = state.dialogue;
    if (!chronicle) {
      scroll.appendChild(el('p', 'ma-empty', 'Loading the dialogue chronicle…'));
      return;
    }
    if (chronicle.error) {
      scroll.appendChild(errorNotice(chronicle.error, function () { loadDialogue(false); }));
      if (!chronicle.blocks.length) { return; }
    }
    scroll.appendChild(el('p', 'ma-meta',
      'These are consolidated summaries of past conversation, not the conversation '
      + 'itself. The raw chat log is not shown here at all.'));
    scroll.appendChild(dialogueMetaPanel(chronicle.meta));

    if (!chronicle.blocks.length) {
      var incomplete = chronicle.gaps && chronicle.gaps.length;
      scroll.appendChild(el('p', 'ma-empty', chronicle.loading
        ? 'Loading the dialogue chronicle…'
        : incomplete
          ? 'No readable consolidated blocks were returned. Known reading gaps are listed below.'
          : 'No consolidated blocks are stored yet.'));
      return;
    }
    chronicle.blocks.forEach(function (block) {
      scroll.appendChild(dialogueBlockCard(block || {}));
    });

    var footer = el('div', 'ma-strip-row');
    footer.style.marginTop = '12px';
    if (chronicle.loading) {
      footer.appendChild(el('span', 'ma-meta', 'Loading more blocks…'));
    } else if (chronicle.cursor) {
      var more = el('button', 'ma-btn ma-btn-accent', 'Load more blocks…');
      more.type = 'button';
      more.title = 'Fetch the next page of consolidated blocks. Nothing is hidden; the '
        + 'rest has simply not been requested yet.';
      on(more, 'click', function () { loadDialogue(true); });
      footer.appendChild(more);
    } else {
      footer.appendChild(el('span', 'ma-meta', 'Every stored block is listed.'));
    }
    scroll.appendChild(footer);
  }

  function documentPanel(scroll, item) {
    var doc = state.doc;
    if (!doc) { scroll.appendChild(el('p', 'ma-empty', 'Loading document…')); return; }
    if (doc.error) {
      scroll.appendChild(errorNotice(doc.error, function () { loadDocument(false); }));
      if (!doc.text) { return; }
    }

    if (!doc.text && doc.loading) {
      scroll.appendChild(el('p', 'ma-empty', 'Loading document…'));
    } else if (!doc.text) {
      scroll.appendChild(el('p', 'ma-empty', 'This source is empty.'));
    } else if (state.readerMode === 'source') {
      scroll.appendChild(sourceLines(doc.text, state.focusLine, state.focusQuery));
    } else {
      var article = el('article', 'md ma-doc');
      article.appendChild(renderMarkdown(doc.text, markdownOptions()));
      scroll.appendChild(article);
    }

    var footer = el('div', 'ma-strip-row');
    footer.style.marginTop = '12px';
    if (doc.loading) { footer.appendChild(el('span', 'ma-meta', 'Loading a further page…')); }
    else if (doc.cursor) {
      var more = el('button', 'ma-btn ma-btn-accent', 'Load more of this document');
      more.type = 'button';
      more.title = 'Read the next part of this file. Paging is bounded, so the rest is '
        + 'not hidden — only not yet requested.';
      on(more, 'click', function () { loadDocument(true); });
      footer.appendChild(more);
      footer.appendChild(el('span', 'ma-meta',
        'Paging is bounded; the remainder is not hidden, only not yet requested.'));
    } else if (doc.complete) {
      footer.appendChild(el('span', 'ma-meta', 'The whole document is loaded.'));
    }
    scroll.appendChild(footer);
  }

  function sourceLines(text, focusLine, needle) {
    var pre = el('div', 'ma-srclines ma-src');
    var lines = String(text).replace(/\r\n?/g, '\n').split('\n');
    var target = null;
    lines.forEach(function (line, index) {
      var row = el('div', 'ma-ln');
      row.appendChild(el('span', 'ma-no', String(index + 1)));
      var body = el('span');
      if (focusLine === index + 1) {
        row.className = 'ma-ln ma-hit';
        target = row;
      }
      if (needle && line.toLowerCase().indexOf(String(needle).toLowerCase()) >= 0) {
        markInto(body, line, needle);
      } else {
        body.appendChild(document.createTextNode(line));
      }
      row.appendChild(body);
      pre.appendChild(row);
    });
    if (target) {
      var handle = setTimeout(function () {
        var index = timers.indexOf(handle);
        if (index >= 0) { timers.splice(index, 1); }
        if (!disposed && target.scrollIntoView) { target.scrollIntoView({ block: 'center' }); }
      }, 0);
      timers.push(handle);
    }
    return pre;
  }

  function markInto(parent, line, needle) {
    var haystack = line.toLowerCase();
    var probe = String(needle).toLowerCase();
    var index = 0;
    while (index < line.length) {
      var hit = haystack.indexOf(probe, index);
      if (hit < 0) { break; }
      if (hit > index) { parent.appendChild(document.createTextNode(line.slice(index, hit))); }
      parent.appendChild(el('mark', null, line.slice(hit, hit + probe.length)));
      index = hit + Math.max(1, probe.length);
    }
    if (index < line.length) { parent.appendChild(document.createTextNode(line.slice(index))); }
  }

  function historyPanel(scroll, item) {
    var history = state.history;
    scroll.appendChild(historyLegend());
    if (item.history === 'none') {
      scroll.appendChild(el('p', 'ma-empty',
        'History unavailable — nothing was recorded about earlier versions of this '
        + 'source, so there is no earlier text to show.'));
      return;
    }
    if (!history) { scroll.appendChild(el('p', 'ma-empty', 'Loading history…')); return; }
    if (history.error) {
      scroll.appendChild(errorNotice(history.error, function () { loadHistory(false); }));
      if (!history.items.length) { return; }
    }
    // `unavailable` is a different fact from `none`: the store this source
    // would use exists in the policy but could not be read. Say so, and leave
    // the backend's own gap entries in place rather than reporting emptiness.
    if (item.history === 'unavailable') {
      scroll.appendChild(el('p', 'ma-empty',
        'History could not be read — a history store exists for this kind of source, '
        + 'but it could not be opened, so no earlier versions can be listed. The strip '
        + 'at the bottom keeps the backend’s own reason for it.'));
      if (!history.items.length && !history.loading && !history.error) { return; }
    }

    var snapshots = history.items.filter(function (event) {
      return event.representation === 'snapshot';
    });
    var bar = el('div', 'ma-strip-row');
    bar.style.marginBottom = '10px';
    bar.appendChild(el('span', 'ma-meta', history.items.length + ' entries kept · '
      + snapshots.length + ' with the earlier text itself'));
    bar.appendChild(el('div', 'ma-spacer'));
    if (comparable(item)) {
      var compare = el('button', 'ma-btn ma-btn-accent', 'Compare selected');
      compare.type = 'button';
      compare.disabled = history.selected.length !== 2;
      compare.title = history.selected.length === 2
        ? 'Show what changed between the two ticked versions, older to newer.'
        : 'Tick two “Full snapshot” entries below, then this shows what changed '
          + 'between them.';
      on(compare, 'click', runCompare);
      bar.appendChild(compare);
    }
    scroll.appendChild(bar);

    if (!comparable(item)) {
      // No earlier text exists to diff, so no comparison affordance is offered.
      scroll.appendChild(el('p', 'ma-meta', NO_COMPARE_REASON));
    } else if (snapshots.length < 2) {
      scroll.appendChild(el('p', 'ma-meta',
        'Comparing versions needs two entries that kept the earlier text itself. '
        + 'A digest only, or a record that something changed, cannot be rebuilt into '
        + 'a version and is never shown as one.'));
    }

    if (state.compare) { scroll.appendChild(comparePanel()); }

    if (!history.items.length && !history.loading) {
      scroll.appendChild(el('p', 'ma-empty', 'No history entries were retained for this source.'));
      return;
    }

    history.items.forEach(function (event) {
      scroll.appendChild(eventRow(event, comparable(item)));
    });

    var footer = el('div', 'ma-strip-row');
    footer.style.marginTop = '12px';
    if (history.loading) { footer.appendChild(el('span', 'ma-meta', 'Loading more entries…')); }
    else if (history.cursor) {
      var more = el('button', 'ma-btn', 'Load more history');
      more.type = 'button';
      more.title = 'Fetch the next page of history entries for this source.';
      on(more, 'click', function () { loadHistory(true); });
      footer.appendChild(more);
    } else {
      footer.appendChild(el('span', 'ma-meta', 'All retained entries are listed.'));
    }
    scroll.appendChild(footer);
  }

  function eventRow(event, allowCompare) {
    var row = el('div', 'ma-ev');
    var snapshot = event.representation === 'snapshot';
    if (snapshot && allowCompare) {
      var box = el('input');
      box.type = 'checkbox';
      box.checked = state.history.selected.indexOf(event.event_id) >= 0;
      box.setAttribute('data-event-id', event.event_id);
      box.title = 'Tick two of these, then press “Compare selected” to see what changed.';
      box.setAttribute('aria-label', 'Tick to compare the version “'
        + (event.summary || event.kind) + '” with another');
      on(box, 'change', function () { toggleCompare(event.event_id); });
      row.appendChild(box);
    } else {
      var spacer = el('span');
      spacer.style.width = '13px';
      spacer.setAttribute('aria-hidden', 'true');
      row.appendChild(spacer);
    }

    var main = el('div', 'ma-ev-main');
    var head = el('div', 'ma-strip-row');
    head.appendChild(el('span', 'ma-ev-sum', event.summary || event.kind || 'event'));
    var repChip = el('span', 'ma-chip' + (snapshot ? '' : ' ma-chip-warn'),
      REPRESENTATION_LABEL[event.representation] || String(event.representation));
    repChip.title = REPRESENTATION_MEANING[event.representation]
      || 'the store reported a kind of entry this reader does not know';
    head.appendChild(repChip);
    head.appendChild(el('span', 'ma-meta', formatEventTime(event.ts) || 'Undated'));
    main.appendChild(head);

    var fields = event.fields && typeof event.fields === 'object' ? event.fields : {};
    var keys = Object.keys(fields);
    if (keys.length) {
      var list = el('div', 'ma-fields');
      keys.slice(0, 6).forEach(function (key) {
        var value = fields[key];
        var text = typeof value === 'string' ? value : JSON.stringify(value);
        if (text && text.length > 220) { text = text.slice(0, 220) + '…'; }
        var line = el('div', null, key.replace(/_/g, ' ') + ': ' + text);
        list.appendChild(line);
      });
      if (fields.new_content_preview_truncated === true
        || fields.old_content_preview_truncated === true) {
        list.appendChild(el('div', null,
          'Previews above are explicitly truncated; the full text is not retained here.'));
      }
      main.appendChild(list);
    }
    row.appendChild(main);

    if (snapshot) {
      var open = el('button', 'ma-btn', 'Open snapshot');
      open.type = 'button';
      open.title = 'Read the stored text of this earlier version in full.';
      open.setAttribute('data-open-event', event.event_id);
      on(open, 'click', function () { openSnapshot(event.event_id); });
      row.appendChild(open);
    } else {
      var no = el('span', 'ma-meta', 'No earlier text kept');
      no.title = 'This entry did not keep the text, so there is nothing to open.';
      row.appendChild(no);
    }
    return row;
  }

  function comparePanel() {
    var panel = el('section');
    panel.style.margin = '0 0 14px';
    if (state.compare.loading) {
      panel.appendChild(el('p', 'ma-empty', 'Reading full snapshots…'));
      return panel;
    }
    if (state.compare.error) {
      panel.appendChild(errorNotice(state.compare.error, null));
      return panel;
    }
    var sides = state.compare.sides;
    if (state.compare.mode === 'single' && sides.length === 1) {
      var head = el('div', 'ma-strip-row');
      head.appendChild(el('h2', 'ma-title', 'Snapshot'));
      head.appendChild(el('span', 'ma-chip' + (sides[0].snapshot.complete ? '' : ' ma-chip-warn'),
        sides[0].snapshot.complete ? 'Complete snapshot' : 'Snapshot page incomplete'));
      head.appendChild(el('div', 'ma-spacer'));
      var close = el('button', 'ma-btn', 'Close');
      close.type = 'button';
      close.title = 'Close this earlier version and go back to the list of entries.';
      on(close, 'click', function () { state.compare = null; renderAll(); });
      head.appendChild(close);
      panel.appendChild(head);
      var article = el('article', 'md ma-doc');
      article.appendChild(renderMarkdown(sides[0].snapshot.text, markdownOptions()));
      panel.appendChild(article);
      return panel;
    }
    if (sides.length !== 2) { return panel; }
    var bar = el('div', 'ma-strip-row');
    bar.appendChild(el('h2', 'ma-title', 'Comparison'));
    bar.appendChild(el('span', 'ma-meta', 'Older → newer, in retained order'));
    bar.appendChild(el('div', 'ma-spacer'));
    var dismiss = el('button', 'ma-btn', 'Close');
    dismiss.type = 'button';
    dismiss.title = 'Close this comparison and go back to the list of entries.';
    on(dismiss, 'click', function () { state.compare = null; renderAll(); });
    bar.appendChild(dismiss);
    panel.appendChild(bar);
    var incomplete = sides.filter(function (side) { return !side.snapshot.complete; });
    if (incomplete.length) {
      panel.appendChild(el('p', 'ma-meta',
        'At least one snapshot was truncated by paging; the comparison covers the loaded text only.'));
    }
    var rows = diffLines(sides[0].snapshot.text, sides[1].snapshot.text);
    var diff = el('div', 'ma-diff');
    var added = 0;
    var removed = 0;
    rows.forEach(function (entry) {
      if (entry.op === '+') { added++; }
      if (entry.op === '-') { removed++; }
      var cls = entry.op === '+' ? 'ma-add' : entry.op === '-' ? 'ma-del' : 'ma-ctx';
      diff.appendChild(el('div', cls, (entry.op === '!' ? '' : entry.op + ' ') + entry.text));
    });
    panel.appendChild(el('p', 'ma-meta', added + ' lines added, ' + removed + ' removed.'));
    panel.appendChild(diff);
    return panel;
  }

  /* Only these four link kinds exist. Each is a provable, authored reference
   * reported by the backend with its own literal `basis` string; nothing here
   * is guessed, ranked or produced by a model. */
  var EDGE_KIND = {
    markdown_link: { label: 'Markdown link', dotted: false,
      note: 'this document contains a Markdown link pointing at that file.' },
    wiki_link: { label: 'Wiki link', dotted: false,
      note: 'this document contains a wiki-style [[link]] pointing at that file.' },
    journal_source_ref: { label: 'Journal read reference', dotted: false,
      note: 'a journal entry here records having read that source.' },
    shared_task_id: { label: 'Shared task id', dotted: true,
      note: 'both documents carry the same task identifier. That is not a link — '
        + 'neither one points at the other.' }
  };

  function graphLegend() {
    var box = el('section', 'ma-legend');
    box.appendChild(el('h3', null, 'How to read this'));
    var dl = el('dl');
    Object.keys(EDGE_KIND).forEach(function (key) {
      dl.appendChild(el('dt', null, EDGE_KIND[key].label));
      dl.appendChild(el('dd', null, EDGE_KIND[key].note));
    });
    dl.appendChild(el('dt', null, 'Solid line'));
    dl.appendChild(el('dd', null, 'a reference written in the document itself.'));
    dl.appendChild(el('dt', null, 'Dotted line'));
    dl.appendChild(el('dd', null,
      'not a reference — the same identifier simply appears in both documents.'));
    box.appendChild(dl);
    box.appendChild(el('p', 'ma-meta', 'Every line below carries the backend\u2019s own '
      + 'reason for it, so you can see exactly why it is there. Link targets are never '
      + 'opened, and nothing on this tab is guessed, ranked or generated.'));
    return box;
  }

  function knownEdges(graph) {
    return graph.edges.filter(function (edge) {
      return edge && EDGE_KIND[edge.kind];
    });
  }

  function relationsPanel(scroll, item) {
    var graph = state.graph;
    scroll.appendChild(el('p', 'ma-meta',
      'Links found in \u201C' + (item.title || item.id) + '\u201D. Only references that '
      + 'are actually written down are listed.'));
    scroll.appendChild(graphLegend());

    if (!graph || graph.loading) {
      scroll.appendChild(el('p', 'ma-empty', 'Loading links\u2026'));
      scroll.appendChild(structureSection(item));
      return;
    }
    if (graph.error) {
      scroll.appendChild(errorNotice(graph.error, function () { loadGraph(); }));
      scroll.appendChild(structureSection(item));
      return;
    }
    var edges = knownEdges(graph);
    var unrecognised = graph.edges.length - edges.length;
    if (!edges.length) {
      var incomplete = graph.gaps && graph.gaps.length;
      scroll.appendChild(el('p', 'ma-empty',
        'No outgoing links or recorded provenance links were reported for this document.'
        + (incomplete ? ' The scan was incomplete; known gaps are listed below.' : '')));
    } else {
      scroll.appendChild(graphFigure(edges, item));
      scroll.appendChild(graphList(edges, item));
    }
    if (unrecognised > 0) {
      scroll.appendChild(el('p', 'ma-meta', unrecognised + ' further link(s) were '
        + 'reported under a kind this reader does not recognise, so they are counted '
        + 'here rather than drawn.'));
    }
    scroll.appendChild(structureSection(item));
  }

  /* Being filed under the same project is structure, not a reference anybody
   * wrote. It is computed here from the catalogue ids and kept out of the
   * diagram so it can never be mistaken for a link. */
  function structureSection(item) {
    var section = el('section', 'ma-structure');
    section.appendChild(el('h2', null, 'Other sources in the same project'));
    section.appendChild(el('p', 'ma-meta',
      'This is structure, not links. These sources sit under the same project id; '
      + 'nobody wrote a reference between them.'));
    var project = projectOf(item);
    if (!project) {
      section.appendChild(el('p', 'ma-empty',
        'This source is global, so it does not belong to a project.'));
      return section;
    }
    var siblings = state.catalog.filter(function (other) {
      return other.id !== item.id && projectOf(other) === project;
    });
    if (!siblings.length) {
      section.appendChild(el('p', 'ma-empty',
        'No other source is filed under the project \u201C' + project + '\u201D.'));
      return section;
    }
    var list = el('ul');
    list.style.listStyle = 'none';
    list.style.padding = '0';
    siblings.forEach(function (other) {
      var li = el('li');
      li.style.padding = '4px 0';
      var link = el('button', 'ma-linkbtn', other.title || other.id);
      link.type = 'button';
      link.title = 'Open ' + (other.title || other.id) + ' \u2014 same project, not a link.';
      link.setAttribute('data-source-id', other.id);
      on(link, 'click', function () { selectSource(other.id); });
      li.appendChild(link);
      li.appendChild(el('span', 'ma-meta', ' \u00B7 ' + familyOf(other.family).label));
      list.appendChild(li);
    });
    section.appendChild(list);
    return section;
  }

  function graphFigure(edges, item) {
    var svgns = 'http://www.w3.org/2000/svg';
    var width = 520;
    var height = Math.min(320, 120 + edges.length * 26);
    var svg = document.createElementNS(svgns, 'svg');
    svg.setAttribute('class', 'ma-graph');
    svg.setAttribute('viewBox', '0 0 ' + width + ' ' + height);
    svg.setAttribute('width', '100%');
    svg.setAttribute('role', 'img');
    svg.setAttribute('aria-label', 'Diagram of ' + edges.length
      + ' links around ' + (item.title || item.id)
      + '. The list below carries the same information.');
    var cx = 96;
    var cy = height / 2;
    var targets = edges.slice(0, 10);
    targets.forEach(function (edge, index) {
      var ty = 30 + (index + 0.5) * ((height - 60) / Math.max(1, targets.length));
      var line = document.createElementNS(svgns, 'line');
      line.setAttribute('x1', String(cx + 8));
      line.setAttribute('y1', String(cy));
      line.setAttribute('x2', '300');
      line.setAttribute('y2', String(ty));
      line.setAttribute('class', 'ma-edge'
        + (EDGE_KIND[edge.kind].dotted ? ' ma-edge-dotted' : ''));
      svg.appendChild(line);
      var node = state.byId[edge.target] || { title: edge.target, family: '' };
      var dot = document.createElementNS(svgns, 'circle');
      dot.setAttribute('cx', '306');
      dot.setAttribute('cy', String(ty));
      dot.setAttribute('r', '4');
      dot.setAttribute('fill', familyOf(node.family).hue);
      svg.appendChild(dot);
      var label = document.createElementNS(svgns, 'text');
      label.setAttribute('x', '316');
      label.setAttribute('y', String(ty + 4));
      label.appendChild(document.createTextNode(
        String(node.title || edge.target).slice(0, 28)));
      svg.appendChild(label);
    });
    var focusDot = document.createElementNS(svgns, 'circle');
    focusDot.setAttribute('cx', String(cx));
    focusDot.setAttribute('cy', String(cy));
    focusDot.setAttribute('r', '6');
    focusDot.setAttribute('fill', '#f07a86');
    svg.appendChild(focusDot);
    var focusLabel = document.createElementNS(svgns, 'text');
    focusLabel.setAttribute('x', '8');
    focusLabel.setAttribute('y', String(cy - 12));
    focusLabel.appendChild(document.createTextNode(
      String(item.title || item.id).slice(0, 22)));
    svg.appendChild(focusLabel);
    var figure = el('figure');
    figure.style.margin = '0 0 12px';
    figure.appendChild(svg);
    if (edges.length > targets.length) {
      figure.appendChild(el('figcaption', 'ma-meta', 'The diagram draws the first '
        + targets.length + ' of ' + edges.length + ' links; the list below is complete.'));
    }
    return figure;
  }

  function graphList(edges, item) {
    var section = el('section');
    section.appendChild(el('h2', 'ma-group-title', 'The same links as a list'));
    var list = el('ul');
    list.style.listStyle = 'none';
    list.style.padding = '0';
    edges.forEach(function (edge) {
      var kind = EDGE_KIND[edge.kind];
      var other = edge.target === item.id ? edge.source : edge.target;
      var node = state.byId[other] || { id: other, title: other, family: '' };
      var li = el('li');
      li.setAttribute('data-edge-kind', edge.kind);
      li.style.padding = '7px 0';
      li.style.borderBottom = '1px solid rgba(255,255,255,.07)';
      var head = el('div', 'ma-strip-row');
      var link = el('button', 'ma-linkbtn', node.title || other);
      link.type = 'button';
      link.title = 'Open ' + (node.title || other) + '.';
      link.setAttribute('data-source-id', other);
      on(link, 'click', function () { selectSource(other); });
      head.appendChild(link);
      var chip = el('span', 'ma-chip', kind.label);
      chip.title = kind.note;
      head.appendChild(chip);
      li.appendChild(head);
      // The backend's own words for why this line exists, shown literally.
      var basis = typeof edge.basis === 'string' && edge.basis
        ? edge.basis : 'No reason was reported for this link.';
      li.appendChild(el('div', 'ma-meta ma-basis', 'Why: ' + basis));
      var evidence = edge.evidence && typeof edge.evidence === 'object' ? edge.evidence : null;
      if (evidence && typeof evidence.source_excerpt === 'string' && evidence.source_excerpt) {
        li.appendChild(el('div', 'ma-meta', 'In this document: ' + evidence.source_excerpt));
      }
      list.appendChild(li);
    });
    section.appendChild(list);
    return section;
  }

  function renderSearch() {
    var search = state.search;
    var scroll = el('div', 'ma-scroll');
    var head = el('div', 'ma-strip-row');
    var back = el('button', 'ma-btn', '← Back');
    back.type = 'button';
    back.title = 'Leave the search results and go back to ' + scopeName() + '.';
    on(back, 'click', function () { state.view = 'home'; renderAll(); });
    head.appendChild(back);
    head.appendChild(el('h1', 'ma-h1', 'Search results'));
    scroll.appendChild(head);

    if (!search) {
      scroll.appendChild(el('p', 'ma-empty', 'Type a literal phrase and press Enter.'));
      ui.stage.appendChild(scroll);
      return;
    }
    scroll.appendChild(el('p', 'ma-meta', 'Literal, case-insensitive substring search for “'
      + search.query + '”. Not fuzzy, not semantic.'));
    if (search.error) {
      scroll.appendChild(errorNotice(search.error, function () { runSearch(false); }));
      ui.stage.appendChild(scroll);
      return;
    }
    if (search.loading && !search.items.length) {
      scroll.appendChild(el('p', 'ma-empty', 'Searching…'));
      ui.stage.appendChild(scroll);
      return;
    }
    if (!search.items.length) {
      var incomplete = search.gaps && search.gaps.length;
      scroll.appendChild(el('p', 'ma-empty', incomplete
        ? 'No matches were returned, but the scan was incomplete. Known reading limits '
          + 'and unreadable sources are listed below.'
        : 'No matches. The phrase does not occur in the fully scanned sources.'));
      ui.stage.appendChild(scroll);
      return;
    }

    var outOfScope = 0;
    scroll.appendChild(el('p', 'ma-meta', search.items.length + ' matches shown.'));
    var list = el('ul');
    list.style.listStyle = 'none';
    list.style.padding = '0';
    search.items.forEach(function (hit) {
      var item = state.byId[hit.id] || { id: hit.id, title: hit.id, family: '' };
      if (!inScope(item)) { outOfScope++; }
      var li = el('li');
      li.style.padding = '8px 0';
      li.style.borderBottom = '1px solid rgba(255,255,255,.07)';
      var head2 = el('div', 'ma-strip-row');
      var open = el('button', 'ma-linkbtn', (item.title || hit.id) + ' · line ' + hit.line);
      open.type = 'button';
      open.title = 'Open ' + (item.title || hit.id) + ' at line ' + hit.line + '.';
      open.setAttribute('data-source-id', hit.id);
      on(open, 'click', function () {
        selectSource(hit.id, { tab: 'read', readerMode: 'source', line: hit.line,
          query: search.query });
      });
      head2.appendChild(open);
      head2.appendChild(el('span', 'ma-chip', familyOf(item.family).label));
      if (!inScope(item)) {
        var outside = el('span', 'ma-chip', 'From another project');
        outside.title = 'This match is in a source outside what you are viewing. It is '
          + 'listed rather than dropped.';
        head2.appendChild(outside);
      }
      li.appendChild(head2);
      var excerpt = el('div');
      excerpt.style.font = '12px/1.5 ui-monospace,SFMono-Regular,Menlo,monospace';
      excerpt.style.wordBreak = 'break-word';
      var text = String(hit.excerpt === undefined ? '' : hit.excerpt);
      var start = Number(hit.match_start);
      var end = Number(hit.match_end);
      if (isFinite(start) && isFinite(end) && end > start && end <= text.length) {
        excerpt.appendChild(document.createTextNode(text.slice(0, start)));
        excerpt.appendChild(el('mark', null, text.slice(start, end)));
        excerpt.appendChild(document.createTextNode(text.slice(end)));
      } else {
        excerpt.appendChild(document.createTextNode(text));
      }
      li.appendChild(excerpt);
      list.appendChild(li);
    });
    scroll.appendChild(list);
    if (outOfScope) {
      scroll.appendChild(el('p', 'ma-meta', outOfScope + ' match(es) come from sources '
        + 'outside what you are currently viewing. They are listed, not dropped.'));
    }
    var footer = el('div', 'ma-strip-row');
    footer.style.marginTop = '12px';
    if (search.loading) { footer.appendChild(el('span', 'ma-meta', 'Loading more matches…')); }
    else if (search.cursor) {
      var more = el('button', 'ma-btn', 'Load more matches');
      more.type = 'button';
      more.title = 'Fetch the next page of matches for this search.';
      on(more, 'click', function () { runSearch(true); });
      footer.appendChild(more);
    } else {
      footer.appendChild(el('span', 'ma-meta', 'All matches within the search bound are listed.'));
    }
    scroll.appendChild(footer);
    ui.stage.appendChild(scroll);
  }

  /* ---------- lifecycle ---------- */

  function dispose() {
    if (disposed) { return; }
    disposed = true;
    pending.slice().forEach(function (controller) {
      try { controller.abort(); } catch (err) { /* already settled */ }
    });
    pending.length = 0;
    timers.splice(0).forEach(function (handle) { clearTimeout(handle); });
    Object.keys(listenerScopes).forEach(detachScope);
    if (ui.root && ui.root.parentNode) { ui.root.parentNode.removeChild(ui.root); }
    if (ui.style && ui.style.parentNode) { ui.style.parentNode.removeChild(ui.style); }
    ui = {};
  }

  function boot() {
    buildShell();
    if (typeof window.__ouroWidgetOnDispose === 'function') {
      window.__ouroWidgetOnDispose(dispose);
    }
    renderAll();
    loadCatalog();
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', boot, { once: true });
  } else {
    boot();
  }

  window.__memoryAtlasTestHooks = {
    renderMarkdown: renderMarkdown,
    dispose: dispose,
    state: state,
    // Retained-listener and timer census, so a leak is a test failure.
    retained: function () {
      var counts = { timers: timers.length, pending: pending.length };
      Object.keys(listenerScopes).forEach(function (name) {
        counts[name] = listenerScopes[name].length;
      });
      return counts;
    }
  };
}());
