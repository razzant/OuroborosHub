/* Token usage and cache health: the selected dashboard D, using exact server intervals. */
(function () {
  'use strict';
  const root = document.getElementById('root');
  if (!root) return;
  const nf = new Intl.NumberFormat('en-US');
  const zone = Intl.DateTimeFormat().resolvedOptions().timeZone || 'UTC';
  const fmt = n => n == null ? '—' : n >= 1e9 ? (n / 1e9).toFixed(2) + ' B' : n >= 1e6 ? (n / 1e6).toFixed(1) + ' M' : n >= 1e3 ? (n / 1e3).toFixed(1) + ' K' : nf.format(n);
  const pct = n => n == null ? '—' : n.toFixed(1) + '%';
  const esc = x => String(x == null ? '' : x).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const clock = ts => new Intl.DateTimeFormat('en-GB', {timeZone: zone, hour:'2-digit', minute:'2-digit'}).format(new Date(ts));
  const dateKey = ts => new Intl.DateTimeFormat('en-CA', {timeZone: zone, year:'numeric', month:'2-digit', day:'2-digit'}).format(new Date(ts));
  const dayLabel = day => new Date(day + 'T12:00:00Z').toLocaleDateString('en-GB', {timeZone:'UTC', day:'numeric', month:'short'});
  const timestamp = ts => dayLabel(dateKey(ts)) + ' ' + clock(ts);
  const icons = {
    refresh:'<path d="M20 7v5h-5M4 17v-5h5"/><path d="M6 7a7 7 0 0 1 11.5-1.5L20 9M4 15l2.5 3.5A7 7 0 0 0 18 17"/>',
    close:'<path d="m6 6 12 12M6 18 18 6"/>',
  };
  const icon = name => '<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" aria-hidden="true">'+icons[name]+'</svg>';
  const presets = [['1H','1h'],['6H','6h'],['24H','24h'],['7D','Week'],['MONTH','Month'],['ALL','All']];
  const state = {range:'24H', committed:null, pending:null, failed:null, snapshot:null, partial:null, selected:null,
    from:'', to:'', customOpen:false, models:false, coverage:false, status:'loading', message:'', page:0};
  let detail = null, requestSeq = 0, detailSeq = 0, disposed = false, pendingTimer = 0;
  let lastHeight = 0, lastWidth = 0, renderFrame = 0;
  const style = document.createElement('style');
  style.textContent = `
:root{color-scheme:dark;--ink:#ededf1;--meta:#aaa7b2;--surface:#202023;--tray:#2a2830;--capsule:rgba(255,255,255,.07);--line:rgba(228,228,231,.14);--rose:#e9687c;--blue:#60a5fa;--write:#bfdbfe;--mint:#40d9a2;--amber:#f4bd45;--focus:#ff9eaa;--shadow:0 1px 2px rgba(0,0,0,.3),0 8px 22px rgba(0,0,0,.15)}
:root[data-theme=light]{color-scheme:light;--ink:#25262b;--meta:#61616c;--surface:#fff;--tray:#f3f2f6;--capsule:#f2f1f4;--line:rgba(38,38,43,.14);--rose:#bb2850;--blue:#2e68d0;--write:#172554;--mint:#178e70;--amber:#8a6100;--focus:#a3243b;--shadow:0 1px 2px rgba(28,33,51,.05),0 7px 18px rgba(28,33,51,.07)}
*{box-sizing:border-box}html{overflow:hidden}html,body{margin:0;padding:0;min-height:0;background:transparent;color:var(--ink);font:14px/1.5 -apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif}
button,select,input{font:inherit;color:inherit}button{border:0;cursor:pointer;background:var(--capsule);border-radius:999px;min-height:36px;padding:7px 12px;display:inline-flex;align-items:center;justify-content:center;gap:6px;white-space:nowrap}button:hover{background:var(--tray)}button:focus-visible,input:focus-visible,summary:focus-visible,.hit:focus-visible{outline:2px solid var(--focus);outline-offset:2px}button:disabled{opacity:.5;cursor:default}button.icon{width:38px;padding:0}
.dash{padding:12px 14px 8px}.top{display:flex;gap:10px;align-items:center;justify-content:space-between;flex-wrap:wrap}.title{font-size:16px;font-weight:600}.controls{display:flex;gap:6px;flex-wrap:wrap;align-items:center}.range{display:flex;gap:2px;flex-wrap:wrap}.range button{min-height:36px;padding:5px 10px}.range button[aria-pressed=true]{background:var(--rose);color:#fff}.custom{display:flex;gap:8px;align-items:end;flex-wrap:wrap;margin-top:10px}.custom[hidden]{display:none}.custom label{display:grid;gap:4px;flex:1;min-width:120px}.custom input{width:100%;min-width:0;border:1px solid var(--line);background:var(--surface);border-radius:9px;padding:7px 8px;min-height:38px;font-size:12px}.scope{margin:8px 0 12px}.meta{color:var(--meta);font-size:12px}.notice{background:var(--tray);border-radius:12px;padding:9px 12px;margin-bottom:12px;font-size:12px;display:flex;justify-content:space-between;gap:8px;align-items:center}.notice.error{color:var(--rose)}.kpis{display:grid;grid-template-columns:repeat(5,minmax(0,1fr));gap:8px;margin:0 0 12px}.kpi{background:var(--surface);border-radius:12px;padding:10px 11px;min-width:0;box-shadow:var(--shadow)}.kpi b{display:block;font-size:24px;line-height:1.3;font-variant-numeric:tabular-nums}.kpi span{display:block;margin-top:4px;color:var(--meta);font-size:12px;overflow-wrap:anywhere}
.panels{display:grid;grid-template-columns:minmax(0,1.65fr) minmax(220px,.85fr);gap:12px;align-items:start}.panel{background:var(--surface);border-radius:14px;padding:12px;min-width:0}.panel h2{font-size:14px;margin:0 0 8px;font-weight:600}.panel-head{display:flex;justify-content:space-between;gap:8px;align-items:center}.chart{width:100%;overflow:hidden}.chart svg{display:block;width:100%;height:auto}.axis{fill:var(--meta);font-size:12px}.grid{stroke:var(--line);stroke-dasharray:2 5}.bar-cache{fill:var(--blue)}.bar-write{fill:var(--write)}.bar-fresh{fill:var(--rose)}.bar-unknown{fill:url(#tokenUnknown)}.point{fill:var(--mint)}.code-mark{stroke:var(--meta);stroke-dasharray:3 4;stroke-width:1}.hit{cursor:pointer}.hit:hover .hitbox,.hit.sel .hitbox{fill:rgba(233,104,124,.13)}.legend{display:flex;flex-wrap:wrap;gap:5px 13px;color:var(--meta);font-size:12px;margin-top:7px}.legend i{display:inline-block;width:8px;height:8px;border-radius:2px;background:var(--blue);margin-right:5px}.legend i.write{background:var(--write)}.legend i.fresh{background:var(--rose)}.legend i.unknown{background:repeating-linear-gradient(135deg,transparent,transparent 2px,var(--amber) 2px,var(--amber) 3px);border:1px solid var(--amber)}.rowline{display:grid;grid-template-columns:minmax(0,1fr) auto;gap:4px 8px;padding:8px 0;border-top:1px solid var(--line)}.name{overflow-wrap:anywhere}.num{text-align:right;font-variant-numeric:tabular-nums}.sub{color:var(--meta);font-size:12px}.sub.right{text-align:right}.fold{margin-top:10px;border-top:1px solid var(--line);padding-top:6px}.fold summary{cursor:pointer;color:var(--meta);min-height:36px;padding:6px 0}.fold p{color:var(--meta);font-size:12px;margin:8px 0}.inspector{margin-top:10px;background:var(--tray);border-radius:12px;padding:12px}.inspector-head{display:flex;align-items:center;justify-content:space-between;gap:8px;margin-bottom:8px}.inspector-head b{font-size:14px}.hourly{display:flex;gap:2px;height:60px;align-items:end;margin:12px 0 4px}.hourly i{display:block;flex:1;min-width:1px;background:var(--blue);border-radius:2px 2px 0 0}.mini-labels,.pager,.footer{display:flex;justify-content:space-between;gap:8px;align-items:center;flex-wrap:wrap}.mini-labels{color:var(--meta);font-size:12px}.pager{margin-top:8px}.pager button{min-height:32px}.footer{margin-top:12px}.footer button{background:none;box-shadow:none;padding:4px 0;color:var(--meta)}.empty{color:var(--meta);padding:22px 0;text-align:center}
@media(max-width:520px){.panels{grid-template-columns:minmax(0,1fr)}}
@media(max-width:800px){.kpis{grid-template-columns:repeat(2,minmax(0,1fr))}.kpi:last-child{grid-column:1/-1}}
@media(max-width:640px){.dash{padding:9px 10px 6px}.controls{flex:1 0 100%}.range{flex:1}.range button{min-height:44px;padding:5px 9px}.kpis{gap:6px}.kpi{padding:9px}.kpi b{font-size:24px}.panel{padding:10px}.custom input,button.icon,summary,.footer button{min-height:44px}.scope{margin-bottom:10px}}
`;
  document.head.append(style);
  function theme(value) {document.documentElement.dataset.theme = value === 'light' ? 'light' : 'dark';}
  theme(window.TOKEN_DASH_THEME || 'dark');
  function problem(data) {
    const q = data.quality || {}, h = q.history_index || {};
    return data.status === 'degraded' || q.read_error || q.ledger_read_error || h.state === 'degraded' || h.state === 'unavailable'
      ? (data.read_error || q.read_error || q.ledger_read_error || h.read_error || 'Incomplete reading') : '';
  }
  function reading() {return state.partial || state.snapshot;}
  function queryString(selection) {
    const q = new URLSearchParams({timeframe: selection.range, timezone: zone});
    if (['7D','MONTH','ALL','CUSTOM'].includes(selection.range)) q.set('daily','1');
    if (selection.range === 'CUSTOM') {q.set('from',selection.from);q.set('to',selection.to);}
    return q;
  }
  async function getData(query) {
    const response = await fetch('/api/extensions/cache_efficiency_snapshot/data?' + query);
    if (!response.ok) throw new Error('HTTP ' + response.status);
    const data = await response.json();
    return data;
  }
  async function load(selection) {
    if (disposed) return;
    const seq = ++requestSeq;
    clearTimeout(pendingTimer);
    state.pending = selection; state.failed = null; state.status = 'loading'; state.message = ''; render();
    try {
      const data = await getData(queryString(selection));
      if (disposed || seq !== requestSeq) return;
      if (data.status === 'loading') {
        const h = data.quality?.history_index || {};
        state.message = 'Loading available history · ' + nf.format(h.archives_done || 0) + ' / ' + nf.format(h.archives_total || 0) + ' archives';
        render();
        pendingTimer = setTimeout(() => load(selection), 1000);
        return;
      }
      if (!data.summary || !Array.isArray(data.buckets)) throw new Error('Invalid reading');
      const error = problem(data);
      // Every usable reading updates the same selection/detail lifecycle. Partial
      // measurements keep their warning and never replace the successful snapshot.
      state.partial = error ? data : null;
      if (!error) {state.snapshot = data; state.committed = {...selection};}
      state.range = selection.range; state.pending = null;
      state.failed = error ? selection : null;
      state.status = error ? 'failed' : 'ready'; state.message = error;
      if (selection.range === 'CUSTOM') state.customOpen = true;
      if (!state.from) state.from = dateKey(data.start);
      if (!state.to) state.to = dateKey(data.end);
      state.page = Math.min(state.page, Math.max(0, data.buckets.length - 1));
      // A retained date is not a retained reading. Invalidate old in-flight detail
      // before committing the new overview; its current bin is the interim truth.
      ++detailSeq; detail = null;
      const selected = data.buckets.find(b => b.ts === state.selected);
      if (!selected) state.selected = null;
      render();
      if (selected && data.daily) loadDetail(selected);
    } catch (error) {
      if (disposed || seq !== requestSeq) return;
      state.pending = null; state.failed = selection; state.status = 'failed'; state.message = error.message; render();
    }
  }
  function inInterval(marker, bucket) {
    const t = Date.parse(marker.ts);
    return t >= Date.parse(bucket.ts) && t < Date.parse(bucket.end_ts);
  }
  function bucketLabel(b, daily) {return daily ? dayLabel(b.day || dateKey(b.ts)) : clock(b.ts);}
  function intervalLabel(b, daily) {
    return daily ? dayLabel(b.day || dateKey(b.ts)) + (b.partial ? ' · partial day' : '') : timestamp(b.ts) + '–' + timestamp(b.end_ts);
  }
  async function loadDetail(bucket) {
    const seq = ++detailSeq;
    detail = {key:bucket.ts, data:null, error:'', loading:true}; render();
    const q = new URLSearchParams({timeframe:'CUSTOM', from:bucket.ts, to:bucket.end_ts, timezone:zone, granularity:'hour'});
    try {
      const data = await getData(q);
      if (disposed || seq !== detailSeq || state.selected !== bucket.ts) return;
      if (data.status === 'loading') throw new Error('History is still loading; select the interval again shortly');
      if (!data.summary || !Array.isArray(data.buckets)) throw new Error('Invalid detail reading');
      detail = {key:bucket.ts, data, error:problem(data), loading:false}; render();
    } catch (error) {
      if (disposed || seq !== detailSeq) return;
      detail = {key:bucket.ts, data:null, error:error.message, loading:false}; render();
    }
  }
  function pick(key) {
    if (state.selected === key) {state.selected = null; detail = null; ++detailSeq; render(); return;}
    const b = reading()?.buckets.find(b => b.ts === key);
    if (!b) return;
    state.selected = key;
    if (reading().daily) loadDetail(b);
    else {++detailSeq;detail = null;render();}
  }
  function visibleBuckets() {
    const buckets = reading()?.buckets || [];
    const n = Math.max(5, Math.min(30, Math.floor((root.clientWidth || 600) / 27)));
    const last = Math.max(0, buckets.length - state.page);
    return {all:buckets, buckets:buckets.slice(Math.max(0,last-n),last), n};
  }
  function chart(buckets, kind, daily) {
    if (!buckets.length) return '<div class="empty">No recorded usage in this interval</div>';
    const panel = root.querySelector(kind === 'volume' ? '.volume-panel' : '.side-panel');
    const width = Math.max(240, (panel?.clientWidth || root.clientWidth - 28) - 24);
    const height = kind === 'volume' ? 205 : 130, left = 55, top = 12, plotHeight = height-44;
    const band = (width-left-8)/buckets.length, bw = Math.min(38,band*.64);
    const max = kind === 'volume' ? Math.max(1,...buckets.map(b=>b.prompt_tokens || 0)) : 100;
    const magnitude = Math.pow(10, Math.floor(Math.log10(max))), ceiling = Math.ceil(max/magnitude)*magnitude;
    let body = '';
    for (const f of [0,.5,1]) {
      const y = top+(1-f)*plotHeight;
      body += `<line class="grid" x1="${left}" x2="${width-8}" y1="${y}" y2="${y}"/><text class="axis" x="${left-7}" y="${y+4}" text-anchor="end">${kind==='volume'?(f?fmt(ceiling*f):'0'):f*100+'%'}</text>`;
    }
    if (kind==='rate') {
      let connected=false,path='';
      buckets.forEach((b,i)=>{if(b.cache_read_rate==null){connected=false;return;}path+=(connected?' L':' M')+(left+(i+.5)*band)+','+(top+(1-b.cache_read_rate/100)*plotHeight);connected=true;});
      body+=`<path d="${path}" fill="none" stroke="var(--mint)" stroke-width="1.5" pointer-events="none"/>`;
    }
    buckets.forEach((b,i)=>{
      const x = left+i*band+(band-bw)/2, label = bucketLabel(b,daily);
      const cache = (b.eligible_cached_tokens || 0)/ceiling*plotHeight;
      const written = (b.eligible_write_tokens || 0)/ceiling*plotHeight;
      const fresh = (b.uncached_input_tokens || 0)/ceiling*plotHeight;
      const unknown = (b.unclassified_tokens ?? b.unknown_prompt_tokens ?? 0)/ceiling*plotHeight;
      const measuredLabel = b.total_records ? `${fmt(b.prompt_tokens)} input · ${pct(b.cache_read_rate)} cache${b.cache_write_tokens!=null?' · '+fmt(b.cache_write_tokens)+' known cache writes':''}${b.unknown_write_records?' · '+nf.format(b.unknown_write_records)+' unknown writes':''} · ${b.unknown_read_records} unknown reads` : 'No recorded usage';
      body += `<g class="hit ${state.selected===b.ts?'sel':''}" data-pick="${esc(b.ts)}" tabindex="0" role="button" aria-label="${esc(intervalLabel(b,daily)+' · '+measuredLabel)}"><title>${esc(intervalLabel(b,daily)+' · '+measuredLabel)}</title><rect class="hitbox" x="${left+i*band}" y="2" width="${band}" height="${height-4}" rx="5" fill="transparent"/>`;
      if (kind === 'volume') {
        body += `<rect class="bar-unknown" data-tokens="${b.unclassified_tokens ?? b.unknown_prompt_tokens ?? 0}" x="${x}" y="${top+plotHeight-unknown}" width="${bw}" height="${unknown}"/><rect class="bar-cache" x="${x}" y="${top+plotHeight-unknown-cache}" width="${bw}" height="${cache}"/><rect class="bar-write" data-tokens="${b.eligible_write_tokens || 0}" x="${x}" y="${top+plotHeight-unknown-cache-written}" width="${bw}" height="${written}"/><rect class="bar-fresh" x="${x}" y="${top+plotHeight-unknown-cache-written-fresh}" width="${bw}" height="${fresh}" rx="2"/>`;
      } else if (b.cache_read_rate != null) body += `<circle class="point" cx="${x+bw/2}" cy="${top+(1-b.cache_read_rate/100)*plotHeight}" r="3"/>`;
      if (kind==='volume') for(const m of (reading().markers || []).filter(m=>inInterval(m,b))) {
        const mx=left+(i+(Date.parse(m.ts)-Date.parse(b.ts))/(Date.parse(b.end_ts)-Date.parse(b.ts)))*band;
        body+=`<line class="code-mark" x1="${mx}" x2="${mx}" y1="${height-31}" y2="${height-23}" pointer-events="none"/>`;
      }
      // Label density changes; measurement intervals and selectable positions do not.
      if (i===0 || i===buckets.length-1 || i % Math.ceil(buckets.length/5) === 0) body += `<text class="axis" x="${x+bw/2}" y="${height-8}" text-anchor="middle">${esc(label)}</text>`;
      body += '</g>';
    });
    const defs = kind==='volume'?'<defs><pattern id="tokenUnknown" width="6" height="6" patternUnits="userSpaceOnUse"><path d="M0 6 6 0" stroke="var(--amber)" stroke-width="1.5"/></pattern></defs>':'';
    return `<svg viewBox="0 0 ${width} ${height}" role="group" aria-label="${kind==='volume'?'Token volume':'Cache hit rate'}">${defs}${body}</svg>`;
  }
  // A card computed from part of the window's records says how many of them it covers.
  const cover = (s, n) => s && s.total_records && n < s.total_records ? ` · ${nf.format(n)} of ${nf.format(s.total_records)} records` : '';
  function counts(s) {
    return `${fmt(s.cached_tokens)} known cache reads · ${fmt(s.cache_write_tokens)} known cache writes · ${nf.format(s.request_count)} requests · ${nf.format(s.session_count)} sessions · ${nf.format(s.unknown_total_records)} unknown totals · ${nf.format(s.unknown_read_records)} unknown cache reads · ${nf.format(s.unknown_write_records || 0)} unknown cache writes`;
  }
  function modelList(rows) {
    const names=rows.map(r=>r.display_name||r.model);
    return rows.map(r=>`<div class="rowline"><span class="name" title="${esc(r.model)}">${esc(names.filter(n=>n===(r.display_name||r.model)).length>1?r.model:r.display_name||r.model)}</span><span class="num">${fmt(r.prompt_tokens)}</span><span class="sub">${esc(r.provider)} · ${r.kind==='attempt'?'requests':r.kind==='subscription_session'?'sessions':esc(r.kind)} · ${nf.format(r.total_records)} records${r.unknown_read_records?' · '+nf.format(r.unknown_read_records)+' unknown reads':''}${r.cache_write_tokens!=null&&r.unknown_write_records?' · '+nf.format(r.unknown_write_records)+' unknown writes':''}</span><span class="sub right">${pct(r.cache_read_rate)} hit<br>${fmt(r.cached_tokens)} known reads${r.cache_write_tokens!=null?'<br>'+fmt(r.cache_write_tokens)+' known writes':''}</span></div>`).join('');
  }
  function inspector() {
    const snap = reading(), b = snap?.buckets.find(b=>b.ts===state.selected);
    if (!b) return '';
    const s = detail?.key===b.ts && detail.data ? detail.data.summary : b;
    let body = `<div class="meta">${fmt(s.prompt_tokens)} input · ${pct(s.cache_read_rate)} from cache${s.cache_write_tokens!=null?' · '+fmt(s.cache_write_tokens)+' known cache writes':''}<br>${counts(s)}</div>`;
    if (detail?.key===b.ts) {
      if (detail.loading) body += '<p class="meta">Loading hourly detail…</p>';
      else if (detail.error) body += `<p class="meta" role="alert">${detail.data?'Partial detail':'Detail unavailable'}: ${esc(detail.error)}</p>`;
      if (!detail.loading && detail.data) {
        const hours = detail.data.buckets, max = Math.max(1,...hours.map(h=>h.prompt_tokens||0));
        body += '<div class="hourly">'+hours.map(h=>`<i data-ts="${esc(h.ts)}" data-tokens="${h.prompt_tokens==null?'':h.prompt_tokens}" title="${esc(intervalLabel(h,false)+' · '+fmt(h.prompt_tokens)+' input')}" style="height:${(h.prompt_tokens||0)/max*100}%;${h.total_records?'':'background:none;'}"></i>`).join('')+'</div>';
        body += `<div class="mini-labels"><span>${hours[0]?clock(hours[0].ts):''}</span><span>Hourly observations</span><span>${hours.at(-1)?clock(hours.at(-1).end_ts):''}</span></div>`;
        body += modelList(detail.data.models || []);
      }
    }
    const marks = (detail?.key===b.ts && detail.data ? detail.data.markers : snap.markers || []).filter(m=>inInterval(m,b));
    body += '<p class="meta">'+(marks.length?marks.map(m=>'Code start '+esc(m.code)+' at '+esc(clock(m.ts))).join(' · '):'No code change observed in this interval.')+'</p>';
    return `<section class="inspector"><div class="inspector-head"><b>${esc(intervalLabel(b,snap.daily))}</b><button class="icon" data-act="clear" aria-label="Close details">${icon('close')}</button></div>${body}</section>`;
  }
  function coverage(snap) {
    if (!snap) return '';
    const q = snap.quality, h = q.history_index;
    return `<p>${esc(q.coverage)}</p><p>${counts(snap.summary)}. ${nf.format(snap.summary.zero_volume_records)} measured zero-volume records.</p><p>${nf.format(q.models_omitted || 0)} model rows omitted · ${nf.format(q.buckets_omitted || 0)} time bins omitted · ${nf.format(q.markers_omitted || 0)} code marks omitted. Totals cover the full selected interval.</p>${h?`<p>History: ${esc(h.state)} · ${nf.format(h.archives_indexed || 0)} archives indexed.<br>Observed span: ${h.indexed_oldest_ts?esc(timestamp(h.indexed_oldest_ts)):'no recorded usage'} – ${h.indexed_newest_ts?esc(timestamp(h.indexed_newest_ts)):''}.<br>${nf.format(h.retained_sources_absent || 0)} indexed sources no longer on disk; their saved observations are retained.<br>${h.recovery?esc(h.recovery):''}</p>`:''}<p>Unclassified input has a known total but no known class: its cache reads are unknown, or it was not read from the cache and its record reports no cache-write count the input can hold. Cache writes are what a provider reports writing to its cache; uncached input was neither read from it nor written to it. A write reported by a record whose reads are unknown counts among known cache writes, while its input stays unclassified in the bars. Records written by a core older than 7.7.0 may carry 0 for a count their provider did not report; such a 0 counts as reported, so that record's unread input is drawn as uncached input. Tokens only: money is on the core's Costs page, which counts single model calls by their start (not harness sessions), so its token figures differ from these. Empty bins are not proof of zero use. Code marks are observations, not causes. Output tokens are outside this view.</p>`;
  }
  function render() {
    if (disposed) return;
    const active = document.activeElement;
    const focus = !root.contains(active) ? null : active.id ? '#'+CSS.escape(active.id)
      : active.dataset.pick ? '[data-chart="'+active.closest('[data-chart]').dataset.chart+'"] [data-pick="'+CSS.escape(active.dataset.pick)+'"]'
      : active.dataset.act ? '[data-act="'+active.dataset.act+'"]'
      : active.dataset.range ? '[data-range="'+active.dataset.range+'"]'
      : active.tagName==='SUMMARY' ? '[data-fold="'+active.parentElement.dataset.fold+'"] summary' : null;
    const snap = reading(), s = snap?.summary;
    const {all,buckets,n} = visibleBuckets();
    const label = snap ? `${timestamp(snap.start)} – ${timestamp(snap.end)} · ${zone}` : 'Waiting for the first reading';
    const loading = state.status==='loading';
    root.innerHTML = `<article class="dash"><header class="top"><div class="title">Token usage & cache health</div><div class="controls"><div class="range" aria-label="Time range">${presets.map(([value,label])=>`<button data-range="${value}" aria-pressed="${state.range===value}">${label}</button>`).join('')}<button data-range="CUSTOM" aria-pressed="${state.range==='CUSTOM'}">Custom</button></div><button class="icon" data-act="refresh" aria-label="Refresh reading">${icon('refresh')}</button></div></header><div class="custom" ${state.customOpen?'':'hidden'}><label class="meta">From<input id="from" type="date" value="${esc(state.from)}"></label><label class="meta">To<input id="to" type="date" value="${esc(state.to)}"></label><button data-act="apply">Apply range</button></div><div class="scope meta">${esc(label)}</div>${loading?`<div class="notice" role="status">${esc(state.message || 'Updating '+(state.pending?.range||'')+'…')}${snap?' · previous reading retained':''}</div>`:state.status==='failed'?`<div class="notice error" role="alert"><span>${state.partial?'Partial reading':'Update failed'} · ${esc(state.message)}${snap&&!state.partial?' · showing the last reading':''}</span><button data-act="retry">Retry</button></div>`:''}<section class="kpis"><div class="kpi"><b>${fmt(s?.prompt_tokens)}</b><span>input tokens</span></div><div class="kpi"><b>${pct(s?.cache_read_rate)}</b><span>cache hit rate · weighted</span></div><div class="kpi"><b>${fmt(s?.cache_write_tokens)}</b><span>cache writes${cover(s, s ? s.total_records - (s.unknown_write_records || 0) : 0)}</span></div><div class="kpi"><b>${fmt(s?.uncached_input_tokens)}</b><span>uncached input${cover(s, s?.write_records || 0)}</span></div><div class="kpi"><b>${s?nf.format(s.measured_records)+' / '+nf.format(s.total_records):'—'}</b><span>measured records</span></div></section><div class="panels"><section class="panel volume-panel"><div class="panel-head"><h2>Token volume</h2><span class="meta">${snap?.daily?'daily input':'input per interval'}</span></div><div class="chart" data-chart="volume"></div><div class="legend"><span><i></i>Cache reads</span><span><i class="write"></i>Cache writes</span><span><i class="fresh"></i>Uncached input</span><span><i class="unknown"></i>Unclassified</span></div>${all.length>n?`<div class="pager"><button data-act="earlier" ${state.page+n>=all.length?'disabled':''}>Earlier</button><span class="meta">${buckets.length} / ${all.length} intervals</span><button data-act="later" ${state.page===0?'disabled':''}>Later</button></div>`:''}${inspector()}</section><section class="panel side-panel"><div class="panel-head"><h2>Cache hit rate</h2><span class="meta">0–100%</span></div><div class="chart" data-chart="rate"></div><div class="fold"><details data-fold="models" ${state.models?'open':''}><summary>Models & sources</summary>${modelList(snap?.models || [])}</details></div><div class="fold"><details data-fold="coverage" ${state.coverage?'open':''}><summary>Coverage & counting</summary>${coverage(snap)}</details></div></section></div>${s?`<div class="scope meta">${counts(s)}</div>`:''}<footer class="footer"><button data-act="coverage">About the data</button><span class="meta">${snap?(state.partial?'Partial read ':'Last read ')+clock(snap.generated_at):'No successful reading yet'}</span></footer></article>`;
    root.querySelector('[data-chart="volume"]').innerHTML = chart(buckets,'volume',snap?.daily);
    root.querySelector('[data-chart="rate"]').innerHTML = chart(buckets,'rate',snap?.daily);
    root.querySelectorAll('[data-range]').forEach(b=>b.onclick=()=>{
      if (b.dataset.range==='CUSTOM') {state.customOpen=true;render();root.querySelector('#from').focus();return;}
      state.customOpen=false;state.selected=null;detail=null;++detailSeq;state.page=0;load({range:b.dataset.range});
    });
    root.querySelectorAll('[data-fold]').forEach(el=>el.addEventListener('toggle',()=>{state[el.dataset.fold]=el.open;resize();}));
    for (const id of ['from','to']) root.querySelector('#'+id).oninput=e=>{state[id]=e.target.value;};
    root.querySelectorAll('[data-pick]').forEach(el=>{
      el.onclick=()=>pick(el.dataset.pick);
      el.onkeydown=e=>{
        if (e.key==='Enter'||e.key===' ') {e.preventDefault();pick(el.dataset.pick);}
        if (e.key==='ArrowRight'||e.key==='ArrowLeft') {
          e.preventDefault();const index=buckets.findIndex(b=>b.ts===el.dataset.pick), next=buckets[index+(e.key==='ArrowRight'?1:-1)];
          if (next) {const kind=el.closest('[data-chart]').dataset.chart;pick(next.ts);root.querySelector('[data-chart="'+kind+'"] [data-pick="'+CSS.escape(next.ts)+'"]')?.focus();}
        }
      };
    });
    root.querySelectorAll('[data-act]').forEach(b=>b.onclick=()=>{
      const act=b.dataset.act;
      if (act==='clear') {state.selected=null;detail=null;++detailSeq;render();}
      if (act==='coverage') {state.coverage=!state.coverage;render();}
      if (act==='earlier'||act==='later') {state.page=Math.max(0,state.page+(act==='earlier'?n:-n));render();}
      if (act==='refresh'||act==='retry') load(state.failed||state.committed||{range:state.range});
      if (act==='apply') {
        if (!state.from||!state.to||state.from>state.to) {state.status='failed';state.message='Choose a valid date range';render();return;}
        state.selected=null;detail=null;++detailSeq;state.page=0;load({range:'CUSTOM',from:state.from,to:state.to});
      }
    });
    if (focus) root.querySelector(focus)?.focus();
    requestAnimationFrame(resize);
  }
  function resize() {
    if (disposed) return;
    const h=Math.ceil(root.getBoundingClientRect().height+8);
    if (h!==lastHeight) {lastHeight=h;window.OuroborosWidget?.resize?.(h);}
  }
  const offTheme=typeof window.OuroborosWidget?.onTheme==='function'?window.OuroborosWidget.onTheme(t=>theme(t?.theme||t)):null;
  const observer=new ResizeObserver(()=>{
    const w=root.clientWidth;
    if (w!==lastWidth) {lastWidth=w;cancelAnimationFrame(renderFrame);renderFrame=requestAnimationFrame(render);}
    else resize();
  });
  observer.observe(root);
  const timer=setInterval(()=>{if (!disposed&&!state.pending&&!document.hidden) load(state.failed||state.committed||{range:state.range});},30000);
  function dispose(){disposed=true;++requestSeq;++detailSeq;clearInterval(timer);clearTimeout(pendingTimer);cancelAnimationFrame(renderFrame);observer.disconnect();if(typeof offTheme==='function')offTheme();}
  if (typeof window.__ouroWidgetOnDispose==='function') window.__ouroWidgetOnDispose(dispose);
  render();load({range:'24H'});
})();
