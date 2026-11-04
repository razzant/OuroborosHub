"""Split-listener phone gateway. Never tunnel the admin listener or the core port.

No proxy headers are trusted. One process/event loop owns pending pairing and streams;
SQLite owns persistent device hashes. No core password, shared cookie jar or SW cache.
"""
import argparse
import asyncio
import contextlib
import io
import json
import os
import secrets
from pathlib import Path
from urllib.parse import urlsplit, unquote

from aiohttp import web, ClientSession, ClientTimeout, DummyCookieJar, WSMsgType
import qrcode
import qrcode.image.svg
from pairing import Pairing
from keyboard import patch_chat

HERE = Path(__file__).resolve().parent
COOKIE = '__Host-ouro_phone'
HOP = {'connection', 'keep-alive', 'proxy-authenticate', 'proxy-authorization',
       'te', 'trailer', 'transfer-encoding', 'upgrade', 'host', 'content-length'}
CREDENTIALS = {'cookie', 'authorization', 'x-ouroboros-password', 'x-skill-token',
               'x-phone-stream', 'x-ouroboros-telegram-miniapp'}
SAFE = {'Cache-Control': 'no-store', 'Referrer-Policy': 'no-referrer',
        'X-Content-Type-Options': 'nosniff', 'X-Frame-Options': 'DENY'}
CSP = "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"


def origin_valid(value):
    p = urlsplit(value)
    return p.scheme == 'https' and p.hostname and p.netloc == p.hostname and not p.path and not p.query and not p.fragment


def clean_headers(headers, response=False):
    nominated = set()
    for value in headers.getall('Connection', []):
        nominated.update(x.strip().lower() for x in value.split(','))
    blocked = HOP | nominated | ({'set-cookie', 'clear-site-data'} if response else CREDENTIALS)
    return {k: v for k, v in headers.items() if k.lower() not in blocked
            and not k.lower().startswith(('forwarded', 'x-forwarded-'))}


class Gateway:
    def __init__(self, state, upstream, public_port=0, admin_port=0, clock=None):
        p = urlsplit(upstream)
        if p.scheme != 'http' or p.hostname != '127.0.0.1' or not p.port or p.path not in ('','/') or p.query or p.fragment or p.username:
            raise ValueError('Upstream must be exactly http://127.0.0.1:PORT')
        self.upstream = upstream.rstrip('/')
        self.state = Path(state)
        self.store = Pairing(state, **({'clock': clock} if clock else {}))
        self.admin_key = secrets.token_urlsafe(32)
        self.gateway_id = secrets.token_hex(12)
        self.origin = ''
        self.current_pair = None
        self.runtime_status = lambda: {}
        self.retry_tunnel = None
        self.public_port, self.admin_port = public_port, admin_port
        self.inflight = {}  # request task -> device; covers acquisition/header waits too
        self.connections = {}  # socket -> device id (ordinary WS and SSE bridges)
        self.streams = {}  # pending upstream response, device, timeout task
        self.session = None
        self.runners = []
        self.public = web.Application(client_max_size=128 * 1024 * 1024)
        self.public.router.add_route('*', '/{tail:.*}', self.dispatch)
        self.admin = web.Application(client_max_size=8192)
        self.admin.router.add_route('*', '/{tail:.*}', self.manage)

    async def start(self):
        self.session = ClientSession(cookie_jar=DummyCookieJar(), trust_env=False,
                                     timeout=ClientTimeout(total=None, sock_connect=10))
        try:
            for path in ('/', '/static/modules/chat.js'):
                async with self.session.get(self.upstream + path, timeout=ClientTimeout(total=15)) as response:
                    if response.status != 200:
                        raise RuntimeError('Upstream unavailable: ' + path)
                    text = await response.text()
                    if path.endswith('chat.js'):
                        patch_chat(text)
        except BaseException:
            await self.session.close()
            raise
        for app, attr in ((self.public, 'public_port'), (self.admin, 'admin_port')):
            runner = web.AppRunner(app, access_log=None, handler_cancellation=True)
            await runner.setup()
            self.runners.append(runner)
            site = web.TCPSite(runner, '127.0.0.1', getattr(self, attr))
            await site.start()
            setattr(self, attr, site._server.sockets[0].getsockname()[1])
        # This file is private local startup material, never exposed by the public listener.
        receipt = {'public_port': self.public_port, 'admin_port': self.admin_port,
                   'admin_url': f'http://127.0.0.1:{self.admin_port}/#' + self.admin_key}
        (self.state / 'launch.json').write_text(json.dumps(receipt), encoding='utf-8')
        print('PHONE_GATEWAY_READY', flush=True)

    async def stop(self):
        for ws in list(self.connections):
            await ws.close(code=1001, message=b'Gateway stopped')
        for key in list(self.streams):
            self.close_stream(key)
        for runner in reversed(self.runners):
            await runner.cleanup()
        if self.session:
            await self.session.close()

    def close_stream(self, key):
        row = self.streams.pop(key, None)
        if row:
            row[0].close()
            row[2].cancel()

    async def revoke(self, key):
        self.store.revoke(key)
        for task, device in list(self.inflight.items()):
            if device == key:
                task.cancel()
        for ws, device in list(self.connections.items()):
            if key == device:
                await ws.close(code=4401, message=b'Device revoked')
        for stream, row in list(self.streams.items()):
            if row[1] == key:
                self.close_stream(stream)

    async def manage(self, request):
        expected = f'127.0.0.1:{self.admin_port}'
        if request.headers.getall('Host', []) != [expected]:
            raise web.HTTPForbidden()
        origin = request.headers.get('Origin')
        if origin and origin != 'http://' + expected:
            raise web.HTTPForbidden()
        if request.path == '/' and request.method == 'GET':
            return self.page('admin.html')
        if request.path in ('/phone.css', '/admin.js') and request.method == 'GET':
            return self.asset(request.path[1:])
        if not secrets.compare_digest(request.headers.get('Authorization', ''), 'Bearer ' + self.admin_key):
            raise web.HTTPUnauthorized()
        if request.path == '/status' and request.method == 'GET':
            pair = self.current_pair
            if pair and not any(row[0] == pair['id'] and self.store.clock() < row[1] for row in self.store.pending.values()):
                pair = None
            return web.json_response({'origin': self.origin, 'devices': self.store.list_devices(),
                                      'pair': pair, **self.runtime_status()}, headers=SAFE)
        if request.method != 'POST':
            raise web.HTTPMethodNotAllowed(request.method, ['POST'])
        data = await request.json()
        if request.path == '/origin':
            value = data.get('origin', '')
            if not isinstance(value, str) or not origin_valid(value):
                raise web.HTTPBadRequest(text='An exact HTTPS hostname is required.')
            if self.origin and self.origin != value:
                self.current_pair = None
                self.store.pending.clear()
                for task in list(self.inflight):
                    task.cancel()
                for ws in list(self.connections):
                    await ws.close(code=1001, message=b'Address changed')
                for key in list(self.streams):
                    self.close_stream(key)
            self.origin = value
            return web.json_response({'ok': True}, headers=SAFE)
        if request.path == '/pair':
            if not self.origin:
                raise web.HTTPConflict(text='Start the tunnel and set its current HTTPS address first.')
            result = self.store.issue()
            link = self.origin + '/_phone/connect#' + result.pop('token')
            output = io.BytesIO()
            qrcode.make(link, image_factory=qrcode.image.svg.SvgPathImage, border=4).save(output)
            result.update(link=link, qr=output.getvalue().decode('utf-8'))
            self.current_pair = result
            return web.json_response(result, headers=SAFE)
        if request.path == '/retry' and self.retry_tunnel:
            self.retry_tunnel()
        elif request.path == '/revoke-pair':
            self.store.revoke_pair(str(data.get('id', '')))
            if self.current_pair and self.current_pair['id'] == str(data.get('id', '')):
                self.current_pair = None
        elif request.path == '/revoke':
            await self.revoke(str(data.get('id', '')))
        else:
            raise web.HTTPNotFound()
        return web.json_response({'ok': True}, headers=SAFE)

    def page(self, name):
        return web.Response(body=(HERE / name).read_bytes(), content_type='text/html', headers={**SAFE, 'Content-Security-Policy': CSP})

    def asset(self, name):
        kinds = {'phone.css':'text/css', 'admin.js':'application/javascript',
                 'connect.js':'application/javascript', 'bootstrap.js':'application/javascript', 'icon.svg':'image/svg+xml',
                 'icon-180.png':'image/png','icon-192.png':'image/png','icon-512.png':'image/png'}
        return web.Response(body=(HERE / name).read_bytes(), content_type=kinds[name], headers=SAFE)

    def validate_public(self, request):
        if not self.origin or request.headers.getall('Host', []) != [urlsplit(self.origin).netloc]:
            raise web.HTTPForbidden(text='Address not configured.')
        origins = request.headers.getall('Origin', [])
        if len(origins) > 1 or (origins and origins != [self.origin]):
            raise web.HTTPForbidden(text='Foreign origin refused.')
        if request.method not in ('GET','HEAD') or request.headers.get('Upgrade', '').lower() == 'websocket':
            if origins != [self.origin]:
                raise web.HTTPForbidden(text='Same-origin confirmation required.')
        # Never allow ambiguous encoded separators or dot segments to reach a different router.
        raw = request.raw_path.split('?', 1)[0]
        decoded = unquote(raw)
        if '\\' in decoded or decoded.startswith('//') or any(x in ('.','..') for x in decoded.split('/')) or '%' in decoded:
            raise web.HTTPBadRequest(text='Ambiguous path refused.')
        return decoded

    async def dispatch(self, request):
        path = self.validate_public(request)
        if path == '/_phone/health' and request.method == 'GET':
            return web.json_response({'phone_gateway': self.gateway_id}, headers=SAFE)
        if path in ('/_phone/connect', '/_phone/help') and request.method == 'GET':
            return self.page('connect.html')
        if path.startswith('/_phone/assets/') and request.method == 'GET':
            name = path.rsplit('/', 1)[-1]
            if name not in ('phone.css','connect.js','bootstrap.js','icon.svg','icon-180.png','icon-192.png','icon-512.png'):
                raise web.HTTPNotFound()
            return self.asset(name)
        if path == '/_phone/manifest.webmanifest' and request.method == 'GET':
            pair = request.query.get('pair', '')
            if pair and (len(pair) > 128 or not self.store.active_pair(pair)):
                raise web.HTTPGone(text='Connection window expired or revoked.')
            return web.json_response({'id':'/', 'name':'Ouroboros', 'short_name':'Ouroboros',
                'start_url':('/_phone/connect#' + pair) if pair else '/', 'scope':'/', 'display':'standalone', 'background_color':'#101114',
                'theme_color':'#101114', 'icons':[
                    {'src':f'/_phone/assets/icon-{size}.png','sizes':f'{size}x{size}','type':'image/png','purpose':'any'}
                    for size in (192,512)]},
                content_type='application/manifest+json', headers=SAFE)
        if path == '/_phone/redeem' and request.method == 'POST':
            try:
                raw = bytearray()
                async for chunk in request.content.iter_chunked(1024):
                    raw.extend(chunk)
                    if len(raw) > 4096:
                        raise web.HTTPRequestEntityTooLarge(max_size=4096, actual_size=len(raw))
                data = json.loads(raw)
                if not isinstance(data, dict):
                    raise ValueError('Invalid connection request.')
                token, label = data.get('token'), data.get('label','Phone')
                if not isinstance(token, str) or len(token) > 128 or not isinstance(label, str):
                    raise ValueError('Invalid connection link.')
                key, secret = self.store.redeem(token, label)
            except (ValueError, json.JSONDecodeError) as exc:
                raise web.HTTPGone(text=str(exc))
            result = web.json_response({'ok':True, 'id':key}, headers=SAFE)
            result.set_cookie(COOKIE, secret, max_age=30*86400, secure=True, httponly=True, samesite='Strict', path='/')
            return result
        device = self.store.device(request.cookies.get(COOKIE, ''))
        if not device:
            if request.method == 'GET' and (path == '/' or 'text/html' in request.headers.get('Accept','')):
                return self.page('connect.html')
            raise web.HTTPUnauthorized(text='Connect this browser using a fresh PC QR.')
        if path == '/_phone/session' and request.method == 'GET':
            return web.json_response({'ok':True,'id':device},headers=SAFE)
        if path == '/_phone/logout' and request.method == 'POST':
            await self.revoke(device)
            response = web.json_response({'ok': True}, headers=SAFE)
            response.del_cookie(COOKIE, path='/')
            return response
        task = asyncio.create_task(self.authorized(request, device, path))
        self.inflight[task] = device
        try:
            response = await task
            if self.store.device(request.cookies.get(COOKIE,'')) != device:
                raise web.HTTPUnauthorized(text='Device revoked.')
            return response
        except asyncio.CancelledError:
            if request.transport:
                request.transport.close()
            raise
        finally:
            self.inflight.pop(task, None)

    async def authorized(self, request, device, path):
        # Keep the direct management UI off the public proxy. Paired phones are
        # full owners; this is not a sandbox against their ordinary Files controls.
        if path == '/api/extensions/phone-pwa' or path.startswith('/api/extensions/phone-pwa/'):
            raise web.HTTPForbidden(text='Use the PC widget to manage phone access.')
        if path.startswith('/_phone/stream/'):
            return await self.sse_socket(request, device, path.rsplit('/',1)[-1])
        if path.startswith('/_phone/'):
            raise web.HTTPNotFound()
        if request.headers.get('Upgrade','').lower() == 'websocket':
            if path != '/ws':
                raise web.HTTPNotFound()
            return await self.websocket(request, device)
        return await self.proxy(request, device, path)

    async def guard(self, request, device):
        while True:
            await asyncio.sleep(0.5)
            if self.store.device(request.cookies.get(COOKIE,'')) != device:
                return

    async def websocket(self, request, device):
        headers = clean_headers(request.headers)
        headers.pop('Origin', None)
        # ClientSession constructs its own websocket handshake; no forwarded key/extensions.
        headers = {k:v for k,v in headers.items() if not k.lower().startswith('sec-websocket-')}
        target = self.upstream + request.raw_path
        async with self.session.ws_connect(target, headers=headers, origin=self.upstream, max_msg_size=128*1024*1024) as upstream:
            client = web.WebSocketResponse(max_msg_size=128*1024*1024)
            await client.prepare(request)
            self.connections[client] = device
            async def relay(source, destination):
                async for message in source:
                    if message.type == WSMsgType.TEXT:
                        await destination.send_str(message.data)
                    elif message.type == WSMsgType.BINARY:
                        await destination.send_bytes(message.data)
                    else:
                        break
            tasks = [asyncio.create_task(relay(client, upstream)), asyncio.create_task(relay(upstream, client)),
                     asyncio.create_task(self.guard(request, device))]
            try:
                await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            finally:
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
                self.connections.pop(client, None)
                await client.close()
            return client

    async def proxy(self, request, device, path):
        headers = clean_headers(request.headers)
        headers.pop('Origin', None)
        # Core sees its own loopback origin, not the remote browser's untrusted forwarding chain.
        if request.headers.get('Origin'):
            headers['Origin'] = self.upstream
        headers['Accept-Encoding'] = 'identity'
        body = request.content.iter_chunked(65536) if request.can_read_body else None
        upstream = await self.session.request(request.method, self.upstream + request.raw_path,
                                              headers=headers, data=body, allow_redirects=False)
        out = clean_headers(upstream.headers, response=True)
        out.update(SAFE)
        if upstream.headers.get('Content-Type','').split(';')[0] == 'text/event-stream':
            if request.headers.get('X-Phone-Stream') != 'websocket':
                upstream.close()
                raise web.HTTPConflict(text='This SSE consumer requires the phone stream adapter.')
            key = secrets.token_urlsafe(24)
            async def expire():
                await asyncio.sleep(15)
                self.close_stream(key)
            self.streams[key] = (upstream, device, asyncio.create_task(expire()))
            return web.json_response({'stream':key,'status':upstream.status,'headers':out},
                                     headers={**SAFE, 'X-Phone-Stream':'websocket'})
        if request.method == 'GET' and path == '/static/modules/chat.js' and upstream.status == 200:
            try:
                text = (await upstream.read()).decode('utf-8')
                text = patch_chat(text)
            except ValueError as exc:
                raise web.HTTPConflict(text=str(exc))
            finally:
                upstream.close()
            out.pop('Content-Encoding', None)
            out.pop('ETag', None)
            return web.Response(body=text.encode('utf-8'), status=200, headers=out)
        if request.method == 'GET' and path == '/' and 'text/html' in upstream.headers.get('Content-Type',''):
            try:
                raw = await upstream.read()
            finally:
                upstream.close()
            text = raw.decode('utf-8')
            overlay = '<link rel="manifest" href="/_phone/manifest.webmanifest"><script src="/_phone/assets/bootstrap.js"></script>'
            text = text.replace('</head>', overlay + '</head>', 1)
            out.pop('Content-Encoding', None)
            out.pop('ETag', None)
            return web.Response(body=text.encode('utf-8'), status=upstream.status, headers=out)
        response = web.StreamResponse(status=upstream.status, headers=out)
        watch = asyncio.create_task(self.guard(request, device))
        async def send_body():
            await response.prepare(request)
            async for chunk in upstream.content.iter_chunked(65536):
                await response.write(chunk)
            await response.write_eof()
        sender = asyncio.create_task(send_body())
        try:
            done, _ = await asyncio.wait((sender,watch), return_when=asyncio.FIRST_COMPLETED)
            if sender in done:
                await sender
            elif request.transport:
                request.transport.close()
        finally:
            sender.cancel(); watch.cancel()
            await asyncio.gather(sender, watch, return_exceptions=True)
            upstream.close()
        return response

    async def sse_socket(self, request, device, key):
        if request.headers.get('Upgrade','').lower() != 'websocket':
            raise web.HTTPBadRequest()
        row = self.streams.get(key)
        if not row or row[1] != device:
            raise web.HTTPNotFound()
        # Claim before the first await: concurrent same-device sockets cannot
        # both acquire this one response.
        self.streams.pop(key)
        upstream, _, timeout = row
        timeout.cancel()
        client = web.WebSocketResponse()
        try:
            await client.prepare(request)
        except BaseException:
            upstream.close()
            raise
        self.connections[client] = device
        async def pump():
            async for chunk in upstream.content.iter_any():
                await client.send_bytes(chunk)
        async def disconnected():
            async for _ in client:
                pass
        tasks = [asyncio.create_task(pump()), asyncio.create_task(disconnected()), asyncio.create_task(self.guard(request,device))]
        close_code = 1011
        try:
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            if tasks[2] in done:
                close_code = 4401
            elif tasks[0] in done and not tasks[0].cancelled() and tasks[0].exception() is None:
                close_code = 1000
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            upstream.close()
            self.connections.pop(client, None)
            await client.close(code=close_code)
        return client


def production_state(value=None):
    declared = os.environ.get('OUROBOROS_SKILL_STATE_DIR')
    data = Path(os.environ.get('OUROBOROS_DATA_DIR', str(Path.home() / 'Ouroboros' / 'data')))
    root = Path(declared).resolve() if declared else (data / 'state' / 'skills' / 'phone-pwa').resolve()
    state = Path(value).resolve() if value else root / 'gateway'
    if not state.is_relative_to(root):
        raise ValueError('State must stay inside the dedicated phone-pwa skill state directory.')
    return state


async def main(args):
    gateway = Gateway(production_state(args.state), args.upstream, args.public_port, args.admin_port)
    await gateway.start()
    try:
        await asyncio.Event().wait()
    finally:
        await gateway.stop()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state', help='Dedicated phone-pwa state descendant; default: gateway/')
    parser.add_argument('--upstream', default='http://127.0.0.1:8765')
    parser.add_argument('--public-port', type=int, default=0)
    parser.add_argument('--admin-port', type=int, default=0)
    try:
        asyncio.run(main(parser.parse_args()))
    except KeyboardInterrupt:
        pass
