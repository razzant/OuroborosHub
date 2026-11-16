"""Temporary request identity and byte custody; no URL-based failure exemptions."""
import base64
import hashlib
import json
import time
from pathlib import Path


class Trace:
    def __init__(self, root, cell):
        self.root, self.cell, self.step = root, cell, 'setup'
        self.events, self.requests = [], {}
        self.browsers, self.coverage_errors = [], []
        self.stream = (root / f'{cell}.requests.jsonl').open('x', encoding='utf-8')

    def event(self, kind, **facts):
        row = dict(kind=kind, cell=self.cell, step=self.step, order=len(self.events),
                   received_ns=time.monotonic_ns(), **facts)
        self.events.append(row)
        self.stream.write(json.dumps(row) + '\n')
        self.stream.flush()
        return row

    def request(self, request):
        if request not in self.requests:
            self.requests[request] = self.event('request', request_id=len(self.requests) + 1,
                id=request.headers.get('x-atlas-qa-request'), url=request.url, method=request.method)
        return self.requests[request]

    def install(self, page):
        def console(message):
            if message.text.startswith('__ATLAS_QA__'):
                self.event('js', payload=json.loads(message.text[len('__ATLAS_QA__'):]))
        page.on('console', console)
        page.on('request', self.request)
        page.on('response', lambda r: self.event('response', request_id=self.request(r.request)['request_id'], status=r.status))
        page.on('requestfinished', lambda r: self.event('finished', request_id=self.request(r)['request_id']))
        page.on('requestfailed', lambda r: self.event('failed', request_id=self.request(r)['request_id'], failure=r.failure))
        page.on('framedetached', lambda f: self.event('frame_detached', url=f.url))
        page.on('close', lambda: self.event('page_closed'))
        page.add_init_script(path=str(Path(__file__).with_suffix('.js')))

    def playwright(self, pw):
        # Use Card unchanged, intercept only creation so setup and teardown are covered.
        trace = self

        class Proxy:
            def __init__(self, obj):
                self.obj = obj

            def __getattr__(self, name):
                value = getattr(self.obj, name)
                if name in ('chromium', 'webkit'):
                    return Proxy(value)
                if name in ('launch', 'new_context', 'new_page'):
                    def create(*args, **kwargs):
                        result = value(*args, **kwargs)
                        if name == 'launch':
                            trace.browsers.append(result)
                        if name == 'new_page':
                            trace.install(result)
                            return result
                        return Proxy(result)
                    return create
                return value

        return Proxy(pw)

    def finish(self):
        """Classify only after browser.close has delivered its terminal callbacks."""
        decisions = []
        routes = [r for r in self.requests.values() if '/api/extensions/memory-atlas/atlas/v2/' in r['url']]
        if not routes:
            self.coverage_errors.append('No Atlas data requests observed')
        for r in routes:
            native = [e for e in self.events if e['kind'] == 'js' and e['payload']['kind'] == 'native_fetch'
                      and r['id'] and e['payload'].get('id') == r['id']]
            terminal = [e for e in self.events if e.get('request_id') == r['request_id']
                        and e['kind'] in ('finished', 'failed')]
            if not r['id'] or len(native) != 1 or len(terminal) != 1:
                self.coverage_errors.append({'request_id': r['request_id'], 'id': r['id'],
                                             'native_fetches': len(native), 'terminal_events': len(terminal)})
        for request in self.requests.values():
            rid, ident = request['request_id'], request['id']
            network = [e for e in self.events if e.get('request_id') == rid]
            failed = [e for e in network if e['kind'] == 'failed']
            if not failed:
                continue
            observed = [e for e in self.events if e['kind'] == 'js' and ident
                        and e['payload'].get('id') == ident]
            def select(kind):
                return [e for e in observed if e['payload']['kind'] == kind]
            native = b''.join(base64.b64decode(e['payload']['data'], validate=True) for e in select('native_chunk'))
            bridge = [e for e in select('bridge') if e['payload']['direction'] == 'receive'
                      and e['payload'].get('phase') in ('data', 'end')]
            delivered = b''.join(base64.b64decode(e['payload']['data'], validate=True)
                                 for e in bridge if e['payload']['phase'] == 'data')
            body = {}
            for name, data in (('native', native), ('delivered', delivered)):
                path = self.root / f'{self.cell}.request-{rid}.{name}.bin'
                path.write_bytes(data)
                body[name] = {'path': path.name, 'bytes': len(data), 'sha256': hashlib.sha256(data).hexdigest()}
            eof, consumed, abort = select('native_eof'), select('consumer_json_complete'), select('native_abort')
            end = [e for e in bridge if e['payload']['phase'] == 'end']
            # Browser clocks share epoch; per-document seq also survives delayed log delivery.
            stamp = lambda e: e['payload']['time_ms']
            proven = (ident and len([r for r in self.requests.values() if r['id'] == ident]) == 1
                and len(failed) == len(eof) == len(consumed) == len(abort) == len(end) == 1
                and failed[0]['failure'] == 'net::ERR_ABORTED'
                and len([e for e in network if e['kind'] == 'response' and e['status'] == 200]) == 1
                and consumed[0]['payload'].get('contract') == 'memory-atlas.read/2'
                and not consumed[0]['payload'].get('body_error')
                and native and native == delivered
                and stamp(eof[0]) <= stamp(end[0]) <= stamp(consumed[0]) < stamp(abort[0])
                and consumed[0]['order'] < failed[0]['order']
                and not any(select(k) for k in ('consumer_json_error', 'native_read_error', 'native_fetch_error')))
            decisions.append({'request_id': rid, 'id': ident, 'request_step': request['step'],
                'failed_step': failed[0]['step'], 'failure': failed, 'bodies': body,
                'evidence_orders': [e['order'] for e in observed],
                'classification': 'PROVEN_ABORT_AFTER_CONSUMPTION' if proven else 'UNRESOLVED_REQUEST_FAILURE'})
        self.event('trace_complete', coverage_errors=self.coverage_errors)
        self.stream.close()
        return decisions
