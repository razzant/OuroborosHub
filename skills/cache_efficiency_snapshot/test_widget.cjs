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
  try {for (const width of [1100,390]) for (const theme of ['dark','light']) {
   const context=await browser.newContext({viewport:{width,height:900},timezoneId:'Europe/Moscow',hasTouch:width===390,colorScheme:theme});
   const page=await context.newPage(), requests=[], errors=[];
   let fail=false, historyLoading=false, detailFail=false, delay=null, detailDelay=null, refreshed=false;
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
    if(historyLoading)body={...body,status:'loading',quality:{...body.quality,history_index:{state:'loading',archives_done:3,archives_total:10}}};
    return route.fulfill({contentType:'application/json',body:JSON.stringify(body)});
   });
   await page.goto('http://widget.test/');
   await page.evaluate(theme=>{window.TOKEN_DASH_THEME=theme;window.heights=[];window.OuroborosWidget={resize:h=>window.heights.push(h),onTheme:fn=>{window.changeTheme=fn;return()=>window.offTheme=true;}};window.__ouroWidgetOnDispose=fn=>window.dispose=fn;},theme);
   await page.addScriptTag({content:script});
   await page.waitForFunction(()=>document.querySelector('.scope')?.textContent.includes('Europe/Moscow')&&!document.querySelector('.notice'));
   const root=page.locator('#root');
   await page.locator('[data-range="1H"]').click();
   await page.waitForFunction(()=>document.querySelector('[data-range="1H"]').getAttribute('aria-pressed')==='true');
   const bars=page.locator('[data-chart="volume"] [data-pick]');
   assert.equal(await bars.count(),12);
   assert.equal(await page.locator('.bar-unknown[data-tokens="2000"]').count(),1);
   assert.equal(await page.locator('.code-mark').count(),1);
   assert.match(await root.innerText(),/2 unknown cache reads/);
   const selected=fixtures['1H'].buckets.find(b=>b.unknown_prompt_tokens===2000);
   await page.locator('[data-chart="volume"] [data-pick="'+selected.ts+'"]').click();
   assert.match(await page.locator('.inspector').innerText(),/2.0 K input/);
   assert.equal(requests.length,2); // Five-minute inspection needs no day request.
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
   fail=false;await page.locator('[data-act="retry"]').click();
   await page.waitForFunction(()=>document.querySelector('[data-range="CUSTOM"]').getAttribute('aria-pressed')==='true'&&!document.querySelector('.notice'));
   assert.equal(requests.at(-1).from,'2026-11-09');assert.equal(requests.at(-1).to,'2026-11-09');
   const daily=fixtures.CUSTOM.buckets[0];
   await page.locator('[data-chart="volume"] [data-pick]').first().click();
   await page.waitForFunction(()=>document.querySelector('.hourly'));
   assert.equal(requests.at(-1).from,daily.ts);assert.equal(requests.at(-1).to,daily.end_ts);
   assert.equal(requests.at(-1).granularity,'hour');assert.equal(await page.locator('.hourly i').count(),24);
   // New overview data invalidates old detail and its pending responses, even for the same date.
   let finishDetail;detailDelay=new Promise(r=>finishDetail=r);refreshed=true;
   await page.locator('[data-act="refresh"]').click();
   await page.waitForFunction(()=>document.querySelector('.inspector')?.textContent.includes('Loading hourly detail'));
   assert.match(await page.locator('.inspector').innerText(),/1.9 K input/);
   finishDetail();detailDelay=null;
   await page.locator('.hourly').waitFor();
   assert.match(await page.locator('.inspector').innerText(),/1.3 K known cache reads/);
   assert.match(await page.locator('.inspector').innerText(),/2 requests/);
   refreshed=false;
   await page.locator('[data-act="clear"]').click();detailFail=true;
   await page.locator('[data-chart="volume"] [data-pick]').first().click();
   await page.waitForFunction(()=>document.querySelector('.inspector')?.textContent.includes('fixture_detail'));
   assert.equal(await page.locator('.hourly').count(),0);detailFail=false;
   historyLoading=true;await page.locator('[data-range="MONTH"]').click();
   await page.waitForFunction(()=>document.querySelector('.notice')?.textContent.includes('3 / 10 archives'));
   assert.equal(await page.locator('[data-range="CUSTOM"]').getAttribute('aria-pressed'),'true');
   historyLoading=false;
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
   await page.locator('[data-range="6H"]').click();
   await page.waitForFunction(()=>document.querySelector('[data-range="6H"]').getAttribute('aria-pressed')==='true'&&!document.querySelector('.notice'));
   const geo=await page.evaluate(()=>({width:innerWidth,scroll:document.documentElement.scrollWidth,charts:[...document.querySelectorAll('.chart')].map(e=>({width:e.clientWidth,svg:e.querySelector('svg').getBoundingClientRect().width})),heights:heights.length}));
   assert.ok(geo.scroll<=width,JSON.stringify(geo));assert.ok(geo.charts.every(c=>c.svg<=c.width+1),JSON.stringify(geo));
   await page.screenshot({path:path.join(output,`${engine}-${width}-${theme}.png`),fullPage:true});
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
