"""Real sockets and isolated upstream: no owner state, model call or core mutation."""
import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from aiohttp import web, ClientSession, DummyCookieJar, WSMsgType
from server import Gateway, COOKIE
from pairing import Pairing


class PairingTest(unittest.TestCase):
    def test_ttl_replay_revoke_restart_and_clock_rollback(self):
        with tempfile.TemporaryDirectory() as directory:
            now=[1000.]
            store=Pairing(directory,clock=lambda:now[0])
            link=store.issue(); now[0]=1600.
            with self.assertRaises(ValueError):store.redeem(link['token'],'expired')
            link=store.issue();store.revoke_pair(link['id'])
            with self.assertRaises(ValueError):store.redeem(link['token'],'revoked')
            link=store.issue();device,secret=store.redeem(link['token'],'my phone')
            other,other_secret=store.redeem(link['token'],'Safari')
            self.assertNotEqual(device,other);self.assertNotEqual(secret,other_secret)
            store.revoke_pair(link['id'])
            with self.assertRaises(ValueError):store.redeem(link['token'],'revoked window')
            restarted=Pairing(directory,clock=lambda:now[0]);self.assertEqual(restarted.device(secret),device)
            with self.assertRaises(ValueError):restarted.redeem(store.issue()['token'],'prior generation')
            now[0]=1599.;self.assertIsNone(restarted.device(secret))
            now[0]=1700.;restarted.revoke(device);self.assertIsNone(restarted.device(secret))
            self.assertNotIn(secret,Path(directory,'devices.sqlite').read_bytes().decode('latin1'))


class GatewayTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.hits=[];self.stream_closed=asyncio.Event();self.waiting=asyncio.Event();self.release=asyncio.Event()
        upstream=web.Application();upstream.router.add_route('*','/{tail:.*}',self.upstream)
        self.runner=web.AppRunner(upstream,handler_cancellation=True);await self.runner.setup();site=web.TCPSite(self.runner,'127.0.0.1',0);await site.start()
        self.target='http://127.0.0.1:'+str(site._server.sockets[0].getsockname()[1])
        self.gateway=Gateway(self.tmp.name,self.target);await self.gateway.start();self.gateway.origin='https://phone.example';self.hits.clear()
        self.base='http://127.0.0.1:'+str(self.gateway.public_port)
        self.client=ClientSession(cookie_jar=DummyCookieJar())
        self.headers={'Host':'phone.example','Origin':'https://phone.example'}

    async def asyncTearDown(self):
        self.release.set();await self.client.close();await self.gateway.stop();await self.runner.cleanup();self.tmp.cleanup()

    async def upstream(self,request):
        self.hits.append((request.method,request.raw_path,dict(request.headers)))
        if request.path=='/delayed':
            self.waiting.set();await self.release.wait()
            return web.Response(text='must not arrive after revocation')
        if request.path=='/ws':
            if request.query.get('delayed'):
                self.waiting.set();await self.release.wait()
            ws=web.WebSocketResponse();await ws.prepare(request)
            async for message in ws:
                if message.type==WSMsgType.TEXT:await ws.send_str(message.data)
            return ws
        if request.path=='/sse-broken':
            response=web.StreamResponse(headers={'Content-Type':'text/event-stream'});await response.prepare(request)
            await response.write(b'data: incomplete\n')
            request.transport.abort()
            return response
        if request.path=='/sse-finite':
            return web.Response(body=b'data: complete\n\n',headers={'Content-Type':'text/event-stream'})
        if request.path=='/static/modules/chat.js':
            from keyboard import OLD,NEW
            text=(NEW if request.query.get('patched') else OLD) + '\nif (preview.show()) { updateMessagesPadding(); reading.reflow(); }'
            return web.Response(text=text,content_type='application/javascript')
        if request.path=='/sse':
            response=web.StreamResponse(headers={'Content-Type':'text/event-stream'});await response.prepare(request)
            try:
                for i in range(100):
                    await response.write(f'id: {i}\ndata: {i}\n\n'.encode());await asyncio.sleep(.02)
            except (ConnectionError,asyncio.CancelledError):self.stream_closed.set()
            return response
        if request.path=='/redirect':return web.Response(status=302,headers={'Location':'https://elsewhere.example/'})
        if request.path=='/':return web.Response(text='<!doctype html><html><head></head><body>isolated upstream</body></html>',content_type='text/html')
        if request.path=='/bytes':
            body=await request.read();return web.Response(body=body or b'\x00\xff\r\n',headers={'Set-Cookie':'core_private=never; Path=/'})
        return web.json_response({'path':request.raw_path,'data':(await request.read()).decode('utf-8')})

    async def paired(self):
        issued=self.gateway.store.issue()
        r=await self.client.post(self.base+'/_phone/redeem',headers=self.headers,json={'token':issued['token'],'label':'test'})
        self.assertEqual(r.status,200,await r.text());d=await r.json()
        value=r.cookies[COOKIE].value
        self.assertIn('Secure',r.headers['Set-Cookie']);self.assertIn('HttpOnly',r.headers['Set-Cookie'])
        return d['id'],{**self.headers,'Cookie':f'{COOKIE}={value}'}

    async def test_anonymous_and_host_origin_spoof_never_reach_core(self):
        for path in ['/api/state','/api/files/read?path=private','/api/extensions/x/read','/ws']:
            r=await self.client.get(self.base+path,headers=self.headers);self.assertEqual(r.status,401)
        for headers in ({'Host':'evil.example'}, {**self.headers,'Origin':'https://evil.example'}, {**self.headers,'X-Forwarded-For':'127.0.0.1','Host':'evil.example'}):
            r=await self.client.get(self.base+'/api/state',headers=headers);self.assertEqual(r.status,403)
        self.assertEqual(self.hits,[])

    async def test_repeated_redemption_window_requires_explicit_post(self):
        issued=self.gateway.store.issue()
        r=await self.client.get(self.base+'/_phone/connect',headers=self.headers);self.assertEqual(r.status,200)
        async def attempt():
            r=await self.client.post(self.base+'/_phone/redeem',headers=self.headers,json={'token':issued['token']});return r.status
        self.assertEqual(sorted(await asyncio.gather(attempt(),attempt())),[200,200])
        self.assertEqual(self.hits,[])

    async def test_proxy_upload_query_binary_cookie_isolation_and_overlay(self):
        _,headers=await self.paired()
        r=await self.client.get(self.base+'/',headers=headers);text=await r.text();self.assertIn('/_phone/assets/bootstrap.js',text)
        r=await self.client.post(self.base+'/bytes',headers={**headers,'Authorization':'Bearer no','X-Forwarded-Host':'evil'},data=b'\0\xffabc')
        self.assertEqual(await r.read(),b'\0\xffabc');self.assertNotIn('Set-Cookie',r.headers)
        r=await self.client.get(self.base+'/api/state?cursor=abc%2B123',headers=headers);self.assertEqual((await r.json())['path'],'/api/state?cursor=abc%2B123')
        for _,_,sent in self.hits:
            self.assertNotIn('Cookie',sent);self.assertNotIn('Authorization',sent);self.assertNotIn('X-Forwarded-Host',sent)
        self.assertEqual(self.hits[-1][2]['Host'],self.target.removeprefix('http://'))

    async def test_csrf_and_revocation_isolated_devices(self):
        device,a=await self.paired();_,b=await self.paired()
        for headers in ({'Host':'phone.example','Cookie':a['Cookie']},{**a,'Origin':'null'}):
            r=await self.client.post(self.base+'/api/change',headers=headers,json={});self.assertEqual(r.status,403)
        await self.gateway.revoke(device)
        r=await self.client.get(self.base+'/api/state',headers=a);self.assertEqual(r.status,401)
        r=await self.client.get(self.base+'/api/state',headers=b);self.assertEqual(r.status,200)

    async def test_websocket_echo_and_active_revoke(self):
        device,headers=await self.paired()
        async with self.client.ws_connect(self.base+'/ws',headers=headers) as ws:
            await ws.send_str('synthetic not an owner message');self.assertEqual((await ws.receive()).data,'synthetic not an owner message')
            await self.gateway.revoke(device)
            result=await asyncio.wait_for(ws.receive(),2);self.assertIn(result.type,(WSMsgType.CLOSE,WSMsgType.CLOSED))

    async def test_post_sse_bridge_and_disconnect(self):
        device,headers=await self.paired()
        r=await self.client.post(self.base+'/sse',headers={**headers,'X-Phone-Stream':'websocket'},json={'v':2,'cursor':{'x':1}})
        self.assertEqual(r.headers['X-Phone-Stream'],'websocket');data=await r.json()
        _,other=await self.paired()
        with self.assertRaises(Exception):await self.client.ws_connect(self.base+'/_phone/stream/'+data['stream'],headers=other)
        async with self.client.ws_connect(self.base+'/_phone/stream/'+data['stream'],headers=headers) as ws:
            self.assertIn(b'data: 0', (await ws.receive()).data)
            await self.gateway.revoke(device)
            message=await asyncio.wait_for(ws.receive(),2)
            while message.type==WSMsgType.BINARY:message=await asyncio.wait_for(ws.receive(),2)
            self.assertIn(message.type,(WSMsgType.CLOSE,WSMsgType.CLOSED))
        await asyncio.wait_for(self.stream_closed.wait(),3)

    async def test_management_separate_capability_and_no_dns_rebinding(self):
        admin='http://127.0.0.1:'+str(self.gateway.admin_port)
        r=await self.client.get(admin+'/status');self.assertEqual(r.status,401)
        auth={'Authorization':'Bearer '+self.gateway.admin_key}
        r=await self.client.get(admin+'/status',headers=auth);self.assertEqual(r.status,200)
        r=await self.client.post(admin+'/pair',headers={**auth,'Origin':'https://evil.example'},json={});self.assertEqual(r.status,403)
        r=await self.client.post(admin+'/pair',headers={**auth,'Host':'evil.example'},json={});self.assertEqual(r.status,403)
        _,headers=await self.paired();r=await self.client.post(self.base+'/_phone/pair',headers=headers,json={});self.assertEqual(r.status,404)

    async def test_revoke_during_header_and_ws_acquisition(self):
        for path in ('/delayed','/ws?delayed=1'):
            self.waiting.clear();self.release.clear()
            device,headers=await self.paired()
            async def connect():
                if path.startswith('/ws'):
                    return await self.client.ws_connect(self.base+path,headers=headers)
                return await self.client.get(self.base+path,headers=headers)
            task=asyncio.create_task(connect())
            await asyncio.wait_for(self.waiting.wait(),2)
            await self.gateway.revoke(device)
            if path.startswith('/ws'):
                with self.assertRaises(Exception):await asyncio.wait_for(task,2)
            else:
                # aiohttp retries an idempotent GET after disconnect; that retry must be unauthorized.
                response=await asyncio.wait_for(task,2)
                self.assertEqual(response.status,401)
            self.release.set();await asyncio.sleep(.05)
            self.assertFalse(self.gateway.inflight);self.assertFalse(self.gateway.connections)

    async def test_disconnect_before_headers_cancels_acquisition(self):
        _,headers=await self.paired()
        task=asyncio.create_task(self.client.get(self.base+'/delayed',headers=headers))
        await asyncio.wait_for(self.waiting.wait(),2)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):await task
        async def settled():
            while self.gateway.inflight:await asyncio.sleep(.01)
        await asyncio.wait_for(settled(),2)
        self.release.set()
        # A legitimate delayed read still completes normally when released.
        r=await self.client.get(self.base+'/delayed',headers=headers)
        self.assertEqual(await r.text(),'must not arrive after revocation')

    async def test_sse_broken_is_not_successful_eof(self):
        _,headers=await self.paired()
        for path,expected in (('/sse-finite',1000),('/sse-broken',1011)):
            r=await self.client.get(self.base+path,headers={**headers,'X-Phone-Stream':'websocket'})
            ticket=(await r.json())['stream']
            async with self.client.ws_connect(self.base+'/_phone/stream/'+ticket,headers=headers) as ws:
                async for _ in ws:pass
                self.assertEqual(ws.close_code,expected)

    async def test_keyboard_overlay_positive_and_already_patched(self):
        from keyboard import NEW
        _,headers=await self.paired()
        for suffix in ('','?patched=1'):
            r=await self.client.get(self.base+'/static/modules/chat.js'+suffix,headers=headers)
            self.assertEqual(r.status,200);self.assertEqual(await r.text(),NEW + '\nif (preview.show()) { updateMessagesPadding(); reading.followAfterLayout(); }')

    async def test_bounded_unauthenticated_json(self):
        r=await self.client.post(self.base+'/_phone/redeem',headers=self.headers,json={'token':'a'*10000})
        self.assertEqual(r.status,413)
        r=await self.client.post(self.base+'/_phone/redeem',headers=self.headers,json=[])
        self.assertEqual(r.status,410)
        _,headers=await self.paired()
        payload=b'x'*10000
        r=await self.client.post(self.base+'/bytes',headers=headers,data=payload)
        self.assertEqual(await r.read(),payload)

    async def test_same_link_multiple_sessions_manifest_handoff_and_expiry(self):
        issued=self.gateway.store.issue();cookies=[]
        for label in ('Browser A','Browser B','Installed app'):
            r=await self.client.post(self.base+'/_phone/redeem',headers=self.headers,json={'token':issued['token'],'label':label})
            self.assertEqual(r.status,200);cookies.append(r.cookies[COOKIE].value)
        self.assertEqual(len(set(cookies)),3)
        r=await self.client.get(self.base+'/_phone/manifest.webmanifest?pair='+issued['token'],headers=self.headers)
        self.assertEqual((await r.json())['start_url'],'/_phone/connect#'+issued['token'])
        self.assertEqual(r.headers['Cache-Control'],'no-store')
        self.gateway.store.revoke_pair(issued['id'])
        r=await self.client.get(self.base+'/_phone/manifest.webmanifest?pair='+issued['token'],headers=self.headers);self.assertEqual(r.status,410)
        r=await self.client.post(self.base+'/_phone/redeem',headers=self.headers,json={'token':issued['token']});self.assertEqual(r.status,410)
        for cookie in cookies:
            r=await self.client.get(self.base+'/_phone/session',headers={**self.headers,'Cookie':f'{COOKIE}={cookie}'})
            self.assertEqual(r.status,200)

    async def test_no_service_worker_or_secret_start_url(self):
        r=await self.client.get(self.base+'/_phone/manifest.webmanifest',headers=self.headers);m=await r.json()
        self.assertEqual(m['start_url'],'/');self.assertNotIn('pair',json.dumps(m))
        r=await self.client.get(self.base+'/_phone/assets/bootstrap.js',headers=self.headers)
        self.assertNotIn('serviceWorker.register',await r.text())
        r=await self.client.get(self.base+'/_phone/connect',headers=self.headers)
        self.assertEqual(r.headers['Cache-Control'],'no-store');self.assertEqual(r.headers['Referrer-Policy'],'no-referrer')

    async def test_management_prefix_excluded_but_other_extension_works(self):
        _, headers = await self.paired()
        for path in ('/api/extensions/phone-pwa', '/api/extensions/phone-pwa/pair', '/api/extensions/phone-pwa%2fstatus'):
            r = await self.client.get(self.base + path, headers=headers)
            self.assertEqual(r.status, 403)
        self.assertEqual(self.hits, [])
        r = await self.client.get(self.base + '/api/extensions/other/status', headers=headers)
        self.assertEqual(r.status, 200)

    async def test_origin_rotation_cancels_header_acquisition(self):
        device, headers = await self.paired()
        task = asyncio.create_task(self.client.get(self.base + '/delayed', headers=headers))
        await asyncio.wait_for(self.waiting.wait(), 2)
        admin = 'http://127.0.0.1:' + str(self.gateway.admin_port)
        r = await self.client.post(admin + '/origin', headers={'Authorization': 'Bearer ' + self.gateway.admin_key},
                                   json={'origin': 'https://new.example'})
        self.assertEqual(r.status, 200)
        r = await asyncio.wait_for(task, 2)
        self.assertEqual(r.status, 403)
        self.assertFalse(self.gateway.inflight)
        self.assertFalse(self.gateway.connections)
        self.release.set()

    async def test_one_ticket_concurrent_same_device_claim(self):
        _, headers = await self.paired()
        r = await self.client.get(self.base + '/sse', headers={**headers, 'X-Phone-Stream': 'websocket'})
        key = (await r.json())['stream']
        async def claim():
            try:
                return await self.client.ws_connect(self.base + '/_phone/stream/' + key, headers=headers)
            except Exception:
                return None
        a, b = await asyncio.gather(claim(), claim())
        self.assertEqual(sum(x is not None for x in (a, b)), 1)
        for ws in (a, b):
            if ws is not None:
                await ws.close()
        self.assertNotIn(key, self.gateway.streams)

    async def test_widget_pair_status_and_revoke_keep_device(self):
        admin = 'http://127.0.0.1:' + str(self.gateway.admin_port)
        auth = {'Authorization': 'Bearer ' + self.gateway.admin_key}
        r = await self.client.post(admin + '/pair', headers=auth, json={})
        data = await r.json()
        token = data['link'].split('#')[1]
        r = await self.client.post(self.base + '/_phone/redeem', headers=self.headers, json={'token': token})
        cookie = r.cookies[COOKIE].value
        r = await self.client.get(admin + '/status', headers=auth)
        self.assertEqual((await r.json())['pair']['id'], data['id'])
        await self.client.post(admin + '/revoke-pair', headers=auth, json={'id': data['id']})
        r = await self.client.get(admin + '/status', headers=auth)
        self.assertIsNone((await r.json())['pair'])
        self.assertIsNotNone(self.gateway.store.device(cookie))


class StateRootTest(unittest.TestCase):
    def test_custom_host_root_and_escape(self):
        from unittest.mock import patch
        from server import production_state
        with tempfile.TemporaryDirectory() as directory:
            with patch.dict('os.environ', {'OUROBOROS_SKILL_STATE_DIR': directory}):
                self.assertEqual(production_state(), Path(directory).resolve() / 'gateway')
                with self.assertRaises(ValueError):
                    production_state(str(Path(directory).parent / 'elsewhere'))


if __name__=='__main__':unittest.main(verbosity=2)
