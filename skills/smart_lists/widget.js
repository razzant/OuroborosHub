// A single view over the existing store. The host bridge confines every request to this skill.
const root = document.getElementById('root');
const timezone = Intl.DateTimeFormat().resolvedOptions().timeZone;
const base = '/api/extensions/smart_lists/';
let current = null;
let busy = false;
let disposed = false;
let timer;
let viewRevision = 0;
let activeRefreshes = 0;
let selectedGroup = '';
let fontSize = 15;
const todayUrl = (offset = 0) => `today?timezone=${encodeURIComponent(timezone)}&group=${encodeURIComponent(selectedGroup)}&offset=${offset}`;
const sameSnapshot = (a, b) => a.date === b.date && a.revision?.store_id === b.revision?.store_id
  && a.revision?.generation === b.revision?.generation;

const style = document.createElement('style');
style.textContent = `
  :root { color-scheme: dark; font: var(--list-font-size, 15px)/1.5 system-ui, sans-serif;
    --fg: #e7e8ef; --muted: #aeb4c3; --error: #f3a7a7; --bg: #171b24; }
  :root[data-theme="light"] { color-scheme: light; --fg: #222832; --muted: #58616d; --error: #ae3030; --bg: #fff; }
  @media (prefers-color-scheme: light) {
    :root:not([data-theme]) { color-scheme: light; --fg: #222832; --muted: #58616d; --error: #ae3030; --bg: #fff; }
  }
  * { box-sizing: border-box; }
  body { margin: 0; background: var(--bg); }
  #root { height: 100vh; padding: 12px 16px 16px; color: var(--fg); display: flex; flex-direction: column; }
  .panel { flex: 1 1 auto; min-height: 0; display: flex; flex-direction: column; }
  header { display: flex; flex: none; align-items: baseline; justify-content: space-between; gap: 12px; margin-bottom: 12px; flex-wrap: wrap; }
  h2 { font-size: 20px; font-weight: 650; letter-spacing: -.025em; margin: 0; }
  .date { color: var(--muted); font-size: 12px; white-space: nowrap; }
  .tabs { display: flex; flex: none; gap: 5px; overflow-x: auto; padding: 2px 0 12px; margin-bottom: 6px; }
  .tabs button { flex: none; margin: 0; border-radius: 20px; padding: 5px 12px; }
  .tabs button[aria-pressed="true"] { background: #398b72; color: #fff; border-color: #398b72; }
  .group-title { font-size: 12px; font-weight: 600; color: var(--muted); padding: 10px 10px 2px; }
  .list { display: block; flex: 1 1 auto; min-height: 0; overflow-y: auto; overscroll-behavior: contain; overflow-anchor: none; }
  .item { display: flex; align-items: flex-start; gap: 12px; padding: 9px 10px; border-radius: 10px; }
  .item:hover { background: rgba(127,140,160,.12); }
  .item input { width: 19px; height: 19px; flex: none; margin: 2px 0 0; accent-color: #78bda4; cursor: pointer; }
  .item span { overflow-wrap: anywhere; min-width: 0; }
  .item small { display: block; font-size: .75em; color: var(--muted); }
  .item:has(input:checked) span { text-decoration: line-through; opacity: .75; }
  .muted, .error { padding: 10px; color: var(--muted); }
  #root > .error { flex: none; max-height: 90px; overflow-y: auto; }
  .error { color: var(--error); white-space: pre-wrap; }
  details { border-top: 1px solid rgba(155,165,180,.25); margin-top: 8px; padding: 8px 2px 0; max-height: 120px; overflow-y: auto; flex: none; }
  .size-controls { display: flex; align-items: center; gap: 4px; }
  .size-controls button { margin: 0; padding: 1px 8px; }
  .size-controls output { min-width: 3ch; text-align: center; color: var(--muted); }
  summary { cursor: pointer; color: var(--muted); font-size: 13px; }
  button { border: 1px solid rgba(155,165,180,.4); border-radius: 8px; background: transparent; color: inherit; padding: 7px 12px; cursor: pointer; margin: 10px 0; }
  button:disabled { cursor: wait; opacity: .6; }
`;
document.head.append(style);

function element(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}
function requestId() {
  // randomUUID is unavailable on plain-HTTP LAN installations. This is a
  // replay key, not an authentication secret; no click is retried blindly.
  return globalThis.crypto?.randomUUID?.() || `${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}`;
}
async function request(path, init) {
  const response = await OuroborosWidget.fetch(base + path, {...init, timeoutMs: 12000});
  if (!response.ok) throw new Error(`List request failed (${response.status})`);
  const result = await response.json();
  if (!result.ok) {
    const error = new Error(result.error || 'List unavailable');
    error.definiteRefusal = true;
    throw error;
  }
  return result;
}
function row(entry, done, showGroup = true) {
  const label = element('label', 'item');
  const box = element('input');
  box.type = 'checkbox'; box.checked = done;
  box.dataset.entryId = entry.id;
  box.setAttribute('aria-label', `${done ? 'Вернуть в дела' : 'Отметить выполненным'}: ${entry.item}`);
  const text = element('span', '', entry.item);
  if ((showGroup && entry.group) || entry.due)
    text.append(element('small', '', [showGroup ? entry.group : '', entry.due].filter(Boolean).join(' · ')));
  label.append(box, text);
  box.addEventListener('change', async () => {
    if (busy) { box.checked = !box.checked; return; }
    busy = true; box.disabled = true;
    const requested = box.checked;
    try {
      await request('check', {method: 'POST', headers: {'content-type': 'application/json'},
        body: JSON.stringify({entry_id: entry.id, done: requested, request_id: requestId()})});
      await refresh();
    } catch (error) {
      if (error.definiteRefusal) box.checked = !requested;
      else await refresh(); // A lost reply may have committed. Read, never guess.
      showError(error.definiteRefusal ? error : new Error('Исход отметки неизвестен; проверь список перед повтором.'));
    } finally { busy = false; box.disabled = false; }
  });
  return label;
}
function showError(error) {
  let node = root.querySelector('.error');
  if (!node) { node = element('p', 'error'); root.prepend(node); }
  node.textContent = error.message || String(error);
}
function draw(data) {
  const archiveWasOpen = root.querySelector('details.archive')?.open;
  const listScrollTop = root.querySelector('.list')?.scrollTop || 0;
  const tabsScrollLeft = root.querySelector('.tabs')?.scrollLeft || 0;
  const focusedId = document.activeElement?.dataset?.entryId;
  const focusedControl = document.activeElement?.dataset?.widgetFocus;
  const panel = element('section', 'panel');
  const head = element('header');
  const sizeControls = element('div', 'size-controls');
  const sizeOutput = element('output');
  const buttons = [];
  for (const [label, step] of [['−', -2], ['+', 2]]) {
    const button = element('button', '', label);
    button.type = 'button';
    button.dataset.widgetFocus = step < 0 ? 'smaller' : 'larger';
    button.setAttribute('aria-label', step < 0 ? 'Уменьшить текст списка' : 'Увеличить текст списка');
    button.addEventListener('click', () => {
      const scroll = root.querySelector('.list');
      const scrollTop = scroll?.scrollTop || 0;
      fontSize += step;
      document.documentElement.style.setProperty('--list-font-size', `${fontSize}px`);
      if (scroll) scroll.scrollTop = scrollTop;
      sizeOutput.textContent = `${fontSize} px`;
      for (const [control, delta] of buttons) control.disabled = fontSize + delta < 13 || fontSize + delta > 19;
    });
    buttons.push([button, step]);
  }
  for (const [button, step] of buttons) button.disabled = fontSize + step < 13 || fontSize + step > 19;
  sizeOutput.textContent = `${fontSize} px`;
  sizeControls.append(buttons[0][0], sizeOutput, buttons[1][0]);
  head.append(element('h2', '', 'Сегодня'), sizeControls, element('span', 'date', `${data.date} · ${data.timezone}`));
  panel.append(head);
  const tabs = element('nav', 'tabs');
  tabs.setAttribute('aria-label', 'Списки');
  for (const group of [{id: '', name: 'Все'}, ...data.groups]) {
    const tab = element('button', '', group.name);
    tab.type = 'button';
    tab.dataset.widgetFocus = `tab:${group.id}`;
    tab.setAttribute('aria-pressed', String(selectedGroup === group.id));
    tab.addEventListener('click', () => {
      if (selectedGroup === group.id || busy) return;
      selectedGroup = group.id;
      root.querySelector('.list')?.scrollTo(0, 0);
      current = null;
      refresh();
    });
    tabs.append(tab);
  }
  panel.append(tabs);
  const list = element('div', 'list');
  appendRows(list, data.rows);
  if (!data.total) list.append(element('p', 'muted', 'Здесь пока пусто. Просто напиши мне, что добавить.'));
  panel.append(list);
  if (data.next_offset < data.total) {
    const more = element('button', '', `Показать ещё · ${data.total - data.next_offset}`);
    more.dataset.widgetFocus = 'more';
    more.addEventListener('click', () => loadMore(more, list));
    panel.append(more);
  }
  if (data.archive_total) {
    const archive = element('details', 'archive');
    archive.open = !!archiveWasOpen;
    const summary = element('summary', '', `Архив · ${data.archive_total}`);
    summary.dataset.widgetFocus = 'archive';
    archive.append(summary);
    for (const entry of data.archive) archive.append(row(entry, true));
    if (data.archive_total > data.archive.length)
      archive.append(element('p', 'muted', 'Остальные завершённые записи доступны через чат.'));
    panel.append(archive);
  }
  const backup = element('button', 'backup', 'Скачать резервную копию');
  backup.dataset.widgetFocus = 'backup';
  backup.addEventListener('click', async () => {
    backup.disabled = true;
    try {
      await OuroborosWidget.download('smart-lists-export.json', base + 'export?filename=smart-lists-export.json');
      await refresh();
    } catch (error) { showError(error); }
    finally { backup.disabled = false; }
  });
  panel.append(backup);
  if (data.export_warning) panel.append(element('p', 'error', data.export_warning));
  root.replaceChildren(panel);
  tabs.scrollLeft = tabsScrollLeft;
  list.scrollTop = listScrollTop;
  if (focusedId) [...root.querySelectorAll('input[data-entry-id]')].find(el => el.dataset.entryId === focusedId)?.focus({preventScroll: true});
  else if (focusedControl) [...root.querySelectorAll('[data-widget-focus]')]
    .find(el => el.dataset.widgetFocus === focusedControl)?.focus({preventScroll: true});
}
function appendRows(list, rows) {
  const selectedName = current?.groups?.find(g => g.id === selectedGroup)?.name;
  for (const entry of rows) {
    const group = entry.group || '';
    if (group && list.lastElementChild?.dataset.groupPath !== group) {
      const label = selectedName
        ? (group === selectedName ? 'Общие' : group.slice(selectedName.length + 3)) : group;
      const heading = element('div', 'group-title', label);
      heading.dataset.groupPath = group;
      list.append(heading);
    }
    const item = row(entry, entry.done, false);
    item.dataset.groupPath = group;
    list.append(item);
  }
}
async function loadMore(button, list) {
  if (busy || !current) return;
  const revision = ++viewRevision; // Invalidate any older refresh in flight.
  const snapshot = current;
  busy = true; button.disabled = true;
  try {
    const next = await request(todayUrl(snapshot.next_offset));
    if (!sameSnapshot(next, snapshot)) { await refresh(); return; }
    if (!disposed && revision === viewRevision && current === snapshot && list.isConnected) {
      appendRows(list, next.rows);
      current.next_offset = next.next_offset;
      current.total = next.total;
      if (current.next_offset >= next.total) button.remove();
      else button.textContent = `Показать ещё · ${next.total - current.next_offset}`;
    }
  } catch (error) { showError(error); }
  finally { busy = false; button.disabled = false; }
}
async function refresh() {
  const revision = ++viewRevision;
  const previouslyLoaded = current?.next_offset || 100;
  activeRefreshes++;
  try {
    for (let attempt = 0; attempt < 2; attempt++) {
      const data = await request(todayUrl());
      let changed = false;
      while (!disposed && revision === viewRevision && data.next_offset < Math.min(previouslyLoaded, data.total)) {
        const next = await request(todayUrl(data.next_offset));
        if (!sameSnapshot(next, data)) { changed = true; break; }
        if (next.next_offset <= data.next_offset) throw new Error('Невозможно продолжить список: страница не продвинулась');
        data.rows.push(...next.rows);
        data.next_offset = next.next_offset;
        data.total = next.total;
      }
      if (changed) continue;
      if (!disposed && revision === viewRevision) { current = data; draw(data); }
      return;
    }
    throw new Error('Список менялся во время обновления; попробуй ещё раз.');
  } catch (error) {
    if (!disposed && revision === viewRevision && selectedGroup && error.definiteRefusal) {
      try {
        const missingGroup = selectedGroup;
        const all = await request(`today?timezone=${encodeURIComponent(timezone)}&group=&offset=0`);
        if (!disposed && revision === viewRevision && selectedGroup === missingGroup &&
            !all.groups.some(group => group.id === missingGroup)) {
          selectedGroup = '';
          current = null;
          await refresh();
          return;
        }
      } catch (_) { /* Preserve the original refusal below. */ }
    }
    if (!disposed && revision === viewRevision) showError(error);
  }
  finally { activeRefreshes--; }
}
refresh();
timer = setInterval(() => { if (!busy && !disposed && !activeRefreshes) refresh(); }, 30000);
const unsubscribeTheme = OuroborosWidget.onTheme?.((theme) => { document.documentElement.dataset.theme = theme; });
window.__ouroWidgetOnDispose?.(() => { disposed = true; clearInterval(timer); unsubscribeTheme?.(); });
