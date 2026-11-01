"""Dedicated passive reads keep intentional catalog omission distinct from failure."""
import json
import os
import subprocess
from pathlib import Path

import plugin
import test_quotas
from test_passive_quota_read import envelope, route, stub
from test_reserve import NOW, _Host, _node
from test_robust_widget import NODE_ROBUST


NODE_PASSIVE = r"""
(async () => {
  // Unmodified registered-route output: this exercises build_view as well as
  // the consumer, including its daemon omission and current quota figures.
  const fixtures = JSON.parse(process.env.PASSIVE_WIDGET_FIXTURE);
  const dedicated = fixtures.dedicated;
  const click=(env,key)=>byFocus(env.root,key).listeners.click[0]({stopPropagation(){}});
  const problem=env=>/\bhas-problem\b/.test(byFocus(env.root,'about').className);
  let env = await boot(dedicated);
  assert.equal(problem(env),false,'catalog was intentionally not requested');
  assert.equal(classes(env.root,'banner').length,0);
  assert.ok(classes(env.root,'lrow').length,'the complete quota reading is rendered');
  assert.equal(classes(env.root,'harness-name')[0].textContent,'Codex');
  assert.doesNotMatch(env.root.textContent,/Claudexor daemon is|Readings below are last known/);
  assert.ok(classes(env.root,'l-fig').some(n=>!n.textContent.startsWith('—')));
  click(env,'about');
  let about=classes(env.root,'about-panel')[0];
  assert.match(about.textContent,/catalog not requested for this quota read/);
  assert.match(about.textContent,/daemon not reported/,'no daemon health invented from successful quota read');
  assert.doesNotMatch(about.textContent,/daemon running|catalog read|Each facet the daemon did not answer/);
  const omitted=classes(about,'dot-label').find(n=>/catalog not requested/.test(n.textContent));
  assert.ok(classes(omitted,'state-dot').some(n=>/\bmuted\b/.test(n.className)));
  click(env,'about');
  click(env,'accounts');
  click(env,'acct:'+dedicated.groups[0].accounts[0].key);
  assert.doesNotMatch(classes(env.root,'inspector')[0].textContent,/catalog not read/);

  for (const state of ['unknown','stopped']) {
    env=await boot(fixtures[state]);
    assert.equal(problem(env),true,'an explicitly reported daemon state remains a problem');
    assert.match(env.root.textContent,new RegExp('Claudexor daemon is '+state));
  }

  // The cold passive envelope has no catalog labels. Existing identity
  // presentation supplies known names, while meaningful catalog names win.
  for (const [id,label,expected] of [
    ['codex','codex','Codex'], ['claude','claude','Claude'],
    ['cursor','cursor','Cursor'], ['opencode','opencode','OpenCode'],
    ['agy','agy','Antigravity'], ['codex','Fixture Codex','Fixture Codex'],
    ['constructor','constructor','constructor']
  ]) {
    const named=JSON.parse(JSON.stringify(dedicated));
    named.groups=[Object.assign({},named.groups[0],{harness_id:id,family_label:label})];
    env=await boot(named);
    assert.equal(classes(env.root,'harness-name')[0].textContent,expected);
    assert.equal(classes(env.root,'harness-initial').length,id==='constructor'?1:0,
      'known families retain their own SVG marks; unknown ids keep initials');
  }

  // Only the exact projection mode AND not_read state are exempt. Legacy
  // catalog omissions and an actual catalog failure remain visible problems.
  for (const [mode,catalog] of [['legacy_status','not_read'],['quota','failed'],['unavailable','not_read']]) {
    const failure=JSON.parse(JSON.stringify(dedicated));
    failure.passive_read.mode=mode; failure.facets.catalog=catalog;
    failure.complete=false; failure.facet_note='catalog '+(catalog==='failed'?'failed':'not read');
    env=await boot(failure);
    assert.equal(problem(env),true,mode+' '+catalog);
    assert.match(classes(env.root,'banners')[0].textContent,/Not read now: catalog/);
    click(env,'about');
    assert.doesNotMatch(classes(env.root,'about-panel')[0].textContent,/catalog not requested/);
  }
  for (const facet of ['accounts','quota']) {
    const failure=JSON.parse(JSON.stringify(dedicated));
    failure.facets[facet]='failed'; failure.complete=false; failure.facet_note=facet+' failed';
    env=await boot(failure);
    assert.equal(problem(env),true);
    assert.match(classes(env.root,'banners')[0].textContent,new RegExp('Not read now: '+facet+' failed'));
    assert.doesNotMatch(classes(env.root,'banners')[0].textContent,/catalog/);
  }

  // A successful dedicated response is kept after a failed transport; its
  // intentionally unrequested catalog gets no fabricated cache observation.
  env=bootControlled(); await settle();
  env.requests[0].resolve(answer(dedicated)); await settle();
  assert.equal(problem(env),false);
  env.poll(); await settle();
  env.requests.at(-1).resolve(answer('bad gateway',502)); await settle();
  assert.ok(classes(env.root,'lrow').length);
  assert.equal(problem(env),true);
  assert.match(env.root.textContent,/Reading could not be refreshed/);
  assert.match(classes(env.root,'l-fig')[0].textContent,/— of/);
  const kept= (await boot(dedicated)).testHooks.keptView(dedicated);
  assert.equal(kept.cached.catalog,undefined,'omission never becomes a dated catalog read');
  assert.equal(kept.passive_read.mode,'unavailable');
  assert.deepEqual(Object.keys(kept.passive_read.timings_ms),[]);
  assert.deepEqual(Object.keys(kept.passive_read.read_errors),[]);
  assert.equal(dedicated.passive_read.mode,'quota','received response is not mutated');
})().catch(error=>{console.error(error.stack||error);process.exitCode=1;});
"""


def test_widget_distinguishes_dedicated_catalog_omission_from_failure(tmp_path, monkeypatch):
    data = envelope()
    assert "daemon" not in data
    calls = stub(monkeypatch, [(data, "", 200)])
    host = _Host(tmp_path)
    plugin.register(host)
    dedicated = route(host)
    assert len(calls) == 1
    fixtures = {"dedicated": dedicated}
    for state in ("unknown", "stopped"):
        view = plugin.build_view(dict(data, daemon={"state": state}), "", NOW)
        view["reserve"] = dedicated["reserve"]
        fixtures[state] = view
    node = _node()
    assert node is not None, 'Node is required'
    harness = test_quotas.NODE_WIDGET_MATRIX.split('(async () => {')[0]
    controlled = NODE_ROBUST.split('(async () => {')[0]
    result = subprocess.run([str(node), '-e', harness + controlled + NODE_PASSIVE],
        cwd=Path(__file__).parent,
        env={**os.environ, 'WIDGET_PATH':str(Path(__file__).with_name('widget.js').resolve()),
             'PASSIVE_WIDGET_FIXTURE':json.dumps(fixtures)},
        capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
