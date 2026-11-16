// Exercise the shipped module in Chromium and WebKit using production-aggregated fixtures.
// PYTHON and PLAYWRIGHT_MODULE select installed tools; no runtime data or network is used.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const {spawnSync} = require('node:child_process');
const pw = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const output = path.resolve(process.argv[2] || 'test-output');
fs.mkdirSync(output, {recursive:true});
const generated = spawnSync(process.env.PYTHON || 'python3', [path.join(__dirname,'test_widget_data.py')], {encoding:'utf8'});
assert.equal(generated.status, 0, generated.stderr);
const fixtures = JSON.parse(generated.stdout);
const script = fs.readFileSync(path.join(__dirname,'widget.js'),'utf8');
const facts = [];
(async()=>{
 for (const engine of ['chromium','webkit']) {
  const browser=await pw[engine].launch();
  // The owner's desktop card (1100x750, dark) and phone (390 px, light) first, then the other two pairings.
  try {for (const [width,height,theme] of [[1100,750,'dark'],[390,844,'light'],[1100,750,'light'],[390,844,'dark']]) {
   const context=await browser.newContext({viewport:{width,height},timezoneId:'Europe/Moscow',hasTouch:width===390,colorScheme:theme});
   const page=await context.newPage(), requests=[], errors=[], shot=async name=>{await page.screenshot({path:path.join(output,`${engine}-${width}-${theme}-${name}.png`),fullPage:true});if(['24h','month-pending-refresh','bare-loading-limit'].includes(name))await page.screenshot({path:path.join(output,`${engine}-${width}-${theme}-${name}-viewport.png`)});};
   let fail=false, historyLoading=false, bareLoading=false, invalidCustom=false, detailFail=false, delay=null, detailDelay=null, refreshed=false, partial=true;
   page.on('pageerror',e=>errors.push(String(e)));
   await page.route('**/*',async route=>{
    const u=new URL(route.request().url());
    if(u.pathname==='/')return route.fulfill({contentType:'text/html',body:'<html><body><div id="root"></div></body></html>'});
    assert.equal(u.pathname,'/api/extensions/cache_efficiency_snapshot/data');
    const q=Object.fromEntries(u.searchParams);requests.push(q);
    if(delay) await delay;
    if(fail)return route.fulfill({status:503,body:'unavailable'});
    if(q.granularity==='hour'&&detailDelay) await detailDelay;
    let body=q.granularity==='hour'?(fixtures.DAY_DETAILS[q.from]||fixtures.DETAIL):fixtures[q.timeframe];
    if(refreshed&&q.timeframe==='CUSTOM')body=fixtures[q.granularity==='hour'?'DETAIL_REFRESH':'CUSTOM_REFRESH'];
    if(detailFail&&q.granularity==='hour')body={...body,quality:{...body.quality,history_index:{state:'degraded',read_error:'fixture_detail'}}};
    if(partial)body={...body,status:'degraded',read_error:'fixture_partial'};
    // A usable reading while history is indexed carries its data; a bare one (nothing published yet) does not.
    if(historyLoading)body={...body,status:'loading',quality:{...body.quality,history_index:{state:'loading',archives_done:3,archives_total:10}}};
    if(bareLoading&&!q.granularity)body={status:'loading',read_error:null,quality:{history_index:{state:'loading',archives_done:0,archives_total:10}}};
    if(invalidCustom&&q.timeframe==='CUSTOM'&&!q.granularity)body={status:'degraded',read_error:'invalid_custom_range'};
    if(q.timeframe==='ALL'&&!historyLoading&&!bareLoading)body={...body,quality:{...body.quality,history_index:{state:'ready',refreshing:false,complete:true,archives_indexed:6604,archives_total:1,archives_done:1,retained_sources_absent:2,restored_sources:2,torn_tails:1,malformed_lines:3,boots_without_code:4,changed_sources_kept_count:1,indexed_oldest_ts:'2026-07-30T02:42:36+00:00',indexed_newest_ts:'2026-11-10T11:23:00+00:00'}}};
    return route.fulfill({contentType:'application/json',body:JSON.stringify(body)});
   });
   await page.goto('http://widget.test/');
   // The host bridge as the widget may use it: theme subscription and the dispose hook (the host measures #root itself).
   // Follow-up reads (>= 1 s timeouts) are held so the test runs them deterministically.
   await page.evaluate(theme=>{window.TOKEN_DASH_THEME=theme;window.OuroborosWidget={onTheme:fn=>{window.changeTheme=fn;return()=>window.offTheme=true;}};window.__ouroWidgetOnDispose=fn=>window.dispose=fn;const schedule=window.setInterval;window.setInterval=(fn,ms)=>{if(ms===30000)window.pollOnce=fn;return schedule(fn,ms);};
     window.followUps=[];let fake=1e6;const later=window.setTimeout,cancel=window.clearTimeout;
     window.setTimeout=(fn,ms,...rest)=>{if(ms>=1000){const id=++fake;window.followUps.push({id,fn,ms});return id;}return later(fn,ms,...rest);};
     window.clearTimeout=id=>{const held=window.followUps.find(f=>f.id===id);if(held)held.cancelled=true;else cancel(id);};
     window.runFollowUp=()=>{const next=window.followUps.filter(f=>!f.cancelled&&!f.ran&&f.ms!==20000).at(-1);if(!next)return null;next.ran=true;next.fn();return next.ms;};},theme);
   await page.addScriptTag({content:script});
   await page.locator('.notice.error').waitFor();
   assert.match(await page.locator('.notice').innerText(),/Partial reading.*fixture_partial/);
   assert.match(await page.locator('.kpi b').first().innerText(),/3.5 K/);
   // Cache writes are their own headline and layer, in tokens only.
   const day=fixtures['24H'].summary, of=n=>` · ${n} of ${day.total_records} records`;
   assert.equal(await page.locator('.kpi span').nth(2).innerText(),'known cache writes'+of(day.total_records-day.unknown_write_records));
   assert.equal(await page.locator('.kpi b').nth(2).innerText(),String(fixtures['24H'].summary.cache_write_tokens));
   assert.equal(await page.locator('.kpi span').nth(3).innerText(),'uncached input'+of(day.write_records));
   assert.equal(await page.locator('.kpi b').nth(3).innerText(),String(fixtures['24H'].summary.uncached_input_tokens));
   assert.match(await page.locator('.legend').first().innerText(),/Cache reads\s+Cache writes\s+Uncached input\s+Unclassified/);
   assert.match(await page.locator('.footer').innerText(),/Partial read/);
   partial=false;await page.evaluate(()=>pollOnce());
   await page.waitForFunction(()=>document.querySelector('.scope')?.textContent.includes('Europe/Moscow')&&!document.querySelector('.notice'));
   await shot('24h');
   const root=page.locator('#root');
   await page.locator('[data-range="1H"]').click();
   await page.waitForFunction(()=>document.querySelector('[data-range="1H"]').getAttribute('aria-pressed')==='true');
   const bars=page.locator('[data-chart="volume"] [data-pick]');
   assert.equal(await bars.count(),12);
   assert.equal(await page.locator('.bar-unknown[data-tokens="2000"]').count(),1);
   assert.equal(await page.locator('.bar-write[data-tokens="20"]').count(),1);
   assert.equal(await page.locator('.bar-write[data-tokens="50"]').count(),1);
   // An interval whose records report no write count says how many, beside its unknown reads.
   assert.equal(await page.locator('[data-chart="volume"] [data-pick][aria-label*="29 unknown writes"]').count(),1);
   assert.match(await page.locator('[data-chart="volume"] [data-pick]').filter({has:page.locator('.bar-write[data-tokens="20"]')}).getAttribute('aria-label'),/20 known cache writes/);
   assert.equal(await page.locator('.code-mark').count(),1);
   assert.match(await root.innerText(),/3 unknown cache reads/);
   // A known write of a record whose reads are unknown is in the text figures, not in the drawn split.
   const unread=page.locator('[data-chart="volume"] [data-pick][aria-label*="30 known cache writes"]');
   assert.equal(await unread.count(),1);
   assert.equal(await unread.locator('.bar-write').getAttribute('data-tokens'),'0');
   assert.equal(await unread.locator('.bar-unknown').getAttribute('data-tokens'),'90');
   const selected=fixtures['1H'].buckets.find(b=>b.unknown_prompt_tokens===2000);
   const requestsBeforeBin=requests.length;
   await page.locator('[data-chart="volume"] [data-pick="'+selected.ts+'"]').click();
   assert.match(await page.locator('.inspector').innerText(),/2.0 K input/);
   assert.equal(requests.length,requestsBeforeBin); // Five-minute inspection needs no day request.
   await page.locator('[data-fold="models"] summary').click();
   await page.waitForFunction(()=>document.querySelector('[data-fold="models"]').open);
   await page.locator('[data-act="refresh"]').click();
   await page.waitForFunction(()=>!document.querySelector('.notice'));
   assert.equal(await page.locator('[data-fold="models"]').evaluate(e=>e.open),true);
   assert.equal(await page.locator('[data-fold="models"] .rowline').count(),25);
   await page.locator('[data-fold="coverage"] summary').click();
   assert.match(await page.locator('[data-fold="coverage"]').innerText(),/5 model rows omitted/);
   const before=await page.locator('.scope').first().innerText(), count=requests.length;
   await page.locator('[data-range="CUSTOM"]').click();
   await page.locator('#from').fill('2026-11-09');await page.locator('#to').fill('2026-11-09');
   assert.equal(requests.length,count); // Editing is not a commit.
   fail=true;await page.locator('[data-act="apply"]').click();
   await page.locator('.notice.error').waitFor();assert.equal(await page.locator('.scope').first().innerText(),before);
   fail=false;await page.evaluate(()=>pollOnce()); // Recover Custom, not the previous 1H.
   await page.waitForFunction(()=>document.querySelector('[data-range="CUSTOM"]').getAttribute('aria-pressed')==='true'&&!document.querySelector('.notice'));
   assert.equal(requests.at(-1).from,'2026-11-09');assert.equal(requests.at(-1).to,'2026-11-09');
   fail=true;await page.locator('[data-act="refresh"]').click();
   await page.locator('.notice.error').waitFor();
   fail=false;await page.locator('[data-act="retry"]').click();
   await page.waitForFunction(()=>!document.querySelector('.notice'));
   const daily=fixtures.CUSTOM.buckets[0];
   await page.locator('[data-chart="volume"] [data-pick]').first().click();
   await page.waitForFunction(()=>document.querySelector('.hourly'));
   assert.equal(requests.at(-1).from,daily.ts);assert.equal(requests.at(-1).to,daily.end_ts);
   assert.equal(requests.at(-1).granularity,'hour');assert.equal(await page.locator('.hourly i').count(),24);
   await shot('custom-day-detail');
   // New overview data invalidates old detail and its pending responses, even for the same date.
   let finishDetail;detailDelay=new Promise(r=>finishDetail=r);refreshed=true;
   await page.locator('[data-act="refresh"]').click();
   await page.waitForFunction(()=>document.querySelector('.inspector')?.textContent.includes('Loading hourly detail'));
   assert.match(await page.locator('.inspector').innerText(),/1.9 K input/);
   finishDetail();detailDelay=null;
   await page.locator('.hourly').waitFor();
   assert.match(await page.locator('.inspector').innerText(),/1.3 K known cache reads/);
   assert.match(await page.locator('.inspector').innerText(),/2 requests/);
   // A degraded overview still refreshes the selected day's usable hourly facts.
   detailDelay=new Promise(r=>finishDetail=r);partial=true;
   await page.evaluate(()=>pollOnce());
   await page.locator('.notice.error').waitFor();
   assert.match(await page.locator('.inspector').innerText(),/Loading hourly detail/);
   assert.match(await page.locator('.inspector').innerText(),/1.9 K input/);
   assert.equal(await page.locator('[data-chart="volume"] [data-pick].sel').getAttribute('data-pick'),daily.ts);
   finishDetail();detailDelay=null;
   await page.waitForFunction(()=>document.querySelector('.inspector')?.textContent.includes('Partial detail'));
   assert.equal(await page.locator('.hourly i').count(),24);
   assert.match(await page.locator('.inspector').innerText(),/1.3 K known cache reads/);
   partial=false;await page.evaluate(()=>pollOnce());
   await page.waitForFunction(()=>!document.querySelector('.notice')&&document.querySelector('.hourly')&&!document.querySelector('.inspector [role="alert"]'));
   refreshed=false;
   await page.locator('[data-act="clear"]').click();detailFail=true;
   await page.locator('[data-chart="volume"] [data-pick]').first().click();
   await page.waitForFunction(()=>document.querySelector('.inspector')?.textContent.includes('fixture_detail'));
   assert.match(await page.locator('.inspector').innerText(),/Partial detail/);
   assert.equal(await page.locator('.hourly i').count(),24);detailFail=false;
   // Month while history is still being indexed: the press shows at once, the usable reading is drawn with its
   // progress, and a bounded follow-up read replaces it when indexing ends.
   const customScope=await page.locator('.scope').first().innerText();
   let releaseMonth;delay=new Promise(r=>releaseMonth=r);historyLoading=true;
   await page.locator('[data-range="MONTH"]').click();
   assert.equal(await page.locator('[data-range="MONTH"]').getAttribute('aria-pressed'),'true');
   assert.equal(await page.locator('[data-range="CUSTOM"]').getAttribute('aria-pressed'),'false');
   assert.match(await page.locator('.notice').innerText(),/Updating Month.*previous reading retained/);
   assert.equal(await page.locator('.scope').first().innerText(),customScope); // the previous reading keeps its dates
   // Refresh keeps the requested Month while the committed snapshot is still Custom.
   const pendingRefresh=page.waitForRequest(r=>new URL(r.url()).searchParams.get('timeframe')==='MONTH');
   await page.locator('[data-act="refresh"]').click();
   await pendingRefresh;
   assert.equal(await page.locator('[data-range="MONTH"]').getAttribute('aria-pressed'),'true');
   await shot('month-pending-refresh');
   releaseMonth();delay=null;
   await page.waitForFunction(()=>document.querySelector('.notice')?.textContent.includes('3 / 10 archives'));
   assert.match(await page.locator('.notice').innerText(),/Indexing history · 3 \/ 10 archives · older days may still be added/);
   assert.notEqual(await page.locator('.scope').first().innerText(),customScope);
   await shot('month-indexing');
   historyLoading=false;assert.equal(await page.evaluate(()=>runFollowUp()),1000);
   await page.waitForFunction(()=>document.querySelector('[data-range="MONTH"]').getAttribute('aria-pressed')==='true'&&!document.querySelector('.notice'));
   const lastDays=fixtures.MONTH.buckets.slice(-3);
   await page.locator('[data-chart="volume"] [data-pick="'+lastDays[0].ts+'"]').press('ArrowRight');
   await page.locator('.hourly').waitFor();
   assert.equal(await page.evaluate(()=>document.activeElement.dataset.pick),lastDays[1].ts);
   await page.keyboard.press('ArrowRight');
   await page.locator('.hourly').waitFor();
   assert.equal(await page.evaluate(()=>document.activeElement.dataset.pick),lastDays[2].ts);
   await page.locator('[data-range="ALL"]').click();
   await page.waitForFunction(()=>document.querySelector('[data-range="ALL"]').getAttribute('aria-pressed')==='true'&&!document.querySelector('.notice'));
   assert.match(await page.locator('.pager').innerText(),/160 intervals/);
   const recent=await page.locator('[data-chart="volume"] [data-pick]').first().getAttribute('data-pick');
   await page.locator('[data-act="earlier"]').click();
   assert.notEqual(await page.locator('[data-chart="volume"] [data-pick]').first().getAttribute('data-pick'),recent);
   assert.match(await page.locator('.scope').nth(1).innerText(),/unconfirmed legacy zeros: 1 cache reads, 1 cache writes/);
   if(!(await page.locator('[data-fold="coverage"]').evaluate(e=>e.open)))await page.locator('[data-fold="coverage"] summary').click();
   const fold=await page.locator('[data-fold="coverage"]').innerText();
   assert.match(fold,/History: ready · 6,604 archives indexed/);assert.match(fold,/2 indexed sources no longer on disk.*2 of them restored from the previous index/);
   assert.match(fold,/1 archive tails without a final newline.*3 malformed usage lines.*4 code starts without a commit.*1 archives changed on disk after indexing/);
   assert.match(fold,/record contract 2/);
   await shot('all');
   // A selection read before is shown at once while its fresh reading is in flight.
   let releaseDay;delay=new Promise(r=>releaseDay=r);
   await page.locator('[data-range="24H"]').click();
   assert.match(await page.locator('.kpi b').first().innerText(),/3.5 K/);
   assert.match(await page.locator('.notice').innerText(),/Updating 24h.*showing the reading from/);
   releaseDay();delay=null;
   await page.waitForFunction(()=>!document.querySelector('.notice'));
   // Nothing usable yet: bounded follow-ups, then the regular cadence with a visible state and Retry.
   bareLoading=true;await page.locator('[data-range="ALL"]').click();
   await page.waitForFunction(()=>document.querySelector('.notice')?.textContent.includes('0 / 10 archives'));
   const delays=[];
   for(let i=0;i<12;i++){
     // Each follow-up's answer schedules the next one, or ends in the waiting state.
     await page.waitForFunction(()=>window.followUps.some(f=>!f.cancelled&&!f.ran&&f.ms!==20000)||document.querySelector('.notice')?.textContent.includes('still running'));
     const ms=await page.evaluate(()=>runFollowUp());if(ms==null)break;delays.push(ms);
   }
   assert.deepEqual(delays,[1000,2000,4000,8000,15000,15000,15000,15000,15000,15000]);
   await page.waitForFunction(()=>document.querySelector('.notice')?.textContent.includes('still running'));
   assert.equal(await page.locator('.notice.error').count(),0); // waiting, not a failure
   assert.equal(await page.locator('[data-act="retry"]').innerText(),'Check now');
   await shot('bare-loading-limit');
   // Ordinary polls must leave the fast follow-up budget exhausted across repeated loading answers.
   for(let i=0;i<3;i++){
     await page.evaluate(()=>pollOnce());
     await page.waitForFunction(()=>document.querySelector('.notice')?.textContent.includes('still running'));
     assert.equal(await page.evaluate(()=>followUps.filter(f=>!f.cancelled&&!f.ran&&f.ms!==20000).length),0);
   }
   // Explicit retry starts a fresh bounded schedule; a new selection does too.
   await page.locator('[data-act="retry"]').click();
   await page.waitForFunction(()=>window.followUps.some(f=>!f.cancelled&&!f.ran&&f.ms!==20000));
   assert.equal(await page.evaluate(()=>followUps.filter(f=>!f.cancelled&&!f.ran&&f.ms!==20000).at(-1).ms),1000);
   await page.locator('[data-range="MONTH"]').click();
   await page.waitForFunction(()=>window.followUps.some(f=>!f.cancelled&&!f.ran&&f.ms!==20000));
   assert.equal(await page.evaluate(()=>followUps.filter(f=>!f.cancelled&&!f.ran&&f.ms!==20000).at(-1).ms),1000);
   // Success clears the budget even when it is an automatic follow-up.
   bareLoading=false;
   assert.equal(await page.evaluate(()=>runFollowUp()),1000);
   await page.waitForFunction(()=>!document.querySelector('.notice'));
   bareLoading=true;await page.evaluate(()=>pollOnce());
   await page.waitForFunction(()=>window.followUps.some(f=>!f.cancelled&&!f.ran&&f.ms!==20000));
   assert.equal(await page.evaluate(()=>followUps.filter(f=>!f.cancelled&&!f.ran&&f.ms!==20000).at(-1).ms),1000);
   bareLoading=false;await page.locator('[data-range="ALL"]').click();
   await page.waitForFunction(()=>document.querySelector('[data-range="ALL"]').getAttribute('aria-pressed')==='true'&&!document.querySelector('.notice'));
   // A server's typed reason is shown in words.
   invalidCustom=true;await page.locator('[data-range="CUSTOM"]').click();await page.locator('#from').fill('2026-11-01');await page.locator('#to').fill('2026-11-02');
   await page.locator('[data-act="apply"]').click();
   await page.waitForFunction(()=>document.querySelector('.notice.error')?.textContent.includes('Choose a valid date range'));
   invalidCustom=false;
   await page.locator('[data-range="6H"]').click();
   await page.waitForFunction(()=>document.querySelector('[data-range="6H"]').getAttribute('aria-pressed')==='true'&&!document.querySelector('.notice'));
   // Both headers and JSON consumption may stall. The exact production deadline releases pending;
   // polling then recovers the failed selection, without requiring another user click.
   for (const phase of ['headers','body']) {
     await page.evaluate(phase=>{
       window.savedFetch=window.fetch;
       window.fetch=(_url,init)=>{window.stalledSignal=init.signal;window.bridgeDeadline=init.timeoutMs;
         return phase==='headers'?new Promise(()=>{}):Promise.resolve({ok:true,json:()=>new Promise(()=>{})});};
     },phase);
     await page.locator('[data-act="refresh"]').click();
     await page.waitForFunction(()=>document.querySelector('.notice')?.textContent.includes('Updating'));
     assert.equal(await page.evaluate(()=>bridgeDeadline),20000);
     assert.equal(await page.evaluate(()=>{const d=followUps.findLast(x=>x.ms===20000&&!x.cancelled&&!x.ran);if(!d)return false;d.ran=true;d.fn();return true;}),true);
     await page.waitForFunction(()=>document.querySelector('.notice.error')?.textContent.includes('20 seconds'));
     assert.equal(await page.evaluate(()=>stalledSignal.aborted),true);
     await page.evaluate(()=>{window.fetch=savedFetch;pollOnce();});
     await page.waitForFunction(()=>!document.querySelector('.notice'));
   }
   // The host releases its overflow suppression in a card shorter than the content: the document must scroll.
   await page.setViewportSize({width,height:400});
   await page.evaluate(()=>{const e=document.documentElement;e.style.setProperty('overflow-y','hidden','important');e.style.removeProperty('overflow-y');});
   assert.notEqual(await page.evaluate(()=>getComputedStyle(document.documentElement).overflowY),'hidden');
   await page.mouse.move(width/2,200);
   for(let i=0;i<20;i++){await page.mouse.wheel(0,3000);await page.waitForTimeout(60);if(await page.evaluate(()=>scrollY+innerHeight>=document.documentElement.scrollHeight-1))break;}
   assert.ok(await page.evaluate(()=>scrollY>0));
   assert.ok(await page.evaluate(()=>document.querySelector('.footer').getBoundingClientRect().bottom<=innerHeight+1),'footer reachable by scrolling');
   await page.evaluate(()=>window.scrollTo(0,0));await page.setViewportSize({width,height});
   const labelsFit=await page.evaluate(()=>[...document.querySelectorAll('svg')].every(svg=>{const box=svg.getBoundingClientRect(),ticks=[...svg.querySelectorAll('g text.axis')].map(t=>t.getBoundingClientRect()).sort((a,b)=>a.left-b.left);return ticks.every((t,i)=>t.left>=box.left-1&&t.right<=box.right+1&&(i===0||ticks[i-1].right<=t.left+1));}));
   assert.ok(labelsFit,'axis labels overlap or leave the chart');
   const geo=await page.evaluate(()=>({width:innerWidth,scroll:document.documentElement.scrollWidth,charts:[...document.querySelectorAll('.chart')].map(e=>({width:e.clientWidth,svg:e.querySelector('svg').getBoundingClientRect().width}))}));
   assert.ok(geo.scroll<=width,JSON.stringify(geo));assert.ok(geo.charts.every(c=>c.svg<=c.width+1),JSON.stringify(geo));
   await shot('6h');
   // An old response after disposal must not redraw or restart timers.
   let release;delay=new Promise(r=>release=r);await page.locator('[data-act="refresh"]').click();
   await page.waitForFunction(()=>document.querySelector('.notice'));
   await page.evaluate(()=>dispose());const frozen=await root.innerHTML();release();delay=null;
   await page.waitForTimeout(50);assert.equal(await root.innerHTML(),frozen);
   assert.equal(await page.evaluate(()=>offTheme),true);assert.deepEqual(errors,[]);
   facts.push({engine,width,theme,requests:requests.length,geometry:geo,errors});
   await context.close();
  }} finally {await browser.close();}
 }
 fs.writeFileSync(path.join(output,'checks.json'),JSON.stringify(facts,null,2));
 console.log('PASS: '+facts.length+' browser/theme/width flows');
})().catch(e=>{fs.writeFileSync(path.join(output,'failure.txt'),String(e.stack));console.error(e);process.exitCode=1;});
