"""Render the approved carry contract and manual credit states in the real JS.

Synthetic chart metadata isolates presentation from the independently tested
history builder. No provider/host endpoint or live account is used.
"""
import json
import os
import subprocess
from pathlib import Path

import test_quotas
from test_reserve import _node, _widget_fixture


NODE_HISTORY = r"""
(async () => {
  const view = JSON.parse(process.env.HISTORY_WIDGET_FIXTURE);
  const chart = view.reserve.chart;
  const now = chart.now;
  const times = [now-7200, now-6600, now-6000, now-5400, now-5400,
                 now-4800, now-4200, now-3600, now];
  const values = [.8,.7,.7,null,1.3,1.2,1.2,1,.9];
  chart.start = now-7200; chart.end = now+7200;
  chart.history = {max_accounts:2, membership_note:'Membership begins at first recorded evidence.',
    line: times.map((t,i) => [t,values[i]]),
    details: times.map((t,i) => ({at:t,value:values[i],accounts:i<4?1:2,
      measured:[2,5,6,8].includes(i)?0:(i<4?1:2), carried:[2,5,6,8].includes(i)?(i<4?1:2):0,
      age_seconds:600, oldest_observed_at:new Date((t-600)*1000).toISOString(),
      origins:{history:i<4?1:2},sources:['synthetic-source'],reset_passed:i===6?1:0,
      change:(i===0||i===4)?'first_recorded':null,added:i===4?1:0,removed:0}))};
  chart.scenarios = {no_new_use:{accounts:1,line:[[now,.3],[now+7200,.3]],schedule:[],checkpoints:[],no_reset:1}};
  const family = view.groups[0];
  const observed = new Date((now-600)*1000).toISOString();
  family.reset_credits = {count:0,current_accounts:1,last_known_count:2,last_known_accounts:1,
    unknown_accounts:0,unreadable_accounts:1,conflict_accounts:0,accounts:3,
    oldest_observed_at:observed,newest_observed_at:observed,
    last_known_oldest_observed_at:observed,last_known_newest_observed_at:observed};
  family.accounts[0].quota.reset_credits = {state:'current',count:0,observed_at:observed,
    source:'synthetic-source',label:'0 reset credits',count_origin:'label'};
  family.accounts[1].quota.reset_credits = {state:'last_known',count:2,observed_at:observed,
    source:'synthetic-source',label:'2 reset credits',count_origin:'label'};
  family.accounts[2].quota.reset_credits = {state:'unreadable',count:null,observed_at:observed,
    source:'synthetic-source',label:'reset credits unavailable <script>',reason:'unreadable_label'};
  let env = await boot(view);
  const svgNodes = (cls) => walk(env.root).filter(n => n.tagName==='PATH' && n.getAttribute('class')===cls);
  assert.equal(walk(env.root).filter(n => n.getAttribute('class')==='chart-svg').length,1);
  assert.equal(svgNodes('line-carried').length,1);
  assert.ok(svgNodes('line-carried')[0].getAttribute('d').includes('L'));
  // Every membership cut creates a new run: there is no invented vertical
  // rise connecting .7 from one account to 1.3 from two.
  const solid = svgNodes('line-observed')[0].getAttribute('d');
  assert.ok((solid.match(/M/g)||[]).length >= 2,solid);
  assert.equal(svgNodes('line-scenario').length,1);
  assert.match(classes(env.root,'chart-legend')[0].textContent,/solid: recorded; dashed: carried/);
  assert.match(classes(env.root,'family-credits')[0].textContent,/Manual reset credits · 0 reported across 1 account/);
  assert.match(classes(env.root,'family-credits')[0].textContent,/last known 2 across 1 account.*1 unreadable/);

  const plot = byFocus(env.root,'chart-plot');
  const readout = classes(env.root,'chart-readout')[0];
  const tip = classes(env.root,'chart-tip')[0];
  plot.listeners.focus[0]();
  assert.match(readout.textContent,/recorded 0.90 of 2 accounts, no new use 0.30 of 1 current account/);
  assert.match(tip.textContent,/last-known history/);
  assert.match(tip.textContent,/2 accounts · 0 recorded, 2 carried/);
  assert.match(tip.textContent,/Oldest carried reading 10 min old at this moment/);
  assert.match(tip.textContent,/from the local history.*Source: synthetic-source/);
  plot.listeners.keydown[0]({key:'Home',preventDefault(){}});
  assert.match(readout.textContent,/recorded 0.80 of 1 account/);
  assert.match(readout.textContent,/First recorded value/);
  // Target exact historical instants using the existing pointer handler.
  const svg = walk(env.root).find(n=>n.getAttribute('class')==='chart-svg');
  const hit = walk(env.root).find(n=>n.getAttribute('class')==='hit');
  const width = Number(svg.getAttribute('width'));
  svg.getBoundingClientRect=()=>({left:0,width});
  const t0=Math.floor(chart.start/3600)*3600;
  const t1=t0+Math.ceil((chart.end-t0)/3600)*3600;
  const hover=(t)=>hit.listeners.pointermove[0]({clientX:30+(t-t0)/(t1-t0)*(width-42)});
  hover(now-5700);
  assert.match(tip.textContent,/15 min old at this moment/,'age advances from source observation at hovered time');
  hover(now-5300);
  assert.match(tip.textContent,/First recorded value of 1 account/);
  assert.match(tip.textContent,/Membership changed · not consumption/);
  hover(now-4000);
  assert.match(tip.textContent,/pre-reset reading retained; refill not measured/);
  assert.doesNotMatch(tip.textContent,/1\.00.*last-known history/,'past reset keeps 1.2, never a fabricated full quota');
  assert.match(classes(env.root,'chart-table')[0].textContent,/History.*Carried|Carried.*History/);

  byFocus(env.root,'accounts').listeners.click[0]({stopPropagation(){}});
  const select=(i)=>byFocus(env.root,'acct:'+family.accounts[i].key).listeners.click[0]({stopPropagation(){}});
  select(0);
  assert.match(classes(env.root,'account-credits')[0].textContent,/Manual reset credits · 0 · observed/);
  assert.match(classes(env.root,'account-credits')[0].title,/source synthetic-source.*0 reset credits/);
  select(1);
  assert.match(classes(env.root,'account-credits')[0].textContent,/last known 2.*not current/);
  select(2);
  assert.match(classes(env.root,'account-credits')[0].textContent,/count unreadable.*<script>/);
  assert.equal(walk(classes(env.root,'account-credits')[0]).filter(n=>n.tagName==='SCRIPT').length,0);

  const kept = env.testHooks.keptView(view);
  assert.equal(kept.groups[0].reset_credits.count,null);
  assert.equal(kept.groups[0].reset_credits.current_accounts,0);
  assert.equal(kept.groups[0].reset_credits.last_known_count,2);
  assert.equal(kept.groups[0].reset_credits.last_known_accounts,2);
  assert.equal(kept.groups[0].accounts[0].quota.reset_credits.state,'last_known');
  assert.equal(kept.reserve.chart.history.line.at(-1)[1],null);
  assert.equal(chart.history.line.at(-1)[1],.9,'kept conversion never mutates the received chart');
  assert.equal(kept.reserve.chart.scenarios,null);
  env=await boot(kept);
  assert.doesNotMatch(classes(env.root,'family-credits')[0].textContent,/0 reported/);
  assert.match(classes(env.root,'family-credits')[0].textContent,/last known 2 across 2 accounts/);
  assert.equal(walk(env.root).filter(n=>n.tagName==='PATH' && n.getAttribute('class')==='line-scenario').length,0);

  const unknown=JSON.parse(JSON.stringify(view));
  unknown.groups[0].reset_credits={count:null,current_accounts:0,last_known_count:null,last_known_accounts:0,
    unknown_accounts:2,unreadable_accounts:0,conflict_accounts:1,accounts:3};
  unknown.groups[0].accounts[0].quota.reset_credits={state:'unknown',count:null,reason:'not_reported'};
  unknown.groups[0].accounts[1].quota.reset_credits={state:'conflict',count:null};
  env=await boot(unknown);
  assert.match(classes(env.root,'family-credits')[0].textContent,/1 with sources disagreeing · 2 unknown/);
  assert.doesNotMatch(classes(env.root,'family-credits')[0].textContent,/0 reported/);
  byFocus(env.root,'accounts').listeners.click[0]({stopPropagation(){}});
  byFocus(env.root,'acct:'+family.accounts[0].key).listeners.click[0]({stopPropagation(){}});
  assert.equal(classes(env.root,'account-credits').length,0,'unreported counter adds no empty row');
  byFocus(env.root,'acct:'+family.accounts[1].key).listeners.click[0]({stopPropagation(){}});
  assert.match(classes(env.root,'account-credits')[0].textContent,/unknown · sources disagree/);
  const unreported=JSON.parse(JSON.stringify(unknown));
  unreported.groups[0].reset_credits.conflict_accounts=0;
  unreported.groups[0].reset_credits.unknown_accounts=3;
  env=await boot(unreported);
  assert.equal(classes(env.root,'family-credits').length,0,'all unreported family adds no counter noise');
  const dated=JSON.parse(JSON.stringify(unknown));
  dated.groups[0].accounts[0].quota.reset_credits={state:'unknown',count:null,reason:'no_current_reading',observed_at:observed};
  env=await boot(dated);
  byFocus(env.root,'accounts').listeners.click[0]({stopPropagation(){}});
  byFocus(env.root,'acct:'+family.accounts[0].key).listeners.click[0]({stopPropagation(){}});
  assert.match(classes(env.root,'account-credits')[0].textContent,/unknown · no current reading/);
})().catch(error=>{console.error(error.stack||error);process.exitCode=1;});
"""


def test_widget_carries_history_and_displays_credits(tmp_path):
    node = _node()
    assert node is not None, 'Node is required'
    fixture = _widget_fixture(tmp_path)['view']
    harness = test_quotas.NODE_WIDGET_MATRIX.split('(async () => {')[0]
    result = subprocess.run([str(node), '-e', harness + NODE_HISTORY],
        cwd=Path(__file__).parent,
        env={**os.environ, 'WIDGET_PATH':str(Path(__file__).with_name('widget.js').resolve()),
             'HISTORY_WIDGET_FIXTURE':json.dumps(fixture)},
        capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
