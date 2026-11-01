"""0.7.0: the widget's reads and Refresh end, whatever the bridge does.

The real widget.js runs in the bundled-Node fake DOM of test_quotas, with a
scripted bridge (each request answered, failed or left hanging by the test)
and timers the test fires by hand. Nothing here talks to a host.
"""

import json
import os
import subprocess
from pathlib import Path

import plugin
import quota_history
import test_quotas
from test_reserve import _node, widget_reserve
from test_last_known import _healthy

NODE_ROBUST = r"""
// A bridge the test drives: every request waits for the test to settle it.
function bootControlled(opts = {}) {
  const made = makeDocument();
  const requests = [];
  const timers = new Map();
  let timerId = 0;
  let intervalCallback = null;
  const disposeHooks = [];
  const errors = [];
  const window = {
    fetch(url, init = {}) {
      const req = { url, method: init.method || 'GET', init };
      req.promise = new Promise((resolve, reject) => { req.resolve = resolve; req.reject = reject; });
      requests.push(req);
      return req.promise;
    },
    setInterval(cb) { intervalCallback = cb; return 17; },
    clearInterval() {},
    setTimeout(cb, ms) { const id = ++timerId; timers.set(id, { cb, ms }); return id; },
    clearTimeout(id) { timers.delete(id); },
    addEventListener() {},
    __ouroWidgetOnDispose(fn) { disposeHooks.push(fn); },
  };
  const quietConsole = { error: (...a) => errors.push(a.map(String).join(' ')), log() {}, warn() {} };
  const context = vm.createContext({
    window, document: made.document, console: quietConsole, Date, Math, Object, Array, String,
    Number, RegExp, Promise, setImmediate, JSON,
  });
  vm.runInContext(instrumentedWidgetSource, context, { filename: 'widget.js' });
  return {
    root: made.root, document: made.document, requests, timers, errors, disposeHooks,
    poll: () => intervalCallback(),
    // Fire every pending timer at least `ms` long (the request backstops).
    fireTimers(ms) {
      [...timers.entries()].forEach(([id, t]) => {
        if (t.ms >= ms) { timers.delete(id); t.cb(); }
      });
    },
  };
}
const answer = (value, status = 200) => ({ ok: status >= 200 && status < 300, status,
  text: async () => (typeof value === 'string' ? value : JSON.stringify(value)) });
const refreshBtn = (env) => byFocus(env.root, 'refresh');

(async () => {
  const fx = JSON.parse(process.env.ROBUST_FIXTURE);

  // 1. Headers never come: the read is bounded (the bridge is asked for
  // 60 s, the widget's own backstop 5 s later), it ends, and the next poll
  // can read again. No exception escapes.
  let env = bootControlled();
  await settle();
  assert.equal(env.requests.length, 1);
  assert.equal(env.requests[0].init.timeoutMs, 60000);
  assert.ok(refreshBtn(env).disabled, 'in flight');
  env.poll();
  await settle();
  assert.equal(env.requests.length, 1, 'no second read while one is in flight');
  env.fireTimers(65000);
  await settle();
  assert.ok(!refreshBtn(env).disabled, 'inFlight released by the backstop');
  assert.match(env.root.textContent, /Endpoint unreachable/);
  assert.ok(byFocus(env.root, 'retry'), 'a Retry is offered');
  env.poll();
  await settle();
  assert.equal(env.requests.length, 2, 'the next poll reads again');
  // The late answer of the first read changes nothing.
  env.requests[0].resolve(answer(fx.good));
  await settle();
  assert.doesNotMatch(env.root.textContent, /Reserve · Codex/);
  env.requests[1].resolve(answer(fx.good));
  await settle();
  assert.match(env.root.textContent, /Reserve · Codex/);

  // 2. A body that never finishes is bounded the same way, and the good
  // screen stays with a dated "could not be refreshed" and a Retry.
  env.poll();
  await settle();
  const pendingBody = env.requests.at(-1);
  pendingBody.resolve({ ok: true, status: 200, text: () => new Promise(() => {}) });
  await settle();
  assert.ok(refreshBtn(env).disabled);
  env.fireTimers(65000);
  await settle();
  assert.ok(!refreshBtn(env).disabled);
  assert.match(env.root.textContent, /Reading could not be refreshed \(no answer within 60 s\)/);
  assert.match(env.root.textContent, /Reserve · Codex/, 'the last whole answer is kept');

  // 3. The bridge's own bound: "widget request timed out" is a timeout.
  env.poll();
  await settle();
  env.requests.at(-1).reject(new Error('widget request timed out'));
  await settle();
  assert.match(env.root.textContent, /no answer within 60 s/);
  assert.ok(!refreshBtn(env).disabled);

  // 4. An abort, a body that is not JSON, and "null": each ends the read,
  // none replaces the kept answer.
  for (const bad of [() => Promise.reject(Object.assign(new Error('The operation was aborted.'), { name: 'AbortError' })),
                     () => Promise.resolve(answer('<html>gateway</html>')),
                     () => Promise.resolve(answer('null')),
                     () => Promise.resolve(answer([1, 2])),
                     () => Promise.resolve(answer({ error: 'boom' }, 500))]) {
    env.poll();
    await settle();
    const req = env.requests.at(-1);
    bad().then(req.resolve, req.reject);
    await settle();
    assert.ok(!refreshBtn(env).disabled);
    assert.match(env.root.textContent, /Reading could not be refreshed/);
    assert.match(env.root.textContent, /Reserve · Codex/);
  }

  // 5. A semantic failure (HTTP 200, the skill says the status failed and
  // shows its dated last-known values) is drawn as it is — and, being the
  // newest screen drawn, it is what a later failed read falls back to (0.7.0
  // repair D6): an older whole answer must not come back looking fresher.
  env.poll();
  await settle();
  env.requests.at(-1).resolve(answer(fx.down));
  await settle();
  assert.match(env.root.textContent, /Claudexor status could not be read\. Everything below is last known/);
  assert.match(env.root.textContent, /Claudexor not read now/);
  env.poll();
  await settle();
  env.requests.at(-1).resolve(answer('not json'));
  await settle();
  assert.match(env.root.textContent, /Claudexor not read now/);
  assert.match(env.root.textContent, /Reading could not be refreshed/);
  // 5b. An answer with nothing to draw and no reason ({ok:false}) is not a
  // screen: the newest one stays, said as kept, never a blank card.
  env.poll();
  await settle();
  env.requests.at(-1).resolve(answer({ ok: false }));
  await settle();
  assert.match(env.root.textContent, /Reading could not be refreshed \(response reported itself incomplete\)/);
  assert.match(env.root.textContent, /Reserve · Codex/);
  assert.ok(!refreshBtn(env).disabled);

  // 6. An answer that cannot be drawn: the screen before it stays, the
  // error is said with a Retry, inFlight is free, and nothing escapes.
  env.poll();
  await settle();
  const broken = JSON.parse(JSON.stringify(fx.good));
  broken.groups[0].accounts = [null];
  env.requests.at(-1).resolve(answer(broken));
  await settle();
  assert.match(env.root.textContent, /The latest answer could not be drawn \(TypeError/);
  assert.match(env.root.textContent, /The screen before it is kept/);
  assert.match(env.root.textContent, /Reserve · Codex/);
  assert.ok(byFocus(env.root, 'retry'));
  assert.ok(!refreshBtn(env).disabled);
  assert.ok(env.errors.some((e) => /could not be drawn/.test(e)), 'the error is logged, not swallowed');
  // The next redraw (a click, a poll) starts from the good view again.
  env.poll();
  await settle();
  env.requests.at(-1).resolve(answer(fx.good));
  await settle();
  assert.doesNotMatch(env.root.textContent, /could not be drawn/);

  // 7. A first answer that cannot be drawn: an error and a Retry, not a blank card.
  env = bootControlled();
  await settle();
  env.requests[0].resolve(answer(broken));
  await settle();
  assert.match(env.root.textContent, /could not be drawn/);
  byFocus(env.root, 'retry').listeners.click[0]({ stopPropagation() {} });
  await settle();
  assert.equal(env.requests.length, 2, 'Retry reads again');

  // 8. Refresh: bounded at 210 s; no answer means the outcome is unknown,
  // it is said so, and the POST is never sent again on its own.
  env = bootControlled();
  await settle();
  env.requests[0].resolve(answer(fx.good));
  await settle();
  refreshBtn(env).listeners.click[0]();
  await settle();
  const post = env.requests.at(-1);
  assert.equal(post.method, 'POST');
  assert.equal(post.init.timeoutMs, 210000);
  env.fireTimers(215000);
  await settle();
  assert.match(env.root.textContent, /whether it ran is unknown\. It is not sent again/);
  assert.ok(!refreshBtn(env).disabled);
  env.poll();
  await settle();
  assert.equal(env.requests.filter((r) => r.method === 'POST').length, 1);
  assert.equal(env.requests.at(-1).method, 'GET', 'passive retry is the ordinary poll');

  // 8b. 0.8.0: a Refresh that ran is followed by one read of the whole
  // projection, inside the same in-flight lifecycle (no poll and no second
  // Refresh meanwhile). When that read fails, the coherent screen stays —
  // kept, dated — and the banner says the refresh ran but its result could
  // not be read; when it hangs, the read's own bound ends it. The POST is
  // never sent again and nothing from its answer is merged in.
  env = bootControlled();
  await settle();
  env.requests[0].resolve(answer(fx.good));
  await settle();
  for (const ending of ['fails', 'hangs']) {
    refreshBtn(env).listeners.click[0]();
    await settle();
    const ran = env.requests.at(-1);
    assert.equal(ran.method, 'POST');
    ran.resolve(answer({ ok: true, quota_updates: [{ harness: 'codex', subject_id: 'c1',
      quota: { state: 'ok', label: '77% used', constraints: [] } }], refreshed_at: new Date().toISOString() }));
    await settle();
    const after = env.requests.at(-1);
    assert.equal(after.method, 'GET');
    assert.doesNotMatch(after.url, /reuse=1/, 'a new status read, not a reused one');
    assert.equal(after.init.timeoutMs, 60000);
    assert.ok(refreshBtn(env).disabled, 'still in flight while the read after the refresh runs');
    const asked = env.requests.length;
    env.poll();
    refreshBtn(env).listeners.click[0]();
    await settle();
    assert.equal(env.requests.length, asked, 'no poll and no second refresh meanwhile');
    if (ending === 'fails') after.resolve(answer('bad gateway', 502));
    else env.fireTimers(65000);
    await settle();
    assert.ok(!refreshBtn(env).disabled);
    assert.match(env.root.textContent, /Live refresh ran at \d\d:\d\d, but the reading after it could not be read/);
    assert.match(env.root.textContent, ending === 'fails' ? /Reading could not be refreshed \(HTTP 502\)/
      : /Reading could not be refreshed \(no answer within 60 s\)/);
    assert.match(env.root.textContent, /Reserve · Codex/, 'the coherent screen stays');
    assert.doesNotMatch(env.root.textContent, /77% used/, 'nothing from the POST answer is merged in');
  }
  assert.equal(env.requests.filter((r) => r.method === 'POST').length, 2);
  // The next ordinary read is drawn as it is, and the note goes with it.
  env.poll();
  await settle();
  env.requests.at(-1).resolve(answer(fx.good));
  await settle();
  assert.doesNotMatch(env.root.textContent, /could not be refreshed|Live refresh ran/);

  // 9. Disposed in flight: the late answer draws nothing and throws nothing.
  env = bootControlled();
  await settle();
  const before = env.root.textContent;
  await env.disposeHooks[0]();
  env.requests[0].resolve(answer(fx.good));
  await settle();
  assert.equal(env.root.textContent, before);
  assert.equal(env.errors.length, 0);
  console.log('ok');
})().catch((error) => {
  console.error(error && error.stack ? error.stack : error);
  process.exitCode = 1;
});
"""


def test_real_widget_ends_every_read_and_refresh(tmp_path, monkeypatch):
    node = _node()
    assert node is not None, "a Node runtime is required for widget tests"
    import time as _time
    now = _time.time()
    good_payload = _healthy()
    for row in good_payload["quota"]:
        row["observed_at"] = plugin.qs.iso(now - 60)
        for c in row["constraints"]:
            c["resets_at"] = plugin.qs.iso(now + 86400)
    store = quota_history.HistoryStore(tmp_path)
    good = plugin.build_view(good_payload, "", now)
    good["reserve"] = widget_reserve(store, good_payload, now, now, harness="codex", name_accounts=True)
    latest = plugin.LatestRead()
    latest.put(good_payload, "", now - 60)
    effective, cached = latest.effective(None)
    down = plugin.build_view(effective, "HTTP 503 from /api/claudexor/status", now,
                             reads=plugin.facet_states(None), cached=cached)
    down["reserve"] = widget_reserve(store, effective, None, now, harness="codex",
                                     cached=cached, reads=plugin.facet_states(None), name_accounts=True)
    assert good["complete"] is True and down["complete"] is False
    widget_path = Path(__file__).with_name("widget.js").resolve()
    harness = test_quotas.NODE_WIDGET_MATRIX.split("(async () => {")[0]
    result = subprocess.run(
        [str(node), "-e", harness + NODE_ROBUST],
        cwd=widget_path.parent,
        env={**dict(os.environ), "WIDGET_PATH": str(widget_path),
             "ROBUST_FIXTURE": json.dumps({"good": good, "down": down})},
        text=True, capture_output=True, timeout=60, check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.strip().endswith("ok")
