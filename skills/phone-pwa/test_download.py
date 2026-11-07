"""Real slow-trickle socket: no external download and no owner state."""
import asyncio
import hashlib
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch
from aiohttp import web
import cloudflare_tunnel as tunnel


class DownloadTest(unittest.IsolatedAsyncioTestCase):
    async def test_whole_deadline_ends_slow_trickle_and_valid_bytes_still_land(self):
        ended = asyncio.Event()
        async def handler(request):
            if request.path == '/fast':
                return web.Response(body=b'verified')
            r = web.StreamResponse(headers={'Content-Length': '10000'})
            await r.prepare(request)
            try:
                for _ in range(10000):
                    await r.write(b'x'); await asyncio.sleep(.025)
            except (ConnectionError, asyncio.CancelledError):
                pass
            finally:
                ended.set()
            return r
        app = web.Application(); app.router.add_get('/{path}', handler)
        runner = web.AppRunner(app, handler_cancellation=True)
        await runner.setup(); site = web.TCPSite(runner, '127.0.0.1', 0); await site.start()
        base = 'http://127.0.0.1:' + str(site._server.sockets[0].getsockname()[1])
        try:
            with tempfile.TemporaryDirectory() as raw, patch.object(tunnel, '_validate_download_url', lambda _: None):
                spec = tunnel._AssetSpec('fixture', 'fixture', 'fixture', 'raw', 10000, base+'/slow', 'not-the-fixture', 'not-the-fixture')
                started = time.monotonic()
                with patch.object(tunnel, 'DOWNLOAD_DEADLINE_SEC', .2):
                    with self.assertRaisesRegex(tunnel.CloudflaredError, 'whole-download deadline'):
                        await asyncio.to_thread(tunnel._download_asset, spec, Path(raw)/'partial')
                self.assertLess(time.monotonic()-started, 2)
                await asyncio.wait_for(ended.wait(), 2)
                digest = hashlib.sha256(b'verified').hexdigest()
                spec = tunnel._AssetSpec('fixture', 'fixture', 'fixture', 'raw', 8, base+'/fast', digest, digest)
                target = Path(raw)/'verified'
                await asyncio.to_thread(tunnel._download_asset, spec, target)
                self.assertEqual(target.read_bytes(), b'verified')
        finally:
            await runner.cleanup()


if __name__ == '__main__': unittest.main()
