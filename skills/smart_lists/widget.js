// One disposable view of the same persistent store; the agent maintains meaning.
const root = document.getElementById("root");
const base = "/api/extensions/smart_lists/";
const turns = {
  unassigned: "Не указан",
  owner: "Мой ход",
  agent: "У Ouroboros",
  other: "Ожидаем",
};
let disposed = false,
  busy = false,
  generation = 0,
  loaded = 100,
  current = null,
  fontSize = 15;
let selectedTurn = "",
  selectedTags = new Set(),
  timer,
  searchTimer;
const el = (tag, cls, text) => {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text !== undefined) n.textContent = text;
  return n;
};
const style = el("style");
style.textContent = `
:root { color-scheme: dark; font: 15px/1.45 system-ui,sans-serif; --fg:#e7e8ef; --muted:#aeb4c3; --bg:#171b24; --line:#444b58; --accent:#a2dbc8; --err:#ffb3ad; }
:root[data-theme="light"] { color-scheme:light; --fg:#222832; --muted:#58616d; --bg:#fff; --line:#ccd1d8; --accent:#215d4b; --err:#a52727; }
* { box-sizing:border-box; } body { margin:0; background:var(--bg); color:var(--fg); }
#root { height:100vh; padding:12px 16px; display:flex; flex-direction:column; gap:10px; }
header,.controls,.tabs,.tag-list,.foot { display:flex; gap:7px; align-items:center; flex-wrap:wrap; flex:none; }
h2 { margin:0; font-size:20px; letter-spacing:-.025em; } header .count { margin-right:auto; }
button,input,select { font:inherit; color:inherit; background:var(--bg); border:1px solid var(--line); border-radius:7px; padding:6px 9px; min-width:0; }
button { cursor:pointer; } button:disabled { opacity:.55; cursor:wait; } button[aria-pressed="true"] { color:var(--accent); border-color:var(--accent); background:color-mix(in srgb,var(--accent) 12%,transparent); }
button:focus-visible,input:focus-visible,select:focus-visible,summary:focus-visible { outline:2px solid var(--accent); outline-offset:2px; }
.controls input { flex:1 1 180px; } .controls select { max-width:100%; flex:1 1 130px; }
.muted,.count,.meta { color:var(--muted); font-size:12px; } .error { color:var(--err); font-size:13px; max-height:70px; overflow:auto; white-space:pre-wrap; }
.error:empty { display:none; } .filters { flex:none; } .filters summary { cursor:pointer; font-size:13px; }
.tag-list { max-height:100px; overflow:auto; padding:6px 0; } .tag-list button { font-size:12px; border-radius:20px; }
.list { flex:1; min-height:0; overflow:auto; overscroll-behavior:contain; }
.item { padding:10px 2px; border-bottom:1px solid var(--line); } .heading { display:flex; align-items:start; gap:10px; }
.heading input { width:18px; height:18px; flex:none; margin:3px 0 0; accent-color:var(--accent); }
.content { min-width:0; flex:1; overflow-wrap:anywhere; } .title { font-size:var(--entry-size,15px); white-space:pre-wrap; } .done .title { text-decoration:line-through; color:var(--muted); }
.step { margin-top:3px; font-size:13px; white-space:pre-wrap; } .meta { margin-top:5px; display:flex; gap:7px; flex-wrap:wrap; }
.meta button { padding:1px 6px; font-size:12px; border-radius:12px; } .detail { margin:6px 0 0 28px; font-size:12px; }
.detail summary { color:var(--muted); cursor:pointer; } .detail p { margin:5px 0; white-space:pre-wrap; overflow-wrap:anywhere; }
.reference { display:block; border:0; text-align:left; color:var(--accent); padding:4px 0; word-break:break-all; }
.foot { justify-content:space-between; } .foot button { font-size:12px; } .empty { padding:22px 0; color:var(--muted); }
`;
document.head.append(style);
const header = el("header");
const count = el("span", "count");
const smaller = el("button", "", "−"),
  larger = el("button", "", "+");
smaller.setAttribute("aria-label", "Уменьшить текст");
larger.setAttribute("aria-label", "Увеличить текст");
function resize(delta) {
  fontSize = Math.max(13, Math.min(19, fontSize + delta));
  root.style.setProperty("--entry-size", fontSize + "px");
  smaller.disabled = fontSize === 13;
  larger.disabled = fontSize === 19;
}
smaller.onclick = () => resize(-2);
larger.onclick = () => resize(2);
header.append(el("h2", "", "Дела"), count, smaller, larger);
const tabs = el("nav", "tabs");
tabs.setAttribute("aria-label", "Чей следующий шаг");
for (const [key, label] of [
  ["", "Все"],
  ["owner", "Мой ход"],
  ["agent", "У Ouroboros"],
  ["other", "Ожидаем"],
]) {
  const b = el("button", "", label);
  b.dataset.turn = key;
  b.setAttribute("aria-pressed", String(key === ""));
  b.onclick = () => {
    if (busy) return;
    selectedTurn = key;
    [...tabs.children].forEach((x) =>
      x.setAttribute("aria-pressed", String(x.dataset.turn === key)),
    );
    changeFilter();
  };
  tabs.append(b);
}
const controls = el("div", "controls");
const search = el("input");
search.type = "search";
search.placeholder = "Найти дело";
search.setAttribute("aria-label", "Поиск дел");
search.oninput = () => {
  clearTimeout(searchTimer);
  searchTimer = setTimeout(changeFilter, 250);
};
const group = el("select");
group.setAttribute("aria-label", "Группа");
group.onchange = changeFilter;
const status = el("select");
status.setAttribute("aria-label", "Статус");
for (const [value, label] of [
  ["open", "Открытые"],
  ["done", "Выполненные"],
  ["all", "Все статусы"],
  ["deleted", "Корзина"],
]) {
  const o = el("option", "", label);
  o.value = value;
  status.append(o);
}
status.onchange = changeFilter;
controls.append(search, group, status);
const filters = el("details", "filters"),
  filterSummary = el("summary", "", "Теги · все");
const tagList = el("div", "tag-list"),
  mode = el("select");
mode.setAttribute("aria-label", "Сочетание тегов");
for (const [value, label] of [
  ["all", "Все выбранные"],
  ["any", "Любой выбранный"],
]) {
  const o = el("option", "", label);
  o.value = value;
  mode.append(o);
}
mode.onchange = changeFilter;
const clear = el("button", "", "Сбросить теги");
clear.onclick = () => {
  selectedTags.clear();
  changeFilter();
};
filters.append(filterSummary, tagList, mode, clear);
const error = el("div", "error");
error.setAttribute("role", "status");
const list = el("div", "list");
const foot = el("div", "foot"),
  more = el("button", "", "Показать ещё"),
  backup = el("button", "", "Скачать копию"),
  retry = el("button", "", "Обновить");
more.hidden = true;
more.onclick = () => {
  loaded += 100;
  refresh();
};
retry.onclick = () => refresh();
backup.onclick = async () => {
  backup.disabled = true;
  try {
    await OuroborosWidget.download(
      "smart-lists-export.json",
      base + "export?filename=smart-lists-export.json",
    );
  } catch (e) {
    error.textContent = e.message;
  } finally {
    backup.disabled = false;
  }
};
foot.append(more, retry, backup);
root.append(header, tabs, controls, filters, error, list, foot);
function changeFilter() {
  loaded = 100;
  list.scrollTop = 0;
  refresh();
}
function requestId() {
  return (
    globalThis.crypto?.randomUUID?.() ||
    `${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}`
  );
}
async function request(path, init) {
  const r = await OuroborosWidget.fetch(base + path, {
    ...init,
    timeoutMs: 12000,
  });
  if (!r.ok) throw new Error(`Ошибка чтения/записи (${r.status})`);
  const d = await r.json();
  if (!d.ok) {
    const failure = new Error(typeof d.error === "string" ? d.error : d.error?.message || "Список недоступен");
    failure.code = d.code;
    throw failure;
  }
  return d;
}
function params(offset) {
  const p = new URLSearchParams({
    offset: String(offset),
    status: status.value,
    group: group.value,
    query: search.value,
    turn: selectedTurn,
    tag_mode: mode.value,
  });
  for (const t of selectedTags) p.append("tag", t);
  return "board?" + p;
}
function tagButton(tag) {
  const b = el("button", "", tag);
  const key = tag.toLocaleLowerCase();
  b.setAttribute(
    "aria-pressed",
    String([...selectedTags].some((t) => t.toLocaleLowerCase() === key)),
  );
  b.onclick = () => {
    if (busy) return;
    const existing = [...selectedTags].find(
      (t) => t.toLocaleLowerCase() === key,
    );
    existing ? selectedTags.delete(existing) : selectedTags.add(tag);
    changeFilter();
  };
  return b;
}
function row(entry) {
  const d = entry.details || {},
    node = el("article", "item" + (entry.status === "done" ? " done" : ""));
  node.dataset.id = entry.id;
  const heading = el("div", "heading"),
    box = el("input");
  box.type = "checkbox";
  box.checked = entry.status === "done";
  box.disabled = !!entry.deleted_at;
  box.setAttribute("aria-label", `Выполнено: ${entry.text}`);
  box.onchange = async () => {
    if (busy) {
      box.checked = !box.checked;
      return;
    }
    busy = true;
    box.disabled = true;
    try {
      await request("check", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({
          entry_id: entry.id,
          done: box.checked,
          request_id: requestId(),
        }),
      });
      await refresh();
    } catch (e) {
      // Restore the last observed visual state; this is NOT proof of rollback.
      box.checked = entry.status === "done";
      await refresh();
      error.textContent =
        "Не удалось подтвердить отметку. Сначала проверь текущее состояние: " +
        e.message;
    } finally {
      busy = false;
      box.disabled = false;
      more.disabled = false;
    }
  };
  const content = el("div", "content");
  content.append(el("div", "title", entry.text));
  if (d.next_step && entry.status !== "done")
    content.append(el("div", "step", "Дальше: " + d.next_step));
  const meta = el("div", "meta");
  meta.append(el("span", "", entry.group_path));
  if (d.turn && d.turn !== "unassigned" && entry.status !== "done")
    meta.append(el("span", "", turns[d.turn]));
  if (entry.due) meta.append(el("span", "", "Срок: " + entry.due.raw));
  for (const t of d.tags || []) meta.append(tagButton(t));
  content.append(meta);
  heading.append(box, content);
  node.append(heading);
  const detail = el("details", "detail");
  detail.dataset.entry = entry.id;
  detail.append(el("summary", "", "Источники и контекст"));
  if (d.note) detail.append(el("p", "", d.note));
  for (const ref of d.references || []) {
    if (/^https?:\/\//.test(ref.target)) {
      const b = el("button", "reference", ref.label + " ↗");
      b.title = ref.target;
      b.onclick = async () => {
        try {
          if (!OuroborosWidget.openExternal)
            throw new Error(
              "Открытие ссылок недоступно в этом host; адрес показан ниже.",
            );
          await OuroborosWidget.openExternal(ref.target);
        } catch (e) {
          error.textContent = e.message;
        }
      };
      detail.append(b, el("p", "muted", ref.target));
    } else detail.append(el("p", "", ref.label + ": " + ref.target));
  }
  detail.append(
    el(
      "p",
      "muted",
      `${entry.id} · обновлено ${entry.updated_at || "неизвестно"}`,
    ),
  );
  if (!d.note && !(d.references || []).length)
    detail.append(el("p", "muted", "Источник ещё не привязан."));
  node.append(detail);
  return node;
}
function draw(data) {
  const scroll = list.scrollTop,
    expanded = new Set(
      [...list.querySelectorAll("details[open]")].map((n) => n.dataset.entry),
    );
  const focus = document.activeElement;
  const focusedEntry = focus?.closest(".item")?.dataset.id;
  const focusKind = focus?.tagName;
  const focusedTag = focus?.closest(".tag-list") ? focus.textContent : null;
  const priorGroup = group.value;
  group.replaceChildren();
  const all = el("option", "", "Все группы");
  all.value = "";
  group.append(all);
  for (const g of data.filter_groups) {
    const o = el("option", "", g.name);
    o.value = g.id;
    group.append(o);
  }
  group.value = priorGroup;
  const tagScroll = tagList.scrollTop;
  const available = [...new Set([...data.available_tags, ...selectedTags])];
  tagList.replaceChildren(...available.map(tagButton));
  tagList.scrollTop = tagScroll;
  filterSummary.textContent = selectedTags.size
    ? "Теги · " + [...selectedTags].join(", ")
    : "Теги · все";
  list.replaceChildren(...data.entries.map(row));
  for (const n of list.querySelectorAll("details"))
    n.open = expanded.has(n.dataset.entry);
  if (!data.total)
    list.append(
      el(
        "p",
        "empty",
        search.value || selectedTags.size || selectedTurn || group.value
          ? "По этим фильтрам ничего нет."
          : "Здесь пока нет дел. Добавлять и уточнять их можно через чат.",
      ),
    );
  list.scrollTop = scroll;
  if (focusedEntry && (focusKind === "INPUT" || focusKind === "SUMMARY")) {
    const item = [...list.querySelectorAll(".item")].find(
      (n) => n.dataset.id === focusedEntry,
    );
    item
      ?.querySelector(focusKind === "SUMMARY" ? "summary" : "input")
      ?.focus({ preventScroll: true });
  }
  if (focusedTag)
    [...tagList.children]
      .find((n) => n.textContent === focusedTag)
      ?.focus({ preventScroll: true });
  count.textContent = String(data.total);
  more.hidden = data.next_offset >= data.total;
  more.disabled = busy;
  more.textContent = `Показать ещё · ${Math.max(0, data.total - data.next_offset)}`;
}
async function refresh() {
  const mine = ++generation;
  const first = params(0);
  retry.disabled = true;
  try {
    const data = await request(first);
    while (data.next_offset < Math.min(loaded, data.total)) {
      const next = await request(
        first.replace(/offset=0(&|$)/, `offset=${data.next_offset}$1`),
      );
      if (
        next.revision.generation !== data.revision.generation ||
        next.revision.store_id !== data.revision.store_id
      )
        throw new Error("Список изменился при чтении. Нажми «Обновить».");
      if (next.next_offset <= data.next_offset)
        throw new Error("Страница не продвинулась");
      data.entries.push(...next.entries);
      data.next_offset = next.next_offset;
    }
    if (!disposed && mine === generation) {
      current = data;
      draw(data);
      error.textContent = "";
    }
  } catch (e) {
    if (!disposed && mine === generation && e.code === "not_found" && group.value) {
      group.value = "";
      loaded = 100;
      await refresh();
      return;
    }
    if (!disposed && mine === generation)
      error.textContent =
        "Список не обновлён; показанные записи могут быть прежними. " +
        e.message;
  } finally {
    if (mine === generation) retry.disabled = false;
  }
}
refresh();
timer = setInterval(() => {
  if (!busy && !disposed && !retry.disabled && !document.hidden) refresh();
}, 30000);
const unsubscribe = OuroborosWidget.onTheme?.(
  (theme) => (document.documentElement.dataset.theme = theme),
);
window.__ouroWidgetOnDispose?.(() => {
  disposed = true;
  generation++;
  clearInterval(timer);
  clearTimeout(searchTimer);
  unsubscribe?.();
});
