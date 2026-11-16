"""Cloud-only visual evidence using the existing RealHost/Card and B8 transfer scenarios.
Usage: capture_frames.py HUB CORE EVIDENCE EMPTY_RUN_ROOT.
Raw DOM, PNG, text, HTTP events and synthetic host data survive both outcomes.
"""
import hashlib
import json
import sys
import tarfile
import traceback
import unicodedata
from pathlib import Path

HUB, CORE, E, RUN_ROOT = map(Path, sys.argv[1:5])
FR = E / 'frames'
FR.mkdir(parents=True, exist_ok=True)
spec = json.loads(Path(__file__).with_name('frames.json').read_text())
DEV = HUB / 'tests/memory_atlas/dev'
sys.path[:0] = [str(HUB / 'tests/memory_atlas/dev'), str(HUB / 'tests/memory_atlas'), str(HUB / 'tests')]

from playwright.sync_api import sync_playwright  # noqa: E402
from realhost import RealHost  # noqa: E402
from test_widget_browser import Card  # noqa: E402
from conftest import ensure_transfers  # noqa: E402


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def subst(value, ids):
    if isinstance(value, str) and value.startswith('$ids.'):
        return ids[value[5:]]
    if isinstance(value, list):
        return [subst(v, ids) for v in value]
    if isinstance(value, dict):
        return {k: subst(v, ids) for k, v in value.items()}
    return value


def expected_route(step, ids):
    opened = subst(step['open'], ids)[-1]
    if step.get('exact_ids_path') or step.get('signature_source'):
        field = ['author']
        if step.get('exact_ids_path'):
            source, name = step['exact_ids_path']
            field = ['author', name] if source.endswith('-author') else ['metadata', 'transfer', name]
        return {'v': 'field', 'address': {'kind': 'record', 'id': opened['id'], 'field': field}}
    route = {key: opened[key] for key in ('v', 'id', 'room') if key in opened}
    if step.get('click'):
        assert step['click'] == [{'name': 'Stored record'}], step
        route['source'] = True
    return route


def check_route(actual, expected):
    assert all(actual.get(key) == value for key, value in expected.items()), (actual, expected)
    if expected['v'] == 'record':
        assert bool(actual.get('source')) == bool(expected.get('source')), (actual, expected)


ROW_FACTS = """(sel) => {
  const els = Array.from(document.querySelectorAll(sel));
  const doc = document.documentElement;
  return {
    count: els.length,
    doc_h_overflow: doc.scrollWidth > doc.clientWidth + 1,
    rows: els.map(e => { const r = e.getBoundingClientRect(); const cs = getComputedStyle(e);
      return {text: e.innerText, html_len: e.innerHTML.length, rect: [r.x, r.y, r.width, r.height],
              h_overflow: e.scrollWidth > e.clientWidth + 1, clipped_y: e.scrollHeight > e.clientHeight + 1 && cs.overflowY !== 'visible',
              overflow_x: cs.overflowX, white_space: cs.whiteSpace, font: cs.fontFamily + ' ' + cs.fontSize}; })
  };
}"""

manifest = {'spec': spec, 'cells': [], 'failures': []}
host = RealHost(CORE, RUN_ROOT, stress=spec.get('stress', True), python=sys.executable)
try:
    host.start()
    runtime = json.loads(host.producer('import platform\nprint(json.dumps({"executable":sys.executable,"version":list(sys.version_info[:3]),"platform":platform.platform()}))\n').strip().splitlines()[-1])
    assert runtime['executable'] == sys.executable and runtime['version'] == list(sys.version_info[:3]), runtime
    (E / 'producer-runtime.json').write_text(json.dumps(runtime, indent=2))
    facts = ensure_transfers(host)
    (E / 'transfer-facts.json').write_text(json.dumps(facts, indent=2, ensure_ascii=False))
    manifest['transfers'] = {'written_by': 'transfer_fixture.write via host.producer', 'ids': sorted(facts)}
    manifest['host'] = {'url': host.url, 'ids': host.ids, 'root': str(RUN_ROOT)}
    with sync_playwright() as pw:
        for cell in spec['matrix']:
            record = {'cell': cell['label'], 'engine': cell['engine'], 'viewport': cell['viewport'], 'theme': cell['theme'], 'steps': []}
            manifest['cells'].append(record)
            card = None
            try:
                card = Card(pw, host, cell['engine'], cell['viewport'], cell['theme'])
                record['http_errors'], record['request_errors'], record['dialogs'] = [], [], []
                card.page.on('response', lambda r: record['http_errors'].append({'status': r.status, 'url': r.url}) if r.status >= 400 else None)
                card.page.on('requestfailed', lambda r: record['request_errors'].append({'url': r.url, 'failure': r.failure}))
                card.page.on('dialog', lambda d: (record['dialogs'].append(d.message), d.dismiss()))
                for step in spec['steps']:
                    row = {'step': step['name']}
                    record['steps'].append(row)
                    try:
                        if step.get('open') is not None:
                            row['title'] = card.open(subst(step['open'], host.ids))
                        for click in step.get('click', []):
                            card.click(click['name'], click.get('exact', False), click.get('within'))
                        if step.get('click'):
                            card.settle()      # the suite's own wait after a click
                        if step.get('wait_text'):
                            card.wait_text(step['wait_text'])
                        row['expected_route'] = expected_route(step, host.ids)
                        row['route'] = card.ev('() => window.__memoryAtlas.route()')
                        row['title'] = card.title()
                        check_route(row['route'], row['expected_route'])
                        sel = step.get('rows_selector')
                        base = f"{cell['label']}__{step['name']}"
                        if sel:
                            loc = card.frame.locator(sel)
                            if step.get('scroll_rows_into_view', True) and loc.count():
                                loc.first.scroll_into_view_if_needed()
                            row['dom'] = card.ev(ROW_FACTS, sel)
                        # the owning suite's own overflow / injection measures, recorded per view (never a replacement for its tests)
                        row['overflowing'] = card.ev("() => Array.from(document.querySelectorAll('.doc, .row, .row .facts, .row .facts > *, .byline')).filter((e) => e.scrollWidth > e.clientWidth + 1).map((e) => e.className + ' ' + e.scrollWidth + '>' + e.clientWidth)")
                        row['page_overflow_px'] = card.ev("() => document.documentElement.scrollWidth - document.documentElement.clientWidth")
                        row['injected_nodes'] = card.ev("() => document.querySelectorAll('.atlas img, .atlas script, .atlas [onerror]').length")
                        row['bylines'] = card.ev("() => Array.from(document.querySelectorAll('.doc > .byline')).map((p) => p.innerText)")
                        row['facts'] = card.ev("() => Object.fromEntries(Array.from(document.querySelectorAll('.facts-grid dt')).map((d) => [d.textContent, d.nextElementSibling.textContent]))")
                        if step.get('metadata_cf_check'):
                            metadata = '\n'.join(row['bylines']) + json.dumps(row['facts'], ensure_ascii=False)
                            row['active_cf'] = [hex(ord(c)) for c in metadata if unicodedata.category(c) == 'Cf']
                            if row['active_cf']:
                                manifest['failures'].append({'cell': cell['label'], 'step': step['name'], 'active_cf': row['active_cf']})
                        for key in ('overflowing', 'injected_nodes'):
                            if row[key] and not step.get('allow_' + key):
                                manifest['failures'].append({'cell': cell['label'], 'step': step['name'], key: row[key]})
                        if row['page_overflow_px'] > 0:
                            manifest['failures'].append({'cell': cell['label'], 'step': step['name'], 'page_overflow_px': row['page_overflow_px']})
                        flags = {'frame': card.ev("() => window.__atlasXss === undefined ? null : window.__atlasXss"),
                                 'page': card.page.evaluate("() => window.__atlasXss === undefined ? null : window.__atlasXss")}
                        row['xss_flags'] = flags
                        if flags['frame'] or flags['page']:
                            manifest['failures'].append({'cell': cell['label'], 'step': step['name'], 'xss_flag_set': flags})
                        text = card.text()
                        row['pane_text_sha256'] = hashlib.sha256(text.encode()).hexdigest()
                        (FR / f'{base}.pane.txt').write_text(text, encoding='utf-8')
                        (FR / f'{base}.frame.html').write_text(card.frame.content(), encoding='utf-8')
                        (FR / f'{base}.page.html').write_text(card.page.content(), encoding='utf-8')
                        if step.get('exact_ids_path'):
                            expected = host.ids
                            for key in step['exact_ids_path']:
                                expected = expected[key]
                            actual = card.ev("() => document.querySelector('[aria-label=\"Exact stored text\"]').textContent")
                            (FR / f'{base}.exact.txt').write_text(actual, encoding='utf-8')
                            row['exact'] = {'expected_sha256': hashlib.sha256(expected.encode()).hexdigest(),
                                            'actual_sha256': hashlib.sha256(actual.encode()).hexdigest(), 'equal': actual == expected}
                            if actual != expected:
                                manifest['failures'].append({'cell': cell['label'], 'step': step['name'], 'exact_text_mismatch': True})
                        if step.get('signature_source'):
                            actual = card.ev("() => document.querySelector('[aria-label=\"Exact stored text\"]').textContent")
                            (FR / f'{base}.signature.json').write_text(actual, encoding='utf-8')
                            parsed = json.loads(actual)
                            row['signature_original_fields_equal'] = all(parsed[k] == v for k, v in host.ids['tr-special-author'].items())
                            if not row['signature_original_fields_equal']:
                                manifest['failures'].append({'cell': cell['label'], 'step': step['name'], 'signature_mismatch': True})
                        for needle in step.get('expect_text', []):
                            ok = needle in text
                            row.setdefault('expect', []).append({'text': needle, 'found': ok})
                            if not ok:
                                manifest['failures'].append({'cell': cell['label'], 'step': step['name'], 'missing': needle})
                        for needle in step.get('forbid_text', []):
                            bad = needle in text
                            row.setdefault('forbid', []).append({'text': needle, 'found': bad})
                            if bad:
                                manifest['failures'].append({'cell': cell['label'], 'step': step['name'], 'forbidden_present': needle})
                        shots = []
                        full = FR / f'{base}.card.png'
                        card.card.screenshot(path=str(full))
                        shots.append({'kind': 'card', 'path': full.name, 'sha256': sha(full)})
                        if sel and card.frame.locator(sel).count():
                            rows_png = FR / f'{base}.rows.png'
                            card.frame.locator(sel).first.screenshot(path=str(rows_png))
                            shots.append({'kind': 'first-row', 'path': rows_png.name, 'sha256': sha(rows_png)})
                        page_png = FR / f'{base}.page.png'
                        card.page.screenshot(path=str(page_png))
                        shots.append({'kind': 'page', 'path': page_png.name, 'sha256': sha(page_png)})
                        if card.frame.locator('.doc').count():
                            doc_png = FR / f'{base}.doc.png'
                            card.frame.locator('.doc').first.screenshot(path=str(doc_png))
                            shots.append({'kind': 'document-element', 'path': doc_png.name, 'sha256': sha(doc_png)})
                        # Keep every visible section of the real scrolling document, without CSS/DOM rewrites.
                        # The tile series records full coverage; a tall element screenshot alone may be clipped by its parent.
                        scroll = card.frame.locator('.pane .scroll').first
                        if step.get('full_scroll') and scroll.count():
                            extent = scroll.evaluate('(e) => ({height:e.scrollHeight, viewport:e.clientHeight})')
                            offsets = list(range(0, extent['height'], max(1, extent['viewport'] - 40)))
                            row['scroll_extent'] = extent
                            for n, offset in enumerate(offsets):
                                actual_top = scroll.evaluate('(e,y) => { e.scrollTop=y; return e.scrollTop; }', offset)
                                card.page.wait_for_timeout(80)
                                tile = FR / f'{base}.scroll-{n:02d}.png'
                                card.card.screenshot(path=str(tile))
                                shots.append({'kind': 'scroll-tile', 'scroll_top': actual_top, 'path': tile.name, 'sha256': sha(tile)})
                        row['shots'] = shots
                        row['route_after_capture'] = card.ev('() => window.__memoryAtlas.route()')
                        check_route(row['route_after_capture'], row['expected_route'])
                    except Exception as error:
                        row['error'] = repr(error)
                        row['traceback'] = traceback.format_exc()
                        manifest['failures'].append({'cell': cell['label'], 'step': step['name'], 'error': repr(error)})
                record['page_errors'] = list(card.errors)
                record['route_answers'] = card.answers
                if card.errors:
                    manifest['failures'].append({'cell': cell['label'], 'page_errors': card.errors})
                for key in ('http_errors', 'request_errors'):
                    if key == 'request_errors' and record[key]:
                        manifest['failures'].append({'cell': cell['label'], key: record[key]})
                if record['dialogs']:
                    manifest['failures'].append({'cell': cell['label'], 'dialogs': record['dialogs']})
            except Exception as error:
                record['error'] = repr(error)
                record['traceback'] = traceback.format_exc()
                manifest['failures'].append({'cell': cell['label'], 'error': repr(error)})
            finally:
                if card is not None:
                    try:
                        card.close()
                    except Exception as error:
                        record['close_error'] = repr(error)
                        manifest['failures'].append({'cell': cell['label'], 'close_error': repr(error)})
except Exception as error:        # start / transfer refusal / anything before or between cells: kept in the manifest, exit 1
    manifest['setup_error'] = repr(error)
    manifest['setup_traceback'] = traceback.format_exc()
    manifest['failures'].append({'setup_error': repr(error)})
finally:
    try:
        host.stop()
    except Exception as error:   # kept AND counted: an incomplete teardown must not exit 0
        manifest['host_stop_error'] = repr(error)
        manifest['failures'].append({'host_stop_error': repr(error)})
    if host.root.exists():
        with tarfile.open(E / 'synthetic-host.tar.gz', 'w:gz') as tf:
            tf.add(host.root, arcname='synthetic-host')
    (E / 'frames-manifest.json').write_text(json.dumps(manifest, indent=2, default=str))
print(json.dumps({'cells': len(manifest['cells']), 'failures': len(manifest['failures'])}))
raise SystemExit(1 if manifest['failures'] else 0)
