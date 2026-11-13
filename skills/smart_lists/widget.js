// Smart Lists board: a disposable view of the skill's one persistent store.
// Four stage columns, explicit tag filters across all of them, one task card.
// The frame keeps nothing of its own: an open card's unsaved form is also
// written to the skill's draft slot, so it outlives this view. Every write
// carries the prior values it was based on, so a concurrent change is refused
// by the store instead of being overwritten, and an unanswered write is never
// repeated under a new request id.
(() => {
  "use strict";
  const root = document.getElementById("root");
  // Keep the legacy theme-contract marker explicit; onTheme updates the live value.
  // data-theme="light"; OuroborosWidget.onTheme?.(theme) updates it.
  root.setAttribute("data-theme", "light");
  const host = window.OuroborosWidget;
  const API = "/api/extensions/smart_lists/";
  const STAGES = [
    ["todo", "To do"],
    ["in_progress", "In progress"],
    ["review", "Review"],
    ["done", "Done"],
  ];
  const STAGE_NAME = Object.fromEntries(STAGES);
  const TURNS = [
    ["unassigned", "Не указан"],
    ["owner", "Мой"],
    ["agent", "Ouroboros"],
    ["other", "Другого человека"],
  ];
  const TURN_NAME = Object.fromEntries(TURNS);
  const PAGE = 100; // the board route's largest page
  const LIMIT = { text: 1000, description: 16000, due: 120, tags: 20, tag: 60, refs: 100, label: 120, target: 1000 };
  // The store counts characters (code points); JavaScript's .length counts UTF-16 units, two per emoji.
  const chars = (value) => [...value].length;
  // An input's maxLength also counts UTF-16 units, so the cap leaves room for a full-length value made of
  // astral characters; the exact limit is checked in characters before anything is sent.
  const cap = (limit) => 2 * limit;
  const CARD_TAGS = 4;
  const REFS_SHOWN = 6;

  const el = (tag, cls, text) => {
    const node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text !== undefined) node.textContent = text;
    return node;
  };
  const button = (cls, text, label) => {
    const node = el("button", cls, text);
    node.type = "button";
    if (label) node.setAttribute("aria-label", label);
    return node;
  };
  const lower = (value) => String(value).toLocaleLowerCase();
  const same = (a, b) => JSON.stringify(a) === JSON.stringify(b);
  const plural = (n, one, few, many) => {
    const tail = Math.abs(n) % 100, unit = tail % 10;
    return tail > 10 && tail < 20 ? many : unit === 1 ? one : unit >= 2 && unit <= 4 ? few : many;
  };
  const tasks = (n) => `${n} ${plural(n, "задача", "задачи", "задач")}`;
  const requestId = () =>
    globalThis.crypto?.randomUUID?.() || `${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}`;

  const style = el("style");
  style.textContent = `
:root{color-scheme:light;--bg:#fff;--ink:#262832;--muted:#676d7a;--well:#f5f6f8;--line:#e5e7eb;--edge:#a8abb2;--accent:#bf2945;--tint:#faedf0;--card:#fff;--warn:#fff4d9;--warnline:#e3c682;--shade:#12141b55}
:root[data-theme=dark]{color-scheme:dark;--bg:#151516;--ink:#ededf0;--muted:#a1a1ab;--well:#1c1c1e;--line:#343438;--edge:#5d5d66;--accent:#fa879b;--tint:#39232a;--card:#252527;--warn:#352b14;--warnline:#6f5a26;--shade:#0b0c1088}
*{box-sizing:border-box}
html,body{margin:0;height:100%}
body{font:14px/1.45 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;color:var(--ink);background:var(--bg)}
[hidden]{display:none!important}
button,input,select,textarea{font:inherit;color:inherit}
button{cursor:pointer;background:var(--card);border:1px solid var(--line);border-radius:7px;padding:6px 11px}
button:hover:not(:disabled){border-color:var(--edge)}
button:disabled{opacity:.55;cursor:default}
button:focus-visible,input:focus-visible,select:focus-visible,textarea:focus-visible,a:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
.on,[aria-pressed=true],[aria-current=page]{color:var(--accent);background:var(--tint);border-color:transparent}
.hint{font-size:12px;color:var(--muted)}
.spacer{flex:1}
#root{height:100vh;display:flex;flex-direction:column;padding:18px;gap:14px;min-width:0}
#root.dragging{user-select:none;-webkit-user-select:none;cursor:grabbing}
.top{display:flex;align-items:center;gap:12px;flex:none}
.top h1{font-size:22px;line-height:1.2;margin:0 10px 0 0}
.tabs{display:flex;gap:3px}
.tabs button{border-color:transparent;background:none}
.tabs button[aria-current=page]{background:var(--tint)}
.search{border:1px solid var(--line);background:var(--bg);border-radius:7px;width:210px;padding:7px 10px;min-width:0}
.menu{position:relative}
.menu>button{padding:6px 10px}
.pop{position:absolute;right:0;top:calc(100% + 6px);z-index:4;display:flex;flex-direction:column;min-width:190px;padding:5px;background:var(--card);border:1px solid var(--line);border-radius:9px;box-shadow:0 8px 28px #0003}
.pop button{border:0;background:none;text-align:left;padding:7px 10px;border-radius:6px}
.pop button:hover:not(:disabled){background:var(--well)}
.alert,.draftbar{display:flex;gap:10px;align-items:center;flex:none;font-size:13px;padding:8px 12px;border-radius:7px;background:var(--warn);border:1px solid var(--warnline);margin-top:-4px}
.alert span,.draftbar span{flex:1;min-width:0;overflow-wrap:anywhere}
.alert button,.draftbar button{font-size:12px;padding:3px 10px;flex:none}
.draftbar{flex-wrap:wrap}
.draftbar span{flex-basis:220px}
.filters{display:flex;align-items:flex-start;gap:7px;flex:none}
.chips{display:flex;flex-wrap:wrap;gap:7px;min-width:0}
.chips button{border-radius:20px;font-size:12px;max-width:240px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.filters>.hint{margin-left:auto;flex:none;white-space:nowrap;line-height:31px}
.board{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));grid-template-rows:minmax(0,1fr);gap:12px;min-height:0;flex:1}
.column{background:var(--well);border-radius:9px;padding:10px;display:flex;flex-direction:column;min-height:0;min-width:0;outline:2px dashed transparent;outline-offset:-2px}
.column.drop{outline-color:var(--accent);background:var(--tint)}
.colhead{display:flex;gap:8px;align-items:center;padding:2px 2px 12px;margin:0;font-weight:600;font-size:13px;flex:none}
.dot{width:7px;height:7px;border-radius:50%;background:#9299a5;flex:none}
.column[data-stage=in_progress] .dot{background:#4a86db}
.column[data-stage=review] .dot{background:#ad7dcc}
.column[data-stage=done] .dot{background:#469d74}
.count{font-weight:400;color:var(--muted)}
.cards{overflow-y:auto;overscroll-behavior:contain;min-height:0;flex:1;scrollbar-width:thin}
.task{display:block;width:100%;text-align:left;border:1px solid var(--line);border-radius:7px;padding:8px 10px;margin:0 0 6px;background:var(--card);min-height:64px;touch-action:pan-y}
.task:hover:not(:disabled){border-color:var(--edge);box-shadow:0 2px 4px #00000012}
.task:focus-visible{outline-offset:-2px}
.task.lifted{opacity:.35}
.task.ghost{position:fixed;z-index:9;margin:0;pointer-events:none;box-shadow:0 12px 32px #0004;border-color:var(--edge)}
.task .title{display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden;font-size:14px;line-height:20px;font-weight:500;margin-bottom:5px;overflow-wrap:anywhere}
.meta{display:flex;align-items:center;gap:5px;min-width:0;overflow:hidden}
.tag{font-size:11px;line-height:18px;padding:0 6px;border-radius:4px;background:var(--well);color:var(--muted);white-space:nowrap;flex:0 1 auto;min-width:2.2em;overflow:hidden;text-overflow:ellipsis}
.tag.rest{flex:none}
.tag.on{background:var(--tint);color:var(--accent)}
.linkcount{font-size:11px;color:var(--muted);margin-left:auto;white-space:nowrap;flex:none;padding-left:3px}
.colfoot{display:flex;gap:8px;align-items:center;font-size:11px;color:var(--muted);padding:5px 2px 0;min-height:26px;flex:none}
.colfoot button{font-size:11px;padding:2px 8px;margin-left:auto;color:var(--ink)}
.empty{font-size:12px;color:var(--muted);padding:20px 4px;margin:0}
.archive{overflow:auto;flex:1;min-height:0}
.archrow{display:grid;grid-template-columns:minmax(0,1fr) 180px 110px 90px;gap:12px;align-items:center;padding:14px 10px;border-bottom:1px solid var(--line)}
.archtitle{border:0;text-align:left;padding:0;background:none;overflow-wrap:anywhere;border-radius:3px}
.archrow small{color:var(--muted);font-size:12px}
.archmore{margin:14px 10px}
dialog{border:1px solid var(--line);border-radius:14px;padding:0;background:var(--card);color:var(--ink);width:min(900px,calc(100% - 40px));max-height:calc(100% - 40px);box-shadow:0 20px 90px #0003;overflow:hidden}
dialog[open]{display:flex;flex-direction:column}
dialog::backdrop{background:var(--shade)}
.dialoghead{display:flex;align-items:center;gap:12px;padding:18px 22px;border-bottom:1px solid var(--line);flex:none}
.dialoghead .close{margin-left:auto;border-color:transparent;background:none;font-size:20px;line-height:1;padding:5px 9px}
.badge{font-size:12px;padding:5px 9px;background:var(--well);border-radius:5px;color:var(--muted)}
.dialogbody{display:grid;grid-template-columns:minmax(0,1fr) 220px;gap:28px;padding:22px;overflow:auto;min-height:0;flex:1 1 auto}
.dialogbody>section,.dialogbody>aside{min-width:0}
.titlebox{display:block;width:calc(100% + 14px);margin:-4px -7px 18px;padding:3px 6px;border:1px solid transparent;border-radius:7px;background:none;resize:none;overflow:hidden;font-size:21px;line-height:1.4;font-weight:700}
.titlebox:hover,.titlebox:focus{border-color:var(--line)}
.label{display:block;font-size:12px;font-weight:600;color:var(--muted);margin:0 0 8px}
.field{margin-bottom:20px}
.description{display:block;width:100%;min-height:155px;border:1px solid var(--line);border-radius:7px;background:var(--bg);padding:12px;resize:vertical;line-height:1.6}
.links{display:flex;flex-wrap:wrap;gap:4px 12px;margin-top:8px;font-size:12px}
.links a,.ref a{color:inherit}
.links a{color:var(--accent);text-decoration:none;max-width:100%;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.links a:hover{text-decoration:underline}
.refs{display:flex;flex-direction:column;gap:8px}
.ref{display:flex;gap:6px;align-items:center;background:var(--well);border-radius:6px;min-width:0}
.ref .open{display:flex;gap:10px;align-items:center;flex:1;min-width:0;padding:9px 0 9px 11px;text-decoration:none;border-radius:6px}
.ref b{font-size:11px;min-width:55px;color:var(--muted);flex:none}
.ref .name{font-size:13px;min-width:0;overflow-wrap:anywhere}
.ref em{margin-left:auto;font-size:11px;color:var(--muted);font-style:normal;flex:none;padding-left:8px}
.ref .drop{border-color:transparent;background:none;color:var(--muted);padding:4px 9px;margin-right:3px;flex:none}
.refs>button{align-self:flex-start;font-size:12px}
.addrow{display:flex;gap:6px;margin-top:10px;flex-wrap:wrap}
.addrow input{min-width:0;flex:2 1 180px;background:var(--bg);border:1px solid var(--line);padding:7px;border-radius:6px}
.addrow input.short{flex:1 1 120px}
aside select,aside input{width:100%;min-height:36px;background:var(--bg);border:1px solid var(--line);border-radius:7px;padding:7px}
aside select{-webkit-appearance:none;appearance:none;padding-right:28px;background-image:linear-gradient(45deg,transparent 50%,var(--muted) 50%),linear-gradient(135deg,var(--muted) 50%,transparent 50%);background-position:calc(100% - 15px) 55%,calc(100% - 10px) 55%;background-size:5px 5px;background-repeat:no-repeat}
.tagbox{display:flex;flex-wrap:wrap;gap:5px;margin-bottom:6px}
.tagbox:empty{display:none}
.tagchip{display:inline-flex;align-items:center;max-width:100%;min-width:0;font-size:12px;line-height:18px;padding:2px 0 2px 8px;border-radius:5px;background:var(--well)}
.tagchip span{white-space:pre-wrap;overflow-wrap:anywhere;min-width:0}
.tagchip button{border:0;background:none;color:var(--muted);padding:0 7px;line-height:18px;flex:none}
aside .hint{margin:-12px 0 20px}
.context p{margin:0 0 6px;font-size:13px;white-space:pre-wrap;overflow-wrap:anywhere}
.note{font-size:12px;margin-top:8px;padding:8px 10px;border-radius:6px;background:var(--warn);border:1px solid var(--warnline);overflow-wrap:anywhere}
.note .theirs{white-space:pre-wrap;max-height:84px;overflow:auto;margin-top:4px;font-size:13px}
.note .row{display:flex;gap:6px;margin-top:8px;flex-wrap:wrap}
.note button{font-size:12px;padding:3px 9px}
.dialogfoot{display:flex;gap:9px;align-items:center;flex-wrap:wrap;padding:14px 22px;border-top:1px solid var(--line);flex:none}
.dialogfoot .note{flex:1 1 100%;margin:0 0 4px}
.dialogfoot .hint{min-width:0}
.draftnote:not(:empty)+.hint:not(:empty)::before{content:" · "}
.primary{background:var(--accent);color:var(--bg);border-color:transparent;font-weight:500}
.primary:hover:not(:disabled){border-color:transparent;filter:brightness(1.08)}
#toast{position:fixed;bottom:18px;left:50%;transform:translateX(-50%);z-index:5;display:flex;gap:14px;align-items:center;max-width:calc(100% - 36px);background:var(--ink);color:var(--bg);padding:9px 16px;border-radius:8px;font-size:12px}
#toast button{background:none;border:0;padding:0;color:inherit;font-size:12px;text-decoration:underline;flex:none}
@media(max-width:800px){
#root{padding:12px}
.top{flex-wrap:wrap}
.search{width:auto;flex:1 1 150px}
.filters{flex-wrap:wrap}
.filters>.hint{margin-left:0;white-space:normal;line-height:1.45}
.board{grid-template-columns:repeat(4,260px);overflow-x:auto}
.archrow{grid-template-columns:minmax(0,1fr) auto}
.archrow .meta,.archrow small{display:none}
dialog{width:calc(100% - 16px);max-height:calc(100% - 16px)}
.dialoghead{padding:12px 16px}
.dialogbody{grid-template-columns:minmax(0,1fr);gap:0;padding:16px}
.dialogfoot{padding:12px 16px}
}
@media(prefers-reduced-motion:no-preference){.task{transition:box-shadow .12s,border-color .12s}}
`;
  document.head.append(style);

  // ---------------------------------------------------------------- requests

  // A refusal the store typed (ok:false) or a client error applied nothing.
  // Anything else (no answer, a timeout, a server fault, an unreadable body)
  // leaves the outcome of a write unknown: `unknown` says which.
  function failure(message, extra) {
    return Object.assign(new Error(message), { code: "", conflicts: [], unknown: false, answer: null }, extra);
  }
  async function call(path, init) {
    let response;
    try {
      response = await host.fetch(API + path, { ...init, timeoutMs: 12000 });
    } catch {
      throw failure("Smart Lists не ответил", { unknown: true });
    }
    let data = null;
    try {
      data = await response.json();
    } catch {
      /* judged by the status below */
    }
    if (response.ok && data && data.ok === true) return data;
    const refused = !!data && data.ok === false;
    const text = refused ? (typeof data.error === "string" ? data.error : data.error?.message) : "";
    throw failure(text || `ошибка ${response.status}`, {
      code: refused ? data.code || data.error?.code || "" : "",
      conflicts: refused && Array.isArray(data.conflicts) ? data.conflicts : [],
      unknown: !refused && !(response.status >= 400 && response.status < 500),
      answer: refused ? data : null,
    });
  }
  // Store states the owner can act on; the agent's own wording names its tools.
  const STORE_HINT = {
    store_not_initialized: "Списка ещё нет. Попроси Ouroboros в чате создать его или восстановить из сохранённой копии.",
    store_missing: "Файл со списками пропал, хотя раньше был сохранён. Пустой список вместо него не создан. Попроси Ouroboros в чате восстановить копию.",
    store_unreadable: "Файл со списками не читается. Он не изменён. Попроси Ouroboros в чате разобраться.",
    store_mismatch: "Файл со списками не совпадает с последним сохранением. Он не изменён. Попроси Ouroboros в чате восстановить нужную копию.",
    store_busy: "Списки сейчас заняты другим процессом. Попробуй ещё раз через минуту.",
  };
  const said = (error) => STORE_HINT[error.code] || error.message;
  const post = (path, text) =>
    call(path, { method: "POST", headers: { "content-type": "application/json" }, body: text });
  const readEntry = async (id) => (await call("entry?entry_id=" + encodeURIComponent(id))).entry;

  // ------------------------------------------------------------------- state

  const view = {
    mode: "board",
    query: "",
    tags: [],
    moreTags: false,
    want: Object.fromEntries(STAGES.map(([stage]) => [stage, PAGE])),
    archiveWant: PAGE,
  };
  let board = null; // last complete board read
  let archive = null; // last complete archive read
  const counts = { stages: null, stagesAt: "", archived: null, archivedAt: "" }; // unfiltered
  let loadError = "";
  let storeState = false; // loadError is a store state, not a passing fault
  let disposed = false;
  let serial = 0;
  let loading = false;
  let writing = 0;
  let card = null; // the open task card, if any
  let drag = null;
  let chipWidth = -1;

  const revisionKey = (revision) => (revision ? `${revision.store_id}:${revision.generation}` : "");
  const filtered = () => view.tags.length > 0 || view.query !== "";
  const isSelected = (tag) => view.tags.some((item) => lower(item) === lower(tag));

  // -------------------------------------------------------------- page frame

  const top = el("header", "top");
  const tabs = el("nav", "tabs");
  tabs.setAttribute("aria-label", "Раздел");
  const boardTab = button("", "Доска");
  const archiveTab = button("", "Архив");
  tabs.append(boardTab, archiveTab);
  const search = el("input", "search");
  search.type = "search";
  search.placeholder = "Найти задачу";
  search.setAttribute("aria-label", "Найти задачу");
  const newTask = button("", "+ Задача");
  const menu = el("div", "menu");
  const menuButton = button("", "⋯", "Ещё действия");
  menuButton.setAttribute("aria-haspopup", "menu");
  menuButton.setAttribute("aria-expanded", "false");
  const pop = el("div", "pop");
  pop.setAttribute("role", "menu");
  pop.hidden = true;
  const reloadItem = button("", "Обновить");
  const exportItem = button("", "Скачать копию");
  for (const item of [reloadItem, exportItem]) item.setAttribute("role", "menuitem");
  pop.append(reloadItem, exportItem);
  menu.append(menuButton, pop);
  top.append(el("h1", "", "Дела"), tabs, el("span", "spacer"), search, newTask, menu);

  // A card form saved earlier and not finished: continued or deleted here, never applied by itself.
  const draftBar = el("div", "draftbar");
  draftBar.setAttribute("role", "status");
  draftBar.hidden = true;
  const draftText = el("span");
  const draftResume = button("", "Продолжить");
  const draftDrop = button("", "Удалить черновик");
  const draftRetry = button("", "Повторить");
  draftBar.append(draftText, draftResume, draftDrop, draftRetry);

  const alertBox = el("div", "alert");
  alertBox.setAttribute("role", "alert");
  alertBox.hidden = true;
  const alertText = el("span");
  const alertRetry = button("", "Повторить");
  alertBox.append(alertText, alertRetry);

  const filters = el("nav", "filters");
  filters.id = "filters";
  filters.setAttribute("aria-label", "Фильтр по тегам");
  const chips = el("div", "chips");
  const allChip = button("", "Все");
  const moreChip = button("", "Ещё теги");
  chips.append(allChip, moreChip);
  const filterHint = el("span", "hint");
  filters.append(chips, filterHint);

  const boardBox = el("section", "board");
  boardBox.setAttribute("aria-label", "Доска задач");
  const archiveBox = el("section", "archive");
  archiveBox.setAttribute("aria-label", "Архив задач");
  archiveBox.hidden = true;
  const archiveRows = el("div");
  const archiveEmpty = el("p", "empty");
  const archiveMore = button("archmore", "");
  archiveBox.append(archiveRows, archiveEmpty, archiveMore);

  const toast = el("div");
  toast.id = "toast";
  toast.hidden = true;
  const toastText = el("span");
  const toastAction = button("", "");
  toast.append(toastText, toastAction);

  const columns = {};
  for (const [stage, name] of STAGES) {
    const node = el("section", "column");
    node.dataset.stage = stage;
    const head = el("h2", "colhead");
    const count = el("span", "count");
    head.append(el("span", "dot"), el("span", "", name), count);
    const list = el("div", "cards");
    const blank = el("p", "empty", "Загрузка…");
    const foot = el("div", "colfoot");
    const seen = el("span");
    const more = button("", "");
    more.hidden = true;
    more.onclick = () => {
      view.want[stage] += PAGE;
      refresh();
    };
    foot.append(seen, more);
    node.append(head, list, foot);
    list.append(blank);
    list.addEventListener("scroll", () => scheduleMeasure(), { passive: true });
    columns[stage] = { node, count, list, blank, seen, more };
    boardBox.append(node);
  }
  root.append(top, draftBar, alertBox, filters, boardBox, archiveBox, toast);

  // Reuse nodes by key so focus, hover and scroll survive every redraw. Keyed
  // nodes sit together, in front of `tail` when the container has other children.
  function sync(container, items, keyOf, build, paint, tail = null) {
    const old = new Map();
    for (const node of container.children) if (node.dataset.key !== undefined) old.set(node.dataset.key, node);
    const taken = new Set();
    const nodes = items.map((item) => {
      let key = String(keyOf(item));
      while (taken.has(key)) key += "\u0000"; // a repeated item still gets its own node
      taken.add(key);
      let node = old.get(key);
      if (node) old.delete(key);
      else {
        node = build(item);
        node.dataset.key = key;
      }
      paint(node, item);
      return node;
    });
    for (const node of old.values()) node.remove();
    let cursor = [...container.children].find((node) => node.dataset.key !== undefined) || tail;
    for (const node of nodes) {
      if (node === cursor) cursor = cursor.nextElementSibling;
      else container.insertBefore(node, cursor);
    }
  }

  let toastTimer;
  function notify(text, action) {
    clearTimeout(toastTimer);
    toastText.textContent = text;
    toast.setAttribute("role", action?.alert ? "alert" : "status");
    toastAction.hidden = !action?.label;
    toastAction.textContent = action?.label || "";
    toastAction.onclick = () => {
      toast.hidden = true;
      action.run();
    };
    toast.hidden = false;
    toastTimer = setTimeout(() => (toast.hidden = true), action?.label ? 9000 : 4500);
  }

  // ----------------------------------------------------------------- reading

  function filterQuery() {
    const params = new URLSearchParams();
    for (const tag of view.tags) params.append("tag", tag);
    if (view.query) params.set("query", view.query);
    return params;
  }
  async function readBoard() {
    const path = (offsets) => {
      const params = filterQuery();
      for (const [stage] of STAGES) params.set("offset_" + stage, String(offsets[stage] || 0));
      params.set("limit", String(PAGE));
      return "board?" + params;
    };
    for (let attempt = 0; attempt < 3; attempt++) {
      const data = await call(path({}));
      let whole = true;
      for (;;) {
        const short = STAGES.map(([stage]) => stage).filter(
          (stage) => data.stages[stage].next_offset < Math.min(view.want[stage], data.stages[stage].total),
        );
        if (!short.length) break;
        const offsets = {};
        for (const [stage] of STAGES)
          offsets[stage] = short.includes(stage) ? data.stages[stage].next_offset : data.stages[stage].total;
        const next = await call(path(offsets));
        if (revisionKey(next.revision) !== revisionKey(data.revision)) {
          whole = false; // the store moved between pages: read again from the start
          break;
        }
        let advanced = false;
        for (const stage of short) {
          const page = next.stages[stage];
          if (page.next_offset <= data.stages[stage].next_offset) continue;
          data.stages[stage].entries.push(...page.entries);
          data.stages[stage].next_offset = page.next_offset;
          advanced = true;
        }
        if (!advanced) throw failure("страница доски не продвинулась");
      }
      if (whole) return data;
    }
    throw failure("доска менялась во время чтения");
  }
  async function readArchive() {
    const path = (offset) => {
      const params = filterQuery();
      params.set("offset", String(offset));
      params.set("limit", String(PAGE));
      return "archive?" + params;
    };
    for (let attempt = 0; attempt < 3; attempt++) {
      const data = await call(path(0));
      let whole = true;
      while (data.next_offset < Math.min(view.archiveWant, data.total)) {
        const next = await call(path(data.next_offset));
        if (revisionKey(next.revision) !== revisionKey(data.revision)) {
          whole = false;
          break;
        }
        if (next.next_offset <= data.next_offset) throw failure("страница архива не продвинулась");
        data.entries.push(...next.entries);
        data.next_offset = next.next_offset;
      }
      if (whole) return data;
    }
    throw failure("архив менялся во время чтения");
  }
  // Unfiltered totals for "6 из 9" and the archive tab. Read again only when
  // the store's revision moved; a failure here leaves the numbers out, never wrong.
  async function readCounts(mode, data, wasFiltered) {
    const at = revisionKey(data.revision);
    const jobs = [];
    if (mode === "board" && !wasFiltered) {
      counts.stages = Object.fromEntries(STAGES.map(([stage]) => [stage, data.stages[stage].total]));
      counts.stagesAt = at;
    } else if (mode === "board" && counts.stagesAt !== at) {
      jobs.push(
        call("board?limit=1").then((all) => {
          counts.stages = Object.fromEntries(STAGES.map(([stage]) => [stage, all.stages[stage].total]));
          counts.stagesAt = revisionKey(all.revision);
        }),
      );
    }
    if (mode === "archive" && !wasFiltered) {
      counts.archived = data.total;
      counts.archivedAt = at;
    } else if (counts.archivedAt !== at) {
      jobs.push(
        call("archive?offset=0&limit=1").then((all) => {
          counts.archived = all.total;
          counts.archivedAt = revisionKey(all.revision);
        }),
      );
    }
    await Promise.allSettled(jobs);
  }
  async function refresh() {
    const mine = ++serial;
    const mode = view.mode;
    const wasFiltered = filtered();
    loading = true;
    try {
      const data = await (mode === "board" ? readBoard() : readArchive());
      if (disposed || mine !== serial) return;
      if (mode === "board") board = data;
      else archive = data;
      loadError = "";
      storeState = false;
      render();
      await readCounts(mode, data, wasFiltered);
      if (disposed || mine !== serial) return;
      if (card?.id && !card.busy && !card.loading && !card.unread) pull().catch(() => {}).then(paintCard);
    } catch (error) {
      if (disposed || mine !== serial) return;
      loadError = said(error);
      storeState = !!STORE_HINT[error.code];
    } finally {
      if (mine === serial) {
        loading = false;
        if (!disposed) render();
      }
    }
  }

  // --------------------------------------------------------------- rendering

  function tagNodes(meta, entry, limit) {
    const details = entry.details || {};
    const all = details.tags || [];
    const first = all.filter(isSelected), rest = all.filter((tag) => !isSelected(tag));
    const ordered = [...first, ...rest];
    const shown = limit && ordered.length > limit ? ordered.slice(0, limit - 1) : ordered;
    const nodes = shown.map((tag) => {
      const node = el("span", isSelected(tag) ? "tag on" : "tag", tag);
      if (tag.length > 14) node.title = tag;
      return node;
    });
    if (shown.length < ordered.length) {
      const extra = el("span", "tag rest", "+" + (ordered.length - shown.length));
      extra.title = ordered.slice(shown.length).join(", ");
      nodes.push(extra);
    }
    const links = details.reference_count ?? (details.references || []).length;
    if (links) {
      const mark = el("span", "linkcount", "↗ " + links);
      mark.setAttribute("aria-label", `ссылок: ${links}`);
      nodes.push(mark);
    }
    meta.replaceChildren(...nodes);
  }
  function buildTask() {
    const node = button("task");
    node.append(el("span", "title"), el("span", "meta"));
    node.addEventListener("click", () => openCard(node.dataset.id, node.firstChild.textContent));
    node.addEventListener("pointerdown", dragStart);
    return node;
  }
  function paintTask(node, entry) {
    node.dataset.id = entry.id;
    node.dataset.stage = entry.stage;
    if (node.firstChild.textContent !== entry.text) node.firstChild.textContent = entry.text;
    node.title = entry.text.length > 70 ? entry.text : "";
    tagNodes(node.lastChild, entry, CARD_TAGS);
  }
  function renderBoard() {
    const known = filtered() && counts.stages && counts.stagesAt === revisionKey(board?.revision);
    const nothing = board && !filtered() && STAGES.every(([stage]) => board.stages[stage].total === 0);
    for (const [stage, name] of STAGES) {
      const column = columns[stage], page = board?.stages[stage];
      sync(column.list, page?.entries || [], (entry) => entry.id, buildTask, paintTask, column.blank);
      column.blank.hidden = !!page?.entries.length;
      column.blank.textContent = !board
        ? loadError ? "Не загружено" : "Загрузка…"
        : nothing && stage === "todo" ? "Задач пока нет. Добавь первую кнопкой «+ Задача» или попроси в чате."
        : "Нет задач";
      column.count.textContent = !page ? "" : known ? `${page.total} из ${counts.stages[stage]}` : String(page.total);
      column.node.setAttribute("aria-label", page ? `${name}: ${tasks(page.total)}` : name);
    }
    scheduleMeasure();
  }
  let measureFrame = 0;
  function scheduleMeasure() {
    if (measureFrame) return;
    measureFrame = requestAnimationFrame(() => {
      measureFrame = 0;
      if (disposed || view.mode !== "board") return;
      for (const [stage] of STAGES) {
        const column = columns[stage], page = board?.stages[stage];
        const cards = column.list.querySelectorAll(".task");
        const box = column.list.getBoundingClientRect();
        let visible = 0;
        for (const node of cards) {
          const rect = node.getBoundingClientRect();
          if (rect.top >= box.top - 1 && rect.bottom <= box.bottom + 1) visible++;
        }
        const total = page ? page.total : 0;
        column.seen.textContent =
          visible < total ? `Видно ${visible} из ${total}${visible < cards.length ? " · прокрутите колонку" : ""}` : "";
        const rest = total - cards.length;
        column.more.hidden = rest <= 0;
        if (rest > 0) column.more.textContent = `Загрузить ещё ${Math.min(PAGE, rest)}`;
      }
    });
  }
  function buildArchiveRow() {
    const row = el("div", "archrow");
    const title = button("archtitle");
    title.onclick = () => openCard(row.dataset.id, title.textContent);
    const back = button("", "Вернуть");
    back.onclick = () => restoreRow(row.dataset.id, back);
    row.append(title, el("span", "meta"), el("small"), back);
    return row;
  }
  function paintArchiveRow(row, entry) {
    row.dataset.id = entry.id;
    row.dataset.archived = entry.archived_at || "";
    row.dataset.stage = entry.stage;
    const [title, meta, stage] = row.children;
    if (title.textContent !== entry.text) title.textContent = entry.text;
    tagNodes(meta, entry, CARD_TAGS);
    stage.textContent = STAGE_NAME[entry.stage] || entry.stage;
  }
  function renderArchive() {
    const entries = archive?.entries || [];
    sync(archiveRows, entries, (entry) => entry.id, buildArchiveRow, paintArchiveRow);
    archiveEmpty.hidden = entries.length > 0;
    archiveEmpty.textContent = !archive
      ? loadError ? "Не загружено" : "Загрузка…"
      : filtered() ? "В архиве нет задач по выбранным фильтрам."
      : "Архив пуст. Задачу можно убрать сюда из её карточки.";
    const rest = archive ? archive.total - entries.length : 0;
    archiveMore.hidden = rest <= 0;
    archiveMore.textContent = `Показать ещё ${Math.min(PAGE, rest)} из ${rest}`;
  }
  function renderFilters() {
    const source = view.mode === "board" ? board : archive;
    const available = source?.available_tags || [];
    const listed = [...available, ...view.tags.filter((tag) => !available.some((item) => lower(item) === lower(tag)))];
    sync(chips, listed, (tag) => tag, (tag) => {
        const chip = button("", tag);
        chip.dataset.tag = tag;
        chip.title = tag;
        chip.onclick = () => {
          const at = view.tags.findIndex((item) => lower(item) === lower(tag));
          if (at < 0) view.tags.push(tag);
          else view.tags.splice(at, 1);
          changeFilter();
        };
        return chip;
      }, (chip, tag) => chip.setAttribute("aria-pressed", String(isSelected(tag))), moreChip);
    allChip.setAttribute("aria-pressed", String(!view.tags.length));
    const total = !source ? null
      : view.mode === "board" ? STAGES.reduce((sum, [stage]) => sum + source.stages[stage].total, 0)
      : source.total;
    const where = view.mode === "board" ? "всю доску" : "архив";
    filterHint.textContent =
      view.tags.length > 1 ? `Задачи со всеми выбранными тегами сразу (И)${total == null ? "" : " · " + tasks(total)}`
      : view.tags.length ? `Только с тегом ${view.tags[0]}${total == null ? "" : " · " + tasks(total)}`
      : view.query ? (total == null ? "Поиск…" : `Найдено: ${tasks(total)}`)
      : `Фильтр действует на ${where}`;
    fitChips();
  }
  // One row of tag chips; the rest wait behind "Ещё теги". Selected tags
  // always stay in view, so a filter in force is never hidden.
  function fitChips() {
    const nodes = [...chips.querySelectorAll("[data-tag]")];
    for (const node of nodes) node.hidden = false;
    moreChip.hidden = false;
    moreChip.textContent = `Ещё теги · ${nodes.length}`;
    const gap = 7;
    const inline = getComputedStyle(filters).flexWrap === "nowrap";
    const room = filters.clientWidth - (inline ? filterHint.offsetWidth + gap : 0);
    const widths = nodes.map((node) => node.offsetWidth);
    const needed = widths.reduce((sum, width) => sum + gap + width, allChip.offsetWidth);
    chipWidth = filters.clientWidth;
    if (needed <= room) {
      moreChip.hidden = true;
      return;
    }
    const budget = room - moreChip.offsetWidth - gap;
    let used = allChip.offsetWidth, full = false;
    const spare = [];
    nodes.forEach((node, index) => {
      if (isSelected(node.dataset.tag)) used += gap + widths[index];
      else if (full || used + gap + widths[index] > budget) {
        full = true;
        spare.push(node);
      } else used += gap + widths[index];
    });
    if (!spare.length) {
      moreChip.hidden = true;
      return;
    }
    if (!view.moreTags) for (const node of spare) node.hidden = true;
    moreChip.textContent = view.moreTags ? "Меньше тегов" : `Ещё теги · ${spare.length}`;
    moreChip.setAttribute("aria-expanded", String(view.moreTags));
  }
  function render() {
    const onBoard = view.mode === "board";
    for (const [tab, here] of [[boardTab, onBoard], [archiveTab, !onBoard]]) {
      if (here) tab.setAttribute("aria-current", "page");
      else tab.removeAttribute("aria-current");
    }
    archiveTab.textContent = counts.archived == null ? "Архив" : `Архив ${counts.archived}`;
    boardBox.hidden = !onBoard;
    archiveBox.hidden = onBoard;
    const stale = onBoard
      ? board ? "Доска не обновлена: показаны прежние данные." : "Доска не загружена."
      : archive ? "Архив не обновлён: показаны прежние данные." : "Архив не загружен.";
    alertBox.hidden = !loadError;
    alertText.textContent = !loadError ? "" : storeState ? loadError : `${stale} Причина: ${loadError}.`;
    alertRetry.disabled = loading;
    renderFilters();
    if (onBoard) renderBoard();
    else renderArchive();
  }
  function changeFilter() {
    for (const [stage] of STAGES) {
      view.want[stage] = PAGE;
      columns[stage].list.scrollTop = 0;
    }
    view.archiveWant = PAGE;
    archiveBox.scrollTop = 0;
    renderFilters();
    refresh();
  }

  // ------------------------------------------------------------ page actions

  function show(mode) {
    if (view.mode === mode) return;
    view.mode = mode;
    render();
    refresh();
  }
  boardTab.onclick = () => show("board");
  archiveTab.onclick = () => show("archive");
  allChip.onclick = () => {
    if (!view.tags.length) return;
    view.tags = [];
    changeFilter();
  };
  moreChip.onclick = () => {
    view.moreTags = !view.moreTags;
    fitChips();
  };
  let searchTimer;
  search.oninput = () => {
    clearTimeout(searchTimer);
    searchTimer = setTimeout(() => {
      const query = search.value.trim();
      if (query === view.query) return;
      view.query = query;
      changeFilter();
    }, 250);
  };
  alertRetry.onclick = () => refresh();
  archiveMore.onclick = () => {
    view.archiveWant += PAGE;
    refresh();
  };
  const closeMenu = () => {
    pop.hidden = true;
    menuButton.setAttribute("aria-expanded", "false");
  };
  menuButton.onclick = () => {
    pop.hidden = !pop.hidden;
    menuButton.setAttribute("aria-expanded", String(!pop.hidden));
    if (!pop.hidden) reloadItem.focus();
  };
  menu.addEventListener("keydown", (event) => {
    if (event.key !== "Escape" || pop.hidden) return;
    closeMenu();
    menuButton.focus();
  });
  document.addEventListener("pointerdown", (event) => {
    if (!pop.hidden && !menu.contains(event.target)) closeMenu();
  });
  reloadItem.onclick = () => {
    closeMenu();
    menuButton.focus();
    refresh();
  };
  exportItem.onclick = async () => {
    closeMenu();
    menuButton.focus();
    try {
      await host.download("smart-lists-export.json", API + "export?filename=smart-lists-export.json");
    } catch (error) {
      notify(`Копия не скачана: ${error.message}`, { alert: true });
    }
  };
  // Arrow keys walk the cards: up and down inside a column, sideways across.
  boardBox.addEventListener("keydown", (event) => {
    const node = event.target.closest?.(".task");
    const step = { ArrowDown: [0, 1], ArrowUp: [0, -1], ArrowRight: [1, 0], ArrowLeft: [-1, 0] }[event.key];
    if (!node || !step || event.altKey || event.ctrlKey || event.metaKey) return;
    const stages = STAGES.map(([stage]) => stage);
    const own = [...columns[node.dataset.stage].list.querySelectorAll(".task")];
    let target = null;
    if (step[1]) target = own[own.indexOf(node) + step[1]];
    else
      for (let at = stages.indexOf(node.dataset.stage) + step[0]; !target && at >= 0 && at < stages.length; at += step[0]) {
        const other = [...columns[stages[at]].list.querySelectorAll(".task")];
        target = other[Math.min(own.indexOf(node), other.length - 1)];
      }
    if (!target) return;
    event.preventDefault();
    target.focus();
    target.scrollIntoView({ block: "nearest" });
  });

  // One stage or archive change made outside the card. Nothing is repeated
  // on an unknown outcome: the board is read again and says what it found.
  async function quickWrite(path, body, done, subject) {
    writing++;
    try {
      await post(path, JSON.stringify({ ...body, request_id: requestId() }));
      done();
    } catch (error) {
      notify(
        error.unknown ? `${subject}: Smart Lists не ответил, результат неизвестен. Данные перечитаны — проверь задачу.`
          : error.code === "field_conflict" ? `${subject}: задача уже изменена в другом месте. Данные перечитаны.`
          : `${subject}: ${said(error)}`,
        { alert: true },
      );
    } finally {
      writing--;
      refresh();
    }
  }
  function moveStage(id, from, to, undoable = true) {
    return quickWrite("edit", { entry_id: id, changes: { stage: to }, expected: { stage: from } }, () =>
        notify(`Перенесено в ${STAGE_NAME[to]}`, undoable ? { label: "Отменить", run: () => moveStage(id, to, from, false) } : null),
      "Задача не перенесена");
  }
  function setArchived(id, archived, was, stage, undoable = true) {
    return quickWrite("archive", { entry_id: id, archived, expected: { archived_at: was } }, () =>
        notify(archived ? "Перемещено в архив" : `Возвращено в ${STAGE_NAME[stage] || "доску"}`,
          undoable && archived ? { label: "Вернуть", run: () => undoArchive(id, stage) } : null),
      archived ? "Задача не отправлена в архив" : "Задача не возвращена");
  }
  // Undo needs the stamp the archive write produced, so read it first.
  async function undoArchive(id, stage) {
    try {
      const entry = await readEntry(id);
      if (entry.archived_at) await setArchived(id, false, entry.archived_at, stage, false);
    } catch (error) {
      notify(`Задача не возвращена: ${said(error)}`, { alert: true });
    }
  }
  async function restoreRow(id, control) {
    const row = control.closest(".archrow");
    control.disabled = true;
    await setArchived(id, false, row.dataset.archived || null, row.dataset.stage, false);
    control.disabled = false;
  }

  // Dragging a card to another column with a mouse or pen. The stage field in
  // the card stays the keyboard and touch way to do the same.
  function dragStart(event) {
    if (event.button !== 0 || event.pointerType === "touch" || card || writing || drag) return;
    const node = event.currentTarget;
    drag = { node, id: node.dataset.id, from: node.dataset.stage, x: event.clientX, y: event.clientY, ghost: null, over: "" };
    window.addEventListener("pointermove", dragMove);
    window.addEventListener("pointerup", dragEnd);
    window.addEventListener("pointercancel", dragStop);
    window.addEventListener("keydown", dragKey, true);
  }
  function dragMove(event) {
    if (!drag) return;
    if (!drag.ghost) {
      if (Math.hypot(event.clientX - drag.x, event.clientY - drag.y) < 6) return;
      const rect = drag.node.getBoundingClientRect();
      drag.dx = drag.x - rect.left;
      drag.dy = drag.y - rect.top;
      drag.ghost = drag.node.cloneNode(true);
      drag.ghost.classList.add("ghost");
      drag.ghost.removeAttribute("data-key");
      drag.ghost.style.width = rect.width + "px";
      drag.ghost.setAttribute("aria-hidden", "true");
      root.append(drag.ghost);
      drag.node.classList.add("lifted");
      root.classList.add("dragging");
    }
    drag.ghost.style.left = event.clientX - drag.dx + "px";
    drag.ghost.style.top = event.clientY - drag.dy + "px";
    drag.over = "";
    for (const [stage] of STAGES) {
      const rect = columns[stage].node.getBoundingClientRect();
      const inside = event.clientX >= rect.left && event.clientX <= rect.right && event.clientY >= rect.top && event.clientY <= rect.bottom;
      if (inside) drag.over = stage;
      columns[stage].node.classList.toggle("drop", inside && stage !== drag.from);
    }
  }
  function dragStop() {
    if (!drag) return null;
    const ended = drag;
    drag = null;
    window.removeEventListener("pointermove", dragMove);
    window.removeEventListener("pointerup", dragEnd);
    window.removeEventListener("pointercancel", dragStop);
    window.removeEventListener("keydown", dragKey, true);
    ended.ghost?.remove();
    ended.node.classList.remove("lifted");
    root.classList.remove("dragging");
    for (const [stage] of STAGES) columns[stage].node.classList.remove("drop");
    return ended;
  }
  function dragEnd() {
    const ended = dragStop();
    if (!ended?.ghost) return; // a plain click: the card's own handler opens it
    // The click that follows a real drag must not open the card.
    const swallow = (event) => {
      event.stopPropagation();
      event.preventDefault();
    };
    window.addEventListener("click", swallow, { capture: true, once: true });
    setTimeout(() => window.removeEventListener("click", swallow, true), 0);
    if (ended.over && ended.over !== ended.from) moveStage(ended.id, ended.from, ended.over);
  }
  function dragKey(event) {
    if (event.key !== "Escape") return;
    event.preventDefault();
    event.stopPropagation();
    dragStop();
  }

  // --------------------------------------------------------------- task card

  const dialog = el("dialog");
  dialog.setAttribute("aria-label", "Карточка задачи");
  const headId = el("span", "hint");
  const headState = el("span", "badge");
  const closeButton = button("close", "×", "Закрыть");
  const head = el("header", "dialoghead");
  head.append(headId, headState, closeButton);

  const body = el("div", "dialogbody");
  const main = el("section");
  const side = el("aside");
  const slots = {}; // field → where its conflict note goes
  const inputs = {};
  function field(parent, name, key, control) {
    const box = el("div", "field");
    const label = el("label", "label", name);
    control.id = "sl-" + key;
    label.htmlFor = control.id;
    slots[key] = el("div", "note");
    slots[key].hidden = true;
    box.append(label, control, slots[key]);
    parent.append(box);
    inputs[key] = control;
    return box;
  }
  inputs.text = el("textarea", "titlebox");
  inputs.text.rows = 1;
  inputs.text.maxLength = cap(LIMIT.text);
  inputs.text.placeholder = "Название задачи";
  inputs.text.setAttribute("aria-label", "Название задачи");
  slots.text = el("div", "note");
  slots.text.hidden = true;
  slots.text.style.margin = "-10px 0 18px";
  main.append(inputs.text, slots.text);
  const descriptionBox = el("textarea", "description");
  descriptionBox.maxLength = cap(LIMIT.description);
  const descriptionField = field(main, "Описание", "description", descriptionBox);
  const descriptionLinks = el("div", "links");
  descriptionField.insertBefore(descriptionLinks, slots.description);

  const refsField = el("div", "field");
  const refsBox = el("div", "refs");
  const refsEmpty = el("span", "hint", "Пока нет ссылок. Добавь тред, PR, MR или другой источник.");
  const refsAll = button("", "");
  refsBox.append(refsEmpty, refsAll);
  const refsNote = el("div", "note");
  refsNote.hidden = true;
  const addRow = el("div", "addrow");
  const addUrl = el("input");
  addUrl.type = "url";
  addUrl.placeholder = "https://…";
  addUrl.setAttribute("aria-label", "Адрес ссылки");
  const addName = el("input", "short");
  addName.placeholder = "Подпись";
  addName.maxLength = cap(LIMIT.label);
  addName.setAttribute("aria-label", "Подпись ссылки, необязательно");
  const addButton = button("", "Добавить");
  addRow.append(addUrl, addName, addButton);
  const addError = el("div", "note");
  addError.hidden = true;
  addError.setAttribute("role", "alert");
  refsField.append(el("span", "label", "Связанные материалы"), refsBox, refsNote, addRow, addError);
  main.append(refsField);
  const contextField = el("div", "field context");
  contextField.append(el("span", "label", "Заметки агента"));
  main.append(contextField);

  const stageBox = el("select");
  for (const [value, name] of STAGES) stageBox.append(new Option(name, value));
  field(side, "Стадия", "stage", stageBox);
  // Tags are whole tokens: a stored tag may hold any text, commas included,
  // and an untouched one is sent back exactly as it came.
  const tagsBox = el("input");
  tagsBox.autocomplete = "off";
  tagsBox.maxLength = cap(LIMIT.tag);
  tagsBox.placeholder = "Новый тег";
  const tagsField = field(side, "Теги", "tags", tagsBox);
  const tagList = el("div", "tagbox");
  tagList.setAttribute("role", "list");
  tagList.setAttribute("aria-label", "Список тегов");
  tagsField.insertBefore(tagList, tagsBox);
  side.append(el("p", "hint", "Enter добавляет тег целиком. Эти же теги фильтруют доску."));
  const turnBox = el("select");
  for (const [value, name] of TURNS) turnBox.append(new Option(name, value));
  field(side, "Чей следующий ход", "turn", turnBox);
  const dueBox = el("input");
  dueBox.maxLength = cap(LIMIT.due);
  dueBox.placeholder = "Не задан";
  dueBox.autocomplete = "off";
  field(side, "Срок", "due", dueBox);
  const stamp = el("p", "hint");
  stamp.style.margin = "0";
  side.append(stamp);
  body.append(main, side);

  const foot = el("footer", "dialogfoot");
  const footNote = el("div", "note");
  footNote.setAttribute("role", "alert");
  footNote.hidden = true;
  const archiveButton = button("", "В архив");
  const status = el("span", "hint");
  status.setAttribute("role", "status");
  // The draft slot's state and the store's, read as one line of text.
  const draftNote = el("span", "draftnote");
  const statusLine = el("span", "hint");
  statusLine.append(draftNote, status);
  const discardButton = button("", "Не сохранять");
  const doneButton = button("primary", "Готово");
  foot.append(footNote, archiveButton, el("span", "spacer"), statusLine, discardButton, doneButton);
  dialog.append(head, body, foot);
  root.append(dialog);

  const SCALARS = ["text", "description", "stage", "tags", "turn", "due"];
  const BLANK = { text: "", description: "", stage: "todo", due: null, archived_at: null, details: {} };
  // What a form value is on the wire, and what the stored entry holds there.
  const wire = {
    text: (value) => value,
    description: (value) => value,
    stage: (value) => value,
    turn: (value) => value,
    // A blank date is no date; any other text goes as typed, as the store keeps it.
    due: (value) => (value.trim() ? value : null),
    tags: (value) => [...value],
  };
  const stored = {
    text: (entry) => entry.text ?? "",
    description: (entry) => entry.description ?? "",
    stage: (entry) => entry.stage || "todo",
    turn: (entry) => entry.details?.turn ?? "unassigned",
    due: (entry) => entry.due?.raw ?? null,
    tags: (entry) => entry.details?.tags ?? [],
    references: (entry) => entry.details?.references ?? [],
  };
  const formOf = { tags: (value) => [...value], due: (value) => value ?? "" };
  const formValue = (key, entry) => (formOf[key] ? formOf[key](stored[key](entry)) : stored[key](entry));
  const inDetails = (key) => key === "tags" || key === "turn" || key === "references";

  function applyOps(list, ops) {
    let result = list.map((ref) => ({ label: ref.label, target: ref.target }));
    for (const op of ops) {
      if (op.drop) result = result.filter((ref) => !(ref.target === op.target && ref.label === op.label));
      else if (!result.some((ref) => ref.target === op.ref.target)) result.push({ ...op.ref });
    }
    return result;
  }
  const draftRefs = () => applyOps(stored.references(card.base), card.ops);
  const draftWire = (key) => (key === "references" ? draftRefs() : wire[key](card.draft[key]));
  const isDirty = (key) => !same(draftWire(key), stored[key](card.base));
  function dirtyKeys() {
    if (!card.id) {
      // A task that does not exist yet has nothing to lose until something is put in it: a name, a
      // description, a link, or a tag, turn, due date or stage other than what the card opened with.
      const from = card.start || newDraft(BLANK);
      const started = card.draft.text.trim() || card.draft.description.trim() || card.ops.length
        || ["stage", "tags", "turn", "due"].some((key) => !same(draftWire(key), wire[key](from[key])));
      return started ? [...SCALARS, "references"].filter(isDirty) : [];
    }
    return [...SCALARS, "references"].filter(isDirty);
  }
  const unsaved = () =>
    !!card && (dirtyKeys().length > 0 || !!card.pending || !!card.creating || !!tagsBox.value.trim());

  function paintTags() {
    sync(tagList, card.draft.tags, (tag) => lower(tag), () => {
        const chip = el("span", "tagchip");
        chip.setAttribute("role", "listitem");
        const drop = button("", "×");
        drop.onclick = () => {
          card.draft.tags = card.draft.tags.filter((item) => lower(item) !== lower(chip.dataset.key));
          paintTags();
          touched();
          tagsBox.focus();
        };
        chip.append(el("span"), drop);
        return chip;
      }, (chip, tag) => {
        if (chip.firstChild.textContent !== tag) chip.firstChild.textContent = tag;
        chip.lastChild.setAttribute("aria-label", `Убрать тег: ${tag}`);
        chip.title = tag.length > 24 ? tag : "";
      });
  }
  // The typed tag joins the draft whole; false when it cannot.
  function takeTag() {
    const tag = tagsBox.value.trim();
    if (!tag) return true;
    if (card.draft.tags.some((item) => lower(item) === lower(tag))) {
      tagsBox.value = ""; // already there: nothing to add
      return true;
    }
    const problem = card.draft.tags.length >= LIMIT.tags ? `У задачи уже ${LIMIT.tags} тегов, больше не поместится.`
      : chars(tag) > LIMIT.tag ? `Тег длиннее ${LIMIT.tag} символов.`
      : "";
    if (problem) {
      card.error = problem;
      paintCard();
      return false;
    }
    card.draft.tags = [...card.draft.tags, tag];
    tagsBox.value = "";
    paintTags();
    touched();
    return true;
  }

  function fill(keys = SCALARS) {
    for (const key of keys) {
      if (key === "tags") paintTags();
      else inputs[key].value = card.draft[key];
    }
    grow(inputs.text, 180);
    grow(inputs.description, 360);
  }
  function grow(area, most) {
    area.style.height = "auto";
    area.style.height = Math.min(most, area.scrollHeight + 2) + "px";
    area.style.overflowY = area.scrollHeight + 2 > most ? "auto" : "hidden";
  }
  // Adopt a fresh read of the entry. Untouched fields follow the store; a
  // field edited here keeps its draft and, if the store moved under it,
  // shows both values and asks which one stays.
  // `sent` holds what this form's own answered write carried: the store
  // showing those values is that write, not a change made elsewhere, so a
  // field typed in again meanwhile simply stays a draft. That holds even when
  // it was typed back to the value it had before the write: it was sent, so
  // it is not untouched.
  function rebase(fresh, saved = [], sent = {}) {
    const changed = [];
    const wasSent = (key) => Object.prototype.hasOwnProperty.call(sent, key);
    const ours = (key, after) => wasSent(key) && same(after, sent[key]);
    for (const key of SCALARS) {
      const before = stored[key](card.base), after = stored[key](fresh), mine = wire[key](card.draft[key]);
      const untouched = same(mine, before) && !(wasSent(key) && !same(mine, sent[key]));
      if (saved.includes(key) || untouched || same(mine, after)) {
        delete card.conflicts[key];
        // An untouched field over an unchanged value keeps its exact stored text.
        if (untouched && same(before, after) && !saved.includes(key)) continue;
        const value = formValue(key, fresh);
        if (!same(card.draft[key], value)) changed.push(key);
        card.draft[key] = value;
      } else if (ours(key, after)) delete card.conflicts[key];
      else if (!same(before, after)) card.conflicts[key] = formValue(key, fresh);
    }
    const before = stored.references(card.base), after = stored.references(fresh);
    if (saved.includes("references")) card.ops = [];
    else if (card.ops.length && !same(before, after) && !ours("references", after)) card.refsMoved = true;
    // Drop steps the store already holds, so they do not read as unsaved.
    card.ops = card.ops.filter((op) =>
      op.drop ? after.some((ref) => ref.target === op.target && ref.label === op.label)
        : !after.some((ref) => ref.target === op.ref.target));
    if (!card.ops.length) card.refsMoved = false;
    card.base = fresh;
    card.gone = !!fresh.deleted_at;
    fill(changed);
  }
  // Read the open entry again. The quiet poll leaves a form alone while its
  // own write is away; a write that was refused reads regardless.
  async function pull(force = false) {
    const id = card.id;
    let fresh;
    try {
      fresh = await readEntry(id);
    } catch (error) {
      if (card?.id === id && error.code === "not_found") card.gone = true;
      throw error;
    }
    if (card?.id !== id || (card.busy && !force)) return;
    rebase(fresh);
  }

  function describe(key, value) {
    if (key === "stage") return STAGE_NAME[value] || value;
    if (key === "turn") return TURN_NAME[value] || value;
    if (key === "tags") return value.length ? value.map((tag) => `«${tag}»`).join(" ") : "(без тегов)";
    return value === "" ? "(пусто)" : value;
  }
  function paintConflict(key) {
    const slot = slots[key];
    const has = Object.prototype.hasOwnProperty.call(card.conflicts, key);
    slot.hidden = !has;
    if (!has) return slot.replaceChildren();
    const theirs = card.conflicts[key];
    if (slot.dataset.shown === JSON.stringify(theirs) && slot.childElementCount) return;
    slot.dataset.shown = JSON.stringify(theirs);
    const take = button("", "Взять это значение");
    take.onclick = () => {
      acted();
      card.draft[key] = Array.isArray(theirs) ? [...theirs] : theirs;
      delete card.conflicts[key];
      fill([key]);
      paintCard();
      inputs[key].focus();
    };
    const keep = button("", "Оставить моё");
    keep.onclick = () => {
      acted();
      delete card.conflicts[key];
      paintCard();
      inputs[key].focus();
    };
    const row = el("div", "row");
    row.append(take, keep);
    slot.setAttribute("role", "alert");
    slot.replaceChildren(el("div", "", "Пока ты редактировал, поле изменили в другом месте. Сейчас в Smart Lists:"),
      el("div", "theirs", describe(key, theirs)), row);
  }
  const isHttp = (target) => /^https?:\/\//i.test(target);
  function linkKind(target) {
    let url = null;
    try {
      url = new URL(target);
    } catch {
      /* not a URL */
    }
    if (!url || !isHttp(target)) return target.startsWith("task:") ? "Задача" : target.startsWith("row:") ? "Запись" : "Ссылка";
    const name = url.hostname.toLowerCase(), path = url.pathname;
    if (name.endsWith("slack.com")) return "Slack";
    if (name === "t.me" || name.endsWith("telegram.org") || name.endsWith("telegram.me")) return "Telegram";
    if (/\/merge_requests\/\d+/.test(path)) return "MR";
    if (/\/pull\/\d+/.test(path)) return "PR";
    if (name.includes("jira") || name.endsWith("atlassian.net")) return "Jira";
    if (name.includes("gitlab")) return "GitLab";
    if (name === "github.com") return "GitHub";
    return "Ссылка";
  }
  // Links leave through the host bridge only; nothing here is parsed as HTML.
  async function openLink(event, target) {
    event.preventDefault();
    try {
      if (!host.openExternal) throw new Error("этот клиент не открывает ссылки");
      await host.openExternal(target);
    } catch (error) {
      card.error = `Ссылка не открылась: ${error.message}. Адрес: ${target}`;
      paintCard();
    }
  }
  function linkNode(target, cls) {
    const node = el("a", cls);
    node.href = target;
    node.rel = "noreferrer noopener";
    node.title = target;
    node.addEventListener("click", (event) => openLink(event, target));
    return node;
  }
  function buildRef(ref) {
    const row = el("div", "ref");
    const web = isHttp(ref.target);
    const open = web ? linkNode(ref.target, "open") : el("span", "open");
    open.append(el("b", "", linkKind(ref.target)), el("span", "name", web ? ref.label : `${ref.label} — ${ref.target}`));
    if (web) open.append(el("em", "", "↗"));
    const remove = button("drop", "×", `Убрать ссылку: ${ref.label}`);
    remove.onclick = () => {
      card.ops.push({ drop: true, target: ref.target, label: ref.label });
      touched();
      (refsBox.querySelector(".ref .open[href], .ref .drop") || addUrl).focus();
    };
    row.append(open, remove);
    return row;
  }
  function paintRefs() {
    const list = draftRefs();
    const shown = card.refsOpen ? list : list.slice(0, REFS_SHOWN);
    sync(refsBox, shown, (ref) => ref.target + "\n" + ref.label, buildRef, () => {}, refsEmpty);
    refsEmpty.hidden = list.length > 0;
    refsAll.hidden = shown.length >= list.length;
    refsAll.textContent = `Показать все ${list.length}`;
    refsNote.hidden = !card.refsMoved;
    refsNote.textContent = card.refsMoved
      ? "Список ссылок изменили в другом месте. Твои правки перенесены поверх нового списка и ещё не сохранены."
      : "";
    const found = [];
    for (const match of card.draft.description.matchAll(/https?:\/\/[^\s<>"'`]+/gi)) {
      const target = match[0].replace(/[.,;:!?)\]}»]+$/, "");
      if (!found.includes(target) && !list.some((ref) => ref.target === target)) found.push(target);
    }
    sync(descriptionLinks, found.slice(0, 12), (target) => target, (target) => {
        const node = linkNode(target, "");
        node.textContent = "↗ " + target.replace(/^https?:\/\//i, "");
        return node;
      }, () => {});
    descriptionLinks.hidden = !found.length;
  }
  function paintCard() {
    if (!card) return;
    const fresh = card.base, isNew = !card.id, shut = card.gone || card.failed;
    headId.textContent = isNew ? "Новая задача" : card.id;
    headState.hidden = !(card.gone || fresh.archived_at);
    headState.textContent = card.gone ? (fresh.deleted_at ? "В корзине" : "Удалена") : "В архиве";
    body.inert = card.loading || card.failed;
    // A task deleted elsewhere keeps the typed text readable and copyable; nothing here can change it.
    for (const control of body.querySelectorAll("input, textarea")) control.readOnly = !!card.gone;
    for (const control of body.querySelectorAll("select, button")) control.disabled = !!card.gone;
    for (const key of SCALARS) paintConflict(key);
    paintRefs();
    const details = fresh.details || {};
    const context = [];
    if (details.next_step) context.push(el("p", "", "Дальше: " + details.next_step));
    if (details.note) context.push(el("p", "", details.note));
    contextField.hidden = !context.length;
    contextField.replaceChildren(contextField.firstChild, ...context);
    const when = (value) => {
      const date = value ? new Date(value) : null;
      return date && !Number.isNaN(date.getTime())
        ? date.toLocaleString("ru", { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" }) : "";
    };
    stamp.textContent = isNew ? "" : [when(fresh.updated_at) && "Изменено " + when(fresh.updated_at),
      fresh.completed_at && when(fresh.completed_at) && "завершено " + when(fresh.completed_at)].filter(Boolean).join(" · ");
    addError.hidden = !card.addError;
    addError.textContent = card.addError || "";

    const dirty = unsaved();
    const clash = Object.keys(card.conflicts).length > 0;
    footNote.hidden = !(card.error || card.gone);
    footNote.textContent = card.error || (card.gone
      ? "Этой задачи больше нет на доске: её удалили. Вернуть из корзины можно через чат."
        + (formKept() ? " Набранный текст остаётся в черновике, пока ты его не удалишь." : "")
      : "");
    archiveButton.hidden = isNew || shut || card.guard;
    archiveButton.textContent = fresh.archived_at ? "Вернуть на доску" : "В архив";
    archiveButton.disabled = card.busy || card.loading;
    discardButton.hidden = !card.guard;
    discardButton.textContent = isNew ? "Не создавать" : "Не сохранять";
    doneButton.textContent = shut ? "Закрыть" : isNew ? "Создать" : card.guard ? "Сохранить" : "Готово";
    doneButton.disabled = card.busy || card.loading;
    status.textContent = card.loading ? "Загружаю…"
      : card.busy ? "Сохраняю…"
      : card.guard ? (isNew ? "Задача ещё не создана." : unsaved() ? "Правки не сохранены." : "Ссылка набрана, но не добавлена.")
      : clash ? "Выбери, какое значение оставить"
      : dirty ? (isNew ? "" : "Есть несохранённые правки")
      : card.saved ? "Сохранено" : "";
    keepDraft();
    paintDraft();
  }
  // Any edit: the form is now ahead of the store.
  function touched() {
    acted();
    card.saved = false;
    card.guard = false;
    card.addError = "";
    paintCard();
  }
  for (const key of SCALARS) {
    if (key === "tags") continue;
    inputs[key].addEventListener("input", () => {
      if (!card) return;
      card.draft[key] = inputs[key].value;
      if (key === "text") grow(inputs.text, 180);
      if (key === "description") grow(inputs.description, 360);
      touched();
    });
  }
  // The stage is the one field that saves as soon as it is chosen, like a
  // board move; a refusal leaves it as an ordinary unsaved edit.
  stageBox.addEventListener("change", () => {
    if (card?.id && !card.busy && !Object.keys(card.conflicts).length) commit(["stage"]);
  });
  tagsBox.addEventListener("input", () => card && touched());
  tagsBox.addEventListener("keydown", (event) => {
    if (event.key !== "Enter" || event.isComposing || event.metaKey || event.ctrlKey) return;
    event.preventDefault();
    if (card) takeTag();
  });
  inputs.text.addEventListener("keydown", (event) => {
    if (event.key !== "Enter" || event.shiftKey || event.isComposing) return;
    event.preventDefault();
    if (card.id) inputs.description.focus();
    else done();
  });

  function addRef() {
    // The store lowercases the scheme and keeps the rest exactly; so does this.
    const raw = addUrl.value.trim().replace(/^https?(?=:\/\/)/i, (scheme) => scheme.toLowerCase());
    let url = null;
    try {
      url = new URL(raw);
    } catch {
      /* refused below */
    }
    const authority = raw.replace(/^https?:\/\//, "").split(/[/?#]/)[0];
    const list = draftRefs();
    const problem =
      !url || !/^https?:\/\//.test(raw) || !url.hostname || url.username || url.password || authority.includes("@")
        ? "Нужен адрес, который начинается с https:// или http://, без логина и пароля."
      : chars(raw) > LIMIT.target ? `Адрес длиннее ${LIMIT.target} символов.`
      : chars(addName.value.trim()) > LIMIT.label ? `Подпись длиннее ${LIMIT.label} символов.`
      : list.some((ref) => ref.target === raw) ? "Эта ссылка уже прикреплена."
      : list.length >= LIMIT.refs ? `У задачи уже ${LIMIT.refs} ссылок, больше не поместится.`
      : "";
    if (problem) {
      card.addError = problem;
      paintCard();
      addUrl.focus();
      return;
    }
    const label = addName.value.trim() || [...(url.hostname + (url.pathname === "/" ? "" : url.pathname))].slice(0, LIMIT.label).join("");
    card.ops.push({ ref: { label, target: raw } });
    card.refsOpen = true;
    addUrl.value = "";
    addName.value = "";
    touched();
    addUrl.focus();
  }
  addButton.onclick = addRef;
  // A link typed but not added yet is part of the form the draft slot keeps.
  for (const input of [addUrl, addName]) input.addEventListener("input", () => card && (acted(), keepDraft()));
  // The frame's sandbox allows no form submission, so Enter is handled here.
  for (const input of [addUrl, addName])
    input.addEventListener("keydown", (event) => {
      if (event.key !== "Enter" || event.isComposing) return;
      event.preventDefault();
      addRef();
    });
  refsAll.onclick = () => {
    card.refsOpen = true;
    paintCard();
  };

  function problems(keys) {
    const draft = card.draft;
    if (keys.includes("text") && !draft.text.trim()) return "У задачи должно быть название.";
    if (keys.includes("text") && chars(draft.text) > LIMIT.text) return `Название длиннее ${LIMIT.text} символов.`;
    if (keys.includes("description") && chars(draft.description) > LIMIT.description)
      return `Описание длиннее ${LIMIT.description} символов.`;
    if (keys.includes("tags")) {
      if (draft.tags.length > LIMIT.tags) return `Тегов больше ${LIMIT.tags}.`;
      if (draft.tags.some((tag) => chars(tag) > LIMIT.tag)) return `Тег длиннее ${LIMIT.tag} символов.`;
    }
    if (keys.includes("due") && chars(draft.due) > LIMIT.due) return `Срок длиннее ${LIMIT.due} символов.`;
    return "";
  }
  // What an answered write means for the form: fields still showing what was
  // sent are now clean; a field typed in while the request was away stays a draft.
  function landed(op, entry) {
    card.pending = null;
    rebase(entry, op.keys.filter((key) => same(draftWire(key), op.sent[key])), op.sent);
    card.saved = true;
    refresh();
  }
  async function send(op) {
    try {
      landed(op, (await post("edit", op.text)).entry);
      return true;
    } catch (error) {
      if (error.code === "field_conflict") {
        // The store refused it, so this request never applied (a repeat that
        // had applied would answer as a replay). The next save is a new request
        // built on the values read now.
        card.pending = null;
        const read = await pull(true).then(() => true, () => false);
        if (read && !Object.keys(card.conflicts).length) {
          // Moved elsewhere to just what the form shows: nothing to choose, and nothing left to save.
          if (!dirtyKeys().length) return (card.saved = true);
          card.error = "Не сохранено: задачу изменили в другом месте, её значения перечитаны. Правки остались в форме: нажми «Готово» ещё раз.";
          return false;
        }
        card.error = read
          ? "Не сохранено: часть полей изменили в другом месте. Отмеченные поля показывают оба значения."
          : "Не сохранено: часть полей изменили в другом месте, а свежие значения не прочитались. Правки остались в форме.";
        return false;
      }
      if (!error.unknown) {
        card.pending = null;
        card.error = `Не сохранено: ${said(error)}.`;
        return false;
      }
      // No answer. Reading is safe, so find out what the store holds now.
      let fresh;
      try {
        fresh = await readEntry(card.id);
      } catch {
        card.pending = op;
        card.error = "Сохранение не подтверждено: Smart Lists не ответил. Правки остались в форме. «Готово» отправит тот же запрос ещё раз, дубля не будет.";
        return false;
      }
      if (op.keys.every((key) => same(stored[key](fresh), op.sent[key]))) {
        landed(op, fresh);
        return true;
      }
      // The check read still shows the old values, yet the request may land later: the
      // outcome stays unknown, so the next «Готово» repeats it as itself.
      card.pending = op;
      rebase(fresh);
      card.error = "Сохранение не подтверждено: Smart Lists не ответил, и в задаче пока прежние значения. Правки остались в форме. «Готово» отправит тот же запрос ещё раз, дубля не будет.";
      return false;
    }
  }
  async function create() {
    // The request is frozen at the first attempt: a retry after an unanswered
    // one repeats exactly it, and the store answers a repeat without a second task.
    card.creating ||= JSON.stringify({ text: card.draft.text.trim(), request_id: requestId() });
    // It is in the draft slot before it is sent, so a view that goes away meanwhile
    // leaves a card that repeats this very request instead of making a second task.
    if (!(await addIsKept())) {
      card.error = `Задача не создана: ${slot.state === "elsewhere" ? "черновик сейчас хранит другое окно" : "черновик не сохранился"}, поэтому запрос не отправлен.`;
      return false;
    }
    try {
      card.id = (await post("add", card.creating)).entry_id;
    } catch (error) {
      if (error.unknown)
        card.error = "Создание не подтверждено: Smart Lists не ответил. «Создать» отправит тот же запрос ещё раз, дубля не будет.";
      else {
        card.creating = null;
        card.error = `Задача не создана: ${said(error)}.`;
      }
      return false;
    }
    card.creating = null;
    card.unread = true;
    if (view.mode === "board") refresh();
    else show("board"); // a new task lives on the board, so that is where it is shown
    return true;
  }
  // Save what is unsaved. True when the store now holds everything in the form.
  async function commit(only) {
    if (!card || card.busy || card.gone || card.failed) return false;
    if (!takeTag()) return false;
    if (Object.keys(card.conflicts).length) {
      card.error = "Сначала выбери в отмеченных полях, какое значение оставить.";
      paintCard();
      return false;
    }
    const wanted = () => dirtyKeys().filter((key) => !only || only.includes(key));
    const problem = problems(card.id ? wanted() : ["text", ...wanted()]);
    if (problem) {
      card.error = problem;
      paintCard();
      return false;
    }
    card.busy = true;
    card.error = "";
    card.guard = false;
    paintCard();
    try {
      if (!card.id && !(await create())) return false;
      if (card.unread) {
        try {
          const fresh = await readEntry(card.id);
          card.base = fresh; // the new task as stored; the rest of the form is still to save
          card.unread = false;
          if (card.draft.text.trim() === fresh.text) card.draft.text = fresh.text;
          fill(["text"]);
        } catch (error) {
          card.error = `Задача создана, но не прочитана: ${said(error)}. Остальные поля ещё не сохранены.`;
          return false;
        }
      }
      if (card.pending && !(await send(card.pending))) return false;
      const keys = wanted();
      if (!keys.length) return true;
      const changes = {}, expected = {}, sent = {};
      for (const key of keys) {
        sent[key] = draftWire(key);
        const [into, from] = inDetails(key) ? [(changes.details ||= {}), (expected.details ||= {})] : [changes, expected];
        into[key] = sent[key];
        from[key] = stored[key](card.base);
      }
      const op = { keys, sent, text: JSON.stringify({ entry_id: card.id, changes, expected, request_id: requestId() }) };
      // Into the draft before it is sent: a view gone meanwhile leaves it to repeat as itself.
      // The save does not depend on the draft, though: it goes even if that write failed.
      card.pending = op;
      await keepDraft();
      return await send(op);
    } finally {
      if (card) {
        card.busy = false;
        paintCard();
      }
    }
  }
  // Typed while a save or an archive move was away: the form stays open.
  const LATER = "Сохранено. Правки, сделанные во время сохранения, ещё не сохранены: нажми «Готово» ещё раз.";
  const LINK_LEFT = "Ссылка набрана, но не добавлена: нажми «Добавить» или сотри её.";
  async function done() {
    if (!card || card.busy || card.loading) return;
    if (card.gone || card.failed) return closeCard();
    acted();
    if (!(await commit()) || !card) return;
    if (!formKept()) return closeCard();
    card.error = unsaved() ? LATER : LINK_LEFT;
    paintCard();
  }
  async function toggleArchive() {
    if (!card?.id || card.busy) return;
    acted();
    if (unsaved()) {
      if (!(await commit()) || !card) return;
      if (unsaved()) {
        card.error = "Сохранено, но в архив задача пока не перенесена: правки, сделанные во время сохранения, ещё не сохранены.";
        return paintCard();
      }
    }
    const id = card.id, archived = !card.base.archived_at, was = card.base.archived_at ?? null, stage = card.base.stage;
    card.busy = true;
    card.error = "";
    paintCard();
    const text = JSON.stringify({ entry_id: id, archived, expected: { archived_at: was }, request_id: requestId() });
    let ok = false;
    try {
      const answer = await post("archive", text);
      if (card?.id === id) rebase(answer.entry);
      ok = true;
    } catch (error) {
      let fresh = null;
      if (error.unknown || error.code === "field_conflict") fresh = await readEntry(id).catch(() => null);
      if (fresh) rebase(fresh);
      if (fresh && error.unknown && !!fresh.archived_at === archived) ok = true;
      else
        card.error = error.unknown
          ? "Не подтверждено: Smart Lists не ответил" + (fresh ? ", задача пока там, где была, но перемещение ещё может дойти. Проверь задачу на доске перед повтором." : ". Проверь задачу на доске перед повтором.")
          : error.code === "field_conflict" ? "Не выполнено: задачу уже переместили в другом месте."
          : `Не выполнено: ${said(error)}.`;
    }
    card.busy = false;
    if (!ok) return paintCard();
    const moved = archived ? "Перемещено в архив" : `Возвращено в ${STAGE_NAME[stage]}`;
    if (unsaved()) {
      // Edits made while the move was away stay on screen over the moved task.
      card.error = `${moved}. Правки, сделанные за это время, ещё не сохранены.`;
      paintCard();
    } else if (linkTyped()) {
      card.error = `${moved}. ${LINK_LEFT}`;
      paintCard();
    } else {
      closeCard();
      notify(moved, archived ? { label: "Вернуть", run: () => undoArchive(id, stage) } : null);
    }
    refresh();
  }

  function newDraft(entry) {
    return Object.fromEntries(SCALARS.map((key) => [key, formValue(key, entry)]));
  }
  function showCard(state, typed = { tag: "", url: "", label: "" }) {
    acted(); // a card opened by the owner is a new form, with nothing yet to say about its draft
    slot.state = "";
    card = { id: null, base: BLANK, draft: newDraft(BLANK), ops: [], conflicts: {}, error: "", addError: "", ...state };
    card.opener = document.activeElement;
    addUrl.value = typed.url;
    addName.value = typed.label;
    tagsBox.value = typed.tag;
    body.scrollTop = 0;
    paintCard();
    if (!dialog.open) dialog.showModal();
    fill();
  }
  async function openCard(id, title) {
    if (card || drag || !(await slotFree()) || card || drag) return;
    showCard({ id, loading: true, draft: { ...newDraft(BLANK), text: title || "" } });
    try {
      const fresh = await readEntry(id);
      if (card?.id !== id) return;
      card.base = fresh;
      card.draft = newDraft(fresh);
      card.gone = !!fresh.deleted_at;
      card.loading = false;
      fill();
    } catch (error) {
      if (card?.id !== id) return;
      card.loading = false;
      if (error.code === "not_found") card.gone = true;
      else {
        card.failed = true;
        card.error = `Карточка не открылась: ${said(error)}. Закрой её и открой ещё раз.`;
      }
    }
    paintCard();
  }
  async function openNew() {
    if (card || !(await slotFree()) || card) return;
    // A task made while a tag filter is on starts with those tags, so it stays in view.
    const start = { ...newDraft(BLANK), tags: [...view.tags] };
    showCard({ start, draft: { ...start, tags: [...start.tags] } });
    inputs.text.focus();
  }
  // Closing clears the draft slot: everything is saved, or the owner discarded it.
  // A task deleted elsewhere cannot take the typed text, so that stays in the slot.
  function closeCard(discard = false) {
    const opener = card?.opener, id = card?.id;
    const kept = !discard && card?.gone && formKept() ? snapshot() : null;
    card = null;
    persist(kept);
    if (kept) slot.stored = kept;
    // Another window's draft met while this card was open may be gone by now: look again.
    if (!slot.mine && slot.stored) slot.ready = readDraftSlot();
    renderDraftBar();
    if (dialog.open) dialog.close();
    const again = (id && root.querySelector(`[data-id="${CSS.escape(id)}"]`)) || (opener?.isConnected ? opener : newTask);
    (again.matches?.(".archrow") ? again.querySelector("button") : again).focus({ preventScroll: true });
  }
  function askClose() {
    if (!card || card.busy) return;
    if (card.guard) {
      card.guard = false;
      return paintCard();
    }
    if (!formKept()) return closeCard();
    card.guard = true;
    paintCard();
    (unsaved() ? doneButton : addButton).focus();
  }
  newTask.onclick = openNew;
  closeButton.onclick = askClose;
  doneButton.onclick = done;
  discardButton.onclick = () => closeCard(true);
  archiveButton.onclick = toggleArchive;
  dialog.addEventListener("cancel", (event) => {
    event.preventDefault();
    askClose();
  });
  // A dialog the platform closed over an unsaved form comes straight back.
  dialog.addEventListener("close", () => {
    if (card) dialog.showModal();
  });
  let pressedOutside = false;
  dialog.addEventListener("pointerdown", (event) => (pressedOutside = event.target === dialog));
  dialog.addEventListener("click", (event) => {
    if (event.target === dialog && pressedOutside) askClose();
  });
  dialog.addEventListener("keydown", (event) => {
    if (event.key === "Enter" && (event.metaKey || event.ctrlKey)) {
      event.preventDefault();
      done();
    }
  });

  // -------------------------------------------------------------- card draft

  // The open card's unsaved form is also kept in the skill's one draft slot,
  // next to the store but never in it. It is written as the form changes (one
  // write at a time, then the newest form) and cleared once everything in it is
  // saved or the owner discards it. Leaving the page only writes the form there:
  // nothing is saved to the task by leaving. Each write names the slot revision
  // it replaces, so a draft another window keeps is never overwritten silently,
  // and a write whose answer was lost is settled by reading the slot.
  const slot = { rev: null, mine: false, held: "", stored: null, problem: "", unreadable: false, blocked: false,
                 ids: new Set(), next: undefined, run: null, state: "", ready: null, queued: 0, done: 0, waiters: [] };
  const DRAFT_HINT = {
    draft_busy: "черновик в этот момент записывал другой запрос",
    draft_too_large: "он больше 4 МБ",
    draft_unreadable: "прежний черновик не читается",
  };
  const draftSaid = (error) => DRAFT_HINT[error.code] || said(error);
  const UNSENT_EDIT = "Сохранение не подтверждено: ответ Smart Lists не дошёл до прежнего вида. «Готово» отправит тот же запрос ещё раз, дубля не будет.";
  const UNSENT_ADD = "Создание не подтверждено: ответ Smart Lists не дошёл до прежнего вида. «Создать» отправит тот же запрос ещё раз, дубля не будет.";
  // Only what the form shows and builds its writes from; agent notes are read again.
  const slim = (entry) => entry === BLANK ? null : {
    id: entry.id, text: stored.text(entry), description: stored.description(entry), stage: stored.stage(entry),
    due: entry.due ?? null, archived_at: entry.archived_at ?? null, deleted_at: entry.deleted_at ?? null,
    updated_at: entry.updated_at ?? null, completed_at: entry.completed_at ?? null,
    details: { tags: stored.tags(entry), turn: stored.turn(entry), references: stored.references(entry) },
  };
  // A link typed but not added is part of the form: never added by itself, never dropped by itself.
  const linkTyped = () => !!addUrl.value.trim() || !!addName.value.trim();
  // Worth keeping: anything not in the store yet, including such a link.
  const formKept = () => unsaved() || linkTyped();
  function snapshot() {
    return {
      v: 1, entry_id: card.id, title: card.draft.text.trim() || stored.text(card.base), base: slim(card.base),
      form: { ...card.draft, tags: [...card.draft.tags] }, ops: card.ops, conflicts: card.conflicts,
      refs_moved: !!card.refsMoved, unread: !!card.unread,
      pending: card.pending ? { keys: card.pending.keys, sent: card.pending.sent, text: card.pending.text } : null,
      creating: card.creating || null, typed: { tag: tagsBox.value, url: addUrl.value, label: addName.value },
    };
  }
  // What the slot holds now. It is this window's to replace when empty or written from here.
  function learn(current) {
    slot.rev = current.revision;
    slot.mine = current.draft === null || slot.ids.has(current.write_id);
    slot.held = slot.mine ? JSON.stringify(current.draft) : "";
    if (current.draft === null) slot.stored = null;
    else if (!slot.mine) slot.stored = current.draft;
    return slot.mine;
  }
  // One write; false when the slot moved elsewhere: another window keeps it, or it was cleared there.
  // A write refused that way is never sent again by itself, not even over an empty slot: only the
  // owner's next action on this card (`acted`) lets the form be written again.
  async function writeSlot(value) {
    const text = JSON.stringify(value);
    if (slot.mine ? text === slot.held : value === null) return true;
    if (slot.blocked) return false;
    if (!slot.mine) {
      if (!learn(await call("draft"))) return false;
      if (text === slot.held) return true;
    }
    for (let attempt = 0; attempt < 3; attempt++) {
      const id = requestId();
      slot.ids.add(id);
      if (slot.ids.size > 64) slot.ids.delete(slot.ids.values().next().value);
      try {
        const answer = await post("draft", JSON.stringify({ expected_revision: slot.rev, write_id: id, draft: value }));
        slot.rev = answer.revision;
        slot.held = text;
        return true;
      } catch (error) {
        const current = error.unknown ? await call("draft") : error.code === "draft_conflict" ? error.answer?.current : null;
        if (!current) throw error;
        if (slot.ids.has(current.write_id)) {
          // The slot's newest write is this window's own, answered late: its revision is ours to replace.
          slot.rev = current.revision;
          slot.held = JSON.stringify(current.draft);
          if (slot.held === text) return true;
          continue;
        }
        if (current.revision === slot.rev) continue; // lost on the way while the slot stayed as it was
        learn(current);
        slot.blocked = true;
        return false;
      }
    }
    throw failure("черновик не записался");
  }
  // The owner's own action on the open card: after a refused write, the next one may take the slot again.
  const acted = () => (slot.blocked = false);
  // Queue a form for the slot. The promise settles once that form, or a newer one that
  // replaced it in the queue, was written or refused; typing after it is not waited for.
  function persist(value) {
    slot.next = value;
    const turn = ++slot.queued;
    slot.run ||= (async () => {
      while (slot.next !== undefined) {
        const next = slot.next, upto = slot.queued;
        slot.next = undefined;
        try {
          slot.state = (await writeSlot(next)) ? (next ? "saved" : "") : slot.mine ? "cleared" : "elsewhere";
        } catch (error) {
          slot.state = `Черновик не сохранён: ${draftSaid(error)}.`;
        }
        slot.done = upto;
        slot.waiters = slot.waiters.filter(([at, settle]) => (at <= upto ? (settle(), false) : true));
        paintDraft();
      }
      slot.run = null;
    })();
    return new Promise((settle) => (slot.done >= turn ? settle() : slot.waiters.push([turn, settle])));
  }
  // The open form into the slot; once nothing in it is unsaved, the slot is cleared.
  function keepDraft() {
    if (!card || card.loading || card.failed) return slot.run || Promise.resolve();
    return persist(formKept() ? snapshot() : null);
  }
  // True once the slot holds a form with this card's frozen add request.
  async function addIsKept() {
    await keepDraft();
    return slot.mine && slot.held !== "" && JSON.parse(slot.held)?.creating === card?.creating;
  }
  function paintDraft() {
    renderDraftBar();
    if (!card) return;
    draftNote.textContent = slot.state === "saved" ? "Черновик сохранён"
      : slot.state === "elsewhere" ? "Черновик сейчас хранит другое окно: эти правки в нём не сохраняются"
      : slot.state === "cleared" ? "Черновик очистили в другом окне: следующее изменение сохранит его снова"
      : slot.state;
  }
  const short = (text) => ([...text].length > 80 ? [...text].slice(0, 80).join("") + "…" : text);
  function renderDraftBar() {
    const saved = slot.stored;
    draftBar.hidden = !!card || !(saved || slot.problem);
    draftResume.hidden = !saved;
    draftDrop.hidden = !(saved || slot.unreadable);
    draftRetry.hidden = !slot.problem || slot.unreadable;
    const title = saved ? `«${short(saved.title) || "без названия"}»` : "";
    draftText.textContent = saved
      ? saved.entry_id ? `Есть несохранённые правки задачи ${title}.`
        : saved.creating ? `Создание задачи ${title} не подтверждено. Её черновик сохранён.`
        : `Есть несохранённая новая задача ${title}.`
      : slot.unreadable ? "Сохранённый черновик карточки не читается. Файл не изменён."
      : `Не удалось проверить, есть ли сохранённый черновик карточки: ${slot.problem}.`;
  }
  async function readDraftSlot() {
    try {
      learn(await call("draft"));
      slot.problem = "";
      slot.unreadable = false;
    } catch (error) {
      slot.problem = draftSaid(error);
      slot.unreadable = error.code === "draft_unreadable";
    }
    renderDraftBar();
  }
  // One card at a time keeps the slot: a saved one is continued or deleted first.
  async function slotFree() {
    await slot.ready;
    if (!slot.stored) return true;
    notify("Сначала продолжи или удали сохранённый черновик карточки.", { alert: true });
    draftResume.focus();
    return false;
  }
  // Continue a saved card: the form as it was, over a fresh read of the task. An
  // unconfirmed write is not sent again here; «Готово» repeats it as itself.
  async function resumeDraft() {
    const saved = slot.stored;
    if (!saved || card || drag) return;
    slot.stored = null;
    if (!slot.mine) {
      slot.mine = true; // the owner chose to continue it here
      slot.held = JSON.stringify(saved);
    }
    const base = saved.base || BLANK;
    showCard({ id: saved.entry_id, base, draft: { ...saved.form, tags: [...saved.form.tags] }, ops: saved.ops,
      conflicts: saved.conflicts, refsMoved: saved.refs_moved, unread: saved.unread, pending: saved.pending,
      creating: saved.creating, gone: !!base.deleted_at, loading: !!saved.entry_id && !saved.unread,
      error: saved.pending ? UNSENT_EDIT : saved.creating && !saved.entry_id ? UNSENT_ADD : "" }, saved.typed);
    card.opener = null;
    if (!card.loading) return;
    const id = card.id;
    try {
      const fresh = await readEntry(id);
      if (card?.id !== id) return;
      card.loading = false;
      const op = card.pending;
      if (op && op.keys.every((key) => same(stored[key](fresh), op.sent[key]))) {
        card.error = "";
        landed(op, fresh); // it had been applied before the view went away
      } else rebase(fresh);
    } catch (error) {
      if (card?.id !== id) return;
      card.loading = false;
      if (error.code === "not_found") card.gone = true;
      else if (!card.pending) card.error = `Задача не перечитана: ${said(error)}. Форма показана такой, какой была.`;
    }
    paintCard();
  }
  async function dropDraft() {
    if (slot.unreadable) {
      try {
        const answer = await post("draft", JSON.stringify(
          { expected_revision: "", write_id: requestId(), draft: null, replace_unreadable: true }));
        Object.assign(slot, { rev: answer.revision, mine: true, held: "null", problem: "", unreadable: false });
      } catch (error) {
        notify(`Черновик не удалён: ${draftSaid(error)}`, { alert: true });
      }
      return renderDraftBar();
    }
    const saved = slot.stored;
    if (!saved) return;
    slot.stored = null;
    if (!slot.mine) {
      slot.mine = true; // the owner chose to delete what was read
      slot.held = JSON.stringify(saved);
    }
    renderDraftBar();
    await persist(null);
    if (slot.held !== "null" && !slot.stored) {
      slot.stored = saved; // not deleted: it stays offered
      notify(slot.state || "Черновик не удалён.", { alert: true });
    }
    renderDraftBar();
  }
  draftResume.onclick = resumeDraft;
  draftDrop.onclick = dropDraft;
  draftRetry.onclick = () => (slot.ready = readDraftSlot());

  // --------------------------------------------------------------- lifecycle

  const stopTheme = host.onTheme?.((theme) => (document.documentElement.dataset.theme = theme));
  const sizes = new ResizeObserver(() => {
    if (filters.clientWidth !== chipWidth) fitChips();
    scheduleMeasure();
  });
  sizes.observe(filters);
  sizes.observe(boardBox);
  const idle = () => !disposed && !document.hidden && !loading && !writing && !drag && !card?.busy;
  const timer = setInterval(() => idle() && refresh(), 30000);
  const onVisible = () => (document.hidden ? keepDraft() : idle() && refresh());
  document.addEventListener("visibilitychange", onVisible);
  window.__ouroWidgetOnDispose?.(() => {
    disposed = true;
    serial++;
    clearInterval(timer);
    clearTimeout(searchTimer);
    clearTimeout(toastTimer);
    cancelAnimationFrame(measureFrame);
    document.removeEventListener("visibilitychange", onVisible);
    sizes.disconnect();
    dragStop();
    stopTheme?.();
    // Only the open form goes to the draft slot; leaving saves nothing to the task.
    return keepDraft();
  });
  slot.ready = readDraftSlot();
  render();
  refresh();
})();
