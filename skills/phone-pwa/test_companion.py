"""Lifecycle orchestration with an injected transport, no owner state or internet."""
import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from types import SimpleNamespace
import companion


class Response:
    status = 200
    async def __aenter__(self): return self
    async def __aexit__(self, *_): pass
    async def json(self): return {'phone_gateway': 'test-generation'}


class Gateway:
    instances = []
    def __init__(self, state, upstream, public_port, admin_port):
        self.__class__.instances.append(self)
        self.state, self.upstream, self.public_port = state, upstream, 12345
        self.gateway_id = 'test-generation'
        self.origin = ''
        self.current_pair = None
        self.store = SimpleNamespace(pending={})
        self.inflight = {}
        self.session = SimpleNamespace(get=lambda *a, **k: Response())
        self.stopped = False
    async def start(self): pass
    async def stop(self): self.stopped = True


class Tunnel:
    instances = []
    def __init__(self, *_):
        self.__class__.instances.append(self)
        self.returncode = None
        self.stopped = False
    async def start(self): pass
    async def wait_url(self, *_): return 'https://phone.example/'
    async def stop(self): self.stopped = True; self.returncode = 0


class CompanionTest(unittest.IsolatedAsyncioTestCase):
    async def test_lifetime_retry_and_stop_own_only_injected_tunnel(self):
        with tempfile.TemporaryDirectory() as raw:
            state = Path(raw)
            (state/'runtime.json').write_text(json.dumps({'upstream': 'http://127.0.0.1:1234'}), encoding='utf-8')
            stops = []
            def lifeline(parent, loop, stop):
                stops.append(stop)
                return SimpleNamespace(set=lambda: None), SimpleNamespace(join=lambda **kw: None)
            async def ensure(_): return state/'verified-executable'
            Gateway.instances.clear(); Tunnel.instances.clear()
            with patch.dict('os.environ', {'OUROBOROS_SKILL_STATE_DIR': raw}), \
                 patch.object(companion, 'Gateway', Gateway), patch.object(companion, 'QuickTunnel', Tunnel), \
                 patch.object(companion, 'ensure_cloudflared', ensure), \
                 patch.object(companion, 'start_parent_lifeline', lifeline), \
                 patch.object(companion, 'require_isolated_process_group', lambda: None):
                task = asyncio.create_task(companion.run())
                try:
                    async def ready():
                        while not (state/'companion-status.json').exists() or json.loads((state/'companion-status.json').read_text())['phase'] != 'ready':
                            await asyncio.sleep(.01)
                    await asyncio.wait_for(ready(), 2)
                    self.assertEqual(Gateway.instances[0].origin, 'https://phone.example')
                    self.assertTrue(json.loads((state/'companion-status.json').read_text())['core_available'])
                finally:
                    stops[0].set()
                    await asyncio.wait_for(task, 2)
                self.assertTrue(Gateway.instances[0].stopped)
                self.assertTrue(Tunnel.instances[0].stopped)
                self.assertEqual(json.loads((state/'companion-status.json').read_text())['phase'], 'stopped')

    async def test_startup_failure_survives_cleanup_without_secret_exception_text(self):
        for stage in ('interface', 'download'):
            with self.subTest(stage=stage), tempfile.TemporaryDirectory() as raw:
                state = Path(raw)
                (state/'runtime.json').write_text(json.dumps({'upstream': 'http://127.0.0.1:1234'}))
                class Broken(Gateway):
                    async def start(self):
                        if stage == 'interface': raise ValueError('secret URL should never be rendered')
                async def ensure(_): raise ValueError('secret URL should never be rendered')
                def lifeline(*_):
                    return SimpleNamespace(set=lambda: None), SimpleNamespace(join=lambda **kw: None)
                with patch.dict('os.environ', {'OUROBOROS_SKILL_STATE_DIR': raw}), \
                     patch.object(companion, 'Gateway', Broken), patch.object(companion, 'ensure_cloudflared', ensure), \
                     patch.object(companion, 'start_parent_lifeline', lifeline), \
                     patch.object(companion, 'require_isolated_process_group', lambda: None):
                    with self.assertRaises(ValueError): await companion.run()
                saved = json.loads((state/'companion-status.json').read_text())
                self.assertEqual(saved['phase'], 'failed')
                self.assertNotIn('secret', saved['message'])
                self.assertIn('off and on', saved['message'])
                self.assertIn('Local interface' if stage == 'interface' else 'runtime preparation', saved['message'])

    async def test_live_unreachable_tunnel_exposes_retry_without_automatic_restart(self):
        with tempfile.TemporaryDirectory() as raw:
            state = Path(raw)
            (state/'runtime.json').write_text(json.dumps({'upstream': 'http://127.0.0.1:1234'}))
            stops = []
            def lifeline(parent, loop, stop):
                stops.append(stop)
                return SimpleNamespace(set=lambda: None), SimpleNamespace(join=lambda **kw: None)
            async def ensure(_): return state/'verified-executable'
            class OfflineGateway(Gateway):
                def __init__(self, *args):
                    super().__init__(*args)
                    # Real client failures, not an uncaught arbitrary exception.
                    from aiohttp import ClientConnectionError
                    def unavailable(url, **kw):
                        if url.startswith('https:'): raise ClientConnectionError()
                        return Response()
                    self.session = SimpleNamespace(get=unavailable)
            Tunnel.instances.clear()
            with patch.dict('os.environ', {'OUROBOROS_SKILL_STATE_DIR': raw}), \
                 patch.object(companion, 'Gateway', OfflineGateway), patch.object(companion, 'QuickTunnel', Tunnel), \
                 patch.object(companion, 'ensure_cloudflared', ensure), patch.object(companion, 'start_parent_lifeline', lifeline), \
                 patch.object(companion, 'require_isolated_process_group', lambda: None):
                task = asyncio.create_task(companion.run())
                try:
                    async def unavailable():
                        while not (state/'companion-status.json').exists() or json.loads((state/'companion-status.json').read_text())['phase'] != 'unavailable':
                            await asyncio.sleep(.01)
                    await asyncio.wait_for(unavailable(), 2)
                    self.assertEqual(len(Tunnel.instances), 1)
                    self.assertIn('Retry', json.loads((state/'companion-status.json').read_text())['message'])
                finally:
                    stops[0].set(); await asyncio.wait_for(task, 2)

    async def test_second_companion_refuses_without_mutating_existing_lifetime(self):
        from platform_support import acquire_file_lock, release_file_lock, PlatformSupportError
        with tempfile.TemporaryDirectory() as raw:
            fd = acquire_file_lock(Path(raw)/'companion.lock', timeout_sec=0)
            try:
                with patch.dict('os.environ', {'OUROBOROS_SKILL_STATE_DIR': raw}), patch.object(companion, 'require_isolated_process_group', lambda: None):
                    with self.assertRaises(PlatformSupportError):
                        await companion.run()
            finally:
                release_file_lock(fd)


if __name__ == '__main__': unittest.main()
