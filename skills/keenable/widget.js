/* Keenable source workbench. The host owns its frame, theme and lifecycle. */
(() => {
    'use strict';
    const root = document.getElementById('root');
    const bridge = window.OuroborosWidget;
    const prefix = '/api/extensions/keenable/';
    const style = document.createElement('style');
    const layout = `
        :root { color-scheme:light dark; }
        * { box-sizing:border-box; }
        body { margin:0; color:CanvasText; background:Canvas; }
        #root { padding:var(--space-4,16px); font:var(--type-body,14px)/var(--line-body,1.5) system-ui,sans-serif; color:var(--text-primary,CanvasText); background:var(--bg-primary,Canvas); }
        .kb { --kb-reader-height:32rem; --kb-field-min:150px; min-width:0; }
        .kb:not(.kb-kit) button,.kb:not(.kb-kit) input,.kb:not(.kb-kit) select { font:inherit; }
        .kb button { cursor:pointer; }
        .kb button:disabled { cursor:wait; }
        .kb input,.kb select { min-width:0; max-width:100%; width:100%; }
        .kb input[type=checkbox] { width:auto; }
        .kb h2,.kb h3,.kb p { margin:0; }
        .kb h2 { font-size:var(--type-section,16px); line-height:var(--line-title,1.3); color:var(--text-primary,CanvasText); font-weight:600; }
        .kb h3 { font-size:var(--type-body,14px); color:var(--text-primary,CanvasText); font-weight:600; }
        .kb label,.kb summary,.kb .kb-meta { font-size:var(--type-meta,12px); color:var(--text-meta,CanvasText); }
        .kb .kb-copy { font-size:var(--type-body,14px); color:var(--text-meta,CanvasText); }
        .kb .kb-tabs,.kb .kb-actions,.kb .kb-meta-row { display:flex; flex-wrap:wrap; gap:var(--space-2,8px); align-items:center; }
        .kb .kb-tabs { border-bottom:1px solid var(--surface-border,GrayText); padding-bottom:var(--space-3,12px); margin-bottom:var(--space-4,16px); }
        .kb .kb-tabs [aria-selected=true] { color:var(--text-primary,CanvasText); border-color:var(--focus-accent-border,Highlight); background:var(--accent-dim,ButtonFace); }
        .kb .kb-tabs .kb-meta { margin-left:auto; }
        .kb .kb-stack { display:grid; gap:var(--space-3,12px); min-width:0; }
        .kb .kb-section { margin-top:var(--space-4,16px); }
        .kb .kb-row { display:flex; align-items:end; gap:var(--space-2,8px); flex-wrap:wrap; }
        .kb .kb-row>.ui-field { flex:1 1 240px; }
        .kb .ui-field { display:grid; gap:var(--space-1,4px); min-width:0; }
        .kb:not(.kb-kit) .ui-control { padding:var(--space-2,8px) var(--space-3,12px); border:1px solid var(--surface-border,GrayText); border-radius:var(--radius,8px); color:var(--text-primary,FieldText); background:var(--bg-secondary,Field); }
        .kb:not(.kb-kit) .btn { min-height:36px; padding:var(--space-2,8px) var(--space-3,12px); border:1px solid var(--surface-border,GrayText); border-radius:var(--radius,8px); color:var(--text-primary,ButtonText); background:var(--bg-secondary,ButtonFace); }
        .kb button:focus-visible,.kb summary:focus-visible,.kb a:focus-visible,.kb [tabindex]:focus-visible { outline:2px solid var(--focus-accent-border,Highlight); outline-offset:2px; }
        .kb .ui-control:focus-visible { outline:2px solid var(--focus-accent-border,Highlight); outline-offset:1px; }
        .kb .kb-grid { display:grid; grid-template-columns:repeat(auto-fit,minmax(min(100%,var(--kb-field-min)),1fr)); gap:var(--space-3,12px); margin-top:var(--space-3,12px); }
        .kb summary { cursor:pointer; color:var(--text-primary,CanvasText); }
        .kb .kb-help { font-size:var(--type-meta,12px); color:var(--text-meta,CanvasText); margin-top:var(--space-2,8px); }
        .kb .kb-status { font-size:var(--type-meta,12px); color:var(--text-meta,CanvasText); min-height:1.35em; overflow-wrap:anywhere; }
        .kb .kb-status[data-tone=error] { color:var(--status-error-fg,CanvasText); }
        .kb .kb-notes { border-left:2px solid var(--status-warn-border,GrayText); padding-left:var(--space-3,12px); font-size:var(--type-meta,12px); color:var(--text-meta,CanvasText); }
        .kb .kb-results { display:grid; gap:0; }
        .kb .kb-result { padding:var(--space-4,16px) 0; border-top:1px solid var(--surface-border,GrayText); display:grid; gap:var(--space-2,8px); min-width:0; }
        .kb .kb-result h3,.kb .kb-source { overflow-wrap:anywhere; }
        .kb .kb-result:first-child { border-top:0; }
        .kb .kb-result .kb-meta { font-size:var(--type-meta,12px); color:var(--text-meta,CanvasText); overflow-wrap:anywhere; }
        .kb .kb-snippet { font-size:var(--type-body,14px); color:var(--text-meta,CanvasText); white-space:pre-wrap; overflow-wrap:anywhere; }
        .kb .kb-snippet:not([data-expanded=true]) { display:-webkit-box; -webkit-box-orient:vertical; -webkit-line-clamp:3; overflow:hidden; }
        .kb .kb-more { justify-self:start; }
        .kb a { color:var(--text-primary,LinkText); text-underline-offset:3px; }
        .kb .kb-empty { padding:var(--space-4,16px) 0; display:grid; gap:var(--space-2,8px); }
        .kb .kb-document { max-height:var(--kb-reader-height); overflow:auto; white-space:pre-wrap; overflow-wrap:anywhere; font:var(--type-body,14px)/var(--line-body,1.5) system-ui,sans-serif; color:var(--text-primary,CanvasText); margin:0; padding:var(--space-4,16px); background:var(--ui-card-bg-soft,Canvas); border:1px solid var(--surface-border,GrayText); border-radius:var(--radius,8px); }
        .kb .kb-raw { max-height:20rem; overflow:auto; white-space:pre-wrap; overflow-wrap:anywhere; font-size:var(--type-meta,12px); color:var(--text-meta,CanvasText); }
        .kb .kb-check { display:flex; gap:var(--space-2,8px); align-items:center; }
        .kb [hidden] { display:none!important; }
    `;
    style.textContent = layout;
    document.head.append(style);
    root.classList.add('ouro-ui', 'kb');

    const create = (tag, text, className) => {
        const node = document.createElement(tag);
        if (text !== undefined) node.textContent = text;
        if (className) node.className = className;
        return node;
    };
    const button = (text, action, primary = false) => {
        const node = create(
            'button',
            text,
            `btn ${primary ? 'btn-primary' : 'btn-default'}`
        );
        node.type = 'button';
        node.addEventListener('click', action);
        return node;
    };
    const field = (name, label, type = 'text', placeholder = '') => {
        const wrapper = create('label', undefined, 'ui-field');
        wrapper.append(create('span', label));
        const input = create('input');
        input.className = 'ui-control';
        input.name = name;
        input.type = type;
        if (placeholder) input.placeholder = placeholder;
        wrapper.append(input);
        fields[name] = input;
        return wrapper;
    };
    const status = () => {
        const node = create('p', '', 'kb-status');
        node.setAttribute('role', 'status');
        node.setAttribute('aria-live', 'polite');
        return node;
    };
    const setStatus = (node, text, tone = 'neutral') => {
        node.textContent = text;
        node.dataset.tone = tone;
    };
    const exactUrl = (value) => {
        try {
            const parsed = new URL(String(value));
            return ['http:', 'https:'].includes(parsed.protocol)
                ? String(value)
                : '';
        } catch {
            return '';
        }
    };
    const sourceLink = (url) => {
        if (!exactUrl(url))
            return create(
                'span',
                url || 'Source URL unavailable',
                'kb-meta kb-source'
            );
        const link = create('a', 'Open source');
        link.href = url;
        link.target = '_blank';
        link.rel = 'noopener noreferrer';
        if (typeof bridge?.openExternal === 'function')
            link.addEventListener('click', (event) => {
                event.preventDefault();
                bridge
                    .openExternal(url)
                    .catch((error) =>
                        setStatus(
                            storageStatus,
                            `Could not open source: ${error.message}. The URL remains selectable in Details.`,
                            'error'
                        )
                    );
            });
        return link;
    };
    const fields = {};
    const results = { search: null, page: null, answer: null };
    const touched = new Set();
    const generations = { search: 0, read: 0 };
    const busy = { search: false, read: false };
    const controllers = new Set();
    let view = 'search',
        disposed = false,
        saveTimer,
        returnFocus,
        saveChain = Promise.resolve(),
        themeOff = () => {};

    const tabs = create('div', undefined, 'kb-tabs');
    tabs.setAttribute('role', 'tablist');
    tabs.setAttribute('aria-label', 'Keenable views');
    const searchTab = button('Search', () => showView('search'));
    const readTab = button('Read page', () => showView('read'));
    [searchTab, readTab].forEach((tab, index) => {
        tab.id = `kb-tab-${index}`;
        tab.setAttribute('role', 'tab');
        tab.setAttribute('aria-controls', `kb-panel-${index}`);
        tab.addEventListener('keydown', (event) => {
            if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key))
                return;
            event.preventDefault();
            const next =
                event.key === 'Home'
                    ? searchTab
                    : event.key === 'End'
                      ? readTab
                      : tab === searchTab
                        ? readTab
                        : searchTab;
            showView(next === searchTab ? 'search' : 'read');
            next.focus();
        });
    });
    tabs.append(searchTab, readTab, create('span', 'Key optional', 'kb-meta'));
    root.append(tabs);
    const searchPanel = create('section', undefined, 'kb-stack');
    searchPanel.id = 'kb-panel-0';
    searchPanel.setAttribute('role', 'tabpanel');
    searchPanel.setAttribute('aria-labelledby', searchTab.id);
    const readPanel = create('section', undefined, 'kb-stack');
    readPanel.id = 'kb-panel-1';
    readPanel.setAttribute('role', 'tabpanel');
    readPanel.setAttribute('aria-labelledby', readTab.id);
    root.append(searchPanel, readPanel);
    const searchIntro = create(
        'p',
        'Find a source. Describe the page you need, then read the promising results.',
        'kb-copy'
    );
    const queryRow = create('div', undefined, 'kb-row');
    queryRow.append(
        field(
            'query',
            'What are you looking for?',
            'text',
            'e.g. Official Python 3.14 pathlib documentation in English'
        )
    );
    const searchButton = button('Search', runSearch, true);
    queryRow.append(searchButton);
    searchPanel.append(searchIntro, queryRow);
    const filters = create('details');
    const filtersSummary = create('summary', 'Filters');
    filters.append(filtersSummary);
    const filterGrid = create('div', undefined, 'kb-grid');
    filterGrid.append(
        field('site', 'Site', 'text', 'example.com (includes subdomains)'),
        field('published_after', 'Published after', 'date'),
        field('published_before', 'Published before', 'date'),
        field('acquired_after', 'Indexed after', 'date'),
        field('acquired_before', 'Indexed before', 'date')
    );
    const modeLabel = create('label', undefined, 'ui-field');
    modeLabel.append(create('span', 'Search mode'));
    const mode = create('select');
    mode.className = 'ui-control';
    mode.name = 'mode';
    for (const [value, label] of [
        ['pro', 'Pro'],
        ['realtime', 'Realtime']
    ]) {
        const option = create('option', label);
        option.value = value;
        mode.append(option);
    }
    fields.mode = mode;
    modeLabel.append(mode);
    filterGrid.append(modeLabel);
    const clearFilters = button('Clear filters', () => {
        clearFilterValues();
        remember();
    });
    filters.append(
        filterGrid,
        create(
            'p',
            'Dates come from vendor metadata and may be missing or inaccurate.',
            'kb-help'
        ),
        clearFilters
    );
    searchPanel.append(filters);
    const searchStatus = status();
    searchPanel.append(searchStatus);
    const searchOutput = create('section', undefined, 'kb-stack');
    searchPanel.append(searchOutput);

    const readerHeading = create('h2', 'Read a page');
    readerHeading.tabIndex = -1;
    const readerHead = create('div', undefined, 'kb-meta-row');
    const backButton = button('Back to results', () => {
        showView('search');
        returnFocus?.focus();
    });
    readerHead.append(backButton, readerHeading);
    readPanel.append(readerHead);
    const urlRow = create('div', undefined, 'kb-row');
    urlRow.append(
        field('url', 'Page URL', 'url', 'https://example.com/article')
    );
    const readButton = button('Read page', () => runRead(false), true);
    readButton.dataset.readAction = '';
    urlRow.append(readButton);
    readPanel.append(urlRow);
    const liveLabel = create('label', undefined, 'kb-check kb-meta');
    const live = create('input');
    live.type = 'checkbox';
    live.className = 'ui-checkbox';
    live.name = 'live';
    fields.live = live;
    liveLabel.append(
        live,
        create('span', 'Fetch live, bypassing the indexed copy')
    );
    readPanel.append(liveLabel);
    const advanced = create('details');
    advanced.append(create('summary', 'Reading options'));
    const advancedGrid = create('div', undefined, 'kb-grid');
    advancedGrid.append(
        field('max_chars', 'Page text limit (characters)', 'number')
    );
    fields.max_chars.value = '8000';
    fields.max_chars.min = '1';
    fields.max_chars.max = '9500';
    advanced.append(
        advancedGrid,
        create(
            'p',
            'Up to 9,500 characters per call. A focused question can retrieve a relevant passage beyond the excerpt.',
            'kb-help'
        )
    );
    readPanel.append(advanced);
    const readStatus = status();
    readPanel.append(readStatus);
    const pageOutput = create('section', undefined, 'kb-stack');
    readPanel.append(pageOutput);
    const questionRow = create('div', undefined, 'kb-row kb-section');
    questionRow.append(
        field(
            'question',
            'Ask about this page',
            'text',
            'e.g. Which parameters does Path.read_text accept?'
        )
    );
    const askButton = button('Ask', () => runRead(true));
    askButton.dataset.readAction = '';
    questionRow.append(askButton);
    readPanel.append(
        questionRow,
        create(
            'p',
            "Keenable's model answers from the page. Verify important claims against the source.",
            'kb-meta'
        )
    );
    const answerOutput = create('section', undefined, 'kb-stack');
    readPanel.append(answerOutput);
    const storageStatus = status();
    storageStatus.classList.add('kb-section');
    root.append(storageStatus);

    async function request(route, data, { track = true } = {}) {
        const controller = new AbortController();
        if (track) controllers.add(controller);
        try {
            const response = await bridge.fetch(prefix + route, {
                method: data === undefined ? 'GET' : 'POST',
                headers:
                    data === undefined
                        ? {}
                        : { 'Content-Type': 'application/json' },
                body: data === undefined ? undefined : JSON.stringify(data),
                signal: controller.signal
            });
            const value = await response.json();
            if (!response.ok)
                throw new Error(
                    value.message || value.error || `HTTP ${response.status}`
                );
            return value;
        } finally {
            controllers.delete(controller);
        }
    }
    function draft() {
        const value = {
            view,
            filter_open: filters.open,
            read_advanced: advanced.open
        };
        Object.entries(fields).forEach(
            ([key, input]) =>
                (value[key] =
                    input.type === 'checkbox' ? input.checked : input.value)
        );
        return value;
    }
    function persist() {
        clearTimeout(saveTimer);
        const value = draft();
        saveChain = saveChain
            .catch(() => {})
            .then(async () => {
                const response = await request(
                    'state',
                    { draft: value },
                    { track: false }
                );
                if (response.ok === false)
                    throw new Error(
                        response.message || 'State could not be saved'
                    );
            })
            .catch((error) => {
                if (!disposed)
                    setStatus(
                        storageStatus,
                        `Draft not saved: ${error.message}`,
                        'error'
                    );
            });
        return saveChain;
    }
    function remember(name) {
        if (name) touched.add(name);
        clearTimeout(saveTimer);
        saveTimer = setTimeout(persist, 300);
    }
    function showView(next, save = true) {
        view = next;
        searchPanel.hidden = next !== 'search';
        readPanel.hidden = next !== 'read';
        [searchTab, readTab].forEach((tab, index) => {
            const selected = (index === 0) === (next === 'search');
            tab.setAttribute('aria-selected', String(selected));
            tab.tabIndex = selected ? 0 : -1;
        });
        if (save) {
            touched.add('view');
            remember();
        }
    }
    function filterSummary() {
        const count = [
            'site',
            'published_after',
            'published_before',
            'acquired_after',
            'acquired_before'
        ].filter((name) => fields[name].value.trim()).length;
        filtersSummary.textContent = `Filters${count ? ` · ${count} active` : ''}${fields.mode.value === 'realtime' ? ' · Realtime' : ''}`;
    }
    function clearFilterValues() {
        for (const name of [
            'site',
            'published_after',
            'published_before',
            'acquired_after',
            'acquired_before'
        ]) {
            fields[name].value = '';
            touched.add(name);
        }
        filterSummary();
    }
    function updateBusy(kind, active) {
        busy[kind] = active;
        if (kind === 'search') {
            searchButton.disabled = active;
            searchButton.textContent = active ? 'Searching…' : 'Search';
            searchPanel.setAttribute('aria-busy', String(active));
        } else {
            for (const control of root.querySelectorAll('[data-read-action]'))
                control.disabled = active;
            readButton.textContent = active ? 'Reading…' : 'Read page';
            readPanel.setAttribute('aria-busy', String(active));
        }
    }
    function renderNotes(parent, result) {
        const notes = [...(result.ui_notes || [])];
        if (result.ui_state_warning) notes.push(result.ui_state_warning);
        if (notes.length) {
            const box = create('div', undefined, 'kb-notes kb-stack');
            notes.forEach((note) => box.append(create('p', note)));
            parent.append(box);
        }
    }
    function details(parent, slot, label = 'Details') {
        const disclosure = create('details');
        disclosure.append(create('summary', label));
        const raw = create('pre', JSON.stringify(slot, null, 2), 'kb-raw');
        disclosure.append(raw);
        parent.append(disclosure);
    }
    function failure(parent, result, retry) {
        const box = create('div', undefined, 'kb-empty');
        box.append(
            create(
                'p',
                result.ui_guidance || result.message || 'The request failed.',
                'kb-copy'
            ),
            button('Retry', retry)
        );
        if (result.ui_state_warning)
            box.append(create('p', result.ui_state_warning, 'kb-meta'));
        parent.append(box);
    }
    async function runSearch() {
        if (busy.search || disposed) return;
        const query = fields.query.value.trim();
        if (!query) {
            setStatus(searchStatus, 'Describe the page you want to find.');
            fields.query.focus();
            return;
        }
        const args = { query, mode: fields.mode.value };
        for (const name of [
            'site',
            'published_after',
            'published_before',
            'acquired_after',
            'acquired_before'
        ])
            if (fields[name].value.trim())
                args[name] = fields[name].value.trim();
        const epoch = ++generations.search;
        updateBusy('search', true);
        setStatus(searchStatus, 'Searching for sources…');
        persist();
        try {
            const result = await request('search', args);
            if (disposed || epoch !== generations.search) return;
            results.search = { request: args, result };
            renderSearch();
            setStatus(
                searchStatus,
                result.ok ? '' : 'Search failed.',
                'neutral'
            );
        } catch (error) {
            if (!disposed) {
                const result = { ok: false, message: error.message };
                results.search = { request: args, result };
                renderSearch();
                setStatus(
                    searchStatus,
                    'Search could not be completed.',
                    'error'
                );
            }
        } finally {
            if (!disposed) updateBusy('search', false);
        }
    }
    function renderSearch() {
        searchOutput.replaceChildren();
        const slot = results.search;
        if (!slot) return;
        const result = slot.result;
        if (!result.ok) {
            failure(searchOutput, result, runSearch);
            details(searchOutput, slot);
            return;
        }
        const headline = create('div', undefined, 'kb-meta-row');
        headline.append(
            create(
                'h2',
                `${result.count ?? (result.results || []).length} sources`
            )
        );
        const newest = result.index_freshness?.newest_acquired_observed;
        if (newest)
            headline.append(
                create(
                    'span',
                    `Newest indexed date returned: ${newest}`,
                    'kb-meta'
                )
            );
        searchOutput.append(headline);
        searchOutput.append(
            create('p', `For “${slot.request.query}”`, 'kb-meta')
        );
        renderNotes(searchOutput, result);
        if (!(result.results || []).length) {
            const empty = create('div', undefined, 'kb-empty');
            empty.append(
                create('h3', 'No source links returned'),
                create(
                    'p',
                    'Try another description, a broader topic or fewer filters. Inspect Details before concluding that a source does not exist.',
                    'kb-copy'
                ),
                button('Clear filters and search again', () => {
                    clearFilterValues();
                    remember();
                    runSearch();
                })
            );
            searchOutput.append(empty);
        }
        const list = create('div', undefined, 'kb-results');
        (result.results || []).forEach((record, index) => {
            const row = create('article', undefined, 'kb-result');
            const title = record.title || 'Untitled source';
            row.append(create('h3', title));
            const url = record.url_truncated ? '' : exactUrl(record.url);
            let domain = '';
            try {
                domain = new URL(url).hostname;
            } catch {}
            const metadata = [
                domain,
                record.published
                    ? `Published (vendor): ${record.published}`
                    : '',
                record.acquired ? `Indexed: ${record.acquired}` : ''
            ]
                .filter(Boolean)
                .join(' · ');
            if (metadata) row.append(create('p', metadata, 'kb-meta'));
            if (record.snippet) {
                const snippet = create('p', record.snippet, 'kb-snippet');
                row.append(snippet);
                const more = button('Show full snippet', () => {
                    const expanded = snippet.dataset.expanded !== 'true';
                    snippet.dataset.expanded = String(expanded);
                    more.textContent = expanded
                        ? 'Show less'
                        : 'Show full snippet';
                    more.setAttribute('aria-expanded', String(expanded));
                });
                more.classList.add('kb-more');
                more.setAttribute('aria-expanded', 'false');
                row.append(more);
            }
            const actions = create('div', undefined, 'kb-actions');
            if (url) {
                const read = button('Read page', () => {
                    returnFocus = read;
                    setUrl(url);
                    showView('read');
                    readerHeading.focus();
                    runRead(false);
                });
                read.setAttribute('aria-label', `Read page: ${title}`);
                read.dataset.resultIndex = String(index);
                read.dataset.readAction = '';
                read.disabled = busy.read;
                actions.append(read, sourceLink(url));
            } else
                actions.append(
                    create(
                        'span',
                        record.url_truncated
                            ? 'Incomplete URL; inspect Details.'
                            : 'No readable page URL returned.',
                        'kb-meta'
                    )
                );
            row.append(actions);
            list.append(row);
        });
        searchOutput.append(list);
        details(searchOutput, slot, 'Search details');
    }
    function setUrl(url) {
        if (fields.url.value !== url) {
            fields.url.value = url;
            generations.read++;
            results.page = null;
            results.answer = null;
            renderReader();
        }
        touched.add('url');
        remember();
    }
    async function runRead(ask, forceLive = false) {
        if (busy.read || disposed) return;
        const url = exactUrl(fields.url.value.trim());
        if (!url) {
            setStatus(readStatus, 'Enter a complete http or https URL.');
            fields.url.focus();
            return;
        }
        const prompt = ask ? fields.question.value.trim() : '';
        if (ask && !prompt) {
            setStatus(readStatus, 'Enter a question about this page.');
            fields.question.focus();
            return;
        }
        if (prompt.length > 2000) {
            setStatus(
                readStatus,
                'Use a question of at most 2,000 characters.'
            );
            return;
        }
        const args = {
            url,
            live: forceLive || fields.live.checked,
            max_chars: fields.max_chars.value || 8000
        };
        if (ask) args.prompt = prompt;
        const epoch = ++generations.read;
        const target = ask ? 'answer' : 'page';
        updateBusy('read', true);
        setStatus(
            readStatus,
            ask ? 'Asking Keenable about this page…' : 'Reading the page…'
        );
        persist();
        try {
            const result = await request('fetch', args);
            if (disposed) return;
            if (epoch !== generations.read || fields.url.value.trim() !== url) {
                setStatus(
                    readStatus,
                    'The earlier page request finished. Read the current URL when ready.'
                );
                return;
            }
            results[target] = { request: args, result };
            renderReader(target);
            setStatus(
                readStatus,
                result.ok
                    ? ask
                        ? 'Answer received.'
                        : 'Page text received.'
                    : 'Page request failed.',
                result.ok ? 'neutral' : 'error'
            );
        } catch (error) {
            if (!disposed && epoch === generations.read) {
                results[target] = {
                    request: args,
                    result: { ok: false, message: error.message }
                };
                renderReader(target);
                setStatus(
                    readStatus,
                    'Page request could not be completed.',
                    'error'
                );
            }
        } finally {
            if (!disposed) updateBusy('read', false);
        }
    }
    async function copyText(node, statusNode) {
        try {
            if (!navigator.clipboard?.writeText)
                throw new Error('Clipboard unavailable');
            await navigator.clipboard.writeText(node.textContent);
            setStatus(statusNode, 'Copied.');
        } catch {
            const range = document.createRange();
            range.selectNodeContents(node);
            const selection = getSelection();
            selection.removeAllRanges();
            selection.addRange(range);
            setStatus(
                statusNode,
                'Text selected. Use your device’s Copy command.'
            );
        }
    }
    function renderDocument(parent, slot, answer) {
        if (!slot) return;
        const result = slot.result;
        if (!result.ok) {
            failure(parent, result, () => runRead(answer));
            details(parent, slot);
            return;
        }
        const heading = create(
            'h3',
            answer
                ? 'Answer generated by Keenable'
                : result.served?.served_title || 'Page text'
        );
        parent.append(heading);
        if (answer) parent.append(create('p', slot.request.prompt, 'kb-meta'));
        else
            parent.append(
                create(
                    'p',
                    slot.request.live
                        ? 'Live fetch · returned Markdown text'
                        : 'Indexed copy, age unknown · returned Markdown text',
                    'kb-meta'
                )
            );
        renderNotes(parent, result);
        const content = create(
            'div',
            result.content || 'No text returned.',
            'kb-document'
        );
        content.tabIndex = 0;
        content.setAttribute('role', 'region');
        content.setAttribute(
            'aria-label',
            answer ? 'Keenable answer' : 'Page text'
        );
        parent.append(content);
        const actions = create('div', undefined, 'kb-actions');
        const copyStatus = status();
        actions.append(
            button(answer ? 'Copy answer' : 'Copy text', () =>
                copyText(content, copyStatus)
            ),
            sourceLink(slot.request.url)
        );
        if (!answer && !slot.request.live) {
            const fetchLive = button('Fetch live', () => {
                fields.live.checked = true;
                touched.add('live');
                runRead(false, true);
            });
            fetchLive.dataset.readAction = '';
            fetchLive.disabled = busy.read;
            actions.append(fetchLive);
        }
        parent.append(actions, copyStatus);
        details(parent, slot, answer ? 'Answer details' : 'Page details');
    }
    function renderReader(target) {
        if (!target || target === 'page') {
            pageOutput.replaceChildren();
            renderDocument(pageOutput, results.page, false);
        }
        if (!target || target === 'answer') {
            answerOutput.replaceChildren();
            renderDocument(answerOutput, results.answer, true);
        }
    }
    for (const [name, input] of Object.entries(fields)) {
        input.addEventListener('input', () => {
            touched.add(name);
            if (name === 'url') {
                generations.read++;
                results.page = null;
                results.answer = null;
                renderReader();
                setStatus(readStatus, '');
            }
            filterSummary();
            remember();
        });
        input.addEventListener('change', () => {
            touched.add(name);
            filterSummary();
            remember();
        });
    }
    for (const [name, action] of [
        ['query', runSearch],
        ['url', () => runRead(false)],
        ['question', () => runRead(true)]
    ])
        fields[name].addEventListener('keydown', (event) => {
            if (
                event.key === 'Enter' &&
                !event.isComposing &&
                !event.repeat &&
                event.keyCode !== 229
            ) {
                event.preventDefault();
                action();
            }
        });
    filters.addEventListener('toggle', () => {
        touched.add('filter_open');
        remember();
    });
    advanced.addEventListener('toggle', () => {
        touched.add('read_advanced');
        remember();
    });
    showView('search', false);
    filterSummary();
    if (typeof bridge?.onTheme === 'function')
        themeOff = bridge.onTheme((theme) => {
            document.documentElement.dataset.theme = theme;
            document.documentElement.style.colorScheme = theme;
        });
    if (typeof window.__ouroWidgetOnDispose === 'function')
        window.__ouroWidgetOnDispose(async () => {
            clearTimeout(saveTimer);
            const saved = persist();
            disposed = true;
            controllers.forEach((controller) => controller.abort());
            themeOff();
            await saved;
        });
    if (!bridge?.fetch) {
        setStatus(
            storageStatus,
            'This host cannot connect module widgets. Update Ouroboros to use this view.',
            'error'
        );
        return;
    }
    // Both requests are optional; fields and actions above are already usable.
    request('author-kit')
        .then((result) => {
            if (disposed) return;
            if (result.ok !== false && typeof result.css === 'string') {
                style.textContent = result.css + '\n' + layout;
                root.classList.add('kb-kit');
            } else
                setStatus(
                    storageStatus,
                    result.message ||
                        'Shared appearance unavailable; native controls remain usable.'
                );
        })
        .catch(() => {
            if (!disposed)
                setStatus(
                    storageStatus,
                    'Shared appearance unavailable; native controls remain usable.'
                );
        });
    request('state')
        .then((saved) => {
            if (disposed) return;
            for (const [name, value] of Object.entries(saved.draft || {}))
                if (!touched.has(name) && fields[name]) {
                    if (fields[name].type === 'checkbox')
                        fields[name].checked = Boolean(value);
                    else fields[name].value = String(value ?? '');
                }
            if (!touched.has('filter_open'))
                filters.open = Boolean(saved.draft?.filter_open);
            if (!touched.has('read_advanced'))
                advanced.open = Boolean(saved.draft?.read_advanced);
            if (
                !touched.has('view') &&
                ['read', 'search'].includes(saved.draft?.view)
            )
                showView(saved.draft.view, false);
            if (generations.search === 0 && saved.results?.search)
                results.search = saved.results.search;
            if (generations.read === 0)
                for (const name of ['page', 'answer']) {
                    const slot = saved.results?.[name];
                    if (slot?.request?.url === fields.url.value)
                        results[name] = slot;
                }
            filterSummary();
            renderSearch();
            renderReader();
        })
        .catch((error) => {
            if (!disposed)
                setStatus(
                    storageStatus,
                    `Previous work could not be restored: ${error.message}`,
                    'error'
                );
        });
})();
